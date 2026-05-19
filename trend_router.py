#!/usr/bin/env python3
"""
trend_router.py - convert fired score snapshots into assistant action intents.

The router is intentionally conservative: it emits pending approvals, not
downstream side effects. A separate playbook adapter should execute only after
the operator approves an event.
"""
import os
import sys
import json
import sqlite3
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db.sqlite")

sys.path.insert(0, HERE)
from scorer import load_config
from calibration import evaluate_calibration


ROUTABLE_STAGES = {"approaching", "very_early"}


def ensure_router_schema(db):
    cols = {row[1] for row in db.execute("PRAGMA table_info(candidates)").fetchall()}
    if "stage" not in cols:
        db.execute("ALTER TABLE candidates ADD COLUMN stage TEXT DEFAULT 'uncalibrated'")
    db.execute(
        """CREATE TABLE IF NOT EXISTS router_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL REFERENCES candidates(id),
            as_of DATE NOT NULL,
            candidate_slug TEXT NOT NULL,
            display_name TEXT NOT NULL,
            stage TEXT,
            composite REAL NOT NULL,
            playbook TEXT NOT NULL,
            route_status TEXT DEFAULT 'pending_approval',
            payload_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(candidate_id, as_of, playbook)
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS router_events_status_idx ON router_events(route_status, created_at)"
    )
    db.commit()


def latest_score_rows(db, as_of=None):
    if as_of:
        return db.execute(
            """SELECT c.id, c.slug, c.display_name, c.category, COALESCE(c.stage, 'uncalibrated'),
                      s.as_of, s.velocity, s.spread, s.vocabulary, s.composite, s.would_fire
               FROM scores s
               JOIN candidates c ON c.id=s.candidate_id
               WHERE s.as_of=? AND c.status='tracking' AND c.slug != '__general__'
               ORDER BY s.composite DESC""",
            (as_of,),
        ).fetchall()

    return db.execute(
        """SELECT c.id, c.slug, c.display_name, c.category, COALESCE(c.stage, 'uncalibrated'),
                  s.as_of, s.velocity, s.spread, s.vocabulary, s.composite, s.would_fire
           FROM scores s
           JOIN (
             SELECT candidate_id, MAX(as_of) AS max_as_of
             FROM scores
             GROUP BY candidate_id
           ) latest ON latest.candidate_id=s.candidate_id AND latest.max_as_of=s.as_of
           JOIN candidates c ON c.id=s.candidate_id
           WHERE c.status='tracking' AND c.slug != '__general__'
           ORDER BY s.composite DESC"""
    ).fetchall()


def playbook_for(slug, category, stage, composite, threshold):
    if stage not in ROUTABLE_STAGES:
        return None
    if composite < threshold:
        return None
    if stage == "very_early":
        return "operator.research_brief"
    return "business_guy.ig_niche"


def seed_query(display_name):
    cleaned = (
        display_name
        .replace("(US)", "")
        .replace("/", " ")
        .replace("  ", " ")
        .strip()
    )
    return f"{cleaned} instagram accounts"


def build_payload(row, playbook):
    cid, slug, name, category, stage, as_of, velocity, spread, vocab, composite, _fire = row
    payload = {
        "candidate_slug": slug,
        "display_name": name,
        "category": category,
        "stage": stage,
        "as_of": as_of,
        "composite": round(composite, 4),
        "signals": {
            "velocity": round(velocity, 4),
            "spread": round(spread, 4),
            "vocabulary": round(vocab, 4),
        },
        "playbook": playbook,
        "requires_approval": True,
    }
    if playbook == "business_guy.ig_niche":
        payload["business_guy"] = {
            "niche_slug": slug,
            "seed_query": seed_query(name),
            "seed_accounts": [],
            "next_step": "operator approves, then business-guy fills seed_accounts and writes niches.yaml",
        }
    elif playbook == "operator.research_brief":
        payload["research"] = {
            "prompt": f"Validate whether {name} is commercially actionable before routing to a playbook.",
            "next_step": "collect evidence and decide whether to promote to business_guy.ig_niche",
        }
    return payload


def upsert_router_event(db, row, playbook, payload):
    cid, slug, name, _category, stage, as_of, _vel, _spread, _vocab, composite, _fire = row
    existing = db.execute(
        """SELECT id FROM router_events
           WHERE candidate_id=? AND as_of=? AND playbook=?""",
        (cid, as_of, playbook),
    ).fetchone()
    if existing:
        db.execute(
            """UPDATE router_events
               SET display_name=?, stage=?, composite=?, payload_json=?
               WHERE id=?""",
            (
                name, stage, composite, json.dumps(payload, sort_keys=True),
                existing[0],
            ),
        )
        return False

    db.execute(
        """INSERT INTO router_events
           (candidate_id, as_of, candidate_slug, display_name, stage, composite, playbook, payload_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            cid, as_of, slug, name, stage, composite, playbook,
            json.dumps(payload, sort_keys=True),
        ),
    )
    return True


def format_digest(events, emitted):
    if not events:
        return "trend_router: no routable fired trends"
    lines = ["trend_router: pending assistant actions"]
    for payload, is_new in events:
        if not emitted:
            prefix = "dry-run"
        else:
            prefix = "emitted" if is_new else "existing"
        lines.append(
            f"  {prefix}: {payload['candidate_slug']} "
            f"{payload['composite']:.3f} -> {payload['playbook']}"
        )
    return "\n".join(lines)


def list_pending(db):
    rows = db.execute(
        """SELECT id, candidate_slug, as_of, composite, playbook, route_status
           FROM router_events
           WHERE route_status='pending_approval'
           ORDER BY composite DESC, created_at ASC"""
    ).fetchall()
    if not rows:
        print("trend_router: no pending approvals")
        return
    print(f"{'id':>4s} {'candidate':<24s} {'as_of':<10s} {'score':>7s}  playbook")
    for rid, slug, as_of, composite, playbook, _status in rows:
        print(f"{rid:>4d} {slug:<24s} {as_of:<10s} {composite:>7.3f}  {playbook}")


def update_route_status(db, route_id, status):
    cur = db.execute(
        "UPDATE router_events SET route_status=? WHERE id=?",
        (status, route_id),
    )
    db.commit()
    if cur.rowcount == 0:
        print(f"trend_router: no router_event id={route_id}", file=sys.stderr)
        return 1
    print(f"trend_router: id={route_id} -> {status}")
    return 0


def calibration_allows_routing(db, cfg, as_of):
    results, failures = evaluate_calibration(db, as_of, cfg, min_events=1)
    if not failures:
        return True
    print("trend_router: calibration failed; refusing to emit assistant actions", file=sys.stderr)
    for failure in failures:
        print(f"  - {failure}", file=sys.stderr)
    print("Run: python3 calibration.py --diagnose", file=sys.stderr)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB, help="SQLite db path")
    ap.add_argument("--as-of", default=None, help="Route a specific score date YYYY-MM-DD")
    ap.add_argument("--emit", action="store_true", help="Write pending router_events")
    ap.add_argument("--notify", action="store_true", help="Send routed events to Telegram")
    ap.add_argument("--skip-calibration", action="store_true",
                    help="Allow --emit even when calibration fails")
    ap.add_argument("--list-pending", action="store_true", help="List pending router approvals")
    ap.add_argument("--approve", type=int, default=None, help="Mark one router_event id approved")
    ap.add_argument("--reject", type=int, default=None, help="Mark one router_event id rejected")
    args = ap.parse_args()
    if sum(bool(x) for x in (args.list_pending, args.approve is not None, args.reject is not None)) > 1:
        ap.error("choose only one of --list-pending, --approve, or --reject")

    if args.as_of:
        datetime.strptime(args.as_of, "%Y-%m-%d")
    if not os.path.exists(args.db):
        print(f"FAIL: db not found: {args.db}", file=sys.stderr)
        return 1

    cfg = load_config()
    db = sqlite3.connect(args.db)
    ensure_router_schema(db)

    if args.list_pending:
        list_pending(db)
        return 0
    if args.approve is not None:
        return update_route_status(db, args.approve, "approved")
    if args.reject is not None:
        return update_route_status(db, args.reject, "rejected")

    route_as_of = datetime.strptime(args.as_of, "%Y-%m-%d") if args.as_of else datetime.utcnow()
    if args.emit and not args.skip_calibration and not calibration_allows_routing(db, cfg, route_as_of):
        return 2

    routed = []
    for row in latest_score_rows(db, args.as_of):
        cid, slug, name, category, stage, as_of, _vel, _spread, _vocab, composite, would_fire = row
        if not would_fire:
            continue
        playbook = playbook_for(slug, category, stage, composite, cfg["threshold"])
        if not playbook:
            continue
        payload = build_payload(row, playbook)
        is_new = False
        if args.emit:
            is_new = upsert_router_event(db, row, playbook, payload)
        routed.append((payload, is_new))

    if args.emit:
        db.commit()

    digest = format_digest(routed, args.emit)
    print(digest)

    has_new_events = any(is_new for _payload, is_new in routed)
    if args.notify and routed and (not args.emit or has_new_events):
        from notify import send
        send("swell-checker\n" + digest)

    return 0


if __name__ == "__main__":
    sys.exit(main())
