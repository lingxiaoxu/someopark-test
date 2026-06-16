#!/usr/bin/env bash
# refresh_and_deploy.sh — unattended daily pipeline for the World Cup predictions.
#
#   1. pull new results + missing odds (+ recent form on Sundays),
#   2. regenerate every prediction export on the CURRENT (growing) OOS sample,
#   3. sync the JSON into the frontend, build, and deploy to Firebase Hosting.
#
# Designed to run from cron / launchd with no human in the loop. Firebase deploy
# unattended needs a CI token: run `firebase login:ci` once and export the token
# as FIREBASE_TOKEN (e.g. in this script's env or the launchd plist). If the token
# is absent it falls back to the interactive login (works when you run it by hand).
#
# Manual run:  bash prediction_market/ops/refresh_and_deploy.sh
set -uo pipefail

REPO="/Users/xuling/code/someopark-test"
FRONTEND="$REPO/someo-park-investment-management"
LOGDIR="$REPO/prediction_market/data/logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/refresh_deploy_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

echo "=== refresh_and_deploy @ $(date) ==="

# Make conda / node / npm / firebase reachable under cron's minimal PATH.
# (Do NOT source the user's shell profile — a non-interactive bash aborts on it.)
export PATH="/opt/homebrew/bin:/Users/xuling/miniforge3/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

cd "$REPO" || { echo "repo not found"; exit 1; }

# Load secrets (POLYGON/API keys live in the gitignored .env files).
set -a
[ -f "$REPO/.env" ] && source "$REPO/.env"
[ -f "$REPO/prediction_market/.env" ] && source "$REPO/prediction_market/.env"
set +a

# --trigger: event-driven gate. Run every ~15 min; only proceed to the full
# pipeline when a NEW match result has just landed (else exit cheaply).
if [ "${1:-}" = "--trigger" ]; then
  TOUT="$(conda run -n someopark_run --no-capture-output python -m prediction_market.ops.match_trigger 2>&1)"
  echo "trigger: $TOUT"
  echo "$TOUT" | grep -q "^RUN" || { echo "trigger: nothing to do — exit"; exit 0; }
  echo "trigger: new result → running full pipeline"
fi

# Weekly (Sunday) also re-pull recent national-team form; daily skips it (cheaper).
FORM_FLAG=""
[ "$(date +%u)" = "7" ] && FORM_FLAG="--with-form"

echo "--- 1) refresh exports on current OOS sample ---"
conda run -n someopark_run --no-capture-output \
  python -m prediction_market.ops.refresh_all --ingest $FORM_FLAG || { echo "refresh failed"; exit 1; }

echo "--- 2) sync + build frontend ---"
cd "$FRONTEND" || { echo "frontend not found"; exit 1; }
npm run sync:wc || { echo "sync failed"; exit 1; }
npm run build  || { echo "build failed"; exit 1; }

echo "--- 3) deploy to firebase hosting ---"
if [ -n "${FIREBASE_TOKEN:-}" ]; then
  firebase deploy --only hosting --project someopark --token "$FIREBASE_TOKEN" || { echo "deploy failed"; exit 1; }
else
  firebase deploy --only hosting --project someopark || { echo "deploy failed (no FIREBASE_TOKEN; needs interactive auth)"; exit 1; }
fi

echo "=== done @ $(date) — https://someopark.web.app ==="
