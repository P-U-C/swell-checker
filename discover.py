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
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db.sqlite")
PROMPT_DISCOVER_GENERAL = os.path.join(HERE, "prompts", "discover_general.md")
SOURCES_YAML = os.path.join(HERE, "sources.yaml")

SOURCE_TEXT_CAP = 45000
GENERAL_CANDIDATE_SLUG = "__general__"
OBSERVATION_DAYS = 28  # newly-promoted candidates wait 4 weeks before routing


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
