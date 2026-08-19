#!/bin/bash
# =============================================================================
# vp_wf_semiannual_full.sh — 半年度全模型总表(9-10 个模型 × 12 窗)
# =============================================================================
# 用途: 模型排序的完整复核,论文与年度审阅用。不是决策脚本 ——
# promote/回退看的是季度窄集对照(vp_wf_quarterly_rnn.sh),这个是全景。
#
# **最重活**: 2026-08 首次跑用了约 36 小时(轻模型 6 个几分钟,
# 三个 torch 模型各 12 窗才是大头)。每窗独立进程 + 断点续跑,
# 被杀最多损失一窗;重跑本脚本即从断点继续。
#
# 调度: **已 launchd 化** com.someopark.vp.semiannual —— 每周六 10:33 拉起,
#   守卫只放行 1 月 / 7 月的**第 3 个**周六(第 1 个归 vp.refreeze、第 2 个归
#   vp.quarterly,最重的排最后)。36 小时的活整段避开夜跑做不到,改为
#   **逐窗避让**: 每窗开跑前若在 20:30-09:30 ET、或 pairs 尚未 COMPLETE,就先睡
#   (见 wait_out_night),
#   所以总墙钟会跨几天 —— 半年一次的排序表不赶时间。中途被杀重跑即断点续跑。
#   分两阶段: 轻模型先出(几分钟),深模型慢慢跑。中途可随时中断。
#
# 用法:
#     bash conductor/vp_wf_semiannual_full.sh            # 全部(轻+深)
#     bash conductor/vp_wf_semiannual_full.sh --light    # 只跑 6 个轻模型(几分钟)
#
# 输出: VolumePrediction/outputs/wf_semiannual/
#         wf_windows_h<YYYYHn>.csv       逐窗 × 逐模型
#         wf_stratified_h<YYYYHn>_agg.csv 分层(行业 / 市值十分位)
#         summary.json                    pooled R² 全模型
#         ranking_<YYYYHn>.txt            排序表 ← **主要看这个**
# 检查: ranking_*.txt 给出全模型 pooled R² 降序表 + 与上期对比。
#       现役 lgbm 若跌出前三 → 该重新评估模型选择。
# 退出码: 0=成功;1=聚合失败;3=pairs 管道运行中已拒绝启动(**仅手动跑**;
#         launchd 路径改为等它收尾,错过一次就是错过半年)。
# =============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$REPO_ROOT/conductor/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/vp_wf_semiannual_$(date +%Y%m%d).log"
OUT_DIR="$REPO_ROOT/VolumePrediction/outputs/wf_semiannual"; mkdir -p "$OUT_DIR/preds"
H="$(date +%Y)H$(( ($(date +%-m) - 1) / 6 + 1 ))"
LIGHT_ONLY=0; [ "${1:-}" = "--light" ] && LIGHT_ONLY=1

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
cd "$REPO_ROOT" || exit 1
set -a; . "$REPO_ROOT/.env"; set +a
# launchd/cron 不 source profile → PATH 无 miniforge → `conda` 找不到 → exit 127。
# 与 vp_shadow_daily.sh 同款(那个已于 2026-08-18 17:33 launchd 首次触发即挂)。
export PATH="/opt/homebrew/bin:/Users/xuling/miniforge3/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
PY="conda run -n someopark_run --no-capture-output python"

# ── 调度守卫(只对 launchd 生效) ───────────────────────────────────────────
# com.someopark.vp.semiannual 每个周六 10:33 拉起,由这里判断是不是该跑的那个
# 周六。判日期放脚本里而不是 plist:"第几个周六"用 Day 枚举表达不了(理由与
# 实测结论见 vp_wf_quarterly_rnn.sh 同一段)。手动跑(无此环境变量)不受影响。
if [ "${VP_SCHED_GUARD:-0}" = "1" ]; then
    _M=$(date +%-m); _DOM=$(date +%-d); _OCC=$(( (_DOM - 1) / 7 + 1 ))
    if [ "$(date +%u)" -ne 6 ]; then log "守卫: 非周六 — 跳过"; exit 0; fi
    case "$_M" in 1|7) ;; *) log "守卫: $_M 月非半年首月 — 跳过"; exit 0;; esac
    # 第 3 个周六: 第 1 个是 vp.refreeze,第 2 个是 vp.quarterly,这个最重的排最后。
    if [ "$_OCC" -ne 3 ]; then log "守卫: 本月第 $_OCC 个周六(要第 3 个)— 跳过"; exit 0; fi
    log "守卫: 通过($(date +%F),$_M 月第 3 个周六)"
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

# 手动跑: 撞上夜跑直接拒绝(exit 3),让人改期 —— 这是原有约定。
# launchd 跑: **不能** exit 3 —— 错过就是错过半年。改为等它收尾(下面
# wait_out_night 同时管时钟与 pairs 状态),等待本身是无害的。
if pairs_running && [ "${VP_SCHED_GUARD:-0}" != "1" ]; then
    log "!!! pairs 管道运行中 — 拒绝启动(数十小时重活)"; exit 3
fi

# ── 夜跑窗口逐窗避让 ──────────────────────────────────────────────────────
# 文件头要求"必须整段避开夜跑",但本脚本是 36 小时的活,整段避开物理上做不到。
# 好在每个窗是独立进程(被杀最多损失一窗),所以改成**逐窗**避让: 每窗开跑前
# 若撞上夜跑就睡,任一窗都不会与 pairs 夜跑抢内存。代价只是总墙钟拉长到跨
# 几天,而半年一次的排序表不赶时间。
# 两个条件取并集,缺一不可:
#   1) 时钟 20:30-09:30 —— 09:30 而不是文件头写的 09:00: 实测 pairs 收尾在
#      09:11-09:26 之间浮动,09:00 放行会正好撞上尾巴。
#   2) pairs_running 为真 —— 夜跑延误跑过 09:30 时,光看时钟会误放行。
# 想强跑(人工盯着的补跑): VP_NO_NIGHT_WAIT=1 bash conductor/vp_wf_semiannual_full.sh
wait_out_night() {
    [ "${VP_NO_NIGHT_WAIT:-0}" = "1" ] && return 0
    local said=0 hm
    while :; do
        hm=$((10#$(date +%H%M)))
        if [ $hm -ge 2030 ] || [ $hm -lt 930 ]; then
            [ $said -eq 0 ] && { log "夜跑窗口($(date '+%H:%M'))— 暂停,09:30 后续跑"; said=1; }
        elif pairs_running; then
            [ $said -eq 0 ] && { log "pairs 夜跑仍未 COMPLETE — 暂停等它收尾"; said=1; }
        else
            break
        fi
        sleep 300
    done
}

LIGHT="ols lassocv pcr5 pls5 fwdstep adaboost"
DEEP="lgbm nn2 nn rnn"
ALL="$LIGHT"; [ $LIGHT_ONLY -eq 0 ] && ALL="$LIGHT $DEEP"

log "=== WF SEMIANNUAL START ($H, light_only=$LIGHT_ONLY) ==="
for MK in $ALL; do
  DEEP_FLAG=""; case " $DEEP " in *" $MK "*) DEEP_FLAG="--deep";; esac
  SEEDS=1; case "$MK" in nn|nn2|rnn) SEEDS=5;; esac
  for W in 0 1 2 3 4 5 6 7 8 9 10 11; do
    F="$OUT_DIR/preds/h${H}_${MK}_w${W}.parquet"
    [ -f "$F" ] && continue
    wait_out_night
    for TRY in 1 2; do
      log "--- $MK w$W try$TRY ---"
      nice -n 14 $PY -m VolumePrediction.evaluation.walkforward \
        --panel prod_v6f32 --models "$MK" $DEEP_FLAG --seeds $SEEDS \
        --tag "h$H" --only-window "$W" --save-preds \
        --out-dir "$OUT_DIR" >>"$LOG" 2>&1
      [ -f "$F" ] && break
      log "!!! $MK w$W try$TRY 失败"
    done
  done
  log "--- $MK 完成 ---"
done

log "--- 聚合 ---"
wait_out_night   # 聚合要把 10 模型 × 12 窗的 preds 一起读进来,同样别撞夜跑
nice -n 14 $PY -m VolumePrediction.evaluation.walkforward \
    --panel prod_v6f32 --models "$(echo $ALL | tr ' ' ',')" \
    --tag "h$H" --aggregate --out-dir "$OUT_DIR" >>"$LOG" 2>&1 || {
    log "!!! 聚合失败"; exit 1; }

nice -n 14 $PY - <<PY >"$OUT_DIR/ranking_$H.txt" 2>>"$LOG"
import glob, json, os
import pandas as pd
out, h = "$OUT_DIR", "$H"
s = json.load(open(f"{out}/summary.json")).get("global_r2", {})
print(f"=== 全模型总表 {h} (12 窗 pooled OOS R2) ===")
rank = sorted(s.items(), key=lambda x: -x[1])
for i, (m, v) in enumerate(rank, 1):
    mark = "  ← 现役" if m == "lgbm" else ""
    print(f"  {i:2d}. {m:9s} {v:8.4f}{mark}")
prev = sorted(glob.glob(f"{out}/ranking_*.txt"))
prev = [p for p in prev if os.path.basename(p) != f"ranking_{h}.txt"]
if prev:
    print(f"\n(上期: {os.path.basename(prev[-1])} — 人工对比排序变化)")
lg = [i for i,(m,_) in enumerate(rank,1) if m=="lgbm"]
if lg and lg[0] > 3:
    print(f"\n!!! 现役 lgbm 排名第 {lg[0]} — 已跌出前三,应重新评估模型选择")
PY
log "$(head -14 "$OUT_DIR/ranking_$H.txt")"
log "=== WF SEMIANNUAL END (exit=0) → $OUT_DIR/ranking_$H.txt ==="
exit 0
