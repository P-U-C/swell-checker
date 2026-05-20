#!/usr/bin/env python3
"""
scorer.py - live scoring for swell-checker candidates.

Same three-signal architecture as the backtest scorer:
  - velocity (mention/media/cohort/participant/adjacent/funding events, log-scaled in trailing window)
  - spread (operator + geographic events, log-scaled in trailing window)
  - vocabulary (positive/negative vocabulary events, all-time)
  - disruption penalty (damping)

Reads events from the db, writes/upserts snapshots to the scores table.
"""
import os
import sys
import math
import yaml
import sqlite3
import argparse
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "db.sqlite")
CFG_PATH = os.path.join(HERE, "scorer_config.yaml")


DEFAULT_CONFIG = {
    "velocity_window_months": 18,
    "spread_window_months": 24,
    "velocity_saturation": 15.0,
    "spread_saturation": 8.0,
    # Composite weights -- spread (operator/geographic deployment) is the
    # strongest signal for "crossing the chasm". Reddit chatter alone
    # (mention/adjacent) saturates velocity but doesn't mean a trend is
    # being adopted -- axe_throwing has plenty of fizzled chatter, the
    # difference is whether NEW LOCATIONS are opening.
    "weights": {"velocity": 0.25, "spread": 0.55, "vocabulary": 0.20},
    # Per-event-type weights inside the velocity bucket. "mention" is
    # weak (people talking), "cohort"/"funding" is strong (capital +
    # group adoption). Saturation stays at 15 so trends with diverse
    # high-signal events still saturate.
    "velocity_type_weights": {
        "mention": 0.30,
        "media": 1.0,
        "cohort": 2.0,
        "funding": 5.0,
        "adjacent": 0.5,
    },
    "threshold": 0.55,
}


def load_config():
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        merged = {**DEFAULT_CONFIG, **cfg}
        if "weights" in cfg:
            merged["weights"] = {**DEFAULT_CONFIG["weights"], **cfg["weights"]}
        return merged
    return DEFAULT_CONFIG.copy()


VELOCITY_TYPES = {"mention", "media", "cohort", "funding", "adjacent"}
SPREAD_TYPES = {"operator", "geographic"}


def velocity_score(events, saturation, type_weights=None):
    """Per-event-type weighted velocity.

    Reddit chatter ("mention") dominates raw event counts but is the
    weakest signal of real adoption. Capital deployment ("cohort" /
    "funding") is the strongest. Without type weights, all mention-
    heavy trends saturate velocity at 1.0 regardless of whether
    they're actually crossing the chasm. With type weights, only
    trends with diverse high-signal events saturate.
    """
    weighted = 0.0
    for etype, mag, _date in events:
        if etype in VELOCITY_TYPES:
            base = abs(mag) if mag < 10 else 1.0 + math.log10(abs(mag))
            tw = (type_weights or {}).get(etype, 1.0)
            weighted += base * tw
    return min(1.0, weighted / saturation)


def spread_score(events, saturation):
    s = 0.0
    for etype, mag, _date in events:
        if etype == "geographic":
            s += abs(mag)
        elif etype == "operator":
            s += 1.0 + math.log10(max(1, abs(mag)))
    return min(1.0, s / saturation)


def vocab_score(events):
    positive, negative = 0.0, 0.0
    for etype, mag, _date in events:
        if etype == "vocabulary":
            if mag > 0:
                positive += mag
            else:
                negative += 1.0
    return max(0.0, min(1.0, positive / 2.0) - min(0.5, negative * 0.25))


def disruption_penalty(events, coefficient=0.08, cap=0.30):
    """Penalty for disruption events (trend reversals / failures).

    Bumped from 0.05 → 0.08 so that mention-heavy fizzled trends
    (axe_throwing has 27 chatter mentions + 3 small disruptions)
    get damped below the calibration cap rather than scoring 0.42.
    Cap stays at 0.30 so a single bad year doesn't kill a trend
    with otherwise strong operator signals.
    """
    p = 0.0
    for etype, mag, _date in events:
        if etype == "disruption" and mag < 0:
            p += abs(mag) * coefficient
    return min(cap, p)


def score_candidate(db, candidate_id, as_of, cfg):
    vel_start = as_of - timedelta(days=30 * cfg["velocity_window_months"])
    spread_start = as_of - timedelta(days=30 * cfg["spread_window_months"])

    all_events = db.execute(
        "SELECT event_type, magnitude, event_date FROM events WHERE candidate_id=? AND event_date<=?",
        (candidate_id, as_of.strftime("%Y-%m-%d")),
    ).fetchall()
    all_events = [(t, m, datetime.strptime(d, "%Y-%m-%d")) for t, m, d in all_events]

    vel_events = [e for e in all_events if e[2] >= vel_start]
    spread_events = [e for e in all_events if e[2] >= spread_start]

    vel = velocity_score(vel_events, cfg["velocity_saturation"], cfg.get("velocity_type_weights"))
    spread = spread_score(spread_events, cfg["spread_saturation"])
    vocab = vocab_score(all_events)
    penalty = disruption_penalty(
        vel_events,
        coefficient=cfg.get("disruption_coefficient", 0.08),
        cap=cfg.get("disruption_cap", 0.30),
    )

    w = cfg["weights"]
    composite = (w["velocity"] * vel + w["spread"] * spread + w["vocabulary"] * vocab) * (1.0 - penalty)

    return {
        "velocity": vel, "spread": spread, "vocabulary": vocab,
        "composite": composite, "would_fire": composite >= cfg["threshold"],
    }


def write_score_snapshot(db, candidate_id, as_of, score):
    """Upsert one score snapshot for a candidate/date."""
    db.execute(
        """INSERT INTO scores
           (candidate_id, as_of, velocity, spread, vocabulary, composite, would_fire)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(candidate_id, as_of) DO UPDATE SET
             velocity=excluded.velocity,
             spread=excluded.spread,
             vocabulary=excluded.vocabulary,
             composite=excluded.composite,
             would_fire=excluded.would_fire,
             created_at=CURRENT_TIMESTAMP""",
        (
            candidate_id, as_of.strftime("%Y-%m-%d"),
            score["velocity"], score["spread"], score["vocabulary"],
            score["composite"], 1 if score["would_fire"] else 0,
        ),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=None, help="Date YYYY-MM-DD (default today)")
    ap.add_argument("--db", default=DB, help="SQLite db path")
    ap.add_argument("--dry-run", action="store_true", help="Print scores without writing snapshots")
    ap.add_argument("--snapshot", action="store_true", help="Deprecated: snapshots are written by default")
    args = ap.parse_args()
    if args.dry_run and args.snapshot:
        ap.error("--dry-run and --snapshot cannot be used together")

    as_of = datetime.strptime(args.as_of, "%Y-%m-%d") if args.as_of else datetime.utcnow()
    cfg = load_config()
    db = sqlite3.connect(args.db)
    write_snapshot = not args.dry_run

    candidates = db.execute(
        "SELECT id, slug, display_name FROM candidates WHERE status='tracking' AND slug != '__general__' ORDER BY display_name"
    ).fetchall()

    print(f"{'candidate':<40s} {'vel':>6} {'spr':>6} {'voc':>6} {'comp':>6}  fire?")
    written = 0
    for cid, slug, name in candidates:
        s = score_candidate(db, cid, as_of, cfg)
        fire = "YES" if s["would_fire"] else "-"
        print(f"{name:<40s} {s['velocity']:>6.2f} {s['spread']:>6.2f} {s['vocabulary']:>6.2f} "
              f"{s['composite']:>6.3f}  {fire}")
        if write_snapshot:
            write_score_snapshot(db, cid, as_of, s)
            written += 1

    if write_snapshot:
        db.commit()
        print(f"\nsnapshot upserted: {written} rows for {as_of.strftime('%Y-%m-%d')}")
    else:
        print("\ndry run: no score rows written")


if __name__ == "__main__":
    main()
