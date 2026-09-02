# AEUS — AI Electric Utilities Strategy — Runbook

All commands run from the repo root `/Users/xuling/code/someopark-test/` and use
the **`qlib_run`** conda env with `.env` loaded (POLYGON_API_KEY, FRED_API_KEY;
EIA/ERCOT keys read from the same root `.env` by the data layer).

> **Isolation:** AEUS only writes inside `qlib-main/electric_utilities_strategy/`
> and *additively* under `price_data/elec_strategy/` (backtest Excel lands in
> `historical_runs/electric_utilities_strategy/`). It never touches
> `semiconductor_strategy/`, `sector_rotation/`, root scripts, or pre-existing
> data. `price_data/macro/`, the macro module's EIA mirror (`price_data/eia/`)
> and the ERCOT dashboard sqlite are read-only.

**Single entrypoint:**
```bash
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh <MODE> [opts]
```
(The pipeline auto-greps the API keys from `.env` and selects `qlib_run`.)

**15 modes:** `update_data` · `daily` · `weekly` · `monthly` · `dry-run` ·
`backtest` · `batch` · `select` · `daily_backtest` · `walk-forward` · `validate`
· `tearsheet` · `test` · `status` · `help`.

**NYSE holiday check:** `daily`, `monthly`, `daily_backtest` skip work + `exit 0`
on weekends/holidays (normal success, via `pandas_market_calendars`); `weekly` /
`dry-run` always run. Pass `--skip-holiday` to bypass (backfill / manual runs).
`--skip-holiday` is the only flag the shell parses itself — everything else is
forwarded verbatim to the Python entrypoints.

**V1 / V2:** V1 = monthly (production default); V2 = semi-monthly (1st +
~mid-month) with the same 4-factor / 12-1 signal. **Production runs V1
`pure_supply_chain`** (first full selection chain, 2026-08-30: both V1 and V2
chains independently picked it, WF OOS Sharpe 1.40). V2 is research-only for
now — `daily_backtest` refreshes its `_v2` P0 caches and always **restores V1
as production** at the end. The chosen `param_set` + `signal_version` are
recorded in both `selected_param_set.json` and `inventory_aeus.json` for
auditability.

---

## 0. First-time initialization (once)

```bash
set -a && source .env && set +a
ENV="conda run -n qlib_run --no-capture-output python -m electric_utilities_strategy"

# Prices: 41 weighted + 10 reserves + XLU/GRID/SPY, full history (2016-01 warm-up)
$ENV.data.aeus_fetch_prices --init --start 2016-01-01
# Company layer: CapEx pulse (yfinance) + utility/water/hyperscaler capex (SEC XBRL)
$ENV.data.company_signals  --init
# Industry layer: EIA retail sales + fuel mix, backlog RPO (SEC XBRL), gas proxy, IPUTIL
$ENV.data.industry_signals --init
# Altdata layer: daily demand, degree days, capacity, state prices, FRED altdata
$ENV.data.altdata_signals  --init
# ERCOT credentialed backfill (archive floor 2023-12)
$ENV.data.ercot_signals    --init

# Verify coverage (must be OK for the 2019+ backtest window)
$ENV.data.aeus_fetch_prices --verify
$ENV.data.company_signals   --verify
$ENV.data.industry_signals  --verify
$ENV.data.altdata_signals   --verify
$ENV.data.ercot_signals     --verify
$ENV.data.pjm_signals       --verify   # wired: all 7 PJM series must be present + fresh (hub, DOM basis, zone load YoY, reserve margin, forced outages, forecast error, shortage_east)
```

> `weekly` runs this same six-layer verify battery (prices / company / industry /
> altdata / ercot / pjm) — any `← STALE` flips the PIT DATA HEALTH FAILED banner.

---

## 1. Production cadence (daily / weekly / monthly)

Mirrors AISS's daily / weekly / monthly structure. All three production cadences
are NYSE-holiday-aware where it matters.

```bash
# ── DAILY (after US close) ──────────────────────────────────────────────
# (a) refresh data incrementally — 9 steps, each WARN-and-continue
#     (prices / capex pulse / utility+water capex / hyperscaler capex /
#      EIA monthly bundle / EIA daily demand + STEO degree days /
#      capacity + state price + FRED / ERCOT / PJM-gated)
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh update_data
# (b) generate today's signal + update inventory_aeus.json  (NYSE-aware;
#     daily itself re-runs a non-fatal update_data first)
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh daily

# ── DAILY BACKTEST (refresh smart_select's per-version P0 caches) ────────
# V1 select → V2 select → restore V1 as production + validate both.  (NYSE-aware)
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh daily_backtest

# ── WEEKLY (data/PIT health + Weekly Review + dry-run) ──────────────────
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh weekly

# ── MONTHLY (daily_backtest → restore V1 → force-rebalance daily) ───────
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh monthly   # NYSE-aware
```

**OpenClaw cron tasks** (run in isolated sessions, report to Telegram, alert on
failure). Fully staggered after both SSRS and AISS (17:55 / 19:00 / Sun 02:00 ET)
to avoid CPU / Polygon rate-limit contention. Full copy-paste payloads live in
`AEUS_CRON_PAYLOADS.md`:

| Task | Schedule (ET) | Command |
|---|---|---|
| `aeus-daily-backtest` | weekdays 19:10 | `bash …/daily_backtest.sh` |
| `aeus-daily` | weekdays 20:20 | `bash …/aeus_pipeline.sh daily` |
| `aeus-weekly` | Sun 03:30 | `bash …/aeus_pipeline.sh weekly` |

> **Idempotency gate (inherited from the AISS 2026-07-21 double-run incident):**
> `daily_backtest.sh` exits 0 in seconds if today's dated log already ends with
> AEUS DAILY BACKTEST COMPLETE (protects against scheduler auto-retry after
> reporting-phase failures). Manual re-runs: pass `--force`. The skip message
> goes to stdout/cron log only; it never creates or appends a dated log.
>
> **Weekly health gate:** `weekly` runs the data/PIT verifies; any stale series
> is marked `← STALE` and the run prints an explicit **FAILED** banner keyword
> (the cron agent keys off it) — the pipeline still completes the review +
> dry-run so you get a full picture.
>
> **Sandbox flags:** test runs must use `AEUSBatchRun --no-prod-write
> --output-dir /tmp/...` (guards selected_param_set.json + all Excel
> hard-writes) and `walk_forward --output-dir /tmp/...` (skips the WF
> diagnostic Excel). `--dry-run` daily does not write smart_select state.

> Engine path note (**corrected 2026-09-02**): the **qlib path
> (`_run_qlib` → `portfolio/strategy.py`) is the production engine** — it runs,
> it does not "consistently raise and fall back". Measured: with the exposure
> amplifier hooked only into `engine._run_native`, a full batch was
> byte-identical ON vs OFF (the native hook never executed); after hooking
> `portfolio/strategy.py`, the same batch changed and the amplifier fired at 61
> rebalances. Any risk/sizing change must therefore be wired into
> `portfolio/strategy.py` (both `generate_trade_decision` and
> `generate_target_weight_position`), and — for parity — into
> `engine._run_native` (the fallback) and `backtest/trade_audit.py` (the replay).
> If you do see `qlib backtest execution failed …, falling back to native loop`,
> the fallback is benign: the native loop carries the same hooks.

**Outputs (all inside the package):**
```
trading_signals/aeus_daily_report_<date>_<ts>.{json,txt}   (subsector layer + stock layer)
inventory_aeus.json                 (+ inventory_history/ snapshots; records param_set/signal_version + stock_holdings)
selected_param_set.json             (updated by select / daily_backtest; restored to V1)
logs/aeus_<mode>_<YYYYMMDD_HHMMSS>.log   (timestamped — find latest with ls -t)
backtest_results/                   (batch CSVs, P0 caches param_oos_by_regime{_v1,_v2}.json,
                                     graph_calibration_report.json, weekly_review*.json)
../../historical_runs/electric_utilities_strategy/   (33-sheet portfolio + WF diagnostic Excel)
report/output/*.pdf                 (tearsheets)
```
Account / trade-ledger / PnL-report outputs arrive with go-live wiring (§7):
until the portfolio_ledger `'aeus'` registration lands, the ledger call degrades
to nominal capital with a daily WARNING — expected, see Troubleshooting.

**Two-layer signal (one level deeper than SSRS, inherited from AISS):** AEUS's
"ETF level" is the **subsector** (a synthetic N-tier `base_w` basket — AISS's
80/15/5 is the 3-tier special case), so each `daily`/`dry-run` also emits the
**individual-stock execution layer** (`stock_decompose.py`): the report TXT
gains `STOCK-LEVEL TARGET HOLDINGS` + `STOCK TRADES` sections, the report JSON
gains `stock_holdings`/`stock_breakdown`/`stock_trades`, and the inventory
gains `stock_holdings`. PIT-correct (late-IPO members gated at 24 months, with
proportional renormalization) and aggregated per ticker across subsectors. With
`purity_tilt.kappa > 0` the within-basket weights tilt dynamically (κ=0 config
default = bit-identical AISS static behavior). Same logic as the backtest
`*_stock_decomp` Excel sheets.

Dry-run any time (no inventory write):
```bash
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh dry-run --force-rebalance
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh status   # current holdings
```

---

## 1b. Event-risk de-risk overlay (phase-1 **DISABLED**)

The AISS-validated event de-risk layer (NFP window + high beta, or bellwether
earnings crash → sell half to cash) is fully carried in the AEUS codebase but
**switched off for phase-1**: `config.yaml` → `risk.event_derisk.enabled: false`.

Why: the shared root detector (`EventRiskDetector.py` / `RefreshEventRiskData.py`)
reads the **semiconductor** universe files; an electric-utilities event universe
+ detector parameterization means touching shared root files = **C-level wiring**
that needs separate approval (AEUS_PLAN §6C). The daily-mode event-risk refresh
step is commented out (TODO) so AEUS never refreshes another strategy's data.

- Parameters preserved as-is for wiring day: `beta_threshold` 2.5 ·
  `nfp_window_days` 2 · `bellwether_drop` −0.045 · `sell_frac` 0.5 ·
  `beta_mode` bottomup
- When approved: restore the daily refresh step + flip `enabled: true` — the
  execution machinery (`reduce_next_open` → `event_derisk` rebalance branch) is
  the same AISS code path, no new code needed.
- Until then: do **not** expect `[EVENT_DERISK]` log lines or an
  `event_risk_heartbeat.log` for AEUS; their absence is by design, not a bug.

---

## 2. Validation — the win criterion (go/no-go)

```bash
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh validate
# or a specific param set / version:
conda run -n qlib_run python -m electric_utilities_strategy.validate --param-set pure_supply_chain --signal-version v1
```
Exit code 0 = PASS (AEUS beats XLU **and** GRID on Sharpe **and** CAGR).
`validate` also reports active return / IR vs the 50/50 XLU+GRID daily-rebalanced
blend as context (not part of the pass/fail gate).

---

## 3. Research: batch, selection, walk-forward, tearsheet

```bash
# Rank all 42 param sets (CSV + per-set Excel in historical_runs/, equity cache)
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh batch

# Production selection (batch + WF OOS filter + MCPS) -> selected_param_set.json
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh select

# Walk-forward IS/OOS robustness (anchored + rolling)
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh walk-forward --mode both

# Single backtest of a named set
conda run -n qlib_run python -m electric_utilities_strategy.AEUSBatchRun --sets default pure_supply_chain

# PDF tearsheet (includes AEUS-vs-XLU/GRID overlay page)
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh tearsheet
# -> report/output/tearsheet_<param>_v1_IS_<ts>.pdf
```

Param sets live in `AEUSStrategyRuns.py` — **42 = the AISS 39-group grid + Group
N** (A signal-weights, B concentration, C vol-scaling/semivol/DD-release, D VIX
de-risk, E momentum window, F supply-chain external, G optimizer, H archetypes,
M single-factor, **N purity-tilt κ 0/0.3/0.5 — AEUS-only, walk-forward decides**).
`list_param_sets()` prints them.

---

## 4. Data maintenance (granular)

```bash
ENV="conda run -n qlib_run python -m electric_utilities_strategy"
$ENV.data.aeus_fetch_prices --update                     # incremental prices
$ENV.data.company_signals   --update-capex               # hyperscaler CapEx pulse (yfinance)
$ENV.data.company_signals   --update-utility-capex       # NEE/DUK/SO capex (XBRL)
$ENV.data.company_signals   --update-water-capex         # AWK capex (XBRL)
$ENV.data.company_signals   --update-hyperscaler-capex   # real hyperscaler capex (XBRL)
$ENV.data.industry_signals  --update-elec-gen            # EIA retail sales
$ENV.data.industry_signals  --update-fuel-mix            # EIA generation fuel mix
$ENV.data.industry_signals  --update-backlog             # RPO backlog (GEV/PWR/EMR/ETN 10-Q)
$ENV.data.industry_signals  --update-gas                 # gas price proxy (+ EIA storage blend)
$ENV.data.industry_signals  --update-pmi                 # FRED IPUTIL
$ENV.data.altdata_signals   --update-fred                # transformer PPI / CPI / construction jobs
$ENV.data.altdata_signals   --update-demand              # EIA daily RTO demand
$ENV.data.altdata_signals   --update-dd                  # STEO CDD/HDD (weather-adjust input)
$ENV.data.altdata_signals   --update-capacity            # EIA-860M capacity
$ENV.data.altdata_signals   --update-state-price         # DC-heavy state price premium
$ENV.data.altdata_signals   --snapshot-gpu               # GPU snapshot (forward-only)
$ENV.data.ercot_signals     --update                     # ERCOT DAM SPP + AS
$ENV.data.pjm_signals       --update                     # PJM: hub LMP + 5 extended feeds (incremental; exit 1 only if hub fails or ≥2 extended feeds return nothing)
```
Frozen stores (capex-pulse / gas family) need `--refreeze` for a full rebuild —
routine `--update-*` is append-only and will not rewrite history.

**V2 供应链图谱滞后标定**（手设滞后改为实证）：
```bash
# 在因子残差收益上重标定每条边的传导滞后 + 评估候选边;
# 产出 backtest_results/graph_calibration_report.json + 可粘贴的 graph_config.edges
$ENV.signals.graph_calibration
# 现状:28 条 v2 边 = 23 先验 + 5 候选全保留(首轮校准),滞后全部 IC-argmax 实证
# 采用:把报告里的 v2_graph_edges 写进 config.yaml 的 signals.supply_chain.graph_config.edges
# 回退:signals.supply_chain.graph_version: "v1"(用回硬编码图谱,代码逐位兼容)
```

PIT rule: every slow source stores its **availability date** (`filed_date` /
`release_date` / EPM publication calendar) and is read back only when
`as_of >= that date`; `merge_frozen()` appends only dates `> max(existing)`
(`data/aeus_pit.py`) — history is never rewritten, no look-ahead.

### Corporate actions (stock splits)

Price sources retro-adjust full history after a split, but inventory
`stock_holdings` keep entry-era `shares`/`cost_basis`/`last_price` — unhandled,
a split fakes a massive MTM jump and corrupts the returns-based subsector
baskets (the AISS 2026-06-12 KLAC 1:10 lesson).

Two layers, shared root module `CorporateActions.py`:

1. **Inventory** — `run_for('aeus')` is registered; pre-go-live there is no
   inventory to adjust, so the daily call **degrades loudly** (WARNING, never
   silently skips). Watch split windows manually until go-live seeds real
   positions; after go-live the AISS behavior applies (shares×factor,
   cost_basis÷factor, market value invariant, audit record, idempotent).
2. **Price store self-heal** — `data/aeus_fetch_prices.update_ticker()`: if the
   7-day overlap refetch disagrees with cached Close by >2% on the same date
   (retro-adjustment signature), the cache is discarded and the full history
   refetched (weekly refreshes the whole universe incl. XLU/GRID benchmarks).

```bash
# manual check (read-only)
conda run -n qlib_run python CorporateActions.py --strategy aeus --dry-run
```

---

## 4b. PJM(2026-09-01 接线,09-02 扩展)

已完成:`.env` 有 `PJM_API_KEY`;`config.yaml` `external_sources.pjm.enabled: true`、`extended: true`;
`--init` 已回填。日常由 `update_data` 第 9 步 `--update` 增量,weekly 六层体检里 `--verify` 判时效。

**六个店**(`price_data/elec_strategy/altdata/pjm_*.json`,全部 PIT 冻结 append-only,internal-use-only,绝不 commit):

| 店 | feed | 日频聚合 | 可得性 | 进哪里 |
|---|---|---|---|---|
| `pjm_da_lmp` | da_hrl_lmps pnode 51288(西枢纽)| 日均 $/MWh,2016+ | 当日 | price_pulse z 均值 |
| `pjm_dom_lmp` | da_hrl_lmps pnode 34964545(DOM 区)| 日均;基差 = DOM − 西枢纽 | 当日 | price_pulse z 均值 |
| `pjm_zone_load` | hrl_load_metered DOM/PEPCO/BC/AEP×4 + RTO | 日 MWh(仅 ≥23 小时的整日)| **+12 天**(计量滞后 ~10-11 天)| power_demand 节点 z 均值(28d 均值 YoY)|
| `pjm_gen_capacity` | day_gen_capacity | (eco_max−committed)/eco_max 日**最小** | 当日 | shortage_east(取负)|
| `pjm_gen_outages` | gen_outages_by_type,forecast_date==执行日 | PJM RTO 与 Dominion 的 forced/total MW | 当日 | shortage_east(RTO forced)|
| `pjm_load_forecast` | load_frcstd_hist RTO/DOM,运行日前最后一次评估 | 24h 合计 MWh | 与计量对齐 +12 天 | 预报误差 = \|预报−计量 RTO\|/计量 30d MAPE → shortage_east |

要点:
- **存档墙**:非会员 Data Miner 2 对 >~731 天的过滤查询返回 400/0 行,扩展 feed 自 `EXT_START=2024-09-15` 起;
  z252 预热后约 2025-10 起可用;之前的 composite 混合自动只含既有腿(历史不变)。已有数据永不因时间流逝丢失(append-only)。
- **不新增 tilt**:ipp_wholesale 已满 2 条;新序列全部进既有 z 均值(price_pulse / demand 节点 / shortage_score)。
- **退回**:`extended: false` → 仅西枢纽腿,与 09-01 接线逐字节等价。
- **退出码**:`--update` 在 wired 下西枢纽 0 行 → exit 1;≥2 个扩展 feed 0 行 → exit 1;单个 miss 只 WARN(weekly `--verify` 兜底时效)。
- 测试:`tests/test_pjm_extended.py`(tmp_path 店 + 注入 fetch,零网络零生产写入)。

---

## 5. Tests

```bash
bash qlib-main/electric_utilities_strategy/aeus_pipeline.sh test
# or: conda run -n qlib_run python -m pytest qlib-main/electric_utilities_strategy/tests/ -v
```
173 pytest tests, fully offline (synthetic data, no network). Deeper QA:
```bash
# 42 param sets × V1/V2 full-grid backtest audit
conda run -n qlib_run python -m electric_utilities_strategy.tests.aeus_matrix
# 6-stage end-to-end integration QA (~5 min with --quick)
bash qlib-main/electric_utilities_strategy/tests/test_pipeline_integration.sh --quick
```

---

## 6. config.yaml — key knobs

| Section | Key | Default | Note |
|---|---|---|---|
| signals.weights | cs_momentum / supply_chain / capex_pulse / cycle_regime | 0.30/0.35/0.25/0.10 | must sum to 1.0 |
| signals.supply_chain | graph_version | v2 | v2 = 28 calibrated edges; v1 = hardcoded prior graph |
| signals.purity_tilt | kappa / clip | 0.0 / 0.40 | κ=0 = AISS static regression anchor; Group N probes 0.3/0.5 |
| portfolio | top_n_sectors / constraints.max_weight | 3 / 0.55 | high conviction (10 pick 3) |
| portfolio.constraints | beta_min / beta_max | 0.40 / 3.00 | allow defensive low beta; don't force high beta to 1.0 |
| risk.vol_scaling | target_vol_annual | 0.30 | AISS starting value; Group C sweeps 0.18–0.40 |
| risk.drawdown | cumulative_dd_halve | −0.25 | halve-book line |
| risk.event_derisk | enabled | **false** | phase-1 off (C-level wiring pending, §1b) |
| rebalance | emergency_derisk_vix | 36 | only true crises |
| signals.regime | vix_high / vix_extreme | 25 / 32 | regime tilt thresholds |
| external_sources | eia / ercot / pjm / sec | true / true / **true** / true | PJM wired 2026-09-01; `pjm.extended: true` (§4b) |
| backtest | start_date | 2019-01-01 | late-IPO floor |

---

## 7. Go-live 拼接(§2.6,建仓日执行)

1. 用**实际交易 param** 的当日 vintage 生成固定段 → 冻入 `backtest_results/aeus_splice_freeze.json`;
2. 定死 `AEUS_LIVE_START` = 建仓日;account_aeus $1,000,000 起账;
3. 外部接线逐项请示(ledger/CorporateActions 各一行注册、master 五件套、QC 注资 K、前端四件套 —— 见 AEUS_PLAN §6);
4. QC 挂载(同日,建仓落盘后):
   `python trading_quantconnect/ops/onboard_aeus.py`(先 --dry-run 看数)
   → scalar_aeus = 官方/账本(≈1.1588)写入 exporter state(五策略常数不动)
   → 按打印的 K(≈$1,158,818)在 QC 侧 CashBook 显式 deposit
   → exporter 常驻循环自动建仓 = 账本股数 × scalar;
   不变量:QC aeus 市值+现金 ≡ 官方口径 ≡ NAV 面板头条(M4 对账基准)。

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `POLYGON_API_KEY`/`FRED_API_KEY` missing | `set -a && source .env && set +a` (pipeline greps them automatically) |
| `LEDGER … 名义 capital` WARNING every daily | **Expected pre-wiring** — portfolio_ledger `'aeus'` registration is C-level, lands at go-live (§7); disappears after wiring |
| update_data step WARN | Single-step failures never block the other 8; find the WARN line in the log — the affected tilt goes graceful-0, price/momentum main chain unaffected |
| Price gaps / stale | `aeus_fetch_prices --verify` and look for `!!` rows; `--update` (or `--init --force` refetch) |
| Renamed / delisted member (SJW precedent) | SJW (water_cooling reserve) was acquired 2025-05 → replaced by YORW; a dead **weighted** member cascades its weight to the reserve automatically (`effective_weights`) — verify prices, then update the universe |
| EIA / ERCOT return empty | Check keys in root `.env` (EIA_API_KEY, ERCOT_API_*); ERCOT token auto-refreshes — repeated failures mean check the subscription |
| ERCOT tilt is 0 before 2024 | Expected — the ERCOT archive floor is 2023-12, pre-2024 the tilt is graceful-0, not an error |
| Long background job silently vanishes | macOS kills detached background processes — run long jobs in the foreground or via a cron session (same as AISS) |
| `validate` FAIL | Check which benchmark/metric failed; `select` auto-rotates params — only intervene on repeated FAILs |
| `qlib backtest execution failed … falling back to native loop` | **Benign** — the native loop carries the same risk/sizing hooks as the qlib path. Not a failure. (Normal production runs use the **qlib** path; see the engine path note above, corrected 2026-09-02.) |
| V2 win-criterion FAIL | V2 is research-only in phase-1; production runs V1 (PASS). Not an error. |
| Regime always risk_on / macro stale | `price_data/macro/` is maintained by the someopark main pipeline; `AEUSdailySignal` self-heals once then degrades non-fatally |

Logs: `logs/aeus_<mode>_YYYYMMDD_HHMMSS.log` (timestamped like AISS — latest via
`ls -t`). Output matrix mirrors AISS: daily reports in `trading_signals/`, batch
in `backtest_results/`, Excel in `historical_runs/electric_utilities_strategy/`,
PDFs in `report/output/`.

---

## Relationship to AISS

AEUS is a full-directory `qlib_run` clone-twin of `semiconductor_strategy`
(AISS, cloned 2026-08-30): same engine architecture (native loop,
smart_select/MCPS, walk-forward, portfolio_record, win-criterion, V1/V2 dual
track), different universe (10 AI-power subsectors vs 8 semi subsectors), a
rebuilt core signal (electric-power knowledge-graph propagation vs semi
supply-chain graph), and an explicit hurdle (beat XLU & GRID). Both strategies
share the same top-of-chain driver — the 4 hyperscalers' AI capex. AEUS extends
AISS in three places: **N-tier base_w baskets** (80/15/5 becomes a special
case), **purity tilt** (Group N, κ=0 bit-identical anchor), and the **9-step
electric-power altdata spectrum** (EIA / ERCOT / PJM) in `update_data`.
Design doc: `AEUS_PLAN.md`; cron payloads: `AEUS_CRON_PAYLOADS.md`.
