# Swell-Checker Status Collation

Snapshot date: 2026-05-19. Last source touched: 2026-04-23.

This is the operator handoff document for revisiting swell-checker after a 26-day pause. Sections: what it is, current state, the original optimal path, previous blockers (and how each was resolved), what's still open, integration angles with the rest of the stack.

## 1. What it is

Swell-checker is an autonomous trend-emergence detector for **physical/lifestyle** trends (the consumer-market complement to `trend-corpus`, which covers financial/tech themes).

- **Input:** a curated list of 24 candidate trends (padel, run clubs, rucking, cold plunge, sober bars, pilates reformer, climbing-urban, longevity clinics, social sauna, mahjong leagues, nordic walking, soulcycle, f45_alternatives, functional_fitness_smallbox, dawn_patrol_fitness, crossfit, + 8 more) with stage labels (`very_early`, `approaching`, `calibration`, `calibration_fizzled`).
- **Pipeline:** Reddit JSON + RSS + Google Trends → ingest → typed event extraction via Sonnet → three-signal scoring (velocity + spread + vocabulary) → weekly Telegram digest on Mondays at 14:00 UTC.
- **Output:** ranked watchlist of trends crossing the chasm, plus per-candidate score history.

This is the **v0 scope**. Phase 2 (deferred) adds open-discovery (auto-extracting candidate trends from prose), Opus-authored thesis briefs when a candidate fires, and threshold-crossing mid-week alerts.

## 2. Current state (city-worker-301, user `swell`)

| Aspect | State |
|---|---|
| Deployed at | `/home/swell/swell-checker/` on city-worker-301 |
| In git? | **No** (now imported to `~/swell-checker/` on the orchestrator at commit `c02b516`) |
| Schema version | v2 (migration script applied 2026-04-23) |
| Candidates seeded | 24 |
| Sources configured | 84 (Reddit + RSS + Trends) |
| Fetches stored | 54 (all from a single 2026-04-23 evening session) |
| Events extracted | **703** typed events (operator=177, media=144, mention=91, adjacent=74, geographic=70, cohort=57, disruption=45, vocabulary=28, funding=17) |
| Scores computed | **0** (scorer was run, no rows persisted — the canonical blocker) |
| Crontab installed | **No** (`sudo crontab -u swell -r` was the last action in the session) |
| Logs | All `/var/log/swell-*.log` files are 0 bytes — cron never fired |
| Telegram bot | Shares peptide-corpus's `@alpha_man_bot`, distinguished by 🌊 prefix |
| Data freshness | 26 days stale |

**Top candidates by event volume** (data signal, not score):

```
padel_us                     111
run_clubs                     77
cold_plunge                   61
sober_bars                    56
pilates_reformer              56
climbing_urban                54
longevity_clinics             50
social_sauna                  46
mahjong_leagues               46
nordic_walking                42
soulcycle                     33
f45_alternatives              28
functional_fitness_smallbox   12
dawn_patrol_fitness            7
crossfit                       5   ← calibration_fizzled, correctly low
```

The signal density on padel_us / run_clubs / cold_plunge is healthy. Crossfit (a known plateau) ranked correctly low. Calibration looks viable.

## 3. The optimal path (per the parked README)

The system was designed for a **4-week observe-only calibration** before acting:

| Week | Action |
|---|---|
| 1-2 | Verify scorer ranks calibration trends correctly. If pickleball / hyrox aren't scoring high and axe-throwing isn't scoring low, extraction is broken. |
| 3-4 | Tune `scorer_config.yaml`. Lower threshold to 0.45 → see what newly fires. Does it match operator intuition? |
| Month 2+ | Act on signals (promote candidates to `tracked` → trigger downstream actions). |

The composite scoring model:

```
composite = 0.4·velocity + 0.4·spread + 0.2·vocabulary
fires_at = composite >= 0.55
```

Damped by a `disruption` penalty for trends that recently spiked-and-crashed.

**Cron cadence (intended):**
- `:30 every 6h` — ingest
- `:45 every 6h` — extract
- `12:45 daily` — health check
- `Monday 14:00 UTC` — weekly watchlist → Telegram

## 4. Previous blockers (and how each was resolved)

From the 2026-04-23 working session (recovered from `~swell/.bash_history`):

1. **`pytrends` missing** — `pip install pytrends --break-system-packages` (resolved).
2. **Stub Trends fetches in db** — `DELETE FROM fetches WHERE source_type='trends'` then `UPDATE sources SET last_fetched_at=NULL` so dedup didn't block refetch (resolved).
3. **Extract prompt insufficient for Trends prose** — added `prompts/extract_general.md` alongside `extract.md` (resolved).
4. **v1 → v2 schema migration** — `migrate_v1_to_v2.sh` ran cleanly with backup at `db.sqlite.pre-v2-backup` (resolved).
5. **Scorer ran but persisted no rows** — `scorer.py` was invoked twice but `SELECT count(*) FROM scores` returns 0. Last action before pause. **Still open** — see §5.
6. **Crontab installed-then-removed** — `sudo crontab -u swell -r`. The pause was **deliberate**, not a failure. Reason inferred: trend-corpus B2 work absorbed the architecture; physical-trends lower priority than financial themes.

## 5. What's still open (technical)

1. **Scorer doesn't persist rows.** `scorer.py` exists at 5KB but `scores` table is empty despite 703 events fed in. Either a silent exception or a missing INSERT. Need to read the file and trace. *~30 min*.
2. **Stage-validation regression test missing.** Calibration plan in §3 is a manual eyeball test. Should be a `python -m swell test-calibration` that asserts `pickleball.composite > 0.6 AND crossfit.composite < 0.4` — fails CI if scorer drifts. *~1 hr*.
3. **Cron schedule not installed.** Bash history shows `crontab -u swell -r` as last act. Re-install requires deciding whether to drive from `/etc/cron.d/swell-corpus` (sector pattern) or per-user crontab (peptide pattern). *~15 min*.
4. **Not in a repo.** Now fixed locally — `~/swell-checker/` at commit `c02b516`. Needs decision on public/private + push target. **Recommend:** `P-U-C/swell-checker` (private until product shape decided).
5. **No connection to trend-intel-private or convergence-latest.** The B2 work explicitly excluded swell-checker. If we want consumer-trend signals to flow into the same dashboard, need an exporter analogous to `export-aggregates`. *~2 hr*.
6. **Open discovery gap (Phase 2).** Currently bound to the 24 hand-curated candidates. Phase 2 spec was "auto-extract candidate trends from prose" — would let it find emerging trends the operator hasn't seen yet.

## 6. Integration angles with the rest of the stack

This is the part Chad flagged: *"directly related to the other work that we are doing"*.

### a. swell-checker → trend-corpus mirror

The architecture maps 1:1 onto trend-corpus's theme_runtime pattern:

| swell-checker | trend-corpus |
|---|---|
| candidates.yaml | themes/*.yaml + entities.yaml |
| sources.yaml | sources.txt per theme |
| events (typed) | claims (typed) |
| scores | convergence-latest |
| watchlist Telegram | daily digest Telegram |

**Cleanest path: re-implement swell-checker as a `theme_runtime` instance** with `theme_id=consumer-trends` and per-trend sub-themes. Reuses ingest/extract/aggregate/digest infrastructure already battle-tested across 14 themes. Eliminates a parallel codebase to maintain.

**Trade-off:** the three-signal scoring model (velocity/spread/vocabulary) is genuinely different from trend-corpus's convergence model. Would either need to be ported INTO theme_runtime or kept as a separate scorer downstream.

### b. swell-checker → business-guy

Chad's framing: *"when a new trend is emerging in swell-checker, find a niche in that market to optimize"*.

Concretely: when swell-checker fires (composite ≥ 0.55) on a candidate like `padel_us`, business-guy's IG-follower scraper auto-seeds against the top 20-30 padel accounts. Niche selection becomes data-driven instead of operator-curated.

**Required wiring:**
- swell-checker emits a `promoted` event when a candidate crosses threshold
- business-guy adds a niche-template generator that takes `candidate_slug` + a seed-account list and writes a config block to `niches.yaml`
- Operator approval gate between (auto-promotion fires Telegram alert; operator clicks through to activate)

This is the strongest argument for un-parking swell-checker — it provides the **input** to business-guy that otherwise requires the operator to hand-pick niches.

### c. swell-checker as a standalone product

Independent of business-guy, the weekly watchlist itself is sellable:
- Substack/Telegram channel for consumer-trend forecasting
- Audience: e-commerce operators, brand managers, VCs, content creators
- Price: $20-50/mo for the digest, $200-500/mo for full event-stream API access
- Fulfillment: existing pipeline emits the digest; selling is a marketing problem

## 7. Recommended next actions

In priority order:

1. **Decide repo destination.** Push the imported source to `P-U-C/swell-checker` (private, like puc-trading). Pre-flight that the existing P-U-C PAT has write access to a new repo. *~15 min once decided.*
2. **Fix the scorer.** Run `scorer.py` against the existing 703 events with debug logging; identify why scores aren't persisting. *~30 min.*
3. **Run a fresh ingest cycle.** Data is 26 days stale; need a clean read to validate the calibration premise. *~1 hr clock time.*
4. **Eyeball calibration.** After step 2-3, do scores rank `padel_us`/`run_clubs` high and `crossfit` low? If yes → tune threshold and proceed. If no → debug extract prompts. *~1 hr.*
5. **Decide architecture: integrate or standalone.** §6.a (theme_runtime mirror) vs keep separate. Drives the rest of the work.
6. **(Operator research)** Chad's "larger research to verify next steps" — feedback into §6 product framing.

## 8. Files in this repo (commit c02b516)

```
swell-checker/
├── README.md            5.2K  -- canonical reference, parked-state intro
├── STATUS.md            this file
├── candidates.yaml      5.2K  -- 24 trends with stage labels + notes
├── sources.yaml         6.2K  -- Reddit/RSS/Trends URLs per candidate
├── schema.sql           2.9K  -- 5-table SQLite schema
├── scorer_config.yaml   251B  -- tunable thresholds + weights
├── seed.py              1.1K  -- idempotent candidate insert
├── ingest.py           11.5K  -- Reddit/RSS/Trends fetcher with dedup
├── extract.py          11.9K  -- Sonnet-driven typed-event extraction
├── scorer.py            5.2K  -- composite computation (open: §5.1)
├── watchlist.py         3.8K  -- weekly ranked digest
├── status.py            2.5K  -- corpus health check
├── health.py            1.8K  -- auth + CLI verification
├── notify.py            2.5K  -- Telegram wiring
├── cron-wrap.sh         702B  -- failure-alert wrapper
├── run.sh               493B  -- single-command entrypoint
├── migrate_v1_to_v2.sh  2.3K  -- v1 → v2 schema migration
├── prompts/
│   ├── extract.md           -- Reddit/RSS extraction prompt
│   └── extract_general.md   -- prose extraction prompt (Trends results, etc.)
└── out/                 (empty -- watchlist target)
```

Not included: `db.sqlite` (827K, on city-worker only), `.env` (Telegram creds, on city-worker only), `__pycache__`.

The live database on city-worker has the 703-event corpus and is the source of truth for the calibration analysis. A copy can be pulled if needed for offline scorer debugging.
