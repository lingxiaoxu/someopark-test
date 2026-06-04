#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
PIPEDIR=$REPO/pipeline_state
LOGFILE=$PIPEDIR/logs/pipeline_current.log
LOCKDIR=$PIPEDIR/daily_pipeline.lock
FINISHED_FILE=$PIPEDIR/runner.finished_at

mkdir -p "$PIPEDIR/logs"
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

run_step 3 "MRPTWalkForward.py --oos-windows 6" "MRPTWalkForward"
run_step 4 "MRPTWalkForwardReport.py" "MRPTWalkForwardReport"
run_step 5 "MTFSWalkForward.py --oos-windows 6" "MTFSWalkForward"
run_step 6 "MTFSWalkForwardReport.py" "MTFSWalkForwardReport"
run_step 7 "DailySignal.py --strategy both --vix-forecast --vix-forecast-finetune" "DailySignal"
run_step 8 "WalkForwardDiagnostic.py" "WalkForwardDiagnostic"
run_step 9 "PnLReport.py --start 2026-03-19" "PnLReport"

log "=== PIPELINE COMPLETE ==="
echo "ALL_DONE" >> "$PIPEDIR/status"
