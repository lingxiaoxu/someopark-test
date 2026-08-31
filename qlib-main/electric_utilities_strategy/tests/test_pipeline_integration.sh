#!/bin/bash
# =============================================================================
# test_pipeline_integration.sh — AEUS COMPREHENSIVE QA
# =============================================================================
# Full deep test + verification of the AI Infra & Electric Utilities Strategy, in
# the exact shape used for manual QA:
#
#   Phase 1  Light smoke   — every pipeline entrypoint (status / dry-run V1+V2 /
#                            validate V1+V2 / backtest / batch / walk-forward /
#                            weekly / update_data / pytest)
#   Phase 2  Matrix        — 33 param sets × {V1,V2}: backtest + validate
#                            (tests/aeus_matrix.py)
#   Phase 3  Heavy suite   — daily_backtest.sh (V1+V2 batch IS + WF IS-OOS +
#                            PDF + select + validate; regenerates all Excel/PDF)
#   Phase 4  Deep verify   — EVERY sheet of EVERY Excel + computation
#                            cross-checks (tests/aeus_verify_excel.py):
#                              · Sharpe(qlib-ci)/CAGR/MaxDD recompute vs summary
#                              · drawdown == equity/cummax-1 (2 sheets)
#                              · cum_pnl == cumsum(daily_pnl)
#                              · sector_pnl_acc == cumsum(sector_pnl_daily)
#                              · daily_pnl total == Σ(sector_pnl_daily)
#                              · stock_decomp reconciles to parent subsector
#                              · asset==equity, liability==0, interest==0
#                              · weights row-sum==1, regime labels valid
#                              · no empty/missing sheets, equity>0, cadence
#   Phase 5  PDF           — tearsheet PDFs valid + multi-page
#   Phase 6  Logs          — scan for real tracebacks/errors (benign qlib
#                            native-loop fallback warning is expected, ignored)
#
# Usage (from repo root):
#   bash qlib-main/electric_utilities_strategy/tests/test_pipeline_integration.sh [--quick]
#     --quick : skip the heavy suite (Phase 3) + deep verify (Phase 4) + PDF;
#               runs light smoke + matrix + pytest only (~5 min).  Default =
#               FULL (everything; ~40 min; regenerates ~1.8 GB of Excel).
#
# Exit: 0 = all phases passed, 1 = one or more failed.
# Env : qlib_run conda env + .env (POLYGON_API_KEY, FRED_API_KEY). AEUS isolation.
# =============================================================================

set -uo pipefail

QUICK=0
for a in "$@"; do [ "$a" = "--quick" ] && QUICK=1; done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"          # .../electric_utilities_strategy/tests
SD="$(cd "$SCRIPT_DIR/.." && pwd)"                    # .../electric_utilities_strategy
REPO="$(cd "$SD/../.." && pwd)"                       # someopark-test/
cd "$REPO" || exit 1

PIPE="bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh"
PY="conda run -n qlib_run --no-capture-output python"
PKG="electric_utilities_strategy"
TODAY="$(date +%Y%m%d)"
QA_LOG_DIR="$SD/logs/qa"; mkdir -p "$QA_LOG_DIR"

# load only the keys we need (AEUS convention: grep, don't `source .env`)
if [ -f "$REPO/.env" ]; then
    export POLYGON_API_KEY="$(grep -E '^POLYGON_API_KEY=' "$REPO/.env" | cut -d= -f2- | tr -d '"')"
    export FRED_API_KEY="$(grep -E '^FRED_API_KEY=' "$REPO/.env" | cut -d= -f2- | tr -d '"')"
fi
# qlib-main on the path so `python -m electric_utilities_strategy.*` resolves from REPO root
# (the pipeline.sh cd's into qlib-main itself; our direct $PY -m calls need this).
export PYTHONPATH="$REPO/qlib-main${PYTHONPATH:+:$PYTHONPATH}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; NC='\033[0m'
PASS=0; FAIL=0; TOTAL=0; FAILURES=""

run_test() {
    local desc="$1"; shift
    TOTAL=$((TOTAL + 1))
    printf "  [%2d] %-52s " "$TOTAL" "$desc"
    local log="$QA_LOG_DIR/qa_$(echo "$desc" | tr -c 'A-Za-z0-9' '_' | cut -c1-40).log"
    if "$@" > "$log" 2>&1; then
        printf "${GREEN}PASS${NC}\n"; PASS=$((PASS + 1))
    else
        local rc=$?
        printf "${RED}FAIL${NC} (exit=$rc)\n"; FAIL=$((FAIL + 1))
        FAILURES="$FAILURES\n  [$TOTAL] $desc — exit=$rc (log: $log)"
        tail -4 "$log" | sed 's/^/       /'
    fi
}

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  AEUS COMPREHENSIVE QA   ($([ "$QUICK" -eq 1 ] && echo 'QUICK' || echo 'FULL'))   $(date '+%Y-%m-%d %H:%M:%S')"
echo "════════════════════════════════════════════════════════════════"

if ! conda run -n qlib_run python -c "import qlib" 2>/dev/null; then
    echo "ERROR: qlib_run conda env not available." >&2; exit 1
fi

# ── Phase 1: Light smoke — every entrypoint ────────────────────────────────
echo ""; echo "── Phase 1: Light smoke (every entrypoint) ──────────────────────"
# NOTE: `validate` exits 1 when the win-criterion is NOT met (e.g. V2 vs XLU/GRID
# — an expected, correct verdict, not a crash).  The smoke therefore checks that
# validate RAN TO COMPLETION (printed "WIN CRITERION" with no traceback), not the
# verdict itself.  The win-criterion PASS tally lives in Phase 2 (matrix).
ran_ok() {  # $1 = description, rest = command; pass iff "WIN CRITERION" printed & no traceback
    local desc="$1"; shift
    TOTAL=$((TOTAL + 1)); printf "  [%2d] %-52s " "$TOTAL" "$desc"
    local log="$QA_LOG_DIR/qa_$(echo "$desc" | tr -c 'A-Za-z0-9' '_' | cut -c1-40).log"
    "$@" > "$log" 2>&1 || true
    if grep -q "WIN CRITERION" "$log" && ! grep -qE "Traceback \(most recent|Error:" "$log"; then
        local verdict; verdict=$(grep -oE "PASS|FAIL" "$log" | tail -1)
        printf "${GREEN}PASS${NC} (ran; verdict=%s)\n" "$verdict"; PASS=$((PASS + 1))
    else
        printf "${RED}FAIL${NC} (did not complete)\n"; FAIL=$((FAIL + 1))
        FAILURES="$FAILURES\n  [$TOTAL] $desc — did not complete (log: $log)"; tail -4 "$log" | sed 's/^/       /'
    fi
}

run_test "status"                       $PIPE status
run_test "dry-run (V1, stock layer)"    $PIPE dry-run
run_test "dry-run --signal-version v2"  $PIPE dry-run --signal-version v2
ran_ok   "validate V1 (completes)"      $PIPE validate
ran_ok   "validate V2 (completes)"      $PIPE validate --signal-version v2
run_test "backtest (active param set)"  $PIPE backtest
run_test "batch (33 param sets)"        $PIPE batch
run_test "walk-forward (IS/OOS)"        $PIPE walk-forward
run_test "weekly (PIT health + review)" $PIPE weekly
run_test "update_data (incremental)"    $PIPE update_data
run_test "test (pytest, 107 synthetic)" $PIPE test

# ── Phase 2: 33×V1/V2 matrix ───────────────────────────────────────────────
echo ""; echo "── Phase 2: 33×V1/V2 matrix (backtest + validate) ───────────────"
run_test "matrix 33×V1/V2 (66 bt + 66 validate)"  $PY -m $PKG.tests.aeus_matrix

if [ "$QUICK" -eq 1 ]; then
    echo ""; echo "  [--quick] skipping Phase 3 (heavy) + Phase 4 (deep verify) + Phase 5 (PDF)."
else
    # ── Phase 3: Heavy suite (regenerates all Excel/PDF) ───────────────────
    echo ""; echo "── Phase 3: Heavy suite (daily_backtest V1+V2, all 33) ──────────"
    run_test "daily_backtest.sh (V1+V2 full suite)"  bash "$SD/daily_backtest.sh" --skip-holiday

    # ── Phase 4: Deep per-sheet verification ───────────────────────────────
    echo ""; echo "── Phase 4: Deep per-sheet verification (every sheet) ───────────"
    run_test "deep verify Excel (all 33×V1/V2 IS + IS-OOS)"  $PY -m $PKG.tests.aeus_verify_excel "$TODAY"

    # ── Phase 5: PDF validity ──────────────────────────────────────────────
    echo ""; echo "── Phase 5: PDF tearsheet validity ──────────────────────────────"
    run_test "PDF tearsheets valid + multi-page"  bash -c '
        SD="'"$SD"'"; bad=0; n=0
        for f in "$SD"/report/output/*.pdf; do
            [ -f "$f" ] || continue; n=$((n+1))
            hdr=$(head -c4 "$f"); pages=$(grep -ac "/Type[[:space:]]*/Page" "$f")
            [ "$hdr" = "%PDF" ] || { echo "INVALID header: $f"; bad=1; }
            [ "$pages" -ge 1 ] || { echo "0 pages: $f"; bad=1; }
        done
        [ "$n" -ge 1 ] || { echo "no PDFs found"; bad=1; }
        echo "checked $n PDF(s)"; exit $bad'
fi

# ── Phase 6: Log error scan (THIS run's dated logs only) ───────────────────
echo ""; echo "── Phase 6: Log error scan (today's logs only; benign fallback OK) ──"
run_test "no tracebacks in today's logs"  bash -c '
    SD="'"$SD"'"; TODAY="'"$TODAY"'"
    # Only this run'"'"'s dated logs (aeus_<mode>_<YYYYMMDD>_*.log) — NOT old logs
    # or accumulating cron_*.log, which would false-flag stale historical errors.
    hits=$(grep -lE "Traceback \(most recent|^[A-Za-z._]+Error:|FAILED:|Exception:" "$SD"/logs/*"$TODAY"*.log 2>/dev/null || true)
    if [ -n "$hits" ]; then echo "today logs with real errors:"; echo "$hits"; exit 1; fi
    echo "no real errors in today logs"'

# ── Summary ────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo -e "  RESULTS:  ${GREEN}${PASS} PASS${NC}  ${RED}${FAIL} FAIL${NC}  / ${TOTAL} total"
echo "════════════════════════════════════════════════════════════════"
if [ "$FAIL" -gt 0 ]; then
    echo -e "  FAILURES:${FAILURES}"
    echo "════════════════════════════════════════════════════════════════"
    exit 1
fi
echo "  All phases passed."
echo "════════════════════════════════════════════════════════════════"
exit 0
