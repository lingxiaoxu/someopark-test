<p align="center">
  <img src="../../public/SOMEO PARK矢量源文件 Big Square.svg" alt="Someopark" width="120"/>
</p>

<h1 align="center">Sector Rotation Strategy</h1>
<p align="center"><b>Institutional-grade GICS sector ETF rotation — powered by qlib</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/conda-qlib__run-green?logo=anaconda&logoColor=white"/>
  <img src="https://img.shields.io/badge/universe-11%20SPDR%20ETFs-orange"/>
  <img src="https://img.shields.io/badge/rebalance-monthly-purple"/>
  <img src="https://img.shields.io/badge/direction-long--only-teal"/>
  <img src="https://img.shields.io/badge/data-Polygon%20%7C%20Yahoo%20%7C%20FRED-lightgrey"/>
</p>

---

> **隔离原则**：本策略只使用 `qlib_run` conda 环境，**绝不调用 `someopark_run`**。
> `price_data/macro/` parquets 由 someopark 主 pipeline 负责写入；本策略只读取，不写入、不更新。

---

## 策略概览

跨 11 个 GICS SPDR 行业 ETF 进行多因子月度轮动。组合信号由横截面动量、时序动量崩溃过滤、相对估值（成分股 TTM P/E）和 Regime 条件权重调整四个模块合成。

| 维度 | 设计 |
|---|---|
| 标的池 | XLE · XLB · XLI · XLY · XLP · XLV · XLF · XLK · XLC · XLU · XLRE |
| 基准 | SPY |
| 回测起点 | 2018-07-01（XLC 创立日；11 行业完整宇宙） |
| 调仓频率 | 每月首个交易日 |
| 方向 | 纯多头，无做空，基础配置无杠杆 |
| 目标 Sharpe | 0.4–0.6（扣费后） |
| 目标最大回撤 | < 20% |

### 信号架构

| 信号 | 权重 | 方法 |
|---|---|---|
| 横截面动量（CS Momentum） | 40% | 12-1 月相对回报，横截面 z-score |
| 时序动量（TS Momentum） | 15% | 12 月自身回报崩溃过滤器（乘数） |
| 相对估值（Relative Value） | 20% | TTM P/E 百分位 vs 10 年历史；成分股季度 EPS via Polygon + yfinance |
| Regime 条件调整 | 25% | 4 态 VIX/利差/ISM 规则型 Regime → 信号权重动态修正 |

### Regime 四态定义

| 状态 | 条件 | CS 动量权重 | 防御板块 |
|---|---|---|---|
| `RISK_ON` | VIX < 20, 利差低, ISM > 50 | 1.0× | 无加成 |
| `TRANSITION_UP` | 各指标改善中 | 1.1× | 无加成 |
| `TRANSITION_DOWN` | 各指标恶化中 | 0.7× | 无加成 |
| `RISK_OFF` | VIX > 30, 利差高, ISM < 48 | 0.6× | XLU / XLP / XLV +0.30 |

---

## 环境配置

### 1. 创建 qlib_run 环境

```bash
# 创建环境（需 Python 3.11 + qlib + polygon + yfinance + fredapi）
conda create -n qlib_run python=3.11
conda run -n qlib_run pip install qlib yfinance polygon-api-client fredapi \
    pandas numpy scipy statsmodels matplotlib pyportfolioopt pytest pytz \
    pandas_market_calendars openpyxl
```

### 2. 配置 API Key

```bash
# 在 someopark-test 项目根目录的 .env 文件中添加：
POLYGON_API_KEY=your_polygon_api_key_here
FRED_API_KEY=your_fred_api_key_here
```

> `.env` 已加入 `.gitignore`。两个 key 分别用于 EPS 历史数据（Polygon）和宏观指标（FRED）。

### 3. 正确运行方式

**所有命令必须在项目根目录（`someopark-test/`）运行，且必须加载 `.env`：**

```bash
# 统一入口（推荐）
set -a && source .env && set +a
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh [mode] [options]

# 直接调用 Python（仅开发调试用）
set -a && source .env && set +a
conda run -n qlib_run --no-capture-output python qlib-main/sector_rotation/<script>.py
```

> 直接 `python` 或 `conda activate` 后运行均不可靠——`conda run -n qlib_run --no-capture-output` 是确保环境正确的唯一方式。

---

## Pipeline 快速参考

```
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh [MODE] [OPTIONS]
```

| MODE | 说明 | 典型耗时 |
|---|---|---|
| `daily` | 标准每日运行：节假日检查 → EPS 自动刷新 → 信号生成 | 1–3 min |
| `weekly` | 每周维护：EPS 增量更新 + dry-run 验证 | 3–5 min |
| `monthly` | 月初强制再平衡（节假日感知，EPS 预刷新） | 2–4 min |
| `eps-update` | 增量 EPS 更新（跳过 ≤7 天内已刷新的标的） | 1–3 min |
| `eps-full` | 强制全量 EPS 重拉（55 只股票，首次运行必用） | ~5 min |
| `eps-symbols` | 指定标的 EPS 更新，例如 `eps-symbols XOM CVX` | < 1 min |
| `backtest` | 全量 IS/OOS 历史回测（2018-07-01 → 今日） | 5–15 min |
| `batch` | 批量运行全部 59 个参数集 → CSV + Excel（含 recent 12m Sharpe） | ~2 min |
| `select` | `batch --select` 简写：运行 59 集 + 写 `selected_param_set.json`（生产选参） | ~2 min |
| `sensitivity` | 参数敏感性扫描（`top_n_sectors` 等） | 5–10 min |
| `regime` | Regime 分析报告（4 态标签 + 汇总） | < 1 min |
| `tearsheet` | 回测 + 生成多页 PDF 绩效报告 | 5–15 min |
| `test` | 运行 pytest 套件（95 个测试，纯合成数据，无网络） | 1–2 min |
| `dry-run` | 只读每日信号，不写 inventory，随时可运行 | 1–2 min |
| `status` | 打印当前持仓状态 + 最新信号文件摘要 | < 5 sec |
| `signal-raw` | 打印 `get_current_signals()` 原始 z-score | 30–60 sec |

### 常用 OPTIONS

| 选项 | 说明 | 默认值 |
|---|---|---|
| `--value-source proxy\|polygon\|constituents` | P/E 数据来源 | `polygon` |
| `--capital N` | 组合资金 USD | 从 inventory 读取 |
| `--date YYYY-MM-DD` | 覆盖信号日期 | 最近交易日 |
| `--force-rebalance` | 强制再平衡（忽略月度调度） | 关 |
| `--skip-holiday` | 跳过 NYSE 节假日检查（回填 / 手动运行） | 关 |
| `--no-eps-check` | 跳过每日 EPS 自动刷新 | 关 |
| `--force` | 与 `eps-update` 配合，强制全量重拉 | 关 |
| `--config PATH` | 指定 config.yaml 路径 | `sector_rotation/config.yaml` |

---

## 快速开始

```bash
# ── 首次运行（先拉取全量 EPS 数据）
set -a && source .env && set +a
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-full

# ── 每日运行
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily

# ── 安全测试（不写 inventory）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run

# ── 查看当前持仓
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh status

# ── 查看原始 z-score 信号
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh signal-raw

# ── 全量回测 + tearsheet
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh tearsheet

# ── 参数集选优（每季度）：运行 59 集 + 选最优 → 写入 selected_param_set.json
# → 之后的 daily/weekly/monthly 自动使用该参数集
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh select

# ── 运行测试套件
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh test
```

---

## EPS 历史数据维护

### 数据结构

板块轮动的相对估值信号依赖 55 只 GICS 成分股的季度 EPS 历史，存储于：

```
price_data/sector_etfs/eps_history.json
```

```json
{
  "fetched_at": "2026-04-24",
  "symbol_meta": {
    "XOM": {
      "last_fetched":    "2026-04-24",
      "newest_end_date": "2025-12-31"
    }
  },
  "symbols": {
    "XOM": [
      {"end_date": "2009-03-31", "eps": 0.43},
      ...
    ]
  }
}
```

- `symbol_meta[sym]["last_fetched"]`：控制刷新频率（`REFRESH_DAYS=7`）
- `newest_end_date`：增量拉取的起点，避免重复下载旧数据
- 旧格式（无 `symbol_meta`）首次读取时自动迁移

### 运行方式

```bash
# 增量更新（推荐，每日 / 每周自动运行）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-update

# 首次 / 强制全量重拉
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-full

# 指定标的更新（季报季后手动补充）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-symbols XOM CVX AAPL NVDA

# 直接调用脚本（开发用）
set -a && source .env && set +a
conda run -n qlib_run --no-capture-output \
    python qlib-main/sector_rotation/update_eps_history.py

# 强制全量重拉特定标的
conda run -n qlib_run --no-capture-output \
    python qlib-main/sector_rotation/update_eps_history.py XOM CVX --force
```

### 增量逻辑

1. 检查 `symbol_meta[sym]["last_fetched"]` vs 今日
2. 距上次拉取 ≤ `REFRESH_DAYS`（7天）→ 跳过（无 Polygon 调用）
3. 超过 7 天 → 拉取 `end_date >= newest_end_date` 的季度数据
4. 合并：新季度覆盖旧同日期数据（处理财报重述）
5. 首次运行或 `--force` → 全量拉取（最多 6 页 × 40 条，约 2009→今）

> 55 只股票约覆盖 3100+ 个季度。日常增量更新每次只拉取少量新数据，非常节省 API 配额。

---

## 文件结构

```
qlib-main/sector_rotation/
├── README.md                       本文件
├── RUNBOOK.md                      运维操作手册（首次/日常/月度/回测/故障排查）
├── config.yaml                     所有可调参数
├── sector_rotation_pipeline.sh     主 Pipeline 控制器（14 个模式）
├── update_eps_history.py           EPS 历史增量维护脚本
├── SectorRotationDailySignal.py    每日信号生成主脚本
├── SectorRotationStrategyRuns.py   59 个命名参数集（13 组，A-M）
├── SectorRotationBatchRun.py       批量参数扫描驱动脚本
│
├── data/
│   ├── universe.py                 ETF 宇宙 + GICS 元数据 + 流动性分级
│   ├── loader.py                   价格（MongoDB → yfinance 回退）+ FRED 宏观数据加载
│   └── cache/                      本地磁盘缓存（gitignore）
│
├── signals/
│   ├── momentum.py                 CS 动量（12-1m）+ TS 动量崩溃过滤 + 加速度
│   ├── value.py                    P/E 百分位相对估值信号（含 EPS store 集成）
│   ├── regime.py                   4 态 Regime 检测（规则型 + HMM 可选）
│   └── composite.py                多因子聚合 + Regime 条件权重调整
│
├── portfolio/
│   ├── optimizer.py                逆波动率 / 风险平价 / GMV + Ledoit-Wolf 协方差
│   ├── risk.py                     波动率缩放 + VIX 应急 + 回撤断路器 + beta
│   └── rebalance.py                月度调度 + 阈值过滤 + 换手率上限
│
├── backtest/
│   ├── engine.py                   事件驱动月度回测 + walk-forward 支持
│   ├── costs.py                    按 ETF 流动性分级的点差 + 市场冲击成本模型
│   ├── metrics.py                  Sharpe / Calmar / IR / CVaR / Brinson 归因
│   └── sensitivity.py              参数敏感性扫描
│
├── report/
│   ├── plots.py                    全部 matplotlib 可视化函数
│   └── tearsheet.py                多页 PDF 绩效报告生成器
│
├── tests/
│   ├── test_signals.py             信号单元测试（无网络）
│   ├── test_optimizer.py           组合 / 风险单元测试
│   ├── test_backtest.py            成本 + 指标单元测试
│   └── test_engine_smoke.py        端到端 smoke 测试（合成数据，无网络）
│
├── notebooks/
│   ├── 01_data_exploration.ipynb   行业相关性、滚动统计
│   ├── 02_signal_research.ipynb    IC 分析、信号衰减曲线
│   └── 03_backtest_analysis.ipynb  全量回测深度分析
│
├── logs/                           运行日志（gitignore）
├── pipeline_state/                 Pipeline 状态文件（gitignore）
└── trading_signals/                信号输出目录（gitignore）
```

### 关键数据路径（项目根目录）

| 路径 | 内容 | 读/写 |
|---|---|---|
| `price_data/sector_etfs/eps_history.json` | 55 只 GICS 成分股季度 EPS 历史 | 读写（`update_eps_history.py`） |
| `price_data/sector_etfs/*.parquet` | ETF 日线 OHLCV 缓存 | 读写（`data/loader.py`） |
| `price_data/macro/*.parquet` | 宏观指标 parquets | **只读**（由 someopark 主 pipeline 写入） |
| `qlib-main/sector_rotation/inventory_sector_rotation.json` | 当前持仓快照 | 读写（`SectorRotationDailySignal.py`） |
| `qlib-main/sector_rotation/inventory_history/` | 每次 inventory 变更时的快照备份 | 写 |
| `qlib-main/sector_rotation/trading_signals/` | 每日信号 JSON / 报告 | 写 |
| `qlib-main/sector_rotation/report/output/` | Tearsheet PDF 输出（`tearsheet` 模式） | 写 |
| `qlib-main/sector_rotation/logs/` | Pipeline 日志 | 写 |
| `qlib-main/sector_rotation/pipeline_state/` | Pipeline 状态标记 | 写 |
| `qlib-main/mlruns/mlflow.db` | MLflow 实验追踪 SQLite（backtest / sensitivity run 历史） | 读写（`backtest/engine.py`） |

---

## 配置参考

所有参数在 `config.yaml` 中管理：

| 节 | 键 | 默认值 | 说明 |
|---|---|---|---|
| `data` | `price_source` | `"yfinance"` | 主数据源（`"mongodb"` 可选） |
| `data` | `macro_path` | `"price_data/macro"` | 宏观 parquet 目录（只读） |
| `signals.weights` | `cross_sectional_momentum` | `0.40` | CS 动量权重 |
| `signals.weights` | `ts_momentum` | `0.15` | TS 动量权重 |
| `signals.weights` | `relative_value` | `0.20` | 估值权重 |
| `signals.weights` | `regime_adjustment` | `0.25` | Regime 调整权重 |
| `signals` | `value_source` | `"polygon"` | EPS 来源（`"constituents"` / `"proxy"` / `"yfinance_info"`） |
| `signals.regime` | `method` | `"rules"` | Regime 检测方法（`"hmm"` 可选） |
| `portfolio` | `optimizer` | `"inv_vol"` | 权重方法（`"risk_parity"` / `"gmv"`） |
| `portfolio` | `top_n_sectors` | `4` | 持仓行业数 |
| `portfolio.constraints` | `max_weight` | `0.40` | 单行业上限 |
| `rebalance` | `zscore_change_threshold` | `0.5` | 触发再平衡的信号变化阈值 |
| `risk.vol_scaling` | `enabled` | `true` | 波动率缩放 |
| `backtest` | `start_date` | `"2018-07-01"` | 回测起点（XLC 创立日，不可早于此日期） |
| `backtest` | `initial_capital` | `1_000_000` | 初始资金 USD |

---

## 全参数完整参考

> 所有 `config.yaml` 参数均可在不修改代码的情况下调整。代码内硬编码常量需直接修改对应 `.py` 文件。

---

### 一、数据参数 `data`

#### `data.cache_dir`
- **默认值** `"../../price_data/sector_etfs"`
- ETF 价格和 EPS 缓存目录（相对于 `qlib-main/sector_rotation/`）
- 引用：`data/loader.py`、`SectorRotationDailySignal.py`

#### `data.price_source`
- **默认值** `"yfinance"`
- 价格数据来源，可选 `"yfinance"` 或 `"mongodb"`
- 引用：`data/loader.py`

#### `data.price_start`
- **默认值** `"2017-01-01"`
- 价格历史起始日，比回测起点早以提供信号 warm-up 数据
- 引用：`data/loader.py`、`SectorRotationDailySignal.py`

#### `data.price_end`
- **默认值** `null`
- 价格历史截止日，`null` = 今日
- 引用：`data/loader.py`

#### `data.fred_api_key_env`
- **默认值** `"FRED_API_KEY"`
- FRED API key 的环境变量名
- 引用：`data/loader.py`

#### `data.mongodb.*`
- 子参数：`host_env`、`port`（27017）、`db`（`"market_data"`）、`collection`（`"prices"`）
- 仅在 `price_source="mongodb"` 时生效
- 引用：`data/loader.py`

---

### 二、标的参数 `universe`

#### `universe.etfs`
- **默认值** `["XLE", "XLB", "XLI", "XLY", "XLP", "XLV", "XLF", "XLK", "XLC", "XLU", "XLRE"]`
- 11 个 GICS SPDR 行业 ETF，构成完整轮动宇宙
- 引用：`backtest/engine.py`、`SectorRotationDailySignal.py`

#### `universe.benchmark`
- **默认值** `"SPY"`
- 基准指数，用于 Alpha/IR 计算和 beta 约束
- 引用：`backtest/engine.py`、`SectorRotationDailySignal.py`

#### `universe.universe_start`
- **默认值** `"2018-07-01"`
- 完整 11 板块宇宙最早有效日期（XLC 于 2018-06-18 上市）
- 注意：`data/universe.py` 同时将此值硬编码为常量 `UNIVERSE_START`

---

### 三、信号参数 `signals`

#### 3.1 信号权重（`signals.weights`）

> 四个权重之和必须 = 1.0

| 参数 | 当前值 | 说明 | 引用 |
|---|---|---|---|
| `cross_sectional_momentum` | `0.40` | 截面动量在复合信号中的权重 | `signals/composite.py`、`backtest/engine.py` |
| `ts_momentum` | `0.15` | 时序动量（crash filter 乘数）权重 | `signals/composite.py` |
| `relative_value` | `0.20` | 相对估值（P/E 百分位）权重 | `signals/composite.py` |
| `regime_adjustment` | `0.25` | Regime 条件调整权重（通过乘数影响其他三个信号） | `signals/composite.py` |

#### 3.2 截面动量（`signals.cs_momentum`）

| 参数 | 默认值 | 说明 | 引用 |
|---|---|---|---|
| `lookback_months` | `12` | 总回看窗口（12-1 动量，即过去 12 个月跳过最近 1 月） | `signals/composite.py`、`signals/momentum.py` |
| `skip_months` | `1` | 跳过最近 N 月，避免短期反转效应 | `signals/composite.py`、`signals/momentum.py` |
| `zscore_window` | `36` | Z-score 标准化的滚动窗口（月数，约 3 年） | `signals/composite.py`、`signals/momentum.py` |

#### 3.3 时序动量（`signals.ts_momentum`）

| 参数 | 默认值 | 说明 | 引用 |
|---|---|---|---|
| `lookback_months` | `12` | 自身 12 月回报，判断板块趋势方向（crash filter） | `signals/composite.py`、`signals/momentum.py` |
| `crash_filter_multiplier` | `0.0` | TS 动量 < 0 时的权重乘数（0 = 完全排除，1 = 不过滤） | `signals/composite.py`、`signals/momentum.py` |

#### 3.4 相对估值（`signals.value`）

| 参数 | 默认值 | 说明 | 引用 |
|---|---|---|---|
| `value_source`（顶层） | `"constituents"` | P/E 数据来源：`"constituents"`（yfinance 季报）/ `"proxy"`（价格比历史均值）/ `"polygon"`（Polygon API） | `backtest/engine.py`、`SectorRotationDailySignal.py` |
| `value.pe_lookback_years` | `10` | P/E 百分位历史窗口（年数） | `signals/composite.py`、`signals/value.py` |
| `value.missing_data_weight` | `0.0` | P/E 数据缺失时的分数（0 = 中性跳过） | `signals/composite.py`、`signals/value.py` |

#### 3.5 加速因子（`signals.acceleration`）

| 参数 | 默认值 | 说明 | 引用 |
|---|---|---|---|
| `enabled` | `true` | 是否启用动量加速度奖励分 | `signals/composite.py` |
| `lookback_months` | `3` | 短期加速度回看窗口（月数） | `signals/composite.py` |
| `weight_boost` | `0.05` | 高加速度板块的复合分数加成 | `signals/composite.py` |

#### 3.6 Regime 检测（`signals.regime`）

**基础阈值**

| 参数 | 默认值 | 说明 | 引用 |
|---|---|---|---|
| `method` | `"rules"` | Regime 检测方法：`"rules"`（规则型）或 `"hmm"`（需要 hmmlearn） | `backtest/engine.py`、`SectorRotationDailySignal.py` |
| `vix_high_threshold` | `25.0` | VIX > 此值 → risk-off | `signals/regime.py`、`SectorRotationDailySignal.py` |
| `vix_extreme_threshold` | `35.0` | VIX > 此值 → 紧急去风险（emergency de-risk） | `signals/regime.py`、`backtest/engine.py` |
| `hy_spread_high_bps` | `450` | HY OAS > 此值（bps）→ 信用压力信号 | `signals/regime.py` |
| `yield_curve_inversion` | `-0.10` | 10Y-2Y 利差 < 此值 → 收益率曲线倒挂警告 | `signals/regime.py` |
| `ism_expansion` | `50.0` | ISM > 此值 → 扩张期，否则收缩期 | `signals/regime.py` |

**Regime 条件信号乘数（`signals.regime.regime_weights`）**

| Regime 状态 | `cs_mom` 乘数 | `ts_mom` 乘数 | `value` 乘数 | 说明 |
|---|---|---|---|---|
| `risk_on` | `1.0` | `1.0` | `1.0` | 全信号标准权重 |
| `risk_off` | `0.6` | `0.8` | `1.2` | 压制动量（恐慌中拥挤效应），提升估值可靠性 |
| `transition_up` | `1.2` | `1.0` | `0.8` | 强化动量（上升周期领头板块），减弱估值偏差 |
| `transition_down` | `0.7` | `0.9` | `1.1` | 保守动量，提升估值防守性 |

**防御板块配置**

| 参数 | 默认值 | 说明 | 引用 |
|---|---|---|---|
| `defensive_sectors` | `["XLU", "XLP", "XLV"]` | risk_off 时获得 Z-score 加分的防御板块 | `signals/composite.py`、`SectorRotationDailySignal.py` |
| `defensive_bonus_risk_off` | `0.3` | risk_off 状态下防御板块的 Z-score 加分幅度 | `signals/composite.py`、`SectorRotationDailySignal.py` |

---

### 四、投资组合参数 `portfolio`

| 参数 | 默认值 | 说明 | 引用 |
|---|---|---|---|
| `optimizer` | `"inv_vol"` | 权重优化方法：`"inv_vol"`、`"risk_parity"`、`"gmv"`、`"mvo"`、`"equal_weight"` | `backtest/engine.py`、`portfolio/optimizer.py` |
| `cov.method` | `"ledoit_wolf"` | 协方差估计方法：`"ledoit_wolf"`、`"oas"`、`"structured_pca"`、`"sample"` | `backtest/engine.py`、`portfolio/optimizer.py` |
| `cov.lookback_days` | `252` | 协方差估计滚动窗口（交易日数） | `backtest/engine.py`、`portfolio/optimizer.py` |
| `cov.min_periods` | `63` | 协方差估计所需最少观测日数 | `portfolio/optimizer.py` |
| `constraints.max_weight` | `0.40` | 单个板块最大权重（防止 XLK 等过度集中） | `backtest/engine.py`、`portfolio/risk.py`、`portfolio/optimizer.py` |
| `constraints.min_weight` | `0.00` | 单个板块最小权重（0 = 允许空仓） | `backtest/engine.py`、`portfolio/optimizer.py` |
| `constraints.max_cash` | `0.50` | 最大现金比例，实际由 `emergency_cash_pct` 控制 | 文档参数 |
| `constraints.beta_min` | `0.70` | 组合相对 SPY 的最低 beta（放宽后优化器自由度更高） | `portfolio/risk.py` |
| `constraints.beta_max` | `1.10` | 组合相对 SPY 的最高 beta | `portfolio/risk.py` |
| `top_n_sectors` | `4` | 持有的核心板块数量（复合分最高的前 N 个） | `backtest/engine.py`、`portfolio/optimizer.py` |
| `min_zscore` | `-0.5` | 分配权重所需最低复合 Z-score（低于此值 → 0 仓位） | `backtest/engine.py`、`portfolio/optimizer.py` |
| `weight_scheme` | `"rank"` | 权重缩放方式：`"rank"` 或 `"zscore_softmax"` | `portfolio/optimizer.py` |

---

### 五、调仓参数 `rebalance`

| 参数 | 默认值 | 说明 | 引用 |
|---|---|---|---|
| `frequency` | `"monthly"` | 调仓频率：`"monthly"` 或 `"biweekly"` | `SectorRotationDailySignal.py` |
| `rebalance_day` | `"first_trading_day"` | 月内调仓时间：`"first_trading_day"` 或 `"last_trading_day"` | `SectorRotationDailySignal.py` |
| `zscore_change_threshold` | `0.5` | Z-score 变化 < 此值时跳过该板块调仓（降低无效换手） | `backtest/engine.py`、`SectorRotationDailySignal.py`、`portfolio/rebalance.py` |
| `emergency_derisk_vix` | `32.0` | VIX 超过此值触发紧急去风险（强制至 50% 现金）| `backtest/engine.py`、`SectorRotationDailySignal.py`、`portfolio/risk.py` |
| `emergency_cash_pct` | `0.50` | 紧急去风险时的目标现金比例 | `backtest/engine.py`、`SectorRotationDailySignal.py`、`portfolio/risk.py` |
| `max_monthly_turnover` | `0.80` | 单侧最大月度换手率上限（超过则混合新旧权重降低冲击） | `backtest/engine.py`、`SectorRotationDailySignal.py`、`portfolio/rebalance.py` |
| `vix_recovery_factor` | `0.80`（代码硬编码，未暴露至 yaml） | 紧急状态解除所需 VIX 降至阈值的比例（如阈值 32，恢复线 = 32 × 0.80 = 25.6） | `SectorRotationDailySignal.py` |

---

### 六、风险管理参数 `risk`

#### 6.1 波动率缩放（`risk.vol_scaling`）

| 参数 | 默认值 | 说明 | 引用 |
|---|---|---|---|
| `enabled` | `true` | 是否启用波动率缩放（realized vol 过高时缩减仓位） | `backtest/engine.py`、`portfolio/risk.py` |
| `target_vol_annual` | `0.12` | 目标年化波动率（12%）；超目标时等比缩减权重 | `backtest/engine.py`、`portfolio/risk.py` |
| `estimation_window` | `20` | 计算实际波动率的滚动窗口（交易日） | `backtest/engine.py`、`portfolio/risk.py` |
| `scale_threshold` | `1.5` | 仅当 `realized_vol > threshold × historical_avg` 时触发缩放（避免过度调整） | `backtest/engine.py`、`portfolio/risk.py` |
| `historical_window` | `252` | 计算历史平均波动率的窗口（交易日，约 1 年） | `backtest/engine.py`、`portfolio/risk.py` |

#### 6.2 回撤熔断器（`risk.drawdown`）

| 参数 | 默认值 | 说明 | 引用 |
|---|---|---|---|
| `monthly_dd_alert` | `-0.08` | 单月回撤超过此值时记录警告日志（不触发自动操作） | 文档参数 |
| `cumulative_dd_halve` | `-0.20` | 累计回撤超过此值 → 仓位减半（COVID 级别极端事件才触发） | `backtest/engine.py`、`portfolio/risk.py` |
| `cumulative_dd_recovery` | `-0.10` | 累计回撤恢复至此值 → 解除熔断 | 文档参数 |

#### 6.3 渐进式 VIX 去风险（`risk.vix_progressive_derisk`）

> 在 `emergency_derisk_vix` 触发前，通过阶梯现金配置逐步降低风险敞口。与紧急去风险独立运作。

| 参数 | 当前值 | 说明 | 引用 |
|---|---|---|---|
| `enabled` | `true` | 启用阶梯式现金增加（代替单一硬切） | `backtest/engine.py`、`portfolio/risk.py` |
| `tiers[0]` | `{vix_above: 28, cash_pct: 0.15}` | 第一档：VIX > 28 → 持有 15% 现金 | `portfolio/risk.py` |
| `tiers[1]` | `{vix_above: 32, cash_pct: 0.35}` | 第二档：VIX > 32 → 持有 35% 现金 | `portfolio/risk.py` |

VIX 完整阶梯：

```
VIX < 28  → 0% cash（全仓）
VIX ≥ 28  → 15% cash
VIX ≥ 32  → 35% cash
VIX ≥ 35  → 50% cash（emergency_derisk_vix 触发）
```

---

### 七、回测参数 `backtest`

| 参数 | 默认值 | 说明 | 引用 |
|---|---|---|---|
| `start_date` | `"2018-07-01"` | 回测起始日（不能早于 XLC 上市日，否则 11 板块宇宙不完整） | `backtest/engine.py` |
| `end_date` | `null` | 回测截止日，`null` = 最新可用数据 | `backtest/engine.py` |
| `initial_capital` | `1_000_000.0` | 初始资本（美元） | `backtest/engine.py`、`SectorRotationDailySignal.py` |
| `is_years` | `3` | Walk-forward 中样本期长度（年） | `backtest/engine.py` |
| `oos_months` | `12` | Walk-forward 样本外评估期（月） | `backtest/engine.py` |
| `walk_forward.enabled` | `false` | 是否启用 walk-forward 验证 | `backtest/engine.py` |
| `walk_forward.step_months` | `6` | Walk-forward 窗口每次滚动步长（月） | `backtest/engine.py` |

---

### 八、交易成本参数 `costs`

| 参数 | 默认值 | 说明 | 引用 |
|---|---|---|---|
| `tier_1_tickers` | `["XLE", "XLK", "XLF", "XLV"]` | 流动性最高（日均成交量 $1B+），单向成本 3 bps | `backtest/costs.py` |
| `tier_2_tickers` | `["XLB", "XLI", "XLY", "XLP", "XLU"]` | 中等流动性，单向成本 5 bps | `backtest/costs.py` |
| `tier_3_tickers` | `["XLC", "XLRE"]` | 流动性较低，单向成本 8 bps | `backtest/costs.py` |
| `tier_1_cost_bps` | `3` | Tier 1 单向交易成本（价差 + 市场冲击，bps） | `backtest/costs.py` |
| `tier_2_cost_bps` | `5` | Tier 2 单向交易成本 | `backtest/costs.py` |
| `tier_3_cost_bps` | `8` | Tier 3 单向交易成本 | `backtest/costs.py` |
| `etf_fee_bps` | `9` | ETF 年管理费（bps），逐日从收益中扣除（9 bps ≈ SPDR 平均 expense ratio） | `backtest/costs.py`、`backtest/engine.py`、`SectorRotationDailySignal.py` |

---

### 九、报告参数 `report`

| 参数 | 默认值 | 说明 |
|---|---|---|
| `output_dir` | `"report/output"` | Tearsheet PDF 输出目录 |
| `figsize` | `[14, 8]` | 图表尺寸（英寸） |
| `dpi` | `150` | 图表分辨率 |
| `pdf_filename` | `"sector_rotation_tearsheet.pdf"` | PDF 报告文件名 |
| `strategy_color` | `"#1f77b4"` | 策略曲线颜色 |
| `benchmark_color` | `"#ff7f0e"` | 基准曲线颜色 |
| `ew_color` | `"#2ca02c"` | 等权基准曲线颜色 |

---

### 十、代码内硬编码常量

> 以下常量未暴露至 `config.yaml`，如需调整须直接修改对应 `.py` 文件。

#### `SectorRotationDailySignal.py`

| 常量 | 值 | 位置 | 说明 |
|---|---|---|---|
| `REBALANCE_THRESHOLD` | `0.03` | `:93` | 权重变化 < 3% 时不触发该板块调仓（独立于 `zscore_change_threshold`） |
| `cache_max_age_hours` | `8.0` | `:238` | 价格/宏观数据缓存过期时间（小时） |
| `macro warmup min_rows` | `252` | `:210` | 宏观数据可用前至少需要 1 年历史 |

#### `signals/regime.py`

| 常量 | 值 | 位置 | 说明 |
|---|---|---|---|
| `rolling_window` | `252` | `:77` | 宏观指标 Z-score 标准化的滚动窗口（交易日） |
| `min_periods` | `63` | `:78` | 标准化所需最少观测日数 |
| `smoothing_days` | `5` | `:248, 314` | Regime 标签滚动众数平滑天数（减少信号抖动） |

#### `portfolio/risk.py`

| 常量 | 值 | 位置 | 说明 |
|---|---|---|---|
| `beta_window` | `252` | `:158` | OLS 回归估计 beta 的滚动窗口（交易日） |
| `beta_min_periods` | `20` | `:170` | Beta 估计所需最少观测日数 |
| `beta_mix_alpha max` | `0.3` | `:413` | Beta 调整时向等权混合的最大比例（30%） |
| `concentration max_iter` | `100` | `:382` | 权重约束的 water-filling 迭代次数 |

#### `portfolio/optimizer.py`

| 常量 | 值 | 位置 | 说明 |
|---|---|---|---|
| `num_factors` | `3` | `:156, 218, 438` | `structured_pca` 协方差估计的因子数 |
| `risk_parity max_iter` | `500` | `:319` | Risk parity 优化器最大迭代次数 |
| `risk_parity tol` | `1e-8` | `:320` | Risk parity 收敛容差 |
| `gmv max_iter` | `500` | `:390` | GMV scipy 优化最大迭代次数 |
| `gmv ftol` | `1e-9` | `:390` | GMV 函数容差 |

#### `signals/momentum.py`

| 常量 | 值 | 位置 | 说明 |
|---|---|---|---|
| `accel_short` | `3` | `:165` | 加速度因子短期回看（月） |
| `accel_long` | `12` | `:166` | 加速度因子长期回看（月） |

#### `signals/value.py`

| 常量 | 值 | 位置 | 说明 |
|---|---|---|---|
| `PE max cap` | `300` | `:323` | 异常 P/E 上限截断值（防止极端值扭曲百分位） |
| `PE percentile min_periods` | `24` | `:669` | P/E 百分位计算所需最少历史季度数 |
| `yfinance pause_sec` | `0.3` | `:191` | yfinance API 调用间隔（秒，限流） |
| `Polygon pause_sec` | `0.2` | `:223` | Polygon API 调用间隔（秒，限流） |

#### `data/universe.py`

| 常量 | 值 | 位置 | 说明 |
|---|---|---|---|
| `UNIVERSE_START` | `date(2018, 7, 1)` | `:32` | 最早有效日期（与 `universe.universe_start` 保持一致） |
| `GICS_COMMSVCS_BREAK` | `date(2018, 9, 28)` | `:35` | XLC 成分股重组日期（GICS 结构性断点） |

#### `data/loader.py`

| 常量 | 值 | 位置 | 说明 |
|---|---|---|---|
| `cache_max_age_hours` | `8.0` | `:188, 414` | 默认缓存过期时间（小时） |
| `monthly ff fill limit` | `31` | `:488` | 月频数据前向填充最大天数 |
| `weekly ff fill limit` | `7` | `:490` | 周频数据前向填充最大天数 |
| `daily ff fill limit` | `5` | `:493` | 日频数据前向填充最大天数 |

#### `backtest/metrics.py`

| 常量 | 值 | 位置 | 说明 |
|---|---|---|---|
| `periods_per_year` | `252` | `:87–237` | 年化收益率计算所用的每年交易日数 |
| `SUBPERIODS` | 固定日期列表 | `:594–600` | 子期分析固定分段：Pre-COVID Bull / COVID Crash / Recovery / Rate Hike / Post-Hike |

---

### 参数总览

| 类别 | `config.yaml` 参数数 | 代码内硬编码常量 |
|---|---|---|
| 数据（`data`） | 9 | 3 |
| 标的（`universe`） | 3 | 2 |
| 信号权重（`signals.weights`） | 4 | — |
| 截面 / 时序动量 | 5 | 3 |
| 相对估值 | 3 | 4 |
| 加速因子 | 3 | — |
| Regime 检测 | 14 | 3 |
| 投资组合（`portfolio`） | 9 | 5 |
| 调仓（`rebalance`） | 7 | 1 |
| 风险管理（`risk`） | 8 | 3 |
| 回测（`backtest`） | 7 | — |
| 交易成本（`costs`） | 7 | 1 |
| 报告（`report`） | 7 | — |
| **合计** | **~86** | **~25** |

---

## 参数集扫描（SectorRotationStrategyRuns）

`SectorRotationStrategyRuns.py` 定义了 **59 个命名参数集**，通过 dotted-path override 应用于 `config.yaml` 基准配置，与 `SectorRotationBatchRun.py` 联合使用进行批量参数空间扫描。

### 快速使用

```python
from SectorRotationStrategyRuns import PARAM_SETS, apply_param_set, list_param_sets

# 查看全部参数集
list_param_sets()

# 应用单个参数集进行回测
from sector_rotation.data.loader import load_config
from sector_rotation.backtest.engine import SectorRotationBacktest

base_cfg = load_config()
cfg = apply_param_set(base_cfg, PARAM_SETS['crisis_defense'])
result = SectorRotationBacktest(cfg).run(prices=prices, macro=macro)
```

```bash
# 批量运行全部 59 集（仅输出分析，不影响生产）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh batch
# 结果：backtest_results/sr_batch_summary_<timestamp>.csv + .xlsx

# 批量运行 + 选优 → 写入 selected_param_set.json（影响生产 daily/weekly/monthly）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh select
# 额外写入：sector_rotation/selected_param_set.json

# 仅运行特定组（不影响生产）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh batch --group L
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh batch --group A B C --sort-by calmar
```

### 设计质量保证

| 维度 | 状态 | 说明 |
|------|------|------|
| **参数集总数** | ✅ 59 集 | A(6)+B(5)+C(4)+D(5)+E(5)+F(5)+G(4)+H(4)+I(4)+J(4)+K(3)+L(6)+M(4) |
| **最少参数数** | ✅ 全部 ≥10 | 最小 10，平均 14.2，Group L 最高 23 |
| **信号权重约束** | ✅ 全部 = 1.0 | CS + TS + RV + REG 严格等于 1.0 |
| **学术根基** | ✅ 19 篇文献 | 每个参数值均有对应理论来源 |
| **受控实验设计** | ✅ Group C/I/M | crash_filter、vol_scaling、value_source、acceleration 均有纯净对照 |
| **内部一致性** | ✅ 已验证 | F4/L1 VIX 逻辑，A2/A4 机制乘数，D4 max_weight |

### 59 个参数集总览

**信号权重约束规则**：所有覆盖 `signals.weights.*` 的参数集，四个权重之和严格 = 1.0。

#### Group A — Signal Factor Architecture（信号因子架构）

| 编号 | 名称 | 参数数 | 核心设计 | 学术依据 |
|------|------|--------|----------|----------|
| A1 | `default` | 12 | CS=0.40 TS=0.15 RV=0.20 REG=0.25；12-1月；acceleration开 | JT1993 |
| A2 | `momentum_heavy` | 16 | CS+TS=0.70；9-0月；risk_off cs=0.5；transition_down cs=0.6 | AMP2013 |
| A3 | `value_tilt` | 14 | RV=0.35；15月慢速；crash_filter=0.3；risk_off RV=1.4 | FF1992 |
| A4 | `regime_driven` | 18 | REG=0.40；vix_high=22；VIX渐进22→20%/26→38% | AB2007 |
| A5 | `ts_dominant` | 16 | TS=0.30；vol=10%；scale=1.2；beta_max=0.95 | MOP2012 |
| A6 | `balanced_four` | 12 | 四因子各0.25；zscore_softmax（无偏基准） | DGU2009 |

#### Group B — Momentum Microstructure（动量微结构）

| 编号 | 名称 | 参数数 | 核心设计 | 学术依据 |
|------|------|--------|----------|----------|
| B1 | `fast_momentum` | 15 | lookback=6，skip=0，zscore_win=24 | JT2001 |
| B2 | `medium_momentum` | 12 | lookback=9，skip=1，zscore_win=30；threshold=0.4 | JT1993 |
| B3 | `no_skip_medium` | 12 | lookback=9，**skip=0**，zscore_win=30（孤立 skip 效应，对比 B2） | JT2001 |
| B4 | `slow_momentum` | 13 | lookback=15，skip=2，zscore_win=48；cov=504天 | Asness1997 |
| B5 | `skip_heavy` | 11 | lookback=12，skip=2，crash_filter=0.5 | JT2001 |

> B2 vs B3：唯一变量 skip=1 vs skip=0，量化短期反转效应的净贡献。

#### Group C — Momentum Crash Protection（动量崩溃保护）

| 编号 | 名称 | 参数数 | 核心设计 | 学术依据 |
|------|------|--------|----------|----------|
| C1 | `full_crash_filter_tight_vol` | 15 | crash=0.0；vol=10%；VIX梯度25/30；两层防御 | DM2016 |
| C2 | `partial_filter_scaled` | 13 | crash=0.5；vol=9%；scale=1.2；dd=-0.18 | BSC2015 |
| C3 | `vol_crash_shield` | 15 | crash=0.0；vol=7%；est_win=10天；VIX 24/28梯度 | MM2017 |
| C4 | `no_filter_vol_only` | 11 | **crash=1.0**；vol=9%（量化 crash_filter 独立贡献） | MM2017 |

> C1 vs C4：crash_filter 边际贡献；C1 vs C3：DM2016 vs MM2017 最优 vol 目标（10% vs 7%）。

#### Group D — Portfolio Construction Theory（组合构建理论）

| 编号 | 名称 | 参数数 | 核心设计 | 学术依据 |
|------|------|--------|----------|----------|
| D1 | `inv_vol_lw` | 12 | inv_vol + LW；lookback=252（生产默认） | LW2004 |
| D2 | `risk_parity_lw` | 15 | ERC + LW；top_n=5；min_zscore=-0.3；vol=11% | MRT2010 |
| D3 | `gmv_lw` | 13 | GMV + LW；lookback=504；beta_max=0.95 | CD2006 |
| D4 | `equal_weight_optimizer` | 12 | 等权重；max_weight=0.25（5板块等权≈20%，合理软上限） | DGU2009 |
| D5 | `inv_vol_oas` | 11 | inv_vol + OAS；lookback=126；zscore_softmax | Chen2010 |

#### Group E — Position Concentration（持仓集中度）

| 编号 | 名称 | 参数数 | 核心设计 | 学术依据 |
|------|------|--------|----------|----------|
| E1 | `concentrated_3` | 14 | top_n=3；min_zscore=0.0；max_w=0.45；softmax | GK2000 |
| E2 | `standard_4` | 10 | top_n=4；min_zscore=-0.5；max_w=0.40；rank（默认） | — |
| E3 | `diversified_5_rp` | 12 | top_n=5；risk_parity；max_w=0.35；softmax；vol=11% | Markowitz1952 |
| E4 | `broad_6_softmax` | 11 | top_n=6；min_zscore=-0.8；max_w=0.28；softmax | Britten-Jones1999 |
| E5 | `score_gated_dynamic` | 11 | top_n=5；min_zscore=0.25（过滤底部40%信号） | GK2000 |

#### Group F — Regime Detection Architecture（机制检测架构）

*每集 18-19 个参数，完整显式化 5 个宏观阈值 + 12 个机制状态乘数 + 防御板块奖励。*

| 编号 | 名称 | 参数数 | 核心设计 | 学术依据 |
|------|------|--------|----------|----------|
| F1 | `hawkish_macro` | 18 | vix_high=20；HY=380；tu cs×1.3；def_bonus=0.45 | AB2007 |
| F2 | `standard_regime` | 18 | vix=25/35；HY=450；所有12乘数完整（生产基准） | W2009 |
| F3 | `dovish_macro` | 18 | vix=28/40；HY=500；def_bonus=0.15 | AB2007 |
| F4 | `momentum_biased_regime` | 19 | tu cs×1.4；**emergency_derisk_vix=37**（与vix_extreme=37一致） | AMP2013 |
| F5 | `defensive_rotation` | 18 | risk_off cs=0.3（最低）；def_bonus=0.50σ；vix=22/32 | 2022年经验 |

#### Group G — Rebalance & Transaction Cost（调仓与交易成本）

| 编号 | 名称 | 参数数 | 核心设计 | 学术依据 |
|------|------|--------|----------|----------|
| G1 | `low_turnover` | 11 | threshold=0.8σ；max_turn=45%；last_trading_day | GP2013 |
| G2 | `responsive` | 12 | threshold=0.2σ；max_turn=100%；skip=0 | GK2000 |
| G3 | `biweekly_controlled` | 13 | biweekly；max_turn=60%；est_win=10天 | GP2013 |
| G4 | `ultra_selective` | 12 | threshold=1.0σ；max_turn=40%；cov=504天 | GP2013 |

#### Group H — VIX De-risk Architecture（VIX 降风险架构）

| 编号 | 名称 | 参数数 | 核心设计 | 学术依据 |
|------|------|--------|----------|----------|
| H1 | `binary_derisk_35` | 11 | 渐进关闭；VIX=35→50%现金（传统经典） | W2009 |
| H2 | `binary_derisk_28` | 12 | 渐进关闭；VIX=28→60%现金；dd=-0.18 | — |
| H3 | `progressive_current` | 11 | VIX>28→15%；VIX>32→35%；VIX≥35→50%（生产配置） | — |
| H4 | `progressive_conservative` | 12 | VIX>30→10%；VIX>36→25%；VIX≥40→50% | W2009 |

#### Group I — Volatility Scaling Science（波动率缩放科学）

| 编号 | 名称 | 参数数 | 核心设计 | 学术依据 |
|------|------|--------|----------|----------|
| I1 | `no_vol_scaling` | 10 | **vol_scaling=False**（对照组，量化 MM2017 命题） | MM2017 |
| I2 | `tight_vol_8pct` | 12 | vol=8%；scale=1.3；est_win=15天（MM2017 最优目标） | MM2017 |
| I3 | `standard_vol_target` | 12 | vol=12%；scale=1.5；est_win=20天（生产默认） | — |
| I4 | `relaxed_vol_target` | 12 | vol=16%；scale=2.0；hist_win=504天；beta_max=1.25 | — |

> I1 vs I3：vol_scaling 总贡献；I2 vs I3：MM2017 最优目标（8%）vs 市场中性目标（12%）。

#### Group J — Beta & Market Exposure（Beta 与市场暴露）

| 编号 | 名称 | 参数数 | 核心设计 | 学术依据 |
|------|------|--------|----------|----------|
| J1 | `tight_beta_tracker` | 11 | beta 0.80-1.05；GMV+LW；vol=11% | — |
| J2 | `standard_beta` | 11 | beta 0.70-1.10；inv_vol+LW（默认） | — |
| J3 | `low_beta_bab` | 13 | beta 0.40-0.82；GMV；RV=0.32；vol=8% | FP2014 BAB |
| J4 | `high_beta_growth` | 13 | beta 0.90-1.32；crash_filter=0.5；vol=16% | FP2014 |

#### Group K — Drawdown Circuit Breaker（回撤熔断器）

| 编号 | 名称 | 参数数 | 核心设计 | 学术依据 |
|------|------|--------|----------|----------|
| K1 | `sensitive_dd` | 12 | -12% 触发；-6% 恢复；vol=10% | GZ1993 短期 |
| K2 | `standard_dd` | 11 | **-20% 触发；-10% 恢复**（生产默认） | Cvitanic-Karatzas |
| K3 | `patient_dd` | 12 | -28% 触发；-14% 恢复；scale=1.7；beta_max=1.18 | GZ1993 长期 |

#### Group L — Market Regime Archetypes（市场环境原型档案）

*面向特定宏观环境的完整参数组合（20-23 参数/集），经内在一致性验证。*

| 编号 | 名称 | 参数数 | 核心场景 | 关键设计 |
|------|------|--------|----------|----------|
| L1 | `tech_bull_2023` | 23 | 2023 AI/科技牛市 | crash=1.0；skip=0；beta_max=1.30；VIX渐进延迟至35/40 |
| L2 | `crisis_defense` | 22 | 2020/2022 系统性风险 | REG=0.42；vix=20；HY=380；VIX24/28梯度；vol=8% |
| L3 | `stagflation` | 19 | 2022 滞胀 | RV=0.35；15月慢速；pe_lookback=7；beta_max=0.92 |
| L4 | `rate_hike_cycle` | 21 | 2022 加息周期 | vix_high=22；HY=420；vol=9%；beta_max=0.92 |
| L5 | `early_recovery` | 21 | 2020Q4/2023H1 复苏前期 | biweekly；skip=0；crash=1.0；tu cs×1.4 |
| L6 | `low_vol_grind` | 21 | 2017/2019 低波慢牛 | vol=8%；threshold=0.75；last_trading_day；top_n=5 |

#### Group M — Isolated Factor Tests（孤立因子测试）

*其余参数保持与 A1 `default` 一致，只改变单一维度。*

| 编号 | 名称 | 参数数 | 测试假设 | 学术依据 |
|------|------|--------|----------|----------|
| M1 | `value_constituents` | 11 | value_source='constituents'（TTM P/E 精确，对照基准） | FF1992 |
| M2 | `value_proxy` | 11 | value_source='proxy'（价格代理，快速低成本） | — |
| M3 | `acceleration_on` | 11 | acceleration=True，weight_boost=0.05（对照基准） | Grinblatt-Han2005 |
| M4 | `acceleration_off` | 10 | acceleration=False（消除加速信号全部影响） | — |

> M1 vs M2：TTM P/E 精确数据 vs 价格代理的 alpha 贡献；M3 vs M4：加速因子独立 alpha 贡献。

### 批量运行与推荐对比

```bash
# 关键对比分析（运行 batch/select 后查看 backtest_results/*.xlsx）
# B2 vs B3: skip=1 vs skip=0（孤立 skip 效应）
# C1 vs C4: crash_filter 独立贡献
# I1 vs I3: vol_scaling 总贡献
# M1 vs M2: value_source 数据质量贡献
# M3 vs M4: acceleration 因子贡献
# F1-F5: 机制参数敏感性全扫描
# L1-L6: 各宏观环境下最优档案识别
```

### 内部一致性规则

1. **信号权重 = 1.0**：所有覆盖 `signals.weights.*` 的集严格验证
2. **VIX 三层逻辑**：`vix_high_threshold` < VIX 渐进第二档 < `emergency_derisk_vix`
3. **信号窗口与 zscore_window 匹配**：`zscore_window` ≈ 2.5–4× `lookback_months`
4. **vol_target 与 beta_max 协调**：低 beta 配低 vol 目标；高 beta 配宽松 vol
5. **调仓频率与信号速度匹配**：biweekly 调仓配短期（9月）信号
6. **cov.lookback_days 与信号周期一致**：慢速信号（15月）配 504 天协方差估计

---

## Cron 定时任务

```cron
# 每日信号：周一至周五 21:15 UTC（约纽约收盘后 17:15 ET，冬令时）
15 21 * * 1-5   cd /Users/xuling/code/someopark-test && \
                set -a && source .env && set +a && \
                bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily \
                >> qlib-main/sector_rotation/logs/cron_daily.log 2>&1

# 每周 EPS 维护：周日 06:00 UTC（01:00 ET）
0 6 * * 0       cd /Users/xuling/code/someopark-test && \
                set -a && source .env && set +a && \
                bash qlib-main/sector_rotation/sector_rotation_pipeline.sh weekly \
                >> qlib-main/sector_rotation/logs/cron_weekly.log 2>&1

# 月度再平衡：每月 1 日 21:30 UTC
30 21 1 * *     cd /Users/xuling/code/someopark-test && \
                set -a && source .env && set +a && \
                bash qlib-main/sector_rotation/sector_rotation_pipeline.sh monthly \
                >> qlib-main/sector_rotation/logs/cron_monthly.log 2>&1
```

---

## 数据来源

| 数据 | 来源 | 频率 | 脚本 |
|---|---|---|---|
| ETF 日线价格 | Yahoo Finance（yfinance） | 每日 | `data/loader.py` |
| 成分股季度 EPS | Polygon `/vX/reference/financials` | 每周增量 | `update_eps_history.py` |
| VIX / SPY 价格 | Yahoo Finance | 每日 | `data/loader.py` |
| FRED 宏观指标 | FRED API（fredapi） | 每日 | `data/loader.py` |
| 宏观 parquets | someopark 主 pipeline（**只读**） | 每日 | — |

---

## 预期表现（文献基准）

| 指标 | 乐观 | 基准 | 保守 |
|---|---|---|---|
| vs SPY 超额回报 | +4% | +2% | +0.5% |
| Sharpe（扣费后） | 0.6 | 0.45 | 0.3 |
| 最大回撤 | -15% | -20% | -30% |
| 年换手率 | 300% | 400% | 600% |
| 年交易成本 | 30 bps | 50 bps | 80 bps |

*OOS Sharpe 通常比 IS 低 40%（Cederburg et al. 2023）。*

---

## GICS 结构性断点警告

**回测必须从 2018-07-01 或之后开始。**

XLC（通信服务 ETF）于 2018-06-18 创立，源于 GICS 重组：
- Meta（FB）、Alphabet（GOOGL）从 XLK → XLC
- Disney（DIS）、Comcast（CMCSA）从 XLY → XLC

2018-07-01 之前的回测使用的是成分不一致的行业划分，横截面动量比较失去意义。

---

## 学术基础

| 缩写 | 完整引用 | 应用 |
|------|----------|------|
| JT1993 | Jegadeesh-Titman (1993) — *Returns to Buying Winners and Selling Losers* | 12-1月窗口最优动量参数 |
| JT2001 | Jegadeesh-Titman (2001) — *Profitability of Momentum Strategies* | 高分散环境下短窗口；skip=0 理论依据 |
| MOP2012 | Moskowitz-Ooi-Pedersen (2012) — *Time Series Momentum*, JFE | TS 动量对 CS 具有独立解释力 |
| BSC2015 | Barroso-Santa-Clara (2015) — *Momentum Has Its Moments* | vol-scaling 优于二元 crash filter |
| DM2016 | Daniel-Moskowitz (2016) — *Momentum Crashes* | 动量崩溃机制；两层防御设计 |
| MM2017 | Moreira-Muir (2017) — *Volatility-Managed Portfolios* | 最优 vol 目标约 7-8%；短窗口估计更有效 |
| AMP2013 | Asness-Moskowitz-Pedersen (2013) — *Value and Momentum Everywhere*, JF | 动量扩张期 IR 最高；恐慌期最脆弱 |
| FF1992 | Fama-French (1992) — *The Cross-Section of Expected Stock Returns* | HML 价值溢价跨周期持续 |
| AB2007 | Ang-Bekaert (2007) — *Stock Return Predictability* | Regime 识别精度对条件信号有效性至关重要 |
| MRT2010 | Maillard-Roncalli-Teïletche (2010) — *Equal Risk Contribution* | ERC 比 inv_vol 更稳健处理板块相关性 |
| CD2006 | Clarke-DeMiguel (2006) — *Minimum-Variance Portfolios* | GMV 在低 beta 约束下系统性降低波动率 |
| DGU2009 | DeMiguel-Garlappi-Uppal (2009) — *Optimal vs Naive Diversification* | 1/N 难以被样本外优化系统性超越 |
| LW2004 | Ledoit-Wolf (2004) — *Analytical Nonlinear Shrinkage* | N 小/T 中时解析收缩估计量优于样本协方差 |
| FP2014 | Frazzini-Pedersen (2014) — *Betting Against Beta* | 低 beta 资产相对 CAPM 预测具有正 alpha |
| GZ1993 | Grossman-Zhou (1993) — *Optimal Investment Strategies* | 最优 floor 约束取决于投资期限与风险厌恶 |
| GP2013 | Garleanu-Pedersen (2013) — *Dynamic Trading with Predictable Returns* | 最优交易速度 = 信号半衰期 vs 成本权衡 |
| GK2000 | Grinold-Kahn (2000) — *Active Portfolio Management* | 基本定律 IR = IC × √BR；集中度与广度权衡 |
| W2009 | Whaley (2009) — *Understanding the VIX* | VIX>35 极端恐慌；VIX 均值回归特性 |
| Chen2010 | Chen-Wiesel-Eldar-Goldsmith (2010) — *Shrinkage Algorithms for MMSE Covariance* | OAS 在小样本时均方误差低于 LW |
| Grinblatt-Han2005 | Grinblatt-Han (2005) — *Prospect Theory, Mental Accounting, and Momentum* | 加速度因子的行为金融学依据 |

*OOS Sharpe 通常比 IS 低 40%（Cederburg et al. 2023, *Beyond the Status Quo*）。*

---

## 与 someopark 主程序的关系

本策略与 someopark 配对交易系统**完全隔离**：

| | someopark 主程序 | sector_rotation |
|---|---|---|
| conda 环境 | `someopark_run` | `qlib_run` |
| 框架 | 自研 | qlib |
| 标的 | 个股配对 | 行业 ETF |
| 调仓频率 | 每日 | 每月 |
| 方向 | 市场中性 | 纯多头 |
| 目标 Sharpe | 2–3（Alpha） | 0.4–0.6（Beta 择时） |
| MacroStateStore | 写入 / 维护 | 只读 |

**已规划的集成路径（未来）：**
1. 信号共享：板块轮动 Regime → someopark 资金权重调整
2. 双策略组合级风险预算

---

*详细运维操作参考 [RUNBOOK.md](RUNBOOK.md)。所有代码使用 `qlib_run` conda 环境——绝不使用 `someopark_run`。*
