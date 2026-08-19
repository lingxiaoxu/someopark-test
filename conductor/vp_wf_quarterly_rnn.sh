#!/bin/bash
# =============================================================================
# vp_wf_quarterly_rnn.sh — 季度关键对照: 窄集 RNN vs 现役 lgbm
# =============================================================================
# 用途: 这是决定 **promote / 回退** 的主证据。窄集(tech+fund1,14 特征)是 RNN
# 唯一有优势的特征域 —— 2026-08 首次复赛 RNN 0.2993 vs 现役全集 lgbm 0.1813,
# 胜 11/12 窗。季度复算,确认优势是否延续。
#
# **重活**: rnn 12 窗 × 5 seeds 约 6 小时(窄集面板 1.3GB,内存友好);
# lgbm 对照 12 窗只需数分钟。每窗独立进程,断点续跑(preds 已存在则跳过)。
#
# 调度: **已 launchd 化** com.someopark.vp.quarterly —— 每周六 10:33 拉起,
#   脚本内 VP_SCHED_GUARD 守卫只放行 1/4/7/10 月的**第 2 个**周六(第 1 个
#   周六 10:33 归 vp.refreeze,两个数小时重活不能撞);10:33 起跑 ~6 小时,
#   16:30 前收工,离 20:30 夜跑窗口还有余量。不必再靠台账记。
#   **必须避开 pairs 夜跑窗口** —— 脚本内置检测: 手动跑撞上直接拒绝(exit 3),
#   launchd 路径改为最多等 2 小时它收尾(错过一次就是错过一个季度)。
#   若中途被系统杀,直接重跑本脚本即可从断点续跑,不会重算已完成的窗。
#
# 用法:
#     bash conductor/vp_wf_quarterly_rnn.sh              # 跑 rnn + lgbm 对照
#     bash conductor/vp_wf_quarterly_rnn.sh --lgbm-only  # 只跑对照(几分钟)
#
# 输出: VolumePrediction/outputs/wf_quarterly/
#         wf_windows_q<YYYYQn>.csv    逐窗 R²(两模型)
#         summary.json                pooled R² + 分层(市值十分位)
#         verdict_<YYYYQn>.txt        判决摘要 ← **主要看这个**
# 检查: 打开 verdict_*.txt。三行结论: RNN pooled / lgbm pooled / RNN 胜几窗。
#       RNN 胜 ≥9/12 且 pooled 高出 ≥0.05 → 优势延续;
#       胜 ≤6/12 或差距 <0.02 → 优势消失,应考虑回退现役。
# 退出码: 0=成功;1=失败;3=pairs 管道运行中,已拒绝启动。
# =============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$REPO_ROOT/conductor/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/vp_wf_quarterly_$(date +%Y%m%d).log"
OUT_DIR="$REPO_ROOT/VolumePrediction/outputs/wf_quarterly"; mkdir -p "$OUT_DIR/preds"
Q="$(date +%Y)Q$(( ($(date +%-m) - 1) / 3 + 1 ))"
LGBM_ONLY=0; [ "${1:-}" = "--lgbm-only" ] && LGBM_ONLY=1

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
cd "$REPO_ROOT" || exit 1
set -a; . "$REPO_ROOT/.env"; set +a
# launchd/cron 不 source profile → PATH 无 miniforge → `conda` 找不到 → exit 127。
# 与 vp_shadow_daily.sh 同款(那个已于 2026-08-18 17:33 launchd 首次触发即挂)。
export PATH="/opt/homebrew/bin:/Users/xuling/miniforge3/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
PY="conda run -n someopark_run --no-capture-output python"

# ── 调度守卫(只对 launchd 生效) ───────────────────────────────────────────
# com.someopark.vp.quarterly 每个周六 10:33 拉起,由这里判断"是不是该跑的
# 那个周六",不是就 exit 0。为什么不用 plist 的 StartCalendarInterval 表达:
# "本月第几个周六"用 Day 枚举根本表达不了 —— 第 2 个周六可能落在 8-14 号
# 里的任意一天,而 8 号本身也可能是周日。
# (launchd 的 Day+Weekday 是 AND 还是 OR,2026-08-18 已用一次性测试 job 实测:
#  条件 Day=当天 + Weekday=周六、当天周二,到点 runs=0 未触发 → **是 AND**。
#  所以 vp.refreeze 那种 Day1-7×Weekday6 的写法本身没问题,只是表达不了这里
#  要的"第 N 个周六"。)
# 手动跑(不带这个环境变量)不受影响,照旧立即执行。
if [ "${VP_SCHED_GUARD:-0}" = "1" ]; then
    _M=$(date +%-m); _DOM=$(date +%-d); _OCC=$(( (_DOM - 1) / 7 + 1 ))
    if [ "$(date +%u)" -ne 6 ]; then log "守卫: 非周六 — 跳过"; exit 0; fi
    case "$_M" in 1|4|7|10) ;; *) log "守卫: $_M 月非季度首月 — 跳过"; exit 0;; esac
    # 第 2 个周六,不是第 1 个 —— 第 1 个周六 10:33 是 vp.refreeze(同样数小时
    # 重活),两个撞一起会抢内存。文件头写的"第一个周末"据此顺延一周。
    if [ "$_OCC" -ne 2 ]; then log "守卫: 本月第 $_OCC 个周六(要第 2 个)— 跳过"; exit 0; fi
    log "守卫: 通过($(date +%F),$_M 月第 2 个周六)"
fi

PIPE_LOG="$REPO_ROOT/pipeline_state/logs/pipeline_current.log"

# pairs 夜跑是否正在进行: 最后一次 START 之后还没有对应的 COMPLETE。
pairs_running() {
    [ -f "$PIPE_LOG" ] || return 1
    local s d
    s=$(grep -n "PIPELINE START" "$PIPE_LOG" | tail -1 | cut -d: -f1)
    d=$(grep -n "PIPELINE COMPLETE" "$PIPE_LOG" | tail -1 | cut -d: -f1)
    [ -n "$s" ] && { [ -z "$d" ] || [ "$d" -lt "$s" ]; }
}

if pairs_running; then
    if [ "${VP_SCHED_GUARD:-0}" != "1" ]; then
        # 手动跑: 直接拒绝,让人改期 —— 原有约定。
        log "!!! pairs 管道运行中 — 拒绝启动(6 小时重活会抢内存)"; exit 3
    fi
    # launchd 跑: 错过就是错过一个季度,不能一走了之。周六 10:33 起跑时
    # 周五夜跑本该已收尾(实测 09:11-09:26),这里只兜"跑晚了"的尾巴,
    # 最多等 2 小时(等到 12:33 还没完,说明夜跑异常,该人工看,再等下去
    # 6 小时的活会顶到 20:30 夜跑窗口里去)。
    log "pairs 夜跑仍未 COMPLETE — 等它收尾(最多 2 小时)"
    WAITED=0
    while pairs_running && [ $WAITED -lt 7200 ]; do sleep 300; WAITED=$((WAITED + 300)); done
    if pairs_running; then
        log "!!! 等了 2 小时 pairs 仍未 COMPLETE — 放弃本季,需人工排查夜跑"; exit 3
    fi
    log "pairs 已收尾(等了 $((WAITED / 60)) 分钟)— 继续"
fi

log "=== WF QUARTERLY START ($Q, lgbm_only=$LGBM_ONLY) ==="
MODELS="lgbm"; [ $LGBM_ONLY -eq 0 ] && MODELS="lgbm rnn"

for MK in $MODELS; do
  for W in 0 1 2 3 4 5 6 7 8 9 10 11; do
    F="$OUT_DIR/preds/q${Q}_${MK}_w${W}.parquet"
    [ -f "$F" ] && { log "skip $MK w$W (已完成)"; continue; }
    for TRY in 1 2; do
      log "--- $MK w$W try$TRY ---"
      nice -n 12 $PY -m VolumePrediction.evaluation.walkforward \
        --panel prod_v6f32n --models "$MK" --deep --seeds 5 \
        --tag "q$Q" --only-window "$W" --save-preds \
        --out-dir "$OUT_DIR" >>"$LOG" 2>&1
      [ -f "$F" ] && break
      log "!!! $MK w$W try$TRY 失败"
    done
  done
done

log "--- 聚合 ---"
nice -n 12 $PY -m VolumePrediction.evaluation.walkforward \
    --panel prod_v6f32n --models "$(echo $MODELS | tr ' ' ',')" \
    --tag "q$Q" --aggregate --out-dir "$OUT_DIR" >>"$LOG" 2>&1 || {
    log "!!! 聚合失败"; exit 1; }

nice -n 12 $PY - <<PY >"$OUT_DIR/verdict_$Q.txt" 2>>"$LOG"
import json
import pandas as pd
out, q = "$OUT_DIR", "$Q"
s = json.load(open(f"{out}/summary.json")).get("global_r2", {})
w = pd.read_csv(f"{out}/wf_windows_q{q}.csv").drop_duplicates(["model","window_id"], keep="last")
piv = w.pivot(index="window_id", columns="model", values="r2_eta")
print(f"=== 窄集季度复赛 {q} ===")
for m, v in sorted(s.items(), key=lambda x: -x[1]):
    print(f"  {m:6s} pooled OOS R2 = {v:.4f}")
if {"rnn","lgbm"} <= set(piv.columns):
    win = int((piv["rnn"] > piv["lgbm"]).sum()); n = int(piv[["rnn","lgbm"]].notna().all(axis=1).sum())
    gap = s.get("rnn", 0) - s.get("lgbm", 0)
    print(f"  RNN 胜 {win}/{n} 窗 | pooled 差距 {gap:+.4f}")
    if win >= 9 and gap >= 0.05:   verdict = "优势延续 — 维持/推进 RNN"
    elif win <= 6 or gap < 0.02:   verdict = "优势消失 — 考虑回退现役 lgbm"
    else:                          verdict = "边缘 — 再观察一季,勿动生产"
    print(f"  判决: {verdict}")
else:
    print("  (仅 lgbm 对照,无 RNN 结果)")
print("\n逐窗:"); print(piv.round(4).to_string())
PY
log "$(cat "$OUT_DIR/verdict_$Q.txt" | head -6)"
log "=== WF QUARTERLY END (exit=0) → $OUT_DIR/verdict_$Q.txt ==="
exit 0
