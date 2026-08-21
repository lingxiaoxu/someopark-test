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
export PATH="/opt/homebrew/bin:/Users/xuling/miniforge3/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# ── 调度守卫(只对 launchd 生效) ───────────────────────────────────────────
# com.someopark.vp.monthly 每天 12:33 拉起,这里只判一件事:**本月跑过没有**。
# 产出 wf_lgbm_YYYYMM.csv 已在 → exit 0;不在 → 跑。
#
# 为什么不按文件头写的"每月 1 号后第一个交易日"来判:
#   1. "第一个交易日"要交易日历,plist 表达不了,脚本里判又得多引一个数据源;
#      而本脚本是对历史窗口的回看复算,跑在哪天与结果无关,这个条件本就是惯例不是约束。
#   2. 产出存在性天然幂等 —— 当月第一次成功后,后续每天拉起都是毫秒级 exit 0。
#   3. **失败自动次日重试**,这条对本脚本尤其要紧: 撞上 pairs 夜跑会 exit 3,
#      若像 vp.refreeze 那样一个月只拉起一次,exit 3 就等于静默丢掉一整月健康检查。
#      每天拉起 + 产出守卫 = exit 3 自愈,不需要给它加"等夜跑收尾"的逻辑
#      (那是 quarterly/semiannual 那种数小时重活才需要的,本脚本 2-3 分钟)。
# 跳过分支故意用 echo 而不是 log(): log() 会 tee 出一个当日 vp_wf_lgbm_YYYYMMDD.log,
# 每天空跑一次就是每年 365 个只有一行的空日志文件。echo 进 launchd 的 StandardOutPath。
# 手动跑(不带这个环境变量)不受影响,照旧立即执行 —— 当月补跑/重跑走这条路。
if [ "${VP_SCHED_GUARD:-0}" = "1" ]; then
    # 周六 10:33 归三个数小时重活(refreeze / quarterly / semiannual,全面板 5.6GB)。
    # 本脚本虽轻也吃 1.3GB 面板,没必要去挤。跳过周六的唯一代价: 某月 1 号恰好是
    # 周六时顺延到 2 号 —— 对"月度回看健康检查"没有任何影响。
    if [ "$(date +%u)" -eq 6 ]; then
        echo "[$(date '+%F %H:%M:%S')] 守卫: 周六让位给 refreeze/quarterly/semiannual — 跳过"; exit 0
    fi
    if [ -f "$OUT_DIR/wf_lgbm_$YM.csv" ]; then
        echo "[$(date '+%F %H:%M:%S')] 守卫: $YM 已有产出 — 跳过"; exit 0
    fi
    echo "[$(date '+%F %H:%M:%S')] 守卫: 通过($YM 本月尚无产出)"
fi

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
new = pd.DataFrame([row])
if len(prev):
    # mode="a" 按 df 自己的列序写值、**不看已有 header** → 列序一变就静默错位:
    # 行数列数都对、不报错,只是每列的值挪了位。2026-08-17 promote 时
    # shadow_blend/shadow_rnn 就是这么写坏两行的(320ad27 已修)。
    # 这里按已有表头对齐;列集不一致就大声退,绝不静默追加。
    if set(new.columns) != set(prev.columns):
        raise SystemExit(f"health_log.csv 表头不匹配 — 新增 {sorted(set(new.columns)-set(prev.columns))} "
                         f"缺失 {sorted(set(prev.columns)-set(new.columns))};需人工迁移表头")
    new = new[prev.columns]
new.to_csv(p, mode="a", header=not os.path.exists(p), index=False)
print("HEALTH:", row)
alert = []
if pooled is not None and pooled < 0.15: alert.append(f"pooled_r2 {pooled:.4f} < 0.15")
if row["vs_prev"] is not None and row["vs_prev"] < -0.02: alert.append(f"环比 {row['vs_prev']:+.4f} < -0.02")
if row["neg_windows"] > 0: alert.append(f"{row['neg_windows']} 个窗为负")
print("ALERT: " + ("; ".join(alert) if alert else "无"))
PY
RC2=$?
# 这段汇总原来没接退出码: 它挂了(比如表头不匹配 SystemExit)脚本照样打印
# "END (exit=0)" 走人,当月就少了一行 health_log 而没人知道。
[ $RC2 -ne 0 ] && { log "!!! 健康汇总失败 (exit=$RC2) — WF 已跑完,产出见 $OUT_DIR"; exit 1; }

grep -E "^HEALTH:|^ALERT:" "$LOG" | tail -2 | while read -r l; do log "$l"; done
log "=== WF LGBM HEALTH END (exit=0) ==="
exit 0
