#!/bin/bash
# Manage only the separate W10 fresh-value / 60-second entry paper experiment.
set -euo pipefail
W10_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
W10_PYTHON="${W10_PYTHON:-/Users/xuling/miniforge3/envs/someopark_run/bin/python}"
W10_LABEL="com.someopark.crypto.w10entrypaper"
W10_DOMAIN="gui/$(id -u)"
W10_PLIST="$HOME/Library/LaunchAgents/$W10_LABEL.plist"

case "${1:-status}" in
  install)
    if launchctl print "$W10_DOMAIN/$W10_LABEL" >/dev/null 2>&1; then
      echo "$W10_LABEL already loaded; existing service unchanged"
      exit 0
    fi
    mkdir -p "$HOME/Library/LaunchAgents" "$W10_REPO/crypto_trading/logs"
    W10_REPO_PATH="$W10_REPO" W10_PYTHON_PATH="$W10_PYTHON" W10_PLIST_PATH="$W10_PLIST" \
      "$W10_PYTHON" - <<'PY'
import json, os, plistlib, time
from pathlib import Path
r=Path(os.environ['W10_REPO_PATH'])
state=json.loads((r/'crypto_trading/trading_signals/live_watch/w8_complete_set_state.json').read_text())
assert state['books']['tilted']['complete_sets'] is False, 'Parent must be W8 tilted'
assert 0 <= time.time()-state['last_tick_ts'] <= 90, 'W8 parent heartbeat must be fresh before registration'
label='com.someopark.crypto.w10entrypaper'
log=str(r/'crypto_trading/logs/w10_entry_paper.log')
p=dict(Label=label,ProgramArguments=[os.environ['W10_PYTHON_PATH'],'-m',
       'crypto_trading.crypto_strategies.w10_entry_paper.fv60_observer','--loop','1'],
       WorkingDirectory=str(r),RunAtLoad=True,KeepAlive=True,ThrottleInterval=30,Nice=10,
       StandardOutPath=log,StandardErrorPath=log,
       EnvironmentVariables=dict(PYTHONUNBUFFERED='1',ALLOW_LIVE_ORDERS='0',
         OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',
         VECLIB_MAXIMUM_THREADS='1',NUMEXPR_NUM_THREADS='1'))
Path(os.environ['W10_PLIST_PATH']).write_bytes(plistlib.dumps(p))
PY
    launchctl bootstrap "$W10_DOMAIN" "$W10_PLIST"
    echo "$W10_LABEL installed"
    ;;
  status) launchctl print "$W10_DOMAIN/$W10_LABEL" ;;
  stop) launchctl bootout "$W10_DOMAIN/$W10_LABEL" ;;
  *) echo "usage: $0 install|status|stop"; exit 2 ;;
esac
