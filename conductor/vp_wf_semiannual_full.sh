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
# 调度: 每半年一次(建议 1 月与 7 月的第一个长周末),**必须整段避开夜跑**。
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
# 退出码: 0=成功;1=聚合失败;3=pairs 管道运行中,已拒绝启动。
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
PY="conda run -n someopark_run --no-capture-output python"

PIPE_LOG="$REPO_ROOT/pipeline_state/logs/pipeline_current.log"
if [ -f "$PIPE_LOG" ]; then
    S=$(grep -n "PIPELINE START" "$PIPE_LOG" | tail -1 | cut -d: -f1)
    D=$(grep -n "PIPELINE COMPLETE" "$PIPE_LOG" | tail -1 | cut -d: -f1)
    if [ -n "$S" ] && { [ -z "$D" ] || [ "$D" -lt "$S" ]; }; then
        log "!!! pairs 管道运行中 — 拒绝启动(数十小时重活)"; exit 3
    fi
fi

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
