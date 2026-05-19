#!/usr/bin/env python3
"""
calibration.py - guardrail checks for scorer quality.

Run after ingest/extract/scorer refreshes. This intentionally fails when the
known-positive and known-negative calibration candidates do not separate.
"""
import os
import sys
import sqlite3
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db.sqlite")

sys.path.insert(0, HERE)
from scorer import load_config, score_candidate


EXPECTATIONS = {
    "pickleball": {"min": 0.60, "why": "mainstream calibration positive"},
    "hyrox": {"min": 0.60, "why": "recent mainstream calibration positive"},
    "axe_throwing": {"max": 0.40, "why": "fizzled calibration negative"},
    "crossfit": {"max": 0.40, "why": "post-peak calibration negative"},
}


def check_expectation(slug, score):
    exp = EXPECTATIONS[slug]
    comp = score["composite"]
    if "min" in exp:
        return comp >= exp["min"], f">= {exp['min']:.2f}"
    return comp <= exp["max"], f"<= {exp['max']:.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB, help="SQLite db path")
    ap.add_argument("--as-of", default=None, help="Date YYYY-MM-DD (default today)")
    ap.add_argument("--min-events", type=int, default=1, help="Minimum events required per calibration candidate")
    ap.add_argument("--warn-only", action="store_true", help="Print failures but exit 0")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"FAIL: db not found: {args.db}", file=sys.stderr)
        return 0 if args.warn_only else 1

    as_of = datetime.strptime(args.as_of, "%Y-%m-%d") if args.as_of else datetime.utcnow()
    cfg = load_config()
    db = sqlite3.connect(args.db)

    print("== swell-checker calibration ==")
    print(f"as_of={as_of.strftime('%Y-%m-%d')}  threshold={cfg['threshold']}")
    print(f"{'slug':<16s} {'events':>6s} {'score':>7s} {'expect':>8s}  status")

    failures = []
    for slug in EXPECTATIONS:
        row = db.execute(
            "SELECT id, display_name FROM candidates WHERE slug=? AND status='tracking'",
            (slug,),
        ).fetchone()
        if not row:
            failures.append(f"{slug}: candidate missing or not tracking")
            print(f"{slug:<16s} {'--':>6s} {'--':>7s} {'--':>8s}  FAIL missing")
            continue

        cid, _name = row
        n_events = db.execute(
            "SELECT COUNT(*) FROM events WHERE candidate_id=? AND event_date<=?",
            (cid, as_of.strftime("%Y-%m-%d")),
        ).fetchone()[0]
        if n_events < args.min_events:
            failures.append(f"{slug}: only {n_events} events, need {args.min_events}")
            print(f"{slug:<16s} {n_events:>6d} {'--':>7s} {'data':>8s}  FAIL sparse")
            continue

        score = score_candidate(db, cid, as_of, cfg)
        ok, expectation = check_expectation(slug, score)
        status = "ok" if ok else "FAIL"
        print(f"{slug:<16s} {n_events:>6d} {score['composite']:>7.3f} {expectation:>8s}  {status}")
        if not ok:
            failures.append(
                f"{slug}: composite {score['composite']:.3f} does not satisfy {expectation}"
            )

    if failures:
        print("\ncalibration: FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 0 if args.warn_only else 1

    print("\ncalibration: passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
