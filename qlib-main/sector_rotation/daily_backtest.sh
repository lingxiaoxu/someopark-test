#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# SSRS Daily Backtest Pipeline
# ═══════════════════════════════════════════════════════════════════════
#
# Runs the full 6-step V1+V2 backtest suite after market close.
# Manages selected_param_set.json safely (backup → switch → restore).
#
# Usage:
#   bash qlib-main/sector_rotation/daily_backtest.sh
#
# Must be run from someopark-test/ root directory.
# Requires: conda env qlib_run, .env with API keys
#
# Output:
#   historical_runs/sector_rotation/  — 240+ Excel files per run
#   qlib-main/sector_rotation/report/output/  — 2 PDF tearsheets
#   qlib-main/sector_rotation/backtest_results/  — P0 cache files
#
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

SR_DIR="qlib-main/sector_rotation"
SEL_JSON="$SR_DIR/selected_param_set.json"
BACKUP_V1="/tmp/ssrs_selected_v1_backup.json"
BACKUP_V2="/tmp/ssrs_selected_v2_backup.json"
DATE=$(date +%Y%m%d)
LOG_DIR="$SR_DIR/logs"
LOG="$LOG_DIR/daily_backtest_${DATE}.log"

mkdir -p "$LOG_DIR"

# ── Helpers ──────────────────────────────────────────────────────────
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
run_qlib() { set -a && source .env && set +a && conda run -n qlib_run --no-capture-output "$@"; }
get_param() { python3 -c "import json;print(json.load(open('$SEL_JSON'))['param_set'])"; }
get_ver() { python3 -c "import json;print(json.load(open('$SEL_JSON')).get('signal_version','v1'))"; }

log "═══════════════════════════════════════════════════════════════"
log "  SSRS DAILY BACKTEST — $DATE"
log "═══════════════════════════════════════════════════════════════"
log ""

# ── Step 1: V1 Select ────────────────────────────────────────────────
log "Step 1/6: V1 Select (WF OOS + MCPS → best V1 param)"
run_qlib python "$SR_DIR/SectorRotationBatchRun.py" --select --save-equity --signal-version v1 >> "$LOG" 2>&1
V1_PARAM=$(get_param)
log "  ✅ V1 selected: $V1_PARAM"

# Backup V1
cp "$SEL_JSON" "$BACKUP_V1"

# ── Step 2: V2 Select ────────────────────────────────────────────────
log ""
log "Step 2/6: V2 Select (WF OOS + MCPS → best V2 param)"
run_qlib python "$SR_DIR/SectorRotationBatchRun.py" --select --save-equity --signal-version v2 >> "$LOG" 2>&1
V2_PARAM=$(get_param)
log "  ✅ V2 selected: $V2_PARAM"

# Backup V2, restore V1
cp "$SEL_JSON" "$BACKUP_V2"
cp "$BACKUP_V1" "$SEL_JSON"
log "  Restored V1: $(get_param)"

# ── Step 3: V1 Batch (59 IS-only Excel) ──────────────────────────────
log ""
log "Step 3/6: V1 Batch --save-equity (59 IS-only portfolio Excel)"
run_qlib python "$SR_DIR/SectorRotationBatchRun.py" --save-equity --signal-version v1 >> "$LOG" 2>&1
V1_BATCH=$(ls historical_runs/sector_rotation/sr_portfolio_*_v1_IS_batch_*${DATE}*.xlsx 2>/dev/null | wc -l | tr -d ' ')
log "  ✅ V1 batch: $V1_BATCH Excel files"

# ── Step 4: V2 Batch (59 IS-only Excel) ──────────────────────────────
log ""
log "Step 4/6: V2 Batch --save-equity (59 IS-only portfolio Excel)"
run_qlib python "$SR_DIR/SectorRotationBatchRun.py" --save-equity --signal-version v2 >> "$LOG" 2>&1
V2_BATCH=$(ls historical_runs/sector_rotation/sr_portfolio_*_v2_IS_batch_*${DATE}*.xlsx 2>/dev/null | wc -l | tr -d ' ')
log "  ✅ V2 batch: $V2_BATCH Excel files"

# ── Step 5: V1 Tearsheet (59 IS-OOS Excel + PDF) ────────────────────
log ""
log "Step 5/6: V1 Tearsheet (param=$V1_PARAM, 59 IS-OOS Excel + PDF)"
# selected_param_set.json already has V1
run_qlib bash "$SR_DIR/sector_rotation_pipeline.sh" tearsheet >> "$LOG" 2>&1
V1_TS_EXCEL=$(ls historical_runs/sector_rotation/sr_portfolio_*_v1_IS-OOS_tearsheet_*${DATE}*.xlsx 2>/dev/null | wc -l | tr -d ' ')
V1_TS_PDF=$(ls "$SR_DIR/report/output/tearsheet_*v1*${DATE}*.pdf" 2>/dev/null | wc -l | tr -d ' ')
log "  ✅ V1 tearsheet: $V1_TS_EXCEL Excel + $V1_TS_PDF PDF"

# ── Step 6: V2 Tearsheet (59 IS-OOS Excel + PDF) ────────────────────
log ""
log "Step 6/6: V2 Tearsheet (param=$V2_PARAM, 59 IS-OOS Excel + PDF)"
# Temporarily switch to V2
cp "$BACKUP_V2" "$SEL_JSON"
log "  Switched to V2: $(get_param) ($(get_ver))"

run_qlib bash "$SR_DIR/sector_rotation_pipeline.sh" tearsheet >> "$LOG" 2>&1
V2_TS_EXCEL=$(ls historical_runs/sector_rotation/sr_portfolio_*_v2_IS-OOS_tearsheet_*${DATE}*.xlsx 2>/dev/null | wc -l | tr -d ' ')
V2_TS_PDF=$(ls "$SR_DIR/report/output/tearsheet_*v2*${DATE}*.pdf" 2>/dev/null | wc -l | tr -d ' ')
log "  ✅ V2 tearsheet: $V2_TS_EXCEL Excel + $V2_TS_PDF PDF"

# ── Restore V1 production param ──────────────────────────────────────
cp "$BACKUP_V1" "$SEL_JSON"
log ""
log "  Final restore: $(get_param) ($(get_ver))"

# ── Summary ──────────────────────────────────────────────────────────
log ""
log "═══════════════════════════════════════════════════════════════"
log "  SSRS DAILY BACKTEST COMPLETE — $DATE"
log "═══════════════════════════════════════════════════════════════"
log "  V1 select:     $V1_PARAM"
log "  V2 select:     $V2_PARAM"
log "  V1 IS batch:   $V1_BATCH files"
log "  V2 IS batch:   $V2_BATCH files"
log "  V1 tearsheet:  $V1_TS_EXCEL Excel + $V1_TS_PDF PDF"
log "  V2 tearsheet:  $V2_TS_EXCEL Excel + $V2_TS_PDF PDF"
log "  Production:    $(get_param) ($(get_ver))"
log "  Log:           $LOG"
log "═══════════════════════════════════════════════════════════════"
