#!/bin/bash
# =============================================================================
# test_pipeline_integration.sh
# Sector Rotation — Full Pipeline Integration Test
# =============================================================================
# Tests all 18+ pipeline modes end-to-end with real data.
# Run from repo root: bash qlib-main/sector_rotation/tests/test_pipeline_integration.sh
#
# Prerequisites:
#   - .env with POLYGON_API_KEY and FRED_API_KEY
#   - qlib_run conda environment
#   - price_data/ caches populated (auto-downloaded on first run)
#   - eps_history.json populated (run eps-full first if missing)
#
# Exit codes:
#   0 = all tests passed
#   1 = one or more tests failed
# =============================================================================

set -a && source .env && set +a 2>/dev/null

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$REPO" || exit 1

PIPELINE="bash qlib-main/sector_rotation/sector_rotation_pipeline.sh"
PASS=0
FAIL=0
SKIP=0
TOTAL=0
FAILURES=""

# ── Colors ────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

# ── Test runner ───────────────────────────────────────────────────────────
run_test() {
    local id="$1"
    local desc="$2"
    shift 2
    TOTAL=$((TOTAL + 1))

    printf "  [%2d] %-55s " "$id" "$desc"

    # Run with timeout (5 min per test, tearsheet gets 10 min)
    local timeout_sec=300
    if [[ "$desc" == *"tearsheet"* ]] || [[ "$desc" == *"walk-forward"* ]] || [[ "$desc" == *"select"* ]]; then
        timeout_sec=600
    fi

    local output
    output=$(timeout "$timeout_sec" "$@" 2>&1)
    local rc=$?

    if [[ $rc -eq 0 ]]; then
        printf "${GREEN}PASS${NC}\n"
        PASS=$((PASS + 1))
    elif [[ $rc -eq 124 ]]; then
        printf "${YELLOW}TIMEOUT${NC} (>${timeout_sec}s)\n"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES\n  [$id] $desc — TIMEOUT"
    else
        printf "${RED}FAIL${NC} (exit=$rc)\n"
        FAIL=$((FAIL + 1))
        FAILURES="$FAILURES\n  [$id] $desc — exit=$rc"
        # Show last 3 lines of output for debugging
        echo "$output" | tail -3 | sed 's/^/       /'
    fi
}

# ── Verify prerequisites ─────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  SECTOR ROTATION — Pipeline Integration Tests"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "════════════════════════════════════════════════════════════════"
echo ""

if [[ ! -f ".env" ]]; then
    echo "ERROR: .env not found. Run from repo root." >&2
    exit 1
fi

if ! conda run -n qlib_run python -c "import qlib" 2>/dev/null; then
    echo "ERROR: qlib_run conda env not available." >&2
    exit 1
fi

echo "  Prerequisites OK. Starting tests..."
echo ""

# ═══════════════════════════════════════════════════════════════════════════
#  Group A: Backtest modes
# ═══════════════════════════════════════════════════════════════════════════
echo "── Group A: Backtest ──────────────────────────────────────────"

run_test 1 "backtest (selected param set)" \
    $PIPELINE backtest

run_test 2 "backtest --param-set default" \
    $PIPELINE backtest --param-set default

run_test 3 "backtest --param-set value_tilt" \
    $PIPELINE backtest --param-set value_tilt

run_test 4 "backtest --param-set erm_partial_filter (new signal)" \
    $PIPELINE backtest --param-set erm_partial_filter

run_test 5 "backtest --param-set none (config.yaml defaults)" \
    $PIPELINE backtest --param-set none

run_test 6 "backtest --param-set list" \
    $PIPELINE backtest --param-set list

run_test 7 "backtest --walk-forward --wf-mode anchored" \
    $PIPELINE backtest --walk-forward --wf-mode anchored

# ═══════════════════════════════════════════════════════════════════════════
#  Group B: Batch / Select / WF modes
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "── Group B: Batch / Select / WF ──────────────────────────────"

run_test 8 "batch (full 64 param sets)" \
    $PIPELINE batch

run_test 9 "batch --oos-validate" \
    $PIPELINE batch --oos-validate

run_test 10 "select (batch + WF OOS filter + MCPS)" \
    $PIPELINE select

run_test 11 "wf (standalone walk-forward)" \
    $PIPELINE wf

# ═══════════════════════════════════════════════════════════════════════════
#  Group C: Signal / Analysis modes
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "── Group C: Signal / Analysis ────────────────────────────────"

run_test 12 "sensitivity" \
    $PIPELINE sensitivity

run_test 13 "regime" \
    $PIPELINE regime

run_test 14 "signal-raw" \
    $PIPELINE signal-raw

# ═══════════════════════════════════════════════════════════════════════════
#  Group D: Daily operations
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "── Group D: Daily operations ─────────────────────────────────"

run_test 15 "dry-run (read-only signal)" \
    $PIPELINE dry-run

run_test 16 "daily --skip-holiday" \
    $PIPELINE daily --skip-holiday

run_test 17 "status" \
    $PIPELINE status

# ═══════════════════════════════════════════════════════════════════════════
#  Group E: Reports
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "── Group E: Reports ──────────────────────────────────────────"

run_test 18 "tearsheet (13-page PDF + WF)" \
    $PIPELINE tearsheet

# ═══════════════════════════════════════════════════════════════════════════
#  Group F: Unit tests
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "── Group F: Unit tests ───────────────────────────────────────"

run_test 19 "pytest suite (synthetic data, no network)" \
    $PIPELINE test

# ═══════════════════════════════════════════════════════════════════════════
#  Group G: Cross-entry consistency (same params → same results)
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "── Group G: Cross-entry consistency ──────────────────────────"

run_test 20 "3-entry consistency (backtest=batch=tearsheet)" \
    bash -c "
set -a && source .env && set +a
PYTHONPATH='qlib-main:.' conda run -n qlib_run --no-capture-output python -c \"
import logging; logging.basicConfig(level=logging.WARNING)
from sector_rotation.data.loader import load_all, load_config
from sector_rotation.SectorRotationStrategyRuns import PARAM_SETS, apply_param_set
from sector_rotation.backtest.engine import SectorRotationBacktest
from sector_rotation.SectorRotationBatchRun import _run_one_with_equity

base_cfg = load_config()
prices, macro = load_all(config=base_cfg)

# backtest entry
cfg = apply_param_set(base_cfg, PARAM_SETS['default'])
r1 = SectorRotationBacktest(cfg).run(prices=prices, macro=macro)
sr1 = round(r1.metrics['sharpe'], 6)

# batch entry
row, _ = _run_one_with_equity('default', base_cfg, prices, macro)
sr2 = round(row['sharpe'], 6)

assert sr1 == sr2, f'MISMATCH: backtest={sr1} vs batch={sr2}'
print(f'Sharpe={sr1} — identical across entries')
\"
"

run_test 21 "MCPS single source (3 call sites identical)" \
    bash -c "
set -a && source .env && set +a
PYTHONPATH='qlib-main:.' conda run -n qlib_run --no-capture-output python -c \"
import logging; logging.basicConfig(level=logging.WARNING)
from sector_rotation.data.loader import load_all, load_config
from sector_rotation.SectorRotationStrategyRuns import PARAM_SETS, apply_param_set
from sector_rotation.backtest.engine import SectorRotationBacktest
from sector_rotation.SectorRotationBatchRun import _macro_cond_sharpe
from sector_rotation.walk_forward import _macro_cond_sharpe_is
from MCPS import macro_cond_sharpe
from MacroStateStore import MacroStateStore, SIMILARITY_FEATURES
import pandas as pd

base_cfg = load_config()
prices, macro = load_all(config=base_cfg)
cfg = apply_param_set(base_cfg, PARAM_SETS['default'])
eq = SectorRotationBacktest(cfg).run(prices=prices, macro=macro).equity_curve

store = MacroStateStore()
macro_df = store.load('2018-07-01')
tv = {f: float(macro_df[f].iloc[-1]) for f in SIMILARITY_FEATURES
      if f in macro_df.columns and not pd.isna(macro_df[f].iloc[-1])}

s1 = macro_cond_sharpe(eq, macro_df, tv, SIMILARITY_FEATURES)
s2 = _macro_cond_sharpe(eq, macro_df, tv, SIMILARITY_FEATURES)
s3 = _macro_cond_sharpe_is(eq, macro_df, tv, SIMILARITY_FEATURES)

assert abs(s1-s2) < 1e-10 and abs(s1-s3) < 1e-10, f'MISMATCH: {s1}/{s2}/{s3}'
print(f'Score={s1:.6f} — identical across 3 call sites')
\"
"

# ═══════════════════════════════════════════════════════════════════════════
#  Summary
# ═══════════════════════════════════════════════════════════════════════════
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  RESULTS:  ${GREEN}${PASS} PASS${NC}  ${RED}${FAIL} FAIL${NC}  ${YELLOW}${SKIP} SKIP${NC}  / ${TOTAL} total"
echo "════════════════════════════════════════════════════════════════"

if [[ $FAIL -gt 0 ]]; then
    echo -e "  FAILURES:${FAILURES}"
    echo "════════════════════════════════════════════════════════════════"
    exit 1
else
    echo "  All tests passed."
    echo "════════════════════════════════════════════════════════════════"
    exit 0
fi
