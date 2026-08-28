#!/bin/bash
# trading_quantconnect/ops/qc_reconcile.sh — launchd 入口:M4 QC↔本地对账平面。
#
# 与 M5(controller/ops/reconcile_eod.sh)的分工:M5 是"账本 vs 独立重算持仓市值",
# 全程本地、不碰 QC;M4 是 QC↔本地那条腿。两者互不替代,报告也各写各的。
#
# 为什么一天跑两趟(16:20 / 23:15 ET),而不是像 M5 那样一趟:
#   两个平面的**数据可用时点根本不同**。
#   ① holdings/target 必须在 16:20 跑:someopark-daily-pipeline 21:30 一改 inventory,
#      exporter 就会推新 target,当时的"QC 持仓 vs 已推 target"现场就没了;
#   ③ equity 只能在 22:00 之后跑:官方 EOD json 由那趟 21:30 的 pipeline 写出,
#      16:20 时当天的官方净值压根还不存在。
#   所以两趟写**同一个报告文件**,由 merge_section() 负责"后一趟不许抹掉前一趟的真裁决"。
#
# 幂等判据不是"当天有没有报告",而是"三段里还有没有非终态的"——
# 这正是两趟制的要求:16:20 那趟必然留下 equity_check=pending,23:15 必须接着跑。
# 三段全终态后再跑就是纯空转,直接跳过。
#
# 只读输入(QC API + 五持仓文件 + state/),只写 reconcile/ 与 logs/ —— 不碰下单、
# 不碰 inventory、不碰 state/,防火墙方向不变。
# 退出码:0 = 出了裁决 / 休市跳过 / 已全终态;1 = 真失败;2 = breach(裁决本身是红的)。
set -uo pipefail

REPO="/Users/xuling/code/someopark-test"
QC="$REPO/trading_quantconnect"
LOGDIR="$QC/logs"
OUTDIR="$QC/reconcile"
CONDA="/Users/xuling/miniforge3/bin/conda"
CONDA_ENV="someopark_run"

cd "$QC" || exit 1
mkdir -p "$LOGDIR"

ET_DATE="$(TZ=America/New_York date +%Y-%m-%d)"
LOG="$LOGDIR/qc_reconcile_$(TZ=America/New_York date +%Y%m%d).log"
log() { echo "[$(TZ=America/New_York date +%H:%M:%S)] $*" >> "$LOG"; }

PY() { "$CONDA" run -n "$CONDA_ENV" --no-capture-output python "$@"; }

# ── 休市闸门 ────────────────────────────────────────────────────────────────
# 注意与 reconcile_eod.sh 的差别:那边找不到 pandas_market_calendars 会退到
# "周中即开盘";这里不退 —— 假期把 23:15 那趟放进来,官方 EOD 根本没更新,
# equity 平面只会拿昨天的数再算一遍 D,白写一份 pending 顶掉前一趟。宁可不跑。
NYSE_STATUS=$(PY -c "
import sys
from datetime import datetime
try:
    import pytz, pandas_market_calendars as mcal
except ImportError:
    print('ERR:pandas_market_calendars 缺失'); sys.exit(0)
try:
    d = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d')
    sched = mcal.get_calendar('NYSE').schedule(start_date=d, end_date=d)
    print('OPEN' if not sched.empty else 'CLOSED:' + d)
except Exception as e:
    print('ERR:' + str(e)[:80])
" 2>/dev/null | tr -d '\r')
case "$NYSE_STATUS" in
    CLOSED*) log "NYSE 休市 (${NYSE_STATUS#CLOSED:}) — skip, exit 0"; exit 0 ;;
    ERR*)    log "休市判定失败 (${NYSE_STATUS#ERR:}) — 不猜,skip, exit 1"; exit 1 ;;
    OPEN)    ;;
    *)       log "休市判定返回意外值 '$NYSE_STATUS' — skip, exit 1"; exit 1 ;;
esac

# ── 幂等:三段全终态才跳过 ──────────────────────────────────────────────────
REPORT="$OUTDIR/qc_reconcile_${ET_DATE}.json"
if [ -f "$REPORT" ]; then
    PENDING=$(PY -c "
import json
T = {'ok', 'breach', 'partial', 'baseline'}
try:
    d = json.load(open('$REPORT'))
    left = [k for k in ('holdings_check', 'target_check', 'equity_check')
            if (d.get(k) or {}).get('status') not in T]
    print(','.join(left) if left else 'NONE')
except Exception as e:
    print('READERR')
" 2>/dev/null | tr -d '[:space:]')
    case "$PENDING" in
        NONE)    log "三段已全终态 ($ET_DATE) — 幂等跳过, exit 0"; exit 0 ;;
        READERR) log "当天报告读不出来 — 当作没有,重跑覆盖" ;;
        *)       log "待补段: $PENDING — 继续跑" ;;
    esac
fi

export PYTHONUNBUFFERED=1
log "══ QC RECONCILE START ($ET_DATE) ══"
"$CONDA" run -n "$CONDA_ENV" --no-capture-output \
    python -m reconcile.qc_reconcile >> "$LOG" 2>&1
rc=$?
V=$(PY -c "
import json
try: print(json.load(open('$REPORT')).get('verdict', '?'))
except Exception: print('NO-FILE')
" 2>/dev/null | tr -d '[:space:]')
log "══ QC RECONCILE END (rc=$rc verdict=$V) ══"
# qc_reconcile.py 对 breach 返回 2;那不是"脚本失败",原样透传给 launchd。
[ $rc -eq 2 ] && exit 2
[ $rc -ne 0 ] && exit 1
exit 0
