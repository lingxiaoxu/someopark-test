#!/bin/bash
# Generate + install launchd agents for crypto_trading (Plan 08 §5 ops).
# House pattern: com.someopark.crypto.* — separate names, never touches the
# existing com.someopark.prediction* agents or MRPT/MTFS scheduling.
#
#   ./crypto_trading/ops/make_launchd.sh install    # write plists + load
#   ./crypto_trading/ops/make_launchd.sh uninstall  # unload + remove plists
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PIPE="$REPO/crypto_trading/pipeline.sh"
LOGS="$REPO/crypto_trading/logs"
OUT="$REPO/crypto_trading/ops/launchd"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$OUT" "$LOGS"

daemon_plist() {  # $1 label-suffix  $2 log  $3... pipeline args
  local suffix="$1" log="$2"; shift 2
  local args=""
  for a in "$@"; do args="$args<string>$a</string>"; done
  cat > "$OUT/com.someopark.crypto.$suffix.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.someopark.crypto.$suffix</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$PIPE</string>$args</array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOGS/$log</string>
  <key>StandardErrorPath</key><string>$LOGS/$log</string>
</dict></plist>
EOF
}

daemon_plist poller     poller.log        poll --interval 10 --book-tickers KXBTCPERP,KXETHPERP,KXSOLPERP,KXXRPPERP,KXDOGEPERP,KXKSHIBPERP,KXBCHPERP,KXLTCPERP,KXLINKPERP,KXNEARPERP,KXSUIPERP,KXHYPEPERP,KXZECPERP
daemon_plist strips     strips.log        strips --interval 90
daemon_plist recorddemo recorder_demo.log record --env demo
daemon_plist idxrecord  idxrecord.log     idxrecord --interval 5
daemon_plist liqrecord  liqrecord.log     liqrecord

# daily top-up: 20:30 local ET = 00:30 UTC — clear of pairs pre_pipeline
# (19:15 ET) and MRPT/MTFS nightly (00:40+ ET)
cat > "$OUT/com.someopark.crypto.daily.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.someopark.crypto.daily</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$PIPE</string><string>daily</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>$LOGS/daily.log</string>
  <key>StandardErrorPath</key><string>$LOGS/daily.log</string>
</dict></plist>
EOF

case "${1:-install}" in
  install)
    for p in "$OUT"/com.someopark.crypto.*.plist; do
      cp "$p" "$LA/"
      launchctl unload "$LA/$(basename "$p")" 2>/dev/null || true
      launchctl load "$LA/$(basename "$p")"
      echo "loaded $(basename "$p")"
    done ;;
  uninstall)
    for p in "$OUT"/com.someopark.crypto.*.plist; do
      launchctl unload "$LA/$(basename "$p")" 2>/dev/null || true
      rm -f "$LA/$(basename "$p")"
      echo "removed $(basename "$p")"
    done ;;
  *) echo "usage: $0 install|uninstall"; exit 2 ;;
esac
