#!/usr/bin/env bash
# discover-warm-queue.sh - cron-safe proposal queue warmer.
#
# Defaults avoid Claude so this can run on the clawd replica without starting
# the Claude CLI. On the canonical residential worker, set
# SWELL_DISCOVERY_NO_CLAUDE=0 after re-authing Claude for that Unix user.
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
    set -a
    . ./.env
    set +a
fi

PYTHON="${PYTHON:-python3}"
LIMIT="${SWELL_DISCOVERY_LIMIT_PER_ADAPTER:-20}"
MODEL="${SWELL_DISCOVERY_MODEL:-sonnet}"
ADAPTERS="${SWELL_DISCOVERY_ADAPTERS:-general_feed,google_related}"
NO_CLAUDE="${SWELL_DISCOVERY_NO_CLAUDE:-1}"

IFS=',' read -r -a ADAPTER_LIST <<< "$ADAPTERS"

COMMON_ARGS=(--run --limit-per-adapter "$LIMIT" --model "$MODEL")
if [[ "$NO_CLAUDE" != "0" ]]; then
    COMMON_ARGS+=(--no-claude)
fi

ran=0
failed=0
for adapter in "${ADAPTER_LIST[@]}"; do
    adapter="${adapter//[[:space:]]/}"
    [[ -n "$adapter" ]] || continue
    ran=$((ran + 1))
    if ! "$PYTHON" discover.py "${COMMON_ARGS[@]}" --adapter "$adapter"; then
        failed=$((failed + 1))
    fi
done

if [[ "$ran" -gt 0 && "$failed" -ge "$ran" ]]; then
    exit 2
fi
exit 0
