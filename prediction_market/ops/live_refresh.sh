#!/usr/bin/env bash
# live_refresh.sh — per-minute in-play refresh wrapper (launchd, every 60s).
#
# Cheap no-op outside match windows. Inside a window it pulls live fixture state
# (API-Football) and regenerates inplay_live.json + upcoming.json into both the
# output dir and the frontend's public/data dir. The local Express server serves
# public/data over the Cloudflare tunnel, so the live site refreshes every minute
# WITHOUT a Firebase redeploy (frontend polls inplay_live.json every 30s).
#
# Manual run:  bash prediction_market/ops/live_refresh.sh
set -uo pipefail

REPO="/Users/xuling/code/someopark-test"

# Minimal-PATH safe (cron/launchd); do NOT source the shell profile.
export PATH="/opt/homebrew/bin:/Users/xuling/miniforge3/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

cd "$REPO" || { echo "repo not found"; exit 1; }

# Load the API-Football key (gitignored .env files).
set -a
[ -f "$REPO/.env" ] && source "$REPO/.env"
[ -f "$REPO/prediction_market/.env" ] && source "$REPO/prediction_market/.env"
set +a

conda run -n someopark_run --no-capture-output \
  python -m prediction_market.ops.live_refresh
