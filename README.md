# swell-checker v0

Autonomous trend-emergence detector. Watches a curated list of physical/lifestyle
trends, extracts typed events from Reddit/RSS/News, scores them on a three-signal
growth model (velocity + spread + vocabulary), emits a weekly watchlist to Telegram.

**Current scope (v0):**
- Curated candidate list (22 trends, edit `candidates.yaml`)
- Physical/lifestyle trends only
- Weekly Telegram watchlist digest
- Shared VM + Telegram bot with peptide-corpus; distinguished by 🌊 prefix

**Deliberately deferred to Phase 2:**
- Open discovery (auto-extracting candidate trends from prose)
- Thesis briefs (Opus-authored narratives for promoted candidates)
- Threshold alerts (mid-week "candidate X just crossed")
- Operator-build vs. investor-angle lens separation

## Files

```
swell-checker/
├── schema.sql           # SQLite schema
├── candidates.yaml      # Seeded trends to track (22 trends in v0)
├── sources.yaml         # Per-candidate source URLs
├── scorer_config.yaml   # Tunable scorer thresholds
├── seed.py              # Insert candidates from yaml (idempotent)
├── ingest.py            # Fetch all sources on cron (Reddit JSON, RSS, Trends)
├── extract.py           # Events from fetches via Sonnet
├── scorer.py            # Compute composite scores (velocity + spread + vocabulary)
├── watchlist.py         # Weekly ranked digest
├── notify.py            # Telegram (shared bot with peptide-corpus, 🌊 prefix)
├── status.py            # Corpus health check
├── health.py            # Auth + CLI verification
├── cron-wrap.sh         # Cron wrapper with Telegram alerts on failure
├── run.sh               # Single-command entrypoint
├── setup.sh             # One-shot VM setup (run as root)
└── prompts/extract.md
```

## First-run workflow

On `city-worker-301` (same VM as peptide-corpus):

```bash
# 1. Upload the tarball
scp swell-checker.tar.gz city-worker-peptides:~/

# 2. Run setup
ssh city-worker-peptides
tar -xzf swell-checker.tar.gz
sudo bash swell-checker/setup.sh

# 3. One-time OAuth login as swell user
sudo -iu swell
claude
# Choose "Log in with Claude", follow the URL, paste code back, /quit
python3 swell-checker/health.py

# 4. Copy Telegram creds from peptide-corpus
exit  # back to ubuntu
sudo cp /home/peptide/peptide-corpus/.env /home/swell/swell-checker/.env
sudo chown swell:swell /home/swell/swell-checker/.env
sudo chmod 600 /home/swell/swell-checker/.env

# 5. First test run
sudo -iu swell
cd swell-checker
python3 ingest.py --limit 3      # test 3 candidates
python3 extract.py --limit 5
python3 scorer.py
python3 watchlist.py             # preview Monday's digest
```

## Cron schedule (auto-installed)

All times UTC, offset from peptide-corpus to spread load on the sub:

- **:30 every 6h** — ingest (fetches all tracked candidates' sources)
- **:45 every 6h** — extract (parses fetches into typed events)
- **12:45 daily** — health check (auth + CLI verification)
- **Monday 14:00 UTC** — weekly watchlist → Telegram

## Scoring logic (quick reference)

**Three signals, each 0-1:**

1. `velocity` — weighted count of mention/media/cohort/funding/adjacent events in trailing 18 months
2. `spread` — operator + geographic events in trailing 24 months (log-scaled)
3. `vocabulary` — positive/negative vocabulary events all-time

**Composite** = 0.4·velocity + 0.4·spread + 0.2·vocabulary, damped by disruption penalty.

Fires at composite ≥ 0.55. Edit `scorer_config.yaml` to tune.

## Calibration period (first 4 weeks)

Do not act on the watchlist output for the first month. Treat it as observation:

- **Week 1-2:** Are calibration candidates (pickleball, hyrox) scoring high? Is axe throwing scoring low? If not, the extraction pipeline isn't producing useful events.
- **Week 3-4:** Tune `scorer_config.yaml`. Try lowering threshold to 0.45 → see what newly fires. Does it match your intuition?
- **Month 2:** Actually act on signals.

## When to upgrade (do not pre-build)

| Problem | Add |
|---|---|
| Curated list feels too narrow | Phase 2: open discovery |
| Want briefs, not just watchlist | Phase 2: Opus-authored thesis briefs when a candidate fires |
| Want alerts between weekly digests | Phase 2: threshold-crossing alerts |
| Digital/financial trends are a gap | v1: separate `swell-digital` system with different sources |
| Weekly digest too crowded | Filter to "stage=approaching" candidates only in watchlist |
| Hitting sub rate limits | Reduce cron frequency to every 12h, or drop lower-signal sources |

## Sharing infrastructure with peptide-corpus

- **Same VM**: city-worker-301
- **Same Claude Code sub**: OAuth via swell user's ~/.claude/
- **Same Telegram bot**: @alpha_man_bot (distinguished by 🌊 vs 📊 prefix)
- **Same Telegram chat**: DM with zozDOTeth
- **Different service user**: `swell` (separate from `peptide`)
- **Different DB**: `/home/swell/swell-checker/db.sqlite`
- **Different cron**: installed under `swell` user's crontab
- **Different log files**: `/var/log/swell-*.log`

If the shared-chat volume gets noisy, create a second Telegram bot, add to same chat. The 🌊 prefix makes sorting easy.
