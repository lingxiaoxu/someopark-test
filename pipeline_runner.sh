#!/bin/bash
REPO=/Users/xuling/code/someopark-test
PIPEDIR=/Users/xuling/code/someopark-test/pipeline_state
LOGFILE=$PIPEDIR/logs/pipeline_current.log

mkdir -p "$PIPEDIR/logs"
cd "$REPO" || exit 1

# Initialize conda for non-interactive shell
source /Users/xuling/miniforge3/etc/profile.d/conda.sh

set -a && source "$REPO/.env" && set +a

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"; }

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
