#!/usr/bin/env python3
"""
discover.py - Phase 2 trend-discovery layer.

Captures new trend candidates from prose surfaces (general feeds today;
Google related/rising and TikTok in Phase B+). Output goes into the
proposed_candidates + proposal_evidence tables, gated by operator
approval. Nothing routes off discovery until --promote.

See docs/phase-2-discovery-research.md for the architectural rationale.

Subcommands:
  --run             Iterate over unprocessed general-feed fetches; LLM
                    extracts proposals; dedup against existing
                    candidates and existing proposals; write rows.
  --list-pending    Show pending_approval proposals (mirrors trend_router).
  --approve <id>    Mark proposal approved.
  --reject <id>     Mark proposal rejected.
  --promote <id>    Create candidate row with status='observing' +
                    router_eligible_at = now + OBSERVATION_DAYS; seed
                    default sources (Google Trends only at this stage).
"""

import os
import re
import sys
import json
import sqlite3
import subprocess
import argparse
import random
import time
import urllib.parse
from datetime import datetime, timedelta

import requests

from ingest import BROWSER_UA

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db.sqlite")
PROMPT_DISCOVER_GENERAL = os.path.join(HERE, "prompts", "discover_general.md")
SOURCES_YAML = os.path.join(HERE, "sources.yaml")

SOURCE_TEXT_CAP = 45000
GENERAL_CANDIDATE_SLUG = "__general__"
OBSERVATION_DAYS = 28  # newly-promoted candidates wait 4 weeks before routing
GOOGLE_RELATED_SLEEP_RANGE = (3.0, 5.0)
GOOGLE_RELATED_NOISE_TERMS = {
    "nike", "adidas", "amazon", "review", "vs", "price", "discount", "near me",
}
GOOGLE_TRENDING_LIFESTYLE_KEYWORDS = {
    "fitness", "wellness", "food", "sport", "sports", "beauty", "hobby",
    "retreat", "club", "studio", "gym", "class", "training", "workout",
    "running", "run", "walking", "hiking", "cycling", "climbing", "yoga",
    "pilates", "sauna", "pickleball", "padel", "skincare", "makeup", "diet",
    "recipe", "coffee", "tea", "dance", "league", "tournament", "health",
}
TIKTOK_CREATIVE_CATEGORIES = (
    "fitness", "beauty_personal_care", "lifestyle", "food_beverage",
    "wellness_health",
)
TIKTOK_HASHTAG_STOPWORDS = {
    "fyp", "foryou", "foryoupage", "viral", "trending", "trend", "xyzcba",
    "tiktokmademebuyit", "tiktok", "duet", "stitch", "capcut",
}
HTTP_TIMEOUT = 20


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


def ensure_discovery_schema(db):
    """Idempotent migration. Safe to run on existing DBs."""
    cols = {row[1] for row in db.execute("PRAGMA table_info(candidates)").fetchall()}
    if "router_eligible_at" not in cols:
        db.execute("ALTER TABLE candidates ADD COLUMN router_eligible_at TIMESTAMP")

    db.execute(
        """CREATE TABLE IF NOT EXISTS proposed_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_slug TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            category TEXT,
            machine_explanation TEXT,
            support_count INTEGER DEFAULT 0,
            first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            proposal_status TEXT DEFAULT 'pending_approval',
            promoted_candidate_id INTEGER REFERENCES candidates(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS proposed_candidates_status_idx "
        "ON proposed_candidates(proposal_status, last_seen_at)"
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS proposal_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id INTEGER NOT NULL REFERENCES proposed_candidates(id) ON DELETE CASCADE,
            surface TEXT NOT NULL,
            seed_slug TEXT,
            source_url TEXT,
            raw_label TEXT,
            evidence_quote TEXT,
            event_date DATE,
            geo TEXT,
            confidence REAL DEFAULT 0.5,
            fetch_id INTEGER REFERENCES fetches(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS proposal_evidence_proposal_idx "
        "ON proposal_evidence(proposal_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS proposal_evidence_surface_idx "
        "ON proposal_evidence(surface, created_at)"
    )
    db.commit()


# -- normalization + dedup ------------------------------------------------

SLUG_RE = re.compile(r"[^a-z0-9_]+")


def normalize_slug(raw: str) -> str:
    """Reduce a proposal slug or display name to canonical form.

    Strips punctuation, lowercases, collapses whitespace to underscores,
    truncates to 30 chars. Two near-duplicates ("sound bath" and
    "sound bath studios") will share the same canonical_slug after
    stemming the trailing common nouns.
    """
    s = (raw or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    # Strip trailing noun suffixes so "X" and "X studios" cluster.
    for tail in ("_studios", "_studio", "_clubs", "_club", "_classes",
                 "_class", "_gyms", "_gym", "_bars", "_bar", "_lounges",
                 "_lounge", "_centers", "_center", "_houses", "_house",
                 "_practices", "_practice"):
        if s.endswith(tail) and len(s) > len(tail) + 3:
            s = s[: -len(tail)]
            break
    return s[:30]


def existing_candidate_slugs(db):
    rows = db.execute(
        "SELECT slug FROM candidates WHERE slug != ?",
        (GENERAL_CANDIDATE_SLUG,),
    ).fetchall()
    out = set()
    for (slug,) in rows:
        out.add(slug)
        out.add(normalize_slug(slug))
    return out


def existing_proposal_slugs(db):
    rows = db.execute(
        "SELECT canonical_slug FROM proposed_candidates"
    ).fetchall()
    return {row[0] for row in rows}


def display_name_from_label(raw: str) -> str:
    cleaned = re.sub(r"^[#r/]+", "", raw or "").strip()
    cleaned = re.sub(r"[_\-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.title() if cleaned else ""


def slug_variants(raw: str):
    slug = normalize_slug(raw)
    if not slug:
        return set()
    variants = {slug}
    if slug.endswith("ies") and len(slug) > 4:
        variants.add(slug[:-3] + "y")
    if slug.endswith("s") and not slug.endswith("ss") and len(slug) > 4:
        variants.add(slug[:-1])
    return variants


def parse_growth_value(value):
    """Return (numeric_growth_pct, label) for pytrends/TikTok growth fields."""
    if value is None:
        return None, "unknown"
    text = str(value).strip()
    if not text:
        return None, "unknown"
    if text.lower() == "breakout":
        return 5001.0, "Breakout"
    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None, text
    number = float(match.group(0))
    return number, f"{number:g}%"


def confidence_from_growth(value):
    growth, label = parse_growth_value(value)
    if label == "Breakout":
        return 0.85
    if growth is None:
        return 0.4
    if growth > 250:
        return 0.7
    if growth > 100:
        return 0.55
    return 0.4


def contains_configured_noise(text: str, terms) -> bool:
    q = (text or "").lower()
    tokens = set(re.findall(r"[a-z0-9]+", q))
    for term in terms:
        term_lc = term.lower()
        if " " in term_lc:
            if term_lc in q:
                return True
        elif term_lc in tokens:
            return True
    return False


def is_seed_synonym(query: str, seed_slug: str, seed_query: str = "") -> bool:
    q_variants = slug_variants(query)
    if not q_variants:
        return True
    seed_variants = slug_variants(seed_slug) | slug_variants(seed_query)
    return bool(q_variants & seed_variants)


def is_lifestyle_query(query: str) -> bool:
    q = (query or "").lower()
    tokens = set(re.findall(r"[a-z0-9]+", q))
    for keyword in GOOGLE_TRENDING_LIFESTYLE_KEYWORDS:
        if keyword in tokens or keyword in q:
            return True
    return False


def trends_url(query: str) -> str:
    return (
        "https://trends.google.com/trends/explore?geo=US&q="
        + urllib.parse.quote_plus(query or "")
    )


def emit_structured_proposal(db, *, skip_slugs, raw_slug, display_name, category,
                             explanation, surface, source_url, raw_label, quote,
                             confidence, dry_run=False, seed_slug=None, geo="US",
                             event_date=None):
    canonical = normalize_slug(raw_slug or display_name)
    if not canonical:
        return "skipped_invalid", None
    if canonical in skip_slugs:
        return "skipped_existing", canonical

    if dry_run:
        skip_slugs.add(canonical)
        print(f"  [proposal:{surface}] {canonical:<28s} {display_name[:42]:<42s} "
              f"conf={confidence:.2f}")
        return "new", canonical

    pid, is_new = upsert_proposal(
        db, canonical, display_name, category or "", explanation or ""
    )
    if pid is None:
        return "skipped_invalid", canonical
    insert_evidence(
        db, pid, surface=surface, source_url=source_url, raw_label=raw_label,
        quote=quote, event_date=event_date, confidence=confidence,
        seed_slug=seed_slug, geo=geo,
    )
    skip_slugs.add(canonical)
    return ("new" if is_new else "bumped"), canonical


def bump_summary(summary, outcome):
    if outcome == "new":
        summary["proposals_new"] += 1
        summary["evidence_rows"] += 1
    elif outcome == "bumped":
        summary["proposals_bumped"] += 1
        summary["evidence_rows"] += 1
    elif outcome == "skipped_existing":
        summary["skipped_existing"] += 1
    elif outcome == "skipped_invalid":
        summary["skipped_invalid"] += 1


def compact_count(value):
    if value is None or value == "":
        return "unknown"
    try:
        n = float(str(value).replace(",", ""))
    except ValueError:
        return str(value)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:g}"


# -- general-feed discovery adapter --------------------------------------

def build_tracked_slugs_block(db) -> str:
    rows = db.execute(
        """SELECT slug, display_name FROM candidates
           WHERE status IN ('tracking', 'observing', 'promoted')
             AND slug != ?
           ORDER BY display_name""",
        (GENERAL_CANDIDATE_SLUG,),
    ).fetchall()
    return "\n".join(f"  - {slug}: {name}" for slug, name in rows)


def load_prompt(path):
    with open(path) as f:
        return f.read()


def parse_proposals(text: str):
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
        if obj.get("type") != "proposal":
            continue
        if "canonical_slug" not in obj or "display_name" not in obj:
            continue
        yield obj


def upsert_proposal(db, raw_slug, display_name, category, explanation):
    """INSERT or bump existing pending proposal. Returns (proposal_id, is_new)."""
    canonical = normalize_slug(raw_slug or display_name)
    if not canonical:
        return None, False

    existing = db.execute(
        "SELECT id, support_count FROM proposed_candidates WHERE canonical_slug=?",
        (canonical,),
    ).fetchone()
    if existing:
        pid, support = existing
        db.execute(
            """UPDATE proposed_candidates
               SET last_seen_at=CURRENT_TIMESTAMP,
                   support_count=support_count+1,
                   display_name=COALESCE(NULLIF(?,''), display_name),
                   category=COALESCE(NULLIF(?,''), category),
                   machine_explanation=COALESCE(NULLIF(?,''), machine_explanation)
               WHERE id=?""",
            (display_name, category or "", explanation or "", pid),
        )
        return pid, False

    cur = db.execute(
        """INSERT INTO proposed_candidates
           (canonical_slug, display_name, category, machine_explanation, support_count)
           VALUES (?,?,?,?,1)""",
        (canonical, display_name, category or "", explanation or ""),
    )
    return cur.lastrowid, True


def insert_evidence(db, proposal_id, surface, source_url, raw_label, quote,
                    event_date=None, confidence=0.5, fetch_id=None,
                    seed_slug=None, geo=None):
    db.execute(
        """INSERT INTO proposal_evidence
           (proposal_id, surface, seed_slug, source_url, raw_label,
            evidence_quote, event_date, geo, confidence, fetch_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (proposal_id, surface, seed_slug, source_url, raw_label,
         (quote or "")[:500], event_date, geo, float(confidence or 0.5),
         fetch_id),
    )


# -- runner --------------------------------------------------------------

def run_general_feed_discovery(db, limit=30, model="sonnet", dry_run=False):
    """Read unprocessed general-feed fetches, run LLM discovery prompt,
    write proposals + evidence. Returns summary dict."""
    prompt_template = load_prompt(PROMPT_DISCOVER_GENERAL)
    tracked_slugs_block = build_tracked_slugs_block(db)
    skip_slugs = existing_candidate_slugs(db) | existing_proposal_slugs(db)

    # general-feed fetches are owned by the __general__ candidate
    rows = db.execute(
        """SELECT f.id, f.raw_text, f.fetched_at, s.url, s.label
           FROM fetches f
           JOIN sources s ON s.id = f.source_id
           JOIN candidates c ON c.id = s.candidate_id
           WHERE c.slug = ?
             AND f.raw_text IS NOT NULL
             AND f.id NOT IN (
               SELECT DISTINCT fetch_id FROM proposal_evidence WHERE fetch_id IS NOT NULL
             )
           ORDER BY f.fetched_at DESC
           LIMIT ?""",
        (GENERAL_CANDIDATE_SLUG, limit),
    ).fetchall()

    print(f"discover.general_feed: {len(rows)} fetches to scan (dry_run={dry_run})")
    proposals_new = 0
    proposals_bumped = 0
    evidence_rows = 0
    skipped_existing = 0

    for fid, text, fetched_at, src_url, src_label in rows:
        prompt = (
            prompt_template
            .replace("{source_label}", src_label or "")
            .replace("{source_url}", src_url or "")
            .replace("{fetch_date}", (fetched_at or "").split(" ")[0])
            .replace("{tracked_slugs_block}", tracked_slugs_block)
            .replace("{text}", (text or "")[:SOURCE_TEXT_CAP])
        )
        try:
            output = run_claude(prompt, model=model)
        except AuthError as e:
            print(f"\nFATAL: {e}", file=sys.stderr)
            print("Aborting; re-login as this user and retry.", file=sys.stderr)
            return None
        except (subprocess.TimeoutExpired, RuntimeError) as e:
            print(f"  FAIL fetch {fid}: {e}", file=sys.stderr)
            continue

        per_fetch = 0
        for obj in parse_proposals(output):
            slug = obj.get("canonical_slug", "")
            display = obj.get("display_name", "").strip()
            canonical = normalize_slug(slug or display)
            if not canonical:
                continue
            if canonical in skip_slugs:
                skipped_existing += 1
                continue
            if dry_run:
                proposals_new += 1
                print(f"  [proposal] {canonical:<28s} {display[:40]:<40s} "
                      f"conf={obj.get('confidence', 0.5):.2f}  src={src_label}")
                continue
            pid, is_new = upsert_proposal(
                db, slug, display, obj.get("category", ""),
                obj.get("rationale", "")
            )
            if pid is None:
                continue
            insert_evidence(
                db, pid, surface="general_feed",
                source_url=src_url, raw_label=display,
                quote=obj.get("evidence_quote", ""),
                event_date=obj.get("event_date"),
                confidence=obj.get("confidence", 0.5),
                fetch_id=fid,
            )
            evidence_rows += 1
            if is_new:
                proposals_new += 1
                skip_slugs.add(canonical)
            else:
                proposals_bumped += 1
            per_fetch += 1

        print(f"  ok fetch {fid}: {per_fetch} proposals  [{src_label}]")
        if not dry_run:
            db.commit()

    summary = {
        "fetches_scanned": len(rows),
        "proposals_new": proposals_new,
        "proposals_bumped": proposals_bumped,
        "evidence_rows": evidence_rows,
        "skipped_existing": skipped_existing,
    }
    print(f"\ndiscover.general_feed: {summary}")
    return summary


# -- Google related/rising discovery adapter -----------------------------

class GoogleRelatedProvider:
    """Provider wrapper for pytrends related/rising and daily trending calls."""

    def __init__(self, geo="US", sleep_range=GOOGLE_RELATED_SLEEP_RANGE,
                 backoff_seconds=60):
        self.geo = geo
        self.sleep_range = sleep_range
        self.backoff_seconds = backoff_seconds
        self._client = None

    def _trend_client(self):
        if self._client is None:
            try:
                from pytrends.request import TrendReq
            except ImportError as exc:
                raise RuntimeError("pytrends not installed; install requirements.txt") from exc
            self._client = TrendReq(
                hl="en-US",
                tz=360,
                requests_args={"headers": {"User-Agent": BROWSER_UA}},
            )
        return self._client

    def _sleep(self):
        low, high = self.sleep_range
        if high <= 0:
            return
        time.sleep(random.uniform(low, high))

    def _call_with_backoff(self, fn):
        last_exc = None
        for attempt in range(2):
            self._sleep()
            try:
                return fn()
            except Exception as exc:
                last_exc = exc
                if "429" in str(exc) and attempt == 0:
                    time.sleep(self.backoff_seconds)
                    continue
                raise
        raise last_exc

    def rising_for(self, query):
        def do_call():
            client = self._trend_client()
            client.build_payload([query], timeframe="today 12-m", geo=self.geo)
            return client.related_queries()

        data = self._call_with_backoff(do_call)
        related = data.get(query) if isinstance(data, dict) else None
        if related is None and isinstance(data, dict) and data:
            related = next(iter(data.values()))
        if not related:
            return []
        rising = related.get("rising")
        if rising is None or getattr(rising, "empty", False):
            return []
        out = []
        for row in rising.to_dict("records"):
            raw_query = row.get("query")
            if raw_query:
                out.append((str(raw_query), row.get("value")))
        return out

    def trending_searches(self):
        def do_call():
            client = self._trend_client()
            if hasattr(client, "today_searches"):
                return client.today_searches(pn="united_states")
            return client.trending_searches(pn="united_states")

        df = self._call_with_backoff(do_call)
        if df is None or getattr(df, "empty", False):
            return []
        values = []
        for row in df.itertuples(index=False):
            if row:
                values.append(str(row[0]))
        return values


def google_related_reject_reason(query, seed_slug, seed_query, skip_slugs):
    if contains_configured_noise(query, GOOGLE_RELATED_NOISE_TERMS):
        return "noise"
    if is_seed_synonym(query, seed_slug, seed_query):
        return "seed_synonym"
    if normalize_slug(query) in skip_slugs:
        return "existing"
    return None


def run_google_related_discovery(db, limit=30, dry_run=False, provider=None):
    """Discover adjacent proposals from Google Trends related/rising queries.

    Also runs the broader daily Trending Searches surface as lower-confidence
    recall, filtered through a lightweight lifestyle keyword allowlist.
    """
    provider = provider or GoogleRelatedProvider()
    skip_slugs = existing_candidate_slugs(db) | existing_proposal_slugs(db)
    summary = {
        "seeds_scanned": 0,
        "trending_checked": 0,
        "proposals_new": 0,
        "proposals_bumped": 0,
        "evidence_rows": 0,
        "skipped_existing": 0,
        "skipped_invalid": 0,
        "filtered_noise": 0,
        "filtered_seed_synonym": 0,
        "filtered_non_lifestyle": 0,
        "provider_errors": 0,
    }

    rows = db.execute(
        """SELECT c.slug, c.display_name, c.category, s.url
           FROM candidates c
           JOIN sources s ON s.candidate_id=c.id
           WHERE c.status='tracking'
             AND c.slug != ?
             AND s.source_type='trends'
           ORDER BY c.slug""",
        (GENERAL_CANDIDATE_SLUG,),
    ).fetchall()

    print(f"discover.google_related: {len(rows)} trend seeds (dry_run={dry_run})")
    emitted = 0
    for seed_slug, seed_name, category, seed_query in rows:
        if emitted >= limit:
            break
        summary["seeds_scanned"] += 1
        try:
            rising = provider.rising_for(seed_query)
        except Exception as exc:
            summary["provider_errors"] += 1
            print(f"  FAIL google_related seed={seed_slug}: {type(exc).__name__}: {str(exc)[:140]}",
                  file=sys.stderr)
            continue

        for raw_query, growth_value in rising:
            if emitted >= limit:
                break
            reason = google_related_reject_reason(raw_query, seed_slug, seed_query, skip_slugs)
            if reason == "noise":
                summary["filtered_noise"] += 1
                continue
            if reason == "seed_synonym":
                summary["filtered_seed_synonym"] += 1
                continue
            if reason == "existing":
                summary["skipped_existing"] += 1
                continue

            growth, growth_label = parse_growth_value(growth_value)
            conf = confidence_from_growth(growth_value)
            quote = f"Google Trends rising related query for {seed_query}: {growth_label}"
            outcome, _ = emit_structured_proposal(
                db, skip_slugs=skip_slugs, raw_slug=raw_query,
                display_name=display_name_from_label(raw_query),
                category=category,
                explanation=f"Rising Google related query around {seed_name}.",
                surface="google_related",
                source_url=trends_url(seed_query),
                raw_label=raw_query,
                quote=quote,
                confidence=conf,
                dry_run=dry_run,
                seed_slug=seed_slug,
                geo="US",
                event_date=datetime.utcnow().strftime("%Y-%m-%d"),
            )
            bump_summary(summary, outcome)
            if outcome in {"new", "bumped"}:
                emitted += 1

    if emitted < limit:
        try:
            trending = provider.trending_searches()
        except Exception as exc:
            summary["provider_errors"] += 1
            print(f"  FAIL google_trending: {type(exc).__name__}: {str(exc)[:140]}",
                  file=sys.stderr)
            trending = []

        for raw_query in trending:
            if emitted >= limit:
                break
            summary["trending_checked"] += 1
            if not is_lifestyle_query(raw_query):
                summary["filtered_non_lifestyle"] += 1
                continue
            if contains_configured_noise(raw_query, GOOGLE_RELATED_NOISE_TERMS):
                summary["filtered_noise"] += 1
                continue
            if normalize_slug(raw_query) in skip_slugs:
                summary["skipped_existing"] += 1
                continue
            outcome, _ = emit_structured_proposal(
                db, skip_slugs=skip_slugs, raw_slug=raw_query,
                display_name=display_name_from_label(raw_query),
                category="lifestyle",
                explanation="Daily Google trending search with lifestyle keywords.",
                surface="google_trending",
                source_url=trends_url(raw_query),
                raw_label=raw_query,
                quote="Daily Google trending search; lower-confidence broad surface.",
                confidence=0.35,
                dry_run=dry_run,
                geo="US",
                event_date=datetime.utcnow().strftime("%Y-%m-%d"),
            )
            bump_summary(summary, outcome)
            if outcome in {"new", "bumped"}:
                emitted += 1

    if not dry_run:
        db.commit()
    print(f"\ndiscover.google_related: {summary}")
    return summary


# -- TikTok Creative Center discovery adapter ----------------------------

class TikTokCreativeProvider:
    """Provider wrapper for TikTok Creative Center hashtag trend data."""

    API_ENDPOINTS = (
        "https://ads.tiktok.com/creative_radar_api/v1/popular_trend/hashtag/list",
        "https://ads.tiktok.com/business/creativecenter/api/v1/inspiration/popular/hashtag/list",
        "https://ads.tiktok.com/business/creativecenter/api/v1/inspiration/popular/hashtag/pc/list",
    )
    HTML_URL = "https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en"

    def __init__(self, region="US", period_days=7, session=None, backoff_seconds=60):
        self.region = region
        self.period_days = period_days
        self.backoff_seconds = backoff_seconds
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": BROWSER_UA,
            "Accept": "application/json,text/plain,text/html,*/*",
            "Referer": self.HTML_URL,
            "Origin": "https://ads.tiktok.com",
        })

    def trending_hashtags(self, category, limit=30):
        records = self._from_api(category, limit)
        if records:
            return records
        return self._from_html(category, limit)

    def _from_api(self, category, limit):
        params = {
            "period": self.period_days,
            "country_code": self.region,
            "region": self.region,
            "category_name": category,
            "industry_name": category,
            "industry_id": category,
            "page": 1,
            "limit": limit,
        }
        for endpoint in self.API_ENDPOINTS:
            try:
                response = self.session.get(endpoint, params=params, timeout=HTTP_TIMEOUT)
                if response.status_code == 429:
                    time.sleep(self.backoff_seconds)
                    response = self.session.get(endpoint, params=params, timeout=HTTP_TIMEOUT)
                response.raise_for_status()
                payload = response.json()
            except Exception:
                continue
            if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200, 40000):
                continue
            records = self._extract_hashtag_records(payload, category)
            if records:
                return records[:limit]
        return []

    def _from_html(self, category, limit):
        try:
            response = self.session.get(self.HTML_URL, timeout=HTTP_TIMEOUT)
            if response.status_code == 429:
                time.sleep(self.backoff_seconds)
                response = self.session.get(self.HTML_URL, timeout=HTTP_TIMEOUT)
            response.raise_for_status()
        except Exception:
            return []

        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            response.text,
        )
        if not match:
            return []
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
        return self._extract_hashtag_records(payload, category)[:limit]

    def _extract_hashtag_records(self, payload, category):
        out = []
        seen = set()

        def walk(node):
            if isinstance(node, dict):
                raw = (
                    node.get("hashtag_name") or node.get("hashtag") or
                    node.get("challenge_name") or node.get("keyword") or
                    node.get("name") or node.get("title")
                )
                if isinstance(raw, str):
                    tag = raw.lstrip("#").strip()
                    if tag and tag.lower() not in seen:
                        seen.add(tag.lower())
                        out.append({
                            "hashtag": tag,
                            "growth_pct": (
                                node.get("growth_pct") or node.get("growth") or
                                node.get("growth_rate") or node.get("post_change") or
                                node.get("publish_cnt_change") or
                                node.get("index_change")
                            ),
                            "post_count": (
                                node.get("post_count") or node.get("posts") or
                                node.get("publish_cnt") or node.get("video_count")
                            ),
                            "view_count": (
                                node.get("view_count") or node.get("views") or
                                node.get("video_views") or node.get("hashtag_vv") or
                                node.get("vv")
                            ),
                            "category": category,
                            "source": "tiktok_creative_center",
                        })
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)
        return out


def tiktok_hashtag_reject_reason(hashtag, skip_slugs):
    tag = (hashtag or "").lstrip("#").strip()
    tag_lc = tag.lower()
    if len(tag) < 4 or len(tag) > 35:
        return "length"
    if tag_lc in TIKTOK_HASHTAG_STOPWORDS:
        return "platform_jargon"
    if normalize_slug(tag) in skip_slugs:
        return "existing"
    return None


def format_tiktok_evidence(record):
    growth_value = record.get("growth_pct")
    _, growth_label = parse_growth_value(growth_value)
    if growth_label == "unknown":
        growth_part = "Posts growth unknown / 7d"
    elif growth_label == "Breakout":
        growth_part = "Posts Breakout / 7d"
    else:
        growth_part = f"Posts +{growth_label} / 7d"
    views = compact_count(record.get("view_count"))
    if views != "unknown":
        return f"{growth_part}, {views} views"
    posts = compact_count(record.get("post_count"))
    if posts != "unknown":
        return f"{growth_part}, {posts} posts"
    return growth_part


def run_tiktok_creative_discovery(db, limit=30, dry_run=False, provider=None):
    provider = provider or TikTokCreativeProvider()
    skip_slugs = existing_candidate_slugs(db) | existing_proposal_slugs(db)
    summary = {
        "categories_scanned": 0,
        "hashtags_checked": 0,
        "proposals_new": 0,
        "proposals_bumped": 0,
        "evidence_rows": 0,
        "skipped_existing": 0,
        "skipped_invalid": 0,
        "filtered_length": 0,
        "filtered_platform_jargon": 0,
        "provider_errors": 0,
    }

    print(f"discover.tiktok: {len(TIKTOK_CREATIVE_CATEGORIES)} categories (dry_run={dry_run})")
    emitted = 0
    for category in TIKTOK_CREATIVE_CATEGORIES:
        if emitted >= limit:
            break
        summary["categories_scanned"] += 1
        try:
            records = provider.trending_hashtags(category, limit=limit)
        except Exception as exc:
            summary["provider_errors"] += 1
            print(f"  FAIL tiktok category={category}: {type(exc).__name__}: {str(exc)[:140]}",
                  file=sys.stderr)
            continue

        for record in records:
            if emitted >= limit:
                break
            if isinstance(record, str):
                record = {"hashtag": record, "category": category}
            hashtag = (record.get("hashtag") or record.get("raw_label") or "").lstrip("#")
            summary["hashtags_checked"] += 1
            reason = tiktok_hashtag_reject_reason(hashtag, skip_slugs)
            if reason == "length":
                summary["filtered_length"] += 1
                continue
            if reason == "platform_jargon":
                summary["filtered_platform_jargon"] += 1
                continue
            if reason == "existing":
                summary["skipped_existing"] += 1
                continue

            conf = confidence_from_growth(record.get("growth_pct"))
            if conf < 0.45:
                conf = 0.45
            raw_label = "#" + hashtag
            outcome, _ = emit_structured_proposal(
                db, skip_slugs=skip_slugs, raw_slug=hashtag,
                display_name=display_name_from_label(hashtag),
                category=(record.get("category") or category).replace("_", " "),
                explanation="Trending TikTok Creative Center hashtag in a lifestyle category.",
                surface="tiktok_creative_center",
                source_url=TikTokCreativeProvider.HTML_URL,
                raw_label=raw_label,
                quote=format_tiktok_evidence(record),
                confidence=conf,
                dry_run=dry_run,
                geo="US",
                event_date=datetime.utcnow().strftime("%Y-%m-%d"),
            )
            bump_summary(summary, outcome)
            if outcome in {"new", "bumped"}:
                emitted += 1

    if not dry_run:
        db.commit()
    print(f"\ndiscover.tiktok: {summary}")
    return summary


# -- CLI subcommands ------------------------------------------------------

def list_pending(db):
    rows = db.execute(
        """SELECT id, canonical_slug, display_name, category, support_count,
                  first_seen_at, last_seen_at
           FROM proposed_candidates
           WHERE proposal_status='pending_approval'
           ORDER BY support_count DESC, last_seen_at DESC"""
    ).fetchall()
    if not rows:
        print("discover: no pending proposals")
        return
    print(f"{'id':>4s} {'canonical_slug':<28s} {'display_name':<32s} "
          f"{'category':<18s} {'spt':>4s}  last_seen")
    for rid, slug, name, category, support, first, last in rows:
        last_short = (last or "")[:10]
        print(f"{rid:>4d} {slug:<28s} {(name or '')[:32]:<32s} "
              f"{(category or '')[:18]:<18s} {support:>4d}  {last_short}")


def show_proposal(db, proposal_id):
    row = db.execute(
        """SELECT id, canonical_slug, display_name, category, machine_explanation,
                  support_count, first_seen_at, last_seen_at, proposal_status
           FROM proposed_candidates WHERE id=?""",
        (proposal_id,),
    ).fetchone()
    if not row:
        print(f"discover: no proposal id={proposal_id}", file=sys.stderr)
        return 1
    pid, slug, name, category, expl, support, first, last, status = row
    print(f"id:         {pid}")
    print(f"slug:       {slug}")
    print(f"display:    {name}")
    print(f"category:   {category}")
    print(f"status:     {status}")
    print(f"support:    {support}")
    print(f"first_seen: {first}")
    print(f"last_seen:  {last}")
    print(f"rationale:  {expl}")
    print()
    print("evidence:")
    ev_rows = db.execute(
        """SELECT surface, source_url, raw_label, evidence_quote, confidence, event_date
           FROM proposal_evidence WHERE proposal_id=? ORDER BY created_at""",
        (pid,),
    ).fetchall()
    for surface, url, raw_label, quote, conf, date in ev_rows:
        date_str = (date or "?")
        print(f"  [{surface}] {date_str} conf={conf:.2f}")
        print(f"    raw:    {raw_label}")
        if url:
            print(f"    url:    {url}")
        if quote:
            print(f"    quote:  {quote[:200]}")
    return 0


def update_proposal_status(db, proposal_id, status):
    row = db.execute(
        "SELECT id, proposal_status FROM proposed_candidates WHERE id=?",
        (proposal_id,),
    ).fetchone()
    if not row:
        print(f"discover: no proposal id={proposal_id}", file=sys.stderr)
        return 1
    if row[1] == status:
        print(f"discover: id={proposal_id} already {status}")
        return 0
    db.execute(
        "UPDATE proposed_candidates SET proposal_status=? WHERE id=?",
        (status, proposal_id),
    )
    db.commit()
    print(f"discover: id={proposal_id} -> {status}")
    return 0


def promote_proposal(db, proposal_id, observation_days=OBSERVATION_DAYS):
    """Create the actual candidate row + seed default Trends source.

    Per docs/phase-2-discovery-research.md §"Rollout sequence":
    - Status starts as 'observing' (not 'tracking')
    - router_eligible_at = now + observation_days
    - Seed only Google Trends + general-feed coverage; Reddit/Places
      added by operator later if signal develops.
    """
    row = db.execute(
        """SELECT canonical_slug, display_name, category, proposal_status,
                  promoted_candidate_id
           FROM proposed_candidates WHERE id=?""",
        (proposal_id,),
    ).fetchone()
    if not row:
        print(f"discover: no proposal id={proposal_id}", file=sys.stderr)
        return 1
    canonical, display, category, status, promoted_cid = row
    if status == "rejected":
        print(f"discover: id={proposal_id} is rejected; not promoting", file=sys.stderr)
        return 1
    if promoted_cid:
        print(f"discover: id={proposal_id} already promoted -> candidate id={promoted_cid}")
        return 0

    # Slug collision check (defensive — should have been caught during run)
    existing = db.execute(
        "SELECT id FROM candidates WHERE slug=?", (canonical,)
    ).fetchone()
    if existing:
        print(f"discover: candidate slug={canonical} already exists (id={existing[0]}). "
              "Aborting promotion.", file=sys.stderr)
        return 1

    eligible_at = (datetime.utcnow() + timedelta(days=observation_days)).strftime("%Y-%m-%d %H:%M:%S")
    cur = db.execute(
        """INSERT INTO candidates
           (slug, display_name, category, stage, status, notes, router_eligible_at)
           VALUES (?,?,?,?,?,?,?)""",
        (
            canonical, display, category or "uncategorized",
            "very_early", "observing",
            f"Promoted from proposal id={proposal_id}; observation window until {eligible_at}",
            eligible_at,
        ),
    )
    new_cid = cur.lastrowid

    # Seed the new candidate with a Google Trends source. The query
    # uses the display name minus parenthetical / slash junk. Reddit
    # is intentionally NOT seeded at promotion time per the research
    # doc -- operator adds Reddit only after signal develops.
    trends_query = re.sub(r"\([^)]*\)", "", display)
    trends_query = re.sub(r"[/_]", " ", trends_query).strip().lower()
    db.execute(
        """INSERT OR IGNORE INTO sources (candidate_id, source_type, url, label)
           VALUES (?,?,?,?)""",
        (new_cid, "trends", trends_query, f"Google Trends: {trends_query}"),
    )

    db.execute(
        """UPDATE proposed_candidates
           SET proposal_status='promoted', promoted_candidate_id=?
           WHERE id=?""",
        (new_cid, proposal_id),
    )
    db.commit()

    print(f"discover: promoted id={proposal_id} -> candidate id={new_cid} "
          f"slug={canonical} status=observing eligible_at={eligible_at}")
    print(f"  seeded source: trends '{trends_query}'")
    print(f"  next steps: ingest will pick up the Trends source on next run; "
          f"add Reddit/Places sources to sources.yaml under '{canonical}:' "
          f"once signal develops.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--db", default=DB, help="SQLite db path")
    ap.add_argument("--run", action="store_true",
                    help="Run discovery adapters over unprocessed fetches")
    ap.add_argument("--limit", type=int, default=30,
                    help="Max fetches per adapter run")
    ap.add_argument("--model", default="sonnet",
                    help="Claude model for discovery prompt")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print proposals without writing to db")
    ap.add_argument("--list-pending", action="store_true",
                    help="List pending_approval proposals")
    ap.add_argument("--show", type=int, default=None,
                    help="Show one proposal with all evidence")
    ap.add_argument("--approve", type=int, default=None,
                    help="Mark proposal id approved")
    ap.add_argument("--reject", type=int, default=None,
                    help="Mark proposal id rejected")
    ap.add_argument("--promote", type=int, default=None,
                    help="Create candidate row from approved proposal id "
                         "with status=observing")
    ap.add_argument("--observation-days", type=int, default=OBSERVATION_DAYS,
                    help=f"Observation window after promotion (default {OBSERVATION_DAYS}d)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"FAIL: db not found: {args.db}", file=sys.stderr)
        return 1

    actions = [args.run, args.list_pending, args.show is not None,
               args.approve is not None, args.reject is not None,
               args.promote is not None]
    if sum(bool(a) for a in actions) != 1:
        ap.error("specify exactly one of --run / --list-pending / --show / "
                 "--approve / --reject / --promote")

    db = sqlite3.connect(args.db)
    ensure_discovery_schema(db)

    if args.run:
        summary = run_general_feed_discovery(
            db, limit=args.limit, model=args.model, dry_run=args.dry_run
        )
        return 0 if summary is not None else 2
    if args.list_pending:
        list_pending(db)
        return 0
    if args.show is not None:
        return show_proposal(db, args.show)
    if args.approve is not None:
        return update_proposal_status(db, args.approve, "approved")
    if args.reject is not None:
        return update_proposal_status(db, args.reject, "rejected")
    if args.promote is not None:
        return promote_proposal(db, args.promote, args.observation_days)

    return 0


if __name__ == "__main__":
    sys.exit(main())
