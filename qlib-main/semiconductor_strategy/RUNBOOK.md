# AISS — AI Infra & Semiconductor Strategy — Runbook

All commands run from the repo root `/Users/xuling/code/someopark-test/` and use
the **`qlib_run`** conda env with `.env` loaded (POLYGON_API_KEY, FRED_API_KEY).

> **Isolation:** AISS only writes inside `qlib-main/semiconductor_strategy/` and
> *additively* under `price_data/semi_strategy/`. It never touches
> `sector_rotation/`, root scripts, or pre-existing data. `price_data/macro/` is
> read-only.

**Single entrypoint:**
```bash
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh <MODE> [opts]
```
(The pipeline auto-loads the two API keys from `.env` and selects `qlib_run`.)

**15 modes:** `update_data` · `daily` · `weekly` · `monthly` · `dry-run` ·
`backtest` · `batch` · `select` · `daily_backtest` · `walk-forward` · `validate`
· `tearsheet` · `test` · `status` · `help`.

**NYSE holiday check:** `daily`, `monthly`, `daily_backtest` skip work + `exit 0`
on weekends/holidays (normal success, via `pandas_market_calendars`); `weekly` /
`dry-run` always run. Pass `--skip-holiday` to bypass (backfill / manual runs).

**V1 / V2:** V1 = monthly (production default, strongest); V2 = semi-monthly
(1st + ~mid-month) with the same 4-factor / 12-1 signal. `daily` auto-picks V1 vs
V2 via `smart_select`'s `version_selector` unless you pass `--signal-version
v1|v2`. The chosen `param_set` + `signal_version` are recorded in both
`selected_param_set.json` and `inventory_aiss.json` for auditability.

---

## 0. First-time initialization (once)

```bash
set -a && source .env && set +a
ENV="conda run -n qlib_run --no-capture-output python -m semiconductor_strategy"

# Prices: 23 stocks + SOXX/SMH/SPY, full history (Polygon caps ~10y → 2016-05)
$ENV.data.aiss_fetch_prices --init --start 2016-01-01
# Company layer: CapEx pulse (yfinance) + MU DIO (SEC XBRL)
$ENV.data.company_signals  --init
# Industry layer: TSMC (TWSE), ASML 6-K bookings (SEC), DRAM proxy, PMI (FRED IPMAN YoY)
$ENV.data.industry_signals --init   # --init includes --update-pmi (needs FRED_API_KEY)

# Verify coverage (must be OK for 2019+ backtest window)
$ENV.data.aiss_fetch_prices --verify
$ENV.data.company_signals   --verify
$ENV.data.industry_signals  --verify
```

---

## 1. Production cadence (daily / weekly / monthly)

Mirrors SSRS's daily / weekly / monthly structure. All three production cadences
are NYSE-holiday-aware where it matters.

```bash
# ── DAILY (after US close) ──────────────────────────────────────────────
# (a) refresh data incrementally  (~1-3 min; skips sources updated recently)
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh update_data
# (b) generate today's signal + update inventory_aiss.json  (NYSE-aware)
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh daily

# ── DAILY BACKTEST (refresh smart_select's per-version P0 caches) ────────
# V1 select → V2 select → restore V1 as production + validate both.  (NYSE-aware)
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh daily_backtest

# ── WEEKLY (data/PIT health + Weekly Review + dry-run) ──────────────────
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh weekly

# ── MONTHLY (daily_backtest → restore V1 → force-rebalance) ─────────────
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh monthly   # NYSE-aware
```

**OpenClaw cron tasks** (run in isolated sessions, report to Telegram, alert on
failure). Staggered after SSRS (16:40 / 17:20 / Sun 01:00 ET) to avoid CPU
contention:

| Task | Schedule (ET) | Command |
|---|---|---|
| `aiss-daily-backtest` | weekdays 18:40 | `bash …/daily_backtest.sh` |
| `aiss-daily` | weekdays 19:20 | `bash …/semiconductor_pipeline.sh daily` |
| `aiss-weekly` | Sun 02:00 | `bash …/semiconductor_pipeline.sh weekly` |

> **2026-07-22 risk fixes:** live daily now feeds the DD circuit with REAL ledger
> equity (account_history snapshots; disable via risk.drawdown.live_dd_enabled=false;
> current DD -11.0% vs -25% threshold → inert today). The v2 Risk Overlay is now
> wired into the qlib production path too — but AISS config keeps it **enabled:false
> by design** (the SSRS-style MA gate would cap the high-beta exposure AISS needs).
>
> **Idempotency gate (2026-07-22):** `daily_backtest.sh` exits 0 in seconds if
> today's dated log already ends with AISS DAILY BACKTEST COMPLETE (protects
> against scheduler auto-retry after reporting-phase failures — the 2026-07-21
> double-run incident). Manual re-runs: pass `--force`. The skip message goes to
> stdout/cron log only; it never creates or appends a dated log.
>
> **Sandbox flags (2026-07-22):** test runs must use `AISSBatchRun
> --no-prod-write --output-dir /tmp/...` (guards selected_param_set.json + all
> Excel hard-writes) and `walk_forward --output-dir /tmp/...` (skips the WF
> diagnostic Excel). `--dry-run` daily no longer writes smart_select state.
> `trade_audit.py` is now import-safe (main() guard) and takes `--output-dir`.

> Engine path note (updated 2026-07-21): the qlib path (`_run_qlib` →
> `portfolio/strategy.py`) now runs **successfully** and is the live production
> path (stack-frame verified: all risk-control calls come from
> `AISSWeightStrategy.generate_trade_decision`); `_run_native` is the fallback.
> If you ever see `qlib backtest execution failed …, falling back to native
> loop`, that fallback is benign by design — not success-degraded.

**Outputs (all inside the package):**
```
trading_signals/aiss_daily_report_<date>_<ts>.{json,txt}   (subsector layer + stock layer)
inventory_aiss.json                 (+ inventory_history/ snapshots; records param_set/signal_version + stock_holdings)
selected_param_set.json             (updated by select / daily_backtest; restored to V1)
logs/aiss_<mode>_<YYYYMMDD_HHMMSS>.log   (timestamped — find latest with ls -t)
account_aiss.json                   (portfolio_ledger 账户：真实 cash/持仓/equity，恒等式校验)
account_history/account_aiss_YYYYMMDD.json      (每日账户快照)
trade_ledger_aiss.jsonl             (append-only 交易/分红/费用台账，realized 定格于成交日)
trading_signals/pnl_reports/pnl_report_YYYYMMDD.pdf        (每日 PnL 报告，daily signal 后自动生成)
trading_signals/risk_management/risk_report_YYYYMMDD.{json,txt,pdf} + risk_workbook_YYYYMMDD.xlsx
```

**Two-layer signal (one level deeper than SSRS):** SSRS trades 11 ETFs, so its
signal stops at the ETF level. AISS's "ETF level" is the **subsector** (a
synthetic 80/15/5 basket), so each `daily`/`dry-run` also emits the **individual-
stock execution layer** (`stock_decompose.py`): the report TXT gains
`STOCK-LEVEL TARGET HOLDINGS` + `STOCK TRADES` sections, the report JSON gains
`stock_holdings`/`stock_breakdown`/`stock_trades`, and the inventory gains
`stock_holdings`. PIT-correct (late-IPO tiers gated) and aggregated per ticker
(ARM in two subsectors → one order). Same logic as the backtest `*_stock_decomp`
Excel sheets.

Dry-run any time (no inventory write):
```bash
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh dry-run --force-rebalance
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh status   # current holdings
```

---

## 1b. Event-risk de-risk overlay (semi crash protection)

Extra de-risk layer on top of VIX (config switch, **ENABLED**). Triggers (either):
`max(SMH bottom-up β, portfolio_beta) > 2.5` AND an NFP within 2 trading days; OR
NVDA/AVGO earnings reaction-day close < −4.5%. On hit → **sell half the book → cash**
(`apply_risk_controls` event tier), held until SMH < −3% lifts it (T+3 cap).

```bash
# Enable (default off): config.yaml -> risk.event_derisk.enabled: true   (already on)
# Daily heartbeat (written every run, even no-trigger):
tail -n 20 qlib-main/semiconductor_strategy/trading_signals/event_risk_heartbeat.log   # + log lines [EVENT_DERISK]
# Shared data refresh (root, qlib_run; conductor runs it daily, non-fatal):
set -a && source .env && set +a
conda run -n qlib_run --no-capture-output python RefreshEventRiskData.py
# Safe test — zero production writes (real inputs, all outputs to a throwaway sandbox):
conda run -n qlib_run --no-capture-output python RunDailySignalSandbox.py aiss 2026-06-04
```

State persists in `inventory_aiss.json` (`event_derisk_active`, mirrors `emergency_mode_active`).
Detector: root `EventRiskDetector.py` (shared with MRPT/MTFS).

---

## 2. Validation — the win criterion (go/no-go)

```bash
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh validate
# or a specific param set:
conda run -n qlib_run python -m semiconductor_strategy.validate --param-set momentum_heavy --json /tmp/aiss_val.json
```
Exit code 0 = PASS (AISS beats SOXX **and** SMH on Sharpe **and** CAGR).

---

## 3. Research: batch, selection, walk-forward, tearsheet

```bash
# Rank all 39 param sets (CSV + per-set Excel in historical_runs/, equity cache)
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh batch

# Production selection (batch + WF OOS filter + MCPS) -> selected_param_set.json
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh select

# Walk-forward IS/OOS robustness (anchored + rolling)
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh walk-forward --mode both

# Single backtest of a named set
conda run -n qlib_run python -m semiconductor_strategy.AISSBatchRun --sets default momentum_heavy

# PDF tearsheet (includes AISS-vs-SOXX/SMH overlay page)
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh tearsheet
# -> report/output/tearsheet_<param>_v1_IS_<ts>.pdf
```

Param sets live in `AISSStrategyRuns.py` (groups A signal-weights, B concentration,
C vol-scaling, D VIX de-risk, E momentum window, F supply-chain external, G optimizer,
H archetypes, M single-factor). `list_param_sets()` prints them.

---

## 4. Data maintenance (granular)

```bash
ENV="conda run -n qlib_run python -m semiconductor_strategy"
$ENV.data.aiss_fetch_prices --update                 # incremental prices
$ENV.data.company_signals   --update-capex           # recompute CapEx pulse
$ENV.data.company_signals   --check-mu-dio           # new MU 10-Q?
$ENV.data.industry_signals  --check-tsmc             # new TWSE month?
$ENV.data.industry_signals  --check-asml             # new ASML 6-K?
$ENV.data.industry_signals  --update-dram            # recompute DRAM proxy
$ENV.data.industry_signals  --update-pmi             # new FRED IPMAN month (V2 graph pmi_proxy)
```

**V2 供应链图谱滞后标定**（手设滞后改为实证）：
```bash
# 在因子残差收益上重标定每条边的传导滞后 + 评估 logic_cpu 入边；
# 产出 backtest_results/graph_calibration_report.json + 可粘贴的 graph_config.edges
$ENV.signals.graph_calibration
# 采用：把报告里的 v2_graph_edges 写进 config.yaml 的 signals.supply_chain.graph_config.edges
# 回退：signals.supply_chain.graph_version: "v1"（用回硬编码图谱，代码逐位兼容）
```

PIT rule: every slow source stores its **availability date** (`filed_date` /
`filing_date` / `release_date`) and is read back only when `as_of >= that date`
(`data/aiss_pit.py`). No look-ahead.

### Corporate actions (stock splits)

Price sources retro-adjust full history after a split, but `inventory_aiss.json`
`stock_holdings` keep entry-era `shares`/`cost_basis`/`last_price` — unhandled,
a split fakes a massive MTM jump and corrupts the returns-based subsector
baskets (2026-06-12 KLAC 1:10: a 10x cliff at the incremental-merge boundary
would have fed a fake −90% daily return into the equipment basket).

Two automatic layers (shared root module `CorporateActions.py`, scope `aiss`):

1. **Inventory** — `AISSdailySignal.run_daily_signal()` step 0b calls
   `run_for('aiss')` before any price load / MTM: Polygon market-wide splits
   daily check (cached) → adjusts `shares`×factor, `cost_basis`÷factor,
   `last_price`÷factor (market value invariant), backs up inventory, appends
   an `applied_corporate_actions` audit record (polygon_id ⇒ idempotent).
   Non-fatal degrade if Polygon is unreachable.
2. **Price store self-heal** — `data/aiss_fetch_prices.update_ticker()`:
   if the 7-day overlap refetch disagrees with cached Close by >2% on the
   same date (retro-adjustment signature), the cache is discarded and the
   full history refetched, so no cliff can sit mid-series.

Status log (one line per check): `trading_signals/corporate_actions.log` —
`NO-ACTION-NEEDED` / `ALREADY-APPLIED` / `APPLIED` / `NO-POSITIONS` / `ERROR`
("check failed" is distinct from "no split found"; ERROR must be investigated).

```bash
# manual check (read-only)
conda run -n qlib_run python CorporateActions.py --strategy aiss --dry-run
```

---

## 5. Tests

```bash
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh test
# or: conda run -n qlib_run python -m pytest qlib-main/semiconductor_strategy/tests/ -v
```

---

## 6. config.yaml — key knobs

| Section | Key | Default | Note |
|---|---|---|---|
| signals.weights | cs_momentum / supply_chain / capex_pulse / cycle_regime | 0.30/0.35/0.25/0.10 | must sum to 1.0 |
| portfolio | top_n_sectors / constraints.max_weight | 3 / 0.55 | high conviction |
| portfolio.constraints | beta_min / beta_max | 0.40 / 3.00 | accept high semis beta |
| risk.vol_scaling | target_vol_annual | 0.30 | run near benchmark vol |
| risk.drawdown | cumulative_dd_halve | −0.25 | semis draw 20%+ normally |
| rebalance | emergency_derisk_vix | 36 | only true crises |
| signals.regime | vix_high / vix_extreme | 25 / 32 | regime tilt thresholds |
| backtest | start_date | 2019-01-01 | post-2018 IPO floor |

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `POLYGON_API_KEY`/`FRED_API_KEY` missing | `set -a && source .env && set +a` (pipeline does this automatically) |
| Price gaps / stale | `aiss_fetch_prices --update` (or `--init --force` to refetch) |
| TSMC has 1 month only | expected — TWSE forward-only; supply_chain uses foundry price proxy |
| `validate` FAIL | re-run `select` to refresh `selected_param_set.json`, or inspect `batch` ranking |
| `qlib backtest execution failed … falling back to native loop` | **Benign / expected** — native is the production engine (same as SSRS). Do NOT treat as failure or degraded. |
| V2 win-criterion FAIL | Expected — semi-monthly V2 is weaker than V1; production runs V1 (PASS). Not an error. |
| Regime always risk_on / macro stale | `price_data/macro/` is maintained by the someopark main pipeline; AISS falls back to live VIX |

---

## Relationship to SSRS

AISS is a `qlib_run` twin of `sector_rotation` (SSRS): same engine architecture,
different universe (semi subsectors vs GICS ETFs), different core signal
(supply-chain propagation vs P/E value), an explicit hurdle (beat SOXX & SMH),
and **one extra layer**: because the subsector is a synthetic basket (not a
directly-tradeable ETF), AISS adds an individual-stock execution layer
(`stock_decompose.py`) to its daily signal + inventory + backtest Excel.
SSRS-only files unused by AISS V1 were removed — see `DELETED_FILES.md`.
