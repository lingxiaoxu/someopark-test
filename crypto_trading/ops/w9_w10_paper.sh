#!/bin/bash
# Manage ONLY the new W9/W10 local-file paper observer. No W7/W8 service changes.
set -euo pipefail
PAPER_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PAPER_LABEL="com.someopark.crypto.w9w10paper"
PAPER_PLIST="$HOME/Library/LaunchAgents/$PAPER_LABEL.plist"
case "${1:-status}" in
  install)
    PAPER_PYTHON="${PAPER_PYTHON:-/Users/xuling/miniforge3/envs/someopark_run/bin/python}"
    mkdir -p "$HOME/Library/LaunchAgents" "$PAPER_REPO/crypto_trading/logs"
    PAPER_REPO_PATH="$PAPER_REPO" PAPER_RUNTIME_PATH="$PAPER_PYTHON" PAPER_PLIST_PATH="$PAPER_PLIST" \
      "$PAPER_PYTHON" - <<'PY'
import os, plistlib
from pathlib import Path
r=os.environ['PAPER_REPO_PATH']
p=dict(Label='com.someopark.crypto.w9w10paper',
       ProgramArguments=[os.environ['PAPER_RUNTIME_PATH'],'-m',
         'crypto_trading.crypto_strategies.downside_paper.observer',
         '--parameters',r+'/crypto_trading/crypto_strategies/downside_paper/parameters.json','--loop','5'],
       WorkingDirectory=r, RunAtLoad=True, KeepAlive=True, ThrottleInterval=30, Nice=10,
       StandardOutPath=r+'/crypto_trading/logs/w9_w10_paper.log',
       StandardErrorPath=r+'/crypto_trading/logs/w9_w10_paper.log',
       EnvironmentVariables=dict(PYTHONUNBUFFERED='1',ALLOW_LIVE_ORDERS='0'))
Path(os.environ['PAPER_PLIST_PATH']).write_bytes(plistlib.dumps(p))
PY
    if launchctl print "gui/$(id -u)/$PAPER_LABEL" >/dev/null 2>&1; then
      echo "W9/W10 paper observer already loaded; existing service unchanged"
    else
      launchctl bootstrap "gui/$(id -u)" "$PAPER_PLIST"
    fi
    ;;
  status) launchctl print "gui/$(id -u)/$PAPER_LABEL" ;;
  stop) launchctl bootout "gui/$(id -u)/$PAPER_LABEL" ;;
  *) echo "usage: $0 install|status|stop"; exit 2 ;;
esac
