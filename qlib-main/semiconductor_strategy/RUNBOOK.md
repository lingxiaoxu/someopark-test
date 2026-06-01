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
# Industry layer: TSMC (TWSE), ASML 6-K bookings (SEC), DRAM proxy
$ENV.data.industry_signals --init

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

> The benign `qlib backtest execution failed …, falling back to native loop`
> warning appears on every backtest in **both** AISS and SSRS — native is the
> production engine by design. It must **not** be counted as success-degraded.

**Outputs (all inside the package):**
```
trading_signals/aiss_daily_report_<date>_<ts>.{json,txt}
inventory_aiss.json                 (+ inventory_history/ snapshots; records param_set/signal_version)
selected_param_set.json             (updated by select / daily_backtest; restored to V1)
logs/aiss_<mode>_<YYYYMMDD_HHMMSS>.log   (timestamped — find latest with ls -t)
```

Dry-run any time (no inventory write):
```bash
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh dry-run --force-rebalance
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh status   # current holdings
```

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
# Rank all 33 param sets (CSV + per-set Excel in historical_runs/, equity cache)
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
```

PIT rule: every slow source stores its **availability date** (`filed_date` /
`filing_date` / `release_date`) and is read back only when `as_of >= that date`
(`data/aiss_pit.py`). No look-ahead.

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
(supply-chain propagation vs P/E value), and an explicit hurdle (beat SOXX & SMH).
SSRS-only files unused by AISS V1 were removed — see `DELETED_FILES.md`.
