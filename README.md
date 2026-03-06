# someopark — Mean Reversion Pair Trading (MRPT)

均值回归配对交易策略回测框架。支持多配对并发、Kalman Filter 对冲比率、动态 z-score 阈值、财报黑名单过滤。

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

| 文件 | 说明 |
|------|------|
| `PortfolioMRPTRun.py` | 策略主逻辑（均值回归 + 财报黑名单） |
| `PortfolioMRPTStrategyRuns.py` | JSON 驱动的批量回测入口，包含全部 32 个 param_set 定义 |
| `PortfolioClasses.py` | Portfolio、Order、StopLoss 等基础类 |
| `PriceDataStore.py` | 价格数据读取与缓存（Polygon / Yahoo） |
| `MRPTUpdateConfigs.py` | 读取 Step 1 结果，DSR 过滤后生成 Step 2 / Step 3 config |
| `MRPTWalkForward.py` | Walk-forward 优化：扩展/滚动训练窗口 + DSR 选参 + 严格 OOS 评估 |
| `MRPTWalkForwardReport.py` | 自动读取最近一次 walk-forward 运行结果，生成完整 OOS 报告（窗口级 + 配对级 + 拼接权益曲线） |
| `MRPTAuditPairs.py` | 验证每个 Excel 输出文件的规则合规性 |
| `MRPTGenerateReport.py` | 生成回测 vs 验证期对比报告 |
| `MRPTFetchEarnings.py` | 从 Polygon 拉取并缓存财报日期 |
| `MRPTDailySignal.py` | 每日信号生成器（盘后运行，输出 OPEN/CLOSE/HOLD 指令） |

---

## 完整运行流程（三步法）

### 准备：更新财报日期缓存

在运行回测前，确保财报缓存是最新的：

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
conda run -n someopark_run python MRPTAuditPairs.py
# 自动读取最新的 strategy_summary CSV，对每个 Excel 文件的每个配对运行 7 项检查
# 输出保存至 historical_runs/audit/audit_<timestamp>.txt
```

---

### Step 1 → Step 2/3：自动选对并生成 config

读取 Step 1 的 summary CSV，从每个配对的 32 次 run 中挑出最优参数集，自动写入 Step 2 和 Step 3 的 config 文件：

```bash
conda run -n someopark_run python MRPTUpdateConfigs.py
# 自动读取 historical_runs/ 中最新的 strategy_summary_*.csv

# 或者指定具体文件：
conda run -n someopark_run python MRPTUpdateConfigs.py historical_runs/strategy_summary_<timestamp>.csv
```

**选对标准（可在脚本顶部修改）：**

| 条件 | 默认值 | 说明 |
|------|--------|------|
| 配对在该 run 的 PnL | > 0 | 基于 acc_pair_trade_pnl_history |
| 配对的开仓次数 | ≥ 3 | pair_trade_history 中 open 行数 |
| Deflated Sharpe Ratio | > 0.5 | 多重比较修正（32 个 param_set = 32 次试验） |

每个配对选出**配对级 Sharpe 最高**（同 Sharpe 时用 PnL 打平）的那个 param_set。DSR < 0.5 的结果被视为噪声并排除。不符合条件的配对被排除在 Step 2/3 之外。

> **DSR（Deflated Sharpe Ratio）**：Bailey & López de Prado (2014) 提出的多重比较修正方法。对同一配对测试 32 个 param_set，最优 Sharpe 必然偏高。DSR 用期望最大 Sharpe 作为基准，修正后 p-value > 0.5 才认为结果具有统计意义，而非偶然选优的假象。

**输出（自动覆盖写入）：**
- `run_configs/runs_20260304_step2_best_backtest.json`
- `run_configs/runs_20260304_step3_forward.json`

脚本还会保存一份可供检查的明细表：`historical_runs/grid_pair_breakdown_<timestamp>.csv`

---

### Step 2 — 组合回测（选中配对 + 各自最优参数）

**目标**：将选中配对合并在**一个 run** 里，每个配对使用各自的最优参数集，进行完整回测。

```bash
export $(cat .env | xargs) && conda run -n someopark_run python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step2_best_backtest.json
```

- 一个 run，所有选中配对跑在同一个组合 portfolio 中
- 每个配对 `[s1, s2, param_set]` 使用自己从 Step 1 选出的 param_set
- 输出：单个 Excel 文件 + summary CSV

---

### Step 3 — 向前验证（Forward Test）

**目标**：用相同配对和参数在最近 30 天的真实市场数据上做验证，检验 Step 2 回测结果是否具有前向一致性。

```bash
export $(cat .env | xargs) && conda run -n someopark_run python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step3_forward.json
```

- 与 Step 2 完全相同的配对和参数
- `trade_start_date`：Step 2 回测结束日的下一个交易日（由 `MRPTUpdateConfigs.py` 自动填入）
- 实际交易只发生在 `trade_start_date` 之后（约最近 30 天）

---

### Step 4 — 生成对比报告

```bash
conda run -n someopark_run python MRPTGenerateReport.py <step2_excel> <step3_excel>

# 示例：
conda run -n someopark_run python MRPTGenerateReport.py \
  historical_runs/portfolio_history_step2_best_per_pair_default_<ts>.xlsx \
  historical_runs/portfolio_history_step3_forward_default_<ts>.xlsx
```

输出：`historical_runs/report_bt_vs_fwd_<timestamp>.xlsx`

报告包含回测期 vs 验证期的 PnL、Sharpe、每配对明细对比。

---

## Walk-Forward 优化（高级）

标准三步法（Step 1→2→3）中，Step 2 的回测数据与 Step 1 的选参数据重叠，存在**样本内过拟合风险**。`MRPTWalkForward.py` 实现了严格的时间分离：

### 原理

支持两种窗口模式：

| 模式 | 说明 |
|------|------|
| `expanding`（默认） | train_start 固定（锚定在首个 OOS 窗口前 18 个月），训练集随窗口递增 |
| `rolling` | 训练集长度固定（始终 18 个月），train_start 随窗口右移 |

默认配置（6 窗口 × 25 交易日，18 个月训练）：

| 窗口 | 训练期（expanding） | 测试期（样本外）|
|------|---------------------|----------------|
| Window 1 | 2024-01-30 → 2025-07-29 | 2025-07-30 → 2025-09-03 |
| Window 2 | 2024-01-30 → 2025-09-03 | 2025-09-04 → 2025-10-08 |
| Window 3 | 2024-01-30 → 2025-10-08 | 2025-10-09 → 2025-11-12 |
| Window 4 | 2024-01-30 → 2025-11-12 | 2025-11-13 → 2025-12-18 |
| Window 5 | 2024-01-30 → 2025-12-18 | 2025-12-19 → 2026-01-27 |
| Window 6 | 2024-01-30 → 2026-01-27 | 2026-01-28 → 2026-03-04 |

- 训练期：跑 32-param 网格搜索，用 DSR 选出每配对最优参数
- 测试期：用训练期选出的参数，在**从未见过**的数据上运行（纯样本外）
- 六段样本外 equity curve 拼接 → 约 150 个交易日的真实 OOS 评估

### 运行

```bash
# expanding 模式（默认）
export $(cat .env | xargs) && conda run -n someopark_run python MRPTWalkForward.py

# rolling 模式（固定训练窗口长度）
export $(cat .env | xargs) && conda run -n someopark_run python MRPTWalkForward.py --mode rolling

# 选项
python MRPTWalkForward.py --oos-windows 6 --oos-days 150 --train-months 18

# 跳过已完成的网格搜索（续跑）
python MRPTWalkForward.py --skip-grid
```

### 输出

```
historical_runs/walk_forward/
  window01_<train_start>_<train_end>/   ← 每个训练窗口的 grid 结果
  window02_.../
  ...
  dsr_selection_log_<ts>.csv            ← 每窗口每配对的 DSR 值 + 是否入选
  walk_forward_summary_<ts>.json        ← 各窗口 OOS Sharpe / PnL / MaxDD 汇总
  oos_report_<ts>.txt                   ← 完整 OOS 报告（MRPTWalkForwardReport.py 生成）
  oos_equity_curve_<ts>.csv             ← 拼接后的样本外权益曲线
  oos_pair_summary_<ts>.csv             ← 配对级 OOS 汇总表
```

### 生成详细 OOS 报告

walk-forward 跑完后，运行以下命令生成完整报告：

```bash
conda run -n someopark_run python MRPTWalkForwardReport.py

# 指定特定运行（按 train_start 前缀过滤）：
conda run -n someopark_run python MRPTWalkForwardReport.py --run-prefix 2024-02-01
```

报告包含：
- **窗口级汇总**：每个 OOS 窗口的 PnL / Sharpe / MaxDD / 天数
- **拼接 OOS 总览**：跨 6 窗口的净 PnL、Sharpe、最大回撤；利息拆分（毛收益 vs 净收益）
- **配对级明细**：每个配对在全部窗口的总 PnL / Sharpe / MaxDD / 胜率 / 交易次数 / 成交额
- **各窗口入选配对**：DSR 过滤后实际参与 OOS 的配对列表

> **利息说明**：`interest_expense_history` 存储每日保证金借贷成本（日利率，每天重复），报告对 OOS 期间各日求和得到实际利息费用。Gross PnL = 配对 dod PnL 合计；Net PnL = Gross − 利息，与 equity 首尾差吻合。

### 解读结果

- **OOS Sharpe > 0** 且**各窗口一致正向**：策略真实可用
- **OOS Sharpe 远低于 IS Sharpe**：存在过拟合，需审查参数复杂度
- **DSR 选中率低**（多数配对被 DSR 过滤）：信号质量弱，需重新设计因子

---

## Run Config 格式

`run_configs/` 目录下的 JSON 文件控制每次回测：

```json
{
  "start_date": "2024-01-30",
  "end_date": "auto_minus_30d",
  "runs": [
    {
      "label": "my_run",
      "param_set": "default",
      "pairs": [
        ["MSCI", "LII"],
        ["LYFT", "UBER", "short_z_long_v"],
        ["CART", "DASH", "vol_adaptive"]
      ]
    }
  ]
}
```

**`end_date` 特殊值：**

| 值 | 说明 |
|----|------|
| `"auto_minus_30d"` | 今天减 30 天（留出验证窗口） |
| `"auto"` | 今天（用于向前验证） |
| `"2026-02-27"` | 固定日期 |

**`trade_start_date`**（可选）：设置后策略在此日期前只做 warmup，不实际开仓，用于向前验证。

**配对格式：**
- `[s1, s2]` — 使用 run 级别的 `param_set`
- `[s1, s2, "param_set_name"]` — 为该配对单独指定参数集（Step 2/3 用此格式）

---

## 可用参数集（param_set）

共 32 个，分 8 组，在 `PortfolioMRPTStrategyRuns.py` 的 `PARAM_SETS` 中定义：

| 组 | 参数集 | 风格 |
|----|--------|------|
| A — 杠杆基准 | `default` | 均衡基准 |
| | `no_leverage` | 无杠杆，纯信号质量测试 |
| | `high_leverage` | 3× 杠杆 + 宽止损 |
| | `tight_stop` | 默认参数 + 紧止损 (×1.5) |
| B — 信号速度 | `fast_signal` | 短窗口 (z=20,v=20)，快速响应 |
| | `slow_signal` | 长窗口 (z=50,v=50)，稳健信号 |
| | `long_z_short_v` | 长 z-score + 短波动率窗口 |
| | `short_z_long_v` | 短 z-score + 长波动率窗口 |
| C — 入场阈值 | `low_entry` | 低门槛高频入场 |
| | `high_entry` | 高门槛低频入场 |
| | `static_threshold` | 静态阈值，不随波动率变化 |
| | `vol_gated` | 极度动态阈值，高波动时几乎不入场 |
| D — 出场策略 | `quick_exit` | 价差开始回归即离场 |
| | `patient_hold` | 等待完全回归再离场 |
| | `flash_hold` | 超短持仓（≤3天） |
| | `symmetric_exit` | 出场阈值与入场阈值保持比例 |
| E — 风险档位 | `aggressive` | 低门槛 + 高杠杆 + 宽止损 |
| | `conservative` | 高门槛 + 低杠杆 + 紧止损 |
| | `deep_dislocation` | 只等极端偏离（>2.5σ）入场 |
| | `high_turnover` | 高频交易 + 严格止损 |
| F — 波动率专项 | `low_vol_specialist` | 低波动率市场专用 |
| | `high_vol_specialist` | 高波动率市场专用 |
| | `vol_adaptive` | 全范围自适应，阈值随波动率线性拉伸 |
| | `vol_agnostic` | 不受波动率门控影响，宽止损兜底 |
| G — 冷静期 | `fast_reentry` | 止损后次日可重新入场 |
| | `slow_reentry` | 止损后等 5 天再重新入场 |
| H — 组合优化 | `stable_signal_quick_exit` | 长窗口稳定信号 + 快速锁利 |
| | `fast_signal_tight_stop` | 快信号 + 严格止损 |
| | `medium_signal_high_leverage` | 均衡窗口 + 3× 杠杆 |
| | `deep_entry_quick_exit` | 极端偏离入场 + 立即锁利 |
| | `conservative_no_leverage` | 保守信号 + 无杠杆 + 超宽止损 |
| | `balanced_plus` | 比 default 略激进的均衡方案 |

---

## 财报黑名单过滤（MRPT 专属）

策略在以下情况下**不开新仓**（已有仓位的平仓和止损不受影响）：

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
| `historical_runs/` | 回测 Excel 结果、strategy summary CSV |
| `historical_runs/audit/` | audit 报告（`MRPTAuditPairs.py` 输出） |
| `charts/` | 策略权益曲线图表 |
| `run_configs/` | 回测配置 JSON 文件 |
| `logs/` | 运行日志 |
| `archive/` | 早期运行的历史归档（不提交到 git） |
