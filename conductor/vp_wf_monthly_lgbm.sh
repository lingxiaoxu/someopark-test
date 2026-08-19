#!/bin/bash
# =============================================================================
# vp_wf_monthly_lgbm.sh — 现役模型(lgbm)月度 walk-forward 健康检查
# =============================================================================
# 用途: 12 窗滚动 OOS 复算现役模型成绩,早发现退化。**极轻**(每窗约 6 秒,
# 全程 2-3 分钟),可以放心月度跑。不改任何生产工件,只产出对照 CSV。
#
# 调度: 每月 1 号之后的第一个交易日,任意时段(建议与月度重冻结错开)。
# 与 pairs 夜跑并行安全: 窄面板 1.3GB / 全面板 5.6GB,lgbm 不吃 GPU;
# 但仍建议避开 20:30-09:00 ET 的夜跑窗口。
#
# 用法:
#     bash conductor/vp_wf_monthly_lgbm.sh
#
# 输出: VolumePrediction/outputs/wf_health/wf_lgbm_YYYYMM.csv  (逐窗 R²)
#       VolumePrediction/outputs/wf_health/health_log.csv      (逐月一行,趋势)
# 检查: 看 health_log.csv 最后一行的 pooled_r2 与 vs_prev。
#       pooled_r2 跌破 0.15 或单月降幅 > 0.02 → 需要人工看一眼(可能该 refreeze)。
# 退出码: 0=成功;1=失败(见日志);3=pairs 管道运行中,已跳过。
# =============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$REPO_ROOT/conductor/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/vp_wf_lgbm_$(date +%Y%m%d).log"
OUT_DIR="$REPO_ROOT/VolumePrediction/outputs/wf_health"; mkdir -p "$OUT_DIR"
YM=$(date +%Y%m)

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
cd "$REPO_ROOT" || exit 1
set -a; . "$REPO_ROOT/.env"; set +a

# launchd/cron 不 source profile → PATH 无 miniforge → `conda` 找不到 → exit 127。
# 与 vp_shadow_daily.sh 同款(那个已于 2026-08-18 17:33 launchd 首次触发即挂)。
# 本脚本目前靠台账手动跑,PATH 天然是好的;这行是提前拆雷,将来上调度器即可直接用。
export PATH="/opt/homebrew/bin:/Users/xuling/miniforge3/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

PIPE_LOG="$REPO_ROOT/pipeline_state/logs/pipeline_current.log"
if [ -f "$PIPE_LOG" ]; then
    S=$(grep -n "PIPELINE START" "$PIPE_LOG" | tail -1 | cut -d: -f1)
    D=$(grep -n "PIPELINE COMPLETE" "$PIPE_LOG" | tail -1 | cut -d: -f1)
    if [ -n "$S" ] && { [ -z "$D" ] || [ "$D" -lt "$S" ]; }; then
        log "!!! pairs 管道运行中 — 跳过本次(下月或手动补跑)"; exit 3
    fi
fi

log "=== WF LGBM HEALTH START ($YM) ==="
nice -n 15 conda run -n someopark_run --no-capture-output python -m \
    VolumePrediction.evaluation.walkforward \
    --panel prod_v6f32 --models lgbm --deep --seeds 1 \
    --tag "health_$YM" --out-dir "$OUT_DIR" >>"$LOG" 2>&1
RC=$?
[ $RC -ne 0 ] && { log "!!! walk-forward 失败 (exit=$RC)"; exit 1; }

nice -n 15 conda run -n someopark_run --no-capture-output python - <<PY >>"$LOG" 2>&1
import json, os
import pandas as pd
out = "$OUT_DIR"; ym = "$YM"
w = pd.read_csv(f"{out}/wf_windows_health_{ym}.csv").drop_duplicates("window_id", keep="last")
w.to_csv(f"{out}/wf_lgbm_{ym}.csv", index=False)
pooled = json.load(open(f"{out}/summary.json")).get("global_r2", {}).get("lgbm")
row = {"ym": ym, "n_windows": len(w), "pooled_r2": pooled,
       "mean_r2": round(float(w.r2_eta.mean()), 6),
       "min_r2": round(float(w.r2_eta.min()), 6),
       "neg_windows": int((w.r2_eta < 0).sum())}
p = f"{out}/health_log.csv"
prev = pd.read_csv(p) if os.path.exists(p) else pd.DataFrame()
row["vs_prev"] = (round(row["pooled_r2"] - float(prev.iloc[-1]["pooled_r2"]), 6)
                  if len(prev) and pooled is not None else None)
pd.DataFrame([row]).to_csv(p, mode="a", header=not os.path.exists(p), index=False)
print("HEALTH:", row)
alert = []
if pooled is not None and pooled < 0.15: alert.append(f"pooled_r2 {pooled:.4f} < 0.15")
if row["vs_prev"] is not None and row["vs_prev"] < -0.02: alert.append(f"环比 {row['vs_prev']:+.4f} < -0.02")
if row["neg_windows"] > 0: alert.append(f"{row['neg_windows']} 个窗为负")
print("ALERT: " + ("; ".join(alert) if alert else "无"))
PY

grep -E "^HEALTH:|^ALERT:" "$LOG" | tail -2 | while read -r l; do log "$l"; done
log "=== WF LGBM HEALTH END (exit=0) ==="
exit 0
