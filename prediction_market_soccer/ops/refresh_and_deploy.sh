#!/usr/bin/env bash
# refresh_and_deploy.sh — Soccer-only scheduled data refresh (legacy filename).
# refresh_all validates and promotes Soccer JSON/PDF to output and the existing
# public/data/soccer directory. Express exposes that data through the current
# tunnel; this job does not rebuild or publish the shared frontend/Hosting site.
# Result acknowledgement happens only after the complete data refresh succeeds.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Hold the existing lock through Soccer refresh and result acknowledgement.
export PATH="/opt/homebrew/bin:/Users/xuling/miniforge3/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
cd "$REPO" || exit 1
if [ "${1:-}" != "--locked" ]; then
  # Gate before recording a publish attempt, so quiet 15-minute ticks remain idle.
  if [ "${1:-}" = "--trigger" ]; then
    set -a
    [ -f "$REPO/.env" ] && source "$REPO/.env"
    [ -f "$REPO/prediction_market_soccer/.env" ] && source "$REPO/prediction_market_soccer/.env"
    set +a
    TOUT="$(conda run -n someopark_run --no-capture-output python -m prediction_market_soccer.ops.match_trigger 2>&1)" || { echo "$TOUT"; exit 1; }
    echo "$TOUT"
    # Pre-match sentry: 15-min guard for calendar/PRE-staging/PIT-cache/disk/poly-global
    # reference prices. Fail-open — the sentry must never block the trigger path.
    conda run -n someopark_run --no-capture-output python -m prediction_market_soccer.ops.pre_match_sentry 2>&1 | tail -3 || true
    echo "$TOUT" | grep -q "^RUN" || exit 0
  fi
  exec conda run -n someopark_run --no-capture-output python -m prediction_market_soccer.ops.proc_lock \
    --run refresh_deploy -- bash "$0" --locked "$@"
fi
shift
LOGDIR="$REPO/prediction_market_soccer/data/logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/refresh_deploy_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

echo "=== refresh_and_deploy @ $(date) ==="

# Make conda reachable under cron's minimal PATH.
# (Do NOT source the user's shell profile — a non-interactive bash aborts on it.)
export PATH="/opt/homebrew/bin:/Users/xuling/miniforge3/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

cd "$REPO" || { echo "repo not found"; exit 1; }

# Load secrets (POLYGON/API keys live in the gitignored .env files).
set -a
[ -f "$REPO/.env" ] && source "$REPO/.env"
[ -f "$REPO/prediction_market_soccer/.env" ] && source "$REPO/prediction_market_soccer/.env"
set +a

# Weekly (Sunday) also re-pull recent national-team form; daily skips it (cheaper).
FORM_FLAG=""
[ "$(date +%u)" = "7" ] && FORM_FLAG="--with-form"

echo "--- 1) refresh exports on current OOS sample ---"
# refresh_all signals "another instance holds the lock" with exit 75 (ops/refresh_all.py
# acquire_or_exit busy_exit=75) — benign on matchdays, when a long live cycle or
# settle_reports holds the shared flock. Treating it as failure wrote tracebacks and a
# launchctl exit 1 every time (2026-09-12 17:16), noise that masks real trigger failures.
conda run -n someopark_run --no-capture-output \
  python -m prediction_market_soccer.ops.refresh_all --ingest $FORM_FLAG
RC=$?
if [ "$RC" = "75" ]; then
  echo "refresh skipped: another instance holds the lock (benign; next tick retries)"
  exit 0
elif [ "$RC" != "0" ]; then
  echo "refresh failed (exit $RC)"; exit 1
fi

# Consume only results included in the successfully promoted Soccer data batch.
cd "$REPO" || exit 1
conda run -n someopark_run --no-capture-output python -m prediction_market_soccer.ops.match_trigger \
  --acknowledge-refresh || { echo "result acknowledgement failed"; exit 1; }

# C-33 health surface: ops/health_export is the single writer of health.json and was in
# no pipeline at all, so the output ledger ("a live output that stops appearing raises an
# alert") could never fire. Runs after a successful refresh, reads only local artifacts
# (no --scan-venues here: that hits venue listings and belongs on a slower cadence).
conda run -n someopark_run --no-capture-output \
  python -m prediction_market_soccer.ops.health_export >/dev/null \
  && echo "health.json: written" || echo "health export: skipped (non-fatal)"

# Optional legacy research output, still opt-in and separate from shared Hosting.
# Wait so it does not overlap the successful data refresh above.
if [ "${1:-}" != "--trigger" ]; then
  # Param sweep is DISABLED for the first ~6 weeks (plan §2.2: tiny early-season
  # samples over-fit; the WC ran it daily over 104 settled). Enable by touching
  # prediction_market_soccer/data/output/.enable_sweep once per-league samples mature.
  if [ -f "$REPO/prediction_market_soccer/data/output/.enable_sweep" ]; then
    echo "--- 4) daily param sweep (time-isolated; sleeping 60s first) ---"
    sleep 60
    cd "$REPO" || exit 0
    conda run -n someopark_run --no-capture-output \
      python -m prediction_market_soccer.ops.param_sweep && echo "param sweep: done" || echo "param sweep: skipped (non-fatal)"
  else
    echo "--- 4) param sweep disabled (cold-start; touch data/output/.enable_sweep to enable) ---"
  fi
fi

echo "=== Soccer data refresh complete @ $(date) — existing Express data service ==="
