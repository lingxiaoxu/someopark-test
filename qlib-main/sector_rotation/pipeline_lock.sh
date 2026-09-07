#!/bin/bash
# pipeline_lock.sh — SSRS 管线互斥锁(2026-09-07,自 electric_utilities_strategy 逐字节移植)。
# 背景: daily_backtest.sh 在 V1→V2→恢复V1 期间会临时改写 selected_param_set.json 与 P0 缓存;
# sector_rotation_pipeline.sh daily 的 smart_select 读+写同一批文件。两者由 openclaw 独立点火,
# 任何时间重叠都会让 daily 读到 V2 当生产、或与恢复动作互相覆盖。
# 锁让"谁后到谁等"成为代码事实,不再依赖 cron 时间表。
#
# ★ SSRS 的重叠是三家里最严重的:**每天 100% 结构性重叠,零余量**。
#   daily_backtest 16:40 起跑(实测 39.5-117.2 分钟,均值 59.9),而 daily 信号的 cron
#   槽是 17:40 —— 必然落在回测窗内;Step 2 与 Step 6 各有一个 V2 窗口
#   (实测 17:25-17:29 切 V2、17:41-17:49 才恢复 V1),正好骑在 17:40 上。
#   至今没出事只因 openclaw 的 cron tick 会阻塞到批次排空,把 daily 推到 17:42-17:50
#   ——那是实现副作用,不是保证:同一机制已两次把两个策略任务放到同一秒启动
#   (2026-08-10、2026-08-12),AISS 更已于 08-12 真的用 V2 出了生产信号。
#
# ★ 为什么锁必须在仓库里,而不是让调度器排序:openclaw 的 payload_timeout_seconds
#   杀掉 agent turn 但**遗弃子 bash**,随后**重试** —— 一个任务会和自己并发。
#   任何调度器排序都防不住这种自撞,只有下面的 kill -0 陈旧锁回收能。
#   SSRS 实测最长 117.2 分钟 vs 7200s 超时 —— 只剩 3 分钟就会武装同样的重试重叠。
#   用法:  . "$SCRIPT_DIR/pipeline_lock.sh"; ssrs_lock_acquire "<tag>" <max_wait_sec> || exit 3
#   语义:  mkdir 原子建锁目录(macOS 无 flock);锁目录在 logs/(gitignored);
#          持有者 PID 死亡 → 视为陈旧锁自动回收;
#          外层已持锁(SSRS_PIPELINE_LOCK_HELD=1,如 pipeline monthly 调用 daily_backtest.sh)→ 直接放行,不自锁死;
#          进程退出(任何原因)自动释放;已有 EXIT trap 会被保留并链式执行。
SSRS_LOCK_DIR="${SSRS_LOCK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/logs/.pipeline.lock}"

ssrs_lock_release() {
    if [ "$(cat "$SSRS_LOCK_DIR/pid" 2>/dev/null)" = "$$" ]; then rm -rf "$SSRS_LOCK_DIR"; fi
}

ssrs_lock_acquire() {
    local tag="${1:-unknown}" max="${2:-0}" waited=0 owner otag osince prev
    if [ "${SSRS_PIPELINE_LOCK_HELD:-0}" = "1" ]; then return 0; fi          # re-entrant
    mkdir -p "$(dirname "$SSRS_LOCK_DIR")"
    while ! mkdir "$SSRS_LOCK_DIR" 2>/dev/null; do
        owner=$(cat "$SSRS_LOCK_DIR/pid" 2>/dev/null); otag=$(cat "$SSRS_LOCK_DIR/tag" 2>/dev/null); osince=$(cat "$SSRS_LOCK_DIR/since" 2>/dev/null)
        if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then
            echo "[lock] stale lock: holder '$otag' pid $owner is dead — reclaiming" >&2
            rm -rf "$SSRS_LOCK_DIR"; continue
        fi
        if [ "$waited" -ge "$max" ]; then
            echo "[lock] BUSY: held by '$otag' (pid ${owner:-?}, since ${osince:-?}); '$tag' waited ${waited}s — giving up" >&2
            return 1
        fi
        [ "$waited" -eq 0 ] && echo "[lock] '$tag' waiting for '$otag' (pid ${owner:-?}, since ${osince:-?}) up to ${max}s…" >&2
        sleep 10; waited=$((waited+10))
    done
    echo $$ > "$SSRS_LOCK_DIR/pid"; date '+%Y-%m-%d %H:%M:%S' > "$SSRS_LOCK_DIR/since"; echo "$tag" > "$SSRS_LOCK_DIR/tag"
    export SSRS_PIPELINE_LOCK_HELD=1
    prev=$(trap -p EXIT | sed -E "s/^trap -- '(.*)' EXIT$/\1/")
    # shellcheck disable=SC2064
    trap "ssrs_lock_release${prev:+; $prev}" EXIT
    [ "$waited" -gt 0 ] && echo "[lock] '$tag' acquired after ${waited}s" >&2
    return 0
}
