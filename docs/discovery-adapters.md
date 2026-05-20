# Discovery Adapters

`discover.py --run` executes the enabled discovery adapters sequentially. They
write only to `proposed_candidates` and `proposal_evidence`; promotion still
puts candidates into `status='observing'` before they become router-eligible.

## Adapters

- `general_feed`: runs the existing LLM discovery prompt over unprocessed
  `__general__` RSS fetches.
- `google_related`: uses `GoogleRelatedProvider` to call pytrends
  related/rising queries for tracked candidates with Trends sources. It also
  samples daily Google trending searches as a lower-confidence
  `google_trending` surface.
- `tiktok`: uses `TikTokCreativeProvider` to query TikTok Creative Center
  hashtag data for configured lifestyle categories.
- `reddit_growing`: uses `RedditSubscriberDeltaProvider` to snapshot watched
  subreddit subscriber counts and emit proposals only after sustained growth.

## Provider State

Network-backed providers are guarded by `provider_state`, one row per provider:

- `last_success_at`, `last_failure_at`, and `last_failure_message`
- `consecutive_failures`, `success_count`, and `failure_count`
- `disabled_until` for circuit-breaker lockouts
- `notes` for the last useful success context

Every adapter checks `ProviderState.is_available()` before calling its upstream
provider. A provider is unavailable while `disabled_until` is in the future, or
while the last provider call is still inside that provider's cooldown window.
Success resets `consecutive_failures`; failures increment it. When a threshold
is reached, the provider is disabled and `notify.py` sends a fire-and-forget
Telegram alert if Telegram env vars are configured.

Current policy:

| provider | cooldown | disable threshold | disabled for |
| --- | ---: | ---: | ---: |
| `google_related_pytrends` | 90s | 3 failures | 24h |
| `google_trending_pytrends` | 90s | 3 failures | 24h |
| `tiktok_creative_center` | 60s | 5 failures | 6h |
| `reddit_subscriber_delta` | 30s | 10 failures | 1h |

Operator commands:

```bash
.venv/bin/python discover.py --provider-status
.venv/bin/python discover.py --provider-reset tiktok_creative_center
```

`--provider-status` prints availability, last success, consecutive failures,
disabled-until time, and the last note/error. `--provider-reset <name>` clears
`disabled_until`, failure count, and the last failure message for one provider.

## Google Trends

Google related/rising discovery no longer sleeps randomly inside the provider.
Instead, `ProviderState` enforces a 90 second cooldown before each
`pytrends.related_queries()` call. The adapter also caps related-query seed
attempts at 5 per run; override with:

```bash
GOOGLE_RELATED_MAX_SEEDS_PER_RUN=3 .venv/bin/python discover.py --run --adapter google_related
```

The adapter records seed-query attempts in `provider_seed_query_history`, keyed
by provider, `seed_slug`, and date. The same seed is not re-queried twice on the
same UTC date, even if the previous attempt produced no proposals.

HTTP 429s force a 24 hour disable for the relevant Google provider state,
because the free pytrends surface often resets on a daily cadence.

## TikTok Creative Center

`TikTokCreativeProvider` still tries the public Creative Center API/HTML paths.
If `TIKTOK_COOKIE` is present, it is sent as a `Cookie:` header:

```bash
TIKTOK_COOKIE='...' .venv/bin/python discover.py --run --adapter tiktok
```

If the cookie expires, TikTok typically returns 401/403 or empty records. Those
now count as provider failures and eventually disable `tiktok_creative_center`;
the status command will show the last error. Refresh workflow:

1. Open TikTok Creative Center in a browser while logged in.
2. Copy the current request cookie into `.env` as `TIKTOK_COOKIE=...`.
3. Run `.venv/bin/python discover.py --provider-reset tiktok_creative_center`.
4. Run a small smoke test: `.venv/bin/python discover.py --run --adapter tiktok --dry-run --limit-per-adapter 5`.

Categories are configurable with `TIKTOK_CATEGORIES`, comma-separated. Default:

```text
fitness,beauty_personal_care,lifestyle,food_beverage,wellness_health
```

## Reddit Subscriber Delta

The previous Reddit growing-subreddits approach used public popular/growth
endpoints, PRAW, and third-party fallbacks. Those surfaces were too frequently
blocked from the worker IP, so `reddit_growing` now watches a configured set of
subreddits and fetches each subreddit directly from:

```text
https://www.reddit.com/r/<subreddit>/about.json
```

Each run upserts today's subscriber count into
`subreddit_subscriber_history`. A subreddit can emit a proposal only when:

- it has at least 14 days of observed history,
- the closest snapshot at least 7 days old exists,
- subscriber count is up more than 5 percent versus that baseline,
- it is not in `REDDIT_GENERIC_SUBS`, and
- its name/description matches the lifestyle keyword whitelist.

The watched list lives in `sources.yaml` under the top-level
`discovery_reddit_seeds:` block:

```yaml
discovery_reddit_seeds:
  - {subreddit: "RunClub", category: "fitness_social"}
  - {subreddit: "pilates", category: "studio_fitness"}
  - {subreddit: "skincareaddiction", category: "beauty"}
```

Tests should mock provider classes or inject fake sessions rather than calling
pytrends, TikTok, or Reddit directly.
