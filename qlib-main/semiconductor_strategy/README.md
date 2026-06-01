<p align="center">
  <img src="../../public/SOMEO PARK矢量源文件 Big Square.svg" alt="Someopark" width="120"/>
</p>

<h1 align="center">AI Infra &amp; Semiconductor Strategy (AISS)</h1>
<p align="center"><b>Institutional-grade semiconductor sub-sector rotation that aims to beat SOXX &amp; SMH — powered by qlib</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/conda-qlib__run-green?logo=anaconda&logoColor=white"/>
  <img src="https://img.shields.io/badge/universe-8%20subsectors%20%C3%97%2023%20stocks-orange"/>
  <img src="https://img.shields.io/badge/rebalance-monthly%20(V1)%20%7C%20semi--monthly%20(V2)-purple"/>
  <img src="https://img.shields.io/badge/direction-long--only-teal"/>
  <img src="https://img.shields.io/badge/benchmarks-SOXX%20%7C%20SMH%20%7C%20SPY-lightgrey"/>
</p>

---

> **隔离原则**：本策略只使用 `qlib_run` conda 环境，**绝不调用 `someopark_run`**。
> 它**只写入本目录**，外加 `price_data/semi_strategy/` 下的*增量*文件；从不修改 `sector_rotation/`、根脚本或任何已有数据文件。
> `price_data/macro/` parquets 为**只读**（someopark 主 pipeline 写入），macro 失效时降级为实时 VIX。

---

> **AISS 存在的唯一理由：跑赢简单持有 SOXX 或 SMH。**
> 已验证的 default 配置在 Sharpe **和** CAGR **和** 回撤上**同时**优于两者：
>
> | (2019-01-02 → 2026-05-29) | CAGR | Vol | Sharpe | Calmar | MaxDD |
> |---|---|---|---|---|---|
> | **AISS (default)** | **44.9%** | 30.9% | **1.36** | **1.22** | **−36.8%** |
> | SOXX | 41.5% | 36.4% | 1.14 | 0.93 | −44.8% |
> | SMH | 44.3% | 35.4% | 1.21 | 0.98 | −45.3% |
> | SPY | 17.9% | 19.5% | 0.94 | 0.53 | −33.7% |
>
> `bash semiconductor_pipeline.sh validate` 可复现该裁决（exit 0 = PASS）。

---

## 策略概览

跨 8 个半导体**子板块**进行多因子轮动。每个子板块是一个固定 **80 / 15 / 5** 的三只股票合成篮子（总收益篮子），可交易资产是子板块本身——引擎按月在子板块**之间**轮动，篮子**内部**保持固定比例。共 23 只独立股票 + SOXX / SMH / SPY 基准。

> **比 SSRS 多一层（个股执行层）**：SSRS 直接交易 11 个 ETF，日度信号停在 ETF 层即可。AISS 的"ETF 层"是 subsector（合成篮子，不可直接下单），所以日度信号**多一步**——把 subsector 目标权重按 80/15/5（PIT 正确，晚期 IPO 自动剔除并重归一）分解到**真实个股持仓/订单**,并**按 ticker 跨子板块聚合**(如 ARM 同属 ai_gpu 与 logic_cpu → 合并成一个订单)。回测端对应 Excel 的 7 个 `*_stock_decomp` sheet,实盘端对应日报的股票层 + inventory 的 `stock_holdings`(见"输出"与 `stock_decompose.py`)。

| 维度 | 设计 |
|---|---|
| 标的池 | 8 子板块：ai_gpu · custom_asic · equipment · memory_hbm · foundry · analog_defense · logic_cpu · rf_edge |
| 基准 | SPY（beta / 信息比率）；**SOXX & SMH = 胜负门槛（必须同时跑赢）** |
| 回测起点 | 2019-01-01（晚期 IPO 地板：ARM/ALAB/CRDO/GFS 上市后 24 个月才进篮） |
| 调仓频率 | V1 每月首个交易日（生产默认） / V2 半月度（1 日 + ~月中） |
| 方向 | 纯多头，无做空，基础配置无杠杆 |
| 胜负门槛 | 在 Sharpe **且** CAGR 上同时跑赢 SOXX **和** SMH |

### 子板块篮子（80 / 15 / 5）

| 子板块 | primary 80% | backup1 15% | backup2 5% | 周期角色 |
|---|---|---|---|---|
| AI / GPU | NVDA | ALAB | ARM | AI-capex 直接受益 |
| Custom ASIC / Net | AVGO | MRVL | CRDO | 滞后 GPU 1–3 月 |
| Equipment | KLAC | LRCX | AMAT | 领先周期 9–18 月 |
| Memory / HBM | MU | WDC | SIMO | 库存周期 |
| Foundry | TSM | UMC | GFS | 领先 fabless 6–12 月 |
| Analog / Defensive | TXN | ADI | MCHP | 晚周期防御 |
| Logic / CPU | AMD | INTC | ARM | AI-server，滞后 GPU |
| RF / Edge | QCOM | SWKS | QRVO | 消费电子，AI-无关 |

> 晚期 IPO（ARM 2023、ALAB 2024、CRDO 2022、GFS 2021）只有在累计 24 个月历史后才并入篮子（PIT 正确；在此之前由 primary 锚定）。

### 信号架构（4 因子，月度，Regime 条件）

| 信号 | 权重 | 方法 |
|---|---|---|
| 横截面动量（`cs_momentum`） | 0.30 | 子板块篮子 12-1 月相对回报，横截面 z-score |
| **供应链（`supply_chain`）** | **0.35** | **知识图谱传播** —— 每个子板块由滞后的上游驱动因子打分（**AISS 核心 alpha**；`signals/supply_chain.py`） |
| CapEx 脉冲（`capex_pulse`） | 0.25 | 超大规模云厂商 AI-CapEx 脉冲（MSFT/GOOGL/META/AMZN 3 月动量 z-score）按子板块 beta 倾斜 |
| 周期 Regime（`cycle_regime`） | 0.10 | VIX + CapEx regime 倾斜（防御 vs AI 周期） |

> 与 SSRS 的根本区别：SSRS 用相对估值（成分股 P/E）作为第三支柱；**AISS 用供应链知识图谱传播**——这是 AISS 独有的 alpha 来源。

### Regime 四态定义

4 态（risk_on / transition_up / transition_down / risk_off），AISS 收紧的 VIX 阈值（25 / 32），动态修正四个因子权重并在 risk-off 时给 Analog 加防御分。

| 状态 | `cs_mom` | `supply_chain` | `capex_pulse` | `cycle_regime` | 说明 |
|---|---|---|---|---|---|
| `risk_on` | 1.1 | 1.1 | 1.2 | 0.8 | 强化进攻因子 |
| `transition_up` | 1.1 | 1.1 | 1.1 | 0.9 | 温和进攻 |
| `transition_down` | 0.8 | 0.9 | 0.7 | 1.3 | 压制 capex，提升周期防御 |
| `risk_off` | 0.5 | 0.5 | 0.3 | 1.8 | 大幅压制进攻；Analog +0.40 防御分 |

### Portfolio & Risk（高信念卫星仓）

top-3 子板块，max-weight 0.55，逆波动率优化器，**30% 波动目标**，beta 0.40–3.00（semis 的 ~2.5 beta 是**被接受的**，不强行拉回 1.0），月度调仓，阶梯式 VIX 去风险（28→10% / 32→25% 现金），−25% 回撤减半，3 个极端止损。

---

## 环境配置

### 1. 创建 qlib_run 环境

```bash
conda create -n qlib_run python=3.11
conda run -n qlib_run pip install qlib yfinance polygon-api-client fredapi \
    pandas numpy scipy statsmodels matplotlib pyportfolioopt pytest pytz \
    pandas_market_calendars openpyxl
```

> AISS 与 SSRS 共用同一个 `qlib_run` 环境。**绝不使用 `someopark_run` 或系统 Python。**

### 2. 配置 API Key

```bash
# 在项目根目录 someopark-test/.env 中：
POLYGON_API_KEY=your_polygon_api_key_here   # 股票价格 + SEC 数据
FRED_API_KEY=your_fred_api_key_here         # 宏观指标
```

> `.env` 已在 `.gitignore`。Pipeline 会自动从 `.env` grep 出这两个 key（不调用 `source .env`，避免 job-control 噪音）。

### 3. 正确运行方式

**所有命令从项目根目录（`someopark-test/`）运行：**

```bash
# 统一入口（推荐 —— 自动加载 key + 选择 qlib_run）
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh [MODE] [OPTIONS]

# 直接调用 Python（仅开发调试）
set -a && source .env && set +a
conda run -n qlib_run --no-capture-output python -m semiconductor_strategy.<module>
```

> 直接 `python` 或 `conda activate` 均不可靠——`conda run -n qlib_run --no-capture-output` 是确保环境正确的唯一方式。

---

## Pipeline 快速参考

```bash
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh [MODE] [OPTIONS]
```

| MODE | 说明 | 典型耗时 |
|---|---|---|
| `update_data` | 增量刷新全部数据源（prices + capex + mu-dio + tsmc/asml + dram + PIT） | 1–3 min |
| `daily` | 生成当日信号（subsector + **个股订单**）+ 更新 `inventory_aiss.json`（**NYSE 节假日感知**） | 1–2 min |
| `weekly` | 数据/PIT 健康检查 + **Weekly Review** + dry-run | 5–15 min |
| `monthly` | `daily_backtest`（V1+V2 选参刷新 P0，恢复 V1）+ force-rebalance（**节假日感知**） | 20–40 min |
| `dry-run` | 只读每日信号，不写 inventory，随时可运行 | 1–2 min |
| `backtest` | 用 active/selected 参数集跑全期回测 | ~2 s（数据已缓存） |
| `batch` | 批量运行全部 **33** 个参数集 → 排名 CSV/Excel | ~2–3 min |
| `select` | batch + WF OOS 过滤 + MCPS 选参 → `selected_param_set.json` | 5–15 min |
| `daily_backtest` | **V1+V2 全套**：batch IS + WF IS-OOS + diagnostic + PDF + select + validate（刷新 smart_select 的 P0 缓存，结束恢复 V1）（**节假日感知**） | 20–40 min |
| `walk-forward` / `wf` | 独立 Walk-Forward IS/OOS（anchored + rolling） | 3–8 min |
| `validate` | 回测 + 对比 SOXX/SMH/SPY → PASS/FAIL（胜负门槛） | ~3 s |
| `tearsheet` | 多页 PDF 绩效报告（含 SOXX/SMH 叠加页） | 视参数集而定 |
| `test` | pytest 套件（107 测试，纯合成数据，无网络） | ~5 s |
| `status` | 打印当前持仓 + 最新信号摘要 | < 5 s |
| `help` | 打印帮助 | — |

### 常用 OPTIONS

| 选项 | 说明 | 默认值 |
|---|---|---|
| `--signal-version v1\|v2` | 信号版本（V1=月度, V2=半月度），用于 daily/backtest/batch/select/wf/validate/tearsheet | smart_select 自动选；config 默认 v1 |
| `--param-set NAME` | 指定参数集（不依赖 selected_param_set.json） | selected / default |
| `--date YYYY-MM-DD` | 覆盖信号日期 | 最近交易日 |
| `--force-rebalance` | 强制再平衡（忽略月度调度） | 关 |
| `--skip-holiday` | 跳过 NYSE 节假日检查（回填 / 手动运行） | 关 |
| `--dry-run` | 不写 inventory | 关 |

> **NYSE 节假日检查**：`daily` / `monthly` / `daily_backtest` 在周末/节假日会跳过工作并 `exit 0`（正常成功，非失败），由 `pandas_market_calendars`（qlib_run）判定，失败时降级为工作日检查。`weekly` / `dry-run` 始终运行。回填或手动运行时加 `--skip-holiday` 绕过。

---

## 快速开始

```bash
# ── 首次：初始化数据层（见下方"数据层维护"）
# ── 每日运行（NYSE 节假日自动跳过）
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh daily

# ── 安全测试（不写 inventory）
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh dry-run

# ── 当前持仓
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh status

# ── 胜负门槛裁决（vs SOXX / SMH / SPY）
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh validate

# ── 全量重套：V1+V2 batch + WF IS-OOS + PDF + select（刷新 P0，恢复 V1）
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh daily_backtest

# ── 参数选优（月度）：batch + WF OOS 过滤 + MCPS → selected_param_set.json
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh select

# ── 独立 Walk-Forward IS/OOS
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh walk-forward

# ── 指定参数集回测
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh backtest --param-set momentum_heavy

# ── V2 半月度版本
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh validate --signal-version v2

# ── 测试套件
bash qlib-main/semiconductor_strategy/semiconductor_pipeline.sh test
```

---

## 数据层维护（PIT，可回填，生产增量）

AISS 没有 SSRS 的 EPS 步骤；它的"另类数据"层全部隔离在 `price_data/semi_strategy/` 下，采用**仅追加的 PIT 冻结**存储（数据刷新不会改写历史 → 可复现）。

### 数据结构

| 来源 | 路径 | 回填 | 日常/生产更新 | PIT 字段 |
|---|---|---|---|---|
| 价格（23 股 + SOXX/SMH/SPY） | `prices/*.parquet` | Polygon `--init` | `--update` | 交易日 |
| 子板块篮子 | （由价格派生） | — | — | — |
| CapEx 脉冲 | `company/capex_pulse.json` | yfinance | `--update-capex` | 同日 |
| MU DIO | `company/mu_dio_proxy.json` | SEC XBRL (CIK 723125) | `--check-mu-dio` | 10-Q `filed` |
| ASML 净订单 | `industry/asml_quarterly_orders.json` | SEC 6-K (CIK 937966) | `--check-asml` | filing/acceptance |
| TSMC 月营收 YoY | `industry/tsmc_monthly_revenue.json` | TWSE OpenAPI（仅向前） | `--check-tsmc` | TWSE 发布 |
| DRAM proxy | `industry/dram_spot_proxy.json` | MU/SOXX 相对强度 | `--update-dram` | 同日 |
| Macro（VIX, 利差…） | `price_data/macro/`（只读）+ FRED | — | — | FRED 滞后 |

### 首次初始化（一次）

```bash
set -a && source .env && set +a
ENV="conda run -n qlib_run --no-capture-output python -m semiconductor_strategy"

# 价格：23 股 + SOXX/SMH/SPY 全历史（Polygon 约 10 年 → 2016-05）
$ENV.data.aiss_fetch_prices --init --start 2016-01-01
# Company 层：CapEx 脉冲（yfinance）+ MU DIO（SEC XBRL）
$ENV.data.company_signals  --init
# Industry 层：TSMC（TWSE）、ASML 6-K 订单（SEC）、DRAM proxy
$ENV.data.industry_signals --init

# 覆盖度校验（2019+ 回测窗口必须 OK）
$ENV.data.aiss_fetch_prices --verify
$ENV.data.company_signals   --verify
$ENV.data.industry_signals  --verify
```

### 增量更新（生产，`update_data` 模式封装）

```bash
ENV="conda run -n qlib_run --no-capture-output python -m semiconductor_strategy"
$ENV.data.aiss_fetch_prices --update                 # 增量价格
$ENV.data.company_signals   --update-capex           # 重算 CapEx 脉冲
$ENV.data.company_signals   --check-mu-dio           # 新 MU 10-Q？
$ENV.data.industry_signals  --check-tsmc             # 新 TWSE 月份？
$ENV.data.industry_signals  --check-asml             # 新 ASML 6-K？
$ENV.data.industry_signals  --update-dram            # 重算 DRAM proxy
```

> **PIT 规则**（`data/aiss_pit.py`）：每个慢源存其**可得日期**（`filed_date` / `filing_date` / `release_date`），只在 `as_of >= 该日期` 时读回；`merge_frozen()` 仅追加 `> max(existing)` 的新日期 → 历史不被改写 → 可复现，无前视。
>
> **外部数据现实**：TSMC 深历史不可自由回填（TWSE 只暴露当月），故当某日 TSMC YoY 不可得时 `supply_chain` 用 foundry 价格动量代理（V1 设计）；ASML / MU-DIO / DRAM 有真实回填历史。

---

## 文件结构

```
qlib-main/semiconductor_strategy/
├── README.md                       本文件
├── RUNBOOK.md                      运维操作手册
├── config.yaml                     所有可调参数（AISS-tuned）
├── semiconductor_pipeline.sh       主 Pipeline 控制器（15 个模式）
├── daily_backtest.sh               V1+V2 全套回测/选参（被 daily_backtest/monthly 调用）
├── validate.py                     胜负门槛裁决（vs SOXX/SMH）
├── AISSdailySignal.py              每日信号生成 + inventory（含 smart_select + 个股分解）
├── stock_decompose.py              subsector 权重 → 个股持仓/订单（PIT，按 ticker 聚合）
├── AISSStrategyRuns.py             33 个命名参数集（组 A–H, M）
├── AISSBatchRun.py                 批量参数扫描 + P0 持久化 + --signal-version
├── smart_select.py                 每日宏观条件选参引擎（MCPS + version_selector）
├── walk_forward.py                 Walk-Forward IS/OOS 分析器
├── weekly_review.py                周报（漂移 + regime + 多视角 + P0 健康）
├── portfolio_record.py             33-sheet 回测 Excel + 5-sheet monitor + WF diagnostic 导出
│
├── data/
│   ├── universe.py                 8 子板块 + 80/15/5 篮子构建 + PIT 入篮
│   ├── loader.py                   价格（隔离 parquet store）+ FRED macro（macro_frozen 仅追加）
│   ├── aiss_fetch_prices.py        Polygon 价格抓取（--init / --update / --verify）
│   ├── aiss_fetch_sec_data.py      SEC XBRL / 6-K 抓取
│   ├── aiss_pit.py                 PIT 可得日期 + merge_frozen（仅追加冻结）
│   ├── company_signals.py          CapEx 脉冲 + MU DIO
│   └── industry_signals.py         TSMC / ASML / DRAM proxy
│
├── signals/
│   ├── momentum.py                 横截面 12-1m 动量
│   ├── supply_chain.py             知识图谱传播（AISS 核心 alpha）
│   ├── regime.py                   4 态 Regime + CapEx regime 倾斜
│   ├── composite.py                4 因子聚合 + Regime 条件权重
│   └── risk_overlay.py             V2 风险叠加（AISS 默认 OFF）
│
├── portfolio/
│   ├── optimizer.py                逆波动率 / 风险平价 / GMV / 等权 + Ledoit-Wolf
│   ├── risk.py                     波动率缩放 + VIX 阶梯去风险 + 回撤断路器 + beta
│   ├── rebalance.py                月度/半月度调度 + 阈值过滤 + 换手率上限
│   ├── stop_loss.py                极端止损（circuit breaker + sector collapse + trailing）
│   └── strategy.py                 AISSWeightStrategy（qlib WeightStrategyBase 适配）
│
├── backtest/
│   ├── engine.py                   事件驱动回测（native loop 为生产引擎）+ walk-forward
│   ├── qlib_adapter.py             qlib Exchange / Executor 适配（休眠脚手架，见下）
│   ├── costs.py                    按 3 tier 的点差成本模型（3/5/8 bps）
│   ├── metrics.py                  Sharpe / Calmar / IR / CVaR
│   ├── trade_audit.py              逐笔交易审计 CSV
│   └── robustness.py               稳健性分析
│
├── report/
│   ├── plots.py                    matplotlib 可视化
│   └── tearsheet.py                多页 PDF 报告
│
├── tests/                          pytest（107 测试，合成数据，无网络）
├── logs/                           运行日志（gitignore）
└── DELETED_FILES.md                AISS V1 中移除的 SSRS 文件清单
```

### 输出文件完整参考

#### 目录结构总览

```
someopark-test/                                            项目根目录
├── price_data/semi_strategy/                              AISS 隔离数据（仅追加）
│   ├── prices/*.parquet                                   23 股 + SOXX/SMH/SPY 价格
│   ├── company/capex_pulse.json · mu_dio_proxy.json       CapEx / MU DIO（PIT 冻结）
│   ├── industry/asml_*.json · tsmc_*.json · dram_*.json   ASML / TSMC / DRAM（PIT 冻结）
│   └── cache/macro_frozen.parquet                         macro 仅追加冻结快照
│
├── price_data/macro/*.parquet                             宏观（只读，主 pipeline 写入）
│
├── historical_runs/semiconductor_strategy/                回测输出 Excel（gitignored）
│   ├── aiss_portfolio_{set}_{v}_{span}_{mode}_{ts}.xlsx   33-sheet 完整回测记录
│   ├── wf_diagnostic_aiss_{v}_IS-OOS_{wfmode}_{mode}_{ts}.xlsx  5-sheet WF 诊断
│   └── trade_audit.csv                                    逐笔交易审计
│
└── qlib-main/semiconductor_strategy/
    ├── selected_param_set.json                            生产参数（含 signal_version）
    ├── inventory_aiss.json                                当前持仓（subsector `holdings` + 个股 `stock_holdings` + param_set/signal_version）
    ├── inventory_history/inventory_aiss_{ts}.json         持仓变更快照（ENTER/CLOSE 各一次/日）
    ├── trading_signals/aiss_daily_report_{date}_{ts}.{json,txt}   日报（subsector 层 + 个股层 stock_holdings/stock_breakdown/stock_trades）
    ├── backtest_results/
    │   ├── aiss_batch_summary_{ts}.csv                    33 集 batch 汇总
    │   ├── param_oos_by_regime{,_v1,_v2}.json             P0: smart_select 缓存（含版本标记）
    │   ├── weekly_review*.json                            周报输出
    │   └── …                                              其它 P0 缓存
    ├── report/output/*.pdf                                Tearsheet PDF
    └── logs/aiss_{mode}_{YYYYMMDD_HHMMSS}.log             运行日志（带时间戳后缀）
```

> **注意日志命名**：AISS 日志带 `_HHMMSS` 时间戳后缀（不同于 SSRS 仅日期），定位最新用 `ls -t logs/aiss_daily_*.log | head -1`。

#### 命名规则

| 占位符 | 格式 | 示例 |
|---|---|---|
| `{ts}` | `YYYYMMDD_HHMMSS` | `20260531_220958` |
| `{date}` | `YYYYMMDD` | `20260531` |
| `{set}` | 参数集名（lowercase_snake） | `opt_equal_weight` |
| `{v}` | 信号版本 | `v1` / `v2` |
| `{span}` | 数据范围 | `IS`（纯样本内） / `IS-OOS`（walk-forward 验证） |
| `{mode}` | 入口 | `batch` / `select` / `tearsheet` / `wf` |
| `{wfmode}` | WF 模式 | `anchored` / `rolling` |

**文件名模板**：
```
aiss_portfolio_{set}_{v1|v2}_{IS|IS-OOS}_{batch|select|tearsheet}_{ts}.xlsx
wf_diagnostic_aiss_{v1|v2}_IS-OOS_{anchored|rolling}_{select|wf|tearsheet}_{ts}.xlsx
```

前端通过文件名区分所有维度：**版本** `_v1_`/`_v2_`、**范围** `_IS_`/`_IS-OOS_`、**入口** `_batch_`/`_select_`/`_tearsheet_`、**参数** 文件名含完整参数集名。

#### 各 Pipeline Mode 输出文件矩阵

| Mode | 信号/报告 | 回测/分析 | Excel 记录 | 备注 |
|---|---|---|---|---|
| **daily** | `trading_signals/` JSON+TXT, `inventory_aiss.json` | — | monitor（调仓日） | 含 param_set/signal_version |
| **dry-run** | `trading_signals/` JSON+TXT | — | — | 不写 inventory |
| **weekly** | dry-run 报告 | data/PIT verify + weekly_review | — | weekly_review 非致命 |
| **monthly** | = daily 调仓 | = daily_backtest 全部 | = daily_backtest | 两步合一，结束恢复 V1 |
| **batch** | — | `aiss_batch_summary_*.csv` | （`--save-equity` 时 ×33） | 33 集汇总 |
| **select** | — | P0 缓存, `selected_param_set.json` | 最优集 + `wf_diagnostic_*` | 生产选参 |
| **daily_backtest** | — | V1+V2 各 33 batch IS + WF IS-OOS + select + validate | 33×2 IS + 33×2 IS-OOS + WF diag | 全套；生产恢复 V1 |
| **walk-forward** | — | fold 汇总 | `wf_diagnostic_*` | IS/OOS 分析 |
| **validate** | console PASS/FAIL | — | — | 胜负门槛 |
| **tearsheet** | — | IS-OOS Excel + PDF | `wf_diagnostic_*` | 含 SOXX/SMH 叠加页 |
| **test / status / help** | — | — | — | 只读/显示 |

#### Portfolio History Excel（33 Sheets = 26 主 + 7 stock_decomp）

`historical_runs/semiconductor_strategy/aiss_portfolio_{set}_{v}_{span}_{mode}_{ts}.xlsx`

**26 个主 sheet**（"sector" 在 AISS 中指子板块）：

| # | Sheet | 频率 | 内容 |
|---|---|---|---|
| 1 | summary | 单行 | Sharpe, Calmar, MaxDD, CAGR, param_set, signal_version |
| 2 | portfolio_history | 日频 | date, equity, asset, liability, daily_pnl, cum_pnl, drawdown_pct |
| 3–6 | asset/liability/equity/asset_cash_history | 日频 | 资产/负债/净值/现金 |
| 7 | sector_prices | 日频 | 8 子板块篮子价格 |
| 8 | share_history | 日频 | 8 子板块持有"份额" |
| 9 | sector_weights | 调仓日 | 8 子板块目标权重 + cash（V1~96, V2~185） |
| 10 | sector_weight_pct | 日频 | 漂移后实际占比 |
| 11 | cost_basis | 日频 | 进入价格（加权均价） |
| 12 | sector_ratio_matrix | 调仓日 | 子板块权重互比矩阵 |
| 13–14 | sector_pnl_acc / sector_pnl_daily | 日频 | 子板块累计/日度 PnL |
| 15 | sector_contribution | 日频 | 子板块对总收益贡献 |
| 16 | daily_pnl | 日频 | 总 PnL + 累计 |
| 17–18 | interest_expense / acc_interest | 日频 | 利息（无杠杆=0） |
| 19–20 | realized_pnl / total_notional | 按子板块 | 已实现 PnL / 累计名义 |
| 21 | drawdown_history | 日频 | dd_dollar, dd_pct |
| 22 | rebalance_trades | 按交易 | date, sector, direction, old/new weight, shares, price, cost |
| 23 | regime_indicators | 日频 | VIX, 利差, capex regime + regime 标签 |
| 24 | strategy_vars | 调仓日 | config 参数 + composite scores + risk flags |
| 25 | stop_loss_history | 按事件 | date, sector, type, reason, entry/current price, pnl |
| 26 | config | 参数表 | 完整 config.yaml 快照 |

**7 个 stock_decomp sheet**（凡含子板块列的 sheet 均分解到个股，列名 `{subsector}/{stock}`，24 列）：

`sector_prices_stock_decomp` · `sector_weights_stock_decomp` · `sector_wt_pct_stock_decomp` · `share_hist_stock_decomp` · `cost_basis_stock_decomp` · `sector_pnl_stock_decomp` · `sector_contrib_stock_decomp`

> 校验不变量：权重每行求和 = 1（含 cash），stock_decomp 各列回拼到子板块误差 < 0.0001，equity 全正。V1 月度调仓 ~96 次；V2 半月度 ~185 次；日频 sheet 始终 ~1862 行。

#### WF Diagnostic Excel（5 Sheets）

`wf_diagnostic_aiss_{v}_IS-OOS_{wfmode}_{mode}_{ts}.xlsx`

| # | Sheet | 内容 |
|---|---|---|
| 1 | fold_summary | 每折 × (IS/OOS dates, Selected, Method) |
| 2 | param_oos_matrix | 33 param × fold 的 OOS Sharpe 矩阵 |
| 3 | param_by_regime | 33 param × regime 的 mean OOS Sharpe |
| 4 | synthetic_equity | 合成 OOS 净值曲线 |
| 5 | selection_log | 每折选参决策记录 |

#### Monitor Excel（5 Sheets，调仓日）

| # | Sheet | 内容 |
|---|---|---|
| 1 | snapshot | equity, regime, VIX, cash, n_positions, param_set, signal_version |
| 2 | holdings | 8 子板块 × (weight, shares, price, cost_basis, pnl, composite_score) |
| 3 | signals | 8 子板块 × 4 因子分量 |
| 4 | smart_select | MCPS score, rank, candidates, version_selector |
| 5 | risk_flags | vol_scaling, vix_emergency, dd_circuit, beta_adj + 阈值 |

---

## 配置参考

所有参数在 `config.yaml` 中管理：

| 节 | 键 | 默认值 | 说明 |
|---|---|---|---|
| `signals.weights` | cs_momentum / supply_chain / capex_pulse / cycle_regime | 0.30 / 0.35 / 0.25 / 0.10 | 四因子权重，和必须 = 1.0 |
| `signals` | signal_version | `"v1"` | V1 月度 / V2 半月度 |
| `signals.supply_chain` | use_external_macro | `true` | TSMC/ASML/DRAM/MU-DIO 可得时用，否则价格代理 |
| `portfolio` | optimizer | `"inv_vol"` | inv_vol / risk_parity / gmv / equal_weight |
| `portfolio` | top_n_sectors | `3` | 高信念集中 |
| `portfolio.constraints` | max_weight | `0.55` | 单子板块上限 |
| `portfolio.constraints` | beta_min / beta_max | `0.40` / `3.00` | 接受高 semis beta，不拉回 1.0 |
| `risk.vol_scaling` | target_vol_annual | `0.30` | 贴近 semis 基准波动运行 |
| `risk.drawdown` | cumulative_dd_halve | `−0.25` | semis 常规回撤 20%+，仅真崩盘减半 |
| `rebalance` | emergency_derisk_vix | `36.0` | 仅真危机（semis 震荡 ≠ 离场） |
| `signals.regime` | vix_high / vix_extreme | `25` / `32` | regime 倾斜阈值 |
| `backtest` | start_date | `"2019-01-01"` | 晚期 IPO 地板 |
| `backtest` | initial_capital | `1_000_000` | 初始资金 USD |

---

## 全参数完整参考

> 所有 `config.yaml` 参数均可在不改代码的情况下调整。

### 一、数据 `data`

| 键 | 默认值 | 说明 |
|---|---|---|
| `price_dir` | `"../../price_data/semi_strategy/prices"` | 隔离 parquet 价格 store |
| `industry_dir` / `company_dir` | `…/industry` · `…/company` | 另类数据存储 |
| `macro_dir` | `"../../price_data/macro"` | 共享宏观（**只读**） |
| `price_source` | `"store"` | AISS 隔离 store（Polygon-backed） |
| `price_start` | `"2016-01-01"` | 早于回测起点以 warm-up |
| `macro_source` | `"fred"` | 宏观来源 |

### 二、标的 `universe`

| 键 | 默认值 | 说明 |
|---|---|---|
| `etfs` | 8 子板块名 | 可交易资产（沿用 SSRS key 名 `etfs`） |
| `benchmark` | `"SPY"` | beta / 信息比率基准 |
| `benchmarks` | `["SOXX","SMH","SPY"]` | SOXX & SMH = 胜负门槛 |
| `subsectors` | 8×3 股票映射 | 每子板块 3 只 |
| `subsector_tier_weights` | `[0.80, 0.15, 0.05]` | 篮子内固定比例 |
| `min_history_months` | `24` | backup tier 入篮所需历史 |
| `universe_start` | `"2019-01-01"` | 完整宇宙起点 |

### 三、信号 `signals`

#### 3.1 权重（和 = 1.0）
`cs_momentum` 0.30 · `supply_chain` 0.35 · `capex_pulse` 0.25 · `cycle_regime` 0.10

#### 3.2 横截面动量 `cs_momentum`
`lookback_months` 12 · `skip_months` 1 · `zscore_window` 36（12-1 动量）

#### 3.3 供应链 `supply_chain`
`graph_version` `"v1"` · `use_external_macro` `true`（PIT 可得时用 TSMC/ASML/MU-DIO/DRAM，否则 foundry 价格代理）· `lag_decay` 0.0（0=硬滞后，>0=指数衰减，V2）

#### 3.4 CapEx 脉冲 `capex_pulse`
`tickers` `[MSFT, GOOGL, META, AMZN]` · `lookback_months` 3 · `zscore_window` 24

#### 3.5 V1 / V2 版本
V1（默认生产）月度调仓 12-1 动量；V2 半月度（1 日 + ~月中）同样的稳健 4 因子 / 12-1 信号——更快 cadence 而非 SSRS 式 gating。`smart_select` 从各版本 OOS 历史中选 V1 vs V2（当前 V1 占优）。**注意**：更快动量（如 6-0）测过且**有害**（semis 易被甩），故 V2 保留 12-1。

#### 3.6 Regime `signals.regime`
`method` `"rules"` · `vix_high_threshold` 25 · `vix_extreme_threshold` 32 · `hy_spread_high_bps` 450 · `yield_curve_inversion` −0.10 · `ism_expansion` 50 · `capex_strong_zscore` 1.0 · `capex_weak_zscore` −1.0。`regime_weights` 见上方"Regime 四态"表。`defensive_sectors` `["analog_defense"]` · `defensive_bonus_risk_off` 0.40。

### 四、投资组合 `portfolio`

| 键 | 默认值 | 说明 |
|---|---|---|
| `optimizer` | `"inv_vol"` | inv_vol / risk_parity / gmv / equal_weight / mvo |
| `cov.method` | `"ledoit_wolf"` | 协方差估计 |
| `cov.lookback_days` / `min_periods` | 252 / 63 | 协方差窗口 |
| `constraints.max_weight` | 0.55 | 单子板块上限（集中到赢家） |
| `constraints.beta_min` / `beta_max` | 0.40 / 3.00 | 接受高 beta |
| `top_n_sectors` | 3 | 持仓子板块数 |
| `min_zscore` | −0.30 | 分配权重所需最低分 |
| `weight_scheme` | `"rank"` | rank / zscore_softmax |

### 五、调仓 `rebalance`

| 键 | 默认值 | 说明 |
|---|---|---|
| `frequency` | `"monthly"` | monthly（V2 引擎追加月中调仓） |
| `rebalance_day` | `"first_trading_day"` | 月内调仓时间 |
| `zscore_change_threshold` | 0.5 | 信号变化 < 此值则跳过该子板块 |
| `emergency_derisk_vix` | 36.0 | 紧急去风险触发 VIX |
| `emergency_cash_pct` | 0.45 | 紧急目标现金 |
| `max_monthly_turnover` | 0.80 | 单侧换手率上限 |

### 六、风险 `risk`

| 键 | 默认值 | 说明 |
|---|---|---|
| `vol_scaling.enabled` | true | 波动率缩放 |
| `vol_scaling.target_vol_annual` | 0.30 | 目标年化波动 |
| `vol_scaling.estimation_window` | 20 | 实际波动窗口 |
| `vol_scaling.scale_threshold` | 1.5 | 仅 realized > 1.5× 历史时缩减 |
| `drawdown.cumulative_dd_halve` | −0.25 | 累计回撤减半线 |
| `drawdown.cumulative_dd_recovery` | −0.12 | 解除线 |
| `vix_progressive_derisk.tiers` | 28→10% / 32→25% | VIX 阶梯现金 |

VIX 完整阶梯：`< 28` 全仓 → `≥ 28` 10% cash → `≥ 32` 25% cash → `≥ 36` 45% cash（emergency 触发）。

### 七、止损 `stop_loss`（极端事件）

`portfolio_circuit_breaker` SPY 3 日 −7% · `sector_collapse` 单子板块自进入 −15% · `trailing_stop` 自峰值 −18% · `cooling_off_days` 10。

### 八、交易成本 `costs`（3 tier）

Tier 1（NVDA/AVGO/AMD/QCOM/TSM/MU/TXN/INTC）3 bps · Tier 2（KLAC/LRCX/AMAT/WDC/ARM/ADI）5 bps · Tier 3（其余）8 bps。引擎按 80/15/5 篮子混合各 tier。`annual_fee_bps` 0（个股无管理费）。

### 九、回测 `backtest`

`start_date` 2019-01-01 · `end_date` null · `initial_capital` 1_000_000 · `is_years` 3 · `oos_months` 6。

### 十、报告 / 记录

`report.output_dir` `"report/output"` · `pdf_filename` `"aiss_tearsheet.pdf"`。`portfolio_record.leverage_ratio` 0.0（无杠杆）· `interest_rate` 0.05。`risk_overlay.enabled` **false**（SSRS 的 V2 MA gate 会坐现金、压制驱动 AISS 回报的高 beta 敞口，故 AISS 关闭）。

---

## 回测框架

### 入口与共享引擎

| 入口 | 用途 |
|---|---|
| `AISSBatchRun.py`（默认） | 33 参数集 × 全期回测，纯 IS，输出 CSV/Excel |
| `AISSBatchRun.py --select` | 三阶段生产选参：WF OOS 过滤 → MCPS（全周期 equity + 今日宏观向量）→ 近 12 月 Sharpe 兜底 → `selected_param_set.json` |
| `walk_forward.py` | IS/OOS 滚动窗口回测（anchored + rolling），输出 fold 汇总 / 合成 OOS 净值 |
| `daily_backtest.sh` | V1+V2 全套（batch IS + WF IS-OOS + diagnostic + PDF + select + validate），刷新 smart_select 的 P0 缓存，结束恢复 V1 |
| `report/tearsheet.py` | 多页 PDF 绩效报告 |

### IS-only vs IS-OOS

- **IS-only**（默认 batch）：全周期既训练又评估，无"未见数据"验证，Sharpe 可能偏高（33 参数集多重测试偏差），用于快速筛选/建立基线。
- **IS-OOS Walk-Forward**：每折在 IS 上选参 → embargo 隔离 → 在 OOS（未来数据）上验证；合成 OOS 净值 = 拼接各折 OOS 段（无重叠、无前视）。`daily_backtest` / `walk-forward` / `tearsheet` 产出。

### 日度信号的两层输出（subsector → 个股）

`daily` / `dry-run` 的信号输出有**两层**，由 `stock_decompose.py` 衔接：

1. **subsector 层(决策层,= SSRS 的 ETF 层)**:8 个子板块的目标权重 / 信号分 / 动作 + subsector 级 inventory `holdings`。
2. **个股层(执行层,AISS 独有的多出一层)**:把每个持有 subsector 的目标权重按 80/15/5 分解到底层个股 ——
   - `decompose_to_stocks()` 用 `universe.effective_weights`(PIT)算篮子内权重,组合权重 = subsector_w × within_w,股数 = floor(组合权重 × 资金 / 个股价);
   - **按 ticker 跨子板块聚合**(ARM 同属 ai_gpu+logic_cpu → 合并成一个订单);
   - `build_stock_trades()` 对比上次 inventory 的 `stock_holdings` 产出逐股 BUY/SELL。

输出落点:日报 TXT 的 `STOCK-LEVEL TARGET HOLDINGS` + `STOCK TRADES` 段、日报 JSON 的 `stock_holdings`/`stock_breakdown`/`stock_trades`、inventory 的 `stock_holdings`。这与回测端的 `*_stock_decomp` Excel sheet 是同一套逻辑的两端。

### smart_select + MCPS + version_selector

`daily` 模式由 `smart_select.py` 在生产中选参：用 autoencoder 把当日宏观状态编码为 latent 向量，对各参数集的全周期 OOS equity 做高斯核相似度加权（MCPS），选出最匹配当前 regime 的参数集；`version_selector` 从 `param_oos_by_regime_{v1,v2}.json`（由 `daily_backtest` 刷新）中比较 V1 vs V2 的 per-regime OOS 表现，选当前更优版本（目前 V1 占优）。选中的 param_set / signal_version 写入 `selected_param_set.json` 与 `inventory_aiss.json`，便于审计。

### 关于 qlib backtest path（休眠脚手架）

`backtest/engine.py` 设计为「qlib path 优先 → native loop 兜底」，但 qlib path 因 qlib `WeightStrategyBase`/`Exchange` 兼容问题（`signal=None` + 未 `qlib.init(region)` 致 `C.trade_unit` 缺失）始终抛错并 fallback。**native loop 是 AISS（与 SSRS 完全相同）的实际生产引擎**，所有验证数字均出自 native loop。每次回测会打印 `qlib backtest execution failed …, falling back to native loop`——这是**良性**、预期内的日志，**不应**被判为失败或 degraded。

---

## 参数集扫描（AISSStrategyRuns，33 个）

`conda run -n qlib_run python -m semiconductor_strategy.AISSStrategyRuns` 打印全部。

| 组 | 参数集 | 维度 |
|---|---|---|
| **A 信号权重** | default · supply_chain_heavy · momentum_heavy · capex_heavy · balanced_four · momentum_capex | 四因子配比 |
| **B 集中度** | concentrated_2 · standard_3 · diversified_4 · broad_5 | top-N + max_weight |
| **C 波动目标** | vol_target_24 · vol_target_30 · vol_target_40 · no_vol_scaling | vol_scaling |
| **D VIX 去风险** | derisk_tight · derisk_loose · no_vix_derisk | 阶梯/紧急阈值 |
| **E 动量窗口** | fast_momentum · standard_momentum · slow_momentum | 6-0 / 12-1 / 15-1 |
| **F 供应链外部数据** | external_on · external_off | TSMC/ASML/DRAM/MU-DIO vs 价格代理 |
| **G 优化器** | opt_inv_vol · opt_risk_parity · opt_gmv · opt_equal_weight | 权重方法 |
| **H 原型** | max_aggression · quality_defensive · ai_capex_tilt · supply_chain_core | 多维组合 |
| **M 单因子隔离** | pure_momentum · pure_supply_chain · pure_capex | 因子归因 |

> `default`（A1）= 已验证的胜负门槛配置（cs.30/sc.35/cx.25/cy.10）。最新 batch 排名靠前：derisk_tight、balanced_four、opt_equal_weight（IS Sharpe ~1.7–1.8 / CAGR ~47%）。

---

## Cron 定时任务

AISS 的三个 OpenClaw cron 任务镜像 SSRS（在 isolated session 中运行，向 Telegram 汇报，失败告警）：

| 任务 | 调度（ET） | 命令 |
|---|---|---|
| `aiss-daily-backtest` | 工作日 18:40 | `bash …/daily_backtest.sh` |
| `aiss-daily` | 工作日 19:20 | `bash …/semiconductor_pipeline.sh daily` |
| `aiss-weekly` | 周日 02:00 | `bash …/semiconductor_pipeline.sh weekly` |

> `daily` / `monthly` / `daily_backtest` 内置 NYSE 节假日检查（休市跳过 + exit 0）。错峰于 SSRS（16:40 / 17:20 / 周日 01:00）以避免 CPU 争用。详见 RUNBOOK。

---

## 数据来源

| 数据 | 来源 | 用途 |
|---|---|---|
| 股票 / ETF 价格 | Polygon（隔离 parquet store） | 篮子构建 + 动量 |
| 超大规模 CapEx | yfinance（MSFT/GOOGL/META/AMZN） | capex_pulse |
| MU DIO | SEC XBRL（CIK 723125） | supply_chain 库存信号 |
| ASML 净订单 | SEC 6-K（CIK 937966） | supply_chain 设备领先指标 |
| TSMC 月营收 | TWSE OpenAPI | supply_chain foundry 领先指标 |
| DRAM proxy | MU/SOXX 相对强度 | supply_chain 内存周期 |
| 宏观（VIX, 利差, ISM…） | `price_data/macro/`（只读）+ FRED | regime + cycle_regime |

---

## 与 SSRS / someopark 主程序的关系

AISS 是 `sector_rotation`（SSRS）的 `qlib_run` 孪生：**相同的引擎架构**（native loop、smart_select/MCPS、walk-forward、portfolio_record、win-criterion），**不同的宇宙**（半导体子板块 vs GICS ETF）、**不同的核心信号**（供应链知识图谱传播 vs P/E 估值）、以及一个明确的硬门槛（跑赢 SOXX & SMH）。AISS 只读取 `price_data/macro/`，只增量写 `price_data/semi_strategy/`，绝不触碰 SSRS 或根脚本。AISS V1 未用到的 SSRS 文件已移除——见 `DELETED_FILES.md`。

运维操作详见 [RUNBOOK.md](RUNBOOK.md)。所有命令使用 `conda run -n qlib_run`。
