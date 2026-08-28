#!/bin/bash
# trading_quantconnect/ops/qc_reconcile.sh — launchd 入口:M4 QC↔本地对账平面。
#
# 与 M5(controller/ops/reconcile_eod.sh)的分工:M5 是"账本 vs 独立重算持仓市值",
# 全程本地、不碰 QC;M4 是 QC↔本地那条腿。两者互不替代,报告也各写各的。
#
# 为什么一天跑好几趟(11:00 / 13:30 / 16:20 / 23:15 / 23:50 ET),而不是像 M5 那样一趟:
#   三个平面的**数据可用时点根本不同**,而且没有任何一个时刻能同时凑齐。
#   ① holdings/target 必须在 16:20 跑:someopark-daily-pipeline 21:30 一改 inventory,
#      exporter 就会推新 target,当时的"QC 持仓 vs 已推 target"现场就没了;
#   ③ equity 要 P 和 Q 同时在手,而两者的窗口不重叠:
#        Q(QC 收盘净值)只在 [16:00 D, 09:30 D+1) 可观测 —— 开盘后读到的是新一天;
#        P(D 日官方 EOD)由 D 日 21:30 那趟 pipeline 算,实测常到 D+1 ~10:15 才落地。
#      所以 16:20 那趟的正事是把 Q **存档**(close_snapshot),次日 11:00/13:30 用
#      --settle 拿存档补算 —— 那条路径一行 QC API 都不调。
#      23:15/23:50 仍留着:P 若当晚就落地,当晚出裁决更好。
#   所有趟写**同一个报告文件**,由 merge_section() 负责"后一趟不许抹掉前一趟的真裁决"。
#
# 幂等判据不是"当天有没有报告",而是"三段里还有没有非终态的 + 有没有欠账"——
# 前半句是两趟制的要求:16:20 那趟必然留下 equity_check=pending,23:15 必须接着跑。
# 后半句是因为**派活口径不能是"今天是哪天"**:官方 EOD 若拖过次日 13:30,
# 16:20 一到 last_session 就换成新一天,前一天再也不会被任何一趟派到,equity 段
# 永久停在 pending;而 prev_report 会跳过它,后一天的 ΔD 就悄悄变成跨两天的量
# (滑点/股息两项却只吃当天成交 —— 混跨度,错的不是缺的)。所以两条路都堵:
# 派活按 unsettled_sessions() 的欠账清单走,真漏了也有 equity_plane 的紧邻性
# 闸门拦住不出裁决。两样都没有才是纯空转,跳过。
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

# 日志按**墙上时间**分片(找日志靠的是"我几点跑的")
LOG="$LOGDIR/qc_reconcile_$(TZ=America/New_York date +%Y%m%d).log"
log() { echo "[$(TZ=America/New_York date +%H:%M:%S)] $*" >> "$LOG"; }
# 手工跑时必须在终端上看得见结果。之前所有输出都只进日志,人在终端上看到的是
# 一片空白 —— 跑成功和跑挂了长得一模一样。launchd 那边 stdout 进 launchd 日志,
# 多一份摘要无害。
say() { log "$*"; echo "$*"; }

PY() { "$CONDA" run -n "$CONDA_ENV" --no-capture-output python "$@"; }

# ── 环境:根 .env(POLYGON_API_KEY)────────────────────────────────────────
# equity 平面的 Q = 现金 + Σ 逐票股数 × **官方收盘价**,收盘价走 Polygon。
# launchd 起的进程只继承 SSH_AUTH_SOCK,不带任何 .env;不 source 就永远拿不到
# 收盘价,③ 每晚都停在 pending 而且看着像"在等官方 EOD"。
# 与 controller/ops/reconcile_eod.sh 同一写法(同一个 key,同一份根 .env)。
set -a
# shellcheck disable=SC1091
[ -f "$REPO/.env" ] && . "$REPO/.env"
set +a
# 拿不到 key 时**明说**:①② 照跑(它们不碰 Polygon),但别让 ③ 的 pending
# 被当成"今晚官方 EOD 还没落地"这种会自愈的原因。
[ -n "${POLYGON_API_KEY:-}" ] || \
    say "!! POLYGON_API_KEY 不在 $REPO/.env 里 — ③ equity 定不出 Q,本趟只出 ①②"

# ── 交易日 + 模式:一次问清,两边同源 ──────────────────────────────────────
# 交易日必须问 Python 要,不能用墙上日期:Python 侧一律用 last_session() 推
# 交易日并据此命名报告,wrapper 若自己 `date +%Y-%m-%d`,过了零点两边就错位
# (00:40 时墙上已是次日,last_session 仍是前一交易日)—— 那样会拿错文件做
# 幂等判断、末尾读回 verdict 报 NO-FILE。
#
# 模式由"此刻是否还在 session 的收盘窗口内"决定,不是由排期时刻硬编码:
#   live   —— 在 [session 收盘, 次交易日开盘) 内。QC 实时净值仍等于该 session
#             的收盘净值,可以取快照存档并直接算 D。
#   settle —— 已越过次日开盘。此刻的 QC 读数是**新一天的盘中值**,拿它算 D
#             量出来的是隔夜跳空加一段行情。改用收盘那趟存下的快照补算,
#             这条路径一行 QC API 都不调。
#
# 原来那道"墙上今天是不是交易日"的休市闸门已经删掉,因为它现在是有害的:
# 周五 20:30 的 pipeline 周六上午才收工,周五那场的官方 EOD 只能在周六补 ——
# 而墙上闸门会把周六判成 CLOSED 直接跳过,那场就永远补不上。非交易日该不该
# 跑,交给下面的幂等闸门判(三段全终态就跳过),不需要第二套日历逻辑。
PLAN=$(PY -c "
import sys
sys.path.insert(0, '$QC')
try:
    from ops import rolloff
    from reconcile.qc_reconcile import last_session, in_close_window
    et = rolloff._et_now()
    s = last_session(et)
    ok, why = in_close_window(s, et)
    print(s + '|' + ('live' if ok else 'settle') + '|' + (why or '在收盘窗口内'))
except Exception as e:
    print('ERR|ERR|' + str(e)[:120])
" 2>/dev/null | tr -d '\r')
ET_DATE=$(echo "$PLAN" | cut -d'|' -f1 | tr -d '[:space:]')
MODE=$(echo "$PLAN" | cut -d'|' -f2 | tr -d '[:space:]')
WHY=$(echo "$PLAN" | cut -d'|' -f3-)
case "$ET_DATE" in
    20[0-9][0-9]-[01][0-9]-[0-3][0-9]) ;;
    *) say "交易日推不出来(得到 '${PLAN:-空}')— 不猜,skip, exit 1"; exit 1 ;;
esac
case "$MODE" in
    live|settle) ;;
    *) say "模式判不出来(得到 '${MODE:-空}')— 不猜,skip, exit 1"; exit 1 ;;
esac
log "session=$ET_DATE mode=$MODE ($WHY) 墙上 $(TZ=America/New_York date '+%m-%d %H:%M')"

# ── 幂等:今天三段全终态**且**没有欠账,才跳过 ────────────────────────────
# 光看当天不够:官方 EOD 若拖过 13:30,16:20 一到 last_session 就换成新一天,
# 前一天的 equity 段再也不会被派到活,永久停在 pending。所以这里还要问
# unsettled_sessions():只要还有哪天欠着(且存着收盘快照、补得回来),就得跑。
REPORT="$OUTDIR/qc_reconcile_${ET_DATE}.json"
PENDING=$(PY -c "
import json, sys
sys.path.insert(0, '$QC')
try:
    from ops import rolloff
    from reconcile.qc_reconcile import TERMINAL, unsettled_sessions
    try:
        d = json.load(open('$REPORT'))
    except FileNotFoundError:
        left = ['当天还没有报告']
    else:
        left = [k for k in ('holdings_check', 'target_check', 'equity_check')
                if (d.get(k) or {}).get('status') not in TERMINAL]
    back = [s for s in unsettled_sessions(rolloff._et_now()) if s != '$ET_DATE']
    if back:
        left.append('欠账:' + ','.join(back))
    print('/'.join(left) if left else 'NONE')
except Exception as e:
    print('READERR:' + str(e)[:100])
" 2>/dev/null | tr -d '[:space:]')
case "$PENDING" in
    NONE)     say "三段已全终态且无欠账 ($ET_DATE) — 幂等跳过, exit 0"; exit 0 ;;
    READERR*) say "幂等闸门判不出来($PENDING)— 当作有活,继续跑" ;;
    *)        say "待补: $PENDING — 继续跑" ;;
esac

export PYTHONUNBUFFERED=1
log "══ QC RECONCILE START ($ET_DATE, $MODE) ══"
if [ "$MODE" = "settle" ]; then
    # 不带 --session:把**所有**欠着的天按时间顺序补完,不只是 last_session 那天。
    "$CONDA" run -n "$CONDA_ENV" --no-capture-output \
        python -m reconcile.qc_reconcile --settle >> "$LOG" 2>&1
    rc=$?
else
    "$CONDA" run -n "$CONDA_ENV" --no-capture-output \
        python -m reconcile.qc_reconcile >> "$LOG" 2>&1
    rc=$?
    # 现场取完再补欠账:--backlog 那条路径一行 QC API 都不调,放在主趟之后
    # 不会耽误 16:20 那个只有一次的窗口。不补的话,官方 EOD 拖过次日 13:30 的
    # 那天就再也没有任何一趟会派到它。
    "$CONDA" run -n "$CONDA_ENV" --no-capture-output \
        python -m reconcile.qc_reconcile --backlog >> "$LOG" 2>&1
    rb=$?
    # 主趟成功但欠账没补上时,别把 rc 从 0 抹平成 0 —— 那等于把"还欠着一天"
    # 藏起来。breach(2)压过失败(1),与 settle_all 的口径一致。
    [ $rb -eq 2 ] && rc=2
    [ $rb -eq 1 ] && [ $rc -eq 0 ] && rc=1
fi
V=$(PY -c "
import json
try: print(json.load(open('$REPORT')).get('verdict', '?'))
except Exception: print('NO-FILE')
" 2>/dev/null | tr -d '[:space:]')
say "══ QC RECONCILE END (rc=$rc verdict=$V) ══"
say "报告: $REPORT"
say "日志: $LOG"
# 手工跑时把三段裁决也打到终端(launchd 那趟多几行无所谓)
"$CONDA" run -n "$CONDA_ENV" --no-capture-output python -c "
import json
try:
    d = json.load(open('$REPORT'))
except Exception as e:
    raise SystemExit(f'  (报告读不出来: {e})')
for k, name in (('holdings_check', '① holdings'), ('target_check', '② target'),
                ('equity_check', '③ equity ')):
    s = d.get(k) or {}
    print(f\"  {name} [{str(s.get('status')).upper()}] {s.get('note') or ''}\"[:200])
" 2>/dev/null
# qc_reconcile.py 对 breach 返回 2;那不是"脚本失败",原样透传给 launchd。
[ $rc -eq 2 ] && exit 2
[ $rc -ne 0 ] && exit 1
exit 0
