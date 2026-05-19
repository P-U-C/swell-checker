#!/usr/bin/env python3
"""
ingest.py v2 - fetch per-candidate sources + global general feeds.

Two passes:
  1. Per-candidate sources (Reddit, Trends) - one fetch per (candidate, source)
  2. General feeds - one fetch globally, tied to virtual candidate '__general__'
     At extract time, LLM determines which real candidate each item is about.

Source types:
  reddit        - unauthenticated JSON via old.reddit.com + browser UA
  trends        - Google Trends via pytrends
  general_feed  - RSS from broad culture/fitness publications
  rss / news    - legacy, still works
"""
import os
import sys
import time
import json
import yaml
import sqlite3
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db.sqlite")
SOURCES_PATH = os.path.join(HERE, "sources.yaml")

# Browser-like UA for Reddit; Google recognizes it, Reddit anti-bot accepts it for old.reddit
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
USER_AGENT = os.environ.get("SWELL_USER_AGENT", BROWSER_UA)

REDDIT_RATE_LIMIT_SECONDS = 3.0
DEDUP_HOURS = 6
MAX_FETCH_BYTES = 500_000

GENERAL_CANDIDATE_SLUG = "__general__"


def load_sources():
    with open(SOURCES_PATH) as f:
        return yaml.safe_load(f)


def ensure_general_candidate(db):
    """The general-feed fetches attach to a virtual candidate."""
    row = db.execute("SELECT id FROM candidates WHERE slug=?", (GENERAL_CANDIDATE_SLUG,)).fetchone()
    if row:
        return row[0]
    cur = db.execute(
        """INSERT INTO candidates (slug, display_name, category, status, notes)
           VALUES (?,?,?,?,?)""",
        (GENERAL_CANDIDATE_SLUG, "(general feeds)", "meta", "tracking",
         "Virtual candidate - general feeds get extracted and re-attributed at extract time"),
    )
    db.commit()
    return cur.lastrowid


def fetch_reddit(url: str) -> str:
    # Force old.reddit.com - cleaner anti-bot
    url = url.replace("www.reddit.com", "old.reddit.com").replace("://reddit.com", "://old.reddit.com")
    if "old.old.reddit.com" in url:
        url = url.replace("old.old.reddit.com", "old.reddit.com")
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    out = []
    try:
        for post in data.get("data", {}).get("children", []):
            p = post.get("data", {})
            out.append(f"TITLE: {p.get('title','')}")
            if p.get("selftext"):
                out.append(f"BODY: {p['selftext'][:2000]}")
            out.append(f"SCORE: {p.get('score',0)}  COMMENTS: {p.get('num_comments',0)}")
            out.append(f"AUTHOR: {p.get('author','')}  CREATED: {datetime.utcfromtimestamp(p.get('created_utc',0)).isoformat()}")
            out.append("---")
    except (KeyError, TypeError) as e:
        return f"[parse error: {e}]"
    return "\n".join(out)[:MAX_FETCH_BYTES]


def fetch_rss(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    import re
    items = re.findall(r"<item[^>]*>(.*?)</item>", raw, re.DOTALL | re.IGNORECASE)[:40]
    if not items:
        items = re.findall(r"<entry[^>]*>(.*?)</entry>", raw, re.DOTALL | re.IGNORECASE)[:40]
    out = []
    for item in items:
        title = re.search(r"<title[^>]*>(.*?)</title>", item, re.DOTALL | re.IGNORECASE)
        desc = re.search(r"<description[^>]*>(.*?)</description>", item, re.DOTALL | re.IGNORECASE)
        if not desc:
            desc = re.search(r"<summary[^>]*>(.*?)</summary>", item, re.DOTALL | re.IGNORECASE)
        if not desc:
            desc = re.search(r"<content[^>]*>(.*?)</content>", item, re.DOTALL | re.IGNORECASE)
        pub = re.search(r"<pubDate[^>]*>(.*?)</pubDate>", item, re.DOTALL | re.IGNORECASE)
        if not pub:
            pub = re.search(r"<updated[^>]*>(.*?)</updated>", item, re.DOTALL | re.IGNORECASE)
        def clean(m):
            if not m:
                return ""
            t = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.DOTALL)
            return re.sub(r"<[^>]+>", "", t).strip()[:500]
        out.append(f"TITLE: {clean(title)}")
        out.append(f"DATE: {clean(pub)}")
        out.append(f"DESC: {clean(desc)}")
        out.append("---")
    return "\n".join(out)[:MAX_FETCH_BYTES]


def fetch_trends(query: str) -> str:
    try:
        from pytrends.request import TrendReq
    except ImportError:
        return "[pytrends not installed - skipping]"
    try:
        pt = TrendReq(hl="en-US", tz=360,
                      requests_args={"headers": {"User-Agent": USER_AGENT}})
        pt.build_payload([query], timeframe="today 12-m", geo="US")
        df = pt.interest_over_time()
        if df.empty:
            return f"[no trends data for '{query}']"
        recent = df[query].tail(12)
        baseline = df[query].head(12)
        delta = (recent.mean() - baseline.mean()) / max(1, baseline.mean()) * 100
        peak = df[query].max()
        peak_date = df[query].idxmax().strftime("%Y-%m-%d")
        out = [
            f"QUERY: {query}",
            f"PEAK_VALUE: {peak} on {peak_date}",
            f"RECENT_12W_AVG: {recent.mean():.1f}",
            f"BASELINE_12W_AVG: {baseline.mean():.1f}",
            f"DELTA_PCT: {delta:+.1f}%",
            "",
            "WEEKLY_VALUES:",
        ]
        for dt, val in df[query].items():
            out.append(f"  {dt.strftime('%Y-%m-%d')}: {val}")
        return "\n".join(out)
    except Exception as e:
        return f"[pytrends error: {e}]"


def ensure_source_row(db, candidate_id: int, source_type: str, url: str, label: str) -> int:
    row = db.execute(
        "SELECT id FROM sources WHERE candidate_id=? AND url=?",
        (candidate_id, url),
    ).fetchone()
    if row:
        return row[0]
    cur = db.execute(
        "INSERT INTO sources (candidate_id, source_type, url, label) VALUES (?,?,?,?)",
        (candidate_id, source_type, url, label),
    )
    db.commit()
    return cur.lastrowid


def fetch_one(src_type: str, url: str) -> str:
    if src_type == "reddit":
        return fetch_reddit(url)
    elif src_type in ("rss", "news", "general_feed"):
        return fetch_rss(url)
    elif src_type == "trends":
        return fetch_trends(url)
    else:
        return f"[source type {src_type} not implemented]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Max candidates to process")
    ap.add_argument("--skip-general", action="store_true")
    ap.add_argument("--only-general", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(DB)
    cfg = load_sources()

    cand_map = {
        row[1]: row[0]
        for row in db.execute("SELECT id, slug FROM candidates WHERE status='tracking'")
    }

    total = 0
    skipped = 0
    failed = 0
    last_reddit_call = 0.0

    # --- pass 1: general feeds (global, tied to __general__) ---
    if not args.skip_general and "general_feeds" in cfg:
        general_id = ensure_general_candidate(db)
        cand_map[GENERAL_CANDIDATE_SLUG] = general_id
        print(f"-- general feeds --")
        for src in cfg["general_feeds"]:
            source_id = ensure_source_row(
                db, general_id, src["type"], src["url"], src.get("label", "")
            )
            recent = db.execute(
                "SELECT id FROM fetches WHERE source_id=? AND fetched_at > ? LIMIT 1",
                (source_id, (datetime.utcnow() - timedelta(hours=DEDUP_HOURS)).isoformat()),
            ).fetchone()
            if recent:
                skipped += 1
                continue
            try:
                text = fetch_one(src["type"], src["url"])
                db.execute("INSERT INTO fetches (source_id, raw_text) VALUES (?,?)",
                           (source_id, text))
                db.execute("UPDATE sources SET last_fetched_at=CURRENT_TIMESTAMP, last_error=NULL WHERE id=?",
                           (source_id,))
                db.commit()
                total += 1
                print(f"  ok  __general__   [{src['type']}] ({len(text)}ch) {src.get('label','')}")
            except Exception as e:
                failed += 1
                db.execute("UPDATE sources SET last_error=? WHERE id=?",
                           (str(e)[:200], source_id))
                db.commit()
                print(f"  FAIL general feed {src.get('label','')}: {type(e).__name__}: {str(e)[:100]}",
                      file=sys.stderr)

    if args.only_general:
        print(f"\ningest (general only): fetched={total} skipped={skipped} failed={failed}")
        return 0

    # --- pass 2: per-candidate sources ---
    print(f"-- per-candidate --")
    candidates_processed = 0
    for slug, src_list in cfg.get("sources", {}).items():
        if slug not in cand_map:
            continue
        if args.limit and candidates_processed >= args.limit:
            break
        candidates_processed += 1
        cand_id = cand_map[slug]

        for src in src_list:
            source_id = ensure_source_row(
                db, cand_id, src["type"], src["url"], src.get("label", "")
            )
            recent = db.execute(
                "SELECT id FROM fetches WHERE source_id=? AND fetched_at > ? LIMIT 1",
                (source_id, (datetime.utcnow() - timedelta(hours=DEDUP_HOURS)).isoformat()),
            ).fetchone()
            if recent:
                skipped += 1
                continue

            if src["type"] == "reddit":
                delta = time.time() - last_reddit_call
                if delta < REDDIT_RATE_LIMIT_SECONDS:
                    time.sleep(REDDIT_RATE_LIMIT_SECONDS - delta)
                last_reddit_call = time.time()

            try:
                text = fetch_one(src["type"], src["url"])
                db.execute("INSERT INTO fetches (source_id, raw_text) VALUES (?,?)",
                           (source_id, text))
                db.execute("UPDATE sources SET last_fetched_at=CURRENT_TIMESTAMP, last_error=NULL WHERE id=?",
                           (source_id,))
                db.commit()
                total += 1
                print(f"  ok  {slug:30s} [{src['type']:12s}] ({len(text)}ch) {src.get('label','')}")
            except urllib.error.HTTPError as e:
                failed += 1
                db.execute("UPDATE sources SET last_error=? WHERE id=?",
                           (f"HTTP {e.code}: {e.reason}", source_id))
                db.commit()
                print(f"  FAIL {slug:30s} [{src['type']:12s}] HTTP {e.code}  {src['url']}",
                      file=sys.stderr)
            except Exception as e:
                failed += 1
                db.execute("UPDATE sources SET last_error=? WHERE id=?",
                           (str(e)[:200], source_id))
                db.commit()
                print(f"  FAIL {slug:30s} [{src['type']:12s}] {type(e).__name__}: {str(e)[:100]}",
                      file=sys.stderr)

    print(f"\ningest: fetched={total} skipped={skipped} failed={failed}")
    return 0 if total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
