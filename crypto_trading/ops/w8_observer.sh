#!/bin/bash
# Install/remove ONLY the independent W8 paper observer; never reload W1-W7.
set -euo pipefail
W8_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
W8_LABEL="com.someopark.crypto.w8observer"
W8_PLIST="$HOME/Library/LaunchAgents/$W8_LABEL.plist"
W8_LOG="$W8_REPO/crypto_trading/logs/w8_observer.log"
case "${1:-status}" in
  install)
    W8_CONDA="$(command -v conda)"
    mkdir -p "$HOME/Library/LaunchAgents" "$W8_REPO/crypto_trading/logs"
    # plistlib handles XML escaping of workspace and runtime paths.
    W8_REPO_PATH="$W8_REPO" W8_CONDA_PATH="$W8_CONDA" W8_PLIST_PATH="$W8_PLIST" \
      conda run -n someopark_run --no-capture-output python - <<'PY'
import os, plistlib
from pathlib import Path
r=os.environ['W8_REPO_PATH']
p=dict(Label='com.someopark.crypto.w8observer',
       ProgramArguments=[os.environ['W8_CONDA_PATH'],'run','-n','someopark_run','--no-capture-output',
                         'python','-m','crypto_trading.crypto_strategies.live_watch.w8_complete_set','--loop','2'],
       WorkingDirectory=r, RunAtLoad=True, KeepAlive=True, ThrottleInterval=10,
       StandardOutPath=r+'/crypto_trading/logs/w8_observer.log',
       StandardErrorPath=r+'/crypto_trading/logs/w8_observer.log',
       EnvironmentVariables=dict(PYTHONUNBUFFERED='1', ALLOW_LIVE_ORDERS='0'))
Path(os.environ['W8_PLIST_PATH']).write_bytes(plistlib.dumps(p))
PY
    if launchctl print "gui/$(id -u)/$W8_LABEL" >/dev/null 2>&1; then
      echo "W8 observer already loaded; existing process left running"
    else
      launchctl bootstrap "gui/$(id -u)" "$W8_PLIST"
    fi
    ;;
  stop) launchctl bootout "gui/$(id -u)/$W8_LABEL" ;;
  status) launchctl print "gui/$(id -u)/$W8_LABEL" ;;
  *) echo "usage: $0 install|stop|status"; exit 2 ;;
esac
