#!/bin/bash
# ops/install_launchd.sh — reproducible install/uninstall of the 5 macro launchd jobs
# (PLAN §8.3; 0-bis whitelist (b): ~/Library/LaunchAgents is the sanctioned target).
#
#   bash prediction_market_macro/ops/install_launchd.sh            # install + load
#   bash prediction_market_macro/ops/install_launchd.sh uninstall  # unload + remove
#   bash prediction_market_macro/ops/install_launchd.sh status     # launchctl view
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/launchd" && pwd)"
DST_DIR="$HOME/Library/LaunchAgents"
PLISTS=(com.someopark.macrorefresh com.someopark.macrotick
        com.someopark.macrowatchdog com.someopark.macroweekly
        com.someopark.macroreplay)

case "${1:-install}" in
  install)
    mkdir -p "$DST_DIR" \
             "$(dirname "$SRC_DIR")/../data/logs"
    for p in "${PLISTS[@]}"; do
      cp "$SRC_DIR/$p.plist" "$DST_DIR/$p.plist"
      launchctl unload "$DST_DIR/$p.plist" 2>/dev/null || true
      launchctl load "$DST_DIR/$p.plist"
      echo "loaded $p"
    done
    ;;
  uninstall)
    for p in "${PLISTS[@]}"; do
      launchctl unload "$DST_DIR/$p.plist" 2>/dev/null || true
      rm -f "$DST_DIR/$p.plist"
      echo "removed $p"
    done
    ;;
  status)
    launchctl list | grep -E "someopark\.macro" || echo "none loaded"
    ;;
  *)
    echo "usage: $0 [install|uninstall|status]" >&2
    exit 1
    ;;
esac
