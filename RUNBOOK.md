# Someopark Run Commands Manual

所有命令必须在 `/Users/xuling/code/someopark-test/` 目录下运行。
所有命令必须先加载 `.env`（含 POLYGON_API_KEY、FRED_API_KEY），并使用 `someopark_run` conda 环境。

**通用前缀（每条命令都要带）：**
```bash
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python <脚本> <参数>
```

---

## 1. PortfolioMRPTStrategyRuns.py — MRPT 批量回测

接收一个 JSON config 文件作为参数。**Step 1 跑完后必须先运行 MRPTUpdateConfigs.py，才能运行 Step 2。**

```bash
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

```bash
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

```bash
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

## 13. DailySignal.py — 每日信号生成

```bash
# 标准每日运行（MRPT + MTFS，regime 自动加权）
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

# 指定日期（回填历史信号）
set -a && source .env && set +a && conda run -n someopark_run --no-capture-output python DailySignal.py --strategy both --date 2026-03-12
```

输出：`trading_signals/mrpt_signals_<date>.json`，`trading_signals/mtfs_signals_<date>.json`，`trading_signals/combined_signals_<date>.json`，`trading_signals/daily_report_<date>.txt`

---

## 标准全流程（从头 Step1 → Step3）

```bash
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