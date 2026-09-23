#!/bin/bash
# Manage ONLY the independent daily-tuned RNN W9 paper services.
set -euo pipefail
W9_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
W9_PYTHON="${W9_PYTHON:-/Users/xuling/miniforge3/envs/someopark_run/bin/python}"
W9_DOMAIN="gui/$(id -u)"

install_service() {
  local component="$1"
  local label="com.someopark.crypto.w9rnn${component}"
  local plist="$HOME/Library/LaunchAgents/$label.plist"
  if launchctl print "$W9_DOMAIN/$label" >/dev/null 2>&1; then
    echo "$label already loaded; existing service unchanged"
    return
  fi
  mkdir -p "$HOME/Library/LaunchAgents" "$W9_REPO/crypto_trading/logs"
  W9_REPO_PATH="$W9_REPO" W9_PYTHON_PATH="$W9_PYTHON" W9_PLIST_PATH="$plist" W9_COMPONENT="$component" \
    "$W9_PYTHON" - <<'PY'
import json, os, plistlib, time
from datetime import datetime, timezone
from pathlib import Path
r = Path(os.environ['W9_REPO_PATH'])
runtime = r/'crypto_trading/trading_signals/w9_rnn_paper'
component = os.environ['W9_COMPONENT']
if component == 'paper':
    status = json.loads((runtime/'model_status.json').read_text())
    active = json.loads((runtime/'active_model.json').read_text())
    now = time.time()
    assert status['status'] == 'running' and 0 <= now-status['heartbeat_ts'] < 30, 'W9 model worker must be healthy first'
    assert active['day'] == datetime.now(timezone.utc).strftime('%Y-%m-%d'), 'Today\'s W9 model must be ready first'
    assert status['model_id'] == active['model_id'], 'Worker must load the published W9 model first'
    with (runtime/'live_predictions.jsonl').open('rb') as stream:
        stream.seek(max(0, stream.seek(0, 2)-200000))
        if stream.tell():
            stream.readline()
        rows = [json.loads(line) for line in stream if line.endswith(b'\n')]
    fresh = {v['asset'] for v in rows if v.get('model_id') == active['model_id']
             and 0 <= now-v.get('persisted_at', 0) <= 120
             and v.get('risk_available') and v.get('strict_recorded_pit_eligible')}
    assert fresh == {'BTC','ETH','SOL','DOGE','XRP'}, 'Wait for fresh W9 predictions for all five assets before paper registration'
    arguments = ['-m', 'crypto_trading.crypto_strategies.w9_rnn_paper.observer', '--loop', '2']
else:
    assert component == 'model'
    arguments = ['-m', 'crypto_trading.crypto_strategies.w9_rnn_paper.model', 'worker',
                 '--runtime-dir', str(runtime), '--poll-seconds', '2']
label = 'com.someopark.crypto.w9rnn'+component
log = str(r/'crypto_trading/logs'/('w9_rnn_'+component+'.log'))
p = dict(Label=label, ProgramArguments=[os.environ['W9_PYTHON_PATH'], *arguments],
         WorkingDirectory=str(r), RunAtLoad=True, KeepAlive=True, ThrottleInterval=30, Nice=10,
         StandardOutPath=log, StandardErrorPath=log,
         EnvironmentVariables=dict(PYTHONUNBUFFERED='1', ALLOW_LIVE_ORDERS='0',
             OMP_NUM_THREADS='1', MKL_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
             VECLIB_MAXIMUM_THREADS='1', NUMEXPR_NUM_THREADS='1'))
Path(os.environ['W9_PLIST_PATH']).write_bytes(plistlib.dumps(p))
PY
  launchctl bootstrap "$W9_DOMAIN" "$plist"
  echo "$label installed"
}

case "${1:-status}" in
  install-model) install_service model ;;
  install-paper) install_service paper ;;
  status)
    for component in model paper; do
      launchctl print "$W9_DOMAIN/com.someopark.crypto.w9rnn${component}" || true
    done
    ;;
  stop)
    for component in paper model; do
      launchctl bootout "$W9_DOMAIN/com.someopark.crypto.w9rnn${component}" || true
    done
    ;;
  *) echo "usage: $0 install-model|install-paper|status|stop"; exit 2 ;;
esac
