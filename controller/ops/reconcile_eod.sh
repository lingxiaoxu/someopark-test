#!/bin/bash
# controller/ops/reconcile_eod.sh — launchd 入口:日终持仓级对账(M5)。
#
# 背景(2026-08-27):reconcile_eod.py 从 2026-08-14 起 13 天没跑过 —— 它从来没有
# 任何调度,只被手工执行过四次。而面板 /reconcile 取"文件名最大"的那份、前端只读
# verdict 不读日期,于是 8/14 的 ok 一直冒充当天的绿灯。本脚本把它变成每交易日
# 自动出裁决;时效守卫在 controllerNav.ts 那侧(age_bdays/stale)。
#
# 为什么要 wrapper 而不是把命令塞进 plist:
#   1) 必须先 source 仓库根 .env(prices.py 无 POLYGON_API_KEY 直接 raise);
#   2) 休市日要跳过 —— 否则会给非交易日写一份 baseline 报告,顶掉上一份真裁决;
#   3) 幂等:当天已有**终态**裁决(ok/breach/partial)就不重跑,让 17:30 那趟
#      变成纯补跑(16:45 若因 controller 中断拿到 incomplete,补跑还有一次机会);
#   4) 日志按 ET 日切分。
#
# 只读输入、只写 controller/output/reconcile_{date}.json —— 遵守 controller 纪律 6。
# 退出码:0 = 出了裁决 / 休市跳过 / 已有终态;1 = 真失败(launchd 记 wrapper 日志)。
set -uo pipefail

REPO="/Users/xuling/code/someopark-test"
LOGDIR="$REPO/controller/logs"
OUTDIR="$REPO/controller/output"
CONDA_ENV="someopark_run"

cd "$REPO" || exit 1
mkdir -p "$LOGDIR"

ET_DATE="$(TZ=America/New_York date +%Y-%m-%d)"
LOG="$LOGDIR/reconcile_$(TZ=America/New_York date +%Y%m%d).log"
log() { echo "[$(TZ=America/New_York date +%H:%M:%S)] $*" >> "$LOG"; }

PY() { conda run -n "$CONDA_ENV" --no-capture-output python "$@"; }

# ── 休市闸门(与 daily_backtest.sh 同一套判据)────────────────────────────────
NYSE_STATUS=$(PY -c "
import sys
from datetime import datetime
try:
    import pytz, pandas_market_calendars as mcal
    nyc_date = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d')
    sched = mcal.get_calendar('NYSE').schedule(start_date=nyc_date, end_date=nyc_date)
    print('OPEN' if not sched.empty else 'CLOSED:' + nyc_date); sys.exit(0)
except ImportError: pass
except Exception as e: print('WARN:' + str(e)[:60], file=sys.stderr)
from datetime import date
t = date.today()
print('CLOSED:' + str(t) + '-weekend' if t.weekday() >= 5 else 'OPEN-WEEKDAY')
" 2>/dev/null) || NYSE_STATUS="OPEN-FALLBACK"
if [[ "$NYSE_STATUS" == CLOSED* ]]; then
    log "NYSE 休市 (${NYSE_STATUS#CLOSED:}) — skip reconcile, exit 0"
    exit 0
fi

# ── 幂等:当天已有终态裁决就不重跑 ───────────────────────────────────────────
REPORT="$OUTDIR/reconcile_${ET_DATE}.json"
if [ -f "$REPORT" ]; then
    V=$(PY -c "
import json,sys
try: print(json.load(open('$REPORT')).get('verdict',''))
except Exception: print('')
" 2>/dev/null | tr -d '[:space:]')
    case "$V" in
        ok|breach|partial)
            log "已有终态裁决 verdict=$V ($ET_DATE) — 幂等跳过, exit 0"
            exit 0 ;;
        *)
            log "已有非终态报告 verdict='${V:-none}' — 补跑覆盖" ;;
    esac
fi

# ── 环境:根 .env(POLYGON_API_KEY)+ 无缓冲 ─────────────────────────────────
set -a
# shellcheck disable=SC1091
[ -f "$REPO/.env" ] && . "$REPO/.env"
set +a
export PYTHONUNBUFFERED=1

log "══ RECONCILE EOD START ($ET_DATE, NYSE $NYSE_STATUS) ══"
conda run -n "$CONDA_ENV" --no-capture-output \
    python -m controller.reconcile_eod >> "$LOG" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
    log "══ RECONCILE EOD FAILED (rc=$rc) ══"
    exit 1
fi
FINAL=$(PY -c "
import json
try: print(json.load(open('$REPORT')).get('verdict','?'))
except Exception: print('NO-FILE')
" 2>/dev/null | tr -d '[:space:]')
log "══ RECONCILE EOD END (verdict=$FINAL) ══"
exit 0
