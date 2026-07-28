#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# daily_backtest.sh — AISS V1 + V2 backtest / selection suite
# ═══════════════════════════════════════════════════════════════════════════
# Mirrors the SSRS daily_backtest flow, purpose-built for AISS.  Runs after the
# daily signal to (a) refresh per-version production parameters + the P0 caches
# that smart_select's V1↔V2 version_selector consumes, and (b) produce a
# validation + PDF tearsheet for each version.  Production stays on V1.
#
#   Step 1  V1 select   → selected_param_set.json (V1) + *_v1 P0 caches
#   Step 2  V2 select   → *_v2 P0 caches            (then restore V1 as production)
#   Step 3  V1 validate + tearsheet
#   Step 4  V2 validate + tearsheet
#
# Safe: backs up / restores selected_param_set.json so production ends on V1.
# Run from repo root:  bash qlib-main/semiconductor_strategy/daily_backtest.sh
# ═══════════════════════════════════════════════════════════════════════════

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QLIB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$QLIB_DIR/.." && pwd)"
PKG="semiconductor_strategy"
SEL="$SCRIPT_DIR/selected_param_set.json"
LOG_DIR="$SCRIPT_DIR/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/aiss_daily_backtest_$(date +%Y%m%d_%H%M%S).log"
BK_V1="$(mktemp -t aiss_sel_v1)"; BK_V2="$(mktemp -t aiss_sel_v2)"

if [ -f "$REPO_ROOT/.env" ]; then
    export POLYGON_API_KEY="$(grep -E '^POLYGON_API_KEY=' "$REPO_ROOT/.env" | cut -d= -f2- | tr -d '"')"
    export FRED_API_KEY="$(grep -E '^FRED_API_KEY=' "$REPO_ROOT/.env" | cut -d= -f2- | tr -d '"')"
fi
cd "$QLIB_DIR"
PY() { conda run -n qlib_run --no-capture-output python "$@"; }
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
getp() { PY -c "import json;print(json.load(open('$SEL')).get('param_set','?'),json.load(open('$SEL')).get('signal_version','?'))" 2>/dev/null; }

# NYSE holiday check (mirrors SSRS): skip + exit 0 on weekends/holidays.
# Pass --skip-holiday to bypass (backfill / manual runs).
# ── Option parsing (mirrors SSRS while/case style) ────────────────────────────
SKIP_HOLIDAY=0
FORCE_RERUN=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-holiday)   SKIP_HOLIDAY=1;   shift ;;
        --force)          FORCE_RERUN=1;    shift ;;
        *)                shift ;;
    esac
done

# ── Idempotency gate (2026-07-22, mirrors SSRS) ──────────────────────────────
# 场景: cron 外层 agent 汇报失败被判 error → 调度器自动重试已成功的重型管道
# (2026-07-21 事故: 产物翻倍)。当天已有完成标记则只汇报不重跑;手动重跑加 --force。
# 注意: 只写 stdout(进 cron 汇总日志),不新建带日期日志——否则 agent tail 最新
# 日期日志会看到只有 skip 行的文件而误判失败。
if [ "$FORCE_RERUN" -eq 0 ]; then
    _done_log=$(grep -l "AISS DAILY BACKTEST COMPLETE" \
        "$LOG_DIR"/aiss_daily_backtest_$(date +%Y%m%d)_*.log 2>/dev/null | head -1)
    if [ -n "$_done_log" ]; then
        echo "[$(date +%H:%M:%S)] AISS daily_backtest already completed today" \
             "($(basename "$_done_log")) — idempotent skip, exit 0. Re-run with --force."
        exit 0
    fi
fi

if [ "$SKIP_HOLIDAY" -eq 0 ]; then
    NYSE_STATUS=$(PY -c "
import sys
from datetime import datetime
try:
    import pytz, pandas_market_calendars as mcal
    nyc_date = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d')
    sched = mcal.get_calendar('NYSE').schedule(start_date=nyc_date, end_date=nyc_date)
    print('OPEN' if not sched.empty else 'CLOSED:' + nyc_date); sys.exit(0)
except ImportError: pass
except Exception as e: print('WARN:' + str(e)[:60], file=sys.stderr)
from datetime import date
t = date.today()
print('CLOSED:' + str(t) + '-weekend' if t.weekday() >= 5 else 'OPEN-WEEKDAY')
" 2>/dev/null) || NYSE_STATUS="OPEN-FALLBACK"
    if [[ "$NYSE_STATUS" == CLOSED* ]]; then
        log "NYSE 休市 (${NYSE_STATUS#CLOSED:}) — skip daily_backtest, exit 0"
        exit 0
    fi
    log "NYSE status: $NYSE_STATUS — proceeding"
fi

log "══ AISS DAILY BACKTEST — V1 + V2 full suite ══"

# Each --select --save-equity --tearsheet run produces, for that version:
#   • per-set IS portfolio Excel        (one per param set)
#   • per-set IS-OOS portfolio Excel     (one per param set)
#   • WF diagnostic Excel                (1)
#   • PDF tearsheet                      (1)
#   • selected_param_set.json + _v{1,2} P0 caches (for smart_select)

log "Step 1/3: V1 full suite (batch IS + WF IS-OOS + diagnostic + PDF + select)"
if PY -m $PKG.AISSBatchRun --select --save-equity --tearsheet --signal-version v1 >> "$LOG" 2>&1; then
    log "  ✅ V1 done — selected: $(getp)"; [ -f "$SEL" ] && cp "$SEL" "$BK_V1"
else
    log "  ⚠️  V1 suite failed (see $LOG)"
fi

log "Step 2/3: V2 full suite (then restore V1 to production)"
if PY -m $PKG.AISSBatchRun --select --save-equity --tearsheet --signal-version v2 >> "$LOG" 2>&1; then
    log "  ✅ V2 done — selected: $(getp)"; [ -f "$SEL" ] && cp "$SEL" "$BK_V2"
else
    log "  ⚠️  V2 suite failed (see $LOG)"
fi
[ -f "$BK_V1" ] && cp "$BK_V1" "$SEL" && log "  Restored production V1: $(getp)"

log "Step 3/3: win-criterion validation (both versions)"
PY -m $PKG.validate --signal-version v1 2>&1 | grep -iE 'WIN CRITERION' | sed 's/^/  V1 /' | tee -a "$LOG" || true
PY -m $PKG.validate --signal-version v2 2>&1 | grep -iE 'WIN CRITERION' | sed 's/^/  V2 /' | tee -a "$LOG" || true

# Artifact tally for this run
_OUT="$REPO_ROOT/historical_runs/semiconductor_strategy"; _PDF="$SCRIPT_DIR/report/output"
log "── Artifacts (this run, $(date +%Y%m%d)) ──"
log "  V1 batch IS Excel    : $(ls "$_OUT"/aiss_portfolio_*_v1_IS_batch_$(date +%Y%m%d)_*.xlsx 2>/dev/null | wc -l | tr -d ' ')"
log "  V1 IS-OOS Excel       : $(ls "$_OUT"/aiss_portfolio_*_v1_IS-OOS_tearsheet_$(date +%Y%m%d)_*.xlsx 2>/dev/null | wc -l | tr -d ' ')"
log "  V2 batch IS Excel    : $(ls "$_OUT"/aiss_portfolio_*_v2_IS_batch_$(date +%Y%m%d)_*.xlsx 2>/dev/null | wc -l | tr -d ' ')"
log "  V2 IS-OOS Excel       : $(ls "$_OUT"/aiss_portfolio_*_v2_IS-OOS_tearsheet_$(date +%Y%m%d)_*.xlsx 2>/dev/null | wc -l | tr -d ' ')"
log "  WF diagnostic Excel   : $(ls "$_OUT"/wf_diagnostic_aiss_*_$(date +%Y%m%d)_*.xlsx 2>/dev/null | wc -l | tr -d ' ')"
log "  PDF tearsheets        : $(ls "$_PDF"/*_$(date +%Y%m%d)_*.pdf 2>/dev/null | wc -l | tr -d ' ')"

rm -f "$BK_V1" "$BK_V2"
log "══ AISS DAILY BACKTEST COMPLETE ══  (log: $LOG)"
