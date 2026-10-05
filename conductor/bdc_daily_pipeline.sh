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

FAILED_STEPS=""   # 2026-09-23:此前任何步骤失败都打 WARN 后照常 DONE + exit 0,
                  # cron 判定词表永远看到 success。现在步骤仍不中断(数据步非致命
                  # 惯例),但失败会累积,结尾如实报 FAILED 并以非零退出。
run_step() {  # $1=label  $2..=command — non-fatal but recorded
  local label="$1"; shift
  hr; log "STEP $label"; hr
  if "$@" >>"$LOGFILE" 2>&1; then
    log "STEP $label: ok"
  else
    log "STEP $label: WARN non-zero exit (recorded, continuing)"
    FAILED_STEPS="$FAILED_STEPS[$label] "
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

# 非致命告警计数(措辞刻意避开 ERROR/failed/traceback,不误触发 cron 判败词表)。
# 2026-09-25:补"与上一日对比的新增数"——7 条设计内修正(单位归一/小计剔除/TSLX
# 申报方不一致)每天必现,只报总数会让判定日日降级 success-degraded,信号稀释;
# 判定应看 new:0=全是已知常态,new>0 才值得降级细看。归档类告警带时间戳天然算新增。
ALERT_SET=$(grep "ALERT\]" "$LOGFILE" 2>/dev/null | grep -v "alerts_in_log" | sed 's/^.*ALERT\] //' | sort -u)
ALERTS=$(printf '%s\n' "$ALERT_SET" | grep -c . || true)
PREV_LOG=$(ls -t "$LOG_DIR"/bdc_daily_*.log 2>/dev/null | grep -vF "$LOGFILE" | head -1)
if [ -n "$PREV_LOG" ]; then
  PREV_SET=$(grep "ALERT\]" "$PREV_LOG" 2>/dev/null | grep -v "alerts_in_log" | sed 's/^.*ALERT\] //' | sort -u)
  NEW_CNT=$(comm -13 <(printf '%s\n' "$PREV_SET") <(printf '%s\n' "$ALERT_SET") | grep -c . || true)
  log "alerts_in_log: ${ALERTS:-0} (new vs prev run: ${NEW_CNT:-0}; non-fatal; grep 'ALERT]' $LOGFILE 查看明细)"
else
  log "alerts_in_log: ${ALERTS:-0} (no prev log to compare; non-fatal; grep 'ALERT]' $LOGFILE 查看明细)"
fi
if [ -n "$FAILED_STEPS" ]; then
  hr; log "BDC look-through daily pipeline FAILED — step(s) ${FAILED_STEPS}exited non-zero (log: $LOGFILE)"; hr
  exit 1
fi
hr; log "BDC look-through daily pipeline DONE  (log: $LOGFILE)"; hr
