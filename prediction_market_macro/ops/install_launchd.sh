#!/bin/bash
# ops/install_launchd.sh — reproducible install/uninstall of the 5 macro launchd jobs
# (PLAN §8.3; 0-bis whitelist (b): ~/Library/LaunchAgents is the sanctioned target).
#
#   bash prediction_market_macro/ops/install_launchd.sh            # install + load
#   bash prediction_market_macro/ops/install_launchd.sh uninstall  # unload + remove
#   bash prediction_market_macro/ops/install_launchd.sh status     # launchctl view
#   bash prediction_market_macro/ops/install_launchd.sh install com.someopark.macrotick
#                                                               # update only tick
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/launchd" && pwd)"
DST_DIR="$HOME/Library/LaunchAgents"
PLISTS=(com.someopark.macrorefresh com.someopark.macrotick
        com.someopark.macrowatchdog com.someopark.macroweekly
        com.someopark.macroreplay)

ACTION="${1:-install}"
if [ "$#" -gt 0 ]; then shift; fi
if [ "$#" -gt 0 ]; then
  for requested in "$@"; do
    known=false
    for p in "${PLISTS[@]}"; do
      if [ "$requested" = "$p" ]; then known=true; break; fi
    done
    if [ "$known" != true ]; then
      echo "unknown macro launchd label: $requested" >&2
      exit 1
    fi
  done
  PLISTS=("$@")
fi

case "$ACTION" in
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
    echo "usage: $0 [install|uninstall|status] [com.someopark.macro<label> ...]" >&2
    exit 1
    ;;
esac
