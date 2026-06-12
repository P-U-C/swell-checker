#!/usr/bin/env python3
"""
scorer.py - live scoring for swell-checker candidates.

Three-signal architecture (redesigned 2026-06-12 -- see scorer_config.yaml):
  - velocity (media/cohort/funding/adjacent events, type-weighted, in trailing window; mention weighted 0)
  - spread (deployment expansion: max of Places-census growth and geographic new-metro events)
  - vocabulary (positive/negative vocabulary events, all-time)
  - disruption penalty (damping)

Reads events from the db, writes/upserts snapshots to the scores table.
"""
import os
import re
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
    "velocity_saturation": 25.0,
    # Spread = real deployment expansion, the max of two sub-signals:
    #   census growth -- %-growth of OPERATIONAL_BUSINESSES (parsed from
    #   Places operator payloads) across the spread window; 10% growth
    #   saturates. Summing daily snapshots (the pre-2026-06-12 design)
    #   pinned spread to 1.0 for every candidate because Places re-reports
    #   the whole footprint each day.
    #   geographic expansion -- count of distinct geographic events
    #   (new-metro entries); 14 saturates. This is what carries a trend
    #   like hyrox whose top-20-metro census is already saturated (the
    #   Places sample has a coverage ceiling) but which keeps entering
    #   new metros.
    "census_growth_ref": 0.10,
    "geo_saturation": 14.0,
    # Composite weights -- rebalanced 2026-06-12. Spread's data proxy
    # (top-20-metro Places sample) undercounts metro-saturated winners,
    # so it can no longer dominate; velocity (now chatter-proof, see
    # below) carries the most weight.
    "weights": {"velocity": 0.45, "spread": 0.35, "vocabulary": 0.20},
    # Per-event-type weights inside the velocity bucket. "mention"
    # (Reddit chatter) is ZERO -- 300+ mentions saturated velocity for
    # every candidate including fizzled ones, so chatter can no longer
    # fake momentum. Capital + group adoption (funding/cohort) and
    # earned media are the signal.
    "velocity_type_weights": {
        "mention": 0.0,
        "media": 1.0,
        "cohort": 2.0,
        "funding": 5.0,
        "adjacent": 0.3,
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
    weakest signal of real adoption -- with mention weighted 0 it cannot
    drive velocity at all. Capital deployment ("cohort"/"funding") and
    earned media are what saturate the bucket.
    """
    weighted = 0.0
    for etype, mag, _date, _quote in events:
        if etype in VELOCITY_TYPES:
            base = abs(mag) if abs(mag) < 10 else 1.0 + math.log10(abs(mag))
            tw = (type_weights or {}).get(etype, 1.0)
            weighted += base * tw
    return min(1.0, weighted / saturation)


def census_growth_score(events, ref):
    """Footprint growth from Places operator payloads.

    Each operator snapshot carries an OPERATIONAL_BUSINESSES census of the
    sampled metros. Growth of that census across the window (first day vs
    last day, max per day) is the deployment signal; `ref` growth (e.g.
    0.10 = +10%) saturates. Counting or summing the snapshots themselves
    is meaningless -- Places re-reports the whole footprint daily.
    """
    by_day = {}
    for etype, _mag, date, quote in events:
        if etype == "operator" and quote and "OPERATIONAL_BUSINESSES" in quote:
            m = re.search(r"OPERATIONAL_BUSINESSES:\s*(\d+)", quote)
            if m:
                key = date.strftime("%Y-%m-%d")
                by_day[key] = max(by_day.get(key, 0), int(m.group(1)))
    if len(by_day) < 2:
        return 0.0
    series = [count for _day, count in sorted(by_day.items())]
    growth = (series[-1] - series[0]) / max(1, series[0])
    return max(0.0, min(1.0, growth / ref))


def geo_expansion_score(events, saturation):
    n = sum(1 for etype, _mag, _date, _quote in events if etype == "geographic")
    return min(1.0, n / saturation)


def spread_score(events, census_ref, geo_saturation):
    """Real deployment expansion: max(census growth, new-metro entries).

    max, not sum: the Places census has a coverage ceiling (fixed top-20
    metro sample), so a metro-saturated but genuinely expanding trend
    (hyrox) shows flat census while racking up geographic events --
    either sub-signal alone is sufficient evidence of spread.
    """
    return max(
        census_growth_score(events, census_ref),
        geo_expansion_score(events, geo_saturation),
    )


def vocab_score(events):
    positive, negative = 0.0, 0.0
    for etype, mag, _date, _quote in events:
        if etype == "vocabulary":
            if mag > 0:
                positive += mag
            else:
                negative += 1.0
    return max(0.0, min(1.0, positive / 2.0) - min(0.5, negative * 0.25))


def disruption_penalty(events, coefficient=0.08, cap=0.30):
    """Penalty for disruption events (trend reversals / failures).

    Cap stays at 0.30 so a single bad year doesn't kill a trend
    with otherwise strong deployment signals.
    """
    p = 0.0
    for etype, mag, _date, _quote in events:
        if etype == "disruption" and mag < 0:
            p += abs(mag) * coefficient
    return min(cap, p)


def score_candidate(db, candidate_id, as_of, cfg):
    vel_start = as_of - timedelta(days=30 * cfg["velocity_window_months"])
    spread_start = as_of - timedelta(days=30 * cfg["spread_window_months"])

    all_events = db.execute(
        "SELECT event_type, magnitude, event_date, evidence_quote FROM events WHERE candidate_id=? AND event_date<=?",
        (candidate_id, as_of.strftime("%Y-%m-%d")),
    ).fetchall()
    all_events = [(t, m, datetime.strptime(d, "%Y-%m-%d"), q) for t, m, d, q in all_events]

    vel_events = [e for e in all_events if e[2] >= vel_start]
    spread_events = [e for e in all_events if e[2] >= spread_start]

    vel = velocity_score(vel_events, cfg["velocity_saturation"], cfg.get("velocity_type_weights"))
    spread = spread_score(spread_events, cfg["census_growth_ref"], cfg["geo_saturation"])
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

    # Score both 'tracking' candidates (seeded from yaml) and 'observing'
    # candidates (promoted from the discovery layer; not yet router-
    # eligible). The observation gate is enforced via would_fire below.
    candidates = db.execute(
        "SELECT id, slug, display_name, status, router_eligible_at "
        "FROM candidates "
        "WHERE status IN ('tracking', 'observing') AND slug != '__general__' "
        "ORDER BY display_name"
    ).fetchall()

    print(f"{'candidate':<40s} {'vel':>6} {'spr':>6} {'voc':>6} {'comp':>6}  fire?")
    written = 0
    now = datetime.utcnow()
    for cid, slug, name, status, router_eligible_at in candidates:
        s = score_candidate(db, cid, as_of, cfg)
        # Observation gate: 'observing' candidates and 'tracking'
        # candidates whose router_eligible_at hasn't elapsed are scored
        # but cannot fire. This is enforced HERE (not in trend_router)
        # so the score snapshot accurately reflects routing eligibility.
        if status == "observing":
            s["would_fire"] = False
            fire = "obs"
        elif router_eligible_at:
            try:
                eligible = datetime.strptime(router_eligible_at[:19], "%Y-%m-%d %H:%M:%S")
                if eligible > now:
                    s["would_fire"] = False
                    fire = "obs"
                else:
                    fire = "YES" if s["would_fire"] else "-"
            except (ValueError, TypeError):
                fire = "YES" if s["would_fire"] else "-"
        else:
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
