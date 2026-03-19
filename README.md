<p align="center">
  <img src="public/SOMEO PARK矢量源文件 Big Square.svg" alt="Someopark" width="160"/>
</p>

<h1 align="center">someopark</h1>
<p align="center"><b>双策略配对交易回测框架</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/conda-someopark__run-green?logo=anaconda&logoColor=white"/>
  <img src="https://img.shields.io/badge/strategies-MRPT%20%7C%20MTFS-orange"/>
  <img src="https://img.shields.io/badge/param__sets-32%20%2B%2031-purple"/>
  <img src="https://img.shields.io/badge/walk--forward-6%20windows-teal"/>
  <img src="https://img.shields.io/badge/data-Polygon%20%7C%20Yahoo-lightgrey"/>
</p>

---

本项目包含两个并列运行的配对交易策略：

| 策略 | 全称 | 核心逻辑 |
|------|------|----------|
| **MRPT** | Mean Reversion Pair Trading | Kalman Filter 对冲比率 + 动态 z-score 均值回归 |
| **MTFS** | Momentum Trend Following Strategy | VAMS 多窗口动量评分 + SMA 趋势确认 + 波动率调仓 |

两个策略共用同一套基础设施（`PortfolioClasses.py`、`PriceDataStore.py`、`run_configs/`），各自拥有独立的运行入口、参数集和 walk-forward 优化流程。

---

## 环境配置

### 1. 创建 Python 环境

依赖 `someopark_run` conda 环境（含 `pandas_market_calendars` 等包）。

### 2. 配置 API Key

复制模板文件，填入真实 key：

```bash
cp .env.example .env
```

编辑 `.env`：

```
POLYGON_API_KEY=your_polygon_api_key_here
FRED_API_KEY=your_fred_api_key_here
MONGO_URI=your_mongo_uri_here
MONGO_VEC_URI=your_mongo_vec_uri_here
```

> `.env` 已加入 `.gitignore`，不会提交到版本库。所有 API key 和数据库连接字符串仅存放于此文件，代码中不含任何硬编码凭证。

### 3. 运行所有脚本的正确方式

**必须同时使用 conda 环境 + 加载 `.env`，否则会报 `ModuleNotFoundError` 或 `KeyError: POLYGON_API_KEY`：**

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python <script.py> [args]
```

示例：

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step1_grid32.json
```

> 直接 `python` 或 `conda activate` 后再 `python` 均不可靠——`conda run -n someopark_run --no-capture-output` 是唯一确保环境正确的方式。
>
> 详细运行命令参考 [RUNBOOK.md](RUNBOOK.md)。

---

## 核心文件

### 共用基础设施

| 文件 | 说明 |
|------|------|
| `PortfolioClasses.py` | Portfolio、Order、StopLoss 等基础类（两个策略共用） |
| `PriceDataStore.py` | 价格数据读取与缓存（Polygon / Yahoo），Parquet 格式 |
| `AuditPairs.py` | 验证 Excel 输出文件规则合规性（MRPT / MTFS 通用，`--strategy mrpt\|mtfs`） |
| `DailySignal.py` | 每日信号生成器（`--strategy mrpt\|mtfs\|both`），含 Regime 检测、Position Monitor、完整报告输出 |
| `SelectPairs.py` | 从 someopark 数据库筛选最优 MRPT / MTFS 配对，决定 s1/s2 方向，可直接写入 pair_universe_*.json |
| `UpdateStep1Configs.py` | 读取 pair_universe_*.json，更新 Step1 grid search config 的 pairs 字段（换配对后必须运行） |
| `CompactPriceData.py` | 合并同一周内的多个 Parquet 文件为单文件，重算 SHA256 hash，更新 index.json |
| `AnalyzeEarningsFilter.py` | 分析财报过滤策略的历史效果，评估屏蔽日对回测的影响 |
| `MomentumPairSelector.py` | 从 S&P500 全市场扫描动量配对候选，下载价格数据并输出候选列表 |

### MRPT 策略

| 文件 | 说明 |
|------|------|
| `PortfolioMRPTRun.py` | MRPT 策略主逻辑（均值回归 + 财报黑名单） |
| `PortfolioMRPTStrategyRuns.py` | JSON 驱动的批量回测入口，含全部 32 个 param_set 定义 |
| `MRPTUpdateConfigs.py` | 读取 Step 1 结果，DSR 过滤后生成 Step 2 / Step 3 config |
| `MRPTWalkForward.py` | Walk-forward 优化：6 窗口 × 27 OOS 交易日，DSR 选参 |
| `MRPTWalkForwardReport.py` | 读取最近一次 walk-forward 结果，生成完整 OOS 报告 |
| `MRPTGenerateReport.py` | 生成回测 vs 验证期对比报告 |
| `MRPTFetchEarnings.py` | 从 Polygon 拉取并缓存财报日期 |

### MTFS 策略

| 文件 | 说明 |
|------|------|
| `PortfolioMTFSRun.py` | MTFS 策略主逻辑（动量评分 + SMA 趋势 + 动量止损） |
| `PortfolioMTFSStrategyRuns.py` | JSON 驱动的批量回测入口，含全部 31 个 param_set 定义 |
| `MTFSUpdateConfigs.py` | 读取 Step 1 结果，DSR 过滤后生成 Step 2 / Step 3 config |
| `MTFSWalkForward.py` | Walk-forward 优化：6 窗口 × 27 OOS 交易日，DSR 选参 |
| `MTFSWalkForwardReport.py` | 读取最近一次 walk-forward 结果，生成完整 OOS 报告（含止损类型分解） |
| `MTFSGenerateReport.py` | 生成回测 vs 验证期对比报告 |

---

## 更换配对的完整流程

当需要重新筛选交易配对时（例如：定期更新、回测表现下滑），按以下顺序操作：

### 1. 筛选配对

```bash
# 分析最近 30 天数据，预览结果（不写入）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python SelectPairs.py

# 分析最近 60 天数据
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python SelectPairs.py --days 60

# 确认结果后写入 pair_universe_mrpt.json / pair_universe_mtfs.json（会自动备份旧文件）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python SelectPairs.py --save
```

**SelectPairs.py 逻辑：**

| 策略 | 筛选逻辑 | s1/s2 方向 |
|------|----------|-----------|
| MRPT | `coint_rate×1.0 + pca_rate×0.5 + similar_bonus×0.3`，优先选跨天稳定出现的协整配对 | 字母序（均值回归不依赖方向） |
| MTFS | `pca_rate²×(1-coint_rate) + similar_rate×0.5`，优先选有因子关联但协整性弱的配对 | **s1 = 近 30 天涨幅更高的 ticker**，s2 = 涨幅低的 ticker |

数据来源：someopark 数据库 `pairs_day_select` 集合（需 `MONGO_URI` 环境变量）。

### 2. 更新 Step1 config

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python UpdateStep1Configs.py
```

更新（in-place）：
- `run_configs/runs_20260304_step1_grid32.json` 中的 `pairs` 字段
- `run_configs/mtfs_runs_step1_grid30.json` 中的 `pairs` 字段

### 3. 重新运行 Step1 → Step3 或 Walk-Forward

参见下方各策略完整流程。

---

## MRPT 完整运行流程（三步法）

### 准备：更新财报日期缓存

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTFetchEarnings.py
```

输出：`price_data/earnings_cache.json`

---

### Step 1 — 参数网格搜索

**目标**：用 32 个参数集分别运行全部 15 个配对，找出每个配对的最优参数集。

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step1_grid32.json
```

- 共 32 个 run，每个 run 包含全部 15 个配对
- 每个 run 使用一个 param_set（如 `default`、`fast_signal`、`patient_hold` 等）
- 输出：32 个 Excel 文件 + `historical_runs/strategy_summary_<timestamp>.csv`

**运行后验证输出质量（可选但推荐）：**

```bash
conda run -n someopark_run python AuditPairs.py --strategy mrpt
# 输出保存至 historical_runs/audit/audit_mrpt_<timestamp>.txt
```

---

### Step 1 → Step 2/3：自动选对并生成 config

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTUpdateConfigs.py historical_runs/strategy_summary_<timestamp>.csv
```

**选对标准：**

| 条件 | 默认值 |
|------|--------|
| 配对 PnL | > 0 |
| 开仓次数 | ≥ 3 |
| Deflated Sharpe Ratio | > 0.5（32 次试验修正） |

每个配对选出**配对级 Sharpe 最高**的 param_set。DSR < 0.5 的结果视为噪声排除。

**输出（自动覆盖写入）：**
- `run_configs/runs_20260304_step2_best_backtest.json`
- `run_configs/runs_20260304_step3_forward.json`

---

### Step 2 — 组合回测

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step2_best_backtest.json
```

所有选中配对在同一个组合 portfolio 中运行，每个配对使用各自最优 param_set。

---

### Step 3 — 向前验证

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step3_forward.json
```

`trade_start_date = auto_minus_70d`（由 `MRPTUpdateConfigs.py` 自动填入），仅最近约 70 天实际交易。

---

### Step 4 — 生成对比报告

```bash
conda run -n someopark_run python MRPTGenerateReport.py <step2_excel> <step3_excel>
```

输出：`historical_runs/report_bt_vs_fwd_<timestamp>.xlsx`

---

## MTFS 完整运行流程（三步法）

### Step 1 — 参数网格搜索

**目标**：用 30 个参数集分别运行全部 15 个配对，找出每个配对的最优动量参数组合。

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_step1_grid30.json
```

- 共 30 个 run，每个 run 包含全部 15 个配对
- 输出：30 个 Excel 文件 + `historical_runs/mtfs_strategy_summary_<timestamp>.csv`

**运行后验证输出质量（可选但推荐）：**

```bash
conda run -n someopark_run python AuditPairs.py --strategy mtfs
# 输出保存至 historical_runs/audit/audit_mtfs_<timestamp>.txt
```

---

### Step 1 → Step 2/3：自动选对并生成 config

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSUpdateConfigs.py historical_runs/mtfs_strategy_summary_<timestamp>.csv
```

**选对标准：**

| 条件 | 默认值 |
|------|--------|
| 配对 PnL | > 0 |
| 开仓次数 | ≥ 3 |
| Deflated Sharpe Ratio | > 0.5（31 次试验修正） |

**输出（自动覆盖写入）：**
- `run_configs/mtfs_runs_step2_best_backtest.json`
- `run_configs/mtfs_runs_step3_forward.json`

---

### Step 2 — 组合回测

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_step2_best_backtest.json
```

---

### Step 3 — 向前验证

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_step3_forward.json
```

`trade_start_date = auto_minus_70d`，仅最近约 70 天实际交易。

---

### Step 4 — 生成对比报告

```bash
conda run -n someopark_run python MTFSGenerateReport.py <step2_excel> <step3_excel>
```

输出：`historical_runs/report_mtfs_bt_vs_fwd_<timestamp>.xlsx`

---

## 每日信号生成（DailySignal.py）

每个交易日收盘后运行，根据最新价格输出操作指令（OPEN/CLOSE/HOLD/FLAT），生成完整报告，并更新本地持仓记录。

```bash
# 双策略合并运行（推荐）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both

# dry-run（只看信号，不写 inventory）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both --dry-run

# 单策略 / 跳过 Regime 检测（离线时）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy mrpt
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both --skip-regime
```

**主要功能：**

- **Regime 检测**（`RegimeDetector.py`）：综合波动率、信用利差、利率、AI 动量等 7 类市场指标，输出 0–100 分，自动决定 MRPT / MTFS 资金权重。分数低（< 40）偏均值回归，分数高（> 60）偏趋势跟随，中间为中性
- **Position Monitor**：对所有已开仓配对，以开仓时注入的 param_set 从 `open_date` 跑模拟至今，每日检测止损条件（MRPT：波动率止损 / 价格止损 / z-score 自然回归 / 时间止损；MTFS：动量衰减 / 配对 PnL 止损 / 波动率止损 / 时间止损），输出 HOLD / CLOSE（自然平仓）/ CLOSE_STOP（止损触发）。仓位在 `open_date` 的 `my_handle_data` 之后注入，止损从次日起检测，与实盘逻辑一致。每对监测结果输出 Excel 至 `trading_signals/monitor_history/`
- **完整报告**：所有输出统一写入 `trading_signals/`，包含信号 JSON 和人类可读中文 TXT 报告（含 Regime 分析、持仓监测、OOS 历史参考）

持仓快照保存在 `inventory_mrpt.json` 和 `inventory_mtfs.json`，每条记录含开仓日期、价格、param_set、开仓对冲比率、开仓信号、Walk-Forward 来源等信息，供 Position Monitor 精确复现开仓状态。每次运行前自动将快照备份到 `inventory_history/`。

---

## Walk-Forward 优化（两个策略均支持）

标准三步法中，Step 2 的回测数据与 Step 1 的选参数据重叠，存在**样本内过拟合风险**。Walk-forward 实现严格的时间分离。

### 原理

支持两种窗口模式：

| 模式 | 说明 |
|------|------|
| `expanding`（默认） | train_start 固定锚定，训练集随窗口递增 |
| `rolling` | 训练集长度固定（始终 18 个月），train_start 随窗口右移 |

默认配置：**6 窗口 × 27 NYSE 交易日 OOS**（共 162 个 OOS 交易日），训练期 18 个月。

#### MRPT Walk-Forward 窗口（6×27，expanding，基于 32 个 param_set）

| 窗口 | 训练期 | 测试期（样本外） |
|------|--------|----------------|
| Window 1 | 2024-01-30 → 2025-08-26 | 2025-08-27 → 2025-10-02 |
| Window 2 | 2024-01-30 → 2025-10-02 | 2025-10-03 → 2025-11-07 |
| Window 3 | 2024-01-30 → 2025-11-07 | 2025-11-10 → 2025-12-16 |
| Window 4 | 2024-01-30 → 2025-12-16 | 2025-12-17 → 2026-01-23 |
| Window 5 | 2024-01-30 → 2026-01-23 | 2026-01-26 → 2026-03-03 |
| Window 6 | 2024-01-30 → 2026-03-03 | 2026-03-04 → 2026-04-09 |

#### MTFS Walk-Forward 窗口（6×27，expanding，基于 31 个 param_set）

| 窗口 | 训练期 | 测试期（样本外） |
|------|--------|----------------|
| Window 1 | 2024-03-15 → 2025-10-03 | 2025-10-06 → 2025-11-11 |
| Window 2 | 2024-03-15 → 2025-11-11 | 2025-11-12 → 2025-12-18 |
| Window 3 | 2024-03-15 → 2025-12-18 | 2025-12-19 → 2026-01-27 |
| Window 4 | 2024-03-15 → 2026-01-27 | 2026-01-28 → 2026-03-05 |
| Window 5 | 2024-03-15 → 2026-03-05 | 2026-03-06 → 2026-04-13 |
| Window 6 | 2024-03-15 → 2026-04-13 | 2026-04-14 → 2026-05-21 |

### 运行 MRPT Walk-Forward

```bash
# expanding 模式（默认）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTWalkForward.py

# rolling 模式
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTWalkForward.py --mode rolling

# 可选参数
python MRPTWalkForward.py --oos-windows 6 --oos-days 162 --train-months 18

# 续跑（跳过已完成的网格搜索）
python MRPTWalkForward.py --skip-grid
```

### 运行 MTFS Walk-Forward

```bash
# expanding 模式（默认）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSWalkForward.py

# rolling 模式
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSWalkForward.py --mode rolling

# 可选参数
python MTFSWalkForward.py --oos-windows 6 --oos-days 162 --train-months 18
```

### Walk-Forward 输出结构

```
historical_runs/walk_forward/           ← MRPT
  window01_<train_start>_<train_end>/
  window02_.../
  ...
  dsr_selection_log_<ts>.csv
  walk_forward_summary_<ts>.json
  oos_report_<ts>.txt
  oos_equity_curve_<ts>.csv
  oos_pair_summary_<ts>.csv

historical_runs/walk_forward_mtfs/      ← MTFS
  window01_<train_start>_<train_end>/
  ...（结构相同）
```

### 生成详细 OOS 报告

```bash
# MRPT
conda run -n someopark_run python MRPTWalkForwardReport.py

# MTFS（含止损类型分解：Momentum Decay / Pair P&L Stop / Volatility Stop / Time-based）
conda run -n someopark_run python MTFSWalkForwardReport.py
```

报告包含：
- **窗口级汇总**：每个 OOS 窗口的 PnL / Sharpe / MaxDD / 天数
- **拼接 OOS 总览**：跨 6 窗口的 GROSS TOTAL（含 first-day interest 修正）→ Interest → NET TOTAL；利息拆分
- **配对级明细**：各配对在全部窗口的总 PnL / Sharpe / 胜率 / 交易次数
- **各窗口入选配对**：DSR 过滤后实际参与 OOS 的配对列表
- **止损分解（MTFS 专属）**：各类止损触发次数及占比

### 解读结果

- **OOS Sharpe > 0** 且**各窗口一致正向**：策略真实可用
- **OOS Sharpe 远低于 IS Sharpe**：存在过拟合，需审查参数复杂度
- **DSR 选中率低**：信号质量弱，需重新设计因子

---

### WalkForwardDiagnostic — 深度诊断报告

`WalkForwardDiagnostic.py` 自动读取最新 walk-forward 结果，输出多维 Excel 诊断报告。

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python WalkForwardDiagnostic.py
```

输出：`historical_runs/wf_diagnostic_<timestamp>.xlsx`

**文件查找逻辑（全自动，无硬编码日期）：**

- `walk_forward_summary_*.json`：按 mtime 取最新，提取 `windows[0].train_start` 作为锚点
- 窗口目录：匹配 `window{NN}_{anchor}_*`，通过锚点避免选错旧 run
- OOS test xlsx：在各窗口目录下递归 glob，按 mtime 取最新（同一参数可能因重跑有多个文件）
- DSR log / OOS pair summary / equity curve：在 wf_dir 根目录 glob，按 mtime 取最新

**Excel 工作表说明：**

| Sheet | 内容 |
|-------|------|
| `Executive_Summary` | 宏观环境 IS→OOS 变化、各窗口 PnL/Sharpe/VIX/SPY、协整检验、Ticker 集中风险、问题配对综合结论 |
| `MRPT_Pairs` / `MTFS_Pairs` | 每个配对 × 7 窗口（IS + 6 OOS）的 Sharpe / MaxDD / 协整 p 值 / 相关系数 |
| `Regime_Comparison` | 每个 OOS 窗口的 VIX、SPY 回报、HY 利差、利率、失业率等宏观指标快照 |
| `Cross_Correlations` | IS vs OOS 跨品种相关矩阵对比，标注变化最大的 ticker 对 |
| `Cointegration` | 每个配对每窗口的协整 p 值，标注 IS 强但 OOS 丧失协整的风险配对 |
| `IS_OOS_Decay` | IS 最优 Sharpe → OOS 实际 Sharpe 的衰减比率；DSR 鲁棒性标签（Fragile / Moderate / Robust） |
| `DSR_Robustness` | 每个配对 × 窗口：31/32 个参数集中通过 DSR 的数量、Pass Rate、Selected 参数的 Sharpe/DSR |
| `OOS_PnL_Heatmap` | 配对 × 窗口 PnL 热图（宽表，直接从 portfolio xlsx 读取 dod_pair_trade_pnl_history） |
| `OOS_PnL_Detail` | 每个配对每窗口的 WinRate、N_Days_Active、N_Stops 明细 |
| `OOS_Curve_Comparison` | MRPT vs MTFS 每日 PnL 相关系数，评估双策略分散化效果 |
| `MRPT_Equity_Curve` / `MTFS_Equity_Curve` | 拼接 6 窗口的逐日权益曲线 |

---

## Run Config 格式

`run_configs/` 目录下的 JSON 文件控制每次回测：

```json
{
  "start_date": "2024-01-30",
  "end_date": "auto_minus_70d",
  "runs": [
    {
      "label": "my_run",
      "param_set": "default",
      "pairs": [
        ["MSCI", "LII"],
        ["LYFT", "UBER", "conservative_no_leverage"],
        ["CART", "DASH", "vol_adaptive"]
      ]
    }
  ]
}
```

**`end_date` 特殊值：**

| 值 | 说明 |
|----|------|
| `"auto"` | 今天（用于向前验证） |
| `"auto_minus_30d"` | 今天减 30 交易日 |
| `"auto_minus_70d"` | 今天减 70 交易日（Step 2 回测截止） |

**`trade_start_date`**（可选）：设置后策略在此日期前只做 warmup，不实际开仓。

**配对格式：**
- `[s1, s2]` — 使用 run 级别的 `param_set`
- `[s1, s2, "param_set_name"]` — 为该配对单独指定参数集（Step 2/3 用此格式）

### Config 文件一览

**MRPT（`runs_20260304_*`）：**

| 文件 | 用途 |
|------|------|
| `runs_20260304_step1_grid32.json` | Step 1：32 param_set 网格搜索 |
| `runs_20260304_step2_best_backtest.json` | Step 2：14 对 × 各自最优 param_set 组合回测 |
| `runs_20260304_step3_forward.json` | Step 3：同 Step 2 配置，向前验证 |
| `runs_20260304_forward30d.json` | 独立 30 日向前验证窗口 |

**MTFS（`mtfs_runs_*`）：**

| 文件 | 用途 |
|------|------|
| `mtfs_runs_step1_grid30.json` | Step 1：31 param_set 网格搜索（文件名历史遗留，实际含 31 个） |
| `mtfs_runs_all_params.json` | 同 step1，MTFSWalkForward.py 的内部 GRID_CONFIG 引用此文件 |
| `mtfs_runs_step2_best_backtest.json` | Step 2：13 对 × 各自最优 param_set 组合回测 |
| `mtfs_runs_step3_forward.json` | Step 3：同 Step 2 配置，向前验证 |
| `mtfs_runs_forward30d.json` | 独立 30 日向前验证窗口 |

---

## 可用参数集（param_set）

### MRPT — 32 个（`PortfolioMRPTStrategyRuns.py`）

| 组 | 参数集 | 风格 |
|----|--------|------|
| A — 杠杆基准 | `default`, `no_leverage`, `high_leverage`, `tight_stop` | 杠杆与止损变体 |
| B — 信号速度 | `fast_signal`, `slow_signal`, `long_z_short_v`, `short_z_long_v` | z-score / 波动率窗口长短 |
| C — 入场阈值 | `low_entry`, `high_entry`, `static_threshold`, `vol_gated` | 入场频率控制 |
| D — 出场策略 | `quick_exit`, `patient_hold`, `flash_hold`, `symmetric_exit` | 持仓时长偏好 |
| E — 风险档位 | `aggressive`, `conservative`, `deep_dislocation`, `high_turnover` | 综合风险偏好 |
| F — 波动率专项 | `low_vol_specialist`, `high_vol_specialist`, `vol_adaptive`, `vol_agnostic` | 波动率适应性 |
| G — 冷静期 | `fast_reentry`, `slow_reentry` | 止损后再入场间隔 |
| H — 组合优化 | `stable_signal_quick_exit`, `fast_signal_tight_stop`, `medium_signal_high_leverage`, `deep_entry_quick_exit`, `conservative_no_leverage`, `balanced_plus` | 多维度组合 |

### MTFS — 31 个（`PortfolioMTFSStrategyRuns.py`）

| 组 | 参数集 | 风格 |
|----|--------|------|
| A — 基准与评分 | `default`, `raw_momentum`, `short_term_tilt`, `long_term_tilt` | VAMS vs 原始动量；短/长期权重 |
| B — 趋势过滤 | `no_trend_filter`, `fast_trend_filter` | SMA 趋势确认开关与速度 |
| C — 风险档位 | `aggressive`, `conservative`, `no_leverage` | 仓位规模与止损力度 |
| D — 换仓频率 | `fast_rebalance`, `slow_rebalance` | 再平衡周期 |
| E — 对冲方式 | `beta_neutral`, `kalman_hedge` | Dollar neutral vs beta/Kalman 对冲 |
| F — 反转保护 | `no_reversal_protection`, `sensitive_reversal` | 动量衰减与反转退出灵敏度 |
| G — 综合均衡 | `balanced`, `fast_strict`, `trend_leverage` | 多维度组合参数 |
| H — 跳过月份 | `no_skip_month` | 移除动量计算的跳过期 |
| I — 入场阈值 | `entry_threshold_weak`, `entry_threshold_strong` | 动量入场门槛高低 |
| J — 波动率仓位 | `vol_weighted_sizing`, `vol_weighted_aggressive` | 波动率加权仓位 |
| K — 窗口对齐 | `monthly_aligned_windows`, `weekly_aligned_windows` | 动量窗口月/周对齐 |
| L — 崩盘防护 | `crash_defensive`, `crash_tolerant` | 极端波动时的仓位缩减 |
| M — 长期 Kalman | `long_term_kalman`, `short_term_beta_neutral` | 长周期 Kalman 对冲 |
| N — 持仓延长 | `let_profits_run` | 宽松止损延长盈利持仓 |

---

## MTFS 止损机制（MRPT 无此设计）

MTFS 策略包含四类止损，在 `MTFSWalkForwardReport.py` 中会按类型分解统计：

| 类型 | 触发条件 |
|------|----------|
| **Momentum Decay** | 动量评分快速衰减（短期均线穿越长期） |
| **Pair P&L Stop** | 单配对亏损超过 `pair_stop_loss_pct` |
| **Volatility Stop** | 价格波动超过 `volatility_stop_loss_multiplier × ATR` |
| **Time-based** | 持仓超过 `max_holding_period` 天强制平仓 |

---

## 财报黑名单过滤（MRPT 专属）

MRPT 策略在以下情况下**不开新仓**（已有仓位的平仓和止损不受影响）：

| 财报时间 | 屏蔽日 |
|---------|--------|
| AMC（盘后） | 财报日前一天 + 财报日当天 |
| BMO（盘前） | 财报日当天 |
| INTRADAY | 财报日当天 |
| UNKNOWN（年报等） | 同 AMC |

财报数据来源：`price_data/earnings_cache.json`（由 `MRPTFetchEarnings.py` 维护）。

---

## 输出目录

| 目录 | 内容 |
|------|------|
| `price_data/` | 日线 OHLCV Parquet 缓存、财报缓存 |
| `historical_runs/` | MRPT 回测 Excel 结果、strategy summary CSV |
| `historical_runs/audit/` | audit 报告（`AuditPairs.py` 输出，含 mrpt / mtfs 子文件） |
| `historical_runs/walk_forward/` | MRPT walk-forward 优化结果 |
| `historical_runs/walk_forward_mtfs/` | MTFS walk-forward 优化结果 |
| `run_configs/` | MRPT / MTFS 回测配置 JSON 文件 |
| `trading_signals/` | 每日信号 JSON、TXT 报告（`DailySignal.py` 输出，不提交到 git） |
| `trading_signals/monitor_history/` | Position Monitor 每对逐日模拟 Excel（`DailySignal.py` 输出） |
| `inventory_mrpt.json` | MRPT 当前持仓快照（开仓日期、价格、param_set、对冲比率、信号、WF 来源等，`DailySignal.py` 维护） |
| `inventory_mtfs.json` | MTFS 当前持仓快照（同上） |
| `inventory_history/` | 每次 DailySignal 运行前自动备份的持仓快照（按 as_of 日期保留唯一快照） |
| `charts/` | 策略权益曲线图表 |
| `logs/` | 运行日志 |
| `archive/` | 早期运行的历史归档（不提交到 git） |

---

## MRPT 32 个参数集设计记录

| 参数集 | 金融内涵 | 参数设计逻辑 | 关键参数 |
|--------|----------|-------------|---------|
| `default` | A1：系统默认基准，均衡回望/入场/杠杆/止损，所有参数集的参照点 | 中等窗口(z=36,v=32)，适中入场门槛(0.75)，2倍杠杆，止损×2，12天持仓。所有维度取中值，是唯一的纯基准 | z_back=36, v_back=32, entry_z=0.75, amplifier=2, stop×2, hold=12 |
| `no_leverage` | A2：无杠杆基准，amplifier=1，剥离杠杆贡献测试纯信号质量 | 保持与default完全相同的其他参数，只将amplifier降至1。目的是单轴对照：杠杆对Sharpe是正贡献还是负贡献 | z_back=36, v_back=32, entry_z=0.75, amplifier=1, stop×2, hold=12 |
| `high_leverage` | A3：高杠杆3倍，配合宽止损(×2.5)防震荡出局 | 高杠杆必须配宽止损，否则频繁止损侵蚀收益。持仓延长至15天给大仓位更多回归时间 | z_back=36, v_back=32, entry_z=0.75, amplifier=3, stop×2.5, hold=15 |
| `tight_stop` | A4：紧止损基准，stop_mult=1.5，快速切损测试严格风控代价 | 只变动止损宽度，其余与default一致。验证紧止损对max drawdown的改善是否值得牺牲部分PnL | z_back=36, v_back=32, entry_z=0.75, amplifier=2, stop×1.5, hold=12 |
| `fast_signal` | B1：快速信号，短窗口(z=20,v=20)，迅速响应价差变化 | 短窗口信号腐烂快，配合短持仓5天，避免信号过期仍持仓。冷静期缩至1天，信号频繁时快速重入 | z_back=20, v_back=20, entry_z=0.75, amplifier=2, stop×2, hold=5 |
| `slow_signal` | B2：慢速信号，长窗口(z=50,v=50)，只对充分确立的偏离入场 | 长窗口过滤噪声，出场门槛提高(0.25)等待完全回归，持仓20天匹配慢信号周期 | z_back=50, v_back=50, entry_z=1.0, amplifier=2, stop×2, hold=20 |
| `long_z_short_v` | B3：错配窗口A — 长z/短v，稳定信号 + 快速波动率响应 | z_back=50稳定z-score信号减少假信号；v_back=20快速追踪波动率变化，动态阈值反应灵敏 | z_back=50, v_back=20, entry_z=0.75, amplifier=2, stop×2, hold=15 |
| `short_z_long_v` | B4：错配窗口B — 短z/长v，敏感信号被稳定波动率基准过滤 | z_back=20快速捕捉偏离；v_back=50建立稳定波动率基准，用稳定门槛过滤短期噪声信号 | z_back=20, v_back=50, entry_z=0.75, amplifier=2, stop×2, hold=15 |
| `low_entry` | C1：低门槛高频入场，entry_z=0.5，entry_factor=1.5 | 降低静态门槛和波动率因子，大幅提高开仓频次。测试高频次低单笔收益是否总体优于低频次高单笔 | z_back=36, v_back=32, entry_z=0.5, factor=1.5, amplifier=2, hold=10 |
| `high_entry` | C2：高门槛低频入场，entry_z=1.25，factor=3.0，极端偏离才入场 | 只在价差超过约2.5σ时入场，每年交易次数极少，每笔利润大。测试择时精度对组合收益的价值 | z_back=36, v_back=32, entry_z=1.25, factor=3.0, amplifier=2, hold=15 |
| `static_threshold` | C3：静态入场阈值，entry/exit_factor=0，阈值不随波动率变化 | 剥离动态阈值的贡献：entry_z固定为1.0，无论波动率高低入场门槛不变。对照动态门槛的净价值 | z_back=36, v_back=32, entry_z=1.0, factor=0(静态), amplifier=2, hold=12 |
| `vol_gated` | C4：极度动态阈值，entry_factor=3.5，低波动时疯狂入场高波动时不入 | 最大化波动率敏感性：normalized_vol≈0时entry_z≈0.5，normalized_vol→1时entry_z→2.25。自动隐身于高波动市场 | z_back=36, v_back=28, entry_z=0.5, factor=3.5, amplifier=2, hold=15 |
| `quick_exit` | D1：即时离场，exit_factor=0.25，一旦价差开始回归就离场 | 一旦价差回归均值方向就立刻平仓，锁定利润避免回吐。短持仓5天配合，测试快速锁利vs等待完全回归 | z_back=36, v_back=32, entry_z=0.75, exit_factor=0.25, hold=5 |
| `patient_hold` | D2：耐心持仓，exit_z=0.25，等待价差完全穿越均值才平仓 | 最大化单笔回归收益：宽止损给空间，持仓20天等待完全均值回归，适合协整强的配对 | z_back=40, v_back=36, entry_z=1.0, exit_z=0.25, stop×2.5, hold=20 |
| `flash_hold` | D3：超短持仓3天，强制3天内离场，测试均值回归是否主要在1-3天内完成 | 极短持仓大幅降低尾部风险暴露；amplifier=1防止短持仓内大仓位出问题。测试快速回归假设 | z_back=25, v_back=20, entry_z=0.5, amplifier=1, stop×1.5, hold=3 |
| `symmetric_exit` | D4：对称入出场，exit_z = entry_z/2，出场门槛始终与入场成比例 | 消除入场和出场门槛设置不一致的问题，测试比例化出场策略能否稳定改善胜率和平均收益 | z_back=36, v_back=32, entry_z=1.0, exit_z=0.5, amplifier=2, hold=15 |
| `aggressive` | E1：激进全面型，低门槛+高杠杆+宽止损+长持仓+快冷静期 | 多维度协同放大：低门槛多开仓、3倍杠杆放大仓位、宽止损容忍波动、长持仓等待回归。承担最大尾部风险 | z_back=30, v_back=28, entry_z=0.5, amplifier=3, stop×2.5, hold=20 |
| `conservative` | E2：保守防守型，高门槛+低杠杆+紧止损+长冷静期 | 多维度协同收缩：高门槛极少开仓、1倍杠杆不放大、紧止损快切损、长冷静期防反复。最低频率最高质量 | z_back=40, v_back=36, entry_z=1.25, amplifier=1, stop×1.5, hold=12 |
| `deep_dislocation` | E3：深度偏离猎手，entry_z=1.25+factor=3.0，只等超极端偏离 | 普通波动下entry_z≈2.5，每年开仓极少。高杠杆3倍放大每次罕见机会的收益。最低频次最大单笔 | z_back=40, v_back=32, entry_z=1.25, factor=3.0, amplifier=3, hold=20 |
| `high_turnover` | E4：高频紧风控，低门槛快速入场+止损极紧+超短持仓+长冷静期 | 高换手率靠胜率和次数取胜，每笔亏损极小。止损×1.5+持仓5天+冷静3天严格控制每笔风险暴露 | z_back=25, v_back=25, entry_z=0.5, amplifier=2, stop×1.5, hold=5 |
| `low_vol_specialist` | F1：低波动率市场专用，短v_back=15捕捉当前低波动 | 低波动时normalized_vol→0，entry_z≈base_entry_z，频繁触发开仓。tight stop适合低波动价差的小幅回归 | z_back=30, v_back=15, entry_z=0.5, factor=1.5, amplifier=2, stop×1.5 |
| `high_vol_specialist` | F2：高波动率市场专用，长v_back=50稳定波动基准 | 高波动时normalized_vol→1，entry_z被推高，只在极端偏离入场。宽止损×3应对高波动价差的大幅震荡 | z_back=40, v_back=50, entry_z=1.0, factor=2.0, amplifier=2, stop×3.0 |
| `vol_adaptive` | F3：波动自适应全范围，entry_factor=3.0自动拉伸阈值 | 低波动：entry_z≈0.5低门槛；高波动：entry_z→2.0高门槛。全自动适应市场状态，无需人工切换模式 | z_back=36, v_back=28, entry_z=0.5, factor=3.0, amplifier=2, stop×2 |
| `vol_agnostic` | F4：波动率无关，低entry_factor不管波动率都积极入场 | 去掉波动率门控：entry_factor=0.5使阈值几乎不随波动率变化，宽止损×3给每笔交易足够呼吸空间 | z_back=36, v_back=32, entry_z=0.75, factor=0.5, amplifier=2, stop×3.0 |
| `fast_reentry` | G1：即时重入，cooling_off=1，止损后次日可重新开仓 | 假设均值回归配对止损后信号很快再次出现，快速重入不错过机会。与slow_reentry形成冷静期对照 | z_back=36, v_back=32, entry_z=0.75, amplifier=2, stop×2, cooling=1 |
| `slow_reentry` | G2：长冷静期，cooling_off=5，止损后等5天才重新评估 | 假设止损意味着协整关系暂时破裂，需要更多时间确认恢复。与fast_reentry对照，测试冷静期设置的净价值 | z_back=36, v_back=32, entry_z=0.75, amplifier=2, stop×2, cooling=5 |
| `stable_signal_quick_exit` | H1：组合优化 — 长z_back稳定信号 + 短持仓快速锁利 | 长窗口(z=45)过滤噪声确保信号质量，快速出场(hold=8,exit_factor=0.5)锁定回归收益，减少持仓暴露时间 | z_back=45, v_back=30, entry_z=0.75, exit_factor=0.5, amplifier=2, hold=8 |
| `fast_signal_tight_stop` | H2：组合优化 — 快信号 + 严格止损，噪声多用止损补偿 | 短窗口(z=20)天然噪声多，tight stop×1.5补偿；短持仓5天防信号腐烂。快信号+严格纪律的组合 | z_back=20, v_back=20, entry_z=0.75, amplifier=2, stop×1.5, hold=5 |
| `medium_signal_high_leverage` | H3：组合优化 — 均衡窗口 + 高杠杆 + 宽止损，协整好的配对最适合 | 均衡窗口(z=36)信号稳定，高杠杆3倍放大每笔回归，宽止损×2.5给大仓位呼吸空间。适合高质量配对 | z_back=36, v_back=28, entry_z=0.75, amplifier=3, stop×2.5, hold=15 |
| `deep_entry_quick_exit` | H4：组合优化 — 深度偏离入场 + 快速获利了结 | 高门槛(entry_z=1.25,factor=2.5)极少开仓但置信度高，快速出场(exit_factor=0.25,hold=8)立即锁利 | z_back=40, v_back=32, entry_z=1.25, factor=2.5, amplifier=2, hold=8 |
| `conservative_no_leverage` | H5：组合优化 — 保守信号 + 无杠杆 + 超宽止损，纯信号质量驱动 | 最低频率入场(z=45,factor=3.0)，amplifier=1完全不放大，超宽止损×3给每笔完全自然回归的空间 | z_back=45, v_back=40, entry_z=1.0, factor=3.0, amplifier=1, stop×3.0 |
| `balanced_plus` | H6：组合优化 — default微调版，窗口略短+持仓略长，适合协整稳定配对 | 在default基础上缩短窗口(z=30)提高响应速度，延长持仓15天等待充分回归，其他参数不变的微幅激进版 | z_back=30, v_back=28, entry_z=0.75, amplifier=2, stop×2, hold=15 |

---

## MTFS 31 个参数集设计记录

| 参数集 | 金融内涵 | 权重设计逻辑 | 建议权重 |
|--------|----------|-------------|---------|
| `default` | A1：默认基准，VAMS评分，短期偏重，10天换仓，严格风控 | LLT=True，windows=[6,12,30,60,120,150]不变。默认配置已充分测试，前三窗口均等偏重是合理基准 | [0.20, 0.20, 0.20, 0.15, 0.15, 0.10] |
| `raw_momentum` | A2：原始动量（不用VAMS），测VAMS vs raw差异 | LLT=True，不修改。原始动量配LLT本身已是对平滑性的一种补偿，权重与default对齐保证对照组纯净 | [0.20, 0.20, 0.20, 0.15, 0.15, 0.10] |
| `short_term_tilt` | A3：极端短期权重，最快响应，高换手 | LLT=True，不修改。极短期偏重是本组核心假设，LLT平滑后短期信号质量更高，无需调整 | [0.30, 0.25, 0.20, 0.10, 0.10, 0.05] |
| `long_term_tilt` | A4：追踪长期趋势，少换手，平滑 | LLT=False，windows改[5,12,20,30,60,110]。核心意图是长期偏重，60/110仍应偏高，但新窗口变短需补充中期 | [0.10, 0.12, 0.18, 0.18, 0.22, 0.20] |
| `kalman_aggressive` | B1：Kalman+高杠杆，不过滤趋势，激进 | LLT=False，windows改[5,12,20,30,60,110]。激进策略应快速响应，前三窗口要重，但Kalman hedge比较稳所以不用极端 | [0.28, 0.25, 0.22, 0.10, 0.10, 0.05] |
| `uniform_weights` | B2：均等权重基准，消除偏向 | LLT=False，windows改[5,12,20,30,60,110]。精神是等权，新windows下维持接近均等 | [0.17, 0.17, 0.17, 0.17, 0.17, 0.15] |
| `aggressive` | C1：宽止损+高杠杆+快重入，激进风控 | LLT=True，不修改。高杠杆激进风控配合前期偏重，与short_term_tilt形成差异化（风控维度 vs 信号维度） | [0.20, 0.20, 0.20, 0.15, 0.15, 0.10] |
| `conservative` | C2：紧止损+低杠杆+长冷却，保守风控 | LLT=False，windows改[5,12,20,30,60,110]。保守策略容忍噪音少，短期信号容易假信号，适度短期偏但不极端 | [0.25, 0.22, 0.22, 0.12, 0.12, 0.07] |
| `vol_sized_conservative` | C3：vol-weighted仓位+低杠杆，保守增益路径 | LLT=True，不修改。已改为LLT=True，vol-weighted sizing本身是风险均衡，权重维持default对称 | [0.20, 0.20, 0.20, 0.15, 0.15, 0.10] |
| `fast_rebalance` | D1：超高频换仓5天，最快再平衡 | LLT=True，不修改。5天换仓已极短频，配合前期偏重权重，LLT平滑减少噪声交易 | [0.25, 0.25, 0.20, 0.15, 0.10, 0.05] |
| `slow_rebalance` | D2：每15天换仓，中频 | LLT=False，windows改[5,12,20,30,60,110]。换仓慢意味着跟中期趋势，不宜过度短期，应向中期窗口集中 | [0.18, 0.18, 0.25, 0.20, 0.12, 0.07] |
| `beta_neutral` | E1：beta-neutral对冲，消除市场暴露 | LLT=True，不修改。beta-neutral配合中短期均衡权重，已改LLT=True，信号质量有保障 | [0.20, 0.20, 0.20, 0.15, 0.15, 0.10] |
| `kalman_hedge` | E2：Kalman对冲，hedge ratio随时间漂移 | LLT=False，windows改[5,12,20,30,60,110]。Kalman对冲适合中长期稳定趋势，信号应中期偏重 | [0.22, 0.22, 0.25, 0.13, 0.12, 0.06] |
| `no_reversal_protection` | F1：不做反转检测，不止损反转，持仓更久 | LLT=False，windows改[5,12,20,30,60,110]。没有反转保护→依赖更长时间的真实趋势确认，中期权重更重更安全 | [0.20, 0.22, 0.25, 0.15, 0.12, 0.06] |
| `sensitive_reversal` | F2：高敏感反转保护，快速止损衰减 | LLT=True，不修改。快速检测反转配合短期偏重，LLT平滑后反转信号更干净 | [0.20, 0.20, 0.20, 0.15, 0.15, 0.10] |
| `balanced` | G1：稳健均衡，适度短期偏重+VAMS+适度杠杆 | LLT=True，不修改。均衡配置权重分布合理，LLT增益已足够，保持当前权重保持对照意义 | [0.15, 0.20, 0.20, 0.20, 0.15, 0.10] |
| `fast_strict` | G2：快速响应+严格风控，短期+低风险 | LLT=True，不修改。短期偏重+严格止损，LLT减少假信号，前期偏重合理 | [0.25, 0.25, 0.20, 0.15, 0.10, 0.05] |
| `trend_leverage` | G3：高杠杆+趋势确认，中期趋势偏重 | LLT=False，windows改[5,12,20,30,60,110]。高杠杆+有趋势确认，中期动量最可靠，20/30天主导 | [0.18, 0.22, 0.25, 0.18, 0.12, 0.05] |
| `no_skip_month` | G4：无skip-month，直接用最近价格，测试skip-month影响 | LLT=True，不修改。skip_days=0使所有窗口都用最新价格，LLT平滑后短期端更清晰，权重与default一致为对照 | [0.20, 0.20, 0.20, 0.15, 0.15, 0.10] |
| `entry_filter_kalman` | H1：强入场阈值(0.05)+Kalman，只做强信号 | LLT=False，windows改[5,12,20,30,60,110]。强过滤意味着只交易高确信度信号，中期信号更稳定可靠，前期稍加权 | [0.23, 0.23, 0.25, 0.12, 0.10, 0.07] |
| `entry_threshold_strong` | H2：强入场阈值(0.05)，高质量少交易，dollar_neutral | LLT=False，windows改[5,12,20,30,60,110]。同H1但dollar_neutral，同理中期偏重，前期略高 | [0.23, 0.23, 0.25, 0.12, 0.10, 0.07] |
| `vol_weighted_sizing` | I1：仓位按波动率加权，低波动品种分更多资金 | LLT=False，windows改[5,12,20,30,60,110]。Vol-weighted本身已是风险均衡机制，动量权重可以更前倾捕捉机会 | [0.28, 0.25, 0.22, 0.10, 0.10, 0.05] |
| `raw_momentum_kalman` | I2：原始动量(use_vams=False)+Kalman，两轴交叉 | LLT=False，windows改[5,12,20,30,60,110]。原始动量对大幅波动很敏感，短期噪音大，中期原始动量更稳 | [0.22, 0.22, 0.25, 0.14, 0.10, 0.07] |
| `monthly_aligned_windows` | J1：月度对齐窗口[21,42,63,84,105,126]，贴合机构月度周期 | LLT=False，windows已是月度对齐不修改。权重维持原样，windows本身就是实验变量，不叠加权重干扰 | [0.20, 0.20, 0.20, 0.15, 0.15, 0.10] |
| `weekly_aligned_windows` | J2：周/月对齐窗口[5,10,20,40,60,90]，高频短周期 | LLT=True，windows已是周对齐不修改。短期偏重配合快速再平衡，LLT减少高频噪声 | [0.25, 0.25, 0.20, 0.15, 0.10, 0.05] |
| `crash_defensive` | K1：极早触发崩溃保护(p75)，崩溃时缩至10% | LLT=False，windows改[5,12,20,30,60,110]。崩溃时仓位极小，主要靠短期信号快速响应崩溃后的反弹 | [0.28, 0.25, 0.22, 0.10, 0.10, 0.05] |
| `crash_tolerant` | K2：放宽崩溃保护(p95)，高vol才触发，保留40%仓位 | LLT=False，windows改[5,12,20,30,60,110]。容忍波动→让中长期趋势发展，不用短期信号频繁调仓 | [0.18, 0.20, 0.25, 0.18, 0.12, 0.07] |
| `long_term_kalman` | L1：极长期权重+Kalman，最稳定的长期对冲 | LLT=False，windows改[5,12,20,30,60,110]。核心是长期，新windows变短，60/110仍应主导，但适当补充中期 | [0.10, 0.13, 0.17, 0.22, 0.23, 0.15] |
| `short_term_beta_neutral` | L2：极短期偏重+beta-neutral，纯相对强弱短期版 | LLT=True，不修改。极短期权重[0.35,0.25,...]配beta-neutral是纯短期相对动量，LLT平滑后效果更佳 | [0.35, 0.25, 0.20, 0.10, 0.05, 0.05] |
| `beta_neutral_long_term` | L3：beta-neutral+极长期权重，消除beta+长期趋势 | LLT=False，windows改[5,12,20,30,60,110]。消除beta暴露，长期相对强弱更稳。和long_term_kalman同理，长期主导 | [0.10, 0.13, 0.17, 0.22, 0.23, 0.15] |
| `let_profits_run` | L4：宽止损+长持仓+慢反转检测，让强势趋势充分发展 | LLT=False，windows改[5,12,20,30,60,110]。慢反转检测+长持仓→中期到长期信号确认趋势，不被短期噪声退出 | [0.13, 0.18, 0.25, 0.22, 0.15, 0.07] |
