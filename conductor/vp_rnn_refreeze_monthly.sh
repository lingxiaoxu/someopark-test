#!/bin/bash
# Daily 10:03 ET probe. Train one RNN candidate per month after the fresh panel
# exists; Saturday is reserved for LGBM/quarterly jobs. Never promotes a model.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 1
export PATH="/opt/homebrew/bin:/Users/xuling/miniforge3/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
set -a; . "$REPO_ROOT/.env"; set +a
LOG="$REPO_ROOT/VolumePrediction/logs/rnn_refreeze_$(date +%Y%m%d).log"
mkdir -p "$(dirname "$LOG")"
nice -n 15 conda run -n someopark_run --no-capture-output \
    python -u -m VolumePrediction.rnn_maintenance --train-monthly "$@" >>"$LOG" 2>&1
RC=$?
echo "[$(date '+%F %T')] RNN monthly probe exit=$RC log=$LOG"
exit "$RC"
