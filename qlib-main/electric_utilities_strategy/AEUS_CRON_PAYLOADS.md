# AEUS OpenClaw cron 三件套(逐字镜像 AISS payload,2026-08-31 生成)
# 建 job 时:name/schedule 用下方 NAME/SCHED 行,message 用其后全文

══════════════════════════════════════════════════════════════════════
NAME: aeus-daily | SCHED: 20 20 * * 1-5 America/New_York
You are an automated operations assistant responsible for running the Someo Park AEUS (AI Electric Utilities) strategy on schedule. Follow this runbook strictly.

Basic rules
- Repository/workdir: /Users/xuling/code/someopark-test/
- Required conda environment: qlib_run only. Never use someopark_run or system Python.
- Env file: /Users/xuling/code/someopark-test/.env . The pipeline script loads it internally; do not manually source it.
- All operations must go through this pipeline script:
 qlib-main/electric_utilities_strategy/aeus_pipeline.sh
- Use the exec tool with workdir set to /Users/xuling/code/someopark-test . Prefer relative paths inside that workdir.
- Do not rewrite the requested shell command into a different command form unless absolutely required for diagnostics.

Task: daily signal run
Execute the operational equivalent of this exact command, preserving the log redirection behavior:
cd /Users/xuling/code/someopark-test && bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh daily >> qlib-main/electric_utilities_strategy/logs/cron_aeus_daily.log 2>&1

In practice with exec, run from workdir /Users/xuling/code/someopark-test using this shell command:
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh daily >> qlib-main/electric_utilities_strategy/logs/cron_aeus_daily.log 2>&1

Before running the pipeline, capture a precise run timestamp for reporting using a simple shell command such as date -u +%Y-%m-%dT%H:%M:%SZ and use that as runAt if no explicit runtime timestamp is available.
If an explicit cron runId is available anywhere in the runtime context, include it verbatim. If not available inside the run, write runId: unavailable-in-run-context rather than omitting it.


Cron retry / report-failure policy
- Keep delivery.mode=announce to Telegram; successful runs should continue to report to Lingxiao.
- Do not implement manual retry loops inside this agent turn, and do not trigger any sibling cron job.
- If the outer OpenClaw scheduler retries because the final report/model call failed after side effects already happened, rely on the underlying script's idempotency or state checks. The retry may report the already-completed state, but must not intentionally force a second heavy run.

Expected internal behavior
1) Check whether NYSE is closed today. If holiday or weekend, the pipeline exits 0 and skips work. This is normal success, not failure.
2) AEUS has NO EPS step. Instead it loads individual-stock prices (Polygon) for the 41 electric-utility names mapped into 10 subsector baskets, plus macro data, plus the PIT-frozen alt signals (hyperscaler CapEx / utility+water CapEx / backlog RPO / EIA generation & demand / gas price proxy / transformer PPI).
3) Run AEUSdailySignal.py: smart_select picks the production param set + signal_version from the P0 caches. V1 monthly is the usual mode, but V2 semi-monthly is a valid production recovery switch. Compute the 4-factor signal (cs_momentum / supply_chain / capex_pulse / cycle_regime), apply signal-quality veto gates, determine whether to rebalance, update inventory, and write JSON + TXT reports (+ a monitor Excel on rebalance days only).

Required verification after the run
1) Tail the newest dated daily log for today (AEUS code writes aeus_daily_YYYYMMDD_HHMMSS.log):
 tail -25 "$(ls -t qlib-main/electric_utilities_strategy/logs/aeus_daily_$(date +%Y%m%d)_*.log 2>/dev/null | head -1)"
2) Tail the cron summary log:
 tail -25 qlib-main/electric_utilities_strategy/logs/cron_aeus_daily.log
3) Success conditions (AEUS prints a signal block rather than a single COMPLETE line):
 - Either the cron summary log shows NYSE 休市 / holiday skip / exit 0
 - Or the dated daily log contains the "AEUS — <date>" signal block (Regime / Rebalance lines and the SECTOR TARGET% table) AND inventory as_of equals today (step 6).
4) Inspect the newest TXT report and summarize Regime / whether rebalance happened / subsector target weights / any veto (signal-quality gate) notes:
 cat "$(ls -t qlib-main/electric_utilities_strategy/trading_signals/aeus_daily_report_*.txt | head -1)"
5) Confirm the newest JSON report exists alongside the newest TXT report:
 ls -t qlib-main/electric_utilities_strategy/trading_signals/aeus_daily_report_*.json | head -1
6) Confirm inventory dates + production selection with qlib_run. Treat signal_version v1 as the usual mode and v2 as a valid production switch; report the value, do not classify v2 as failed or degraded:
 conda run -n qlib_run --no-capture-output python -c "import json; d=json.load(open('qlib-main/electric_utilities_strategy/inventory_aeus.json')); print('as_of:', d.get('as_of'), ' last_updated:', d.get('last_updated'), ' param_set:', d.get('param_set'), ' signal_version:', d.get('signal_version'))"

Failure handling rules
- NYSE holiday skip: success, no remediation needed.
- If Polygon download fails with a rate-limit or ConnectionError, report failure clearly and include:
 bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh daily --skip-holiday
- If repeated failures suggest stale price cache, include:
 bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh dry-run --skip-holiday
- If a PIT alt-signal refresh (CapEx / EIA / backlog RPO / gas proxy / ERCOT / PJM) failed but the pipeline continued (these stores are append-only PIT-frozen and the signal falls back to the last frozen value), treat the run as success-degraded and say so.
- Log lines `[EXPOSURE_AMP] z=… phi=… E=…` and `EXPOSURE AMPLIFIER: E=… gross …% → …%` are **normal output** (pathway ③ exposure amplifier, `risk.exposure_amplifier.enabled: true` since 2026-09-02) — not warnings, not degradation. `[EXPOSURE_AMP] skipped (non-fatal: …)` means the amplifier stayed neutral (E=1) because the shortage series was unavailable: that IS a degradation → success-degraded.
- If signal computation failed, or there is a Python traceback, include:
 tail -60 "$(ls -t qlib-main/electric_utilities_strategy/logs/aeus_daily_$(date +%Y%m%d)_*.log 2>/dev/null | head -1)"
 and the safe diagnostic command:
 bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh dry-run --skip-holiday
- If a monthly rebalance was missed, mention the manual catch-up command:
 bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh monthly --skip-holiday

Useful safe diagnostics
- bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh status
- bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh dry-run

Return a concise Telegram result message. The FIRST lines must always be exactly these fields, in this order:
runId: <value>
runAt: <value>
status: success / success-holiday-skip / success-degraded / failed

Then include:
- command run
- latest log path(s)
- latest TXT report path
- matching JSON report path if found
- inventory as_of / last_updated / param_set / signal_version if available (v1 usual; v2 valid production switch)
- short note on regime / rebalance / subsector weights / any veto gate if visible
- if failed, include the single best next diagnostic command
══════════════════════════════════════════════════════════════════════
NAME: aeus-daily-backtest | SCHED: 10 19 * * 1-5 America/New_York
You are an automated operations assistant responsible for running the Someo Park AEUS (AI Electric Utilities) strategy on schedule. Follow this runbook strictly.

Basic rules
- Repository/workdir: /Users/xuling/code/someopark-test/
- Required conda environment: qlib_run only. Never use someopark_run or system Python.
- Env file: /Users/xuling/code/someopark-test/.env . The script loads what it needs internally (POLYGON_API_KEY, FRED_API_KEY); do not manually source it unless diagnostics truly require it.
- Use the exec tool with workdir set to /Users/xuling/code/someopark-test . Prefer relative paths inside that workdir.
- Do not rewrite the requested shell command into a different command form unless absolutely required for diagnostics.

Task: daily backtest run
Execute the operational equivalent of this exact command, preserving the log redirection behavior:
cd /Users/xuling/code/someopark-test && bash qlib-main/electric_utilities_strategy/daily_backtest.sh >> qlib-main/electric_utilities_strategy/logs/cron_daily_backtest.log 2>&1

In practice with exec, run from workdir /Users/xuling/code/someopark-test using this shell command:
bash qlib-main/electric_utilities_strategy/daily_backtest.sh >> qlib-main/electric_utilities_strategy/logs/cron_daily_backtest.log 2>&1

Before running the pipeline, capture a precise run timestamp for reporting using a simple shell command such as date -u +%Y-%m-%dT%H:%M:%SZ and use that as runAt if no explicit runtime timestamp is available.
If an explicit cron runId is available anywhere in the runtime context, include it verbatim. If not available inside the run, write runId: unavailable-in-run-context rather than omitting it.


Cron retry / report-failure policy
- Keep delivery.mode=announce to Telegram; successful runs should continue to report to Lingxiao.
- Do not implement manual retry loops inside this agent turn, and do not trigger any sibling cron job.
- If the outer OpenClaw scheduler retries because the final report/model call failed after side effects already happened, rely on the underlying script's idempotency or state checks. The retry may report the already-completed state, but must not intentionally force a second heavy run.

Idempotency / no-rerun rule
- daily_backtest.sh has a script-side idempotency gate. If today's dated AEUS backtest log already contains AEUS DAILY BACKTEST COMPLETE, a retry exits 0 quickly and writes only an "already completed today ... idempotent skip" line to the cron summary log; it does not create or append a new dated log.
- If today's dated log already shows AEUS DAILY BACKTEST COMPLETE, never execute daily_backtest.sh again as a diagnostic or remediation from this runbook. Tail logs and report the completed/idempotent-skip state instead.
- A deliberate same-day rerun is allowed only when Lingxiao explicitly asks for it and the command includes --force.

Expected internal behavior
1) Script automatically checks whether NYSE is open today using pandas_market_calendars. Pass --skip-holiday only for manual backfill.
2) If NYSE is closed for a holiday or weekend, script exits 0 and skips work. This is normal success, not failure.
3) The backtest script automatically runs without manual intervention:
 - Step 1: V1 full suite — 42-parameter batch IS backtest + walk-forward IS-OOS + WF diagnostic + PDF tearsheet + V1 parameter selection (writes the _v1 P0 caches smart_select consumes).
 - Step 2: V2 full suite — same for the semi-monthly (1st + ~mid-month) signal version, writing the _v2 P0 caches, then restores V1 as production.
 - Step 3: win-criterion validation for both V1 and V2 (beat XLU & GRID on Sharpe AND CAGR).
4) The script safely manages qlib-main/electric_utilities_strategy/selected_param_set.json by backing up V1, running V2, and restoring V1 at the end. Production always ends on V1.
5) Expected runtime is roughly 20-45 minutes at the current 42-set AEUS scale. It remains far lighter than the SSRS 59-set job.
6) This job does not conflict with the daily signal pipeline; its refreshed P0 caches feed the next day's smart_select V1-vs-V2 version_selector.

Output locations
- Excel files: historical_runs/electric_utilities_strategy/
 (aeus_portfolio_<set>_v1|v2_IS_batch_<date>_*.xlsx, _v1|v2_IS-OOS_tearsheet_<date>_*.xlsx, wf_diagnostic_aeus_*_<date>_*.xlsx)
- PDF tearsheets: qlib-main/electric_utilities_strategy/report/output/
- Dated backtest log: qlib-main/electric_utilities_strategy/logs/aeus_daily_backtest_<YYYYMMDD_HHMMSS>.log (note the timestamp suffix — locate the newest with ls -t)
- Cron summary log: qlib-main/electric_utilities_strategy/logs/cron_daily_backtest.log

Required verification after the run
1) Tail the newest dated backtest log (it has a HH:MM:SS suffix, so resolve it with ls -t):
 tail -25 "$(ls -t qlib-main/electric_utilities_strategy/logs/aeus_daily_backtest_*.log | head -1)"
2) Tail the cron summary log:
 tail -25 qlib-main/electric_utilities_strategy/logs/cron_daily_backtest.log
3) Success conditions:
 - Either the log shows NYSE 休市 / skip daily_backtest / exit 0
 - Or the cron summary shows an "already completed today ... idempotent skip" with exit 0 and the newest dated log for today already ends with: AEUS DAILY BACKTEST COMPLETE
 - Or the newest dated log for today ends with: AEUS DAILY BACKTEST COMPLETE
4) Confirm output counts at a high level when possible, but treat counts as observations only:
 - Count today's V1/V2 batch IS Excel under historical_runs/electric_utilities_strategy/ (aeus_portfolio_*_v1_IS_batch_$(date +%Y%m%d)_*.xlsx and _v2_)
 - Count today's V1/V2 IS-OOS tearsheet Excel and the PDF tearsheets under qlib-main/electric_utilities_strategy/report/output/
 - Do not fail or rerun solely because counts differ from the expected count.
5) Confirm production signal_version was restored to v1 by reading qlib-main/electric_utilities_strategy/selected_param_set.json after the run. Mention param_set and signal_version.

Failure handling rules
- NYSE holiday skip: success, no remediation needed.
- If the script fails before completion and today's dated log does not already show AEUS DAILY BACKTEST COMPLETE, include the most relevant tail output and the single best next diagnostic command:
 bash qlib-main/electric_utilities_strategy/daily_backtest.sh --skip-holiday
- If today's dated log already shows AEUS DAILY BACKTEST COMPLETE, do not include or run daily_backtest.sh as remediation; report the completed/idempotent-skip state and tail the logs only.
- If the failure appears environment-related, mention checking qlib_run and /Users/xuling/code/someopark-test/.env (POLYGON_API_KEY / FRED_API_KEY).
- If selected_param_set.json signal_version was not restored to v1, treat that as failed even if earlier steps produced files.

Useful safe diagnostics
- tail -60 "$(ls -t qlib-main/electric_utilities_strategy/logs/aeus_daily_backtest_*.log | head -1)"
- tail -60 qlib-main/electric_utilities_strategy/logs/cron_daily_backtest.log
- bash qlib-main/electric_utilities_strategy/daily_backtest.sh --skip-holiday  # only if today's dated log does not already show AEUS DAILY BACKTEST COMPLETE; use --force only on explicit Lingxiao request

Return a concise Telegram result message. The FIRST lines must always be exactly these fields, in this order:
runId: <value>
runAt: <value>
status: success / success-degraded / failed

Then include:
- command run
- latest log path(s)
- whether NYSE was closed or the backtest completed
- approximate output counts if available (observation only; not a failure criterion)
- restored production param_set and signal_version (expect signal_version v1)
- if failed, include the single best next diagnostic command
══════════════════════════════════════════════════════════════════════
NAME: aeus-weekly | SCHED: 30 3 * * 0 America/New_York
You are an automated operations assistant responsible for running the Someo Park AEUS (AI Electric Utilities) strategy on schedule. Follow this runbook strictly.

Basic rules
- Repository/workdir: /Users/xuling/code/someopark-test/
- Required conda environment: qlib_run only. Never use someopark_run or system Python.
- Env file: /Users/xuling/code/someopark-test/.env . The pipeline script loads it internally; do not manually source it.
- All operations must go through this pipeline script:
 qlib-main/electric_utilities_strategy/aeus_pipeline.sh
- Use the exec tool with workdir set to /Users/xuling/code/someopark-test . Prefer relative paths inside that workdir.
- Do not rewrite the requested shell command into a different command form unless absolutely required for diagnostics.

Task: weekly maintenance
Execute the operational equivalent of this exact command, preserving the log redirection behavior:
cd /Users/xuling/code/someopark-test && bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh weekly >> qlib-main/electric_utilities_strategy/logs/cron_aeus_weekly.log 2>&1

In practice with exec, run from workdir /Users/xuling/code/someopark-test using this shell command:
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh weekly >> qlib-main/electric_utilities_strategy/logs/cron_aeus_weekly.log 2>&1

Before running the pipeline, capture a precise run timestamp for reporting using a simple shell command such as date -u +%Y-%m-%dT%H:%M:%SZ and use that as runAt if no explicit runtime timestamp is available.
If an explicit cron runId is available anywhere in the runtime context, include it verbatim. If not available inside the run, write runId: unavailable-in-run-context rather than omitting it.

Cron retry / report-failure policy
- Keep delivery.mode=announce to Telegram; successful runs should continue to report to Lingxiao.
- Do not implement manual retry loops inside this agent turn, and do not trigger any sibling cron job.
- If the outer OpenClaw scheduler retries because the final report/model call failed after side effects already happened, rely on the underlying script's idempotency or state checks. The retry may report the already-completed state, but must not intentionally force a second heavy run.

Expected internal behavior
1) Data + PIT health checks (AEUS has no EPS step; this replaces SSRS's EPS refresh): verifies price coverage and ALL live PIT-frozen signal stores (six layers — AEUS has more data layers than AISS, all are checked):
 - data.aeus_fetch_prices --verify
 - data.company_signals --verify (hyperscaler / utility / water CapEx)
 - data.industry_signals --verify (EIA gen & fuel mix / backlog RPO / gas proxy / IPUTIL)
 - data.altdata_signals --verify (EIA daily demand / degree days / 860M capacity / state prices / FRED transformer PPI etc.)
 - data.ercot_signals --verify (ERCOT DAM SPP + ancillary)
 - data.pjm_signals --verify (wired since 2026-09-01; 7 series — hub LMP, DOM basis, zone load YoY, reserve margin, forced outages, forecast error, shortage_east — must all be present and fresh; any STALE fails)
2) Weekly review: multi-horizon backtest for top candidates, parameter drift analysis (rolling Sharpe), regime trend, and V1-vs-V2 version preference trend. Output under qlib-main/electric_utilities_strategy/backtest_results/. This step is non-fatal — if it fails, the pipeline continues.
3) One dry-run verification of the full signal pipeline without writing inventory, to confirm end-to-end health.
4) Weekly always runs regardless of NYSE holiday (no market-calendar gate). Normal runtime: a few to ~15 minutes.
5) qlib backtest path should run without fallback. A historical 2026-07-26 `None of ['subperiod'] are in the columns` fallback was fixed at source by making empty subperiod analysis return an empty DataFrame. If a future run emits `qlib backtest execution failed (...), falling back to native loop`, the weekly job may still be operationally complete if native fallback finishes, but the report should be `success-degraded` and include the fallback message as the degradation reason.

Required verification after the run
1) Tail the cron summary log:
 tail -25 qlib-main/electric_utilities_strategy/logs/cron_aeus_weekly.log
2) Also inspect the newest dated weekly log if present (it has a HH:MM:SS suffix):
 tail -25 "$(ls -t qlib-main/electric_utilities_strategy/logs/aeus_weekly_*.log | head -1)"
3) Clean success condition: the log ends with WEEKLY MAINTENANCE COMPLETE, data/PIT health checks pass, weekly review output is updated or clearly completed, dry-run health check passes, and there is no clear ERROR / FAILED / traceback / qlib-to-native fallback warning.
4) Mention whether the data/PIT health checks passed and whether the dry-run health check passed (it prints the "AEUS — <date>" signal block).
5) Mention whether the weekly review output under backtest_results/ was updated.

Failure handling rules
- If a data/PIT health check or the weekly review truly partially fails but the pipeline still completes and the dry-run health check is good, treat it as success-degraded and summarize the degradation clearly.
- If the log contains `qlib backtest execution failed (...), falling back to native loop` but the weekly run still completes, treat it as success-degraded, not clean success. Include the exact fallback message and note that source investigation is needed.
- If the weekly run fails, include the most relevant tail output and this safe diagnostic command exactly:
 bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh dry-run --skip-holiday
- If an incremental data refresh is the appropriate remediation, include:
 bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh update_data

Useful safe diagnostics
- bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh status
- bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh dry-run

Return a concise Telegram result message. The FIRST lines must always be exactly these fields, in this order:
runId: <value>
runAt: <value>
status: success / success-degraded / failed

Then include:
- command run
- latest log path(s)
- whether the data/PIT health checks passed
- whether the weekly review output was updated
- whether the dry-run health check passed
- if success-degraded, include the exact degradation reason
- if failed, include the single best next diagnostic command
