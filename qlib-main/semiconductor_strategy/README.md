<h1 align="center">AISS — AI Infra &amp; Semiconductor Strategy</h1>
<p align="center"><b>Active semiconductor sub-sector rotation that aims to beat SOXX and SMH</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/conda-qlib__run-green?logo=anaconda&logoColor=white"/>
  <img src="https://img.shields.io/badge/universe-8%20subsectors%20%C3%97%203%20stocks-orange"/>
  <img src="https://img.shields.io/badge/rebalance-monthly-purple"/>
  <img src="https://img.shields.io/badge/benchmarks-SOXX%20%7C%20SMH%20%7C%20SPY-teal"/>
</p>

---

> **The only reason AISS exists is to be better than simply holding SOXX or SMH.**
> The validated default config beats **both** on Sharpe **and** CAGR **and** drawdown:
>
> | (2019-01 → 2026-05) | CAGR | Vol | Sharpe | MaxDD |
> |---|---|---|---|---|
> | **AISS (default)** | **45.7%** | 29.6% | **1.42** | **−36.8%** |
> | SOXX | 41.5% | 36.4% | 1.14 | −44.8% |
> | SMH | 44.3% | 35.4% | 1.21 | −45.3% |
> | SPY | 17.9% | 19.5% | 0.94 | −33.7% |
>
> Run `bash semiconductor_pipeline.sh validate` to reproduce the verdict.

---

## Isolation principle

- AISS runs **only** in the `qlib_run` conda env (the twin of SSRS / `sector_rotation`).
- It **only writes inside this directory** plus *additive* files under
  `price_data/semi_strategy/`. It never modifies `sector_rotation/`, the root
  scripts, or any pre-existing data file.
- Macro data (`price_data/macro/`) is **read-only** (maintained by the someopark
  main pipeline). AISS degrades to live VIX if macro is stale.

---

## Strategy

8 semiconductor **subsectors**, each a fixed **80 / 15 / 5** basket of 3 stocks.
The tradeable asset is the subsector (a synthetic total-return basket), so the
engine rotates *between* subsectors monthly while holding fixed proportions
*within* each. 23 unique stocks + SOXX/SMH/SPY benchmarks.

| Subsector | primary 80% | backup1 15% | backup2 5% | cycle role |
|---|---|---|---|---|
| AI / GPU | NVDA | ALAB | ARM | AI-capex direct |
| Custom ASIC / Net | AVGO | MRVL | CRDO | lags GPU 1–3m |
| Equipment | KLAC | LRCX | AMAT | leads cycle 9–18m |
| Memory / HBM | MU | WDC | SIMO | inventory cycle |
| Foundry | TSM | UMC | GFS | leads fabless 6–12m |
| Analog / Defensive | TXN | ADI | MCHP | late-cycle defensive |
| Logic / CPU | AMD | INTC | ARM | AI-server, lags GPU |
| RF / Edge | QCOM | SWKS | QRVO | consumer, AI-independent |

Late IPOs (ARM 2023, ALAB 2024, CRDO 2022, GFS 2021) phase into their basket
only after 24 months of history (PIT-correct; the primary anchors meanwhile).

### Four-factor signal (monthly, regime-conditioned)

| Factor | Weight | What it is |
|---|---|---|
| `cs_momentum` | 0.30 | 12-1 month cross-sectional momentum of the subsector baskets |
| **`supply_chain`** | **0.35** | **Knowledge-graph propagation** — each subsector scored by lagged upstream drivers (the core AISS alpha; `signals/supply_chain.py`) |
| `capex_pulse` | 0.25 | Hyperscaler AI-CapEx pulse (MSFT/GOOGL/META/AMZN 3M momentum z-score) tilted by subsector beta |
| `cycle_regime` | 0.10 | VIX + CapEx regime tilt (defensive vs AI-cycle) |

Regime: 4-state (risk_on / transition_up / transition_down / risk_off) with
AISS-tightened VIX thresholds (25 / 32) that re-weight the four factors and add a
defensive bonus to Analog in risk-off.

### Portfolio & risk (high-conviction satellite)
top-3 subsectors, max-weight 0.55, inverse-vol optimizer, **30% vol target**,
beta 0.40–3.00 (the semis beta ~2.5 is *accepted*, not fought), monthly rebalance,
graduated VIX de-risk (28→10% / 32→25% cash), −25% drawdown halver, 3 extreme stop-losses.

---

## Data layer (PIT, backfilled, production-incremental)

All isolated under `price_data/semi_strategy/`:

| Source | Path | Backfill | Daily/Prod update | PIT field |
|---|---|---|---|---|
| Prices (23 stocks + SOXX/SMH/SPY) | `prices/*.parquet` | Polygon `--init` | `--update` | trading day |
| Subsector baskets | (derived) | — | — | — |
| CapEx pulse | `company/capex_pulse.json` | yfinance | `--update-capex` | same day |
| MU DIO | `company/mu_dio_proxy.json` | SEC XBRL (CIK 723125) | `--check-mu-dio` | 10-Q `filed` |
| ASML net bookings | `industry/asml_quarterly_orders.json` | SEC 6-K (CIK 937966) | `--check-asml` | filing/acceptance |
| TSMC revenue YoY | `industry/tsmc_monthly_revenue.json` | TWSE OpenAPI (forward-only) | `--check-tsmc` | TWSE release |
| DRAM proxy | `industry/dram_spot_proxy.json` | MU/SOXX RS | `--update-dram` | same day |
| Macro (VIX, spreads…) | `price_data/macro/` (read-only) + FRED | — | — | FRED lags |

Initialize once:
```bash
set -a && source .env && set +a
conda run -n qlib_run python -m semiconductor_strategy.data.aiss_fetch_prices --init --start 2016-01-01
conda run -n qlib_run python -m semiconductor_strategy.data.company_signals  --init
conda run -n qlib_run python -m semiconductor_strategy.data.industry_signals --init
```

> External-data realism: TSMC deep history is not freely backfillable (TWSE only
> exposes the current month), so `supply_chain` uses the foundry price-momentum
> proxy when TSMC YoY is unavailable for a date — the V1 design. ASML/MU-DIO/DRAM
> have real backfilled history.

---

## Pipeline (single production entrypoint)

```bash
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh <MODE>
```

| Mode | Action |
|---|---|
| `update_data` | Incremental refresh of all data sources (cron daily, pre-close-after) |
| `daily` | Generate today's target weights + update `inventory_aiss.json` |
| `dry-run` | Daily signal without writing inventory |
| `backtest` | Single full backtest of the active param set |
| `batch` | Backtest all 33 param sets → ranked CSV/Excel |
| `select` | Batch + walk-forward OOS + MCPS → `selected_param_set.json` |
| `walk-forward` | IS/OOS robustness (anchored + rolling) |
| `validate` | Backtest vs SOXX/SMH/SPY + PASS/FAIL verdict |
| `tearsheet` | PDF report (with the SOXX/SMH overlay page) |
| `test` | pytest suite (offline) |

Typical cron: `update_data` then `daily` after the US close; `select` monthly.

---

## File structure

```
semiconductor_strategy/
├── config.yaml                 all tunable parameters (AISS-tuned)
├── semiconductor_pipeline.sh   production entrypoint
├── validate.py                 win-criterion gate (vs SOXX/SMH)
├── AISSdailySignal.py          daily signal + inventory
├── AISSBatchRun.py             batch / select
├── AISSStrategyRuns.py         33 named param sets (groups A–H, M)
├── walk_forward.py             IS/OOS analyzer
├── smart_select.py             daily macro-conditioned param selection
├── data/   universe · loader · aiss_fetch_prices · aiss_fetch_sec_data
│           · aiss_pit · company_signals · industry_signals
├── signals/ momentum · supply_chain · regime · composite · risk_overlay
├── portfolio/ optimizer · risk · rebalance · stop_loss · strategy
├── backtest/ engine · metrics · costs · qlib_adapter · robustness · …
├── report/ plots · tearsheet
├── tests/  pytest (offline, synthetic)
└── DELETED_FILES.md            SSRS files removed as unused in AISS V1
```

See [RUNBOOK.md](RUNBOOK.md) for operations. All commands use `conda run -n qlib_run`.
