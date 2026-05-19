#!/usr/bin/env python3
"""
extract.py v2 - typed events from fetches.

Three extraction paths:
  1. trends       → algorithmic (parse time series, emit proportional events, no LLM)
  2. general_feed → LLM with general prompt; re-attributes each event to a
                    specific candidate slug from the tracked list
  3. other        → LLM with per-candidate prompt (same as v1)
"""
import os
import re
import sys
import json
import sqlite3
import subprocess
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db.sqlite")
PROMPT_PER_CAND = os.path.join(HERE, "prompts", "extract.md")
PROMPT_GENERAL = os.path.join(HERE, "prompts", "extract_general.md")

SOURCE_TEXT_CAP = 45000
GENERAL_CANDIDATE_SLUG = "__general__"


class AuthError(RuntimeError):
    pass


def run_claude(prompt_text: str, model: str = "sonnet", timeout: int = 300) -> str:
    result = subprocess.run(
        ["claude", "-p", prompt_text, "--model", model, "--output-format", "text"],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        stderr_lc = (result.stderr or "").lower()
        auth_markers = ("login", "unauthorized", "authenticate", "token expired",
                        "not logged in", "credentials", "oauth")
        if any(m in stderr_lc for m in auth_markers):
            raise AuthError(f"claude auth failed: {result.stderr[:300]}")
        raise RuntimeError(f"claude CLI failed ({result.returncode}): {result.stderr[:500]}")
    return result.stdout


def load_prompt(path):
    with open(path) as f:
        return f.read()


def parse_events(text: str):
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if "event_type" not in obj or "evidence_quote" not in obj:
            continue
        yield obj


def insert_event(db, candidate_id, fetch_id, source_url, source_type, obj):
    try:
        confidence = float(obj.get("confidence", 0.7))
    except (TypeError, ValueError):
        confidence = 0.7
    try:
        magnitude = float(obj.get("magnitude", 1.0))
    except (TypeError, ValueError):
        magnitude = 1.0
    event_date = obj.get("event_date") or datetime.utcnow().strftime("%Y-%m-%d")
    try:
        if len(event_date) == 7:
            event_date = event_date + "-01"
        datetime.strptime(event_date, "%Y-%m-%d")
    except ValueError:
        event_date = datetime.utcnow().strftime("%Y-%m-%d")

    quote = f"[{source_type}] " + (obj["evidence_quote"] or "")
    db.execute(
        """INSERT INTO events
           (candidate_id, fetch_id, event_type, magnitude, event_date,
            evidence_quote, source_url, confidence)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            candidate_id, fetch_id,
            obj["event_type"], magnitude, event_date,
            quote[:300], source_url, confidence,
        ),
    )


# -- algorithmic trends extractor ------------------------------------

TRENDS_WEEKLY_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}):\s+(\d+)\s*$", re.MULTILINE)


def extract_trends_events(raw_text: str, fetch_date: str):
    events = []
    weekly = [(m.group(1), int(m.group(2))) for m in TRENDS_WEEKLY_RE.finditer(raw_text)]
    weekly = weekly[-26:]  # last 6 months only

    for date, val in weekly:
        if val < 10:
            continue
        events.append({
            "event_type": "mention",
            "magnitude": min(10.0, val / 10.0),
            "event_date": date,
            "evidence_quote": f"Google Trends interest={val} on {date}",
            "confidence": 0.85,
        })

    delta_match = re.search(r"DELTA_PCT:\s*([+-]?\d+\.?\d*)", raw_text)
    peak_match = re.search(r"PEAK_VALUE:\s*(\d+)\s+on\s+(\d{4}-\d{2}-\d{2})", raw_text)
    recent_match = re.search(r"RECENT_12W_AVG:\s*(\d+\.?\d*)", raw_text)

    if delta_match:
        delta = float(delta_match.group(1))
        if delta > 50:
            events.append({
                "event_type": "media", "magnitude": 3.0, "event_date": fetch_date,
                "evidence_quote": f"Google Trends: delta +{delta:.0f}% (strong acceleration)",
                "confidence": 0.85,
            })
        elif delta > 20:
            events.append({
                "event_type": "cohort", "magnitude": 2.0, "event_date": fetch_date,
                "evidence_quote": f"Google Trends: delta +{delta:.0f}% (moderate acceleration)",
                "confidence": 0.8,
            })
        elif delta < -20:
            events.append({
                "event_type": "disruption", "magnitude": -1.0, "event_date": fetch_date,
                "evidence_quote": f"Google Trends: delta {delta:.0f}% (decline)",
                "confidence": 0.8,
            })

    if peak_match and recent_match:
        peak = int(peak_match.group(1))
        recent = float(recent_match.group(1))
        if peak == 100 and recent > 60:
            events.append({
                "event_type": "mention", "magnitude": 5.0, "event_date": fetch_date,
                "evidence_quote": f"Google Trends: hot topic (peak=100, recent_avg={recent:.0f})",
                "confidence": 0.9,
            })

    return events


# -- general feed candidate list construction -----------------------

def build_candidate_list_for_prompt(db) -> str:
    """Produce a formatted string for the general-feed prompt's {candidate_list}."""
    rows = db.execute(
        """SELECT slug, display_name, notes FROM candidates
           WHERE status='tracking' AND slug != ?
           ORDER BY display_name""",
        (GENERAL_CANDIDATE_SLUG,),
    ).fetchall()
    lines = []
    for slug, name, notes in rows:
        note_str = f" — {notes[:80]}" if notes else ""
        lines.append(f"  {slug}: {name}{note_str}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--model", default="sonnet")
    args = ap.parse_args()

    db = sqlite3.connect(DB)
    prompt_per_cand = load_prompt(PROMPT_PER_CAND)
    prompt_general = load_prompt(PROMPT_GENERAL)

    # slug -> candidate_id mapping for general-feed re-attribution
    slug_to_id = {
        row[1]: row[0]
        for row in db.execute("SELECT id, slug FROM candidates WHERE status='tracking'")
    }

    rows = db.execute(
        """SELECT f.id, f.source_id, f.raw_text, f.fetched_at,
                  s.url, s.source_type, s.label, c.id, c.slug, c.display_name, c.category
           FROM fetches f
           JOIN sources s ON s.id = f.source_id
           JOIN candidates c ON c.id = s.candidate_id
           WHERE f.processed=0 AND f.raw_text IS NOT NULL
           ORDER BY f.fetched_at ASC LIMIT ?""",
        (args.limit,),
    ).fetchall()

    print(f"extract: {len(rows)} fetches to process, model={args.model}")
    total_events = 0
    trends_events = 0
    general_events = 0
    percand_events = 0
    general_orphans = 0  # emitted events with bad/missing candidate_slug

    for fid, sid, text, fetched_at, url, stype, slabel, cid, cslug, cname, category in rows:
        fetch_date = fetched_at.split(" ")[0] if fetched_at else datetime.utcnow().strftime("%Y-%m-%d")

        # Skip ingest-stub fetches
        if text.startswith("[") and "]" in text[:60] and len(text) < 200:
            db.execute("UPDATE fetches SET processed=1, error=? WHERE id=?",
                       (f"ingest stub: {text[:150]}", fid))
            db.commit()
            print(f"  skip fetch {fid}: ingest stub  [{cname}]")
            continue

        # -- trends: algorithmic --
        if stype == "trends":
            events = extract_trends_events(text, fetch_date)
            count = 0
            for obj in events:
                try:
                    insert_event(db, cid, fid, url, stype, obj)
                    count += 1
                except (sqlite3.IntegrityError, KeyError, ValueError):
                    pass
            db.execute("UPDATE fetches SET processed=1 WHERE id=?", (fid,))
            db.commit()
            total_events += count
            trends_events += count
            print(f"  ok  fetch {fid}: {count} trends events [{cname}]")
            continue

        # -- general feed: LLM with general prompt, re-attribute to slugs --
        if stype == "general_feed":
            cand_list = build_candidate_list_for_prompt(db)
            prompt = (
                prompt_general
                .replace("{source_label}", slabel or url)
                .replace("{source_url}", url)
                .replace("{fetch_date}", fetch_date)
                .replace("{candidate_list}", cand_list)
                .replace("{text}", text[:SOURCE_TEXT_CAP])
            )
            try:
                output = run_claude(prompt, model=args.model)
            except AuthError as e:
                print(f"\nFATAL: {e}", file=sys.stderr)
                sys.exit(2)
            except (subprocess.TimeoutExpired, RuntimeError) as e:
                print(f"  FAIL fetch {fid} (general {slabel}): {e}", file=sys.stderr)
                db.execute("UPDATE fetches SET error=? WHERE id=?", (str(e)[:500], fid))
                db.commit()
                continue

            count = 0
            for obj in parse_events(output):
                target_slug = obj.get("candidate_slug", "").strip()
                if target_slug not in slug_to_id or target_slug == GENERAL_CANDIDATE_SLUG:
                    general_orphans += 1
                    continue
                target_cid = slug_to_id[target_slug]
                try:
                    insert_event(db, target_cid, fid, url, f"general:{slabel[:20]}", obj)
                    count += 1
                except (sqlite3.IntegrityError, KeyError, ValueError):
                    pass
            db.execute("UPDATE fetches SET processed=1 WHERE id=?", (fid,))
            db.commit()
            total_events += count
            general_events += count
            print(f"  ok  fetch {fid}: {count} general events from [{slabel}]")
            continue

        # -- per-candidate: LLM with per-candidate prompt --
        prompt = (
            prompt_per_cand
            .replace("{candidate_name}", cname)
            .replace("{category}", category)
            .replace("{source_type}", stype)
            .replace("{source_url}", url)
            .replace("{fetch_date}", fetch_date)
            .replace("{text}", text[:SOURCE_TEXT_CAP])
        )
        try:
            output = run_claude(prompt, model=args.model)
        except AuthError as e:
            print(f"\nFATAL: {e}", file=sys.stderr)
            sys.exit(2)
        except (subprocess.TimeoutExpired, RuntimeError) as e:
            print(f"  FAIL fetch {fid} ({cname}): {e}", file=sys.stderr)
            db.execute("UPDATE fetches SET error=? WHERE id=?", (str(e)[:500], fid))
            db.commit()
            continue

        count = 0
        for obj in parse_events(output):
            try:
                insert_event(db, cid, fid, url, stype, obj)
                count += 1
            except (sqlite3.IntegrityError, KeyError, ValueError):
                pass
        db.execute("UPDATE fetches SET processed=1 WHERE id=?", (fid,))
        db.commit()
        total_events += count
        percand_events += count
        print(f"  ok  fetch {fid}: {count} events [{stype}] [{cname}]")

    print(f"\nextract: {total_events} events "
          f"({trends_events} trends, {percand_events} per-cand, {general_events} general; "
          f"{general_orphans} general orphans skipped) across {len(rows)} fetches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
