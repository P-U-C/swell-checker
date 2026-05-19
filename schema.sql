-- swell-checker schema
-- SQLite, WAL mode, append-only events

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Tracked trends (seeded from candidates.yaml, can be added later)
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,            -- e.g. "padel_us"
    display_name TEXT NOT NULL,           -- e.g. "Padel (US)"
    category TEXT NOT NULL,               -- e.g. "racquet_sport"
    stage TEXT DEFAULT 'uncalibrated',     -- approaching | very_early | calibration | calibration_fizzled
    status TEXT DEFAULT 'tracking',       -- tracking | paused | promoted | dropped
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Sources: URL or query we fetch. Tied to a candidate.
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    source_type TEXT NOT NULL,            -- reddit | trends | rss | meetup | news
    url TEXT NOT NULL,
    label TEXT,                           -- "r/padel", "Google Trends: padel", etc
    last_fetched_at TIMESTAMP,
    last_error TEXT,
    UNIQUE(candidate_id, url)
);

-- Fetches: each ingest cycle produces one row per source with raw_text and fetched_at.
-- Events are extracted from these. Raw_text is kept for a rolling window then pruned.
CREATE TABLE IF NOT EXISTS fetches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    raw_text TEXT,
    processed BOOLEAN DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS fetches_processed_idx ON fetches(processed) WHERE processed = 0;

-- Events: the atomic signal. Typed.
-- Types: mention | operator | cohort | vocabulary | geographic | media | funding | adjacent | disruption
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    fetch_id INTEGER REFERENCES fetches(id),
    event_type TEXT NOT NULL,
    magnitude REAL DEFAULT 1.0,
    event_date DATE NOT NULL,             -- Date the signal occurred, not when extracted
    evidence_quote TEXT NOT NULL,         -- Verbatim from source
    source_url TEXT,
    confidence REAL DEFAULT 0.7,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS events_candidate_idx ON events(candidate_id, event_date);
CREATE INDEX IF NOT EXISTS events_type_idx ON events(event_type);

-- Score snapshots - one row per candidate per weekly scoring run
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    as_of DATE NOT NULL,
    velocity REAL NOT NULL,
    spread REAL NOT NULL,
    vocabulary REAL NOT NULL,
    composite REAL NOT NULL,
    would_fire BOOLEAN NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(candidate_id, as_of)
);
CREATE INDEX IF NOT EXISTS scores_candidate_idx ON scores(candidate_id, as_of);

-- Assistant/router events: fired scores converted into downstream action intents.
CREATE TABLE IF NOT EXISTS router_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    as_of DATE NOT NULL,
    candidate_slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    stage TEXT,
    composite REAL NOT NULL,
    playbook TEXT NOT NULL,
    route_status TEXT DEFAULT 'pending_approval', -- pending_approval | approved | rejected | executed
    payload_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(candidate_id, as_of, playbook)
);
CREATE INDEX IF NOT EXISTS router_events_status_idx ON router_events(route_status, created_at);
