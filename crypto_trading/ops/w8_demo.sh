#!/bin/bash
# Manage ONLY the independent W8 demo mirror; never reload W7 or W8 observer.
set -euo pipefail
W8_DEMO_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
W8_DEMO_LABEL="com.someopark.crypto.w8demo"
W8_DEMO_PLIST="$HOME/Library/LaunchAgents/$W8_DEMO_LABEL.plist"
case "${1:-status}" in
  install)
    if launchctl print "gui/$(id -u)/$W8_DEMO_LABEL" >/dev/null 2>&1; then
      echo "W8 demo mirror already loaded; existing process left running"
      exit 0
    fi
    W8_DEMO_CONDA="$(command -v conda)"
    mkdir -p "$HOME/Library/LaunchAgents" "$W8_DEMO_REPO/crypto_trading/logs"
    # plistlib handles XML escaping of workspace and runtime paths.
    W8_DEMO_REPO_PATH="$W8_DEMO_REPO" W8_DEMO_CONDA_PATH="$W8_DEMO_CONDA" \
      W8_DEMO_PLIST_PATH="$W8_DEMO_PLIST" \
      conda run -n someopark_run --no-capture-output python - <<'PY'
import os, plistlib
from pathlib import Path
r = os.environ['W8_DEMO_REPO_PATH']
p = dict(Label='com.someopark.crypto.w8demo',
         ProgramArguments=[os.environ['W8_DEMO_CONDA_PATH'], 'run', '-n', 'someopark_run',
                           '--no-capture-output', 'python', '-m',
                           'crypto_trading.ops.w8_demo_mirror', '--arm'],
         WorkingDirectory=r, RunAtLoad=True, KeepAlive=True, ThrottleInterval=10,
         StandardOutPath=r+'/crypto_trading/logs/w8_demo_mirror.log',
         StandardErrorPath=r+'/crypto_trading/logs/w8_demo_mirror.log',
         EnvironmentVariables=dict(PYTHONUNBUFFERED='1', ALLOW_LIVE_ORDERS='0'))
Path(os.environ['W8_DEMO_PLIST_PATH']).write_bytes(plistlib.dumps(p))
PY
    launchctl bootstrap "gui/$(id -u)" "$W8_DEMO_PLIST"
    ;;
  stop) launchctl bootout "gui/$(id -u)/$W8_DEMO_LABEL" ;;
  status) launchctl print "gui/$(id -u)/$W8_DEMO_LABEL" ;;
  *) echo "usage: $0 install|stop|status"; exit 2 ;;
esac
