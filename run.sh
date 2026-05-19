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

PYTHON="${PYTHON:-python3}"

"$PYTHON" ingest.py
"$PYTHON" extract.py --limit 50
"$PYTHON" scorer.py
"$PYTHON" calibration.py --diagnose
"$PYTHON" trend_router.py --emit

if [[ "${1:-}" == "--watchlist" ]]; then
    "$PYTHON" watchlist.py --snapshot | "$PYTHON" notify.py stdin
    "$PYTHON" trend_router.py --emit --notify
fi

"$PYTHON" status.py
