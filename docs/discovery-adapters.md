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
  hashtag data for fitness, beauty, lifestyle, food, and wellness/health
  categories.
- `reddit_growing`: uses `RedditGrowingProvider` to try Reddit popular
  subreddits first, then PRAW credentials if available, then third-party growth
  JSON fallbacks. Treat this surface as recall-oriented and noisy.

## Provider Boundaries

The network clients are wrapped in provider classes so tests and future API
replacements do not touch discovery flow code:

- `GoogleRelatedProvider.rising_for(query)`
- `GoogleRelatedProvider.trending_searches()`
- `TikTokCreativeProvider.trending_hashtags(category)`
- `RedditGrowingProvider.growing_subreddits()`

Tests should mock those providers rather than pytrends, TikTok, or Reddit
directly.

## Rate Limits And Blocking

Google related/rising calls sleep 3-5 seconds between pytrends requests and
back off on HTTP 429 before retrying once. TikTok and Reddit providers also
back off once on 429, but otherwise degrade to an empty result for that adapter
instead of failing the whole discovery run.

TikTok Creative Center is a public page but may return permission errors from
its dashboard API. The provider tries the current Creative Center API paths with
browser headers and a cookie jar, then falls back to parsing embedded Next.js
JSON from the public hashtag page.

Reddit's unauthenticated JSON endpoints can return 403 from some hosts. The
provider keeps the official Reddit path first, uses PRAW when configured via the
existing Reddit environment variables, and only then tries third-party fallback
JSON.
