#!/usr/bin/env python3
"""
scorer.py - live scoring for swell-checker candidates.

Same three-signal architecture as the backtest scorer:
  - velocity (mention/media/cohort/participant/adjacent/funding events, log-scaled in trailing window)
  - spread (operator + geographic events, log-scaled in trailing window)
  - vocabulary (positive/negative vocabulary events, all-time)
  - disruption penalty (damping)

Reads events from the db, writes snapshots to the scores table.
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
    "weights": {"velocity": 0.40, "spread": 0.40, "vocabulary": 0.20},
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


def velocity_score(events, saturation):
    weighted = 0.0
    for etype, mag, _date in events:
        if etype in VELOCITY_TYPES:
            weighted += abs(mag) if mag < 10 else 1.0 + math.log10(abs(mag))
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


def disruption_penalty(events):
    p = 0.0
    for etype, mag, _date in events:
        if etype == "disruption" and mag < 0:
            p += abs(mag) * 0.05
    return min(0.3, p)


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

    vel = velocity_score(vel_events, cfg["velocity_saturation"])
    spread = spread_score(spread_events, cfg["spread_saturation"])
    vocab = vocab_score(all_events)
    penalty = disruption_penalty(vel_events)

    w = cfg["weights"]
    composite = (w["velocity"] * vel + w["spread"] * spread + w["vocabulary"] * vocab) * (1.0 - penalty)

    return {
        "velocity": vel, "spread": spread, "vocabulary": vocab,
        "composite": composite, "would_fire": composite >= cfg["threshold"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=None, help="Date YYYY-MM-DD (default today)")
    ap.add_argument("--snapshot", action="store_true", help="Write scores to db")
    args = ap.parse_args()

    as_of = datetime.strptime(args.as_of, "%Y-%m-%d") if args.as_of else datetime.utcnow()
    cfg = load_config()
    db = sqlite3.connect(DB)

    candidates = db.execute(
        "SELECT id, slug, display_name FROM candidates WHERE status='tracking' AND slug != '__general__' ORDER BY display_name"
    ).fetchall()

    print(f"{'candidate':<40s} {'vel':>6} {'spr':>6} {'voc':>6} {'comp':>6}  fire?")
    for cid, slug, name in candidates:
        s = score_candidate(db, cid, as_of, cfg)
        fire = "YES" if s["would_fire"] else "-"
        print(f"{name:<40s} {s['velocity']:>6.2f} {s['spread']:>6.2f} {s['vocabulary']:>6.2f} "
              f"{s['composite']:>6.3f}  {fire}")
        if args.snapshot:
            try:
                db.execute(
                    """INSERT INTO scores
                       (candidate_id, as_of, velocity, spread, vocabulary, composite, would_fire)
                       VALUES (?,?,?,?,?,?,?)""",
                    (cid, as_of.strftime("%Y-%m-%d"),
                     s["velocity"], s["spread"], s["vocabulary"],
                     s["composite"], 1 if s["would_fire"] else 0),
                )
                db.commit()
            except sqlite3.IntegrityError:
                # Already scored for this date
                pass


if __name__ == "__main__":
    main()
