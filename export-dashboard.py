#!/usr/bin/env python3
"""export-dashboard.py - emit the public swell dashboard JSON.

Reads the local db.sqlite (on clawd that's the daily mirror pulled from
worker-1 at 13:40 UTC) and writes one self-contained JSON consumed by the
static page at pft.permanentupperclass.com/swell/ (GitHub Pages, served
from the pft-validator repo -- same pattern as /scanner/).

Per tracked/observing candidate: the latest score snapshot, the composite
delta vs ~7 days earlier, and 7-day event flow. Calibration anchors are
included but flagged so the page can de-emphasize them.
"""
import json
import os
import sys
import sqlite3
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db.sqlite")
OUT = os.path.expanduser(
    os.environ.get("SWELL_DASHBOARD_PATH", "~/pft-validator/swell/swell.json")
)

CALIBRATION_SLUGS = {"pickleball", "hyrox", "axe_throwing", "crossfit"}

sys.path.insert(0, HERE)
from scorer import load_config


def main() -> int:
    if not os.path.exists(DB):
        print(f"export-dashboard: db not found: {DB}", file=sys.stderr)
        return 1
    cfg = load_config()
    db = sqlite3.connect(DB)

    latest_as_of = db.execute("SELECT MAX(as_of) FROM scores").fetchone()[0]
    if not latest_as_of:
        print("export-dashboard: no score snapshots", file=sys.stderr)
        return 1
    week_ago = (
        datetime.strptime(latest_as_of, "%Y-%m-%d") - timedelta(days=7)
    ).strftime("%Y-%m-%d")

    rows = db.execute(
        """SELECT c.id, c.slug, c.display_name, c.category, c.status,
                  COALESCE(c.stage, ''), s.velocity, s.spread, s.vocabulary,
                  s.composite, s.would_fire, s.as_of
           FROM candidates c
           JOIN scores s ON s.candidate_id = c.id AND s.as_of = (
               SELECT MAX(as_of) FROM scores WHERE candidate_id = c.id
           )
           WHERE c.status IN ('tracking', 'observing') AND c.slug != '__general__'
           ORDER BY s.composite DESC, c.display_name""",
    ).fetchall()

    candidates = []
    for cid, slug, name, category, status, stage, vel, spr, voc, comp, fire, as_of in rows:
        prev = db.execute(
            """SELECT composite FROM scores
               WHERE candidate_id=? AND as_of <= ?
               ORDER BY as_of DESC LIMIT 1""",
            (cid, week_ago),
        ).fetchone()
        events_7d, last_event = db.execute(
            """SELECT COUNT(*), MAX(event_date) FROM events
               WHERE candidate_id=? AND event_date > date(?, '-7 days')""",
            (cid, as_of),
        ).fetchone()
        candidates.append(
            {
                "slug": slug,
                "name": name,
                "category": category,
                "status": status,
                "stage": stage,
                "is_calibration": slug in CALIBRATION_SLUGS,
                "velocity": round(vel, 3),
                "spread": round(spr, 3),
                "vocabulary": round(voc, 3),
                "composite": round(comp, 3),
                "firing": bool(fire),
                "delta_7d": round(comp - prev[0], 3) if prev else None,
                "events_7d": events_7d,
                "last_event": last_event,
                "as_of": as_of,
            }
        )

    payload = {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": latest_as_of,
        "threshold": cfg["threshold"],
        "firing_count": sum(1 for c in candidates if c["firing"]),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
        f.write("\n")
    print(
        f"export-dashboard: {len(candidates)} candidates "
        f"({payload['firing_count']} firing) as_of={latest_as_of} -> {OUT}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
