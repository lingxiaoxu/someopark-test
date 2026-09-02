#!/bin/bash
# pipeline_lock.sh — AEUS 管线互斥锁(2026-09-01)。
# 背景: daily_backtest.sh 在 V1→V2→恢复V1 期间会临时改写 selected_param_set.json 与 P0 缓存;
# aeus_pipeline.sh daily 的 smart_select 读+写同一批文件。两者由 openclaw 独立点火,
# 任何时间重叠都会让 daily 读到 V2 当生产、或与恢复动作互相覆盖。
# 锁让"谁后到谁等"成为代码事实,不再依赖 cron 时间表。
#   用法:  . "$SCRIPT_DIR/pipeline_lock.sh"; aeus_lock_acquire "<tag>" <max_wait_sec> || exit 3
#   语义:  mkdir 原子建锁目录(macOS 无 flock);锁目录在 logs/(gitignored);
#          持有者 PID 死亡 → 视为陈旧锁自动回收;
#          外层已持锁(AEUS_PIPELINE_LOCK_HELD=1,如 pipeline monthly 调用 daily_backtest.sh)→ 直接放行,不自锁死;
#          进程退出(任何原因)自动释放;已有 EXIT trap 会被保留并链式执行。
AEUS_LOCK_DIR="${AEUS_LOCK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/logs/.pipeline.lock}"

aeus_lock_release() {
    if [ "$(cat "$AEUS_LOCK_DIR/pid" 2>/dev/null)" = "$$" ]; then rm -rf "$AEUS_LOCK_DIR"; fi
}

aeus_lock_acquire() {
    local tag="${1:-unknown}" max="${2:-0}" waited=0 owner otag osince prev
    if [ "${AEUS_PIPELINE_LOCK_HELD:-0}" = "1" ]; then return 0; fi          # re-entrant
    mkdir -p "$(dirname "$AEUS_LOCK_DIR")"
    while ! mkdir "$AEUS_LOCK_DIR" 2>/dev/null; do
        owner=$(cat "$AEUS_LOCK_DIR/pid" 2>/dev/null); otag=$(cat "$AEUS_LOCK_DIR/tag" 2>/dev/null); osince=$(cat "$AEUS_LOCK_DIR/since" 2>/dev/null)
        if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then
            echo "[lock] stale lock: holder '$otag' pid $owner is dead — reclaiming" >&2
            rm -rf "$AEUS_LOCK_DIR"; continue
        fi
        if [ "$waited" -ge "$max" ]; then
            echo "[lock] BUSY: held by '$otag' (pid ${owner:-?}, since ${osince:-?}); '$tag' waited ${waited}s — giving up" >&2
            return 1
        fi
        [ "$waited" -eq 0 ] && echo "[lock] '$tag' waiting for '$otag' (pid ${owner:-?}, since ${osince:-?}) up to ${max}s…" >&2
        sleep 10; waited=$((waited+10))
    done
    echo $$ > "$AEUS_LOCK_DIR/pid"; date '+%Y-%m-%d %H:%M:%S' > "$AEUS_LOCK_DIR/since"; echo "$tag" > "$AEUS_LOCK_DIR/tag"
    export AEUS_PIPELINE_LOCK_HELD=1
    prev=$(trap -p EXIT | sed -E "s/^trap -- '(.*)' EXIT$/\1/")
    # shellcheck disable=SC2064
    trap "aeus_lock_release${prev:+; $prev}" EXIT
    [ "$waited" -gt 0 ] && echo "[lock] '$tag' acquired after ${waited}s" >&2
    return 0
}
