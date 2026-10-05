#!/bin/bash
# W11 (W7 prod rules as a paper mirror + model overlays). Paper only.
#   ops/w11.sh run        foreground observer (Ctrl-C to stop)
#   ops/w11.sh once       one tick
#   ops/w11.sh train      retrain models now (writes trading_signals/w11_prod_mirror/{models,data}/<stamp>)
#   ops/w11.sh report     print + write report/latest.md
#   ops/w11.sh status     launchd state, last ticks, ledgers
#   ops/w11.sh plist      print the LaunchAgent plist to install (installation needs the user's approval)
set -u
REPO=/Users/xuling/code/someopark-test; CT=$REPO/crypto_trading; OUT=$CT/trading_signals/w11_prod_mirror; LABEL=com.someopark.crypto.w11mirror
cd $REPO
PY="/Users/xuling/miniforge3/envs/someopark_run/bin/python"
case "${1:-status}" in
  run)    PYTHONPATH=$REPO exec nice -n 5 $PY -m crypto_trading.crypto_strategies.w11_prod_mirror.observer ;;
  once)   PYTHONPATH=$REPO $PY -m crypto_trading.crypto_strategies.w11_prod_mirror.observer --once ;;
  train)  PYTHONPATH=$REPO nice -n 15 $PY -m crypto_trading.crypto_strategies.w11_prod_mirror.train ;;
  report) PYTHONPATH=$REPO $PY -m crypto_trading.crypto_strategies.w11_prod_mirror.report ;;
  status) launchctl print gui/$(id -u)/$LABEL 2>/dev/null | grep -E "state =|pid =" ; tail -3 $CT/logs/w11_mirror.log 2>/dev/null | cut -c1-200
          $PY -c "import json;d=json.load(open('$OUT/state.json'));print('cycles',d.get('cycles'),'model',d.get('model_stamp'),'fills',len(d.get('fills',[])),'errors',len(d.get('errors',[])));print({k:(round(v['pnl'],2),v['trades']) for k,v in d['ledgers'].items()})" 2>/dev/null ;;
  plist)  cat <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>-m</string><string>crypto_trading.crypto_strategies.w11_prod_mirror.observer</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict><key>PYTHONPATH</key><string>$REPO</string><key>PYTHONUNBUFFERED</key><string>1</string><key>HF_HUB_OFFLINE</key><string>1</string><key>OMP_NUM_THREADS</key><string>2</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>30</integer><key>Nice</key><integer>5</integer>
  <key>StandardOutPath</key><string>$CT/logs/w11_mirror.log</string><key>StandardErrorPath</key><string>$CT/logs/w11_mirror.log</string>
</dict></plist>
PL
  ;;
  *) echo "usage: $0 run|once|train|report|status|plist" ;;
esac
