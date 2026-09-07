#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# SSRS Daily Backtest Pipeline
# ═══════════════════════════════════════════════════════════════════════
#
# Runs the full 6-step V1+V2 backtest suite after market close.
# Manages selected_param_set.json safely (backup → switch → restore).
#
# ⚠️  IMPORTANT: This script uses ONLY the qlib_run conda environment.
#     NEVER use someopark_run — it lacks required packages (qlib, etc).
#     All Python calls go through: conda run -n qlib_run --no-capture-output
#
# Usage:
#   cd /Users/xuling/code/someopark-test
#   bash qlib-main/sector_rotation/daily_backtest.sh
#
# Must be run from someopark-test/ root directory.
# Requires: conda env qlib_run, .env with POLYGON_API_KEY & FRED_API_KEY
#
# Output:
#   historical_runs/sector_rotation/  — ~256 Excel files per run
#   qlib-main/sector_rotation/report/output/  — 2 PDF tearsheets (V1 + V2)
#   qlib-main/sector_rotation/backtest_results/  — P0 cache files
#
# ═══════════════════════════════════════════════════════════════════════

set -uo pipefail
# NOTE: intentionally NOT using set -e here.
# Each step has its own error handling. We don't want a failed file count
# or tearsheet sub-pipeline to abort the remaining steps (V2 still needs to run).

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

CONDA_QLIB="conda run -n qlib_run --no-capture-output"

# ── NYSE Holiday Check (skip on non-trading days) ────────────────────
# 2026-09-07 修:此处原为 `$CONDA_QLIB python3`。在 conda 环境里 `python3` **不解析到
# 环境自身的解释器** —— 实测 `conda run -n qlib_run python3` 落到
# /opt/homebrew/opt/python@3.14/bin/python3.14(系统 Homebrew Python),那里没有 pytz,
# 于是 import 必抛、检查恒走 except 兜底,而兜底**只判周末、完全不认假日**。
# 结果:daily_backtest 在每个休市日都会跑满 60 分钟的全套件(2026-09-07 Labor Day 实测
# 触发)。AISS/AEUS 用 `PY() { conda run -n qlib_run ... python "$@"; }` 因而不受影响。
NYSE_STATUS=$($CONDA_QLIB python -c "
import sys
from datetime import datetime
try:
    import pytz, pandas_market_calendars as mcal
    nyc_date = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d')
    nyse = mcal.get_calendar('NYSE')
    schedule = nyse.schedule(start_date=nyc_date, end_date=nyc_date)
    print('OPEN' if not schedule.empty else 'CLOSED:' + nyc_date)
except Exception:
    from datetime import date
    today = date.today()
    print('CLOSED:weekend' if today.weekday() >= 5 else 'OPEN')
" 2>/dev/null) || NYSE_STATUS="OPEN"

if [[ "$NYSE_STATUS" == CLOSED* ]]; then
    echo "[$(date +%H:%M:%S)] NYSE closed (${NYSE_STATUS#CLOSED:}) — skipping daily backtest"
    exit 0
fi

SR_DIR="qlib-main/sector_rotation"
SEL_JSON="$SR_DIR/selected_param_set.json"
# 2026-09-07: 固定路径改 mktemp(对齐 AISS/AEUS)。两个并发实例(openclaw 超时遗弃
# 子进程后重试就会发生)用同一个固定路径时会**互相覆盖对方的 V1 备份** —— 先跑完的
# 那个把 V2 写进备份,后跑完的据此"恢复",生产就永久停在 V2。
BACKUP_V1="$(mktemp -t ssrs_sel_v1)"
BACKUP_V2="$(mktemp -t ssrs_sel_v2)"
DATE=$(date +%Y%m%d)
LOG_DIR="$SR_DIR/logs"
LOG="$LOG_DIR/daily_backtest_${DATE}.log"

mkdir -p "$LOG_DIR"

# ── Idempotency gate (2026-07-22, mirrors AISS) ──────────────────────
# 场景: cron 外层 agent 汇报失败被判 error → 调度器自动重试已成功的重型管道
# (2026-07-21 AISS 事故: 产物翻倍)。当天日志已有完成标记则只汇报不重跑;
# 手动重跑加 --force。skip 消息只写 stdout(进 cron 汇总日志),不追加进 $LOG——
# SSRS 日志按天单文件,追加会破坏"日志以 SSRS DAILY BACKTEST COMPLETE 结尾"的成功判据。
FORCE_RERUN=0
for _arg in "$@"; do [ "$_arg" = "--force" ] && FORCE_RERUN=1; done
if [ "$FORCE_RERUN" -eq 0 ] && [ -f "$LOG" ] \
   && grep -q "SSRS DAILY BACKTEST COMPLETE" "$LOG"; then
    echo "[$(date +%H:%M:%S)] SSRS daily_backtest already completed today" \
         "($(basename "$LOG")) — idempotent skip, exit 0. Re-run with --force."
    exit 0
fi

# ── Helpers ──────────────────────────────────────────────────────────
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
run_qlib() { set -a && source .env && set +a && conda run -n qlib_run --no-capture-output "$@"; }
get_param() { python3 -c "import json;print(json.load(open('$SEL_JSON'))['param_set'])"; }
get_ver() { python3 -c "import json;print(json.load(open('$SEL_JSON')).get('signal_version','v1'))"; }

log "═══════════════════════════════════════════════════════════════"
log "  SSRS DAILY BACKTEST — $DATE"
log "═══════════════════════════════════════════════════════════════"
log ""

# ── 2026-09-07 mutual exclusion(pipeline_lock.sh,移植自 AEUS 2026-09-01)────
#    放在幂等门与 NYSE 检查之后、套件之前:拿不到锁 = 什么都没写,重试无需 --force。
#    SSRS 的重叠是三家里最严重的:16:40 起跑、实测 39.5-117.2 分钟,daily 信号槽
#    17:40 必落在窗内,两个 V2 窗口(Step 2 与 Step 6)正好骑在上面。至今没出事
#    只因 openclaw 的 cron tick 阻塞把 daily 推后 —— 那是副作用不是保证。
. "$SR_DIR/pipeline_lock.sh"
if ! ssrs_lock_acquire "daily_backtest" "${SSRS_LOCK_WAIT:-1200}"; then
    echo "[$(date +%H:%M:%S)] ══ SSRS DAILY BACKTEST FAILED — pipeline lock busy (daily/monthly still running); nothing was run, retry later ══"
    exit 3
fi

# ── 运行前预备份(2026-09-07,对齐 AISS/AEUS)────────────────────────
# 必须在 Step 1 之前:mktemp 出来的 BACKUP_V1 初始是空文件,若 Step 1 失败且
# selected_param_set.json 缺失,下面那句 `cp "$SEL_JSON" "$BACKUP_V1"` 也会失败,
# 备份就一直是空的 —— 最后的恢复会把**空文件**盖到生产上(AEUS 2026-09-01 实际
# 发生过的事故形态)。先把当前生产选择存下来兜底。
[ -s "$SEL_JSON" ] && cp "$SEL_JSON" "$BACKUP_V1"

# ── 崩溃安全的 V1 恢复(2026-09-07;AEUS 的锁未覆盖此项)──────────────
#    在**第一次写 V2 之前**装 EXIT trap:SIGKILL / openclaw 超时遗弃子进程时,
#    正常路径的"Final restore"不会执行,生产就会永久停在 V2。trap 与显式恢复
#    幂等(同一份 BACKUP_V1 覆盖同一个文件),正常路径下不会重复动作。
_ssrs_restore_v1_on_exit() {
    if [ -s "$BACKUP_V1" ] && [ -f "$SEL_JSON" ] && ! cmp -s "$BACKUP_V1" "$SEL_JSON"; then
        cp "$BACKUP_V1" "$SEL_JSON" 2>/dev/null \
            && echo "[$(date +%H:%M:%S)] [trap] 异常退出 — 已把 selected_param_set.json 恢复为 V1" | tee -a "$LOG"
    fi
    rm -f "$BACKUP_V1" "$BACKUP_V2" 2>/dev/null
}
_prev_trap=$(trap -p EXIT | sed -E "s/^trap -- '(.*)' EXIT$/\1/")
# shellcheck disable=SC2064
trap "_ssrs_restore_v1_on_exit${_prev_trap:+; $_prev_trap}" EXIT

# ── Step 1: V1 Select ────────────────────────────────────────────────
log "Step 1/6: V1 Select (WF OOS + MCPS → best V1 param)"
if run_qlib python "$SR_DIR/SectorRotationBatchRun.py" --select --save-equity --signal-version v1 >> "$LOG" 2>&1; then
    V1_PARAM=$(get_param)
    log "  ✅ V1 selected: $V1_PARAM"
else
    V1_PARAM="FAILED"
    log "  ⚠️ V1 select failed (RC=$?)"
fi

# Backup V1(仅在非空时覆盖预备份 —— Step 1 失败留下的坏文件不该顶掉好备份)
[ -s "$SEL_JSON" ] && cp "$SEL_JSON" "$BACKUP_V1"

# ── Step 2: V2 Select ────────────────────────────────────────────────
log ""
log "Step 2/6: V2 Select (WF OOS + MCPS → best V2 param)"
if run_qlib python "$SR_DIR/SectorRotationBatchRun.py" --select --save-equity --signal-version v2 >> "$LOG" 2>&1; then
    V2_PARAM=$(get_param)
    log "  ✅ V2 selected: $V2_PARAM"
else
    V2_PARAM="FAILED"
    log "  ⚠️ V2 select failed (RC=$?)"
fi

# Backup V2, restore V1
[ -s "$SEL_JSON" ] && cp "$SEL_JSON" "$BACKUP_V2"
if [ -s "$BACKUP_V1" ]; then
    cp "$BACKUP_V1" "$SEL_JSON"; log "  Restored V1: $(get_param)"
else
    log "  ⚠️ V1 备份为空 — selected_param_set.json 保持原样(绝不用空文件覆盖生产)"
fi

# ── Step 3: V1 Batch (all-set IS-only Excel) ──────────────────────────────
log ""
log "Step 3/6: V1 Batch --save-equity (all-set IS-only portfolio Excel)"
if run_qlib python "$SR_DIR/SectorRotationBatchRun.py" --save-equity --signal-version v1 >> "$LOG" 2>&1; then
    V1_BATCH=$(ls historical_runs/sector_rotation/sr_portfolio_*_v1_IS_batch_*${DATE}*.xlsx 2>/dev/null | wc -l | tr -d ' ')
    log "  ✅ V1 batch: $V1_BATCH Excel files"
else
    V1_BATCH=0
    log "  ⚠️ V1 batch failed (RC=$?)"
fi

# ── Step 4: V2 Batch (all-set IS-only Excel) ──────────────────────────────
log ""
log "Step 4/6: V2 Batch --save-equity (all-set IS-only portfolio Excel)"
if run_qlib python "$SR_DIR/SectorRotationBatchRun.py" --save-equity --signal-version v2 >> "$LOG" 2>&1; then
    V2_BATCH=$(ls historical_runs/sector_rotation/sr_portfolio_*_v2_IS_batch_*${DATE}*.xlsx 2>/dev/null | wc -l | tr -d ' ')
    log "  ✅ V2 batch: $V2_BATCH Excel files"
else
    V2_BATCH=0
    log "  ⚠️ V2 batch failed (RC=$?)"
fi

# ── Step 5: V1 Tearsheet (all-set IS-OOS Excel + PDF) ────────────────────
log ""
log "Step 5/6: V1 Tearsheet (param=$V1_PARAM, all-set IS-OOS Excel + PDF)"
# selected_param_set.json already has V1
if run_qlib bash "$SR_DIR/sector_rotation_pipeline.sh" tearsheet >> "$LOG" 2>&1; then
    V1_TS_EXCEL=$(ls historical_runs/sector_rotation/sr_portfolio_*_v1_IS-OOS_tearsheet_*${DATE}*.xlsx 2>/dev/null | wc -l | tr -d ' ')
    V1_TS_PDF=$(ls "$SR_DIR"/report/output/tearsheet_*v1*${DATE}*.pdf 2>/dev/null | wc -l | tr -d ' ')
    log "  ✅ V1 tearsheet: $V1_TS_EXCEL Excel + $V1_TS_PDF PDF"
else
    log "  ⚠️ V1 tearsheet failed (RC=$?) — continuing to V2"
    V1_TS_EXCEL=0; V1_TS_PDF=0
fi

# ── Step 6: V2 Tearsheet (all-set IS-OOS Excel + PDF) ────────────────────
log ""
log "Step 6/6: V2 Tearsheet (param=$V2_PARAM, all-set IS-OOS Excel + PDF)"
# Temporarily switch to V2(备份为空则跳过整段,不拿空文件覆盖生产)
if [ -s "$BACKUP_V2" ]; then
    cp "$BACKUP_V2" "$SEL_JSON"; log "  Switched to V2: $(get_param) ($(get_ver))"
else
    log "  ⚠️ V2 备份为空 — 跳过 V2 tearsheet 的切换"
fi

if run_qlib bash "$SR_DIR/sector_rotation_pipeline.sh" tearsheet >> "$LOG" 2>&1; then
    V2_TS_EXCEL=$(ls historical_runs/sector_rotation/sr_portfolio_*_v2_IS-OOS_tearsheet_*${DATE}*.xlsx 2>/dev/null | wc -l | tr -d ' ')
    V2_TS_PDF=$(ls "$SR_DIR"/report/output/tearsheet_*v2*${DATE}*.pdf 2>/dev/null | wc -l | tr -d ' ')
    log "  ✅ V2 tearsheet: $V2_TS_EXCEL Excel + $V2_TS_PDF PDF"
else
    log "  ⚠️ V2 tearsheet failed (RC=$?) — continuing to restore"
    V2_TS_EXCEL=0; V2_TS_PDF=0
fi

# ── Restore V1 production param ──────────────────────────────────────
if [ -s "$BACKUP_V1" ]; then
    cp "$BACKUP_V1" "$SEL_JSON"
    log ""
    log "  Final restore: $(get_param) ($(get_ver))"
else
    log ""
    log "  ⚠️ V1 备份为空 — 未做最终恢复,selected_param_set.json 保持原样"
fi

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
