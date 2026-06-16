#!/bin/bash
# =============================================================================
# bdc_daily_pipeline.sh — Private-Credit BDC look-through daily pipeline
# =============================================================================
# Daily production driver for the BDC underlying-loan look-through
# (portfolio_of_private_credit_deals). Mirrors the project's strategy-pipeline
# conventions (log/hr/PY, non-fatal data steps, heartbeat). Scheduling is arranged
# externally (target window 15:45–16:05 ET); this script is the entrypoint only.
#
# Usage (run from anywhere; paths resolve absolutely):
#     bash conductor/bdc_daily_pipeline.sh daily
#     bash conductor/bdc_daily_pipeline.sh daily --sandbox /tmp/bdc_run   # dev, zero prod impact
#
# Steps (§7.1):
#   A  SyncPrivateCreditRates   rates daily (MacroStateStore -> fred_rates.csv)
#   C  RefreshBDCHoldings        probe 5 CIKs; ingest only on a NEW 10-Q/10-K (filing-driven)
#   D  RunBDCLookThrough         daily re-valuation + holdings diff (new/changed/exited)
#   E  heartbeat + reports       written by RunBDCLookThrough
# Every step is NON-FATAL (loud-alert + continue), per the project convention. A 15-min
# wall-clock self-kill keeps the run inside its window.
#
# Environment: conda env `someopark_run` + `.env` (FRED_API_KEY). EDGAR needs no key.
# All outputs are additive (price_data/bdc_holdings/, module bdc_results/, public/data).
# =============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$REPO_ROOT/conductor/logs"
mkdir -p "$LOG_DIR"
CONDA_ENV="someopark_run"
TS="$(date '+%Y%m%d_%H%M%S')"
LOGFILE="$LOG_DIR/bdc_daily_$TS.log"

MODE="${1:-daily}"
SANDBOX=""
[ "${2:-}" = "--sandbox" ] && SANDBOX="${3:-}"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"; }
hr()  { echo "══════════════════════════════════════════════════════════════════" | tee -a "$LOGFILE"; }
PY()  { conda run -n "$CONDA_ENV" --no-capture-output python "$@"; }

# load FRED key (EDGAR is key-free)
if [ -f "$REPO_ROOT/.env" ]; then set -a; . "$REPO_ROOT/.env"; set +a; fi

# 15-minute self-kill so the run never overruns the 15:45–16:05 window
( sleep 900 && log "WATCHDOG: 15-min limit hit, killing pipeline" && kill -TERM $$ ) &
WATCHDOG=$!
trap 'kill "$WATCHDOG" 2>/dev/null' EXIT

SB_ARGS=""
[ -n "$SANDBOX" ] && SB_ARGS="--sandbox $SANDBOX"

run_step() {  # $1=label  $2..=command — non-fatal
  local label="$1"; shift
  hr; log "STEP $label"; hr
  if "$@" >>"$LOGFILE" 2>&1; then
    log "STEP $label: ok"
  else
    log "STEP $label: WARN non-zero exit (non-fatal, continuing)"
  fi
}

if [ "$MODE" != "daily" ]; then
  echo "usage: bash conductor/bdc_daily_pipeline.sh daily [--sandbox DIR]"; exit 1
fi

hr; log "BDC look-through daily pipeline  (sandbox='${SANDBOX:-none}')"; hr
cd "$REPO_ROOT"

# A) rates — MacroStateStore -> fred_rates.csv
run_step "A SyncPrivateCreditRates" PY "$REPO_ROOT/SyncPrivateCreditRates.py" $SB_ARGS

# C) holdings — probe + (filing-driven) ingest
run_step "C RefreshBDCHoldings" PY "$REPO_ROOT/RefreshBDCHoldings.py" $SB_ARGS

# D/E) re-valuation + diff + reports + heartbeat
run_step "D RunBDCLookThrough" PY "$REPO_ROOT/RunBDCLookThrough.py" $SB_ARGS

hr; log "BDC look-through daily pipeline DONE  (log: $LOGFILE)"; hr
