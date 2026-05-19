#!/usr/bin/env python3
"""
status.py - quick corpus health view.

  python3 status.py              # summary counts
  python3 status.py --candidates # event counts per candidate
  python3 status.py --sources    # per-source fetch status + errors
"""
import os
import sys
import sqlite3
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db.sqlite")


def summary(db):
    print("== swell-checker status ==")
    n_cand = db.execute("SELECT COUNT(*) FROM candidates WHERE status='tracking'").fetchone()[0]
    n_src = db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    n_fetch = db.execute("SELECT COUNT(*) FROM fetches").fetchone()[0]
    n_unproc = db.execute("SELECT COUNT(*) FROM fetches WHERE processed=0").fetchone()[0]
    n_events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    n_scores = db.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    n_errs = db.execute("SELECT COUNT(*) FROM sources WHERE last_error IS NOT NULL").fetchone()[0]
    print(f"candidates:  {n_cand} tracking")
    print(f"sources:     {n_src} total, {n_errs} with recent errors")
    print(f"fetches:     {n_fetch} total, {n_unproc} unprocessed")
    print(f"events:      {n_events} total")
    print(f"scores:      {n_scores} snapshots")


def per_candidate(db):
    print("== events per candidate ==")
    rows = db.execute(
        """SELECT c.display_name, COUNT(e.id), MAX(e.event_date)
           FROM candidates c LEFT JOIN events e ON e.candidate_id=c.id
           WHERE c.status='tracking' AND c.slug != '__general__'
           GROUP BY c.id ORDER BY COUNT(e.id) DESC"""
    ).fetchall()
    for name, n, last in rows:
        print(f"  {n or 0:4d}  {(last or '—'):<12s}  {name}")


def per_source(db):
    print("== sources ==")
    rows = db.execute(
        """SELECT c.slug, s.source_type, s.label, s.last_fetched_at, s.last_error
           FROM sources s JOIN candidates c ON c.id=s.candidate_id
           ORDER BY c.slug, s.source_type"""
    ).fetchall()
    for slug, stype, label, last, err in rows:
        marker = "ERR" if err else "ok "
        print(f"  {marker}  {slug:30s} [{stype:6s}] {last or '—':<22s} {label or ''}")
        if err:
            print(f"           → {err[:150]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", action="store_true")
    ap.add_argument("--sources", action="store_true")
    args = ap.parse_args()
    db = sqlite3.connect(DB)
    if args.candidates:
        per_candidate(db)
    elif args.sources:
        per_source(db)
    else:
        summary(db)


if __name__ == "__main__":
    main()
