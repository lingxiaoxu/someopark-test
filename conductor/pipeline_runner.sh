#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
PIPEDIR=$REPO/pipeline_state
LOGFILE=$PIPEDIR/logs/pipeline_current.log
LOCKDIR=$PIPEDIR/daily_pipeline.lock
FINISHED_FILE=$PIPEDIR/runner.finished_at

mkdir -p "$PIPEDIR/logs"

# ── 日志轮转(2026-08-18 加)────────────────────────────────────────────────
# pipeline_current.log 此前跨夜纯追加、从不轮转,实测涨到 980MB:tail -f 监控
# 被系统回收、全文排查极慢。启动时超过阈值就压缩归档(实测 19:1,980MB→51MB),
# 保留最近 7 份。用 `: >` **原地清空**而非 mv —— 保住 inode,已挂在该文件上的
# tail -f 监控能识别截断并继续跟随,mv 会让它们静默跟着旧 inode。
LOG_MAX_BYTES=$((200 * 1024 * 1024))
if [ -f "$LOGFILE" ] && \
   [ "$(stat -f%z "$LOGFILE" 2>/dev/null || echo 0)" -gt "$LOG_MAX_BYTES" ]; then
    mkdir -p "$PIPEDIR/logs/archive"
    if gzip -c "$LOGFILE" > "$PIPEDIR/logs/archive/pipeline_$(date '+%Y%m%d_%H%M%S').log.gz"; then
        : > "$LOGFILE"                       # 原地截断,保 inode
        ls -t "$PIPEDIR/logs/archive"/pipeline_*.log.gz 2>/dev/null \
            | tail -n +8 | tr '\n' '\0' | xargs -0 rm -f 2>/dev/null
    fi                                       # 归档失败则原样保留,绝不丢日志
fi

cd "$REPO" || exit 1

# Initialize conda for non-interactive shell
source /Users/xuling/miniforge3/etc/profile.d/conda.sh

set -a && source "$REPO/.env" && set +a

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"; }

last_status_line() {
    if [ -f "$PIPEDIR/status" ]; then
        tail -1 "$PIPEDIR/status"
    fi
}

is_terminal_status() {
    local status="$1"
    [[ "$status" == "ALL_DONE" || "$status" == FAIL:* || "$status" == ABORTED:* ]]
}

cleanup_runner() {
    local rc=$?
    local status
    status=$(last_status_line)

    if ! is_terminal_status "$status"; then
        if [ "$rc" -eq 143 ] || [ "$rc" -eq 130 ]; then
            echo "ABORTED:signal:pipeline_runner" > "$PIPEDIR/status"
        elif [ "$rc" -ne 0 ]; then
            echo "FAIL:runner:exit:$rc" > "$PIPEDIR/status"
        fi
    fi

    date -u '+%Y-%m-%dT%H:%M:%SZ' > "$FINISHED_FILE"

    if [ -d "$LOCKDIR" ]; then
        lock_runner=$(cat "$LOCKDIR/runner.pid" 2>/dev/null | tr -cd '0-9')
        if [ "$lock_runner" = "$$" ]; then
            rm -rf "$LOCKDIR"
        fi
    fi

    exit "$rc"
}

term_handler() {
    log "=== PIPELINE RECEIVED TERM (PID=$$ PGID=$(ps -o pgid= -p "$$" 2>/dev/null | tr -d ' ')) ==="
    echo "ABORTED:TERM:pipeline_runner" > "$PIPEDIR/status"
    exit 143
}

int_handler() {
    log "=== PIPELINE RECEIVED INT (PID=$$ PGID=$(ps -o pgid= -p "$$" 2>/dev/null | tr -d ' ')) ==="
    echo "ABORTED:INT:pipeline_runner" > "$PIPEDIR/status"
    exit 130
}

trap cleanup_runner EXIT
trap term_handler TERM
trap int_handler INT

run_step() {
    local NUM=$1 CMD=$2 NAME=$3
    log "=== STEP $NUM START: $NAME ==="
    set -a && source "$REPO/.env" && set +a && conda run -n someopark_run --no-capture-output python $CMD >> "$LOGFILE" 2>&1
    local RC=$?
    log "=== STEP $NUM END: $NAME (exit=$RC) ==="
    if [ $RC -ne 0 ]; then
        echo "FAIL:$NUM:$NAME:$RC" > "$PIPEDIR/status"
        exit $RC
    fi
    echo "DONE:$NUM" >> "$PIPEDIR/status"
}

log "=== PIPELINE START (PID=$$, PPID=$PPID) ==="
echo "RUNNING" > "$PIPEDIR/status"

# Semi event-risk data refresh (qlib_run; runs BEFORE the strategies so both
# MTFS and AISS read fresh event_risk data). NON-FATAL: a failure must not abort
# the daily pipeline (the overlay is default-off until validated + enabled).
log "[event-risk] refreshing event_risk data (non-fatal)..."
conda run -n qlib_run --no-capture-output python "$REPO/RefreshEventRiskData.py" >> "$LOGFILE" 2>&1 \
    && log "[event-risk] data refresh done" \
    || log "[event-risk] WARN: data refresh failed (non-fatal, continuing)"

run_step 3 "MRPTWalkForward.py --mode rolling --train-months 19 --oos-windows 9 --oos-window-days 50 --oos-overlap 10" "MRPTWalkForward"
run_step 4 "MRPTWalkForwardReport.py" "MRPTWalkForwardReport"
run_step 5 "MTFSWalkForward.py --mode rolling --train-months 19 --oos-windows 9 --oos-window-days 50 --oos-overlap 10" "MTFSWalkForward"
run_step 6 "MTFSWalkForwardReport.py" "MTFSWalkForwardReport"
# MacroSimilarity 增量更新(黄金窗口相似度存储;冻结 encoder,秒级)。
# NON-FATAL:失败不 abort(DailySignal 对缺失存储有完整回退)。
log "[macro-sim] updating similarity store (non-fatal)..."
conda run -n someopark_run --no-capture-output python "$REPO/MacroSimilarity.py" --update >> "$LOGFILE" 2>&1 \
    && log "[macro-sim] update done" \
    || log "[macro-sim] WARN: update failed (non-fatal, DailySignal falls back to last-window)"

run_step 7 "DailySignal.py --strategy both --vix-forecast --vix-forecast-finetune" "DailySignal"
# Controller security master 补录(STEP 7 之后:吃当天最终 inventory,新开仓的
# 首见票注册进白名单,shadow 循环下一轮热重载即吃进新结构,免手跑)。
# NON-FATAL:构建失败 master 原样不动(save 前任何异常都不落盘,原子替换),
# controller 继续服务旧结构 + 前端结构同步亮 ×,当天手动补跑即可 —— 与旧流程同。
log "[sec-master] refreshing controller security master (non-fatal)..."
conda run -n someopark_run --no-capture-output python -m controller.registry --build-master >> "$LOGFILE" 2>&1 \
    && log "[sec-master] build done" \
    || log "[sec-master] WARN: build failed (non-fatal, controller serves old structure; run manually)"
run_step 8 "WalkForwardDiagnostic.py" "WalkForwardDiagnostic"
# 起始日期**不再硬编码**（此前固定 2026-03-19）。不传 --start 时 PnLReport 走
# default_report_start()：取运行日前一月所属季度的首日 —— 季度首月沿用上一季度
# 起点,次月起切到本季度（7月→4/1；8月起→7/1；10月→7/1；11月起→10/1）。
# 规则只有一处真源(PnLReport.default_report_start)，勿在此重复写死日期。
run_step 9 "PnLReport.py" "PnLReport"

log "=== PIPELINE COMPLETE ==="
echo "ALL_DONE" >> "$PIPEDIR/status"
