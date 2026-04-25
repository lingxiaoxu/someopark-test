#!/bin/bash
# =============================================================================
# sector_rotation_pipeline.sh
# Sector Rotation Strategy — Master Pipeline Controller
# =============================================================================
# LOCATION : qlib-main/sector_rotation/sector_rotation_pipeline.sh
# RUN FROM : repo root (someopark-test/)
#
# ─── 隔离原则 ─────────────────────────────────────────────────────────────────
#   本脚本只使用 qlib_run conda 环境，绝不调用 someopark_run 环境。
#   不修改、不读取、不干预 someopark 主程序的任何状态文件。
#   MacroStateStore 数据（price_data/macro/）由 someopark 主 pipeline 维护；
#   本脚本只读取（read-only），不写入、不更新。
#
# ─── MODES ──────────────────────────────────────────────────────────────────
#   daily         Standard daily run (default): holiday check → EPS auto-refresh → signal
#   weekly        Weekly maintenance: EPS incremental update + dry-run validation
#   monthly       Month-start force-rebalance (holiday-aware, EPS pre-refresh)
#   eps-update    Incremental EPS history update (skips symbols fresh ≤7 days)
#   eps-full      Force full EPS re-fetch all 55 symbols (~5 min, first-run setup)
#   eps-symbols   Targeted EPS update for specific tickers (pass after mode name)
#                 e.g.: bash ... eps-symbols XOM CVX AAPL NVDA
#   backtest      Full IS/OOS historical backtest (2018-07-01 → today)
#   sensitivity   Parameter sensitivity sweep (top_n_sectors etc., via sensitivity.py)
#   regime        Regime analysis report — 4-state labels + summary (regime.py)
#   tearsheet     Backtest + generate multi-page PDF performance tearsheet
#   test          Run pytest suite (95 tests, fully network-free synthetic data)
#   dry-run       Read-only daily signal — no inventory write, safe to run anytime
#   status        Print current portfolio state + latest signal file summary
#   signal-raw    Print raw composite z-scores via get_current_signals() API
#   help          Show this usage message
#
# ─── OPTIONS ────────────────────────────────────────────────────────────────
#   --value-source proxy|polygon|constituents  P/E value source (default: polygon)
#   --capital N           Portfolio capital USD (default: read from inventory)
#   --date YYYY-MM-DD     Override signal date (default: latest weekday)
#   --force-rebalance     Force rebalance regardless of monthly schedule
#   --skip-holiday        Bypass NYSE holiday check (use for backfill / manual runs)
#   --no-eps-check        Skip auto EPS freshness check in daily mode
#   --force               (with eps-update) Force full re-fetch for all symbols
#   --config PATH         Path to config.yaml (default: sector_rotation/config.yaml)
#
# ─── EXAMPLES ───────────────────────────────────────────────────────────────
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily --value-source polygon
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh weekly
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh monthly --capital 2000000
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-full
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-update
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-symbols XOM CVX AAPL MSFT
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh backtest
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh sensitivity
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh regime
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh tearsheet
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh test
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh status
#   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh signal-raw
#
# ─── CRON SETUP (sample — all times UTC) ─────────────────────────────────────
#   # Daily signal: Mon-Fri 17:15 NY time = 21:15 UTC (winter) / 21:15 UTC (summer)
#   15 21 * * 1-5   cd /Users/xuling/code/someopark-test && \
#                   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily \
#                   >> qlib-main/sector_rotation/logs/cron_daily.log 2>&1
#
#   # Weekly EPS maintenance: Sunday 06:00 UTC (01:00 ET)
#   0 6 * * 0       cd /Users/xuling/code/someopark-test && \
#                   bash qlib-main/sector_rotation/sector_rotation_pipeline.sh weekly \
#                   >> qlib-main/sector_rotation/logs/cron_weekly.log 2>&1
#
# ─── NOTES ──────────────────────────────────────────────────────────────────
#   • Python env   : qlib_run ONLY — never someopark_run
#   • EPS store    : price_data/sector_etfs/eps_history.json (update_eps_history.py)
#   • Macro data   : price_data/macro/ parquets (READ-ONLY, written by someopark pipeline)
#   • Inventory    : qlib-main/sector_rotation/inventory_sector_rotation.json
#   • Signals out  : qlib-main/sector_rotation/trading_signals/
#   • Logs         : qlib-main/sector_rotation/logs/
#   • State        : qlib-main/sector_rotation/pipeline_state/
# =============================================================================

# ── Resolve paths (works regardless of CWD at call time) ─────────────────────
SR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # qlib-main/sector_rotation/
REPO="$(cd "$SR_DIR/../.." && pwd)"                       # someopark-test/
LOG_DIR="$SR_DIR/logs"
STATE_DIR="$SR_DIR/pipeline_state"

mkdir -p "$LOG_DIR" "$STATE_DIR"

# ── Mode parsing (first positional arg, default: daily) ──────────────────────
MODE="${1:-daily}"
[[ $# -gt 0 ]] && shift

# ── Option defaults ───────────────────────────────────────────────────────────
VALUE_SOURCE="polygon"
CAPITAL=""
SIGNAL_DATE=""
FORCE_REBALANCE=""
SKIP_HOLIDAY=0
NO_EPS_CHECK=0
EXTRA_FORCE=""
CONFIG_OVERRIDE=""
EXTRA_SYMBOLS=()

# ── Option parsing ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --value-source)     VALUE_SOURCE="$2";      shift 2 ;;
        --capital)          CAPITAL="$2";           shift 2 ;;
        --date)             SIGNAL_DATE="$2";       shift 2 ;;
        --config)           CONFIG_OVERRIDE="$2";   shift 2 ;;
        --force-rebalance)  FORCE_REBALANCE="--force-rebalance"; shift ;;
        --skip-holiday)     SKIP_HOLIDAY=1;         shift ;;
        --no-eps-check)     NO_EPS_CHECK=1;         shift ;;
        --force)            EXTRA_FORCE="--force";  shift ;;
        help|--help|-h)     MODE="help";            shift ;;
        -*)
            echo "ERROR: Unknown option: $1" >&2
            echo "Run: bash $0 help" >&2
            exit 1
            ;;
        *)
            EXTRA_SYMBOLS+=("$1"); shift ;;
    esac
done

# ── Logging setup ─────────────────────────────────────────────────────────────
TODAY=$(date +%Y%m%d)
LOGFILE="$LOG_DIR/sr_${MODE}_${TODAY}.log"
CURRENT_LINK="$LOG_DIR/sr_${MODE}_current.log"
cp /dev/null "$LOGFILE"
ln -sf "$LOGFILE" "$CURRENT_LINK" 2>/dev/null || true

log() {
    echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"
}
log_section() {
    log "══════════════════════════════════════════════════════"
    log "  $*"
    log "══════════════════════════════════════════════════════"
}
log_warn() { log "WARNING: $*"; }
log_fail() {
    log "FAILED: $*"
    echo "FAIL:$*" >> "$STATE_DIR/sr_status_${MODE}"
}

# ── Environment setup (qlib_run ONLY — never someopark_run) ───────────────────
source /Users/xuling/miniforge3/etc/profile.d/conda.sh 2>/dev/null || {
    log "ERROR: conda not found at /Users/xuling/miniforge3/etc/profile.d/conda.sh"
    exit 1
}

cd "$REPO" || { log "ERROR: Cannot cd to $REPO"; exit 1; }

if [[ ! -f "$REPO/.env" ]]; then
    log "ERROR: .env not found at $REPO/.env"
    exit 1
fi
set -a && source "$REPO/.env" && set +a

# Single conda env — qlib_run only
CONDA_QLIB="conda run -n qlib_run --no-capture-output"

# ── NYSE Holiday Check ────────────────────────────────────────────────────────
# Uses pandas_market_calendars within qlib_run.
# Falls back to weekday-only check if the library is unavailable.
# NEVER calls someopark_run.
check_nyse_open() {
    if [[ $SKIP_HOLIDAY -eq 1 ]]; then
        log "Holiday check skipped (--skip-holiday)"
        return 0
    fi

    local NYSE_STATUS
    NYSE_STATUS=$($CONDA_QLIB python3 -c "
import sys
from datetime import datetime
try:
    import pytz
    import pandas_market_calendars as mcal
    nyc_tz = pytz.timezone('America/New_York')
    nyc_date = datetime.now(nyc_tz).strftime('%Y-%m-%d')
    nyse = mcal.get_calendar('NYSE')
    schedule = nyse.schedule(start_date=nyc_date, end_date=nyc_date)
    print('OPEN' if not schedule.empty else 'CLOSED:' + nyc_date)
    sys.exit(0)
except ImportError:
    pass  # fall through to weekday check
except Exception as e:
    print('WARN:' + str(e)[:60], file=sys.stderr)
    pass

# Fallback: weekday check only (cannot catch NYSE-specific holidays)
from datetime import date
today = date.today()
if today.weekday() >= 5:  # 5=Saturday, 6=Sunday
    print('CLOSED:' + str(today) + '-weekend')
else:
    print('OPEN-WEEKDAY')
" 2>/dev/null) || NYSE_STATUS="OPEN-FALLBACK"

    if [[ "$NYSE_STATUS" == CLOSED* ]]; then
        local NYSE_DATE="${NYSE_STATUS#CLOSED:}"
        log_section "NYSE 休市 ($NYSE_DATE) — pipeline skip, exit 0"
        echo "HOLIDAY:$NYSE_DATE" >> "$STATE_DIR/sr_status_${MODE}"
        exit 0
    fi

    if [[ "$NYSE_STATUS" == OPEN-WEEKDAY ]]; then
        log_warn "pandas_market_calendars not in qlib_run — weekday check only (install: conda run -n qlib_run pip install pandas-market-calendars)"
    fi
    log "NYSE status: $NYSE_STATUS — proceeding"
}

# ── EPS auto-freshness check ──────────────────────────────────────────────────
# If eps_history.json is stale (>7 days), run incremental update automatically.
run_eps_auto_refresh() {
    if [[ $NO_EPS_CHECK -eq 1 ]]; then
        log "EPS auto-refresh skipped (--no-eps-check)"
        return 0
    fi

    local EPS_STORE="$REPO/price_data/sector_etfs/eps_history.json"

    if [[ ! -f "$EPS_STORE" ]]; then
        log_warn "EPS store not found: $EPS_STORE"
        log_warn "Value signal will fall back to 'proxy' mode automatically."
        log_warn "First-time setup: run  bash $0 eps-full"
        return 0
    fi

    local EPS_STALENESS
    EPS_STALENESS=$($CONDA_QLIB python -c "
import json, datetime, pathlib
p = pathlib.Path('price_data/sector_etfs/eps_history.json')
with open(p) as f:
    d = json.load(f)
fetched = d.get('fetched_at', '')
if not fetched:
    print(99)
else:
    days = (datetime.date.today() - datetime.date.fromisoformat(fetched)).days
    print(days)
" 2>/dev/null) || EPS_STALENESS="99"

    if [[ "$EPS_STALENESS" -gt 7 ]]; then
        log "EPS store stale (${EPS_STALENESS} days) — running incremental update..."
        set -a && source "$REPO/.env" && set +a
        $CONDA_QLIB python qlib-main/sector_rotation/update_eps_history.py >> "$LOGFILE" 2>&1
        local RC=$?
        if [[ $RC -ne 0 ]]; then
            log_warn "EPS incremental update failed (exit=$RC). Continuing with stale data."
        else
            log "EPS incremental update complete."
        fi
    else
        log "EPS store is fresh (${EPS_STALENESS} days old, threshold=7) — skip update."
    fi
}

# ── Python step runner ────────────────────────────────────────────────────────
run_python() {
    local STEP_NUM="$1"
    local STEP_DESC="$2"
    shift 2

    log "→ STEP $STEP_NUM START: $STEP_DESC"
    set -a && source "$REPO/.env" && set +a
    $CONDA_QLIB python "$@" >> "$LOGFILE" 2>&1
    local RC=$?

    if [[ $RC -ne 0 ]]; then
        log_fail "STEP $STEP_NUM ($STEP_DESC) exit=$RC"
        log "See log: $LOGFILE"
        exit $RC
    fi

    log "→ STEP $STEP_NUM OK: $STEP_DESC"
    echo "DONE:$STEP_NUM:$STEP_DESC" >> "$STATE_DIR/sr_status_${MODE}"
}

# ── Build DailySignal CLI arguments ──────────────────────────────────────────
build_signal_args() {
    local ARGS="--value-source $VALUE_SOURCE"
    [[ -n "$CAPITAL" ]]         && ARGS="$ARGS --capital $CAPITAL"
    [[ -n "$SIGNAL_DATE" ]]     && ARGS="$ARGS --date $SIGNAL_DATE"
    [[ -n "$FORCE_REBALANCE" ]] && ARGS="$ARGS $FORCE_REBALANCE"
    [[ -n "$CONFIG_OVERRIDE" ]] && ARGS="$ARGS --config $CONFIG_OVERRIDE"
    echo "$ARGS"
}

# =============================================================================
# MAIN DISPATCH
# =============================================================================
log_section "SR PIPELINE  mode=$MODE  $(date '+%Y-%m-%d %H:%M:%S')"
echo "RUNNING:$MODE:$(date '+%Y-%m-%d %H:%M:%S')" > "$STATE_DIR/sr_status_${MODE}"

case "$MODE" in

# ─────────────────────────────────────────────────────────────────────────────
# DAILY — Standard market-day signal run
# Schedule: Mon-Fri, after NYSE close (17:15 ET recommended)
# Steps: NYSE holiday check → EPS auto-refresh → DailySignal
# ─────────────────────────────────────────────────────────────────────────────
daily)
    log "Mode: DAILY  value-source=$VALUE_SOURCE"
    check_nyse_open
    run_eps_auto_refresh

    SIGNAL_ARGS=$(build_signal_args)
    run_python 1 "DailySignal ($SIGNAL_ARGS)" \
        qlib-main/sector_rotation/SectorRotationDailySignal.py $SIGNAL_ARGS

    log_section "DAILY COMPLETE — see trading_signals/ for output"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# WEEKLY — Sunday maintenance
# Schedule: Sunday pre-market (06:00 UTC / 01:00 ET)
# Steps: EPS incremental update (all stale symbols) → dry-run validation
# ─────────────────────────────────────────────────────────────────────────────
weekly)
    log "Mode: WEEKLY  value-source=$VALUE_SOURCE"

    # Step 1: Incremental EPS update (REFRESH_DAYS=7, skips symbols updated this week)
    run_python 1 "EPS incremental update (55 symbols, skips fresh)" \
        qlib-main/sector_rotation/update_eps_history.py

    # Step 2: Dry-run to confirm full pipeline is healthy (no inventory write)
    SIGNAL_ARGS="$(build_signal_args) --dry-run"
    run_python 2 "DailySignal dry-run validation ($SIGNAL_ARGS)" \
        qlib-main/sector_rotation/SectorRotationDailySignal.py $SIGNAL_ARGS

    log_section "WEEKLY MAINTENANCE COMPLETE"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# MONTHLY — First trading day force-rebalance
# DailySignal normally detects the first trading day automatically.
# Run this mode manually if daily missed the rebalance date.
# ─────────────────────────────────────────────────────────────────────────────
monthly)
    log "Mode: MONTHLY  force-rebalance  value-source=$VALUE_SOURCE"
    check_nyse_open

    # Step 1: EPS refresh before important rebalance
    run_python 1 "EPS incremental update (pre-rebalance)" \
        qlib-main/sector_rotation/update_eps_history.py

    # Step 2: Force-rebalance daily signal
    SIGNAL_ARGS="$(build_signal_args) --force-rebalance"
    run_python 2 "DailySignal force-rebalance ($SIGNAL_ARGS)" \
        qlib-main/sector_rotation/SectorRotationDailySignal.py $SIGNAL_ARGS

    log_section "MONTHLY REBALANCE COMPLETE"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# EPS-UPDATE — Incremental EPS maintenance
# Only re-fetches symbols where last_fetched > REFRESH_DAYS (7) ago.
# Add --force to trigger full re-fetch regardless of last_fetched.
# ─────────────────────────────────────────────────────────────────────────────
eps-update)
    log "Mode: EPS-UPDATE  (incremental${EXTRA_FORCE:+, force-flag set})"
    run_python 1 "update_eps_history${EXTRA_FORCE:+ $EXTRA_FORCE}" \
        qlib-main/sector_rotation/update_eps_history.py $EXTRA_FORCE
    log_section "EPS UPDATE COMPLETE"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# EPS-FULL — Force full re-fetch all 55 symbols
# Use for: first-time setup, data corruption repair, gaps > 30 days.
# Takes ~5 min. Requires POLYGON_API_KEY in .env
# ─────────────────────────────────────────────────────────────────────────────
eps-full)
    log "Mode: EPS-FULL  (~5 min, 55 symbols, POLYGON_API_KEY required)"
    if [[ -z "${POLYGON_API_KEY:-}" ]]; then
        log "ERROR: POLYGON_API_KEY not set. Run: set -a && source .env && set +a"
        exit 1
    fi
    run_python 1 "update_eps_history --force (full re-fetch)" \
        qlib-main/sector_rotation/update_eps_history.py --force
    log_section "EPS FULL FETCH COMPLETE"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# EPS-SYMBOLS — Targeted EPS update for specific tickers
# Usage: bash sector_rotation_pipeline.sh eps-symbols XOM CVX AAPL MSFT
# Add --force to force full re-fetch for those symbols.
# ─────────────────────────────────────────────────────────────────────────────
eps-symbols)
    if [[ ${#EXTRA_SYMBOLS[@]} -eq 0 ]]; then
        log "ERROR: eps-symbols requires ticker list."
        log "  Example: bash $0 eps-symbols XOM CVX AAPL MSFT"
        exit 1
    fi
    log "Mode: EPS-SYMBOLS  symbols=${EXTRA_SYMBOLS[*]}${EXTRA_FORCE:+  --force}"
    if [[ -n "$EXTRA_FORCE" ]]; then
        run_python 1 "update_eps_history --force ${EXTRA_SYMBOLS[*]}" \
            qlib-main/sector_rotation/update_eps_history.py "${EXTRA_SYMBOLS[@]}" --force
    else
        run_python 1 "update_eps_history ${EXTRA_SYMBOLS[*]}" \
            qlib-main/sector_rotation/update_eps_history.py "${EXTRA_SYMBOLS[@]}"
    fi
    log_section "EPS SYMBOLS UPDATE COMPLETE"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# BACKTEST — Full IS/OOS historical backtest (2018-07-01 → today)
# Config from config.yaml backtest section.
# ─────────────────────────────────────────────────────────────────────────────
backtest)
    log "Mode: BACKTEST  (2018-07-01 → today)"
    set -a && source "$REPO/.env" && set +a
    PYTHONPATH="$REPO/qlib-main" $CONDA_QLIB python -m sector_rotation.backtest.engine \
        >> "$LOGFILE" 2>&1
    RC=$?
    if [[ $RC -ne 0 ]]; then log_fail "STEP 1 (SectorRotationBacktest engine) exit=$RC"; exit $RC; fi
    log "→ STEP 1 OK: SectorRotationBacktest engine"
    log_section "BACKTEST COMPLETE"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# SENSITIVITY — Parameter sensitivity sweep (top_n_sectors and other params)
# Sweeps key config parameters and prints Sharpe/MaxDD table.
# Useful after major market changes or regime shifts.
# ─────────────────────────────────────────────────────────────────────────────
sensitivity)
    log "Mode: SENSITIVITY  (parameter sweep via sensitivity.py)"
    run_python 1 "sensitivity parameter sweep" \
        qlib-main/sector_rotation/backtest/sensitivity.py
    log_section "SENSITIVITY SWEEP COMPLETE"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# REGIME — Print current and historical regime analysis
# Runs regime.py standalone: computes 4-state regime labels, prints summary.
# Useful for debugging regime detection or reviewing regime history.
# ─────────────────────────────────────────────────────────────────────────────
regime)
    log "Mode: REGIME  (regime analysis via regime.py)"
    run_python 1 "regime analysis" \
        qlib-main/sector_rotation/signals/regime.py
    log_section "REGIME ANALYSIS COMPLETE"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# TEARSHEET — Backtest + PDF tearsheet generation
# Output: report/output/sector_rotation_tearsheet.pdf
# ─────────────────────────────────────────────────────────────────────────────
tearsheet)
    log "Mode: TEARSHEET  (backtest + PDF)"
    set -a && source "$REPO/.env" && set +a
    $CONDA_QLIB python - >> "$LOGFILE" 2>&1 <<'PYEOF'
import sys, pathlib, logging
sys.path.insert(0, str(pathlib.Path('qlib-main').resolve()))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

from sector_rotation.data.loader import load_all, load_config
from sector_rotation.backtest.engine import SectorRotationBacktest
from sector_rotation.report.tearsheet import generate_tearsheet

cfg = load_config()
prices, macro = load_all(config=cfg)
bt = SectorRotationBacktest(cfg)
result = bt.run(prices, macro)
print(result.summary())
generate_tearsheet(result, prices=prices)
print("Tearsheet saved to report/output/sector_rotation_tearsheet.pdf")
PYEOF
    RC=$?
    if [[ $RC -ne 0 ]]; then
        log_fail "tearsheet (exit=$RC)"
        exit $RC
    fi
    log_section "TEARSHEET COMPLETE"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# TEST — Full pytest suite (95 tests, synthetic data, no network)
# ─────────────────────────────────────────────────────────────────────────────
test)
    log "Mode: TEST  (pytest 95 tests, no network)"
    set -a && source "$REPO/.env" && set +a
    $CONDA_QLIB python -m pytest qlib-main/sector_rotation/tests/ -v --tb=short \
        2>&1 | tee -a "$LOGFILE"
    RC=${PIPESTATUS[0]}
    if [[ $RC -ne 0 ]]; then
        log_fail "pytest returned exit=$RC"
        exit $RC
    fi
    log "All tests passed."
    log_section "TEST RUN COMPLETE"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# DRY-RUN — Read-only signal run (no inventory write)
# Safe to run at any time. Useful for: config validation, signal inspection,
# testing --value-source changes, previewing what a real run would do.
# ─────────────────────────────────────────────────────────────────────────────
dry-run)
    log "Mode: DRY-RUN  value-source=$VALUE_SOURCE  (inventory unchanged)"
    SIGNAL_ARGS="$(build_signal_args) --dry-run"
    run_python 1 "DailySignal dry-run ($SIGNAL_ARGS)" \
        qlib-main/sector_rotation/SectorRotationDailySignal.py $SIGNAL_ARGS
    log_section "DRY-RUN COMPLETE  (inventory NOT modified)"
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# STATUS — Print current portfolio state and latest signal summary
# Read-only. Uses qlib_run python (json/glob/pathlib — no extra packages needed).
# ─────────────────────────────────────────────────────────────────────────────
status)
    _SR="$SR_DIR"
    _REPO="$REPO"
    _SCRIPT="$0"
    set -a && source "$REPO/.env" && set +a
    $CONDA_QLIB python - <<PYEOF
import json, os, glob, pathlib, datetime

sr_dir   = pathlib.Path("$_SR")
repo     = pathlib.Path("$_REPO")
script   = "$_SCRIPT"
inv_path = sr_dir / "inventory_sector_rotation.json"
sig_dir  = sr_dir / "trading_signals"
eps_path = repo / "price_data" / "sector_etfs" / "eps_history.json"
macro_p  = repo / "price_data" / "macro"

print()
print("=" * 64)
print("  SECTOR ROTATION — STATUS  " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
print("=" * 64)

# ── Inventory ─────────────────────────────────────────────────────────────────
if inv_path.exists():
    with open(inv_path) as f:
        inv = json.load(f)
    print(f"\n  As of       : {inv.get('as_of', 'never run')}")
    print(f"  Capital     : \${inv.get('capital', 0):>12,.0f}")
    holdings = inv.get('holdings', {})
    if holdings:
        print(f"\n  HOLDINGS ({len(holdings)} positions):")
        print(f"  {'ETF':<6} {'Weight':>8} {'Shares':>8} {'Cost':>8} {'Entry':>12} {'Days':>5}")
        print("  " + "─" * 56)
        for tkr, h in sorted(holdings.items(), key=lambda x: -x[1].get('weight', 0)):
            print(f"  {tkr:<6} {h.get('weight',0)*100:>7.1f}% {h.get('shares',0):>8,} "
                  f"\${h.get('cost_basis',0):>7.2f} {h.get('entry_date','n/a'):>12} "
                  f"{h.get('days_held',0):>5}d")
    else:
        print("\n  No open positions.")
    rb = inv.get('rebalance_history', [])
    if rb:
        lr = rb[-1]
        print(f"\n  Last rebalance : {lr.get('date','n/a')} "
              f"({lr.get('reason','')} | regime={lr.get('regime','')})")
else:
    print("\n  No inventory found — run 'daily' or 'dry-run' first.")

# ── Latest signal report ──────────────────────────────────────────────────────
reports = sorted(glob.glob(str(sig_dir / "sr_daily_report_*.json")))
if reports:
    with open(reports[-1]) as f:
        rpt = json.load(f)
    print(f"\n  Latest report : {pathlib.Path(reports[-1]).name}")
    regime = rpt.get('regime', {})
    print(f"  Regime        : {str(regime.get('label','n/a')).upper()}"
          f"  (score={regime.get('score','n/a')},"
          f" vix={regime.get('vix','n/a')})")
    rb_dec = rpt.get('rebalance_decision', {})
    print(f"  Rebalanced    : {rb_dec.get('rebalance','n/a')} — {rb_dec.get('reason','')}")
    sigs = rpt.get('signals', [])
    active = [s for s in sigs if s.get('action') not in ('FLAT', 'HOLD', None)]
    if active:
        print(f"\n  ACTIONS  ({len(active)} today):")
        for s in sorted(active, key=lambda x: -abs(x.get('target_weight', 0))):
            print(f"    {s.get('ticker','?'):<5} {s.get('action','?'):<10} "
                  f"target={s.get('target_weight',0)*100:.1f}%  "
                  f"delta={s.get('weight_delta',0)*100:+.1f}%")
else:
    print("\n  No signal reports found.")

# ── EPS store ──────────────────────────────────────────────────────────────────
if eps_path.exists():
    with open(eps_path) as f:
        eps = json.load(f)
    n_syms = len(eps.get('symbols', {}))
    n_qtrs = sum(len(v) for v in eps.get('symbols', {}).values())
    fetched = eps.get('fetched_at', 'unknown')
    meta = eps.get('symbol_meta', {})
    today = datetime.date.today()
    def days_old(sym):
        lf = meta.get(sym, {}).get('last_fetched', '')
        return (today - datetime.date.fromisoformat(lf)).days if lf else 999
    stale = [s for s in meta if days_old(s) > 7]
    print(f"\n  EPS store     : {n_syms} symbols | {n_qtrs} quarters | fetched_at={fetched}")
    if stale:
        print(f"  EPS stale     : {len(stale)} symbols >7 days old (run: bash {script} eps-update)")
else:
    print(f"\n  EPS store     : NOT FOUND — run: bash {script} eps-full")

# ── Macro data freshness (read-only check) ────────────────────────────────────
if macro_p.exists():
    parquets = list(macro_p.rglob("*.parquet")) + list(macro_p.rglob("*.pkl"))
    if parquets:
        newest = max(parquets, key=lambda p: p.stat().st_mtime)
        age = (datetime.datetime.now() - datetime.datetime.fromtimestamp(newest.stat().st_mtime))
        print(f"\n  Macro data    : last updated {int(age.total_seconds()/3600)}h ago "
              f"({newest.parent.name}/{newest.name})")
        if age.total_seconds() > 86400 * 2:
            print("                  Stale >2 days — someopark pre_pipeline.sh should update MacroStateStore")
    else:
        print("\n  Macro data    : parquets not found in price_data/macro/")
else:
    print("\n  Macro data    : price_data/macro/ not found — regime signal will use yfinance fallback")

print()
PYEOF
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL-RAW — Raw composite z-scores via Python API
# Computes live composite signals without the full daily pipeline overhead.
# Useful for research, signal verification, and regime debugging.
# ─────────────────────────────────────────────────────────────────────────────
signal-raw)
    log "Mode: SIGNAL-RAW  (get_current_signals() API)"
    set -a && source "$REPO/.env" && set +a
    $CONDA_QLIB python - <<'PYEOF' 2>&1 | tee -a "$LOGFILE"
import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path('qlib-main').resolve()))

from sector_rotation.signals.composite import get_current_signals
result = get_current_signals()

print("\n═══ SECTOR ROTATION — RAW COMPOSITE Z-SCORES ═══")
print(f"  Date   : {result.get('date', 'n/a')}")
print(f"  Regime : {str(result.get('regime', 'n/a')).upper()}")
print()
composite = result.get('composite', {})
ranked = sorted(composite.items(), key=lambda x: -x[1])
print(f"  {'ETF':<6}  {'Z-Score':>8}   RANK  BAR")
print("  " + "─" * 40)
for rank, (tkr, z) in enumerate(ranked, 1):
    bar = "█" * max(0, int((z + 2) * 3))
    print(f"  {tkr:<6}  {z:>8.3f}   #{rank:<2}  {bar}")
print()
components = result.get('components', {})
if components:
    print("  COMPONENT SIGNALS (latest month-end):")
    for comp_name, comp_vals in components.items():
        if isinstance(comp_vals, dict):
            print(f"\n  [{comp_name}]")
            for tkr, v in sorted(comp_vals.items(), key=lambda x: -x[1]):
                print(f"    {tkr:<6}  {v:>8.4f}")
PYEOF
    ;;

# ─────────────────────────────────────────────────────────────────────────────
# HELP
# ─────────────────────────────────────────────────────────────────────────────
help)
    sed -n '2,80p' "${BASH_SOURCE[0]}"
    ;;

*)
    echo "Unknown mode: $MODE"
    echo ""
    echo "Available: daily | weekly | monthly | eps-update | eps-full | eps-symbols |"
    echo "           backtest | sensitivity | regime | tearsheet | test |"
    echo "           dry-run | status | signal-raw | help"
    exit 1
    ;;
esac

# ── Final status record ───────────────────────────────────────────────────────
echo "ALL_DONE:$MODE:$(date '+%Y-%m-%d %H:%M:%S')" >> "$STATE_DIR/sr_status_${MODE}"
log "Pipeline complete — mode=$MODE  log=$LOGFILE"
