# Someopark Run Commands Manual

所有命令必须在 `/Users/xuling/code/someopark-test/` 目录下运行。
所有命令必须先加载 `.env`（含 POLYGON_API_KEY、FRED_API_KEY、MONGO_URI、MONGO_VEC_URI），并使用 `someopark_run` conda 环境。

**通用前缀（每条命令都要带）：**
```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python <脚本> <参数>
```

---

## 0a. SelectPairs.py — 从数据库筛选最优配对（换配对时首先运行）

**从 someopark 数据库的 `pairs_day_select` 集合中筛选 MRPT 和 MTFS 最优 15 对配对。需要 `MONGO_URI` 环境变量。**

```bash
# 预览：分析最近30天，打印推荐配对（不写入任何文件）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python SelectPairs.py

# 分析最近60天
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python SelectPairs.py --days 60

# 确认结果无误后，写入 pair_universe_mrpt.json / pair_universe_mtfs.json
# （自动将旧文件备份为 pair_universe_mrpt_backup.json / pair_universe_mtfs_backup.json）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python SelectPairs.py --save
```

**筛选逻辑：**

| 策略 | 评分公式 | s1/s2 方向 |
|------|----------|-----------|
| MRPT | `coint_rate×1.0 + pca_rate×0.5 + similar_bonus×0.3` | 字母序（均值回归不依赖方向） |
| MTFS | `pca_rate²×(1-coint_rate) + similar_rate×0.5`，0.9× 惩罚偶发协整 | **s1 = 近 30 天涨幅更高的 ticker**，s2 = 涨幅低的 ticker |

输出（`--save` 时覆写）：
- `pair_universe_mrpt.json` — MRPT 15对
- `pair_universe_mtfs.json` — MTFS 15对
- 旧文件自动备份为 `*_backup.json`

**完成后必须运行 `UpdateStep1Configs.py`（见下节）。**

---

## 0b. UpdateStep1Configs.py — 换配对后更新 Step1 config（换配对时才需要）

**只在修改了 `pair_universe_mrpt.json` 或 `pair_universe_mtfs.json` 之后运行，普通回测不需要。**

影响范围：
- `PortfolioMRPTStrategyRuns.py` / `PortfolioMTFSStrategyRuns.py` Step1 grid search（直接读 config 里的 pairs）
- `MRPTWalkForward.py` / `MTFSWalkForward.py`（内部也读 Step1 config 里的 param_set + pairs 做 grid search）

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python UpdateStep1Configs.py
```

更新（in-place）：
- `run_configs/runs_20260304_step1_grid32.json` — MRPT Step1 config 的 pairs
- `run_configs/mtfs_runs_step1_grid30.json` — MTFS Step1 config 的 pairs

完成后再运行 Step1 grid search 或 WalkForward。

---

## 1. PortfolioMRPTStrategyRuns.py — MRPT 批量回测

接收一个 JSON config 文件作为参数。**Step 1 跑完后必须先运行 MRPTUpdateConfigs.py，才能运行 Step 2。**

```bash
# 换配对时才需要（更新 runs_20260304_step1_grid32.json 的 pairs，Step2/3 不受影响）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python UpdateStep1Configs.py

# Step 1: Grid search（32个param_set × 15对）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step1_grid32.json

# Step 1 完成后：更新 Step2/Step3 config（指定 Step 1 输出的 CSV）
# CSV 文件名格式：historical_runs/strategy_summary_<ts>.csv
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTUpdateConfigs.py historical_runs/strategy_summary_<ts>.csv

# Step 2: 最佳 param_set 回测（依赖 UpdateConfigs 输出）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step2_best_backtest.json

# Step 3: Forward validation（最近70天）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step3_forward.json
```

输出：`historical_runs/portfolio_history_*.xlsx`，`historical_runs/mrpt_strategy_summary_<ts>.csv`（Step1输出），`historical_runs/strategy_summary_<ts>.csv`（Step1输出）

---

## 2. PortfolioMTFSStrategyRuns.py — MTFS 批量回测

接收一个 JSON config 文件作为参数。**Step 1 跑完后必须先运行 MTFSUpdateConfigs.py，才能运行 Step 2。**

```bash
# 换配对时才需要（更新 mtfs_runs_step1_grid30.json 的 pairs，Step2/3 不受影响）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python UpdateStep1Configs.py

# Step 1: Grid search（31个param_set × 15对）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_step1_grid30.json

# Step 1 完成后：更新 Step2/Step3 config（指定 Step 1 输出的 CSV）
# CSV 文件名格式：historical_runs/mtfs_strategy_summary_<ts>.csv
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSUpdateConfigs.py historical_runs/mtfs_strategy_summary_<ts>.csv

# Step 2: 最佳 param_set 回测（依赖 UpdateConfigs 输出）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_step2_best_backtest.json

# Step 3: Forward validation（最近70天）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_step3_forward.json
```

输出：`historical_runs/portfolio_history_MTFS_*.xlsx`，`historical_runs/mtfs_strategy_summary_<ts>.csv`

---

## 3. PortfolioMRPTRun.py — MRPT 单次运行

不单独直接运行，由 `PortfolioMRPTStrategyRuns.py` 调用。

---

## 4. PortfolioMTFSRun.py — MTFS 单次运行

不单独直接运行，由 `PortfolioMTFSStrategyRuns.py` 调用。

---

## 4b. MRPTFetchEarnings.py — 财报日期缓存

**从 Polygon 拉取 S&P 500 全量财报日期，缓存到 `price_data/earnings_cache.json`。MRPT + MTFS 的 Earnings Blackout 依赖此缓存。**

```bash
# 全量 S&P 500 (~621 tickers，首次运行或换配对后)
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTFetchEarnings.py --full

# 增量（只查可能有新 filing 的 ticker，cache 3 天内 fetch 过则跳过）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTFetchEarnings.py --incremental

# 仅当前 pair universe
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTFetchEarnings.py

# 指定 ticker
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTFetchEarnings.py MSCI NVDA
```

**日常运行无需手动执行**：DailySignal.py 每次运行时自动调用 `update_earnings_incremental()`，如果 cache 在 3 天内已更新过则跳过（零 API 调用、零延迟）。

**数据缓存说明**：

| 缓存文件 | 内容 | 更新方式 | 更新时机 |
|---------|------|---------|---------|
| `price_data/earnings_cache.json` | S&P 500 财报日期（~598 symbols，含 release_timing） | `MRPTFetchEarnings.py` + DailySignal 增量 | 每 3 天自动增量；换配对后 `--full` |
| `price_data/dividends_cache.json` | 分红记录（按 symbol 存储所有历史分红） | `PriceDataStore._fetch_dividends()` 按需 | 加载价格数据时自动检查并增量拉取 |

> 分红缓存不需要手动维护——每次 DailySignal 或 WalkForward 加载价格数据时，`PriceDataStore` 自动检查每个 symbol 的 `fetched_through` 是否覆盖请求的日期范围，不覆盖则从 Polygon 增量拉取。

---

## 5. MRPTUpdateConfigs.py — 更新 MRPT Step2/Step3 config

从 Step 1 的 CSV 汇总结果中，按 DSR 选出每对最佳 param_set，更新 step2/step3 的 JSON config。

```bash
# 自动使用 historical_runs/ 下最新的 mrpt_strategy_summary_*.csv
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTUpdateConfigs.py

# 指定 Step 1 的 CSV（推荐，避免误用 step3 的 CSV）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTUpdateConfigs.py historical_runs/strategy_summary_<ts>.csv
```

输出：覆盖写入 `run_configs/runs_20260304_step2_best_backtest.json`，`run_configs/runs_20260304_step3_forward.json`

---

## 6. MTFSUpdateConfigs.py — 更新 MTFS Step2/Step3 config

```bash
# 自动使用 historical_runs/ 下最新的 mtfs_strategy_summary_*.csv
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSUpdateConfigs.py

# 指定 Step 1 的 CSV（推荐）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSUpdateConfigs.py historical_runs/mtfs_strategy_summary_<ts>.csv
```

输出：覆盖写入 `run_configs/mtfs_runs_step2_best_backtest.json`，`run_configs/mtfs_runs_step3_forward.json`

---

## 6.5 MacroStateStore — 宏观快照更新（WalkForward 和 DailySignal 之前必须运行）

MacroStateStore 存储每日宏观快照（280 列指标），是 MCPS（Macro-Conditioned Parameter Selection）的数据基础。
**WalkForward 在 `select_pairs_with_dsr()` 中调用 MCPS 选择最优参数集时依赖此数据；DailySignal 每日从 Top-K 候选中选参数时也依赖此数据。**

```bash
# 每次运行 WalkForward 或 DailySignal 之前执行
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MacroStateStore.py --update
```

**数据分层（无重复）：**
- `MacroDataStore` → VIX/MOVE 原始价格 → `price_data/macro/vix/`, `move/`（RegimeDetector 使用）
- `MacroStateStore` → 读 MacroDataStore 文件 + yfinance/FRED → `price_data/macro/state/`（MCPS 使用）

如果 update 失败，WalkForward/DailySignal 降级为纯 DSR Top-1 选择（不影响正常运行）。

---

## 7. MRPTWalkForward.py — MRPT Walk-Forward 9窗口(rolling 19mo,50td,重叠10)

**换配对后需先运行 `UpdateStep1Configs.py`（WalkForward 内部读 `runs_20260304_step1_grid32.json` 的 param_set + pairs 做 grid search）。**

```bash
# 换配对时才需要（更新 runs_20260304_step1_grid32.json 的 pairs）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python UpdateStep1Configs.py

# 标准运行（6个OOS窗口，expanding模式，输出到 historical_runs/walk_forward/）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTWalkForward.py --mode rolling --train-months 19 --oos-windows 9 --oos-window-days 50 --oos-overlap 10

# 常用可选参数
#   --mode expanding|rolling     窗口模式（默认 expanding）
#   --oos-days 150               总OOS交易日数（默认150 = 6×25）
#   --train-months 18            训练期月数（默认18）
#   --last-date 2026-03-12       数据截止日期（默认自动取最近交易日）
#   --output-dir <path>          输出目录（默认 historical_runs/walk_forward/）
#   --skip-grid                  跳过已有CSV的窗口
```

输出：`historical_runs/walk_forward/walk_forward_summary_<ts>.json`，`dsr_selection_log_<ts>.csv`，`oos_equity_curve_<ts>.csv`

---

## 8. MTFSWalkForward.py — MTFS Walk-Forward 9窗口(rolling 19mo,50td,重叠10)

**换配对后需先运行 `UpdateStep1Configs.py`（WalkForward 内部读 `mtfs_runs_step1_grid30.json` 的 param_set + pairs 做 grid search）。**

```bash
# 换配对时才需要（更新 mtfs_runs_step1_grid30.json 的 pairs）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python UpdateStep1Configs.py

# 标准运行（6个OOS窗口，expanding模式，输出到 historical_runs/walk_forward_mtfs/）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSWalkForward.py --mode rolling --train-months 19 --oos-windows 9 --oos-window-days 50 --oos-overlap 10

# 常用可选参数
#   --mode expanding|rolling     窗口模式（默认 expanding）
#   --oos-days 162               总OOS交易日数（默认162 = 6×27）
#   --train-months 18            训练期月数（默认18）
#   --last-date 2026-03-12       数据截止日期（默认自动取最近交易日）
#   --output-dir <path>          输出目录（默认 historical_runs/walk_forward_mtfs/）
#   --skip-grid                  跳过已有CSV的窗口
```

输出：`historical_runs/walk_forward_mtfs/walk_forward_summary_<ts>.json`，`dsr_selection_log_<ts>.csv`，`oos_equity_curve_<ts>.csv`

---

## 9. MRPTWalkForwardReport.py — MRPT Walk-Forward 报告

```bash
# 自动读取 historical_runs/walk_forward/ 下最新的 walk_forward_summary_*.json
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTWalkForwardReport.py

# 指定目录
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTWalkForwardReport.py --wf-dir historical_runs/walk_forward/
```

---

## 10. MTFSWalkForwardReport.py — MTFS Walk-Forward 报告

```bash
# 自动读取 historical_runs/walk_forward_mtfs/ 下最新的 walk_forward_summary_*.json
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSWalkForwardReport.py

# 指定目录
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSWalkForwardReport.py --wf-dir historical_runs/walk_forward_mtfs/
```

---

## 11. MRPTGenerateReport.py — MRPT IS/OOS 综合报告

```bash
# 自动使用最新的 step2(backtest) + step3(forward) Excel
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTGenerateReport.py

# 指定文件
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTGenerateReport.py \
  historical_runs/portfolio_history_all15_best_per_pair_<ts>.xlsx \
  historical_runs/portfolio_history_forward30d_<ts>.xlsx

# 指定输出文件
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTGenerateReport.py \
  historical_runs/portfolio_history_all15_best_per_pair_<ts>.xlsx \
  historical_runs/portfolio_history_forward30d_<ts>.xlsx \
  historical_runs/mrpt_report_output.xlsx
```

---

## 12. MTFSGenerateReport.py — MTFS IS/OOS 综合报告

```bash
# 自动使用最新的 MTFS step2 + step3 Excel
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSGenerateReport.py

# 指定文件
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSGenerateReport.py \
  historical_runs/portfolio_history_MTFS_all15_<ts>.xlsx \
  historical_runs/portfolio_history_MTFS_fwd_<ts>.xlsx

# 指定输出文件
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSGenerateReport.py \
  historical_runs/portfolio_history_MTFS_all15_<ts>.xlsx \
  historical_runs/portfolio_history_MTFS_fwd_<ts>.xlsx \
  historical_runs/mtfs_report_output.xlsx
```

---

## 12b. VIXForecast.py — VIX Chronos-2 预测（可独立运行）

**每日 VIX 预测模块，独立于 DailySignal 运行。输出 0.15–0.85 分数供 RegimeDetector 使用。**

双模型集成：
- `finetune-full`：VIX + VIX9D/VIX3M past_covariates，无 FOMC
- `finetune-fomc`：VIX + VIX9D/VIX3M past_covariates + FOMC future_covariates
- 集成权重：W_full=0.542（Dir Acc 65%）/ W_fomc=0.458（Dir Acc 55%）

Checkpoint 当日复用，当天首次运行约 2–3 分钟，再次运行直接读缓存（<10 秒）。

```bash
# Zero-shot 推理（快速，约 5 秒）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python VIXForecast.py

# 双模型 fine-tuning + 推理（首次约 2-3 分钟，当日 checkpoint 复用后秒级）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python VIXForecast.py --finetune

# 强制重新 fine-tune（忽略当日 checkpoint）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python VIXForecast.py --finetune --no-cache

# FOMC rule override：FOMC 在 ≤10 交易日内时切换为 fomc 模型
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python VIXForecast.py --finetune --fomc-rule
```

**输出字段说明：**

| 字段 | 说明 |
|------|------|
| `score` | 0.15–0.85，<0.45 偏 MRPT，>0.55 偏 MTFS |
| `pred_median` | 预测 VIX 均值（未来 10 交易日中位数均值） |
| `pred_q10` / `pred_q90` | 预测 P10 / P90 区间 |
| `current_vix` | context 末日 VIX |
| `change_pct` | (pred_median - current_vix) / current_vix |
| `direction` | `up` (>+3%) / `down` (<-3%) / `flat` |
| `mode` | `finetune-dual` / `zero-shot-cov` |
| `ensemble_method` | `weighted-dirAcc` / `fomc-rule(Ntd)` |
| `models.full` / `models.fomc` | 各子模型详细结果 |

**Checkpoint 位置：**
- `historical_runs/vix_chronos2/ft_ckpt_full/` — finetune-full checkpoint
- `historical_runs/vix_chronos2/ft_ckpt_fomc/` — finetune-fomc checkpoint
- `historical_runs/vix_chronos2/vix_forecast_cache.json` — 当日推理缓存

**零数据泄露设计：**
- context：`[-504:]` 历史数据（不含今天之后）
- VIX9D/VIX3M：past_covariates，OOS 段用最后值填充（未知）
- FOMC 特征：future_covariates，日历公告已知，无泄露
- 训练样本：全部在今天之前，预测窗口是明天起

**在 DailySignal 中启用：**

```bash
# DailySignal 默认不启用 VIXForecast；通过 RegimeDetector 初始化参数开启
# 在 DailySignal.py 中找到 RegimeDetector(use_vix_forecast=True, vix_forecast_finetune=True)
```

---

## 13. DailySignal.py — 每日信号生成

### 13.0 MacroStateStore 日更新

见 **Section 6.5**。每次运行前执行一次即可，WalkForward 和 DailySignal 共用同一次更新结果。

```bash
# 标准模式每日运行（MRPT + MTFS，regime 自动加权， VIX 预测 finetune 双模型）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both --vix-forecast --vix-forecast-finetune

# 不开启预测的每日运行（MRPT + MTFS，regime 自动加权）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both

# 单策略运行
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy mrpt
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy mtfs

# 指定总资本（默认从 inventory 中读取）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both --total-capital 1000000

# 手动 60/40 权重（跳过 regime 自动权重）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both --total-capital 1000000 --mrpt-weight 0.6

# 跳过 regime（等权 50/50）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both --skip-regime

# Dry run（不更新 inventory，只打印信号）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both --dry-run

# VIX 预测模式（Chronos-2 zero-shot，score > 0.65 或 < 0.35 时对 volatility score ±0.05 微调）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both --vix-forecast

# VIX 预测 finetune 双模型 ensemble（finetune-full + finetune-fomc，首次运行约多 2 分钟训练）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both --vix-forecast --vix-forecast-finetune

# 指定日期（回填历史信号）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both --date 2026-03-12
```

**Position Monitor 行为（Step 1）：**
- 对 `inventory_mrpt.json` / `inventory_mtfs.json` 中所有开仓记录，从 `open_date` 模拟至今（或 `--date` 指定日期），每日检测止损条件
- 使用开仓时记录的 `param_set` + `open_hedge_ratio`，与实盘参数完全一致
- MRPT 止损：波动率止损（spread vs mean±2.5σ）、价格止损（spread × 0.8/1.5）、时间止损（max_holding_period）、z-score 自然回归
- MTFS 止损：动量衰减/反转（exit_on_momentum_decay + SMA 穿越）、配对 PnL 止损（-3%）、波动率止损（价格比率）、时间止损
- MTFS 止盈：信号衰竭止盈（|ms| 衰减到入场值 30% 以下且盈利）、利润追踪止盈（利润从峰值回撤超过分级阈值且仍盈利）
- 输出：HOLD（继续持仓）/ CLOSE（自然平仓 / 止盈平仓）/ CLOSE_STOP（止损触发，含触发日期和原因）
- 每对模拟 Excel 保存至 `trading_signals/monitor_history/monitor_<strategy>_<pair>_<ts>.xlsx`
- 每次运行前自动将 inventory 备份到 `inventory_history/`（按 as_of 日期保留唯一快照）

**信号质量门控（Step 2 新信号生成）：**
- 财报黑名单（MRPT + MTFS）：财报日附近不开新仓（`BLACKOUT` action）
- VIX 宏观门控（MTFS）：VIX term slope 急变时暂停新开仓（`MACRO_VETO`）
- 持仓容量（MRPT + MTFS）：同时持仓 pair 数上限各 8 对，超出后不开新仓（`MACRO_VETO`）
- Ticker 集中度（MRPT + MTFS）：单 ticker 最多出现在 2 个持仓中（`MACRO_VETO`）
- 相关性过滤（MRPT）：60 日 daily return 相关性 < 0.16 拒绝开仓（dev toggle: 0.20 strict / 0.16 relaxed；`MACRO_VETO`，不适用 MTFS——跨行业动量分化设计）
- 亏损防重开（MRPT + MTFS）：Step 1 亏损关仓的 pair，Step 2 当天不重开（`MACRO_VETO`）
- 弱信号拦截（MTFS）：|momentum_spread| < 0.05 拒绝开仓（`MACRO_VETO`）
- 详见 README.md「信号质量门控」章节

输出：`trading_signals/mrpt_signals_<date>.json`，`trading_signals/mtfs_signals_<date>.json`，`trading_signals/combined_signals_<date>.json`，`trading_signals/daily_report_<date>.txt`，`trading_signals/monitor_history/monitor_*.xlsx`

---

## 14. WalkForwardDiagnostic.py — Walk-Forward 深度诊断

在 MRPT 和 MTFS Walk-Forward 都完成后运行，自动读取最新结果，生成多维 Excel 诊断报告。

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python WalkForwardDiagnostic.py
```

输出：`historical_runs/wf_diagnostic_<timestamp>.xlsx`，包含以下 sheet：

| Sheet | 内容 |
|-------|------|
| `Executive_Summary` | 宏观环境 IS→OOS 变化、各窗口 PnL/Sharpe/VIX/SPY、协整检验、Ticker 集中风险、问题配对综合结论 |
| `MRPT_Pairs` / `MTFS_Pairs` | 每个配对 × 7 窗口（IS + 6 OOS）的 Sharpe / MaxDD / 协整 p 值 / 相关系数 |
| `Regime_Comparison` | 每个 OOS 窗口的 VIX、SPY 回报、HY 利差、利率、失业率等宏观指标快照 |
| `Cross_Correlations` | IS vs OOS 跨品种相关矩阵对比，标注变化最大的 ticker 对 |
| `Cointegration` | 每个配对每窗口的协整 p 值，标注 IS 强但 OOS 丧失协整的风险配对 |
| `IS_OOS_Decay` | IS 最优 Sharpe → OOS 实际 Sharpe 的衰减比率；DSR 鲁棒性标签（Fragile / Moderate / Robust） |
| `DSR_Robustness` | 每个配对 × 窗口：31/32 个参数集中通过 DSR 的数量、Pass Rate、Selected 参数的 Sharpe/DSR |
| `OOS_PnL_Heatmap` | 配对 × 窗口 PnL 热图（宽表，直接从 portfolio xlsx 读取 `dod_pair_trade_pnl_history`） |
| `OOS_PnL_Detail` | 每个配对每窗口的 WinRate、N_Days_Active、N_Stops 明细 |
| `OOS_Curve_Comparison` | MRPT vs MTFS 每日 PnL 相关系数，评估双策略分散化效果 |
| `MRPT_Equity_Curve` / `MTFS_Equity_Curve` | 拼接全部窗口(去重)的逐日权益曲线 |

> 所有文件自动按 mtime 查找最新版本，无需指定日期或路径。

---

## 标准全流程（从头 Step1 → Step3）

```bash
# ── 换配对时才需要（Step 0）──
# 0a. 从数据库筛选配对并写入 pair_universe_*.json
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python SelectPairs.py --save
# 0b. 将新配对写入 Step1 config
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python UpdateStep1Configs.py

# ── 必须最先运行：更新宏观快照（见 Section 6.5）──
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MacroStateStore.py --update

# ── MRPT ──
# 1. Grid search
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step1_grid32.json
# 2. 用 Step1 输出的 strategy_summary_<ts>.csv 更新 Step2/3 config（必须在 Step2 前执行）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTUpdateConfigs.py historical_runs/strategy_summary_<ts>.csv
# 3. Backtest with best params
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step2_best_backtest.json
# 4. Forward validation
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMRPTStrategyRuns.py run_configs/runs_20260304_step3_forward.json

# ── MTFS ──
# 1. Grid search
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_step1_grid30.json
# 2. 用 Step1 输出的 mtfs_strategy_summary_<ts>.csv 更新 Step2/3 config（必须在 Step2 前执行）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSUpdateConfigs.py historical_runs/mtfs_strategy_summary_<ts>.csv
# 3. Backtest with best params
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_step2_best_backtest.json
# 4. Forward validation
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python PortfolioMTFSStrategyRuns.py run_configs/mtfs_runs_step3_forward.json
```

---

## 标准全流程（重新跑一次 Walk-Forward + 更新信号）

```bash
# ── 换配对时才需要（Step 0）──
# 0a. 从数据库筛选配对并写入 pair_universe_*.json
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python SelectPairs.py --save
# 0b. 将新配对写入 Step1 config
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python UpdateStep1Configs.py

# ── 必须最先运行：更新宏观快照（见 Section 6.5）──
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MacroStateStore.py --update

# 1. MRPT Walk-Forward
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTWalkForward.py --mode rolling --train-months 19 --oos-windows 9 --oos-window-days 50 --oos-overlap 10

# 2. MRPT Walk-Forward Report
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTWalkForwardReport.py

# 3. MTFS Walk-Forward
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSWalkForward.py --mode rolling --train-months 19 --oos-windows 9 --oos-window-days 50 --oos-overlap 10

# 4. MTFS Walk-Forward Report
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSWalkForwardReport.py

# 5. 每日信号
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both

# 6. Walk-Forward 深度诊断（两个 WalkForward 都跑完后运行）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python WalkForwardDiagnostic.py
# 输出：historical_runs/wf_diagnostic_<timestamp>.xlsx
```

---

## run_configs/ — 重要 Config 文件说明

### MRPT

| 文件 | 用途 | runs数 | 日期范围 |
|---|---|---|---|
| `runs_20260304_step1_grid32.json` | Step 1 Grid search：32个param_set × 15对 | 32 | 2024-01-02 ~ auto_minus_70d |
| `runs_20260304_step2_best_backtest.json` | Step 2 回测：每对最佳param_set（UpdateConfigs写入） | 1 | 2024-09-12 ~ auto_minus_30d |
| `runs_20260304_step3_forward.json` | Step 3 Forward：最近~70天验证（UpdateConfigs写入） | 1 | 2024-09-12 ~ auto |

### MTFS

| 文件 | 用途 | runs数 | 日期范围 |
|---|---|---|---|
| `mtfs_runs_step1_grid30.json` | Step 1 Grid search：31个param_set × 15对 | 31 | 2023-12-16 ~ auto_minus_70d |
| `mtfs_runs_step2_best_backtest.json` | Step 2 回测：每对最佳param_set（UpdateConfigs写入） | 1 | 2024-09-12 ~ auto_minus_70d |
| `mtfs_runs_step3_forward.json` | Step 3 Forward：最近~70天验证（UpdateConfigs写入） | 1 | 2024-09-12 ~ auto |

> **注意**：`step2` 和 `step3` 文件由 `UpdateConfigs.py` 自动覆盖写入，不要手动编辑 pairs/param_set 部分。

---

## historical_runs/ — 输出文件结构

### 文件名规律

**MRPT 回测 Excel**
```
portfolio_history_<label>_<param_set>_<YYYYMMDD_HHMMSS>.xlsx
```
- Step 1 示例：`portfolio_history_all15_default_default_20260312_145802.xlsx`
- Step 2 示例：`portfolio_history_step2_best_per_pair_default_20260312_150013.xlsx`
- Step 3 示例：`portfolio_history_step3_forward_default_20260312_150155.xlsx`

**MRPT 汇总 CSV**（UpdateConfigs 的输入）
```
strategy_summary_<YYYYMMDD_HHMMSS>.csv         ← Step 1 每次运行输出
grid_pair_breakdown_<YYYYMMDD_HHMMSS>.csv      ← Step 1 每对每param_set明细
```

**MRPT 综合报告 Excel**（GenerateReport 输出）
```
report_bt_vs_fwd_<YYYYMMDD_HHMMSS>.xlsx
```

---

**MTFS 回测 Excel**
```
portfolio_history_MTFS_<label>_<param_set>_<YYYYMMDD_HHMMSS>.xlsx
```
- Step 1 示例：`portfolio_history_MTFS_all15_default_default_20260312_180155.xlsx`
- Step 2 示例：`portfolio_history_MTFS_step2_best_per_pair_default_20260312_175938.xlsx`
- Step 3 示例：`portfolio_history_MTFS_step3_forward_default_20260312_180257.xlsx`

**MTFS 汇总 CSV**（UpdateConfigs 的输入）
```
mtfs_strategy_summary_<YYYYMMDD_HHMMSS>.csv    ← Step 1 每次运行输出
mtfs_grid_pair_breakdown_<YYYYMMDD_HHMMSS>.csv ← Step 1 每对每param_set明细
```

**MTFS 综合报告 Excel**（GenerateReport 输出）
```
mtfs_report_bt_vs_fwd_<YYYYMMDD_HHMMSS>.xlsx
```

---

### walk_forward/ 和 walk_forward_mtfs/ 结构

```
walk_forward/
├── walk_forward_summary_<ts>.json      ← WalkForward 主输出，DailySignal 读取此文件
├── dsr_selection_log_<ts>.csv          ← 每窗口 DSR 筛选明细
├── oos_equity_curve_<ts>.csv           ← OOS 逐日净值曲线（WalkForward 输出）
├── oos_equity_curve_<ts>.csv           ← OOS 逐日净值曲线（WalkForwardReport 输出，含所有窗口拼接）
├── oos_pair_summary_<ts>.csv           ← OOS 每对汇总（WalkForwardReport 输出）
├── oos_report_<ts>.txt                 ← OOS 文字报告
└── window<NN>_<train_start>_<oos_end>/ ← 每个OOS窗口目录
    ├── wf_window<NN>_<dates>           ← 窗口内grid search结果子目录
    ├── selected_pairs.json             ← 该窗口选出的 pair+param_set
    ├── historical_runs/                ← 该窗口内的回测 Excel
    ├── charts/                         ← 该窗口图表
    └── logs/                           ← 该窗口日志
```

> **DailySignal 读取规则**：自动找 `walk_forward_summary_*.json` 中 mtime 最新的文件，不按文件名排序。

---

## Corporate Actions（拆股/合股，MRPT/MTFS）

价格源（Polygon/yfinance）在 split 后全历史回溯调整，但 inventory 的 shares/open_price 是开仓口径——不处理会产生幻影巨亏并触发假止损（2026-06-12 KLAC 1:10 拆股事故：KLAC/REGN 出现 -118k 幻影亏损）。由 `CorporateActions.py` 处理：

- **DailySignal 每日自动**（Step 1 monitor 前）：Polygon 市场级 splits 日检（cache `price_data/splits_cache.json`）→ 命中持仓则调整 inventory（`sX_shares`×factor、`open_sX_price`÷factor、`open_hedge_ratio`、`open_price_level_stop` 同步换算；成本基数与 PnL 美元值不变）+ 备份 + `applied_corporate_actions` 留痕（polygon_id 幂等，重跑绝不二次调整）
- **日志分级**（`trading_signals/corporate_actions.log`，每次检查一行）：`NO-ACTION-NEEDED`（检查跑了、无 split）/ `ALREADY-APPLIED`（检测到但已应用，幂等跳过）/ `APPLIED`（实际调整）/ `NO-POSITIONS` / `ERROR`（**检查本身失败**——与"无 split"是两回事，看到 ERROR 必须排查）
- **Mongo 价格读取层**：`stock_data` 2025-05 起为 as-traded 追加（之前批量载入已调整，分界 `MONGO_AS_TRADED_SINCE`），PortfolioMRPTRun/PortfolioMTFSRun/PnLReport/UpdateStrategyPerformance 的 Mongo loader 读取时按 splits 回溯调整消除断崖
- **历史快照**：inventory_history 不重写；RiskManager/PnLReport/UpdateStrategyPerformance 读取时经 `adjust_position_view()` 换算（marker + open_date 判据，未来生效的 split 不会提前应用）
- 手动检查：`conda run -n someopark_run python CorporateActions.py --strategy both --dry-run`
- V1 仅 splits；spinoff/换股合并等检测到只告警不自动改仓

---

## 事件风险降险 overlay（半导体，MRPT/MTFS）

半导体崩盘保护（默认 off）。触发：`SMH 30d β-vs-SPY > 2.5` 且 2 交易日内有 NFP；或 NVDA/AVGO 财报反应日收盘 < −4.5%。命中 → T+1 关一半"做多腿含半导体"的 pair + veto 半导体做多腿新开（`MACRO_VETO`）；SMH 当日<−3% 提前解 veto，最迟 T+3。

```bash
# 启用(默认 off):.env 加
SEMI_EVENT_DERISK_ENABLED=1

# 数据维护(conductor 每日非致命早步会自动跑;手动:)
set -a && source .env && set +a
conda run -n qlib_run --no-capture-output python RefreshEventRiskData.py   # 刷 event_risk 价库+NFP+bellwether 日历+死票哨兵
conda run -n qlib_run --no-capture-output python FetchBellwetherEarnings.py # 仅 NVDA/AVGO 前向财报日历

# 安全测试(零生产污染:读真实输入,输出全进一次性沙盒,dry_run 跳过所有生产写入)
conda run -n someopark_run --no-capture-output python RunDailySignalSandbox.py mtfs 2026-06-04   # / mrpt
```

- 每日留痕(不触发也写一行):`trading_signals/event_risk_heartbeat.log`;日志 `[SEMI_EVENT]` / `[SEMI_EVENT_VETO]`
- 核心模块:`EventRiskDetector.py`(两策略共享);状态:`pipeline_state/semi_event_veto.json`
- AISS 侧(qlib_run)见 `qlib-main/semiconductor_strategy/RUNBOOK.md`

---

## trading_signals/ — 每日信号文件结构

### 文件名规律

```
mrpt_signals_<YYYYMMDD>.json          ← MRPT 当日信号
mtfs_signals_<YYYYMMDD>.json          ← MTFS 当日信号
combined_signals_<YYYYMMDD>.json      ← 合并信号（含 regime 权重）
daily_report_<YYYYMMDD>.json          ← 完整报告（JSON）
daily_report_<YYYYMMDD>.txt           ← 完整报告（人可读文本）
```

### 文件内容结构

**`mrpt_signals_<date>.json` / `mtfs_signals_<date>.json`**
```json
{
  "strategy": "mrpt",
  "signal_date": "2026-03-13",
  "capital": 548000,
  "sim_capital": 500000,
  "scale_factor": 1.096,
  "regime": { "score": 42.0, "label": "neutral" },
  "signals": [
    { "pair": "DG/MOS", "action": "OPEN_LONG", "z_score": -3.70,
      "s1_shares": 1005, "s2_shares": -5208, "s1_price": 135.64, "s2_price": 31.21 },
    ...
  ]
}
```

**`combined_signals_<date>.json`**
```json
{
  "mode": "both",
  "signal_date": "2026-03-13",
  "total_capital": 1000000,
  "regime": { "score": 42.0, "mrpt_weight": 0.55, "mtfs_weight": 0.45 },
  "position_monitor": [...],
  "mrpt": { ... },
  "mtfs": { ... }
}
```

**`daily_report_<date>.json`**
```json
{
  "report_type": "combined",
  "signal_date": "2026-03-13",
  "total_capital": 1000000,
  "regime": { ... },
  "position_monitor": [...],
  "portfolio": { ... },
  "mrpt": { ... },
  "mtfs": { ... }
}
```

---

## 核心配置文件（手动维护）

### pair_universe_mrpt.json / pair_universe_mtfs.json — 交易配对唯一来源

所有脚本通过 `pair_universe.py`（内部加载器模块，不直接运行）读取，修改后无需改动任何代码。

```
pair_universe_mrpt.json   — MRPT 15对：s1=均值回归多腿，s2=空腿
pair_universe_mtfs.json   — MTFS 15对：s1=动量强腿（做多），s2=动量弱腿（做空）
                            注意：MTFS 的 s1/s2 顺序与 MRPT 相反（s1 = 近期涨幅更高）
pair_universe_mrpt_backup.json / pair_universe_mtfs_backup.json — SelectPairs --save 时自动备份
```

**推荐更新方式（通过 SelectPairs.py）：**
```bash
# 预览
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python SelectPairs.py
# 写入
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python SelectPairs.py --save
```

**字段说明：**

| 字段 | 说明 |
|---|---|
| `s1` | 第一腿 ticker |
| `s2` | 第二腿 ticker |
| `sector` | 所属板块（`tech` / `finance` / `industrial` / `energy` / `food`） |
| `z_col` | （MRPT）Z-score 列名，格式 `Z_<sector>` |
| `spread_col` | （MTFS）动量差列名，格式 `Momentum_Spread_<sector>` |

**修改配对后必须执行：**
```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python UpdateStep1Configs.py
```
然后重新跑 Step1 grid search 或 WalkForward。

---

### inventory_mrpt.json / inventory_mtfs.json — 当前持仓状态

**由 `DailySignal.py` 自动维护，不要手动编辑 pairs 内容。** 以下字段可在必要时手动调整：

```
inventory_mrpt.json   — MRPT 当前开仓记录
inventory_mtfs.json   — MTFS 当前开仓记录
```

**字段说明：**

| 字段 | 说明 |
|---|---|
| `as_of` | 最后更新日期（DailySignal 写入） |
| `capital` | 该策略分配资本（DailySignal 按 regime 权重计算后写入） |
| `pairs.<key>.direction` | 持仓方向：`"long"` / `"short"` / `null`（无仓位） |
| `pairs.<key>.s1_shares` | S1 持仓股数（正=多，负=空） |
| `pairs.<key>.s2_shares` | S2 持仓股数 |
| `pairs.<key>.open_date` | 开仓日期 |
| `pairs.<key>.open_s1_price` | 开仓时 S1 价格（用于计算未实现 PnL） |
| `pairs.<key>.open_s2_price` | 开仓时 S2 价格 |
| `pairs.<key>.days_held` | 已持仓日历天数（每日 idempotent 递增） |
| `pairs.<key>.last_updated` | 最后更新日期（防止重复计数） |
| `pairs.<key>.param_set` | 该仓位使用的参数组（Position Monitor 用此参数跑模拟） |
| `pairs.<key>.open_hedge_ratio` | 开仓时的对冲比率（MRPT: Kalman ratio；MTFS: dollar ratio） |
| `pairs.<key>.open_signal` | 开仓时的信号值（MRPT: z_score；MTFS: momentum_spread） |
| `pairs.<key>.wf_source` | 来源 Walk-Forward 文件（`walk_forward_summary_*.json`） |
| `pairs.<key>.open_price_level_stop` | 开仓时的价格止损水位（MRPT 专属，null 表示未设置） |
| `pairs.<key>.peak_unrealized_pnl` | 持仓期间最高未实现盈利（MTFS 专属，Trailing Profit Stop 用此追踪利润峰值） |
| `pairs.<key>.monitor_log` | 最近一次 Position Monitor 输出摘要（action / days_held / upnl） |

> **注意**：`days_held` 基于日历天数，每天只递增一次（通过 `last_updated` 保证 re-run 幂等）。持仓期间 shares 固定不变，不随 regime 调整。inventory 每次运行前自动备份至 `inventory_history/`，按 `as_of` 日期去重保留唯一快照。

---

## Strategy Performance 前端数据更新

### 数据源

`someo-park-investment-management/public/data/strategy_performance.json` — 静态文件，Firebase Hosting 部署后前端直接读取。

### 更新脚本

```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output \
  python UpdateStrategyPerformance.py --start YYYY-MM-DD --end YYYY-MM-DD
```

- `--start`：从这一天开始重算（含）
- `--end`：截止日期（含），通常是最新有 PnL report 的交易日
- `--dry-run`：只打印结果，不写文件
- `--daily-weights`：用每日实际 regime 权重（默认 fixed，用 end 日期最新权重）

### 注意：capital base 拼接问题

`UpdateStrategyPerformance` 每次都从 `SIM_CAPITAL = $500,000` 起算 sim equity，再按 regime 权重缩放为 real equity。**如果只补最近几天**，新段的 real equity 起点（= regime_capital × 1.0）会和历史数据的 equity 不连续，造成虚假暴跌。

**正确做法**：每次补数据时，`--start` 必须从上一次数据的连续起点往回拉足够长，让 realized PnL 从头累积。

例如，当前数据到 4/21，补 4/22～4/30，应该用：
```bash
python UpdateStrategyPerformance.py --start 2026-03-19 --end 2026-04-30
```
而不是 `--start 2026-04-22`。起点选 `strategy_performance.json` 中当前连续段的第一天即可（通常是上次完整重跑的起始日）。

### 更新后需要重新 build + deploy

```bash
cd someo-park-investment-management
npm run build && firebase deploy --only hosting
```

> `VITE_*` 变量和 `public/data/` 静态文件都是构建时/部署时注入的，改了必须重新 deploy。

## 附注：2026-07-03 交易路径修复分界（MONITOR/RISK_DEFENSE/COOLING 三计划）

7/3-7/5 休市窗口内上线，详情见 `.claude/plan/strategies-plan/EXECUTION_LOG_RISK_BATCH.md`：

| 分界 | 内容 |
|------|------|
| **2026-07-03（口径）** | monitor upnl 去掉 ×当日 regime scale（此前 ±11% 失真）；历史 monitor_log 不回改 |
| **2026-07-06（数据）** | Mongo loader 含当日 bar：signal_date=T 起用 T 收盘（此前永远 T-1）。WF OOS 统计跨此分界不可直接对比 |
| **2026-07-03（语义）** | 模拟空仓不再直接算退出——显式评估退出规则，不成立打 `[MONITOR_GUARD]` HOLD；开仓 bar==最后 bar 直接 HOLD |
| **新 veto** | Cross-day cooling（盈 1td / 其他 3td）+ 组合熔断（影子期 `_CB_SHADOW` 至约 7/13，确认无误报后置 False）|
| **观察项** | 每日 `[MONITOR_GUARD]`（频率应≈每日 1 次且递减）、`[CIRCUIT_BREAKER][SHADOW]`（would-close 合理性）、`[COOLING]` 首例 |

## 附注:MTFS fast-confirm 竞争变体(2026-07-12 上线)

- `PARAM_SETS` 35 个(31 母 + 4 fc 变体);`grid30.json` 同步 35 个 run 条目
  (文件名 grid30 为历史命名)。fc 变体自 **2026-07-13(周一)WF** 起参赛,
  DSR 选中才进生产。
- **回滚规程(重要)**:只从 grid30.json 移除 run 条目;`PARAM_SETS` 里的
  set 定义**必须保留**,直至下次 WF 的 selected_pairs 不再引用它且所有 fc
  持仓已平——extract_signals 的 `_resolve_param_set` 对未知 set 名直接 raise。
- 验收记录:基线逐 bit 对比 35 sheets 零差异;fc 冒烟 40 次拦截语义正确;
  load_config 解析 35 runs ✓。详见 `.claude/plan/systemic strategies/` 的 plan。
