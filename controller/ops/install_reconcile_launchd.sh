#!/bin/bash
# controller/ops/install_reconcile_launchd.sh — 安装/卸载日终持仓级对账定时任务。
#
#   bash controller/ops/install_reconcile_launchd.sh            # 安装 + 加载
#   bash controller/ops/install_reconcile_launchd.sh uninstall  # 卸载 + 删除
#   bash controller/ops/install_reconcile_launchd.sh status     # 查看状态
#   bash controller/ops/install_reconcile_launchd.sh runnow     # 立刻跑一次(不改调度)
#
# 刻意与 install_launchd.sh(影子循环)分开:那个脚本安装时会 pkill
# controller.run_controller,对账任务完全不该碰常驻循环。一次只装一个守护。
#
# 这是**定时任务**不是常驻进程:RunAtLoad=false、无 KeepAlive,
# 所以 `launchctl list` 里平时看不到 PID(只有跑的那一刻有),不代表没装上。
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/launchd" && pwd)"
DST_DIR="$HOME/Library/LaunchAgents"
REPO="/Users/xuling/code/someopark-test"
PLIST=com.someopark.controllerreconcile

case "${1:-install}" in
  install)
    mkdir -p "$DST_DIR" "$REPO/controller/logs"
    chmod +x "$REPO/controller/ops/reconcile_eod.sh"
    plutil -lint "$SRC_DIR/$PLIST.plist"
    cp "$SRC_DIR/$PLIST.plist" "$DST_DIR/$PLIST.plist"
    launchctl unload "$DST_DIR/$PLIST.plist" 2>/dev/null || true
    launchctl load "$DST_DIR/$PLIST.plist"
    echo "loaded $PLIST — 16:45 / 17:30 ET(休市与幂等由 wrapper 判)"
    ;;
  uninstall)
    launchctl unload "$DST_DIR/$PLIST.plist" 2>/dev/null || true
    rm -f "$DST_DIR/$PLIST.plist"
    echo "removed $PLIST"
    ;;
  status)
    if [ -f "$DST_DIR/$PLIST.plist" ]; then
      echo "plist installed: $DST_DIR/$PLIST.plist"
    else
      echo "plist NOT installed"
    fi
    launchctl list | grep -E "$PLIST" \
      || echo "(定时任务未在运行中 —— 正常,只在 16:45/17:30 起来)"
    echo "--- 最近报告 ---"
    ls -t "$REPO/controller/output"/reconcile_*.json 2>/dev/null | head -3 \
      || echo "(none)"
    echo "--- 最近 wrapper 日志 ---"
    ls -t "$REPO/controller/logs"/reconcile_*.log 2>/dev/null | head -3 \
      || echo "(none)"
    ;;
  runnow)
    exec "$REPO/controller/ops/reconcile_eod.sh"
    ;;
  *)
    echo "usage: $0 [install|uninstall|status|runnow]" >&2; exit 2 ;;
esac
