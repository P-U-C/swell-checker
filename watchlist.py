#!/usr/bin/env python3
"""
watchlist.py - weekly digest for swell-checker.

Scores all candidates today, compares to last week's snapshot if available,
and produces a ranked watchlist. Emits to stdout; cron pipes to notify.py.

Usage:
  python3 watchlist.py           # print to stdout
  python3 watchlist.py --snapshot  # also write today's snapshot to scores table
"""
import os
import sys
import sqlite3
import argparse
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db.sqlite")

sys.path.insert(0, HERE)
from scorer import score_candidate, load_config, write_score_snapshot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", action="store_true", help="Persist today's scores to db")
    ap.add_argument("--max-items", type=int, default=15)
    args = ap.parse_args()

    cfg = load_config()
    db = sqlite3.connect(DB)
    today = datetime.utcnow()
    week_ago = today - timedelta(days=7)

    cands = db.execute(
        "SELECT id, slug, display_name, category FROM candidates WHERE status='tracking' AND slug != '__general__'"
    ).fetchall()

    rows = []
    for cid, slug, name, category in cands:
        s = score_candidate(db, cid, today, cfg)

        # Previous score (closest snapshot within last 14 days)
        prev = db.execute(
            """SELECT composite FROM scores
               WHERE candidate_id=? AND as_of < ?
               ORDER BY as_of DESC LIMIT 1""",
            (cid, today.strftime("%Y-%m-%d")),
        ).fetchone()
        prev_composite = prev[0] if prev else None
        delta = s["composite"] - prev_composite if prev_composite is not None else None

        # Event count for this week (for context)
        n_events_7d = db.execute(
            "SELECT COUNT(*) FROM events WHERE candidate_id=? AND event_date >= ?",
            (cid, week_ago.strftime("%Y-%m-%d")),
        ).fetchone()[0]

        rows.append({
            "slug": slug, "name": name, "category": category,
            "score": s, "delta": delta, "events_7d": n_events_7d,
        })

        if args.snapshot:
            write_score_snapshot(db, cid, today, s)

    if args.snapshot:
        db.commit()

    # Rank by composite descending
    rows.sort(key=lambda r: -r["score"]["composite"])

    lines = []
    lines.append("🌊 swell-checker weekly watchlist")
    lines.append(f"{today.strftime('%Y-%m-%d')}  threshold={cfg['threshold']}")
    lines.append("")

    firing = [r for r in rows if r["score"]["would_fire"]]
    if firing:
        lines.append("🔥 FIRING:")
        for r in firing:
            lines.append(f"  {r['name']}: {r['score']['composite']:.3f}")
        lines.append("")

    lines.append("Top watch:")
    for r in rows[:args.max_items]:
        delta_str = ""
        if r["delta"] is not None:
            arrow = "↑" if r["delta"] > 0.01 else ("↓" if r["delta"] < -0.01 else "·")
            delta_str = f" {arrow}{abs(r['delta']):.2f}"
        fire = " 🔥" if r["score"]["would_fire"] else ""
        lines.append(
            f"  {r['score']['composite']:.2f}{delta_str}  "
            f"{r['name']}  (n={r['events_7d']} 7d){fire}"
        )

    lines.append("")
    lines.append(f"(tracking {len(rows)} candidates)")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
