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
| 回测 + Walk-Forward IS/OOS | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh backtest --walk-forward` |
| 批量参数扫描（仅分析） | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh batch` |
| 批量 + OOS 验证 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh batch --oos-validate` |
| **参数选优 → OOS 过滤 → 写入生产** | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh select` |
| 独立 Walk-Forward 分析 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh wf` |
| 参数敏感性扫描 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh sensitivity` |
| Regime 历史分析 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh regime` |
| 生成 PDF 报告（含 WF IS/OOS） | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh tearsheet` |
| 运行测试套件 | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh test` |
| 查看原始 Z-score | `bash qlib-main/sector_rotation/sector_rotation_pipeline.sh signal-raw` |

---

## 零、运行 Schedule（调度计划）

### 时区约定

所有时间以 **ET（美东时间）** 为基准。UTC 换算：EST = UTC-5（冬）／EDT = UTC-4（夏）。
NYSE 收盘时间：**4:00 PM ET（周一至周五）**。

---

### 完整 Cron 配置

```bash
crontab -e
```

```cron
# ──────────────────────────────────────────────────────────────────────
# SECTOR ROTATION — 调度（所有时间 UTC）
# 冬令时 EST = UTC-5 / 夏令时 EDT = UTC-4
# ──────────────────────────────────────────────────────────────────────

# 【每日】工作日 17:15 ET = 21:15 UTC（冬）/ 17:15 ET = 21:15 UTC（夏）
# 脚本自动检测 NYSE 休市并 exit 0（不计入失败）
15 21 * * 1-5  cd /Users/xuling/code/someopark-test && \
               bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily \
               >> qlib-main/sector_rotation/logs/cron_sr_daily.log 2>&1

# 【每周日】01:00 ET = 06:00 UTC — EPS 增量维护 + dry-run 验证
0 6 * * 0      cd /Users/xuling/code/someopark-test && \
               bash qlib-main/sector_rotation/sector_rotation_pipeline.sh weekly \
               >> qlib-main/sector_rotation/logs/cron_sr_weekly.log 2>&1
```

> **夏令时说明**：22:20 UTC 在夏令时为 ET 18:20，在冬令时为 ET 17:20，均在 yfinance 调整后收盘价可用窗口内（4:30–4:45 PM ET），无需调整。

---

### 每日运行（工作日，自动）

**触发时间**：每个工作日 17:15 ET（收盘后约 75 分钟）

**内部步骤**：

| 步骤 | 内容 | 耗时 |
|------|------|------|
| ① NYSE holiday check | 检测是否为交易日，若休市则 exit 0 | <5 秒 |
| ② EPS auto-refresh | 若 `eps_history.json` > 7 天未更新，触发增量拉取 | 跳过 0 秒 / 拉取 1–3 分钟 |
| ③ SectorRotationDailySignal | 加载 ETF 价格 + MacroStateStore → 信号 → 调仓判断 → 写 inventory + 报告 | 2–4 分钟 |

**总耗时**：正常 3–5 分钟，EPS 触发时 5–8 分钟

**每日输出**：

```
trading_signals/sr_daily_report_YYYYMMDD_HHMMSS.txt   ← 人可读摘要（核心检查点）
trading_signals/sr_daily_report_YYYYMMDD_HHMMSS.json  ← 完整机器可读报告
inventory_sector_rotation.json                        ← 当前持仓（月首才变更）
inventory_history/inventory_sector_rotation_<ts>.json ← 每次变更的快照
logs/sr_daily_YYYYMMDD.log                            ← 完整运行日志
```

**每日验证清单**：

```bash
# 1. 日志末尾无报错
tail -20 qlib-main/sector_rotation/logs/sr_daily_$(date +%Y%m%d).log
# 预期末行：══ SR PIPELINE  mode=daily ── DAILY COMPLETE

# 2. 今日报告文件已生成
ls -lt qlib-main/sector_rotation/trading_signals/ | head -4
# 应有今日时间戳的 .txt 和 .json

# 3. 查看信号摘要（Regime / Rebalance / 权重分布）
cat $(ls -t qlib-main/sector_rotation/trading_signals/sr_daily_report_*.txt | head -1)

# 4. 确认 inventory 日期更新
python3 -c "
import json
d = json.load(open('qlib-main/sector_rotation/inventory_sector_rotation.json'))
print('as_of:', d.get('as_of'), '  last_updated:', d.get('last_updated'))
"
```

**绝大多数工作日**：无交易（HOLD），月首交易日自动触发 `monthly_rebalance`。

---

### 月首交易日（每月，含在每日 cron 内）

**无需额外操作**。`daily` 脚本自动识别月首交易日并触发 `monthly_rebalance`。

**当日额外操作（人工）**：
1. 查看 TXT 报告中的 trades 清单（ENTER / EXIT / INCREASE / DECREASE）
2. 确认 regime 状态和持仓权重合理
3. **次日开盘按清单执行交易**（若对接实盘在此步下单）

**月底可选**：生成月度绩效 tearsheet（约 10–15 分钟）
```bash
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh tearsheet
# 输出：qlib-main/sector_rotation/report/output/sector_rotation_tearsheet.pdf
```

**手动补跑**（若 daily cron 当日未运行）：
```bash
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh monthly --skip-holiday
```

---

### 每周运行（周日，自动）

**触发时间**：周日 01:00 ET = 06:00 UTC

**步骤**：

| 步骤 | 内容 | 耗时 |
|------|------|------|
| ① EPS 增量更新 | 拉取全部 55 个成分股中 > 7 天未更新的 symbol | 1–10 分钟 |
| ② dry-run 验证 | 跑完整信号 pipeline 不写 inventory | 2–3 分钟 |

**总耗时**：5–15 分钟

**验证**：
```bash
tail -20 qlib-main/sector_rotation/logs/cron_sr_weekly.log
# 预期：══ WEEKLY MAINTENANCE COMPLETE

grep -i "error\|fail\|traceback" qlib-main/sector_rotation/logs/cron_sr_weekly.log
# 应无输出
```

---

### 每月（手动，月末/月初）

**时机**：每月月末或月初、重大市场结构变化后

```bash
# 1. 参数选优（约 5–8 分钟）：
#    - 运行全部 59 个参数集 batch 回测
#    - Walk-Forward IS/OOS 验证（73折, anchored）→ 排除过拟合参数
#    - 从 OOS 幸存者中用 MCPS 选最优 → 写 selected_param_set.json
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh select

# 2.（可选）独立 Walk-Forward 分析（anchored + rolling，输出 CSV）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh wf

# 3.（可选）完整回测验证（使用选中的参数集）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh backtest

# 4.（可选）PDF tearsheet（含 WF OOS 曲线 + fold 明细表）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh tearsheet

# 5.（每季度）EPS 全量刷新
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-full
```

**`select` 三阶段流程**：
```
Stage 1: WF OOS 过滤
  → 跑 WalkForwardAnalyzer (anchored, 73 folds, step=15d, oos=6m)
  → 统计每个参数被选中时的平均 OOS Sharpe
  → 排除 mean OOS Sharpe ≤ 0 的参数（过拟合陷阱）

Stage 2: MCPS 选择（在 OOS 幸存者中）
  → 用今天宏观向量 + 全量历史 equity → MCPS.macro_cond_sharpe()
  → 选得分最高的（宏观环境最匹配的）

Stage 3: Fallback
  → 如果 MCPS 不可用 → 从 OOS 幸存者中选 recent_sharpe_12m 最高的
```

**月度检查要点**：

| 指标 | 目标 | 行动 |
|------|------|------|
| `select` 输出参数集 | OOS 验证 + MCPS 选出 | 审阅 `selected_param_set.json` |
| `selection_method` | `"mcps_oos_filtered"` | 若为 `"recent_sharpe_12m"` → MCPS 可能不可用 |
| `n_oos_survivors` | ≥ 3 | 若太少 → 参数空间可能需要扩展 |
| `oos_mean_sharpe` | > 0.5 | 选出参数在历史 OOS 中的平均表现 |
| `wf_mean_wfe` | > 0.5 | 整体 Walk-Forward Efficiency（IS→OOS 衰减） |
| IS Sharpe | 0.8–1.2 | 过高可能过拟合 |
| OOS MaxDD | < 20% | 超过 25% → 检查 vol_scaling 参数 |

---

### 紧急情况（VIX > 32，随时）

**自动检测**：`daily` 运行时 `should_emergency_rebalance()` 自动检查，无需额外触发。

**触发结果**：报告中 `Rebalance: YES (emergency_vix)`，目标持仓 → 50% 现金 + 50% 防御板块（XLU / XLP / XLV）

> VIX 完整阶梯（生产配置）：VIX < 28 → 0% 现金；VIX ≥ 28 → 15% 现金；VIX ≥ 32 → 35% 现金；VIX ≥ 35 → 50% 现金（emergency_derisk_vix）

**人工流程**：
1. 收到 daily 报告，确认 `emergency_vix` 触发
2. 审阅新目标持仓，确认现金权重合理
3. 当日或次日开盘执行减仓
4. 每日继续运行，等待 VIX 回落 < 25 后自动恢复 risk_on

**随时查看状态（不写入）**：
```bash
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh status
```

---

### 故障处理

#### 故障 1：NYSE 休市（预期行为，非错误）

**现象**：日志出现 `NYSE 休市 (YYYY-MM-DD) — pipeline skip, exit 0`

**处理**：正常，脚本 exit 0，cron 不报错，无需操作。

---

#### 故障 2：yfinance ETF 价格下载失败

**现象**：日志含 `YFRateLimitError` / `ConnectionError` / `No data returned`

**处理**：
```bash
# 等 30 分钟后重跑（rate limit 通常快速恢复）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily --skip-holiday

# 若频繁失败，删除价格缓存强制重下
rm price_data/sector_etfs/prices_yfinance_*.pkl
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run --skip-holiday
```

---

#### 故障 3：EPS 增量更新失败（非致命）

**现象**：日志含 `EPS incremental update failed`，pipeline 继续运行

**影响**：value signal 自动降级为 `proxy` 模式（price-to-5yr-avg），信号仍然有效，覆盖率略低

**处理**：
```bash
# 手动检查详细错误
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh eps-update
cat qlib-main/sector_rotation/logs/sr_eps-update_$(date +%Y%m%d).log | grep -i error

# 验证 Polygon API key
set -a && source .env && set +a && echo "Key: ${POLYGON_API_KEY:0:8}..."
```

---

#### 故障 4：MacroStateStore 数据陈旧

**现象**：日志含 `MacroStateStore load failed` 或 `Falling back to FRED API`

**处理**：
- macro parquets 由 **someopark 主 pipeline** 维护，sector rotation 不负责更新
- 若 MacroStateStore 不可用，regime 信号自动降级为仅用实时 VIX（yfinance），可接受
- 若完全无数据，FRED fallback 自动触发，需要 `FRED_API_KEY` 有效
- 确认主 pipeline 当日已运行；恢复后次日数据自动更新

---

#### 故障 5：信号计算失败（exit 非零）

**现象**：日志末行含 `FAILED:` / `exit=1` / Python traceback

**处理**：
```bash
# 查看完整错误
tail -60 qlib-main/sector_rotation/logs/sr_daily_$(date +%Y%m%d).log

# 安全诊断（不写 inventory）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run --skip-holiday
```

常见原因：
- 价格数据不足 → 检查 `price_data/sector_etfs/`
- qlib_run 环境包版本冲突 → `conda run -n qlib_run python -m pytest qlib-main/sector_rotation/tests/ -x`
- config.yaml 参数错误 → `git diff qlib-main/sector_rotation/config.yaml`

---

#### 故障 6：错过月首调仓

**现象**：月初几天后发现 inventory `rebalance_history` 无当月记录

**处理**：
```bash
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh monthly --skip-holiday
# 或
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily --force-rebalance --skip-holiday
```

---

#### 故障 7：inventory 状态异常

**现象**：持仓权重错误 / `last_updated` 停留在过去某日

**处理**：
```bash
# 查看备份历史
ls -lt qlib-main/sector_rotation/inventory_history/

# 恢复到最近正常快照
cp qlib-main/sector_rotation/inventory_history/inventory_sector_rotation_<正常时间戳>.json \
   qlib-main/sector_rotation/inventory_sector_rotation.json

# 验证恢复状态
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run --skip-holiday
```

---

### 与 someopark 主 Pipeline 的执行顺序

```
~01:15 UTC    someopark: MacroStateStore.py --update（写入 price_data/macro/）
~01:15 UTC    someopark: DailySignal.py --strategy both（MRPT + MTFS 信号）
              ↓  price_data/macro/ parquets 写入完成
~22:20 UTC    sector_rotation: sector_rotation_pipeline.sh daily（读取 price_data/macro/，只读）
```

两者间隔约 21 小时，MacroStateStore 数据当日内始终有效，无竞争风险，可并行运行也无妨。

---

### 频率总览

| 频率 | 时间（ET） | 命令 | 预计耗时 | 人工操作 |
|------|-----------|------|---------|---------|
| 每个工作日 | 17:15 PM（cron） | `daily` | 3–5 分钟 | 月首：审阅交易清单 + 次日执行 |
| 每周日 | 01:00 AM（cron） | `weekly` | 5–15 分钟 | 无 |
| **每月** | 任意（手动） | **`select`** | 5–8 分钟 | 审阅 selected_param_set.json + OOS 指标 |
| 每月（可选） | 任意 | `tearsheet` | 10–15 分钟 | 审阅 PDF（P11-P13 含 WF 结果） |
| 每月（可选） | 任意 | `wf` | 3–5 分钟 | 审阅 CSV（逐折 IS/OOS 明细） |
| 每季度末 | 任意 | `eps-full` | 5 分钟 | 无 |
| VIX > 32 | daily 自动触发 | （含在 daily 内） | — | 确认后当日 / 次日执行 emergency de-risk |

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
| `rebalance.emergency_derisk_vix` | `32.0` | VIX 超过此值 → 紧急 de-risk（移至 50% 现金）|
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

### 批量参数扫描与生产选参

#### 架构说明

```
每月（select）：
  select  →  运行 59 集 batch 回测
          →  Walk-Forward IS/OOS 验证（73 折）→ 排除过拟合参数
          →  从 OOS 幸存者中用 MCPS(Gaussian-kernel-weighted Sharpe) 选最优
          →  写 selected_param_set.json
                    ↓（自动生效）
每日/每周/每月（daily/weekly/monthly）：
  SectorRotationDailySignal
          →  step 1b: 读取 selected_param_set.json
          →  apply_param_set(cfg, PARAM_SETS[name])
          →  用该参数组合生成信号 / 调仓
```

**MCPS 核心算法**（`MCPS.py::macro_cond_sharpe()`）：
```
d_t = ||macro_t - today_vec||₂         每天到今天的宏观状态距离
σ   = median(d_t)                       自适应带宽
w_t = exp(-d_t² / 2σ²)                Gaussian 核权重
wmean = Σ(w_t × r_t) / Σw_t           加权日均回报
wvar  = Σ(w_t × (r_t - wmean)²) / Σw_t 加权方差
score = wmean / √wvar × √252          年化加权 Sharpe
```

**OOS 过滤**：WalkForwardAnalyzer 跑 73 折（6个月OOS，15天步长），
只保留 mean OOS Sharpe > 0 的参数集。典型结果：59 → 4 个幸存者。

**selected_param_set.json 不存在** → 静默跳过，使用 `config.yaml` 默认参数，行为不变。

#### 常用命令

```bash
# 【推荐，每季度】运行 59 集 + 选优 + 写入生产
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh select

# 仅批量分析（不影响生产）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh batch

# 仅运行特定组（分析用）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh batch --group L
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh batch --group A B C --sort-by calmar

# 结果输出：backtest_results/sr_batch_summary_<timestamp>.csv + .xlsx
#   - sharpe:             全期 Sharpe（2018-07-01 → 今）
#   - recent_sharpe_12m:  近期 12 个月 Sharpe（select 的选参依据）
#   - calmar, max_drawdown, annual_turnover 等

# 查看当前全部参数集列表
conda run -n qlib_run python -c "
import sys; sys.path.insert(0, 'qlib-main/sector_rotation')
from SectorRotationStrategyRuns import list_param_sets
list_param_sets()
"
```

#### selected_param_set.json 管理

```bash
# 查看当前选中的参数集
cat qlib-main/sector_rotation/selected_param_set.json
# 示例输出：
# {
#   "param_set": "tight_beta_tracker",
#   "selection_method": "mcps_oos_filtered",
#   "mcps_oos_filtered": 1.0934,
#   "recent_sharpe_12m": 1.9651,
#   "full_period_sharpe": 1.0196,
#   "full_period_calmar": 0.653,
#   "selected_at": "2026-05-04",
#   "n_candidates": 4,
#   "n_oos_survivors": 4,
#   "oos_filter_applied": true,
#   "oos_mean_sharpe": 1.3652,
#   "oos_n_selected": 8,
#   "wf_mean_wfe": 0.6395,
#   "macro_data_days": 1970
# }
# 关键字段说明：
#   selection_method: "mcps_oos_filtered" = WF OOS 过滤 + MCPS 选参
#   n_oos_survivors: 通过 OOS 验证的参数数量（59→4）
#   oos_mean_sharpe: 被选参数在 WF 中被选时的平均 OOS Sharpe
#   wf_mean_wfe: 全部 73 折的平均 Walk-Forward Efficiency

# 验证 DailySignal 确实 pick up 了该参数集（日志中查找）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh dry-run 2>&1 | grep "PARAM SELECT"
# 预期：INFO [PARAM SELECT] Active: tight_beta_tracker | recent_sr12m=1.9651 | selected=2026-05-04

# 手动覆盖（临时指定特定参数集，绕过 select 结果）
python3 -c "
import json
from pathlib import Path
sel = {
    'param_set': 'partial_filter_scaled',
    'recent_sharpe_12m': 1.095,
    'full_period_sharpe': 1.095,
    'selected_at': '$(date +%Y-%m-%d)',
    'n_candidates': 1,
}
Path('qlib-main/sector_rotation/selected_param_set.json').write_text(json.dumps(sel, indent=2))
print('Done:', sel['param_set'])
"

# 回退到 config.yaml 默认参数（删除 selected_param_set.json）
rm qlib-main/sector_rotation/selected_param_set.json
# → 下次 daily 运行自动使用 config.yaml 默认值
```

> 参数集设计详情见 README.md「参数集扫描」章节，涵盖 13 组 59 集的核心设计与学术依据。

---

### Walk-Forward IS/OOS 分析

**核心文件**：`walk_forward.py` → `WalkForwardAnalyzer` 类

**理论基础**：WFO (Pardo 2008) + DSR (Bailey & López de Prado 2014) + CPCV 净化/禁运 (López de Prado 2018) + Conditional Parameter Optimization (Chan et al. 2021)

**运行命令**：
```bash
# 独立 WF 分析（anchored + rolling 两种模式，输出 CSV）
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh wf

# 指定模式和参数
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh wf --mode anchored --step-days 15 --oos-months 6

# 通过 backtest 入口运行
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh backtest --walk-forward

# 通过 batch 入口运行
bash qlib-main/sector_rotation/sector_rotation_pipeline.sh batch --oos-validate
```

**默认参数**：

| 参数 | 值 | 说明 |
|------|------|------|
| `is_years_min` | 3 | IS 最短 3 年（anchored 模式逐折扩展） |
| `oos_months` | 6 | 每折 OOS 窗口 6 个月 |
| `step_days` | 15 | 每 15 交易日（~3 周）滚动一步 |
| `embargo_days` | 5 | IS/OOS 间隔 5 交易日 |
| `mode` | anchored | IS 从固定起点扩展；rolling = 固定宽度 |

**时间切分（anchored, step=15d, oos=6m, ~73 折）**：
```
             IS (expanding from 2018-07)     [5d]  OOS (6 months)
Fold  1: [2018-07 ──────── 2021-07-01] [embargo] [2021-07-08 ─── 2022-01-06]
Fold  2: [2018-07 ──────── 2021-07-22] [embargo] [2021-07-29 ─── 2022-01-27]
  ...          (每 15 交易日前移一步，OOS 重叠 ~88%)
Fold 73: [2018-07 ──────── 2025-01-31] [embargo] [2025-02-10 ─── 2026-05-04]
```

**每折选参数逻辑**：
1. 59 组参数各取 IS 段 equity → 计算 IS 指标
2. IS 最后 30 交易日宏观状态均值 → `is_macro_vector`
3. `MCPS.macro_cond_sharpe()` 打分（高斯核加权 Sharpe）
4. DSR 过滤（N=59 多重测试校正）
5. 选最高分 → OOS 评估 → WFE = OOS_SR / IS_SR

**输出文件**：
```
backtest_results/wf_anchored_fold_summary_<ts>.csv   逐折明细
backtest_results/wf_rolling_fold_summary_<ts>.csv    rolling 模式
```

CSV 列：`fold, is_start, is_end, oos_start, oos_end, selected, method, is_sharpe, mcps_score, dsr_pvalue, oos_sharpe, oos_return, oos_maxdd, oos_regime, wfe`

**审阅要点**：

| 指标 | 目标 | 行动 |
|------|------|------|
| Synthetic OOS Sharpe | > 0.3 | < 0.2 → 策略可能在当前市场失效 |
| Mean WFE | > 0.5 | < 0.3 → 严重过拟合 |
| OOS 幸存者数 | ≥ 3 | < 2 → 参数空间可能需要扩展 |
| 被选中次数分布 | 不过度集中 | 一个参数占 > 60% → 缺乏适应性 |

---

### 单策略回测

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

# 每日信号：周一至周五 17:15 ET (21:15 UTC)
# 自动跳过 NYSE 节假日
15 21 * * 1-5   cd /Users/xuling/code/someopark-test && \
    bash qlib-main/sector_rotation/sector_rotation_pipeline.sh daily \
    >> qlib-main/sector_rotation/logs/cron_sr_daily.log 2>&1

# 每周 EPS 维护：周日 06:00 UTC (01:00 ET)
# 增量更新 55 个股票的 EPS + 验证 dry-run
0 6 * * 0   cd /Users/xuling/code/someopark-test && \
    bash qlib-main/sector_rotation/sector_rotation_pipeline.sh weekly \
    >> qlib-main/sector_rotation/logs/cron_sr_weekly.log 2>&1
```

> **注意**：月首交易日的 rebalance 由 `daily` 模式自动检测触发，无需单独 cron。
> 如需保证月首一定执行，可在月初手动运行 `monthly` 模式作为 fallback。

### 验证 cron 正在运行

```bash
# 查看最近 cron 日志
tail -50 qlib-main/sector_rotation/logs/cron_sr_daily.log

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
├── SectorRotationBatchRun.py           59组参数 batch 回测 + --select + --oos-validate
├── SectorRotationStrategyRuns.py       59组参数集定义 + apply_param_set()
├── walk_forward.py                     Walk-Forward IS/OOS 分析框架（WFE/DSR）
├── selected_param_set.json             当前生产参数（--select 写入，DailySignal 读取）
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
│   ├── engine.py                       事件驱动月度回测 + --walk-forward + --param-set
│   ├── costs.py                        点差 + impact 成本模型（按 ETF 流动性分层）
│   ├── metrics.py                      Sharpe/Calmar/IR/CVaR/Brinson 归因
│   ├── robustness.py                   参数敏感性 + bootstrap 置信区间
│   └── sensitivity.py                  参数扫描分析
│
├── backtest_results/                   batch/select/WF 输出
│   ├── sr_batch_summary_<ts>.csv       59组全时期指标排名
│   ├── sr_batch_summary_<ts>.xlsx      同上（Excel 条件格式）
│   ├── wf_anchored_fold_summary_<ts>.csv  WF 逐折明细（anchored IS）
│   ├── wf_rolling_fold_summary_<ts>.csv   WF 逐折明细（rolling IS）
│   └── selected_param_set.json         select 归档副本
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
