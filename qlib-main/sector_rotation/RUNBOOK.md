# Sector Rotation Strategy — Runbook

所有命令必须在 `/Users/xuling/code/someopark-test/` 目录下运行。

**Pipeline 控制脚本（推荐）：**
```bash
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh [MODE] [OPTIONS]
```

**通用前缀（直接调用 Python 时）：**
```bash
set -a && source .env && set +a && conda run -n qlib_run --no-capture-output python <脚本>
```

> ## 隔离原则（必须遵守）
>
> **板块轮动策略只使用 `qlib_run` conda 环境，绝不调用 `someopark_run`。**
>
> | | someopark 主 pipeline | sector_rotation |
> |---|---|---|
> | Conda 环境 | `someopark_run` | `qlib_run` |
> | Pipeline 脚本 | `pre_pipeline.sh` | `sector_rotation_pipeline.sh` |
> | 状态文件 | `pipeline_state/` (root) | `sector_rotation/pipeline_state/` |
> | Inventory | `inventory_mrpt.json` etc. | `inventory_sector_rotation.json` |
> | 信号输出 | `trading_signals/` (root) | `sector_rotation/trading_signals/` |
>
> **MacroStateStore（`price_data/macro/`）**：由 someopark 主 pipeline 负责写入和更新。
> 板块轮动只**读取**这些 parquets，不写入、不更新，不调用 `MacroStateStore.py`。
> 若 macro 数据陈旧，regime signal 自动降级为仅用 yfinance VIX（不报错）。

---

## 快速参考

| 场景 | 命令 |
|------|------|
| 每日信号（标准） | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily` |
| 看当前持仓/信号 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh status` |
| 不写 inventory 测试 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run` |
| 每周 EPS 维护 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh weekly` |
| 月末强制再平衡 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh monthly` |
| 首次 EPS 全量获取 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-full` |
| 运行历史回测 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh backtest` |
| 参数敏感性扫描 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh sensitivity` |
| Regime 历史分析 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh regime` |
| 生成 PDF 报告 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh tearsheet` |
| 运行测试套件 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh test` |
| 查看原始 Z-score | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh signal-raw` |

---

## 一、首次初始化（新机器 / 数据重置）

**完整初始化流程（按顺序执行）：**

```bash
# 0. 确认 conda 环境和 .env 正常
conda run -n qlib_run --no-capture-output python -c "import qlib; print('qlib OK')"
set -a && source .env && set +a && echo "POLYGON_API_KEY=${POLYGON_API_KEY:0:8}..."

# 1. 首次全量 EPS 历史获取（约 5 分钟，55 个股票 × Polygon API 分页）
#    输出: price_data/sector_etfs/eps_history.json（~3100+ 季度，2009→今）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-full

# 2. 验证 EPS 数据
set -a && source .env && set +a && conda run -n qlib_run --no-capture-output python -c "
import json
with open('price_data/sector_etfs/eps_history.json') as f: d = json.load(f)
print(f'Symbols: {len(d[\"symbols\"])}')
print(f'Quarters: {sum(len(v) for v in d[\"symbols\"].values())}')
print(f'Fetched: {d[\"fetched_at\"]}')
"

# 3. 首次 dry-run 验证（不写 inventory，确认全链路正常）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run

# 4. 正式首日运行（写入 inventory）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily
```

---

## 二、每日操作（Daily Operations）

**标准每日流程（由 pipeline 脚本自动处理）：**

```bash
# 标准运行（NYSE 休市自动跳过，EPS 自动检查刷新，polygon value source）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily

# 等效的直接 Python 调用（不含 holiday/EPS/macro 自动检查）
set -a && source .env && set +a && conda run -n qlib_run --no-capture-output \
    python qlib-main/sector_rotation/SectorRotationDailySignal.py \
    --value-source polygon

# 指定资本（覆盖 inventory 中的值）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily --capital 1500000

# 指定 value source（proxy 最快，无需 EPS 数据；polygon 最准确）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily --value-source proxy
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily --value-source polygon
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily --value-source constituents

# 回填历史日期（不写 inventory，仅输出报告）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily \
    --date 2026-04-01 --skip-holiday

# Dry-run（任何时候安全运行，不修改 inventory）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run

# 跳过节假日检查（适用于回填 / 测试）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily --skip-holiday

# 不更新 MacroStateStore（加速，若主 pipeline 已更新）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily --no-macro-update
```

**每日输出文件：**

```
qlib-main/sector_rotation/trading_signals/
├── sr_daily_report_<YYYYMMDD>_<ts>.json   完整报告（含 regime、rebalance、signals、costs）
└── sr_daily_report_<YYYYMMDD>_<ts>.txt    人可读文本摘要

qlib-main/sector_rotation/inventory_sector_rotation.json   当前持仓状态
qlib-main/sector_rotation/inventory_history/               每次运行的快照备份
```

---

## 三、每周维护（Weekly Maintenance）

**推荐：每周日自动运行（cron 或手动触发）。**

```bash
# 标准每周维护（EPS 增量更新 + dry-run 验证）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh weekly

# 手动 EPS 增量更新（仅更新 last_fetched 超过 7 天的 symbol）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-update

# 强制全量 EPS 更新（用于修复数据或超长缺口后）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-update --force
# 等价于:
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-full

# 只更新特定 symbol（盈利季节针对性更新）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-symbols XOM CVX COP SLB

# 验证 EPS store 状态
conda run -n qlib_run --no-capture-output python -c "
import json
from pathlib import Path
store = json.load(open('price_data/sector_etfs/eps_history.json'))
meta = store.get('symbol_meta', {})
stale = {k:v for k,v in meta.items() if not v.get('last_fetched')}
print(f'Total symbols: {len(store[\"symbols\"])}')
print(f'Total quarters: {sum(len(v) for v in store[\"symbols\"].values())}')
print(f'Store fetched_at: {store.get(\"fetched_at\")}')
print(f'Missing meta: {len(stale)} symbols')
import datetime
today = datetime.date.today()
def days_old(sym):
    lf = meta.get(sym, {}).get('last_fetched', '')
    return (today - datetime.date.fromisoformat(lf)).days if lf else 999
oldest = sorted(meta.keys(), key=days_old, reverse=True)[:5]
print('Most stale:', [(s, days_old(s)) for s in oldest])
"
```

**EPS store 格式参考：**
```json
{
  "fetched_at": "2026-04-24",
  "symbol_meta": {
    "XOM": { "last_fetched": "2026-04-24", "newest_end_date": "2025-12-31" }
  },
  "symbols": {
    "XOM": [
      { "end_date": "2009-03-31", "eps": 0.43 },
      ...
    ]
  }
}
```

---

## 四、月度再平衡（Monthly Rebalance）

**再平衡触发条件（由 DailySignal.py 自动判断）：**
- 月首交易日（`rebalance_day: "first_trading_day"` in config.yaml）
- VIX 紧急 de-risk（`emergency_derisk_vix: 35.0`）
- `--force-rebalance` 标志

```bash
# 月首交易日（DailySignal 自动触发，daily 模式即可）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily

# 手动强制触发再平衡（如错过自动触发）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh monthly

# 月度模式含 EPS 更新（确保 value signal 最新）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh monthly --capital 1000000

# 直接 Python 调用（force-rebalance）
set -a && source .env && set +a && conda run -n qlib_run --no-capture-output \
    python qlib-main/sector_rotation/SectorRotationDailySignal.py \
    --value-source polygon --force-rebalance
```

**再平衡参数（config.yaml rebalance 节）：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `rebalance.frequency` | `monthly` | 再平衡频率 |
| `rebalance.rebalance_day` | `first_trading_day` | 月首还是月末交易日 |
| `rebalance.zscore_change_threshold` | `0.5` | Z-score 变化低于此值 → 不再平衡该 sector |
| `rebalance.emergency_derisk_vix` | `35.0` | VIX 超过此值 → 紧急 de-risk（移至 50% 现金）|
| `rebalance.emergency_cash_pct` | `0.50` | 紧急 de-risk 现金比例 |

---

## 五、信号研究与调试（Research & Debugging）

```bash
# 查看当前持仓和最新信号摘要
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh status

# 查看原始复合 Z-score（11 个 sector ETF，不经过 optimizer）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh signal-raw

# 测试不同 value source 效果（dry-run 不写 inventory）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run --value-source proxy
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run --value-source polygon
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run --value-source constituents

# Python API：获取最新 composite signals
set -a && source .env && set +a && conda run -n qlib_run --no-capture-output python -c "
import sys, json
sys.path.insert(0, 'qlib-main')
from sector_rotation.signals.composite import get_current_signals
result = get_current_signals()
print(json.dumps(result, indent=2, default=str))
"

# Python API：只看 regime
set -a && source .env && set +a && conda run -n qlib_run --no-capture-output python -c "
import sys
sys.path.insert(0, 'qlib-main')
from sector_rotation.data.loader import load_all
from sector_rotation.signals.regime import compute_regime, regime_to_monthly
prices, macro = load_all()
regime_daily = compute_regime(macro, method='rules')
print(regime_daily.tail(20).to_string())
"

# 查看 momentum signals
set -a && source .env && set +a && conda run -n qlib_run --no-capture-output python -c "
import sys
sys.path.insert(0, 'qlib-main')
from sector_rotation.data.loader import load_all
from sector_rotation.data.universe import get_tickers
from sector_rotation.signals.momentum import compute_all_momentum
prices, macro = load_all()
etfs = get_tickers(include_benchmark=False)
mom = compute_all_momentum(prices[[t for t in etfs if t in prices.columns]])
print('CS Momentum (last 3 months):')
print(mom['cs_mom'].tail(3).round(3).to_string())
print()
print('TS Multiplier (last 3 months):')
print(mom['ts_mult'].tail(3).round(2).to_string())
"
```

---

## 六、历史回测与研究分析（Historical Backtest & Research）

```bash
# 标准全量回测（config.yaml backtest 节参数，2018-07-01 → 最新）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh backtest

# 参数敏感性扫描（top_n_sectors 等关键参数的 Sharpe/MaxDD 影响）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh sensitivity

# Regime 分析报告（4 状态标签历史 + 最近 30 天 + 月度汇总）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh regime

# 生成 PDF tearsheet（自动运行回测 + 生成报告）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh tearsheet

# Robustness 分析（bootstrap Sharpe + subperiod analysis）
# robustness.py 是库模块，其 __main__ 使用合成数据做 smoke test；
# 实际使用：在运行 backtest 后通过 Python 调用 bootstrap_sharpe / subperiod_analysis
set -a && source .env && set +a && conda run -n qlib_run --no-capture-output python -c "
import sys, pathlib
sys.path.insert(0, 'qlib-main')
from sector_rotation.data.loader import load_all, load_config
from sector_rotation.backtest.engine import SectorRotationBacktest
from sector_rotation.backtest.robustness import bootstrap_sharpe, subperiod_analysis

cfg = load_config()
prices, macro = load_all(config=cfg)
bt = SectorRotationBacktest(cfg)
result = bt.run(prices, macro)
returns = result.portfolio_returns   # adjust to your result attribute name
bench = result.benchmark_returns

bs = bootstrap_sharpe(returns, n_bootstrap=500)
sp = subperiod_analysis(returns, bench)
print(sp[['cagr', 'sharpe', 'max_dd']].round(3))
"

# Python 直接调用 engine
set -a && source .env && set +a && conda run -n qlib_run --no-capture-output \
    python qlib-main/sector_rotation/backtest/engine.py

# 定制参数回测
set -a && source .env && set +a && conda run -n qlib_run --no-capture-output python -c "
import sys, json
sys.path.insert(0, 'qlib-main')
from sector_rotation.data.loader import load_all, load_config
from sector_rotation.backtest.engine import SectorRotationBacktest

cfg = load_config()
# 覆盖参数
cfg['backtest']['start_date'] = '2020-01-01'
cfg['portfolio']['top_n_sectors'] = 5
prices, macro = load_all(config=cfg)
bt = SectorRotationBacktest(cfg)
result = bt.run(prices, macro)
print(result.summary())
"
```

**回测参数参考（config.yaml backtest 节）：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `backtest.start_date` | `2018-07-01` | 最早起始日（不能早于 XLC 上市日） |
| `backtest.end_date` | `null` | null = 最新可用数据 |
| `backtest.initial_capital` | `1,000,000` | 初始资本 |
| `portfolio.top_n_sectors` | `4` | 做多的 sector 数量 |
| `portfolio.optimizer` | `inv_vol` | 权重方法：inv_vol / risk_parity / gmv / equal_weight |
| `portfolio.constraints.max_weight` | `0.40` | 单个 sector 最大权重 |

> ⚠️ **GICS 结构断裂警告**：回测必须从 **2018-07-01** 或之后开始。
> XLC（通信服务）于 2018-06-18 从 XLK/XLY 拆分创建，早于此日期的跨板块动量信号无效。

---

## 七、测试（Testing）

```bash
# 运行完整测试套件（95 tests，无需网络，合成数据，约 30 秒）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh test

# 等效直接调用
conda run -n qlib_run --no-capture-output \
    python -m pytest qlib-main/sector_rotation/tests/ -v --tb=short

# 运行单个测试文件
conda run -n qlib_run --no-capture-output \
    python -m pytest qlib-main/sector_rotation/tests/test_signals.py -v

# 带 coverage 报告
conda run -n qlib_run --no-capture-output \
    python -m pytest qlib-main/sector_rotation/tests/ -v \
    --cov=qlib-main/sector_rotation --cov-report=term-missing

# 只跑快速测试（跳过 smoke test）
conda run -n qlib_run --no-capture-output \
    python -m pytest qlib-main/sector_rotation/tests/ -v \
    --ignore=qlib-main/sector_rotation/tests/test_engine_smoke.py
```

**测试文件说明：**

| 文件 | 内容 | 网络 |
|------|------|------|
| `test_signals.py` | 信号计算单元测试（momentum、value、regime、composite） | ✗ |
| `test_optimizer.py` | 组合优化 + 风险控制单元测试 | ✗ |
| `test_backtest.py` | 交易成本 + 指标单元测试 | ✗ |
| `test_engine_smoke.py` | 端到端 smoke test（合成价格数据） | ✗ |

---

## 八、Cron 自动化设置

### 完整 crontab 配置

```bash
# 编辑 crontab
crontab -e
```

```cron
# ──────────────────────────────────────────────────────────────────────────────
# SECTOR ROTATION — 自动化调度
# 所有时间为 UTC。NYSE: UTC-5 (EST) / UTC-4 (EDT)
# ──────────────────────────────────────────────────────────────────────────────

# 每日信号：周一至周五 17:15 ET after close (21:15 UTC winter / 21:15 UTC summer)
# 自动跳过 NYSE 节假日
15 21 * * 1-5   cd /Users/xuling/code/someopark-test && \
    bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily \
    >> qlib-main/sector_rotation/logs/cron_daily.log 2>&1

# 每周 EPS 维护：周日 06:00 UTC (01:00 ET / 02:00 EDT)
# 增量更新 55 个股票的 EPS + 验证 dry-run
0 6 * * 0   cd /Users/xuling/code/someopark-test && \
    bash qlib-main/sector_rotation/sector_rotation_pipeline.sh weekly \
    >> qlib-main/sector_rotation/logs/cron_weekly.log 2>&1
```

> **注意**：月首交易日的 rebalance 由 `daily` 模式自动检测触发，无需单独 cron。
> 如需保证月首一定执行，可在月初手动运行 `monthly` 模式作为 fallback。

### 验证 cron 正在运行

```bash
# 查看最近 cron 日志
tail -50 qlib-main/sector_rotation/logs/cron_daily.log

# 查看当天 daily 日志
tail -100 qlib-main/sector_rotation/logs/sr_daily_$(date +%Y%m%d).log

# 查看最近 status（每次运行更新）
cat qlib-main/sector_rotation/pipeline_state/sr_status_daily
```

---

## 九、config.yaml 关键参数速查

**位置：** `qlib-main/sector_rotation/config.yaml`

```bash
# 查看当前配置
cat qlib-main/sector_rotation/config.yaml
```

### 最常用调整项

```yaml
# 信号权重（必须总和 = 1.0）
signals:
  weights:
    cross_sectional_momentum: 0.40  # 12-1月动量
    ts_momentum:              0.15  # 时序动量（crash filter）
    relative_value:           0.20  # P/E 相对价值
    regime_adjustment:        0.25  # regime 条件调整

# Value signal 数据源
  value_source: "constituents"  # 改为 "polygon" 获得最佳覆盖率

# 组合参数
portfolio:
  top_n_sectors: 4           # 做多 4 个 sector
  optimizer: "inv_vol"       # inv_vol / risk_parity / gmv / equal_weight
  constraints:
    max_weight: 0.40         # 单个 sector 最大 40%

# Regime 阈值
signals:
  regime:
    vix_high_threshold: 25.0     # VIX > 25 → risk-off
    vix_extreme_threshold: 35.0  # VIX > 35 → emergency de-risk
```

---

## 十、文件结构参考

```
qlib-main/sector_rotation/
├── RUNBOOK.md                          本文件
├── sector_rotation_pipeline.sh         Master pipeline 控制脚本
├── SectorRotationDailySignal.py        每日信号生成器（主脚本）
├── update_eps_history.py               EPS 历史增量维护（每周/每日）
├── config.yaml                         所有策略参数
├── inventory_sector_rotation.json      当前持仓（DailySignal 维护）
│
├── signals/
│   ├── composite.py                    多因子复合信号聚合 + regime 调节
│   ├── momentum.py                     CS 动量（12-1m）+ TS crash filter + 加速
│   ├── value.py                        P/E 分位数相对价值信号
│   └── regime.py                       4 状态 regime 检测（rules-based + HMM 选项）
│
├── portfolio/
│   ├── optimizer.py                    inv-vol / risk-parity / GMV + Ledoit-Wolf 协方差
│   ├── risk.py                         vol scaling + VIX 紧急 + 回撤断路 + beta
│   └── rebalance.py                    月度计划 + threshold filter + turnover cap
│
├── backtest/
│   ├── engine.py                       事件驱动月度回测 + walk-forward
│   ├── costs.py                        点差 + impact 成本模型（按 ETF 流动性分层）
│   ├── metrics.py                      Sharpe/Calmar/IR/CVaR/Brinson 归因
│   ├── robustness.py                   参数敏感性 + bootstrap 置信区间
│   └── sensitivity.py                  参数扫描分析
│
├── data/
│   ├── loader.py                       价格（yfinance 缓存）+ FRED 宏观数据加载
│   └── universe.py                     ETF universe + GICS 元数据 + 流动性分层
│
├── report/
│   ├── plots.py                        所有 matplotlib 可视化函数
│   └── tearsheet.py                    多页 PDF tearsheet 生成器
│
├── tests/                              pytest 套件（95 tests，无网络）
│
├── trading_signals/                    每日输出（gittracked）
│   └── sr_daily_report_<date>_<ts>.json/txt
│
├── inventory_history/                  inventory 历史快照（gitignored）
├── logs/                               运行日志（gitignored）
└── pipeline_state/                     pipeline 状态文件（gitignored）
```

**外部依赖文件（不在 sector_rotation/ 目录）：**

```
price_data/sector_etfs/
├── eps_history.json                    EPS 历史存储（update_eps_history.py 维护）
└── prices_yfinance_*.pkl               ETF 价格缓存（loader.py 维护，首次自动下载）

price_data/macro/                       宏观数据 parquets（MacroStateStore.py 维护）
```

---

## 十一、故障排查（Troubleshooting）

### 常见问题

**Q: `daily` 跑了但没有 rebalance**

DailySignal 每月才再平衡（first_trading_day）。检查：
```bash
# 查看 inventory 的 rebalance_history
python3 -c "
import json
inv = json.load(open('qlib-main/sector_rotation/inventory_sector_rotation.json'))
for rb in inv.get('rebalance_history', [])[-3:]:
    print(rb['date'], rb['reason'], rb['regime'])
"
```
若需强制触发：
```bash
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily --force-rebalance
```

---

**Q: value signal 全部为 0.0 / P/E 覆盖率低**

检查 EPS store 状态：
```bash
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh status
```
若 EPS store 不存在：
```bash
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-full
```
若 `value_source: constituents`（yfinance，仅最近 4-8 季度），切换到 `polygon`：
```bash
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily --value-source polygon
```
或修改 `config.yaml`：`value_source: "polygon"`

---

**Q: regime 始终是 RISK_ON / MacroStateStore 数据陈旧**

`price_data/macro/` parquets 由 someopark 主 pipeline（`pre_pipeline.sh` step 3）负责更新。
sector rotation 只读取这些文件，**不更新它们**。

若主 pipeline 今天没有运行，检查宏观数据时间戳：
```bash
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh status
# 输出中会显示: "Macro data: last updated Xh ago"
```

若需要手动更新 MacroStateStore，使用**主 pipeline 的方式**（在主 pipeline 会话中）：
```bash
# 这是 someopark 主 pipeline 的命令，不属于 sector rotation 的职责
set -a && source .env && set +a && \
conda run -n someopark_run --no-capture-output python MacroStateStore.py --update
```

若 MacroStateStore 数据陈旧，sector rotation 会自动降级：regime 仅用实时 VIX（yfinance）
而不用完整宏观因子，信号仍然有效，只是 regime 精度略低。

---

**Q: 价格缓存过旧（ETF 价格未更新）**

价格缓存在 `price_data/sector_etfs/prices_yfinance_*.pkl`，首次运行自动下载，之后仅在缓存超过 1 天时重新拉取。强制刷新：
```bash
# 删除缓存，下次运行自动重下
rm price_data/sector_etfs/prices_yfinance_*.pkl
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run
```

---

**Q: `eps-full` 失败 / POLYGON_API_KEY 未设置**

```bash
# 检查 API key
set -a && source .env && set +a
echo "Key length: ${#POLYGON_API_KEY}"

# 测试 Polygon API
curl -s "https://api.polygon.io/vX/reference/financials?ticker=AAPL&timeframe=quarterly&limit=1&apiKey=$POLYGON_API_KEY" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status'), len(d.get('results', [])))"
```

---

**Q: `test` 失败**

```bash
# 详细错误
conda run -n qlib_run --no-capture-output \
    python -m pytest qlib-main/sector_rotation/tests/ -v --tb=long -x

# 单独运行失败的测试
conda run -n qlib_run --no-capture-output \
    python -m pytest qlib-main/sector_rotation/tests/test_signals.py::TestMomentum -v
```

---

**Q: `inventory_sector_rotation.json` 状态异常（需要手动重置）**

```bash
# 备份当前 inventory
cp qlib-main/sector_rotation/inventory_sector_rotation.json \
   qlib-main/sector_rotation/inventory_history/inventory_manual_backup_$(date +%Y%m%d).json

# 重置（下次 daily 运行时自动重建）
echo '{"as_of": null, "last_updated": null, "capital": 1000000, "holdings": {}, "cash_weight": 0.0, "prev_weights": {}, "prev_composite_scores": {}, "rebalance_history": []}' \
  > qlib-main/sector_rotation/inventory_sector_rotation.json

# 验证
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run
```

---

## 十二、环境变量参考

| 变量 | 用途 | 必需 |
|------|------|------|
| `POLYGON_API_KEY` | EPS 历史获取（`eps-full`, `eps-update`, `value-source polygon`） | EPS/polygon 模式 |
| `FRED_API_KEY` | MacroStateStore 宏观数据（VIX、HY spread、yield curve、ISM 等） | regime 信号 |
| `MONGO_URI` | （可选）MongoDB 价格源，默认不启用 | ✗ |

加载方式（每个会话只需一次）：
```bash
set -a && source .env && set +a
```

---

## 十三、与 someopark 主 Pipeline 的关系

板块轮动策略与 someopark 配对交易策略**完全独立、环境隔离**：

| | someopark (pairs) | sector_rotation |
|---|---|---|
| Conda env | `someopark_run` | `qlib_run` (严格隔离) |
| Universe | 个股配对 | 11 SPDR ETFs |
| 频率 | 每日 | 每月再平衡 |
| 方向 | 市场中性 | 纯多头 |
| Signal 文件 | `trading_signals/` (root) | `sector_rotation/trading_signals/` |
| Inventory | `inventory_mrpt.json` etc. | `inventory_sector_rotation.json` |
| Pipeline 脚本 | `pre_pipeline.sh` / `pipeline_runner.sh` | `sector_rotation_pipeline.sh` |

**`price_data/macro/` — 只读共享（sector rotation 不写）：**
- 由 `someopark_run` 的 `MacroStateStore.py --update` 维护（在 `pre_pipeline.sh` 中）
- `sector_rotation_pipeline.sh` 和 `SectorRotationDailySignal.py` **只读取** 这些 parquets
- 两者不存在写冲突，sector rotation 永远不会修改这些文件
- 若 macro parquets 陈旧 > 2 天，`status` 模式会显示警告，但不会阻止运行

**调用顺序建议（两个 pipeline 都需要当天运行时）：**
```
pre_pipeline.sh (someopark_run) → DailySignal.py (someopark_run)  ← 独立
sector_rotation_pipeline.sh daily (qlib_run)                       ← 独立，读 macro parquets
```
顺序无依赖，可任意顺序或并行，不会相互干扰。

**未来集成路径（规划中）：**
1. Regime 信号共享：sector rotation regime → someopark 资本分配（只读接口）
2. 组合级风险预算：跨策略 beta/vol 控制
