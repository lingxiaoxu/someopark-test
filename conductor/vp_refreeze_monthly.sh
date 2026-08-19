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
# 调度建议: 每月第一个交易日 **22:00 ET 之后**(当月首个日更已完成)。
#   这是重活(全量面板重训),会占数小时 CPU 与约 6GB 内存 ——
#   **务必避开 pairs 夜跑窗口(20:30-09:00 ET)**,否则两边抢内存可能被系统杀。
#   建议就放在月初的周末,或调度器确认 pipeline 已 COMPLETE 之后。
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
# 由 com.someopark.vp.refreeze 每月首周六拉起,尚未真正触发过,属未爆的同款雷。
export PATH="/opt/homebrew/bin:/Users/xuling/miniforge3/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# ── 跑批窗口保护: pairs 管道正在跑时拒绝启动(避免内存竞争导致双双被杀) ──
PIPE_LOG="$REPO_ROOT/pipeline_state/logs/pipeline_current.log"
if [ -z "$DRY" ] && [ -f "$PIPE_LOG" ]; then
    LAST_START=$(grep -n "PIPELINE START" "$PIPE_LOG" | tail -1 | cut -d: -f1)
    LAST_DONE=$(grep -n "PIPELINE COMPLETE" "$PIPE_LOG" | tail -1 | cut -d: -f1)
    if [ -n "$LAST_START" ] && { [ -z "$LAST_DONE" ] || [ "$LAST_DONE" -lt "$LAST_START" ]; }; then
        log "!!! pairs 管道正在运行 — 拒绝启动重冻结(改期或等 PIPELINE COMPLETE)"
        exit 3
    fi
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
