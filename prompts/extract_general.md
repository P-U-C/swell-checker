You are reading articles from a general-audience publication ({source_label}).
Your job is to identify whether any article is about one of the tracked trends listed below, and if so, extract a signal event for that trend.

Most articles in a general feed are NOT about any tracked trend. Output nothing for those.
Only emit an event when an article clearly concerns a specific tracked trend.

TRACKED TRENDS (slug : description):
{candidate_list}

EVENT TYPES:
- mention: the trend is being discussed with positive energy, engagement, or growth language.
- operator: a new venue, gym, club, brand, studio, chain, or business has launched/expanded.
- cohort: a specific person, community, or institutional adopter has embraced this trend.
- vocabulary: a new term, protocol name, sub-discipline emerged.
- geographic: new city/region arrival.
- media: mainstream coverage (the fact that this ARTICLE exists is already a media signal).
- funding: VC/M&A/IPO in the space.
- adjacent: new product category / apparel brand / certification body spawned.
- disruption: saturation, closure, regulatory loss, cultural backlash. NEGATIVE magnitude.

CRITICAL RULES:
- You MUST output exactly one event per article that matches a tracked trend. Zero events for articles that don't match.
- An article being in a general feed IS itself a media signal. If you emit a media event, magnitude should be 2 or 3 (this is broader coverage than niche press).
- evidence_quote must be verbatim from the source, <= 300 chars.
- When uncertain which trend an article is about, DO NOT guess. Output nothing.
- Match on strong textual signal, not vibes. "Padel tournament in Boston" → padel_us. "New fitness studio opens" alone → NOT enough, skip.

OUTPUT FORMAT (JSONL, one per matched article, include candidate_slug):

{
  "candidate_slug": "slug from the list above",
  "event_type": "mention | operator | cohort | vocabulary | geographic | media | funding | adjacent | disruption",
  "magnitude": <float>,
  "event_date": "YYYY-MM-DD",
  "evidence_quote": "verbatim, <= 300 chars",
  "confidence": <0.0-1.0>
}

SOURCE: {source_label}
SOURCE URL: {source_url}
FETCH DATE: {fetch_date}

ARTICLES:
{text}
