# someopark — 双策略配对交易回测框架

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
```

> `.env` 已加入 `.gitignore`，不会提交到版本库。

### 3. 运行所有脚本的正确方式

**必须同时使用 conda 环境 + 加载 `.env`，否则会报 `ModuleNotFoundError` 或 `KeyError: POLYGON_API_KEY`：**

```bash
export $(cat .env | xargs) && conda run -n someopark_run python <script.py> [args]
```

示例：

```bash
export $(cat .env | xargs) && conda run -n someopark_run python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step1_grid32.json
```

> 直接 `python` 或 `conda activate` 后再 `python` 均不可靠——`conda run -n someopark_run` 是唯一确保环境正确的方式。

---

## 核心文件

### 共用基础设施

| 文件 | 说明 |
|------|------|
| `PortfolioClasses.py` | Portfolio、Order、StopLoss 等基础类（两个策略共用） |
| `PriceDataStore.py` | 价格数据读取与缓存（Polygon / Yahoo），Parquet 格式 |
| `AuditPairs.py` | 验证 Excel 输出文件规则合规性（MRPT / MTFS 通用，`--strategy mrpt\|mtfs`） |
| `DailySignal.py` | 每日信号生成器（MRPT / MTFS 通用，`--strategy mrpt\|mtfs`） |

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
| `PortfolioMTFSStrategyRuns.py` | JSON 驱动的批量回测入口，含全部 30 个 param_set 定义 |
| `MTFSUpdateConfigs.py` | 读取 Step 1 结果，DSR 过滤后生成 Step 2 / Step 3 config |
| `MTFSWalkForward.py` | Walk-forward 优化：6 窗口 × 27 OOS 交易日，DSR 选参 |
| `MTFSWalkForwardReport.py` | 读取最近一次 walk-forward 结果，生成完整 OOS 报告（含止损类型分解） |
| `MTFSGenerateReport.py` | 生成回测 vs 验证期对比报告 |

---

## MRPT 完整运行流程（三步法）

### 准备：更新财报日期缓存

```bash
export $(cat .env | xargs) && conda run -n someopark_run python MRPTFetchEarnings.py
```

输出：`price_data/earnings_cache.json`

---

### Step 1 — 参数网格搜索

**目标**：用 32 个参数集分别运行全部 15 个配对，找出每个配对的最优参数集。

```bash
export $(cat .env | xargs) && conda run -n someopark_run python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step1_grid32.json
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
conda run -n someopark_run python MRPTUpdateConfigs.py
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
export $(cat .env | xargs) && conda run -n someopark_run python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step2_best_backtest.json
```

所有选中配对在同一个组合 portfolio 中运行，每个配对使用各自最优 param_set。

---

### Step 3 — 向前验证

```bash
export $(cat .env | xargs) && conda run -n someopark_run python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step3_forward.json
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
export $(cat .env | xargs) && conda run -n someopark_run python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_step1_grid30.json
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
conda run -n someopark_run python MTFSUpdateConfigs.py
```

**选对标准：**

| 条件 | 默认值 |
|------|--------|
| 配对 PnL | > 0 |
| 开仓次数 | ≥ 3 |
| Deflated Sharpe Ratio | > 0.5（30 次试验修正） |

**输出（自动覆盖写入）：**
- `run_configs/mtfs_runs_step2_best_backtest.json`
- `run_configs/mtfs_runs_step3_forward.json`

---

### Step 2 — 组合回测

```bash
export $(cat .env | xargs) && conda run -n someopark_run python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_step2_best_backtest.json
```

---

### Step 3 — 向前验证

```bash
export $(cat .env | xargs) && conda run -n someopark_run python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_step3_forward.json
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

每个交易日收盘后运行，根据最新价格输出每对的操作指令（OPEN/CLOSE/HOLD/FLAT），并更新本地持仓记录。

```bash
# MRPT 信号（均值回归策略）
export $(cat .env | xargs) && conda run -n someopark_run python DailySignal.py --strategy mrpt

# MTFS 信号（动量趋势策略）
export $(cat .env | xargs) && conda run -n someopark_run python DailySignal.py --strategy mtfs

# 指定日期（补跑）
python DailySignal.py --strategy mrpt --date 2026-03-04

# dry-run（只看信号，不更新 inventory）
python DailySignal.py --strategy mtfs --dry-run
```

- 持仓记录分别保存在 `inventory_mrpt.json` 和 `inventory_mtfs.json`
- 信号输出写入 `signals/mrpt_signals_YYYYMMDD.json` 和 `signals/mtfs_signals_YYYYMMDD.json`
- MRPT 信号包含财报黑名单（BLACKOUT），MTFS 不适用

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

#### MTFS Walk-Forward 窗口（6×27，expanding，基于 30 个 param_set）

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
export $(cat .env | xargs) && conda run -n someopark_run python MRPTWalkForward.py

# rolling 模式
export $(cat .env | xargs) && conda run -n someopark_run python MRPTWalkForward.py --mode rolling

# 可选参数
python MRPTWalkForward.py --oos-windows 6 --oos-days 162 --train-months 18

# 续跑（跳过已完成的网格搜索）
python MRPTWalkForward.py --skip-grid
```

### 运行 MTFS Walk-Forward

```bash
# expanding 模式（默认）
export $(cat .env | xargs) && conda run -n someopark_run python MTFSWalkForward.py

# rolling 模式
export $(cat .env | xargs) && conda run -n someopark_run python MTFSWalkForward.py --mode rolling

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
- **拼接 OOS 总览**：跨 6 窗口的净 PnL、Sharpe、最大回撤；利息拆分
- **配对级明细**：各配对在全部窗口的总 PnL / Sharpe / 胜率 / 交易次数
- **各窗口入选配对**：DSR 过滤后实际参与 OOS 的配对列表
- **止损分解（MTFS 专属）**：各类止损触发次数及占比

### 解读结果

- **OOS Sharpe > 0** 且**各窗口一致正向**：策略真实可用
- **OOS Sharpe 远低于 IS Sharpe**：存在过拟合，需审查参数复杂度
- **DSR 选中率低**：信号质量弱，需重新设计因子

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
| `mtfs_runs_step1_grid30.json` | Step 1：30 param_set 网格搜索 |
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

### MTFS — 30 个（`PortfolioMTFSStrategyRuns.py`）

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
| `charts/` | 策略权益曲线图表 |
| `logs/` | 运行日志 |
| `archive/` | 早期运行的历史归档（不提交到 git） |
