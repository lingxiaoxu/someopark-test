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

- **动量**：Moskowitz, Ooi, Pedersen (2012), *Time Series Momentum*, JFE
- **行业动量**：Gupta, Kelly (2019, AQR), *Factor Momentum Everywhere*
- **Regime**：Guidolin, Timmermann (2007), *Asset Allocation under Multivariate Regime Switching*
- **估值 × 动量**：Asness, Moskowitz, Pedersen (2013), *Value and Momentum Everywhere*, JF
- **OOS 衰减**：Cederburg et al. (2023), *Beyond the Status Quo*

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
