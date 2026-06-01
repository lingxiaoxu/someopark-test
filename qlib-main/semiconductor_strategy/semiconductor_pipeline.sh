#!/bin/bash
# =============================================================================
# semiconductor_pipeline.sh — AISS (AI Infra & Semiconductor Strategy) pipeline
# =============================================================================
# Single production entrypoint for the AISS strategy.  Mirrors SSRS's pipeline
# but is purpose-built for AISS: individual-stock data, no EPS/value modes.
#
# Usage (run from the repo root or anywhere; paths are resolved absolutely):
#     bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh <MODE> [opts]
#
# Modes:
#     update_data   Incremental data refresh (prices + capex + dram + slow PIT checks)
#     daily         Generate today's AISS signal + update inventory  (NYSE holiday-aware)
#     weekly        Data/PIT health + weekly_review (drift/regime/multi-horizon) + dry-run
#     monthly       Refresh P0 (V1+V2 select via daily_backtest, restore V1) + force-rebalance  (holiday-aware)
#     dry-run       Daily signal WITHOUT writing inventory (safe any time)
#     backtest      Single full backtest of the active/selected param set
#     batch         Backtest all param sets -> ranked CSV/Excel
#     select        Batch + walk-forward OOS selection -> selected_param_set.json
#                   (add --signal-version v1|v2 to select per version)
#     daily_backtest V1+V2 select + validate + tearsheet suite (refreshes the
#                   per-version P0 caches smart_select uses to pick V1 vs V2)
#     walk-forward  Walk-forward IS/OOS robustness (anchored + rolling)
#     validate      Backtest + compare vs SOXX/SMH/SPY (win criterion)
#                   (add --signal-version v1|v2)
#
# Signal versions: V1 = monthly rebalance (production default, strongest);
#                  V2 = semi-monthly (1st + ~mid-month) with the same 12-1 signal.
#                  Most modes accept `--signal-version v1|v2`; daily auto-picks
#                  via smart_select unless you pass --signal-version.
#     tearsheet     Full PDF tearsheet (vs SOXX/SMH/SPY)
#     test          Run the pytest suite (offline, synthetic data)
#     status        Print current inventory + latest signal summary
#     help          This message
#
# Flags: --skip-holiday  bypass the NYSE holiday check (backfill / manual runs).
#        daily_backtest.sh also honors --skip-holiday. daily/monthly/daily_backtest
#        skip + exit 0 on weekends/holidays (normal success); weekly + dry-run always run.
#
# Environment: ALWAYS conda env `qlib_run` + `.env` (POLYGON_API_KEY, FRED_API_KEY).
# All outputs stay inside this strategy dir (and additive data under price_data/).
# =============================================================================

set -uo pipefail

# --- resolve paths (script may be invoked from anywhere) ---------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"     # .../semiconductor_strategy
SELF="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"            # absolute path to this script
QLIB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"                       # .../qlib-main
REPO_ROOT="$(cd "$QLIB_DIR/.." && pwd)"                        # .../someopark-test
PKG="semiconductor_strategy"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d_%H%M%S)"

CONDA_ENV="qlib_run"
PY() { conda run -n "$CONDA_ENV" --no-capture-output python "$@"; }

# --- load .env (only the keys we need; avoids the & job-control noise) -------
if [ -f "$REPO_ROOT/.env" ]; then
    export POLYGON_API_KEY="$(grep -E '^POLYGON_API_KEY=' "$REPO_ROOT/.env" | cut -d= -f2- | tr -d '"' )"
    export FRED_API_KEY="$(grep -E '^FRED_API_KEY=' "$REPO_ROOT/.env" | cut -d= -f2- | tr -d '"' )"
fi

# ── Mode parsing (first positional arg, default: help) ───────────────────────
MODE="${1:-help}"
[[ $# -gt 0 ]] && shift

# ── Option defaults ───────────────────────────────────────────────────────────
SKIP_HOLIDAY=0
PASS_ARGS=()           # everything except --skip-holiday is forwarded to python

# ── Option parsing (mirrors SSRS while/case style) ────────────────────────────
# AISS handles --skip-holiday in the shell (the NYSE check is a shell concern)
# and forwards every other flag verbatim to the python entrypoints, which self-
# parse their own flags (--signal-version / --param-set / --date /
# --force-rebalance / --dry-run / etc.).  Hence the catch-all collects rather
# than errors on `-*`, the one deliberate deviation from SSRS's parser.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-holiday)   SKIP_HOLIDAY=1;       shift ;;
        help|--help|-h)   MODE="help";          shift ;;
        *)                PASS_ARGS+=("$1");     shift ;;
    esac
done
set -- ${PASS_ARGS[@]+"${PASS_ARGS[@]}"}

cd "$QLIB_DIR"   # so `python -m semiconductor_strategy.*` resolves

# Clear stale __pycache__ (prevents bytecode bugs after code edits)
find "$SCRIPT_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

log() { echo "[$(date '+%H:%M:%S')] $*"; }
hr()  { echo "══════════════════════════════════════════════════════════════════"; }

# ── NYSE holiday check (mirrors SSRS) ─────────────────────────────────────────
# Skips work + exits 0 on weekends/holidays (normal success, not failure).
# Uses pandas_market_calendars in qlib_run; falls back to a weekday-only check.
check_nyse_open() {
    if [ "$SKIP_HOLIDAY" -eq 1 ]; then log "Holiday check skipped (--skip-holiday)"; return 0; fi
    local NYSE_STATUS
    NYSE_STATUS=$(PY -c "
import sys
from datetime import datetime
try:
    import pytz
    import pandas_market_calendars as mcal
    nyc_date = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d')
    sched = mcal.get_calendar('NYSE').schedule(start_date=nyc_date, end_date=nyc_date)
    print('OPEN' if not sched.empty else 'CLOSED:' + nyc_date); sys.exit(0)
except ImportError:
    pass
except Exception as e:
    print('WARN:' + str(e)[:60], file=sys.stderr)
from datetime import date
t = date.today()
print('CLOSED:' + str(t) + '-weekend' if t.weekday() >= 5 else 'OPEN-WEEKDAY')
" 2>/dev/null) || NYSE_STATUS="OPEN-FALLBACK"
    if [[ "$NYSE_STATUS" == CLOSED* ]]; then
        hr; log "NYSE 休市 (${NYSE_STATUS#CLOSED:}) — pipeline skip, exit 0"; hr
        exit 0
    fi
    log "NYSE status: $NYSE_STATUS — proceeding"
}

run_update_data() {
    hr; log "AISS update_data — incremental refresh"; hr
    log "1/5 prices (Polygon incremental)…"
    PY -m $PKG.data.aiss_fetch_prices --update            "$@"
    log "2/5 CapEx pulse (yfinance)…"
    PY -m $PKG.data.company_signals  --update-capex        "$@"
    log "3/5 MU DIO (SEC XBRL)…"
    PY -m $PKG.data.company_signals  --check-mu-dio        "$@"
    log "4/5 TSMC / ASML (TWSE + SEC, PIT)…"
    PY -m $PKG.data.industry_signals --check-tsmc --check-asml "$@"
    log "5/5 DRAM proxy (computed)…"
    PY -m $PKG.data.industry_signals --update-dram         "$@"
    log "update_data complete."
}

case "$MODE" in
    update_data|update-data)
        run_update_data "$@" 2>&1 | tee "$LOG_DIR/aiss_update_data_$TS.log"
        ;;

    daily)
        check_nyse_open
        hr; log "AISS daily signal"; hr
        PY -m $PKG.AISSdailySignal "$@" 2>&1 | tee "$LOG_DIR/aiss_daily_$TS.log"
        ;;

    dry-run|dryrun)
        PY -m $PKG.AISSdailySignal --dry-run "$@" 2>&1 | tee "$LOG_DIR/aiss_dryrun_$TS.log"
        ;;

    backtest)
        hr; log "AISS single backtest"; hr
        PY -m $PKG.backtest.engine "$@" 2>&1 | tee "$LOG_DIR/aiss_backtest_$TS.log"
        ;;

    batch)
        hr; log "AISS batch (all param sets)"; hr
        PY -m $PKG.AISSBatchRun "$@" 2>&1 | tee "$LOG_DIR/aiss_batch_$TS.log"
        ;;

    select)
        hr; log "AISS production param selection (batch + WF OOS + MCPS)"; hr
        PY -m $PKG.AISSBatchRun --select --save-equity "$@" 2>&1 | tee "$LOG_DIR/aiss_select_$TS.log"
        ;;

    walk-forward|wf)
        hr; log "AISS walk-forward IS/OOS"; hr
        PY -m $PKG.walk_forward "$@" 2>&1 | tee "$LOG_DIR/aiss_wf_$TS.log"
        ;;

    daily_backtest|daily-backtest)
        hr; log "AISS V1+V2 backtest/selection suite"; hr
        bash "$SCRIPT_DIR/daily_backtest.sh" "$@"
        ;;

    validate)
        hr; log "AISS validation vs SOXX / SMH / SPY (win criterion)"; hr
        PY -m $PKG.validate "$@" 2>&1 | tee "$LOG_DIR/aiss_validate_$TS.log"
        ;;

    tearsheet)
        hr; log "AISS tearsheet (PDF, vs SOXX/SMH/SPY)"; hr
        PY -m $PKG.report.tearsheet "$@" 2>&1 | tee "$LOG_DIR/aiss_tearsheet_$TS.log"
        ;;

    test)
        hr; log "AISS test suite"; hr
        PY -m pytest "$SCRIPT_DIR/tests/" -v --tb=short "$@"
        ;;

    status)
        PY -m $PKG.AISSdailySignal --dry-run --status-only 2>/dev/null || \
          PY -c "import json,glob,os; \
d=json.load(open('$SCRIPT_DIR/inventory_aiss.json')) if os.path.exists('$SCRIPT_DIR/inventory_aiss.json') else {}; \
print('inventory as_of:', d.get('as_of')); print('holdings:', d.get('holdings'))"
        ;;

    weekly)
        # Mirrors SSRS 'weekly': data/PIT health + weekly_review + dry-run validation.
        # (AISS has no EPS step; SSRS's EPS refresh is replaced by data/PIT health checks.)
        WK_LOG="$LOG_DIR/aiss_weekly_$TS.log"
        hr; log "AISS WEEKLY maintenance (data/PIT health + weekly review + dry-run)"; hr
        log "1/3 data + PIT health checks (prices / company / industry coverage)…"
        PY -m $PKG.data.aiss_fetch_prices --verify 2>&1 | tee -a "$WK_LOG" || true
        PY -m $PKG.data.company_signals  --verify 2>&1 | tee -a "$WK_LOG" || true
        PY -m $PKG.data.industry_signals --verify 2>&1 | tee -a "$WK_LOG" || true
        log "2/3 weekly review (multi-horizon + param drift + regime trend + P0-cache health)…"
        PY -m $PKG.weekly_review "$@" 2>&1 | tee -a "$WK_LOG" \
          || log "  WARN: weekly_review failed — continuing with dry-run"
        log "3/3 dry-run validation (full pipeline healthy, no inventory write)…"
        PY -m $PKG.AISSdailySignal --dry-run 2>&1 | tee -a "$WK_LOG"
        log "WEEKLY MAINTENANCE COMPLETE  (log: $WK_LOG)"
        ;;

    monthly)
        # Mirrors SSRS 'monthly': refresh all P0 selection data + force-rebalance.
        # AISS refreshes P0 via daily_backtest (V1+V2 select → restore V1), which
        # encompasses the per-version 'select'; then a force-rebalance daily signal.
        check_nyse_open
        MO_LOG="$LOG_DIR/aiss_monthly_$TS.log"
        hr; log "AISS MONTHLY (V1+V2 select suite + restore V1 + force-rebalance)"; hr
        log "1/2 daily_backtest: V1+V2 batch + WF-OOS select → refresh P0 caches → restore V1"
        bash "$SCRIPT_DIR/daily_backtest.sh" 2>&1 | tee -a "$MO_LOG"
        log "2/2 force-rebalance daily signal (smart_select with fresh P0 caches)…"
        PY -m $PKG.AISSdailySignal --force-rebalance "$@" 2>&1 | tee -a "$MO_LOG"
        log "MONTHLY REBALANCE COMPLETE  (log: $MO_LOG)"
        ;;

    help|--help|-h|"")
        sed -n '2,46p' "$SELF" | sed 's/^# \{0,1\}//'
        ;;

    *)
        echo "Unknown mode: $MODE"; echo "Run: bash semiconductor_pipeline.sh help"; exit 2 ;;
esac
