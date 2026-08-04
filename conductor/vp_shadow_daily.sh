#!/bin/bash
# =============================================================================
# vp_shadow_daily.sh — VolumePrediction 每日影子跑批(日更 + RNN 候选影子 AB)
# =============================================================================
# 交给调度器的唯一入口。做两件事,都只写 VolumePrediction 自己的目录:
#   1) daily_update  — 拉当日 raw → 生成预测工件 → 四个 adapter advice → health
#   2) shadow_rnn    — RNN 候选工件跑真实服务路径,滚动 seq_tail,落当日预测;
#                      并按滞后口径(昨日预测 vs 今日实际)追加 AB 对照 CSV
#
# 调度建议: 每个交易日 **17:30 ET 之后**(收盘后 raw 已可得)。
#   与 pairs 夜跑不冲突 —— 实测日更 19 秒、零 Polygon 调用(走本地缓存)、
#   写 price_data/volume_prediction/ 与 VolumePrediction/outputs/,
#   而 pairs 管道写 price_data/week_*/ 与 trading_signals/,零路径重叠。
#
# 用法(可从任意目录调用,路径全部绝对解析):
#     bash conductor/vp_shadow_daily.sh
#     bash conductor/vp_shadow_daily.sh --no-rnn      # 只跑日更,跳过 RNN 影子
#
# 退出码: 0=全部成功;1=日更失败(需排查);2=日更成功但 RNN 影子失败(非致命)。
# 两步都非致命地互相隔离 —— RNN 影子只是候选评估,绝不能拖累生产日更。
#
# 环境: conda env `someopark_run` + 仓库根 `.env`(POLYGON_API_KEY / MONGO_URI)。
# 日志: conductor/logs/vp_shadow_YYYYMMDD.log(同日重跑追加,便于比对)。
# 幂等: daily_update 与 shadow_rnn 均按日戳幂等,同日重复运行不会二次写状态。
# =============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$REPO_ROOT/conductor/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/vp_shadow_$(date +%Y%m%d).log"

RUN_RNN=1
[ "${1:-}" = "--no-rnn" ] && RUN_RNN=0

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

cd "$REPO_ROOT" || exit 1
set -a; . "$REPO_ROOT/.env"; set +a

PY="conda run -n someopark_run --no-capture-output python"

log "=== VP SHADOW START (rnn=$RUN_RNN) ==="

# ── 1) 生产日更(致命: 失败即退出,不继续跑影子) ──────────────────────────
log "--- STEP 1: daily_update ---"
nice -n 10 $PY -m VolumePrediction.daily_update >>"$LOG" 2>&1
RC1=$?
if [ $RC1 -ne 0 ]; then
    log "!!! daily_update 失败 (exit=$RC1) — 需排查,跳过影子"
    log "=== VP SHADOW END (exit=1) ==="
    exit 1
fi
# 学习模型回退是静默失败的典型形态,单独抓一次
if grep -q "学习模型服务失败" "$LOG"; then
    log "!!! 警告: 学习模型服务失败并回退全 ma5 — 检查 registry/工件"
fi
log "--- STEP 1 OK ---"

# ── 2) RNN 候选影子 AB(非致命: 失败只影响候选评估) ──────────────────────
RC2=0
if [ $RUN_RNN -eq 1 ]; then
    log "--- STEP 2: shadow_rnn ---"
    nice -n 12 $PY -m VolumePrediction.shadow_rnn >>"$LOG" 2>&1
    RC2=$?
    if [ $RC2 -ne 0 ]; then
        # 断档(seq_tail 与目标日不连续)会走到这里 —— 这是有意的拒绝出数,
        # 补跑当日或 refreeze 后即可恢复,不影响生产日更。
        log "!!! shadow_rnn 失败 (exit=$RC2) — 非致命;若为'断档'需补跑或 refreeze"
    else
        log "--- STEP 2 OK ---"
    fi
fi

# ── 3) TCA fills 增量回捞(非致命;幂等,只捞新成交) ─────────────────────
# λ 需要 ≥60 笔正冲击样本才脱离纸面先验。AISS/SSRS 月度调仓 → 自然累积;
# 每日跑一次即可把新成交纳入,不必人工补跑。
log "--- STEP 3: tca_backfill ---"
nice -n 15 $PY -m VolumePrediction.tca_backfill >>"$LOG" 2>&1
RC3=$?
[ $RC3 -ne 0 ] && log "!!! tca_backfill 失败 (exit=$RC3) — 非致命,不影响预测工件"

log "=== VP SHADOW END (daily=0, rnn=$RC2, tca=${RC3:-0}) ==="
[ $RC2 -ne 0 ] && exit 2
exit 0
