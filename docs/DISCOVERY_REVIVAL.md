# Discovery Revival Review

Date reviewed: 2026-05-29

This report covers the Foreshore discovery queue freeze in `swell-checker`.
I did not run `discover.py --run` and did not invoke the `claude` CLI. All
runtime evidence below comes from read-only SQLite queries, static code review,
`crontab -l`, and the permitted context files in `~/editorial` and `~/scripts`.

## Executive Summary

The proposal queue is frozen at 2026-05-20. The raw corpus is not frozen:
`fetches.fetched_at` reaches 2026-05-29 and `events.created_at` reaches
2026-05-28. The break is therefore in the discovery proposal step, not in
ingest/extract.

There are two separate operational problems:

1. No discovery queue-warming cron exists on this clawd box. `crontab -l` has
   the daily peptides pull at 13:40 UTC and the Foreshore weekly at 15:00 UTC
   Monday, but no `discover.py --run` or wrapper.
2. The canonical DB copied from peptides also shows no discovery provider
   state updates after 2026-05-20. That means the canonical discovery process
   either has not been scheduled, has not run, or has failed before it reached
   provider-state writes since then.

The code had one major single point of failure before this patch: the
`general_feed` adapter called `claude -p ...`; an auth failure returned `None`,
causing the full adapter runner to return `None` and the CLI to exit `2` before
the rest of the adapters could help. That matches the operator note about the
discovery user losing Claude auth, but this replica does not contain a direct
post-2026-05-20 Claude error log proving that auth was the only cause.

This patch makes discovery resilient and adds a cron-safe wrapper:

- `discover.py --no-claude` now runs a conservative heuristic general-feed
  fallback without invoking Claude.
- Claude auth/runtime failures in `general_feed` now fall back to heuristics
  instead of aborting the full run.
- The adapter runner now catches failures per adapter, logs a summary, and
  exits nonzero only when every selected adapter failed.
- `discover-warm-queue.sh` gives the operator one idempotent cron target.

## Verified Evidence

Read-only DB checks against `db.sqlite` on clawd:

```text
proposed_candidates_total        20
proposal_min_created_at          2026-05-20 05:27:54
proposal_max_created_at          2026-05-20 19:11:38
provider_state_max_updated_at    2026-05-20 19:31:20
fetches_max_fetched_at           2026-05-29 13:02:58
events_max_created_at            2026-05-28 13:33:23
```

Proposal/evidence dates:

```text
proposed_candidates by date:
2026-05-20 | 20

proposal_evidence by surface:
general_feed    | 18 | 2026-05-20 05:27:54 | 2026-05-20 14:12:43
google_related  |  2 | 2026-05-20 19:11:38 | 2026-05-20 19:11:38
```

Provider state:

```text
google_related_pytrends   last_success=2026-05-20 19:11:38  failures=0  note=seed=barre query=barre class
google_trending_pytrends  last_success=NULL                 failures=0
reddit_subscriber_delta   last_success=2026-05-20 19:11:17  failures=0  note=snapshots=32
tiktok_creative_center    last_success=NULL                 failures=1  disabled_until=2026-06-19 19:31:20
```

Fetch/event freshness:

```text
fetches by date:
2026-05-29 | 51
2026-05-28 | 76
2026-05-27 | 76

events by date:
2026-05-28 | 458
2026-05-27 | 276
```

Pull log:

```text
[2026-05-28T13:40:02Z] pulled peptides swell db (events=3324)
[2026-05-29T13:40:01Z] pulled peptides swell db (events=3324)
```

Crontab on clawd contains:

```cron
0 15 * * 1 cd /home/ubuntu/editorial && ./workflow/nightly.sh foreshore >> /home/ubuntu/editorial/nightly.log 2>&1
40 13 * * * /home/ubuntu/scripts/pull-swell-from-peptides.sh >> /home/ubuntu/swell-checker/pull.log 2>&1
```

It does not contain a `discover.py --run` cron or `discover-warm-queue.sh` cron.

## Architecture Map

`/home/ubuntu/scripts/pull-swell-from-peptides.sh` explicitly says peptides
`worker-1` is canonical and clawd is a read-replica for editorial/Foreshore. It
pulls:

```text
REMOTE_DB=/home/ubuntu/swell-checker/db.sqlite
LOCAL_DB=/home/ubuntu/swell-checker/db.sqlite
```

The editorial side reads the local clawd DB by default:

```python
FORESHORE_DISCOVER_DIR = "/home/ubuntu/swell-checker"
FORESHORE_DISCOVER_PYTHON = "python3"
FORESHORE_DISCOVER_DB = "db.sqlite"
```

`source_to_issue.py` calls `discover.py --list-pending` and `discover.py --show`
through that local path. It does not run discovery. It only consumes whatever is
already in `proposed_candidates`.

Recommended steady state:

- Run discovery on peptides, the canonical/residential worker.
- Schedule it before clawd's 13:40 UTC DB pull.
- Let the 13:40 pull copy fresh proposals to clawd.
- Let Foreshore consume the copied queue at 15:00 UTC Monday.

Running discovery on clawd can work as an emergency stopgap for adapters that do
not need a residential IP, but it mutates the read-replica and those local writes
will be overwritten by the next successful peptides pull.

## Pipeline Trace

### CLI entry

`discover.py --run` resolves the selected adapter through `resolve_adapters()`.
With no adapter it runs:

```python
("general_feed", "google_related", "tiktok", "reddit_growing")
```

The runner then executes adapters sequentially and writes proposals to
`proposed_candidates` plus evidence rows to `proposal_evidence`. Promotion is
separate and operator-gated through `--approve`, `--reject`, and `--promote`.

### Proposal storage

`upsert_proposal()` normalizes the slug, inserts a new proposal, or bumps an
existing proposal's `support_count` and `last_seen_at`. `insert_evidence()` adds
surface-specific proof. This is the correct sidecar model: nothing reaches the
watchlist/router until an operator promotes a proposal into `candidates`.

### `general_feed`

Inputs:

- `fetches` rows whose source belongs to candidate slug `__general__`
- `raw_text IS NOT NULL`
- no existing `proposal_evidence.fetch_id` for that fetch

Original path:

1. Load `prompts/discover_general.md`.
2. Build a block of already tracked candidates.
3. Substitute source label, URL, fetch date, tracked slugs, and raw article text.
4. Call `run_claude()`, which shells out to:

   ```text
   claude -p <prompt> --model <model> --output-format text
   ```

5. Parse JSONL objects with `type="proposal"`.
6. Dedup against existing candidates/proposals.
7. Upsert the proposal and insert evidence.

Why this was a single point of failure before this patch:

- `run_claude()` raises `AuthError` on auth/login/credential markers.
- `run_general_feed_discovery()` returned `None` on `AuthError`.
- `run_discovery_adapters()` returned `None` when any adapter returned `None`.
- `main()` returned exit `2`.
- Since `general_feed` is the first default adapter, Claude auth failure could
  prevent all later default adapters from doing useful work.

Current patched behavior:

- `--no-claude` skips `run_claude()` entirely.
- `AuthError`, timeout, and Claude runtime errors fall back to a conservative
  no-LLM heuristic over article titles/descriptions.
- The first auth failure disables Claude for the rest of that adapter run so it
  does not keep retrying the CLI for every fetch.

### `google_related`

Inputs:

- tracking candidates with `sources.source_type='trends'`

Provider:

- `GoogleRelatedProvider`
- `pytrends.request.TrendReq`
- `related_queries()` for seed-adjacent rising queries
- `today_searches()` or `trending_searches()` for broader daily trends

Guards:

- `ProviderState` rows:
  - `google_related_pytrends`
  - `google_trending_pytrends`
- 90 second cooldown
- disable after 3 failures for 24 hours
- `GOOGLE_RELATED_MAX_SEEDS_PER_RUN`, default 5
- `provider_seed_query_history` prevents re-querying the same seed twice on the
  same UTC date.

Dependencies:

- outbound HTTPS to Google Trends
- `pytrends` installed from repo requirements
- no Claude
- no API key
- no residential IP requirement known, though Google may rate-limit datacenter
  traffic

Status on this DB:

- last success 2026-05-20 19:11:38
- no consecutive failures
- not the source of the freeze by itself

Safe on clawd:

- Yes, with normal network caveats.
- Writes only to the local replica if run here.

### `tiktok`

Provider:

- `TikTokCreativeProvider`
- public TikTok Creative Center API/HTML endpoints
- optional `TIKTOK_COOKIE`
- optional `TIKTOK_CATEGORIES`

Guards:

- provider row `tiktok_creative_center`
- 60 second cooldown
- disable after 5 failures for 6 hours

Dependencies:

- outbound HTTPS to TikTok
- no Claude
- no residential IP requirement known
- optional cookie in `.env`

Status on this DB:

- last failure 2026-05-20
- disabled until 2026-06-19 19:31:20
- failure message says TikTok pulled/deprecated the Trends feature site-wide

Conclusion:

- TikTok is currently disabled and should not be treated as the primary cause
  of the full queue freeze.
- It can be reset only after the upstream surface works again or a fresh cookie
  is added.

### `reddit_growing`

Provider:

- `RedditSubscriberDeltaProvider`
- loads `discovery_reddit_seeds` from `sources.yaml`
- fetches `https://www.reddit.com/r/<subreddit>/about.json`
- records subscriber snapshots into `subreddit_subscriber_history`
- emits only when there is enough history and growth exceeds 5 percent

Dependencies:

- outbound HTTPS to Reddit
- no Claude
- no API key required for this adapter
- optional `SWELL_REDDIT_USER_AGENT`
- residential IP strongly preferred because Reddit public endpoints often block
  or degrade datacenter traffic

Status on this DB:

- last success 2026-05-20 19:11:17
- no consecutive failures
- subreddit history contains 32 snapshots, all from 2026-05-20

Safe on clawd:

- Not reliable. It may work for a few requests, but this is the adapter most
  likely to degrade on a non-residential IP.
- Prefer peptides/worker-1 for this adapter.

## Adapter Requirements Matrix

| Adapter | Network | Residential IP | Claude auth | Env/API requirements | Safe on clawd |
| --- | --- | --- | --- | --- | --- |
| `general_feed` | only existing DB fetches plus Claude unless `--no-claude` | no | yes unless `--no-claude` | none for heuristic; Claude login for LLM | yes only with `--no-claude`; do not invoke Claude on clawd |
| `google_related` | Google Trends via pytrends | not required, but rate-limit risk | no | `pytrends`; optional `GOOGLE_RELATED_MAX_SEEDS_PER_RUN` | yes |
| `tiktok` | TikTok Creative Center | not required | no | optional `TIKTOK_COOKIE`, `TIKTOK_CATEGORIES` | yes, but currently disabled upstream |
| `reddit_growing` | Reddit `about.json` | strongly preferred | no | optional `SWELL_REDDIT_USER_AGENT` | not recommended |

The current clawd `.env` has Telegram configuration only. It does not contain
`TIKTOK_COOKIE` or Reddit API credentials.

## Root Cause

The precise root cause is not a single provider-state lock.

What is proven:

- The proposal queue has not received any row after 2026-05-20.
- Provider state has not been updated after 2026-05-20.
- Raw fetches and extracted events continued through 2026-05-29/2026-05-28.
- The clawd crontab lacks a discovery cron.
- TikTok is disabled until 2026-06-19, but Google and Reddit provider states
  are not locked and show zero consecutive failures.

What is strongly indicated but must be confirmed on peptides:

- The operator note that the discovery user lost Claude auth is consistent with
  the old code path: the first default adapter (`general_feed`) could abort the
  full run on `AuthError`.
- Because provider state is stale for all providers, the canonical run likely
  has not been scheduled since 2026-05-20, or it failed before completing a
  provider-backed adapter.

Conclusion:

- Immediate cause for Foreshore starvation on clawd: missing queue-warming cron
  plus stale replicated proposals.
- Likely canonical cause: missing/failed discovery cron on peptides, with Claude
  auth failure as the most plausible first-adapter failure mode.
- Not sufficient as a full explanation: TikTok provider lock. It affects only
  the TikTok surface.

## Implemented Local Fixes

### Resilient adapter runner

`run_discovery_adapters()` now catches exceptions per adapter, records a failed
summary for that adapter, and continues to later adapters. It prints an overall
adapter summary with status, new proposal count, bumped count, evidence count,
and error/skip reason.

Exit policy:

- Exit `0` if at least one selected adapter completed or was skipped by provider
  state.
- Exit `2` only if every selected adapter failed.

This is suitable for cron because one broken upstream no longer zeros the whole
proposal run, but a totally dead run still alerts through `cron-wrap.sh`.

### `--no-claude` heuristic fallback

`discover.py --run --no-claude` never invokes the `claude` CLI. For the
`general_feed` adapter it parses RSS-style `TITLE`/`DATE`/`DESC` chunks already
present in `fetches.raw_text`, matches explicit health/fitness/lifestyle
keywords, and emits low-confidence proposal objects.

This fallback is intentionally conservative. It will miss many good discoveries
that Claude would catch, but it keeps Foreshore from going empty when Claude is
unavailable.

### Claude auth/runtime fallback

When `--no-claude` is not set, `general_feed` still tries Claude first. If it
gets an auth failure, timeout, or runtime failure, it falls back to heuristics.
On auth failure it disables further Claude calls for the rest of that adapter
run.

This does not mean clawd should invoke Claude. The wrapper defaults to
`SWELL_DISCOVERY_NO_CLAUDE=1` for this reason.

### Cron wrapper

Added `discover-warm-queue.sh`.

Defaults:

```text
SWELL_DISCOVERY_ADAPTERS=general_feed,google_related
SWELL_DISCOVERY_NO_CLAUDE=1
SWELL_DISCOVERY_LIMIT_PER_ADAPTER=20
SWELL_DISCOVERY_MODEL=sonnet
```

The default is safe for clawd because it does not invoke Claude and does not run
Reddit. On peptides, the operator can set `SWELL_DISCOVERY_NO_CLAUDE=0` after
Claude is re-authenticated and include `reddit_growing`.

## Cron Lines To Install

Do not install both long-term. The canonical peptides line is the preferred
steady-state fix.

### Preferred: canonical peptides/worker-1

Install on the peptides/worker-1 user that owns `/home/ubuntu/swell-checker`.
This runs before the clawd 13:40 UTC pull and before Foreshore at 15:00 UTC.

```cron
20 13 * * 1 cd /home/ubuntu/swell-checker && SWELL_DISCOVERY_ADAPTERS=general_feed,google_related,reddit_growing SWELL_DISCOVERY_NO_CLAUDE=0 ./cron-wrap.sh discover ./discover-warm-queue.sh >> /home/ubuntu/swell-checker/discover-run.log 2>&1
```

Use this only after re-authing Claude for the discovery user on peptides. If
Claude cannot be re-authenticated immediately, use the same line with
`SWELL_DISCOVERY_NO_CLAUDE=1` as a temporary degraded mode.

### Emergency clawd replica stopgap

This writes to clawd's local replica only and will be overwritten by the next
successful peptides pull. It is useful only to warm Monday Foreshore while the
canonical worker is being repaired.

```cron
50 14 * * 1 cd /home/ubuntu/swell-checker && SWELL_DISCOVERY_ADAPTERS=general_feed,google_related SWELL_DISCOVERY_NO_CLAUDE=1 ./cron-wrap.sh discover ./discover-warm-queue.sh >> /home/ubuntu/swell-checker/discover-run.log 2>&1
```

Do not remove the existing 13:40 pull or the 15:00 Foreshore nightly.

## Manual Operator Steps

These steps must be done by the operator; I did not perform them.

1. On peptides/worker-1, log in as the Unix user that will run discovery.

   ```bash
   ssh ubuntu@192.168.6.130
   cd /home/ubuntu/swell-checker
   ```

2. Re-auth Claude on peptides only. Do not do this on clawd.

   ```bash
   claude
   ```

   Complete the login flow, then quit the interactive CLI.

3. On peptides, run the health check if it is safe for that host/user.

   ```bash
   python3 health.py
   ```

4. Install the preferred cron line on peptides by merging it into the existing
   crontab. Do not blindly replace the whole crontab unless that is intended.

   ```bash
   crontab -l > /tmp/swell.cron
   sensible-editor /tmp/swell.cron
   crontab /tmp/swell.cron
   ```

5. Run one live discovery on peptides after re-auth, or run degraded no-Claude
   mode if Claude is still unavailable.

   Full canonical mode:

   ```bash
   cd /home/ubuntu/swell-checker
   SWELL_DISCOVERY_ADAPTERS=general_feed,google_related,reddit_growing SWELL_DISCOVERY_NO_CLAUDE=0 ./discover-warm-queue.sh
   ```

   Degraded no-Claude mode:

   ```bash
   cd /home/ubuntu/swell-checker
   SWELL_DISCOVERY_ADAPTERS=general_feed,google_related SWELL_DISCOVERY_NO_CLAUDE=1 ./discover-warm-queue.sh
   ```

6. Copy the canonical DB to clawd, or wait for the 13:40 UTC pull.

   ```bash
   /home/ubuntu/scripts/pull-swell-from-peptides.sh
   ```

7. On clawd, verify proposals are fresh without invoking Claude.

   ```bash
   cd /home/ubuntu/swell-checker
   python3 discover.py --list-pending
   sqlite3 'file:db.sqlite?mode=ro' "SELECT date(created_at), COUNT(*) FROM proposed_candidates GROUP BY date(created_at) ORDER BY date(created_at) DESC LIMIT 5;"
   sqlite3 'file:db.sqlite?mode=ro' "SELECT provider_name, updated_at, last_failure_message FROM provider_state ORDER BY provider_name;"
   ```

8. If TikTok should be revived later, wait until Creative Center trend endpoints
   work again, add a fresh `TIKTOK_COOKIE` if required, then run on the canonical
   worker:

   ```bash
   python3 discover.py --provider-reset tiktok_creative_center
   ```

## Validation Performed

Safe local checks run on clawd:

```bash
python3 -m py_compile discover.py
bash -n discover-warm-queue.sh
python3 -m unittest tests/test_discover.py
```

The unit suite passed: 25 tests.

No live discovery run was executed. No Claude CLI command was invoked.
