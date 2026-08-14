#!/bin/bash
# controller/ops/shadow_loop.sh — launchd 入口:常驻估值影子循环。
#
# 为什么要 wrapper 而不是把命令塞进 plist:
#   1) 必须先 source 仓库根 .env(prices.py 无 POLYGON_API_KEY 直接 raise);
#   2) 单实例锁 —— 两个循环同时写 nav_latest/nav_stream 会互相踩,必须互斥;
#   3) 日志按 ET 日切分,沿用既有 shadow_YYYYMMDD.log 命名。
#
# launchd 侧 KeepAlive=true:任何退出都拉起(ThrottleInterval=30 防崩溃空转)。
# 进程内还有 tick 级 fail-not-die(scheduler.run),launchd 是最后一道兜底:
# 管的是"进程整个没了"(OOM/kill/重启),不是"某轮 tick 失败"。
set -uo pipefail

REPO="/Users/xuling/code/someopark-test"
LOCK="$REPO/controller/output/.shadow_loop.lock"
LOGDIR="$REPO/controller/logs"
CONDA_ENV="someopark_run"

cd "$REPO" || exit 1
mkdir -p "$LOGDIR" "$REPO/controller/output"

# ── 单实例锁(mkdir 原子;记 PID 以便识别陈旧锁)──────────────────────────────
if ! mkdir "$LOCK" 2>/dev/null; then
  old="$(cat "$LOCK/pid" 2>/dev/null || echo '')"
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
    echo "[$(date -u +%FT%TZ)] another shadow loop is alive (pid $old) — exit" \
      >> "$LOGDIR/shadow_launchd.log"
    exit 0                      # 正常退出;KeepAlive 会 30s 后再试,不算崩溃
  fi
  echo "[$(date -u +%FT%TZ)] stale lock (pid '${old:-none}') — reclaiming" \
    >> "$LOGDIR/shadow_launchd.log"
  rm -rf "$LOCK" && mkdir "$LOCK" || exit 1
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT INT TERM

# ── 环境:根 .env(POLYGON_API_KEY)+ 无缓冲(否则日志几小时不落地)────────────
set -a
# shellcheck disable=SC1091
[ -f "$REPO/.env" ] && . "$REPO/.env"
set +a
export PYTHONUNBUFFERED=1

LOG="$LOGDIR/shadow_$(TZ=America/New_York date +%Y%m%d).log"
echo "[$(date -u +%FT%TZ)] shadow loop starting (pid $$, wrapper)" >> "$LOG"
exec conda run -n "$CONDA_ENV" --no-capture-output \
  python -m controller.run_controller --interval 1m >> "$LOG" 2>&1
