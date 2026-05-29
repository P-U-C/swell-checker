#!/usr/bin/env python3
"""
discover.py - Phase 2 trend-discovery layer.

Captures new trend candidates from prose, search, social, and forum surfaces.
Output goes into the
proposed_candidates + proposal_evidence tables, gated by operator
approval. Nothing routes off discovery until --promote.

See docs/phase-2-discovery-research.md for the architectural rationale.

Subcommands:
  --run             Run enabled discovery adapters; dedup against existing
                    candidates and existing proposals; write rows.
  --list-pending    Show pending_approval proposals (mirrors trend_router).
  --approve <id>    Mark proposal approved.
  --reject <id>     Mark proposal rejected.
  --promote <id>    Create candidate row with status='observing' +
                    router_eligible_at = now + OBSERVATION_DAYS; seed
                    default sources (Google Trends only at this stage).
  --provider-status Show provider cooldown/failure/circuit-breaker state.
  --provider-reset  Clear disabled_until and failure count for one provider.
"""

import os
import re
import sys
import json
import html
import sqlite3
import subprocess
import argparse
import time
import urllib.parse
import threading
from datetime import date, datetime, timedelta

import requests
import yaml

from ingest import BROWSER_UA

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db.sqlite")
PROMPT_DISCOVER_GENERAL = os.path.join(HERE, "prompts", "discover_general.md")
SOURCES_YAML = os.path.join(HERE, "sources.yaml")

SOURCE_TEXT_CAP = 45000
GENERAL_CANDIDATE_SLUG = "__general__"
OBSERVATION_DAYS = 28  # newly-promoted candidates wait 4 weeks before routing
PROVIDER_POLICY = {
    "google_related_pytrends": {
        "cooldown_seconds": 90,
        "disable_threshold": 3,
        "disable_minutes": 1440,
    },
    "google_trending_pytrends": {
        "cooldown_seconds": 90,
        "disable_threshold": 3,
        "disable_minutes": 1440,
    },
    "tiktok_creative_center": {
        "cooldown_seconds": 60,
        "disable_threshold": 5,
        "disable_minutes": 360,
    },
    "reddit_subscriber_delta": {
        "cooldown_seconds": 30,
        "disable_threshold": 10,
        "disable_minutes": 60,
    },
}
DEFAULT_PROVIDER_POLICY = {
    "cooldown_seconds": 0,
    "disable_threshold": 3,
    "disable_minutes": 60,
}
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
REDDIT_GENERIC_SUBS = {
    "all", "popular", "askreddit", "funny", "news", "worldnews", "pics",
    "videos", "gaming",
}
REDDIT_LIFESTYLE_KEYWORDS = {
    "gym", "workout", "running", "runner", "wellness", "food", "drink",
    "social", "class", "studio", "practice", "club", "sport", "fitness",
    "health", "hiking", "cycling", "climbing", "yoga", "pilates", "sauna",
    "nutrition", "recipe", "coffee", "tea", "outdoor", "training", "league",
    "beauty", "skincare", "makeup", "sober",
}
HTTP_TIMEOUT = 20
REDDIT_SUBSCRIBER_GROWTH_THRESHOLD = 5.0
REDDIT_SUBSCRIBER_MIN_HISTORY_DAYS = 14
DEFAULT_ADAPTERS = ("general_feed", "google_related", "tiktok", "reddit_growing")
ADAPTER_PROVIDER_STATES = {
    "google_related": ("google_related_pytrends", "google_trending_pytrends"),
    "tiktok": ("tiktok_creative_center",),
    "reddit_growing": ("reddit_subscriber_delta",),
}
ADAPTER_ALIASES = {
    "all": "all",
    "general": "general_feed",
    "general_feed": "general_feed",
    "google": "google_related",
    "google_related": "google_related",
    "google_trending": "google_related",
    "tiktok": "tiktok",
    "tiktok_creative_center": "tiktok",
    "reddit": "reddit_growing",
    "reddit_growing": "reddit_growing",
}
GENERAL_HEURISTIC_KEYWORDS = {
    "at-home", "barre", "beauty", "breathwork", "climbing", "cold plunge",
    "community", "creatine", "dating", "fermentation", "fitness", "glp",
    "health", "hiking", "longevity", "menopause", "nutrition", "padel",
    "peptide", "pickleball", "pilates", "protein", "run club", "running",
    "sauna", "skincare", "sleep", "sober", "supplement", "therapy",
    "training", "walking", "wellness", "workout", "yoga",
}
GENERAL_HEURISTIC_REJECT_KEYWORDS = {
    "election", "game industry", "rockstar game", "stock market", "ukraine",
    "war",
}
GENERAL_HEURISTIC_CATEGORY_KEYWORDS = (
    ("beauty_tech", {"led", "skincare", "beauty", "makeup", "facial"}),
    ("boutique_fitness", {"barre", "pilates", "yoga", "workout", "training"}),
    ("fitness_social", {"run club", "running", "walking", "community"}),
    ("fitness_outdoor", {"climbing", "hiking", "bouldering", "outdoor"}),
    ("health_optimization", {
        "creatine", "glp", "health", "longevity", "menopause", "nutrition",
        "peptide", "protein", "supplement", "therapy", "wellness",
    }),
    ("food_beverage", {"coffee", "tea", "fermentation", "recipe"}),
    ("sober_social", {"sober", "sobriety", "stopdrinking"}),
)


class AuthError(RuntimeError):
    pass


def utcnow():
    return datetime.utcnow()


def parse_db_timestamp(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:26] if "." in text else text[:19], fmt)
        except ValueError:
            continue
    return None


def format_utc(value):
    dt = parse_db_timestamp(value)
    if not dt:
        return "never"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def age_label(value):
    dt = parse_db_timestamp(value)
    if not dt:
        return "never"
    seconds = max(0, int((utcnow() - dt).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def is_rate_limit_error(exc):
    text = f"{type(exc).__name__}: {exc}".lower()
    return "429" in text or "too many requests" in text or "ratelimit" in text


def env_int(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def google_related_max_seeds_per_run():
    return max(1, env_int("GOOGLE_RELATED_MAX_SEEDS_PER_RUN", 5))


def tiktok_categories():
    raw = os.environ.get("TIKTOK_CATEGORIES", "").strip()
    if not raw:
        return TIKTOK_CREATIVE_CATEGORIES
    parsed = tuple(part.strip() for part in raw.split(",") if part.strip())
    return parsed or TIKTOK_CREATIVE_CATEGORIES


def provider_policy(name, override=None):
    policy = dict(DEFAULT_PROVIDER_POLICY)
    policy.update(PROVIDER_POLICY.get(name, {}))
    if override:
        policy.update(override)
    return policy


def provider_action_hint(name):
    if name == "tiktok_creative_center":
        return "re-paste TIKTOK_COOKIE in .env and run `discover.py --provider-reset tiktok_creative_center`"
    if name.startswith("google_"):
        return f"wait for the cooldown or run `discover.py --provider-reset {name}` after confirming Google is reachable"
    if name == "reddit_subscriber_delta":
        return "check Reddit about.json reachability from the worker and run `discover.py --provider-reset reddit_subscriber_delta`"
    return f"inspect upstream credentials/network and run `discover.py --provider-reset {name}`"


def send_provider_disabled_alert(provider_name, row, disabled_until, error_message):
    if not os.environ.get("TG_BOT_TOKEN") or not os.environ.get("TG_CHAT_ID"):
        return
    last_success = row.get("last_success_at")
    text = "\n".join([
        "swell-checker [PROVIDER DISABLED]",
        f"provider: {provider_name}",
        f"last_success: {format_utc(last_success)} ({age_label(last_success)})",
        f"consecutive_failures: {row.get('consecutive_failures', 0)}",
        f"last_error: \"{str(error_message or '')[:500]}\"",
        f"disabled_until: {format_utc(disabled_until)}",
        f"action: {provider_action_hint(provider_name)}",
    ])

    def worker():
        try:
            import notify
            notify.send(text)
        except Exception as exc:
            print(f"provider_state: alert send failed: {exc}", file=sys.stderr)

    threading.Thread(target=worker, daemon=True).start()


class ProviderState:
    def __init__(self, db, name, policy=None):
        self.db = db
        self.name = name
        self.policy = provider_policy(name, policy)
        self.db.execute(
            """INSERT OR IGNORE INTO provider_state
               (provider_name, consecutive_failures, success_count, failure_count)
               VALUES (?, 0, 0, 0)""",
            (name,),
        )
        self.db.commit()

    def row(self):
        row = self.db.execute(
            """SELECT provider_name, last_success_at, last_failure_at,
                      last_failure_message, consecutive_failures, success_count,
                      failure_count, disabled_until, notes, updated_at
               FROM provider_state WHERE provider_name=?""",
            (self.name,),
        ).fetchone()
        if not row:
            return {}
        keys = (
            "provider_name", "last_success_at", "last_failure_at",
            "last_failure_message", "consecutive_failures", "success_count",
            "failure_count", "disabled_until", "notes", "updated_at",
        )
        return dict(zip(keys, row))

    def _last_call_at(self, row=None):
        row = row or self.row()
        candidates = [
            parse_db_timestamp(row.get("last_success_at")),
            parse_db_timestamp(row.get("last_failure_at")),
        ]
        candidates = [dt for dt in candidates if dt is not None]
        return max(candidates) if candidates else None

    def is_available(self):
        row = self.row()
        now = utcnow()
        disabled_until = parse_db_timestamp(row.get("disabled_until"))
        if disabled_until and disabled_until > now:
            return False
        cooldown = int(self.policy.get("cooldown_seconds", 0) or 0)
        last_call = self._last_call_at(row)
        if cooldown > 0 and last_call:
            return (now - last_call).total_seconds() >= cooldown
        return True

    def unavailable_reason(self):
        row = self.row()
        now = utcnow()
        disabled_until = parse_db_timestamp(row.get("disabled_until"))
        if disabled_until and disabled_until > now:
            seconds = int((disabled_until - now).total_seconds())
            return f"disabled until {format_utc(disabled_until)} ({seconds}s remaining)"
        remaining = self.cooldown_remaining_seconds()
        if remaining > 0:
            return f"cooldown {remaining}s remaining"
        return ""

    def cooldown_remaining_seconds(self):
        row = self.row()
        now = utcnow()
        disabled_until = parse_db_timestamp(row.get("disabled_until"))
        disabled_remaining = 0
        if disabled_until and disabled_until > now:
            disabled_remaining = int((disabled_until - now).total_seconds())
        cooldown = int(self.policy.get("cooldown_seconds", 0) or 0)
        cooldown_remaining = 0
        last_call = self._last_call_at(row)
        if cooldown > 0 and last_call:
            elapsed = int((now - last_call).total_seconds())
            cooldown_remaining = max(0, cooldown - elapsed)
        return max(disabled_remaining, cooldown_remaining)

    def record_success(self, notes=None):
        now = utcnow().strftime("%Y-%m-%d %H:%M:%S")
        self.db.execute(
            """UPDATE provider_state
               SET last_success_at=?,
                   consecutive_failures=0,
                   success_count=success_count+1,
                   disabled_until=NULL,
                   notes=COALESCE(?, notes),
                   updated_at=?
               WHERE provider_name=?""",
            (now, notes, now, self.name),
        )
        self.db.commit()

    def record_failure(self, error_message, disable_minutes=None, force_disable=False):
        before = self.row()
        now_dt = utcnow()
        now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        consecutive = int(before.get("consecutive_failures") or 0) + 1
        threshold = int(self.policy.get("disable_threshold", 1) or 1)
        minutes = int(
            disable_minutes
            if disable_minutes is not None
            else self.policy.get("disable_minutes", 60)
        )
        disabled_until = None
        if force_disable or consecutive >= threshold:
            disabled_until = now_dt + timedelta(minutes=minutes)
        disabled_until_str = (
            disabled_until.strftime("%Y-%m-%d %H:%M:%S")
            if disabled_until else before.get("disabled_until")
        )
        self.db.execute(
            """UPDATE provider_state
               SET last_failure_at=?,
                   last_failure_message=?,
                   consecutive_failures=?,
                   failure_count=failure_count+1,
                   disabled_until=?,
                   updated_at=?
               WHERE provider_name=?""",
            (now, str(error_message or "")[:1000], consecutive,
             disabled_until_str, now, self.name),
        )
        self.db.commit()

        previous_disabled = parse_db_timestamp(before.get("disabled_until"))
        was_disabled = previous_disabled and previous_disabled > now_dt
        if disabled_until and not was_disabled:
            after = self.row()
            send_provider_disabled_alert(
                self.name, after, disabled_until_str, error_message
            )

    def reset(self):
        now = utcnow().strftime("%Y-%m-%d %H:%M:%S")
        self.db.execute(
            """UPDATE provider_state
               SET consecutive_failures=0,
                   failure_count=0,
                   last_failure_message=NULL,
                   disabled_until=NULL,
                   updated_at=?
               WHERE provider_name=?""",
            (now, self.name),
        )
        self.db.commit()


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
    db.execute(
        """CREATE TABLE IF NOT EXISTS provider_state (
            provider_name TEXT PRIMARY KEY,
            last_success_at TIMESTAMP,
            last_failure_at TIMESTAMP,
            last_failure_message TEXT,
            consecutive_failures INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            disabled_until TIMESTAMP,
            notes TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS provider_seed_query_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_name TEXT NOT NULL,
            seed_slug TEXT NOT NULL,
            query_date DATE NOT NULL,
            success INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(provider_name, seed_slug, query_date)
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS provider_seed_query_history_idx "
        "ON provider_seed_query_history(provider_name, seed_slug, query_date)"
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS subreddit_subscriber_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subreddit TEXT NOT NULL,
            subscribers INTEGER NOT NULL,
            snapshot_date DATE NOT NULL,
            UNIQUE(subreddit, snapshot_date)
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS subreddit_subscriber_history_idx "
        "ON subreddit_subscriber_history(subreddit, snapshot_date)"
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


def clean_feed_text(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def feed_item_chunks(raw_text):
    for chunk in re.split(r"\s+---\s+", raw_text or ""):
        chunk = chunk.strip()
        if chunk:
            yield chunk


def parse_general_feed_item(chunk):
    title = ""
    date_text = ""
    desc = ""

    title_match = re.search(r"\bTITLE:\s*(.*?)(?:\s+DATE:|\s+DESC:|$)", chunk, re.S)
    if title_match:
        title = clean_feed_text(title_match.group(1))
    date_match = re.search(r"\bDATE:\s*(.*?)(?:\s+DESC:|$)", chunk, re.S)
    if date_match:
        date_text = clean_feed_text(date_match.group(1))
    desc_match = re.search(r"\bDESC:\s*(.*)$", chunk, re.S)
    if desc_match:
        desc = clean_feed_text(desc_match.group(1))

    if not title and not desc:
        return None
    return {"title": title, "date_text": date_text, "description": desc}


def keyword_hits(text, keywords):
    text_lc = (text or "").lower()
    hits = []
    for keyword in sorted(keywords, key=len, reverse=True):
        keyword_lc = keyword.lower()
        if " " in keyword_lc or "-" in keyword_lc:
            if keyword_lc in text_lc:
                hits.append(keyword)
        elif re.search(rf"\b{re.escape(keyword_lc)}\b", text_lc):
            hits.append(keyword)
    return hits


def title_segment_with_hits(title, hits):
    title = clean_feed_text(title)
    if not title:
        return ""
    segments = [
        segment.strip()
        for segment in re.split(r"\s+(?:[-\u2013\u2014]|:)\s+|[;|]", title)
        if segment.strip()
    ]
    for segment in segments:
        if keyword_hits(segment, hits):
            return segment
    return title


def heuristic_general_label(title, description, hits):
    label = title_segment_with_hits(title, hits)
    if not label:
        label = title_segment_with_hits(description, hits)
    label = re.sub(
        r"^(inside|why|how|what|the|a|an|all|guide to|in defense of)\s+",
        "",
        label,
        flags=re.I,
    )
    label = re.sub(
        r"\b(?:are|is|was|were)\s+(?:taking over|everywhere|the new|now|back)\b.*",
        "",
        label,
        flags=re.I,
    )
    label = re.sub(r"\b(?:taking over|goes mainstream)\b.*", "", label, flags=re.I)
    label = re.sub(r"\s+", " ", label).strip(" -" + "\u2013\u2014" + ":;,.")
    words = label.split()
    if len(words) > 8:
        label = " ".join(words[:8]).strip(" -" + "\u2013\u2014" + ":;,.")
    return label


def heuristic_general_category(text):
    text_lc = (text or "").lower()
    for category, keywords in GENERAL_HEURISTIC_CATEGORY_KEYWORDS:
        if keyword_hits(text_lc, keywords):
            return category
    return "lifestyle"


def general_feed_event_date(item, fetched_at):
    raw = " ".join([item.get("date_text") or "", fetched_at or ""])
    iso = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", raw)
    if iso:
        return iso.group(1)
    if fetched_at:
        return str(fetched_at).split(" ")[0]
    return datetime.utcnow().strftime("%Y-%m-%d")


def heuristic_general_feed_proposals(text, fetched_at, src_label):
    """Conservative no-LLM fallback for general-feed discovery.

    This is intentionally lower-recall than the Claude prompt. It only emits
    article-title proposals when the title/description contains explicit
    health, fitness, beauty, food, or lifestyle keywords.
    """
    proposals = []
    seen = set()
    for chunk in feed_item_chunks(text):
        item = parse_general_feed_item(chunk)
        if not item:
            continue
        haystack = " ".join([
            item.get("title") or "",
            item.get("description") or "",
            src_label or "",
        ])
        if keyword_hits(haystack, GENERAL_HEURISTIC_REJECT_KEYWORDS):
            continue
        hits = keyword_hits(haystack, GENERAL_HEURISTIC_KEYWORDS)
        if not hits:
            continue
        label = heuristic_general_label(
            item.get("title") or "",
            item.get("description") or "",
            hits,
        )
        canonical = normalize_slug(label)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        desc = item.get("description") or ""
        quote = item.get("title") or label
        if desc:
            quote = f"{quote} -- {desc[:260]}"
        confidence = min(0.5, 0.32 + (0.06 * min(len(hits), 3)))
        proposals.append({
            "type": "proposal",
            "canonical_slug": canonical,
            "display_name": display_name_from_label(label),
            "category": heuristic_general_category(haystack),
            "rationale": (
                "No-LLM heuristic matched explicit discovery keywords: "
                + ", ".join(hits[:5])
            ),
            "evidence_quote": quote,
            "event_date": general_feed_event_date(item, fetched_at),
            "confidence": confidence,
        })
    return proposals


# -- runner --------------------------------------------------------------

def run_general_feed_discovery(db, limit=30, model="sonnet", dry_run=False,
                               no_claude=False):
    """Read unprocessed general-feed fetches, run LLM discovery prompt,
    write proposals + evidence. Returns summary dict."""
    prompt_template = None if no_claude else load_prompt(PROMPT_DISCOVER_GENERAL)
    tracked_slugs_block = "" if no_claude else build_tracked_slugs_block(db)
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

    print(f"discover.general_feed: {len(rows)} fetches to scan "
          f"(dry_run={dry_run}, no_claude={no_claude})")
    summary = {
        "fetches_scanned": len(rows),
        "fetches_claude": 0,
        "fetches_heuristic": 0,
        "claude_auth_failures": 0,
        "claude_runtime_errors": 0,
        "proposals_new": 0,
        "proposals_bumped": 0,
        "evidence_rows": 0,
        "skipped_existing": 0,
        "skipped_invalid": 0,
    }
    claude_disabled = bool(no_claude)

    for fid, text, fetched_at, src_url, src_label in rows:
        proposals = []
        used_heuristic = False
        if claude_disabled:
            proposals = heuristic_general_feed_proposals(text, fetched_at, src_label)
            summary["fetches_heuristic"] += 1
            used_heuristic = True
        else:
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
                summary["fetches_claude"] += 1
                proposals = list(parse_proposals(output))
            except AuthError as exc:
                summary["claude_auth_failures"] += 1
                claude_disabled = True
                used_heuristic = True
                proposals = heuristic_general_feed_proposals(text, fetched_at, src_label)
                summary["fetches_heuristic"] += 1
                print(
                    f"  WARN general_feed fetch {fid}: {exc}; "
                    "using heuristic fallback for remaining fetches",
                    file=sys.stderr,
                )
            except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
                summary["claude_runtime_errors"] += 1
                used_heuristic = True
                proposals = heuristic_general_feed_proposals(text, fetched_at, src_label)
                summary["fetches_heuristic"] += 1
                print(
                    f"  WARN general_feed fetch {fid}: {exc}; "
                    "using heuristic fallback for this fetch",
                    file=sys.stderr,
                )

        per_fetch = 0
        for obj in proposals:
            slug = obj.get("canonical_slug", "")
            display = obj.get("display_name", "").strip()
            canonical = normalize_slug(slug or display)
            if not canonical:
                summary["skipped_invalid"] += 1
                continue
            if canonical in skip_slugs:
                summary["skipped_existing"] += 1
                continue
            if dry_run:
                summary["proposals_new"] += 1
                skip_slugs.add(canonical)
                print(f"  [proposal] {canonical:<28s} {display[:40]:<40s} "
                      f"conf={obj.get('confidence', 0.5):.2f}  src={src_label}")
                continue
            pid, is_new = upsert_proposal(
                db, slug, display, obj.get("category", ""),
                obj.get("rationale", "")
            )
            if pid is None:
                summary["skipped_invalid"] += 1
                continue
            insert_evidence(
                db, pid, surface="general_feed",
                source_url=src_url, raw_label=display,
                quote=obj.get("evidence_quote", ""),
                event_date=obj.get("event_date"),
                confidence=obj.get("confidence", 0.5),
                fetch_id=fid,
            )
            summary["evidence_rows"] += 1
            if is_new:
                summary["proposals_new"] += 1
                skip_slugs.add(canonical)
            else:
                summary["proposals_bumped"] += 1
            per_fetch += 1

        mode = "heuristic" if used_heuristic else "claude"
        print(f"  ok fetch {fid}: {per_fetch} proposals via {mode}  [{src_label}]")
        if not dry_run:
            db.commit()

    print(f"\ndiscover.general_feed: {summary}")
    return summary


# -- Google related/rising discovery adapter -----------------------------

class GoogleRelatedProvider:
    """Provider wrapper for pytrends related/rising and daily trending calls."""

    def __init__(self, geo="US"):
        self.geo = geo
        self._client = None
        self.rate_limited = False

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

    def _call(self, fn):
        if self.rate_limited:
            raise RuntimeError("Google Trends rate limited; skipping remaining calls")
        try:
            return fn()
        except Exception as exc:
            if is_rate_limit_error(exc):
                self.rate_limited = True
            raise

    def rising_for(self, query):
        def do_call():
            client = self._trend_client()
            client.build_payload([query], timeframe="today 12-m", geo=self.geo)
            return client.related_queries()

        data = self._call(do_call)
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
                try:
                    return client.today_searches(pn="united_states")
                except Exception as exc:
                    if "404" not in str(exc):
                        raise
            return client.trending_searches(pn="united_states")

        df = self._call(do_call)
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


def provider_seed_queried_on(db, provider_name, seed_slug, query_date=None):
    query_date = query_date or date.today().isoformat()
    row = db.execute(
        """SELECT id FROM provider_seed_query_history
           WHERE provider_name=? AND seed_slug=? AND query_date=?""",
        (provider_name, seed_slug, query_date),
    ).fetchone()
    return row is not None


def record_provider_seed_query(db, provider_name, seed_slug, success=True, query_date=None):
    query_date = query_date or date.today().isoformat()
    db.execute(
        """INSERT INTO provider_seed_query_history
           (provider_name, seed_slug, query_date, success)
           VALUES (?,?,?,?)
           ON CONFLICT(provider_name, seed_slug, query_date)
           DO UPDATE SET success=excluded.success""",
        (provider_name, seed_slug, query_date, 1 if success else 0),
    )
    db.commit()


def run_google_related_discovery(db, limit=30, dry_run=False, provider=None):
    """Discover adjacent proposals from Google Trends related/rising queries.

    Also runs the broader daily Trending Searches surface as lower-confidence
    recall, filtered through a lightweight lifestyle keyword allowlist.
    """
    provider = provider or GoogleRelatedProvider()
    related_state = ProviderState(db, "google_related_pytrends")
    trending_state = ProviderState(db, "google_trending_pytrends")
    skip_slugs = existing_candidate_slugs(db) | existing_proposal_slugs(db)
    summary = {
        "seeds_scanned": 0,
        "seeds_queried": 0,
        "seeds_skipped_today": 0,
        "provider_skipped": 0,
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
    max_seed_queries = google_related_max_seeds_per_run()
    for seed_slug, seed_name, category, seed_query in rows:
        if emitted >= limit:
            break
        if summary["seeds_queried"] >= max_seed_queries:
            print(f"  SKIP google_related: per-run seed cap reached ({max_seed_queries})")
            break
        summary["seeds_scanned"] += 1
        if provider_seed_queried_on(db, "google_related_pytrends", seed_slug):
            summary["seeds_skipped_today"] += 1
            continue
        if not related_state.is_available():
            summary["provider_skipped"] += 1
            print(f"  SKIP google_related seed={seed_slug}: {related_state.unavailable_reason()}")
            break
        try:
            rising = provider.rising_for(seed_query)
        except Exception as exc:
            summary["provider_errors"] += 1
            msg = f"{type(exc).__name__}: {str(exc)[:500]}"
            record_provider_seed_query(db, "google_related_pytrends", seed_slug, success=False)
            if is_rate_limit_error(exc):
                related_state.record_failure(msg, disable_minutes=1440, force_disable=True)
            else:
                related_state.record_failure(msg)
            print(f"  FAIL google_related seed={seed_slug}: {type(exc).__name__}: {str(exc)[:140]}",
                  file=sys.stderr)
            if "429" in str(exc) or "rate limited" in str(exc).lower():
                break
            break
        summary["seeds_queried"] += 1
        related_state.record_success(notes=f"seed={seed_slug} query={seed_query}")
        record_provider_seed_query(db, "google_related_pytrends", seed_slug, success=True)

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

    if emitted < limit and not getattr(provider, "rate_limited", False):
        try:
            if not trending_state.is_available():
                summary["provider_skipped"] += 1
                print(f"  SKIP google_trending: {trending_state.unavailable_reason()}")
                trending = []
            else:
                trending = provider.trending_searches()
                trending_state.record_success(notes="daily trending searches")
        except Exception as exc:
            summary["provider_errors"] += 1
            msg = f"{type(exc).__name__}: {str(exc)[:500]}"
            if is_rate_limit_error(exc):
                trending_state.record_failure(msg, disable_minutes=1440, force_disable=True)
            else:
                trending_state.record_failure(msg)
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
        headers = {
            "User-Agent": BROWSER_UA,
            "Accept": "application/json,text/plain,text/html,*/*",
            "Referer": self.HTML_URL,
            "Origin": "https://ads.tiktok.com",
        }
        cookie = os.environ.get("TIKTOK_COOKIE", "").strip()
        if cookie:
            headers["Cookie"] = cookie
        self.session.headers.update(headers)
        self.last_error = ""

    def trending_hashtags(self, category, limit=30):
        self.last_error = ""
        records = self._from_api(category, limit)
        if records:
            return records
        records = self._from_html(category, limit)
        if records:
            return records
        if not self.last_error:
            self.last_error = "empty TikTok Creative Center response"
        return []

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
                    self.last_error = "429 Too Many Requests from TikTok Creative Center"
                    time.sleep(self.backoff_seconds)
                    response = self.session.get(endpoint, params=params, timeout=HTTP_TIMEOUT)
                if response.status_code in (401, 403):
                    self.last_error = (
                        f"{response.status_code} Unauthorized - TIKTOK_COOKIE may have expired"
                    )
                    continue
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                continue
            if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200, 40000):
                self.last_error = f"TikTok API returned code={payload.get('code')}"
                continue
            records = self._extract_hashtag_records(payload, category)
            if records:
                return records[:limit]
        return []

    def _from_html(self, category, limit):
        try:
            response = self.session.get(self.HTML_URL, timeout=HTTP_TIMEOUT)
            if response.status_code == 429:
                self.last_error = "429 Too Many Requests from TikTok Creative Center HTML"
                time.sleep(self.backoff_seconds)
                response = self.session.get(self.HTML_URL, timeout=HTTP_TIMEOUT)
            if response.status_code in (401, 403):
                self.last_error = (
                    f"{response.status_code} Unauthorized - TIKTOK_COOKIE may have expired"
                )
                return []
            response.raise_for_status()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            return []

        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            response.text,
        )
        if not match:
            self.last_error = "TikTok HTML did not expose __NEXT_DATA__"
            return []
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            self.last_error = "TikTok HTML did not contain parseable __NEXT_DATA__"
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
    if tag_lc in TIKTOK_HASHTAG_STOPWORDS:
        return "platform_jargon"
    if len(tag) < 4 or len(tag) > 35:
        return "length"
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
    state = ProviderState(db, "tiktok_creative_center")
    categories = tiktok_categories()
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
        "provider_skipped": 0,
        "empty_responses": 0,
    }

    print(f"discover.tiktok: {len(categories)} categories (dry_run={dry_run})")
    emitted = 0
    for category in categories:
        if emitted >= limit:
            break
        if not state.is_available():
            summary["provider_skipped"] += 1
            print(f"  SKIP tiktok category={category}: {state.unavailable_reason()}")
            break
        summary["categories_scanned"] += 1
        try:
            records = provider.trending_hashtags(category, limit=limit)
        except Exception as exc:
            summary["provider_errors"] += 1
            state.record_failure(f"{type(exc).__name__}: {str(exc)[:500]}")
            print(f"  FAIL tiktok category={category}: {type(exc).__name__}: {str(exc)[:140]}",
                  file=sys.stderr)
            break
        if not records:
            summary["empty_responses"] += 1
            msg = getattr(provider, "last_error", "") or (
                f"empty TikTok Creative Center response for category={category}"
            )
            state.record_failure(msg)
            print(f"  SKIP tiktok category={category}: {msg}")
            break
        state.record_success(notes=f"category={category}")

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


# -- Reddit growing subreddit discovery adapter --------------------------

def load_reddit_discovery_seeds(path=SOURCES_YAML):
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return []
    seeds = []
    for item in data.get("discovery_reddit_seeds", []) or []:
        if not isinstance(item, dict):
            continue
        subreddit = normalize_subreddit_name(item.get("subreddit", ""))
        if not subreddit:
            continue
        seeds.append({
            "subreddit": subreddit,
            "category": item.get("category") or "reddit_lifestyle",
        })
    return seeds


class RedditSubscriberDeltaProvider:
    """Provider wrapper for watched-subreddit subscriber delta snapshots."""

    ABOUT_URL = "https://www.reddit.com/r/{subreddit}/about.json"

    def __init__(self, seeds=None, session=None):
        self.seeds = seeds if seeds is not None else load_reddit_discovery_seeds()
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": os.environ.get("SWELL_REDDIT_USER_AGENT", BROWSER_UA),
            "Accept": "application/json,text/plain,*/*",
        })
        self.last_error = ""
        self.stats = {
            "watched": len(self.seeds),
            "snapshots_recorded": 0,
            "fetch_failures": 0,
            "not_enough_history": 0,
            "below_growth_threshold": 0,
        }

    def emerging_subreddits(self, db, limit=30):
        today = date.today().isoformat()
        out = []
        self.last_error = ""
        self.stats.update({
            "watched": len(self.seeds),
            "snapshots_recorded": 0,
            "fetch_failures": 0,
            "not_enough_history": 0,
            "below_growth_threshold": 0,
        })
        for seed in self.seeds:
            if len(out) >= limit:
                break
            subreddit = normalize_subreddit_name(seed.get("subreddit", ""))
            if not subreddit:
                continue
            try:
                record = self.fetch_about(subreddit)
            except Exception as exc:
                self.stats["fetch_failures"] += 1
                self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                continue
            if not record:
                self.stats["fetch_failures"] += 1
                if not self.last_error:
                    self.last_error = f"empty Reddit about.json for r/{subreddit}"
                continue
            subscribers = record.get("subscribers")
            try:
                subscribers = int(subscribers)
            except (TypeError, ValueError):
                self.stats["fetch_failures"] += 1
                self.last_error = f"missing subscriber count for r/{subreddit}"
                continue
            upsert_subreddit_snapshot(db, subreddit, subscribers, today)
            self.stats["snapshots_recorded"] += 1

            baseline = subreddit_baseline_snapshot(db, subreddit, today)
            oldest = subreddit_oldest_snapshot_date(db, subreddit)
            if not baseline or not oldest:
                self.stats["not_enough_history"] += 1
                continue
            min_start = date.fromisoformat(today) - timedelta(days=REDDIT_SUBSCRIBER_MIN_HISTORY_DAYS)
            if date.fromisoformat(oldest) > min_start:
                self.stats["not_enough_history"] += 1
                continue
            baseline_date, baseline_subscribers = baseline
            if not baseline_subscribers or baseline_subscribers <= 0:
                self.stats["not_enough_history"] += 1
                continue
            growth_pct = ((subscribers - baseline_subscribers) / baseline_subscribers) * 100.0
            if growth_pct <= REDDIT_SUBSCRIBER_GROWTH_THRESHOLD:
                self.stats["below_growth_threshold"] += 1
                continue

            record.update({
                "name": subreddit,
                "display_name_prefixed": f"r/{subreddit}",
                "subscribers": subscribers,
                "growth_rate": growth_pct,
                "growth_pct": growth_pct,
                "baseline_date": baseline_date,
                "baseline_subscribers": baseline_subscribers,
                "category": seed.get("category") or "reddit_lifestyle",
                "source": "reddit_subscriber_delta",
            })
            out.append(record)
        db.commit()
        return out

    def fetch_about(self, subreddit):
        url = self.ABOUT_URL.format(subreddit=urllib.parse.quote(subreddit))
        response = self.session.get(url, timeout=HTTP_TIMEOUT)
        if response.status_code in (401, 403):
            self.last_error = f"{response.status_code} from Reddit about.json for r/{subreddit}"
            return None
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if not data:
            return None
        display_name = data.get("display_name") or subreddit
        return {
            "name": normalize_subreddit_name(display_name),
            "display_name_prefixed": data.get("display_name_prefixed") or f"r/{display_name}",
            "description": data.get("public_description") or data.get("title") or "",
            "subscribers": data.get("subscribers"),
        }


RedditGrowingProvider = RedditSubscriberDeltaProvider


def normalize_subreddit_name(raw):
    name = (raw or "").strip()
    name = re.sub(r"^/?r/", "", name, flags=re.I)
    name = name.strip().strip("/")
    return re.sub(r"[^A-Za-z0-9_]", "", name).lower()


def upsert_subreddit_snapshot(db, subreddit, subscribers, snapshot_date=None):
    snapshot_date = snapshot_date or date.today().isoformat()
    db.execute(
        """INSERT INTO subreddit_subscriber_history
           (subreddit, subscribers, snapshot_date)
           VALUES (?,?,?)
           ON CONFLICT(subreddit, snapshot_date)
           DO UPDATE SET subscribers=excluded.subscribers""",
        (normalize_subreddit_name(subreddit), int(subscribers), snapshot_date),
    )


def subreddit_oldest_snapshot_date(db, subreddit):
    row = db.execute(
        "SELECT MIN(snapshot_date) FROM subreddit_subscriber_history WHERE subreddit=?",
        (normalize_subreddit_name(subreddit),),
    ).fetchone()
    return row[0] if row and row[0] else None


def subreddit_baseline_snapshot(db, subreddit, today=None):
    today_date = date.fromisoformat(today) if isinstance(today, str) else (today or date.today())
    target = (today_date - timedelta(days=7)).isoformat()
    row = db.execute(
        """SELECT snapshot_date, subscribers
           FROM subreddit_subscriber_history
           WHERE subreddit=? AND snapshot_date <= ?
           ORDER BY snapshot_date DESC
           LIMIT 1""",
        (normalize_subreddit_name(subreddit), target),
    ).fetchone()
    return (row[0], row[1]) if row else None


def reddit_growing_reject_reason(record, skip_slugs):
    name = normalize_subreddit_name(record.get("name") or record.get("display_name_prefixed"))
    if not name:
        return "invalid"
    if name in REDDIT_GENERIC_SUBS:
        return "generic"
    if normalize_slug(name) in skip_slugs:
        return "existing"
    desc = " ".join([
        name,
        record.get("display_name_prefixed") or "",
        record.get("description") or "",
    ]).lower()
    if not any(keyword in desc for keyword in REDDIT_LIFESTYLE_KEYWORDS):
        return "non_lifestyle"
    return None


def format_reddit_evidence(record):
    desc = (record.get("description") or "").strip()
    subscribers = compact_count(record.get("subscribers"))
    growth = record.get("growth_rate")
    _, growth_label = parse_growth_value(growth)
    parts = []
    if desc:
        parts.append(desc[:220])
    if subscribers != "unknown":
        parts.append(f"{subscribers} subscribers")
    if growth_label != "unknown":
        parts.append(f"{growth_label} growth")
    else:
        parts.append("growth rate unavailable")
    return " | ".join(parts)


def run_reddit_growing_discovery(db, limit=30, dry_run=False, provider=None):
    provider = provider or RedditSubscriberDeltaProvider()
    state = ProviderState(db, "reddit_subscriber_delta")
    skip_slugs = existing_candidate_slugs(db) | existing_proposal_slugs(db)
    summary = {
        "subreddits_checked": 0,
        "snapshots_recorded": 0,
        "not_enough_history": 0,
        "below_growth_threshold": 0,
        "proposals_new": 0,
        "proposals_bumped": 0,
        "evidence_rows": 0,
        "skipped_existing": 0,
        "skipped_invalid": 0,
        "filtered_generic": 0,
        "filtered_non_lifestyle": 0,
        "provider_errors": 0,
        "provider_skipped": 0,
    }

    print(f"discover.reddit_growing: checking subscriber deltas (dry_run={dry_run})")
    if not state.is_available():
        summary["provider_skipped"] += 1
        print(f"  SKIP reddit_growing: {state.unavailable_reason()}")
        records = []
    else:
        try:
            if hasattr(provider, "emerging_subreddits"):
                records = provider.emerging_subreddits(db, limit=limit * 3)
                stats = getattr(provider, "stats", {}) or {}
                summary["snapshots_recorded"] = stats.get("snapshots_recorded", 0)
                summary["not_enough_history"] = stats.get("not_enough_history", 0)
                summary["below_growth_threshold"] = stats.get("below_growth_threshold", 0)
                if stats.get("snapshots_recorded", 0) > 0:
                    state.record_success(notes=f"snapshots={stats.get('snapshots_recorded', 0)}")
                else:
                    msg = getattr(provider, "last_error", "") or "no Reddit subscriber snapshots recorded"
                    state.record_failure(msg)
            else:
                records = provider.growing_subreddits(limit=limit * 3)
                state.record_success(notes="legacy injected provider")
        except Exception as exc:
            summary["provider_errors"] += 1
            state.record_failure(f"{type(exc).__name__}: {str(exc)[:500]}")
            print(f"  FAIL reddit_growing: {type(exc).__name__}: {str(exc)[:140]}",
                  file=sys.stderr)
            records = []

    emitted = 0
    for record in records:
        if emitted >= limit:
            break
        if isinstance(record, str):
            record = {"name": record, "display_name_prefixed": f"r/{record}"}
        summary["subreddits_checked"] += 1
        reason = reddit_growing_reject_reason(record, skip_slugs)
        if reason == "invalid":
            summary["skipped_invalid"] += 1
            continue
        if reason == "generic":
            summary["filtered_generic"] += 1
            continue
        if reason == "existing":
            summary["skipped_existing"] += 1
            continue
        if reason == "non_lifestyle":
            summary["filtered_non_lifestyle"] += 1
            continue

        name = normalize_subreddit_name(record.get("name") or record.get("display_name_prefixed"))
        conf = confidence_from_growth(record.get("growth_rate"))
        if conf < 0.35:
            conf = 0.35
        outcome, _ = emit_structured_proposal(
            db, skip_slugs=skip_slugs, raw_slug=name,
            display_name=f"r/{name}",
            category=record.get("category") or "reddit_lifestyle",
            explanation="Watched subreddit with subscriber growth above the discovery threshold.",
            surface="reddit_growing",
            source_url=f"https://www.reddit.com/r/{name}/",
            raw_label=f"r/{name}",
            quote=format_reddit_evidence(record),
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
    print(f"\ndiscover.reddit_growing: {summary}")
    return summary


def resolve_adapters(adapter_name):
    if adapter_name is None:
        return list(DEFAULT_ADAPTERS)
    resolved = ADAPTER_ALIASES.get(adapter_name)
    if resolved is None:
        raise ValueError(f"unknown adapter: {adapter_name}")
    if resolved == "all":
        return list(DEFAULT_ADAPTERS)
    return [resolved]


def provider_skip_summary(db, adapter):
    provider_names = ADAPTER_PROVIDER_STATES.get(adapter, ())
    if not provider_names:
        return None
    states = [ProviderState(db, name) for name in provider_names]
    if not all(not state.is_available() for state in states):
        return None
    reasons = "; ".join(
        f"{state.name}: {state.unavailable_reason()}" for state in states
    )
    return {
        "adapter_status": "skipped",
        "provider_skipped": 1,
        "skip_reason": reasons,
        "proposals_new": 0,
        "proposals_bumped": 0,
        "evidence_rows": 0,
    }


def adapter_failure_summary(exc):
    return {
        "adapter_status": "failed",
        "error_type": type(exc).__name__,
        "error_message": str(exc)[:500],
        "proposals_new": 0,
        "proposals_bumped": 0,
        "evidence_rows": 0,
    }


def adapter_summary_failed(summary):
    return bool(summary and summary.get("adapter_status") == "failed")


def discovery_run_exit_code(summaries):
    if not summaries:
        return 2
    if all(adapter_summary_failed(summary) for summary in summaries.values()):
        return 2
    return 0


def print_discovery_run_summary(summaries):
    print("\ndiscover.run: adapter summary")
    for adapter, summary in summaries.items():
        summary = summary or {}
        status = summary.get("adapter_status", "ok")
        new = int(summary.get("proposals_new") or 0)
        bumped = int(summary.get("proposals_bumped") or 0)
        evidence = int(summary.get("evidence_rows") or 0)
        extra = ""
        if status == "failed":
            extra = f" error={summary.get('error_type')}: {summary.get('error_message')}"
        elif summary.get("skip_reason"):
            extra = f" reason={summary.get('skip_reason')}"
        print(
            f"  {adapter:<16s} status={status:<7s} "
            f"new={new:<3d} bumped={bumped:<3d} evidence={evidence:<3d}{extra}"
        )


def run_discovery_adapters(db, adapters, limit_per_adapter=30, model="sonnet",
                           dry_run=False, no_claude=False):
    summaries = {}
    for adapter in adapters:
        skipped = provider_skip_summary(db, adapter)
        if skipped is not None:
            print(f"discover.{adapter}: SKIP {skipped['skip_reason']}")
            summary = skipped
        else:
            try:
                if adapter == "general_feed":
                    summary = run_general_feed_discovery(
                        db, limit=limit_per_adapter, model=model,
                        dry_run=dry_run, no_claude=no_claude,
                    )
                elif adapter == "google_related":
                    summary = run_google_related_discovery(
                        db, limit=limit_per_adapter, dry_run=dry_run
                    )
                elif adapter == "tiktok":
                    summary = run_tiktok_creative_discovery(
                        db, limit=limit_per_adapter, dry_run=dry_run
                    )
                elif adapter == "reddit_growing":
                    summary = run_reddit_growing_discovery(
                        db, limit=limit_per_adapter, dry_run=dry_run
                    )
                else:
                    raise ValueError(f"unknown adapter: {adapter}")
                if summary is None:
                    raise RuntimeError("adapter returned no summary")
                summary.setdefault("adapter_status", "ok")
            except Exception as exc:
                summary = adapter_failure_summary(exc)
                print(
                    f"discover.{adapter}: FAIL {type(exc).__name__}: {str(exc)[:300]}",
                    file=sys.stderr,
                )
        summaries[adapter] = summary
    print_discovery_run_summary(summaries)
    return summaries


# -- CLI subcommands ------------------------------------------------------

def provider_status_rows(db):
    rows = []
    for name in PROVIDER_POLICY:
        state = ProviderState(db, name)
        row = state.row()
        rows.append((name, state, row))
    return rows


def show_provider_status(db):
    rows = provider_status_rows(db)
    print(f"{'provider':<28s} {'avail':<5s} {'last_success':<22s} "
          f"{'fail':>4s} {'disabled_until':<22s} note")
    for name, state, row in rows:
        available = "yes" if state.is_available() else "no"
        disabled = format_utc(row.get("disabled_until"))
        if disabled == "never":
            disabled = "-"
        failures = int(row.get("consecutive_failures") or 0)
        note = (
            row.get("last_failure_message")
            if failures > 0 else row.get("notes")
        ) or ""
        remaining = state.cooldown_remaining_seconds()
        if remaining > 0 and not note:
            note = state.unavailable_reason()
        print(f"{name:<28s} {available:<5s} "
              f"{format_utc(row.get('last_success_at')):<22s} "
              f"{failures:>4d} "
              f"{disabled:<22s} {str(note)[:120]}")


def reset_provider(db, provider_name):
    if provider_name not in PROVIDER_POLICY:
        known = ", ".join(PROVIDER_POLICY)
        print(f"discover: unknown provider '{provider_name}'. Known: {known}", file=sys.stderr)
        return 1
    ProviderState(db, provider_name).reset()
    print(f"discover: provider reset: {provider_name}")
    return 0


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
                    help="Run discovery adapters")
    ap.add_argument("--adapter", default=None,
                    choices=sorted(ADAPTER_ALIASES.keys()),
                    help="Limit --run to one adapter")
    ap.add_argument("--limit-per-adapter", type=int, default=30,
                    help="Max proposals/fetches per adapter run")
    ap.add_argument("--limit", type=int, default=None,
                    help="Backward-compatible alias for --limit-per-adapter")
    ap.add_argument("--model", default="sonnet",
                    help="Claude model for discovery prompt")
    ap.add_argument("--no-claude", action="store_true",
                    help="Do not invoke Claude; use heuristic general-feed fallback")
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
    ap.add_argument("--provider-status", action="store_true",
                    help="Show provider cooldown/failure/circuit-breaker state")
    ap.add_argument("--provider-reset", default=None,
                    help="Clear disabled_until and failure count for one provider")
    ap.add_argument("--observation-days", type=int, default=OBSERVATION_DAYS,
                    help=f"Observation window after promotion (default {OBSERVATION_DAYS}d)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"FAIL: db not found: {args.db}", file=sys.stderr)
        return 1

    actions = [args.run, args.list_pending, args.show is not None,
               args.approve is not None, args.reject is not None,
               args.promote is not None, args.provider_status,
               args.provider_reset is not None]
    if sum(bool(a) for a in actions) != 1:
        ap.error("specify exactly one of --run / --list-pending / --show / "
                 "--approve / --reject / --promote / --provider-status / "
                 "--provider-reset")

    db = sqlite3.connect(args.db)
    ensure_discovery_schema(db)

    if args.run:
        limit_per_adapter = args.limit if args.limit is not None else args.limit_per_adapter
        try:
            adapters = resolve_adapters(args.adapter)
        except ValueError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        summaries = run_discovery_adapters(
            db, adapters, limit_per_adapter=limit_per_adapter,
            model=args.model, dry_run=args.dry_run,
            no_claude=args.no_claude,
        )
        return discovery_run_exit_code(summaries)
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
    if args.provider_status:
        show_provider_status(db)
        return 0
    if args.provider_reset is not None:
        return reset_provider(db, args.provider_reset)

    return 0


if __name__ == "__main__":
    sys.exit(main())
