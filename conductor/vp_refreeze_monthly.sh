#!/bin/bash
# =============================================================================
# vp_refreeze_monthly.sh — VolumePrediction 月度重冻结(候选产出,不自动晋升)
# =============================================================================
# 为什么需要: 生产工件的 per-ticker 冻结统计随时间漂移(serve 的通用路径用
# 冻结日的 mu/sd,距冻结日越远偏差越大),health.model_drift_tradedays 会累积。
# 月度重冻结 = 用最新面板重训 → 产出**候选**工件 → 跑 OOS 对照。
#
# **绝不自动晋升**。本脚本只产出候选并打印对照结果;registry.production 指针
# 的切换永远人工执行(§7.6 红线)。
#
# 调度: **已 launchd 化** com.someopark.vp.refreeze —— 每周六 10:33 拉起,
#   由脚本内 VP_SCHED_GUARD 守卫判"本月候选是否已产出"(registry 键
#   lgbm_prod_YYYYMM*),已有就 exit 0。效果 = 每月首个无冲突周六跑一次,
#   失败自动下周六重试(原 Day1-7×Weekday6 一月一次的写法,exit 3 = 静默丢月)。
#   这是重活(全量面板重训 ~4.5h,占数小时 CPU 与约 6GB 内存)——
#   **务必避开 pairs 夜跑窗口(20:30-09:00 ET)**,否则两边抢内存可能被系统杀。
#
# 用法:
#     bash conductor/vp_refreeze_monthly.sh --dry-run   # 只报告将做什么(推荐先跑)
#     bash conductor/vp_refreeze_monthly.sh             # 真跑
#
# 退出码: 0=成功(候选已产出);1=失败(见日志);3=被跑批窗口保护拒绝启动。
# 环境: conda env `someopark_run` + 仓库根 `.env`。
# 日志: conductor/logs/vp_refreeze_YYYYMMDD.log
# =============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$REPO_ROOT/conductor/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/vp_refreeze_$(date +%Y%m%d).log"

DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="--dry-run"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

cd "$REPO_ROOT" || exit 1
set -a; . "$REPO_ROOT/.env"; set +a

# launchd/cron 不 source profile → PATH 无 miniforge → `nice … conda …` exit 127。
# 与 vp_shadow_daily.sh 同一处缺陷(那个已于 2026-08-18 17:33 真实触发);本脚本
# 由 com.someopark.vp.refreeze 拉起,尚未真正触发过,属未爆的同款雷。
export PATH="/opt/homebrew/bin:/Users/xuling/miniforge3/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# ── 调度守卫(只对 launchd 生效;2026-08-21 改) ──────────────────────────
# 原 plist 是 Day1-7×Weekday6 拼"首周六",**一个月只拉起一次** —— 撞上 pairs
# 夜跑 exit 3 就静默丢一整月候选(quarterly/semiannual 当初正是为此改成"等",
# 本脚本被漏掉)。现改为 plist 每周六 10:33 拉起,这里判三件事:
#   1. 本月候选已产出(registry 有 lgbm_prod_YYYYMM* 键) → 跳过。
#      真跑成功必写这个键(refreeze.py record_model,version=lgbm_prod_YYYYMMDD);
#      dry-run 不写,不会误挡真跑。现役 lgbm_prodv6_* 无下划线,前缀不误匹配。
#      效果 = 每月首个"无冲突周六"跑一次,失败自动下周六重试。
#   2. quarterly(1/4/7/10 月第 2 周六)/ semiannual(1/7 月第 3 周六)的
#      触发日 → 让位顺延(同为 10:33 起跑的数小时重活,撞车会抢内存)。
#      让位只可能发生在首周六失败后的重试路径上,顺延一周无实质影响。
#   3. 非周六(保险;plist 本就只在周六拉起)。
# 跳过分支用 echo 不用 log():log() 会 tee 出当日 vp_refreeze_YYYYMMDD.log,
# 每个已完成月的后续周六都会多一个单行日志文件。echo 进 launchd StandardOutPath。
# 手动跑(不带 VP_SCHED_GUARD)不受影响,照旧立即执行。
REGISTRY_JSON="$REPO_ROOT/VolumePrediction/outputs/registry/registry.json"
if [ "${VP_SCHED_GUARD:-0}" = "1" ]; then
    _now() { date '+%F %H:%M:%S'; }
    if [ "$(date +%u)" -ne 6 ]; then
        echo "[$(_now)] 守卫: 非周六 — 跳过"; exit 0
    fi
    if [ -f "$REGISTRY_JSON" ] && grep -q "\"lgbm_prod_$(date +%Y%m)" "$REGISTRY_JSON"; then
        echo "[$(_now)] 守卫: $(date +%Y%m) 候选已在 registry — 跳过"; exit 0
    fi
    _M=$(date +%-m); _OCC=$(( ($(date +%-d) - 1) / 7 + 1 ))
    case "$_M" in 1|4|7|10) [ "$_OCC" -eq 2 ] && {
        echo "[$(_now)] 守卫: 让位 quarterly($_M 月第 2 周六)— 顺延下周"; exit 0; };; esac
    case "$_M" in 1|7) [ "$_OCC" -eq 3 ] && {
        echo "[$(_now)] 守卫: 让位 semiannual($_M 月第 3 周六)— 顺延下周"; exit 0; };; esac
    echo "[$(_now)] 守卫: 通过($(date +%F),$(date +%Y%m) 尚无候选)"
fi

# ── 跑批窗口保护: pairs 管道正在跑时不能并行(内存竞争会双双被杀) ────────
# 判据: 最后一次 START 之后还没有对应的 COMPLETE。
PIPE_LOG="$REPO_ROOT/pipeline_state/logs/pipeline_current.log"
pairs_running() {
    [ -f "$PIPE_LOG" ] || return 1
    local s d
    s=$(grep -n "PIPELINE START" "$PIPE_LOG" | tail -1 | cut -d: -f1)
    d=$(grep -n "PIPELINE COMPLETE" "$PIPE_LOG" | tail -1 | cut -d: -f1)
    [ -n "$s" ] && { [ -z "$d" ] || [ "$d" -lt "$s" ]; }
}
if [ -z "$DRY" ] && pairs_running; then
    if [ "${VP_SCHED_GUARD:-0}" != "1" ]; then
        # 手动跑: 直接拒绝,让人改期 —— 原有约定。
        log "!!! pairs 管道正在运行 — 拒绝启动重冻结(改期或等 PIPELINE COMPLETE)"
        exit 3
    fi
    # launchd 跑: 与 vp_wf_quarterly_rnn.sh 同款 —— 周六 10:33 时周五夜跑本该
    # 已收尾(实测 09:11-09:26),这里只兜"跑晚了"的尾巴,最多等 2 小时
    # (12:33 还没完说明夜跑异常,该人工看;再等 4.5h 的活会顶进 20:30 夜跑窗)。
    # 等不到就 exit 3 —— 现在有下周六重试,不再是丢一个月。
    log "pairs 夜跑仍未 COMPLETE — 等它收尾(最多 2 小时)"
    WAITED=0
    while pairs_running && [ $WAITED -lt 7200 ]; do sleep 300; WAITED=$((WAITED + 300)); done
    if pairs_running; then
        log "!!! 等了 2 小时 pairs 仍未 COMPLETE — 本周放弃,下周六自动重试;需人工排查夜跑(悬空 START?)"
        exit 3
    fi
    log "pairs 已收尾(等了 $((WAITED / 60)) 分钟)— 继续"
fi

log "=== VP REFREEZE START ${DRY:-(real)} ==="
nice -n 15 conda run -n someopark_run --no-capture-output \
    python -m VolumePrediction.refreeze $DRY >>"$LOG" 2>&1
RC=$?
if [ $RC -ne 0 ]; then
    log "!!! refreeze 失败 (exit=$RC) — 生产工件未受影响(候选流程独立)"
    log "=== VP REFREEZE END (exit=1) ==="
    exit 1
fi

log "--- 候选已产出。晋升需人工: 复核 OOS 对照 → 修改 registry.production ---"
log "=== VP REFREEZE END (exit=0) ==="
exit 0
