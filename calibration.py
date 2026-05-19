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


def evaluate_calibration(db, as_of, cfg, min_events):
    results = []
    failures = []
    for slug in EXPECTATIONS:
        row = db.execute(
            "SELECT id, display_name FROM candidates WHERE slug=? AND status='tracking'",
            (slug,),
        ).fetchone()
        if not row:
            result = {
                "slug": slug, "candidate_id": None, "events": None,
                "score": None, "expectation": "--", "ok": False,
                "status": "FAIL missing",
            }
            results.append(result)
            failures.append(f"{slug}: candidate missing or not tracking")
            continue

        cid, _name = row
        n_events = db.execute(
            "SELECT COUNT(*) FROM events WHERE candidate_id=? AND event_date<=?",
            (cid, as_of.strftime("%Y-%m-%d")),
        ).fetchone()[0]
        if n_events < min_events:
            result = {
                "slug": slug, "candidate_id": cid, "events": n_events,
                "score": None, "expectation": "data", "ok": False,
                "status": "FAIL sparse",
            }
            results.append(result)
            failures.append(f"{slug}: only {n_events} events, need {min_events}")
            continue

        score = score_candidate(db, cid, as_of, cfg)
        ok, expectation = check_expectation(slug, score)
        result = {
            "slug": slug, "candidate_id": cid, "events": n_events,
            "score": score["composite"], "expectation": expectation,
            "ok": ok, "status": "ok" if ok else "FAIL",
        }
        results.append(result)
        if not ok:
            failures.append(
                f"{slug}: composite {score['composite']:.3f} does not satisfy {expectation}"
            )
    return results, failures


def source_diagnostics(db, candidate_id):
    return db.execute(
        """SELECT s.source_type, COALESCE(s.label, s.url), s.last_fetched_at,
                  s.last_error, COUNT(f.id), SUM(CASE WHEN f.processed=0 THEN 1 ELSE 0 END),
                  MAX(f.fetched_at)
           FROM sources s
           LEFT JOIN fetches f ON f.source_id=s.id
           WHERE s.candidate_id=?
           GROUP BY s.id
           ORDER BY s.source_type, s.label""",
        (candidate_id,),
    ).fetchall()


def event_type_counts(db, candidate_id, as_of):
    return db.execute(
        """SELECT event_type, COUNT(*), ROUND(SUM(magnitude), 2), MAX(event_date)
           FROM events
           WHERE candidate_id=? AND event_date<=?
           GROUP BY event_type
           ORDER BY COUNT(*) DESC, event_type""",
        (candidate_id, as_of.strftime("%Y-%m-%d")),
    ).fetchall()


def print_report(db, results, failures, cfg, as_of, diagnose=False):
    print("== swell-checker calibration ==")
    print(f"as_of={as_of.strftime('%Y-%m-%d')}  threshold={cfg['threshold']}")
    print(f"{'slug':<16s} {'events':>6s} {'score':>7s} {'expect':>8s}  status")

    for result in results:
        n_events = "--" if result["events"] is None else f"{result['events']:d}"
        score = "--" if result["score"] is None else f"{result['score']:.3f}"
        print(
            f"{result['slug']:<16s} {n_events:>6s} {score:>7s} "
            f"{result['expectation']:>8s}  {result['status']}"
        )

    if diagnose:
        print("\n== calibration diagnostics ==")
        for result in results:
            cid = result["candidate_id"]
            if cid is None:
                continue
            print(f"\n{result['slug']}:")
            counts = event_type_counts(db, cid, as_of)
            if counts:
                print("  events by type:")
                for etype, count, magnitude_sum, last_date in counts:
                    print(f"    {etype:<12s} count={count:<3d} mag_sum={magnitude_sum:<6} last={last_date}")
            else:
                print("  events by type: none")

            sources = source_diagnostics(db, cid)
            if sources:
                print("  sources:")
                for stype, label, last_fetched, last_error, fetches, unprocessed, last_fetch in sources:
                    err = f" err={last_error[:90]}" if last_error else ""
                    print(
                        f"    {stype:<12s} fetches={fetches:<3d} unprocessed={unprocessed or 0:<3d} "
                        f"last_source={last_fetched or '-'} last_fetch={last_fetch or '-'} {label}{err}"
                    )
            else:
                print("  sources: none")

    if failures:
        print("\ncalibration: FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return False

    print("\ncalibration: passed")
    return True


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
    ap.add_argument("--diagnose", action="store_true", help="Print source/fetch/event diagnostics")
    ap.add_argument("--warn-only", action="store_true", help="Print failures but exit 0")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"FAIL: db not found: {args.db}", file=sys.stderr)
        return 0 if args.warn_only else 1

    as_of = datetime.strptime(args.as_of, "%Y-%m-%d") if args.as_of else datetime.utcnow()
    cfg = load_config()
    db = sqlite3.connect(args.db)

    results, failures = evaluate_calibration(db, as_of, cfg, args.min_events)
    ok = print_report(db, results, failures, cfg, as_of, diagnose=args.diagnose)
    if not ok:
        return 0 if args.warn_only else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
