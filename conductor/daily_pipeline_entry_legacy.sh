#!/bin/bash
# conductor/daily_pipeline_entry_legacy.sh
# Legacy foreground entrypoint for the SomeoPark daily cron.
# Deprecated: production cron now uses daily_pipeline_launch_nohup.py.
# It keeps the long-running pipeline in the foreground of this wrapper while
# providing an atomic single-flight lock and observable PID metadata.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
PIPEDIR=$REPO/pipeline_state
LOGDIR=$PIPEDIR/logs
ENTRY_LOG=$LOGDIR/daily_pipeline_entry.log
LOCKDIR=$PIPEDIR/daily_pipeline.lock

RUNNER_PID_FILE=$PIPEDIR/runner.pid
RUNNER_PGID_FILE=$PIPEDIR/runner.pgid
RUNNER_STARTED_FILE=$PIPEDIR/runner.started_at
RUNNER_FINISHED_FILE=$PIPEDIR/runner.finished_at
WRAPPER_PID_FILE=$PIPEDIR/wrapper.pid

runner_pid=""
lock_acquired=0
aborting=0

mkdir -p "$LOGDIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*" | tee -a "$ENTRY_LOG"
}

last_status_line() {
    if [ -f "$PIPEDIR/status" ]; then
        tail -1 "$PIPEDIR/status"
    fi
}

is_terminal_status() {
    local status="$1"
    [[ "$status" == "ALL_DONE" || "$status" == FAIL:* || "$status" == ABORTED:* ]]
}

pid_alive() {
    local pid="$1"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

read_first_existing_pid() {
    local pid=""
    if [ -f "$RUNNER_PID_FILE" ]; then
        pid=$(cat "$RUNNER_PID_FILE" 2>/dev/null | tr -cd '0-9')
    fi
    if [ -z "$pid" ] && [ -f "$LOCKDIR/runner.pid" ]; then
        pid=$(cat "$LOCKDIR/runner.pid" 2>/dev/null | tr -cd '0-9')
    fi
    if [ -z "$pid" ] && [ -f "$WRAPPER_PID_FILE" ]; then
        pid=$(cat "$WRAPPER_PID_FILE" 2>/dev/null | tr -cd '0-9')
    fi
    if [ -z "$pid" ] && [ -f "$LOCKDIR/wrapper.pid" ]; then
        pid=$(cat "$LOCKDIR/wrapper.pid" 2>/dev/null | tr -cd '0-9')
    fi
    echo "$pid"
}

read_live_existing_pid() {
    local file pid
    for file in "$RUNNER_PID_FILE" "$LOCKDIR/runner.pid" "$WRAPPER_PID_FILE" "$LOCKDIR/wrapper.pid"; do
        if [ -f "$file" ]; then
            pid=$(cat "$file" 2>/dev/null | tr -cd '0-9')
            if pid_alive "$pid"; then
                echo "$pid"
                return 0
            fi
        fi
    done
    return 1
}

cleanup_lock() {
    if [ "$lock_acquired" -eq 1 ]; then
        rm -rf "$LOCKDIR"
    fi
}

terminate_tree() {
    local pid="$1"
    local child
    for child in $(pgrep -P "$pid" 2>/dev/null); do
        terminate_tree "$child"
        kill -TERM "$child" 2>/dev/null || true
    done
}

abort_handler() {
    local sig="$1"
    aborting=1
    log "Received $sig; terminating runner_pid=${runner_pid:-NONE}"

    if [ -n "$runner_pid" ]; then
        terminate_tree "$runner_pid"
        kill -TERM "$runner_pid" 2>/dev/null || true
        sleep 5
        if pid_alive "$runner_pid"; then
            terminate_tree "$runner_pid"
            kill -KILL "$runner_pid" 2>/dev/null || true
        fi
    fi

    local status
    status=$(last_status_line)
    if ! is_terminal_status "$status"; then
        echo "ABORTED:$sig:daily_pipeline_entry" > "$PIPEDIR/status"
    fi

    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$RUNNER_FINISHED_FILE"
    cleanup_lock
    exit 143
}

trap 'abort_handler TERM' TERM
trap 'abort_handler INT' INT
trap 'cleanup_lock' EXIT

cd "$REPO" || exit 1

existing_status=$(last_status_line)

if [ -d "$LOCKDIR" ]; then
    existing_pid=$(read_first_existing_pid)
    live_pid=$(read_live_existing_pid || true)
    if [ -n "$live_pid" ] && ! is_terminal_status "$existing_status"; then
        existing_pid="$live_pid"
        log "Another daily pipeline appears active; pid=$existing_pid status=${existing_status:-UNKNOWN}"
        echo "ACTIVE:$existing_pid:${existing_status:-UNKNOWN}"
        exit 75
    fi

    log "Clearing stale daily pipeline lock; pid=${existing_pid:-NONE} status=${existing_status:-UNKNOWN}"
    rm -rf "$LOCKDIR"
fi

if ! mkdir "$LOCKDIR" 2>/dev/null; then
    existing_pid=$(read_first_existing_pid)
    log "Could not acquire daily pipeline lock; pid=${existing_pid:-UNKNOWN} status=${existing_status:-UNKNOWN}"
    echo "ACTIVE:${existing_pid:-UNKNOWN}:${existing_status:-UNKNOWN}"
    exit 75
fi

lock_acquired=1
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$LOCKDIR/acquired_at"
echo "$$" > "$LOCKDIR/wrapper.pid"
echo "$$" > "$WRAPPER_PID_FILE"
log "Acquired daily pipeline lock; wrapper_pid=$$"

rm -f "$PIPEDIR/pre_status"
log "Starting conductor/pre_pipeline.sh"
bash "$SCRIPT_DIR/pre_pipeline.sh"
pre_exit=$?
pre_status=""
if [ -f "$PIPEDIR/pre_status" ]; then
    pre_status=$(tail -1 "$PIPEDIR/pre_status")
fi

if [ "$pre_exit" -eq 0 ] && [ "$pre_status" = "ALL_DONE" ]; then
    log "conductor/pre_pipeline.sh completed successfully"
else
    if [ "$pre_exit" -eq 0 ] && [ -z "$pre_status" ] && tail -40 "$LOGDIR/pre_pipeline_current.log" 2>/dev/null | grep -q "NYSE 今日休市"; then
        log "NYSE closed; conductor/pre_pipeline.sh skipped without status"
        echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$RUNNER_FINISHED_FILE"
        exit 0
    fi

    log "conductor/pre_pipeline.sh failed or status unknown; exit=$pre_exit status=${pre_status:-MISSING}"
    exit "${pre_exit:-1}"
fi

rm -f "$PIPEDIR/status"
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$RUNNER_STARTED_FILE"
log "Starting conductor/pipeline_runner.sh in foreground-managed child"

bash "$SCRIPT_DIR/pipeline_runner.sh" &
runner_pid=$!
echo "$runner_pid" > "$RUNNER_PID_FILE"
echo "$runner_pid" > "$LOCKDIR/runner.pid"
runner_pgid=$(ps -o pgid= -p "$runner_pid" 2>/dev/null | tr -d ' ')
echo "${runner_pgid:-UNKNOWN}" > "$RUNNER_PGID_FILE"
echo "${runner_pgid:-UNKNOWN}" > "$LOCKDIR/runner.pgid"
log "conductor/pipeline_runner.sh started; runner_pid=$runner_pid runner_pgid=${runner_pgid:-UNKNOWN}"

wait "$runner_pid"
runner_exit=$?
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$RUNNER_FINISHED_FILE"

if [ "$aborting" -eq 0 ]; then
    log "conductor/pipeline_runner.sh exited; exit=$runner_exit status=$(last_status_line)"
fi

exit "$runner_exit"
