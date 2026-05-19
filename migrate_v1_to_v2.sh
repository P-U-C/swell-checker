#!/usr/bin/env bash
# migrate_v1_to_v2.sh - upgrade an existing swell-checker deployment in-place.
#
# Run as 'swell' user from inside /home/swell/swell-checker/
# AFTER copying the new files into place.
#
# What this does:
#   1. Verify the new files are present
#   2. Delete old per-candidate news sources from the DB
#      (they're replaced by global general_feeds)
#   3. Delete unprocessed news fetches (they have no source left)
#   4. Reset Reddit fetch errors so we retry with new UA
#   5. Trigger fresh ingest via normal flow
#
# Safe to re-run: idempotent where possible.

set -euo pipefail

cd "$(dirname "$0")"

required=(ingest.py extract.py sources.yaml prompts/extract_general.md)
for f in "${required[@]}"; do
    [[ -f "$f" ]] || { echo "MISSING: $f"; exit 1; }
done
echo "ok  new files in place"

# Backup db before mutation
cp -n db.sqlite db.sqlite.pre-v2-backup 2>/dev/null || echo "(backup already exists, keeping it)"
echo "ok  db backed up to db.sqlite.pre-v2-backup"

# Count before
n_sources_before=$(sqlite3 db.sqlite "SELECT COUNT(*) FROM sources;")
n_events_before=$(sqlite3 db.sqlite "SELECT COUNT(*) FROM events;")
echo "before: $n_sources_before sources, $n_events_before events"

# Delete per-candidate news sources (they don't appear in new sources.yaml)
deleted_news=$(sqlite3 db.sqlite "
DELETE FROM fetches WHERE source_id IN (SELECT id FROM sources WHERE source_type='news');
SELECT changes();")
echo "cleared $deleted_news news-source fetches"

sqlite3 db.sqlite "DELETE FROM sources WHERE source_type='news';"
echo "cleared per-candidate news source rows"

# Reset reddit errors so we retry with new UA
sqlite3 db.sqlite "UPDATE sources SET last_error=NULL, last_fetched_at=NULL WHERE source_type='reddit';"
echo "ok  reset reddit source errors"

# Count after
n_sources_after=$(sqlite3 db.sqlite "SELECT COUNT(*) FROM sources;")
n_events_after=$(sqlite3 db.sqlite "SELECT COUNT(*) FROM events;")
echo "after:  $n_sources_after sources, $n_events_after events (existing events kept)"

echo ""
echo "migration complete. next:"
echo "  python3 ingest.py       # will add new general_feeds + refetch reddit with new UA"
echo "  python3 extract.py      # processes fetches via new routing"
echo "  python3 scorer.py       # see new calibration"
