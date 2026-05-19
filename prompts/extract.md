You are extracting typed signal events from a source document about the trend "{candidate_name}" (category: {category}).

Your job is to identify observable signals that would indicate whether this trend is in growth, plateau, or decline. Each event must be a single, concrete, verifiable observation with a direct quote from the source.

EVENT TYPES:
- mention: the trend is being discussed with positive energy, engagement, or growth language. Higher magnitude = stronger signal.
- operator: a new venue, gym, club, brand, studio, chain, or business entity has launched or expanded in this category. Magnitude = rough count (1 venue = 1.0, 10 venues = 10.0, log-scale for large numbers).
- cohort: a specific person, community, influencer, or institutional adopter has embraced this trend (e.g., "Attia discusses on podcast", "r/running moderators launch r/runclub").
- vocabulary: a new term, protocol name, sub-discipline, or category vocabulary has emerged (e.g., "hybrid fitness" for Hyrox-style racing, "cold-plunge circuit" as a workout format). Magnitude 1.0 unless multiple new terms.
- geographic: a new city, country, or region has the trend arriving for the first time. Magnitude = count of new locations.
- media: the trend is covered by mainstream media (NYT, WSJ, national TV, ESPN broadcast, etc.). Magnitude: 1=local news, 2=trade press, 3=national mainstream.
- funding: VC investment, M&A, IPO, or major corporate investment in the space.
- adjacent: a new product category, apparel brand, coaching industry, certification body, or ecosystem business has spawned around this trend.
- disruption: a negative signal - saturation, closure, regulatory loss, cultural backlash. Use NEGATIVE magnitude.

CRITICAL RULES:
- Each event MUST include a verbatim evidence_quote from the source, <= 300 characters, copied exactly.
- If the source is just general chatter with no concrete observable signal, output nothing.
- If the source is about a different topic entirely, output nothing.
- Prefer precise dates; use fetch date ({fetch_date}) only if the event is clearly "recent".
- Skip promotional content, opinion pieces without new facts, and predictions.
- A Reddit post announcing "just opened my first X" → operator event. A Reddit post saying "I love X" → NOT an event (ambient noise).

OUTPUT FORMAT:
One JSON object per line (JSONL). No preamble, no markdown fences.

{
  "event_type": "mention | operator | cohort | vocabulary | geographic | media | funding | adjacent | disruption",
  "magnitude": <float>,
  "event_date": "YYYY-MM-DD",
  "evidence_quote": "verbatim passage from source, <= 300 chars",
  "confidence": <0.0-1.0>
}

CANDIDATE: {candidate_name}
CATEGORY: {category}
SOURCE TYPE: {source_type}
SOURCE URL: {source_url}
FETCH DATE: {fetch_date}

SOURCE TEXT:
{text}
