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

## 7. MRPTWalkForward.py — MRPT Walk-Forward 6窗口

**换配对后需先运行 `UpdateStep1Configs.py`（WalkForward 内部读 `runs_20260304_step1_grid32.json` 的 param_set + pairs 做 grid search）。**

```bash
# 换配对时才需要（更新 runs_20260304_step1_grid32.json 的 pairs）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python UpdateStep1Configs.py

# 标准运行（6个OOS窗口，expanding模式，输出到 historical_runs/walk_forward/）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTWalkForward.py --oos-windows 6

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

## 8. MTFSWalkForward.py — MTFS Walk-Forward 6窗口

**换配对后需先运行 `UpdateStep1Configs.py`（WalkForward 内部读 `mtfs_runs_step1_grid30.json` 的 param_set + pairs 做 grid search）。**

```bash
# 换配对时才需要（更新 mtfs_runs_step1_grid30.json 的 pairs）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python UpdateStep1Configs.py

# 标准运行（6个OOS窗口，expanding模式，输出到 historical_runs/walk_forward_mtfs/）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSWalkForward.py --oos-windows 6

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

**Position Monitor 行为：**
- 对 `inventory_mrpt.json` / `inventory_mtfs.json` 中所有开仓记录，从 `open_date` 模拟至今（或 `--date` 指定日期），每日检测止损条件
- 使用开仓时记录的 `param_set` + `open_hedge_ratio`，与实盘参数完全一致
- MRPT 止损：波动率止损（spread vs mean±2.5σ）、价格止损（spread × 0.8/1.5）、时间止损（max_holding_period）、z-score 自然回归
- MTFS 止损：动量衰减/反转（exit_on_momentum_decay + SMA 穿越）、配对 PnL 止损（-3%）、波动率止损（价格比率）、时间止损
- 输出：HOLD（继续持仓）/ CLOSE（自然平仓）/ CLOSE_STOP（止损触发，含触发日期和原因）
- 每对模拟 Excel 保存至 `trading_signals/monitor_history/monitor_<strategy>_<pair>_<ts>.xlsx`
- 每次运行前自动将 inventory 备份到 `inventory_history/`（按 as_of 日期保留唯一快照）

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
| `MRPT_Equity_Curve` / `MTFS_Equity_Curve` | 拼接 6 窗口的逐日权益曲线 |

> 所有文件自动按 mtime 查找最新版本，无需指定日期或路径。

---

## 标准全流程（从头 Step1 → Step3）

```bash
# ── 换配对时才需要（Step 0）──
# 0a. 从数据库筛选配对并写入 pair_universe_*.json
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python SelectPairs.py --save
# 0b. 将新配对写入 Step1 config
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python UpdateStep1Configs.py

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

# 1. MRPT Walk-Forward
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTWalkForward.py --oos-windows 6

# 2. MRPT Walk-Forward Report
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MRPTWalkForwardReport.py

# 3. MTFS Walk-Forward
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python MTFSWalkForward.py --oos-windows 6

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
| `pairs.<key>.monitor_log` | 最近一次 Position Monitor 输出摘要（action / days_held / upnl） |

> **注意**：`days_held` 基于日历天数，每天只递增一次（通过 `last_updated` 保证 re-run 幂等）。持仓期间 shares 固定不变，不随 regime 调整。inventory 每次运行前自动备份至 `inventory_history/`，按 `as_of` 日期去重保留唯一快照。