#!/bin/bash
# controller/ops/install_launchd.sh — 安装/卸载 controller 常驻估值影子循环守护。
# (与 prediction_market_macro/ops/install_launchd.sh 同款约定;
#  ~/Library/LaunchAgents 是既定落点。)
#
#   bash controller/ops/install_launchd.sh            # 安装 + 加载(含开机自启)
#   bash controller/ops/install_launchd.sh uninstall  # 卸载 + 删除
#   bash controller/ops/install_launchd.sh status     # 查看状态
#
# 安装会先停掉手工 nohup 起的循环 —— 两个实例同写 nav_latest/nav_stream 会互相踩。
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/launchd" && pwd)"
DST_DIR="$HOME/Library/LaunchAgents"
REPO="/Users/xuling/code/someopark-test"
PLIST=com.someopark.controllershadow

case "${1:-install}" in
  install)
    mkdir -p "$DST_DIR" "$REPO/controller/logs"
    chmod +x "$REPO/controller/ops/shadow_loop.sh"
    # 手工实例先让位(launchd 接管后由 KeepAlive 负责存活)
    if pgrep -f "controller.run_controller" >/dev/null 2>&1; then
      echo "stopping manually-started loop(s)..."
      pkill -f "controller.run_controller" || true
      sleep 3
    fi
    rm -rf "$REPO/controller/output/.shadow_loop.lock"
    cp "$SRC_DIR/$PLIST.plist" "$DST_DIR/$PLIST.plist"
    launchctl unload "$DST_DIR/$PLIST.plist" 2>/dev/null || true
    launchctl load "$DST_DIR/$PLIST.plist"
    echo "loaded $PLIST"
    ;;
  uninstall)
    launchctl unload "$DST_DIR/$PLIST.plist" 2>/dev/null || true
    rm -f "$DST_DIR/$PLIST.plist"
    rm -rf "$REPO/controller/output/.shadow_loop.lock"
    echo "removed $PLIST"
    ;;
  status)
    launchctl list | grep -E "PID|$PLIST" || echo "$PLIST not loaded"
    echo "--- processes ---"
    pgrep -fl "controller.run_controller" || echo "(no loop process)"
    echo "--- nav_latest mtime (存活判据;conda run 会缓冲 stdout) ---"
    ls -l "$REPO/controller/output/nav_latest.json" 2>/dev/null || echo "(none)"
    ;;
  *)
    echo "usage: $0 [install|uninstall|status]" >&2; exit 2 ;;
esac
