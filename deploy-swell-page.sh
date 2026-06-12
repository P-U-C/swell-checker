#!/usr/bin/env bash
# deploy-swell-page.sh
#
# Regenerate the public swell dashboard JSON from the local db mirror and
# publish it (GitHub Pages served from the pft-validator repo, same pattern
# as the scanner's deploy-scanner.sh). Idempotent: no data change, no push.
#
# Runs on clawd after the 13:40 pull-swell-from-peptides.sh mirror refresh.
# Auth: pft-validator pushes via the git credential store (repo-local
# store-only helper override -- see git-credential-setup memory).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PFT="${PFT_VALIDATOR_DIR:-$HOME/pft-validator}"

python3 "$HERE/export-dashboard.py" || { echo "deploy-swell: export failed" >&2; exit 1; }

cd "$PFT" || { echo "deploy-swell: $PFT missing" >&2; exit 1; }

git add swell/
if git diff --cached --quiet -- swell/; then
  echo "deploy-swell: no change; nothing to deploy"
  exit 0
fi

git -c user.email=zeroexzoz@gmail.com -c user.name="swell-deploy" \
  commit -q -m "swell: dashboard refresh $(date -u +%Y-%m-%dT%H:%MZ)"
git -c rebase.autoStash=true pull --rebase -q origin main || true
git push origin HEAD:main >/dev/null 2>&1 \
  && echo "deploy-swell: deployed $(git rev-parse --short HEAD)" \
  || { echo "deploy-swell: push failed" >&2; exit 1; }
