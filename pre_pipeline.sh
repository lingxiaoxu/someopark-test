#!/bin/bash
# pre_pipeline.sh
# Runs Step 1 (SelectPairs.py) and Step 2 (UpdateStep1Configs.py)
# This is the standard first stage of the daily pipeline.
# After this completes, run pipeline_runner.sh for Steps 3-9.

REPO=/Users/xuling/code/someopark-test
PIPEDIR=$REPO/pipeline_state
LOGFILE=$PIPEDIR/logs/pre_pipeline_current.log

mkdir -p "$PIPEDIR/logs"
cd "$REPO" || exit 1

# Initialize conda for non-interactive shell
source /Users/xuling/miniforge3/etc/profile.d/conda.sh

set -a && source "$REPO/.env" && set +a

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"; }

# === NYSE Holiday Check (BEFORE writing any status files) ===
# Get NYSE date in New York time (cron runs at UTC 21:45 = NY 17:45 = same NYSE trading day)
NYSE_STATUS=$(conda run -n someopark_run --no-capture-output python3 -c "
import pandas_market_calendars as mcal
from datetime import datetime
import pytz
nyc_tz = pytz.timezone('America/New_York')
nyc_date = datetime.now(nyc_tz).strftime('%Y-%m-%d')
nyse = mcal.get_calendar('NYSE')
schedule = nyse.schedule(start_date=nyc_date, end_date=nyc_date)
print('OPEN' if not schedule.empty else 'CLOSED:' + nyc_date)
" 2>/dev/null)

if [[ "$NYSE_STATUS" == CLOSED* ]]; then
    NYSE_DATE="${NYSE_STATUS#CLOSED:}"
    log "=== NYSE 今日休市 ($NYSE_DATE)，跳过 pipeline，exit 0 ==="
    # Do NOT write any status files — watchdog will also detect holiday and skip
    exit 0
fi

run_step() {
    local NUM=$1 CMD=$2 NAME=$3
    log "=== STEP $NUM START: $NAME ==="
    set -a && source "$REPO/.env" && set +a && conda run -n someopark_run --no-capture-output python $CMD >> "$LOGFILE" 2>&1
    local RC=$?
    log "=== STEP $NUM END: $NAME (exit=$RC) ==="
    if [ $RC -ne 0 ]; then
        echo "FAIL:$NUM:$NAME:$RC" > "$PIPEDIR/pre_status"
        log "PIPELINE PRE-STAGE FAILED at step $NUM"
        exit $RC
    fi
    echo "DONE:$NUM" >> "$PIPEDIR/pre_status"
}

log "=== PRE-PIPELINE START (PID=$$) ==="
echo "RUNNING" > "$PIPEDIR/pre_status"

run_step 1 "SelectPairs.py --save" "SelectPairs"
run_step 2 "UpdateStep1Configs.py" "UpdateStep1Configs"
run_step 3 "MacroStateStore.py --update" "MacroStateStore_update"

log "=== PRE-PIPELINE COMPLETE — ready to run pipeline_runner.sh ==="
echo "ALL_DONE" >> "$PIPEDIR/pre_status"
