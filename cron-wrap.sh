#!/usr/bin/env bash
# cron-wrap.sh - wrap a pipeline step, alert Telegram on failure.
#
# Usage:
#   cron-wrap.sh ingest ./ingest.py
#   cron-wrap.sh extract ./extract.py --limit 30
#   cron-wrap.sh health ./health.py
#   cron-wrap.sh weekly ./run.sh --watchlist
set -uo pipefail

cd "$(dirname "$0")"

# Load .env if present
if [[ -f .env ]]; then
    set -a
    . ./.env
    set +a
fi

LABEL="$1"
shift

TMP_ERR=$(mktemp)
"$@" 2> >(tee "$TMP_ERR" >&2)
EXIT=$?

if [[ $EXIT -ne 0 ]]; then
  ERR_TAIL=$(tail -c 500 "$TMP_ERR" 2>/dev/null || echo "(no stderr captured)")
  python3 notify.py alert "${LABEL} exit=${EXIT}"$'\n'"---stderr tail---"$'\n'"${ERR_TAIL}"
fi

rm -f "$TMP_ERR"
exit $EXIT
