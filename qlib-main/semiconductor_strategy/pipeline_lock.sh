#!/bin/bash
# pipeline_lock.sh — AISS 管线互斥锁(2026-09-07,自 electric_utilities_strategy 逐字节移植)。
# 背景: daily_backtest.sh 在 V1→V2→恢复V1 期间会临时改写 selected_param_set.json 与 P0 缓存;
# semiconductor_pipeline.sh daily 的 smart_select 读+写同一批文件。两者由 openclaw 独立点火,
# 任何时间重叠都会让 daily 读到 V2 当生产、或与恢复动作互相覆盖。
# 锁让"谁后到谁等"成为代码事实,不再依赖 cron 时间表。
#
# ★ AISS 不是预防性移植 —— 它已经中过招(2026-08-12,三家里唯一真正发生过的一次):
#     19:35:54  daily_backtest 把 selected_param_set.json 写成 pure_capex / v2
#     19:37:44  aiss-daily 点火(落在 V2 窗口内)
#     19:38:58  [SMART SELECT] Active: pure_capex (ver=v2)   ← 生产信号用了 V2
#     19:50:19  才恢复 V1
#   25 天日志里唯一一条 ver=v2,已固化进 trading_signals/aiss_daily_report_20260812_*.json
#   (该报告标注为已知污染,不重新生成 —— 重生成会毁证据并违反 PIT)。未造成下单
#   只因 AISS 月度调仓且当日非调仓日。
#
# ★ 为什么锁必须在仓库里,而不是让调度器排序:openclaw 的 payload_timeout_seconds
#   杀掉 agent turn 但**遗弃子 bash**,随后**重试** —— 同一天 18:37 那趟超时(3606s)后
#   19:39 重试,两个 daily_backtest 实例并发存活 19:40:11→19:50:38 共 10.5 分钟,
#   同时操作 selected_param_set.json;脚本自带的 COMPLETE 标记幂等门当时还没写出来,
#   拦不住。**任何调度器排序都防不住一个任务撞自己**,只有下面的 kill -0 陈旧锁回收能。
#   余量实测:AISS 最差 4.4 分钟(08-11),SSRS 为 0(每天 100% 结构性重叠),AEUS ~14 分钟。
#   用法:  . "$SCRIPT_DIR/pipeline_lock.sh"; aiss_lock_acquire "<tag>" <max_wait_sec> || exit 3
#   语义:  mkdir 原子建锁目录(macOS 无 flock);锁目录在 logs/(gitignored);
#          持有者 PID 死亡 → 视为陈旧锁自动回收;
#          外层已持锁(AISS_PIPELINE_LOCK_HELD=1,如 pipeline monthly 调用 daily_backtest.sh)→ 直接放行,不自锁死;
#          进程退出(任何原因)自动释放;已有 EXIT trap 会被保留并链式执行。
AISS_LOCK_DIR="${AISS_LOCK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/logs/.pipeline.lock}"

aiss_lock_release() {
    if [ "$(cat "$AISS_LOCK_DIR/pid" 2>/dev/null)" = "$$" ]; then rm -rf "$AISS_LOCK_DIR"; fi
}

aiss_lock_acquire() {
    local tag="${1:-unknown}" max="${2:-0}" waited=0 owner otag osince prev
    if [ "${AISS_PIPELINE_LOCK_HELD:-0}" = "1" ]; then return 0; fi          # re-entrant
    mkdir -p "$(dirname "$AISS_LOCK_DIR")"
    while ! mkdir "$AISS_LOCK_DIR" 2>/dev/null; do
        owner=$(cat "$AISS_LOCK_DIR/pid" 2>/dev/null); otag=$(cat "$AISS_LOCK_DIR/tag" 2>/dev/null); osince=$(cat "$AISS_LOCK_DIR/since" 2>/dev/null)
        if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then
            echo "[lock] stale lock: holder '$otag' pid $owner is dead — reclaiming" >&2
            rm -rf "$AISS_LOCK_DIR"; continue
        fi
        if [ "$waited" -ge "$max" ]; then
            echo "[lock] BUSY: held by '$otag' (pid ${owner:-?}, since ${osince:-?}); '$tag' waited ${waited}s — giving up" >&2
            return 1
        fi
        [ "$waited" -eq 0 ] && echo "[lock] '$tag' waiting for '$otag' (pid ${owner:-?}, since ${osince:-?}) up to ${max}s…" >&2
        sleep 10; waited=$((waited+10))
    done
    echo $$ > "$AISS_LOCK_DIR/pid"; date '+%Y-%m-%d %H:%M:%S' > "$AISS_LOCK_DIR/since"; echo "$tag" > "$AISS_LOCK_DIR/tag"
    export AISS_PIPELINE_LOCK_HELD=1
    prev=$(trap -p EXIT | sed -E "s/^trap -- '(.*)' EXIT$/\1/")
    # shellcheck disable=SC2064
    trap "aiss_lock_release${prev:+; $prev}" EXIT
    [ "$waited" -gt 0 ] && echo "[lock] '$tag' acquired after ${waited}s" >&2
    return 0
}
