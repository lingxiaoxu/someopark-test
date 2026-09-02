#!/bin/bash
# =============================================================================
# semiconductor_pipeline.sh — AISS (AI Infra & Semiconductor Strategy) pipeline
# =============================================================================
# Single production entrypoint for the AISS strategy.  Mirrors SSRS's pipeline
# but is purpose-built for AISS: individual-stock data, no EPS/value modes.
#
# Usage (run from the repo root or anywhere; paths are resolved absolutely):
#     bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh <MODE> [opts]
#
# Modes:
#     update_data   Incremental data refresh (prices + capex + dram + slow PIT checks)
#     daily         Generate today's AISS signal + update inventory  (NYSE holiday-aware)
#     weekly        Data/PIT health + weekly_review (drift/regime/multi-horizon) + dry-run
#     monthly       Refresh P0 (V1+V2 select via daily_backtest, restore V1) + force-rebalance  (holiday-aware)
#     dry-run       Daily signal WITHOUT writing inventory (safe any time)
#     backtest      Single full backtest of the active/selected param set
#     batch         Backtest all param sets -> ranked CSV/Excel
#     select        Batch + walk-forward OOS selection -> selected_param_set.json
#                   (add --signal-version v1|v2 to select per version)
#     daily_backtest V1+V2 select + validate + tearsheet suite (refreshes the
#                   per-version P0 caches smart_select uses to pick V1 vs V2)
#     walk-forward  Walk-forward IS/OOS robustness (anchored + rolling)
#     validate      Backtest + compare vs SOXX/SMH/SPY (win criterion)
#                   (add --signal-version v1|v2)
#
# Signal versions: V1 = monthly rebalance (production default, strongest);
#                  V2 = semi-monthly (1st + ~mid-month) with the same 12-1 signal.
#                  Most modes accept `--signal-version v1|v2`; daily auto-picks
#                  via smart_select unless you pass --signal-version.
#     tearsheet     Full PDF tearsheet (vs SOXX/SMH/SPY)
#     test          Run the pytest suite (offline, synthetic data)
#     status        Print current inventory + latest signal summary
#     help          This message
#
# Flags: --skip-holiday  bypass the NYSE holiday check (backfill / manual runs).
#        daily_backtest.sh also honors --skip-holiday. daily/monthly/daily_backtest
#        skip + exit 0 on weekends/holidays (normal success); weekly + dry-run always run.
#
# Environment: ALWAYS conda env `qlib_run` + `.env` (POLYGON_API_KEY, FRED_API_KEY).
# All outputs stay inside this strategy dir (and additive data under price_data/).
# =============================================================================

set -uo pipefail

# --- resolve paths (script may be invoked from anywhere) ---------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"     # .../semiconductor_strategy
SELF="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"            # absolute path to this script
QLIB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"                       # .../qlib-main
REPO_ROOT="$(cd "$QLIB_DIR/.." && pwd)"                        # .../someopark-test
PKG="semiconductor_strategy"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d_%H%M%S)"

CONDA_ENV="qlib_run"
# ── 2026-09-01 hardening (mirrored from AEUS, same-night incident): openclaw exec may spawn a
# NON-LOGIN shell with no conda on PATH (AEUS 19:10 daily-backtest died "conda: command not
# found", then the unconditional restore copied an EMPTY mktemp over selected_param_set.json).
# Resolve conda ourselves instead of trusting the caller's shell.
if ! command -v conda >/dev/null 2>&1; then
    for _c in /Users/xuling/miniforge3 "$HOME/miniforge3" "$HOME/miniconda3" /opt/homebrew/Caskroom/miniforge/base; do
        if [ -f "$_c/etc/profile.d/conda.sh" ]; then . "$_c/etc/profile.d/conda.sh"; break; fi
    done
    command -v conda >/dev/null 2>&1 || { echo "FATAL: conda not found (PATH=$PATH)" >&2; exit 2; }
fi
PY() { conda run -n "$CONDA_ENV" --no-capture-output python "$@"; }

# --- load .env (only the keys we need; avoids the & job-control noise) -------
if [ -f "$REPO_ROOT/.env" ]; then
    export POLYGON_API_KEY="$(grep -E '^POLYGON_API_KEY=' "$REPO_ROOT/.env" | cut -d= -f2- | tr -d '"' )"
    export FRED_API_KEY="$(grep -E '^FRED_API_KEY=' "$REPO_ROOT/.env" | cut -d= -f2- | tr -d '"' )"
fi

# ── Mode parsing (first positional arg, default: help) ───────────────────────
MODE="${1:-help}"
[[ $# -gt 0 ]] && shift

# ── Option defaults ───────────────────────────────────────────────────────────
SKIP_HOLIDAY=0
PASS_ARGS=()           # everything except --skip-holiday is forwarded to python

# ── Option parsing (mirrors SSRS while/case style) ────────────────────────────
# AISS handles --skip-holiday in the shell (the NYSE check is a shell concern)
# and forwards every other flag verbatim to the python entrypoints, which self-
# parse their own flags (--signal-version / --param-set / --date /
# --force-rebalance / --dry-run / etc.).  Hence the catch-all collects rather
# than errors on `-*`, the one deliberate deviation from SSRS's parser.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-holiday)   SKIP_HOLIDAY=1;       shift ;;
        help|--help|-h)   MODE="help";          shift ;;
        *)                PASS_ARGS+=("$1");     shift ;;
    esac
done
set -- ${PASS_ARGS[@]+"${PASS_ARGS[@]}"}

cd "$QLIB_DIR"   # so `python -m semiconductor_strategy.*` resolves

# Clear stale __pycache__ (prevents bytecode bugs after code edits)
find "$SCRIPT_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

log() { echo "[$(date '+%H:%M:%S')] $*"; }
hr()  { echo "══════════════════════════════════════════════════════════════════"; }

# ── NYSE holiday check (mirrors SSRS) ─────────────────────────────────────────
# Skips work + exits 0 on weekends/holidays (normal success, not failure).
# Uses pandas_market_calendars in qlib_run; falls back to a weekday-only check.
check_nyse_open() {
    if [ "$SKIP_HOLIDAY" -eq 1 ]; then log "Holiday check skipped (--skip-holiday)"; return 0; fi
    local NYSE_STATUS
    NYSE_STATUS=$(PY -c "
import sys
from datetime import datetime
try:
    import pytz
    import pandas_market_calendars as mcal
    nyc_date = datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d')
    sched = mcal.get_calendar('NYSE').schedule(start_date=nyc_date, end_date=nyc_date)
    print('OPEN' if not sched.empty else 'CLOSED:' + nyc_date); sys.exit(0)
except ImportError:
    pass
except Exception as e:
    print('WARN:' + str(e)[:60], file=sys.stderr)
from datetime import date
t = date.today()
print('CLOSED:' + str(t) + '-weekend' if t.weekday() >= 5 else 'OPEN-WEEKDAY')
" 2>/dev/null) || NYSE_STATUS="OPEN-FALLBACK"
    if [[ "$NYSE_STATUS" == CLOSED* ]]; then
        hr; log "NYSE 休市 (${NYSE_STATUS#CLOSED:}) — pipeline skip, exit 0"; hr
        exit 0
    fi
    log "NYSE status: $NYSE_STATUS — proceeding"
}

# 步骤集合 = data/ 层**所有**带 --update/--check 的 updater。
# 2026-08-27 补上 6/7 两步: `--update-pmi` 和 `--update-hyperscaler-capex` 一直存在
# 于 CLI 却从未被这里调用,于是 pmi_series 停在 2026-04(缺 5/6/7 三期)。新增
# updater 时**必须同步加进这个函数**,否则又是一条无人调用的死通路。
# 每步独立失败(一个源挂了不该拖垮其余六个),但**失败要记账**并反映到返回值上:
# 原写法末尾是 `log "…complete."`,函数因此恒返回 0,调用方的 `|| log WARN` 守卫
# 永远不可能触发 —— 又一条"跑着但永不报警"的死通路,与本次要修的病理同源。
run_update_data() {
    hr; log "AISS update_data — incremental refresh"; hr
    local rc=0 n=0
    _step() {   # _step "<描述>" <cmd...>
        n=$((n + 1)); local desc="$1"; shift
        # ${} 是必须的: bash 3.2(macOS 自带)按**字节**解析变量名,紧跟其后的多字节
        # 字符 '…' 会被吃进名字里 → `$desc…` 变成未绑定变量 desc…,set -u 直接中止。
        log "${n}/7 ${desc}…"
        if ! PY "$@"; then
            rc=1; log "  WARN: $desc 失败(继续下一步;weekly --verify 会把它标 STALE)"
        fi
    }
    _step "prices (Polygon incremental)"        -m $PKG.data.aiss_fetch_prices --update "$@"
    _step "CapEx pulse (yfinance)"              -m $PKG.data.company_signals  --update-capex "$@"
    _step "MU DIO (SEC XBRL)"                   -m $PKG.data.company_signals  --check-mu-dio "$@"
    _step "hyperscaler actual CapEx (SEC XBRL)" -m $PKG.data.company_signals  --update-hyperscaler-capex "$@"
    _step "TSMC / ASML (TWSE + SEC, PIT)"       -m $PKG.data.industry_signals --check-tsmc --check-asml "$@"
    _step "DRAM proxy (computed)"               -m $PKG.data.industry_signals --update-dram "$@"
    _step "PMI / IPMAN (FRED, PIT)"             -m $PKG.data.industry_signals --update-pmi "$@"
    if [ "$rc" -eq 0 ]; then log "update_data complete (7/7 ok)."
    else log "update_data complete WITH FAILURES — 见上面的 WARN 行。"; fi
    return "$rc"
}

case "$MODE" in
    update_data|update-data)
        run_update_data "$@" 2>&1 | tee "$LOG_DIR/aiss_update_data_$TS.log"
        ;;

    daily)
        check_nyse_open
        # Event-risk shared data refresh (NON-FATAL, idempotent): keeps the event_risk
        # price store + NFP/bellwether calendars current so the AISS event-risk overlay
        # (in AISSdailySignal) reads fresh data even if the someopark conductor hasn't
        # run its refresh yet.  Safe to run from both pipelines (dedupe keep-last).
        hr; log "event-risk data refresh (non-fatal)"; hr
        PY "$REPO_ROOT/RefreshEventRiskData.py" 2>&1 | tee "$LOG_DIR/aiss_event_refresh_$TS.log" \
            || log "WARN: event-risk data refresh failed (non-fatal, continuing)"
        # PIT 信号层刷新 (NON-FATAL, 幂等 append-only)。
        # 2026-08-27 接线。此前 update_data 只存在于手工调用,没有任何 cron/launchd
        # 调它 —— capex_pulse 与 dram_proxy 从 2026-06-04 冻到 08-27(57 个交易日),
        # 而这两者 + tsmc/asml/mu_dio/pmi 一起喂 composite 的 **0.70 权重**
        # (capex_tilt .25 + cycle_regime .10 + supply_chain .35)。实测代价: 真实
        # capex z 在 7/31 月末翻负(-0.45),生产仍用冻结的 +0.43,而 capex_tilt =
        # cs_zscore(z × beta) 只保留**符号** → 8/3 那次月度调仓方向做反了。
        # 与 macro self-heal (AISSdailySignal._load_macro_from_store) 同模式:
        # 补一次、失败降级、**绝不阻断 signal** —— 数据旧总好过当天没信号。
        # 不传 "$@": daily 的参数是 AISSdailySignal 的(--date/--dry-run 等),
        # 喂给 updater 会直接 argparse 报错。
        hr; log "PIT signal data refresh (non-fatal)"; hr
        run_update_data 2>&1 | tee "$LOG_DIR/aiss_update_data_$TS.log" \
            || log "WARN: PIT data refresh failed (non-fatal, signal proceeds on existing data)"
        # AISSdailySignal refreshes its own price store (incremental, before marking)
        # so positions mark to today's close regardless of how it's invoked.
        hr; log "AISS daily signal"; hr
        PY -m $PKG.AISSdailySignal "$@" 2>&1 | tee "$LOG_DIR/aiss_daily_$TS.log"
        ;;

    dry-run|dryrun)
        PY -m $PKG.AISSdailySignal --dry-run "$@" 2>&1 | tee "$LOG_DIR/aiss_dryrun_$TS.log"
        ;;

    backtest)
        hr; log "AISS single backtest"; hr
        PY -m $PKG.backtest.engine "$@" 2>&1 | tee "$LOG_DIR/aiss_backtest_$TS.log"
        ;;

    batch)
        hr; log "AISS batch (all param sets)"; hr
        PY -m $PKG.AISSBatchRun "$@" 2>&1 | tee "$LOG_DIR/aiss_batch_$TS.log"
        ;;

    select)
        hr; log "AISS production param selection (batch + WF OOS + MCPS)"; hr
        PY -m $PKG.AISSBatchRun --select --save-equity "$@" 2>&1 | tee "$LOG_DIR/aiss_select_$TS.log"
        ;;

    walk-forward|wf)
        hr; log "AISS walk-forward IS/OOS"; hr
        PY -m $PKG.walk_forward "$@" 2>&1 | tee "$LOG_DIR/aiss_wf_$TS.log"
        ;;

    daily_backtest|daily-backtest)
        hr; log "AISS V1+V2 backtest/selection suite"; hr
        bash "$SCRIPT_DIR/daily_backtest.sh" "$@"
        ;;

    validate)
        hr; log "AISS validation vs SOXX / SMH / SPY (win criterion)"; hr
        PY -m $PKG.validate "$@" 2>&1 | tee "$LOG_DIR/aiss_validate_$TS.log"
        ;;

    tearsheet)
        hr; log "AISS tearsheet (PDF, vs SOXX/SMH/SPY)"; hr
        PY -m $PKG.report.tearsheet "$@" 2>&1 | tee "$LOG_DIR/aiss_tearsheet_$TS.log"
        ;;

    test)
        hr; log "AISS test suite"; hr
        PY -m pytest "$SCRIPT_DIR/tests/" -v --tb=short "$@"
        ;;

    status)
        PY -m $PKG.AISSdailySignal --dry-run --status-only 2>/dev/null || \
          PY -c "import json,glob,os; \
d=json.load(open('$SCRIPT_DIR/inventory_aiss.json')) if os.path.exists('$SCRIPT_DIR/inventory_aiss.json') else {}; \
print('inventory as_of:', d.get('as_of')); print('holdings:', d.get('holdings'))"
        ;;

    weekly)
        # Mirrors SSRS 'weekly': data/PIT health + weekly_review + dry-run validation.
        # (AISS has no EPS step; SSRS's EPS refresh is replaced by data/PIT health checks.)
        WK_LOG="$LOG_DIR/aiss_weekly_$TS.log"
        hr; log "AISS WEEKLY maintenance (price refresh + data/PIT health + weekly review + dry-run)"; hr
        # Incremental price refresh for the FULL universe incl. benchmarks (SOXX/SMH/SPY)
        # BEFORE verifying. Benchmarks are not in the daily signal's load path
        # (daily loads SR ETFs + SPY + the stock universe; SOXX/SMH only refresh when
        # the monthly optimizer runs load_subsector_prices), so without this they go
        # stale between monthly rebalances and verify always flags them. --update is
        # incremental + time-throttled, so this is cheap and idempotent. (non-fatal)
        log "0/4 price refresh (incremental, full universe incl. SOXX/SMH benchmarks)…"
        PY -m $PKG.data.aiss_fetch_prices --update 2>&1 | tee -a "$WK_LOG" \
          || log "  WARN: weekly price refresh failed (non-fatal, verify will report)"
        log "1/4 data + PIT health checks (prices / company / industry coverage)…"
        # 2026-08-27: 原本三行都是 `|| true`,把体检结果整个吞掉 —— 加上 verify()
        # 当时只查"有没有"不查"新不新",于是 capex_pulse 冻死 57 个交易日期间,
        # weekly 日志白纸黑字打着 `→2026-06-04` 和 `RESULT: OK`,一次没报警。
        # 现在 verify 会在过期时退非零,这里把它汇总成一条**显式的 FAILED 横幅**。
        # 仍不 `exit 1`:weekly 后面还有 review + dry-run,数据旧不该让它们不跑;
        # 要的是"看日志的人一眼看见",不是让整个 weekly 崩掉。
        #
        # 措辞里的 "FAILED" 是**必须**的,不是修辞: 跑 weekly 的是 openclaw cron
        # (aiss-weekly, 周日 02:00 ET),它的 runbook 判"干净成功"的条件是
        # 「…and there is no clear ERROR / FAILED / traceback」。原来写 "PIT DATA
        # STALE" —— STALE 不在那串关键词里,于是脚本喊得再响,Telegram 那头照报
        # success。这是同一病理的第四层: 数据没人更新 → 体检看不出新旧 → 体检结果
        # 被 `|| true` 吞掉 → **看护人的判据认不出这个词**。
        VERIFY_OUT="/tmp/aiss_weekly_verify_$TS.txt"   # 收尾横幅要靠它列出具体是哪几条
        PIT_STALE=0
        PY -m $PKG.data.aiss_fetch_prices --verify 2>&1 | tee -a "$WK_LOG" "$VERIFY_OUT" || PIT_STALE=1
        PY -m $PKG.data.company_signals  --verify 2>&1 | tee -a "$WK_LOG" "$VERIFY_OUT" || PIT_STALE=1
        PY -m $PKG.data.industry_signals --verify 2>&1 | tee -a "$WK_LOG" "$VERIFY_OUT" || PIT_STALE=1
        if [ "$PIT_STALE" -ne 0 ]; then
            hr | tee -a "$WK_LOG"
            log "!!! PIT DATA HEALTH FAILED — 上面标了 ← STALE 的序列已过期。它们喂 composite" | tee -a "$WK_LOG"
            log "!!! 的 0.70 权重(capex_tilt .25 + cycle_regime .10 + supply_chain .35)。" | tee -a "$WK_LOG"
            log "!!! 先跑: bash $SELF update_data   再查上游数据源是否还在发布。" | tee -a "$WK_LOG"
            hr | tee -a "$WK_LOG"
        fi
        log "2/4 weekly review (multi-horizon + param drift + regime trend + P0-cache health)…"
        PY -m $PKG.weekly_review "$@" 2>&1 | tee -a "$WK_LOG" \
          || log "  WARN: weekly_review failed — continuing with dry-run"
        log "3/4 dry-run validation (full pipeline healthy, no inventory write)…"
        PY -m $PKG.AISSdailySignal --dry-run 2>&1 | tee -a "$WK_LOG"
        # 收尾复述 —— 位置本身就是修复的一部分。上面那条横幅打在 1/4,而 2/4 的
        # weekly_review(多轮回测)+ 3/4 dry-run 会往后刷成百上千行;cron runbook
        # 的验收只 `tail -25`,横幅早被冲出视野。所以必须在**最后**再喊一次,并把
        # 具体过期的序列名带上,让 tail 到的人不用回头翻日志。
        if [ "${PIT_STALE:-0}" -ne 0 ]; then
            # verify 行形如 `  capex_pulse  : … ← STALE (57交易日 > 5交易日)`;
            # 取第一个冒号前的名字,逗号分隔(名字本身可能带空格,如 "pmi (IPMAN)")。
            # substr 兜底: 万一某个 verify 的行没有冒号,$1 就是整行,截断免得糊屏。
            # 抽不出来就退回一句指路,绝不假装知道是哪条。
            STALE_LIST=$(awk -F: '
                /← STALE/ { gsub(/^[ \t]+|[ \t]+$/, "", $1)
                            out = out (out ? ", " : "") substr($1, 1, 40) }
                END { print out }' "$VERIFY_OUT" 2>/dev/null)
            hr | tee -a "$WK_LOG"
            log "!!! PIT DATA HEALTH FAILED — 过期序列: ${STALE_LIST:-见日志中 ← STALE 标记}" | tee -a "$WK_LOG"
            log "!!! 修复: bash $SELF update_data" | tee -a "$WK_LOG"
            hr | tee -a "$WK_LOG"
        fi
        rm -f "$VERIFY_OUT"
        log "WEEKLY MAINTENANCE COMPLETE  (log: $WK_LOG)"
        ;;

    monthly)
        # Mirrors SSRS 'monthly': refresh all P0 selection data + force-rebalance.
        # AISS refreshes P0 via daily_backtest (V1+V2 select → restore V1), which
        # encompasses the per-version 'select'; then a force-rebalance daily signal.
        check_nyse_open
        MO_LOG="$LOG_DIR/aiss_monthly_$TS.log"
        hr; log "AISS MONTHLY (V1+V2 select suite + restore V1 + force-rebalance)"; hr
        log "1/2 daily_backtest: V1+V2 batch + WF-OOS select → refresh P0 caches → restore V1"
        bash "$SCRIPT_DIR/daily_backtest.sh" 2>&1 | tee -a "$MO_LOG"
        log "2/2 force-rebalance daily signal (smart_select with fresh P0 caches)…"
        PY -m $PKG.AISSdailySignal --force-rebalance "$@" 2>&1 | tee -a "$MO_LOG"
        log "MONTHLY REBALANCE COMPLETE  (log: $MO_LOG)"
        ;;

    help|--help|-h|"")
        sed -n '2,46p' "$SELF" | sed 's/^# \{0,1\}//'
        ;;

    *)
        echo "Unknown mode: $MODE"; echo "Run: bash semiconductor_pipeline.sh help"; exit 2 ;;
esac
