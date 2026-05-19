#!/usr/bin/env bash
# run.sh - single entrypoint for swell-checker.
# Usage:
#   ./run.sh              # ingest + extract + score
#   ./run.sh --watchlist  # also push watchlist to Telegram
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f .env ]]; then
    set -a && . ./.env && set +a
fi

python3 ingest.py
python3 extract.py --limit 50
python3 scorer.py --snapshot

if [[ "${1:-}" == "--watchlist" ]]; then
    python3 watchlist.py --snapshot | python3 notify.py stdin
fi

python3 status.py
