#!/usr/bin/env bash
# Re-render the Burry tracker from the live signal log and redeploy it to Vercel.
#
# The deployed dashboard is a static snapshot: its data is baked into index.html
# at render time. Run this whenever you want the live (phone-accessible) site to
# reflect the current ~/.substack-trader/signal_log.db.
#
# Usage:
#   scripts/redeploy_burry_tracker.sh
set -euo pipefail

# Resolve repo root regardless of the caller's working directory.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Under launchd the PATH is minimal (/usr/bin:/bin:/usr/sbin:/sbin). Prepend Homebrew's
# bin so node/npm/npx (and vercel) resolve when this runs as a scheduled job.
export PATH="/opt/homebrew/bin:$PATH"

if [ ! -x ".venv/bin/python" ]; then
  echo "ERROR: .venv/bin/python not found. Create the venv first (see README)." >&2
  exit 1
fi

echo "Re-rendering dashboard from ~/.substack-trader/signal_log.db ..."
.venv/bin/python -m substack_trader.render_dashboard

echo "Deploying to Vercel (production) ..."
# npx --yes pulls the current Vercel CLI, so this keeps working even if the global
# install drifts below Vercel's required API version.
npx --yes vercel@latest deploy --prod --yes --cwd dashboard

echo "Done."
