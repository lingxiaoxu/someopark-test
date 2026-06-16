<p align="center">
  <img src="../public/SOMEO PARK矢量源文件 Big Square.svg" alt="Someopark" width="160"/>
</p>

<h1 align="center">prediction_market</h1>
<p align="center"><b>世界杯 2026 × Kalshi + Polymarket 量化交易系统</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/conda-someopark__run-green?logo=anaconda&logoColor=white"/>
  <img src="https://img.shields.io/badge/venues-Kalshi%20%7C%20Polymarket-orange"/>
  <img src="https://img.shields.io/badge/model-Dixon--Coles%20%7C%20MonteCarlo%20200k-purple"/>
  <img src="https://img.shields.io/badge/data-API--Football%20(league%3D1)-lightgrey"/>
  <img src="https://img.shields.io/badge/tests-90%20passing-brightgreen"/>
  <img src="https://img.shields.io/badge/isolated-prediction__market%2F-red"/>
</p>

---

把这套系统当作一个**小型自营交易台（prop desk）**：跨 **Kalshi + Polymarket US** 两个 CFTC 监管场所交易 2026 世界杯（48 队 / 104 场，2026‑06‑11 → 07‑19）。研究端按顶级足球分析师的标准建模，执行端按量化工程的标准做数据、风控、回测与跨场执行。

> **隔离铁律**：本项目完全自包含于 `prediction_market/` —— 读自己的 `.env`、只写自己的 `data/`、**绝不 import 根仓库任何代码**。唯一对外写入是可选的前端 JSON（`--emit-frontend`）。所有命令在 **`someopark_run`** conda 环境下运行。

设计文档：`.claude/plan/prediction market plan/`（13 个文件，00–12）；逐条实现核对：[`PLAN_AUDIT.md`](PLAN_AUDIT.md)。

---

## 三个预测品类（共用同一模拟引擎，内部一致）

| 品类 | 核心问题 | 方法 | 标的 |
|------|----------|------|------|
| **① 单场比赛** | 这一场谁赢 / 比分 / 进球 | Dixon‑Coles 双泊松比分模型（小组/淘汰分别建模）+ 赛中 in‑play 实时模型 | 3‑way 胜平负、总进球、双方进球、晋级 |
| **② 冠军** | 谁举杯 | 蒙特卡洛锦标赛模拟（48 队 2026 赛制，N≥200k） | "Men's World Cup winner?" 48 互斥 outcome |
| **③ 金靴** | 谁进球最多 | 嵌套在锦标赛路径里的球员进球模拟 | Kalshi `KXWCGOALLEADER` / Polymarket 对应市场 |

三品类共用**队伍强度底座**与**同一批模拟路径**，保证冠军、晋级、金靴概率自洽。其上叠加第四类**交易**机会：**④ 跨场所相对价值 / 套利**（同一标的 Kalshi vs Polymarket US 价差）。

---

## 架构总览

```
  足球数据 (API-Football)          场所行情 (Kalshi / Polymarket)
        │                                  │
        ▼                                  ▼
  ┌───────────────────────────────────────────────────────┐
  │ 数据层  ingest/ : 节流摄取 → SQLite + 原始快照 + 增量水位 │
  └───────────────────────────────────────────────────────┘
        ▼
  ┌───────────────────────────────────────────────────────┐
  │ 模型层  model/  : 强度(反解) → Dixon-Coles → MC 锦标赛    │
  │                  → 金靴嵌套 → in-play → 集成 → OOS/校准   │
  └───────────────────────────────────────────────────────┘
        ▼
  ┌───────────────────────────────────────────────────────┐
  │ 策略/执行  strategy/ exec/ : de-vig → edge → 分数Kelly    │
  │            → 风控/限额 → 目标净头寸 → 合法订单(场所规则)   │
  └───────────────────────────────────────────────────────┘
        ▼
  ┌───────────────────────────────────────────────────────┐
  │ 场所  venues/ : Kalshi + Polymarket US 执行；Global 只读  │
  │                venue_guard 路由守卫 + 实盘交易硬闸         │
  └───────────────────────────────────────────────────────┘
```

---

## 环境配置

### 1. Python 环境

所有命令在 `someopark_run` conda 环境下运行（含 numpy/scipy/pandas；项目额外依赖见 `requirements.txt`）。

```bash
conda run -n someopark_run --no-capture-output python -m pip install -r prediction_market/requirements.txt
```

### 2. 配置 `.env`（已 gitignore，绝不提交）

```bash
cp prediction_market/.env.example prediction_market/.env   # 然后填入真实 key
```

| 变量 | 用途 |
|------|------|
| `API_FOOTBALL_KEY` | API‑Football 主数据源（league=1, season=2026） |
| `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` | Kalshi 凭证（私钥 PEM 存**仓库外**，按路径引用） |
| `KALSHI_ENV` / `KALSHI_TRADING_ENABLED` | 环境(demo/prod) / **实盘下单硬闸** |
| `PMUS_KEY_ID` / `PMUS_SECRET` / `PMUS_TRADING_ENABLED` | Polymarket US Ed25519 凭证 / **实盘下单硬闸** |

> 所有 API key / secret 仅存于 `.env`，代码无任何硬编码凭证。Kalshi PEM 私钥与 Polymarket secret 受 `.gitignore`（`*.key` / `*.pem` / `.env`）与**仓库外存储**双重保护。

### 3. 运行方式

```bash
set -a && source prediction_market/.env && set +a && \
  conda run -n someopark_run --no-capture-output python -m prediction_market.<module> [args]
```

---

## 核心文件

### 配置 / 数据层（`config/` `ingest/`）

| 文件 | 说明 |
|------|------|
| `config/config.py` | 中央配置：路径、模型/场所/风控/数据参数（全部锚定 `prediction_market/`，单一可追溯来源） |
| `ingest/prior_ingest.py` | 静态先验（file 10）：12 组 48 队出线模拟 + FIFA 排名 + 分组，恒等式校验(±2pp)，队名别名映射 |
| `ingest/api_football.py` | API‑Football 节流客户端：预算护栏(7000/月)、读写限流+429 退避、批量 `/fixtures?ids=`(≤20，events 内嵌)、逐调用记账 |
| `ingest/store.py` | 本地存储：SQLite 业务表 + append‑only 原始快照 + 增量水位(watermark) + 月度用量；`data/wc.db` |
| `ingest/soccer_ingest.py` | 摄取编排：watermark/TTL 闸（TTL 内重跑 0 请求）、幂等 upsert、coverage 感知；`--scope {static\|results\|live\|h2h\|squads\|all}` |

### 模型层（`model/`）

| 文件 | 说明 |
|------|------|
| `model/strength.py` | 队伍强度底座：FIFA 排名 → 评分，**反解拟合**到先验期望积分（坐标下降 + Dixon‑Coles 解析） |
| `model/dixon_coles.py` | 单场内核：双泊松 + 低分相关修正 → 比分矩阵 → 胜平负/总进球/双方进球/晋级（含加时+点球） |
| `model/tournament.py` | 蒙特卡洛锦标赛（2026 48 队赛制，best‑8‑thirds），向量化，20 万次约 4 秒 → 冠军/晋级/各轮/E[场次] |
| `model/golden_boot.py` | 金靴嵌套模拟：球员进球与球队走多远相关（同一批路径），Poisson(μ×已打场次) |
| `model/inplay.py` | 赛中实时模型（分钟+比分+红牌 → 实时胜平负、公平平局价、剩余进球），驱动赛中交易 |
| `model/match_pricing.py` | 单场定价：从比分矩阵导出任意单场市场（小组 72 场全量） |
| `model/ensemble.py` | 集成：参数变体 → 概率均值 + **离散度**（替换占位 sigma，喂仓位） |
| `model/calibrate.py` | 校准/评分：Brier / Log‑loss / 可靠性曲线 / CLV / bootstrap CI |
| `model/oos_eval.py` | OOS 体检：冻结赛前模型对已打比赛打分，查系统性偏差（放真钱前门禁） |
| `model/run_model.py` | 编排器：先验 → 强度 → 锦标赛 → 金靴 → 单场定价 → 前端 JSON；`--full` / `--ensemble` / `--emit-frontend` |

### 策略 / 执行 / 场所（`strategy/` `exec/` `venues/`）

| 文件 | 说明 |
|------|------|
| `strategy/devig.py` | 去 vig：multiplicative / power / Shin（长尾标的用 power/Shin 校 favorite‑longshot 偏差） |
| `strategy/edge.py` | edge 计算与下单门槛：`p_eff = p − k·σ`，`net_edge ≥ θ` 才交易（费率显式传入，不硬编码） |
| `strategy/sizing.py` | 分数 Kelly + 单市场/深度上限 |
| `strategy/risk.py` | 组合风控：单市场/主题/总暴露上限（跨两场所）+ 当日亏损 kill‑switch |
| `strategy/cross_venue.py` | 跨场数学：锁定套利 `net_lock` + 统计相对价值 + `<$1` 篮子套利（结算等价闸） |
| `strategy/xv_monitor.py` | 跨场比价监控（live 只读）：Global 盘口去 vig vs `p_model`，输出冠军价差/相对价值报告 |
| `exec/order_translation.py` | 目标净头寸 → 合法订单：编码场所规则（Kalshi netting / Poly US intent），下单前自检清单 |
| `venues/polymarket_global/reader.py` | Polymarket Global 只读 reader（Gamma/CLOB/Data，免凭证，`executable=False`） |
| `jobs/hourly_job.py` | 每小时编排：增量摄取 → 强度+赛果更新 → 模型 → 跨场监控 → OOS → 结构化日志（`--dry-run`/`--loop`） |
| `venues/base.py` | 统一 `Venue` / `ExecutionVenue` 接口 + `OrderBook`/`Balance`/`Position` 类型 |
| `venues/guard.py` | `venue_guard`：执行只允许 Kalshi/Poly US（拦截 Global）；**实盘交易硬闸**（prod/真钱 须显式授权） |
| `venues/kalshi/auth.py` | Kalshi RSA‑PSS 签名（毫秒时间戳，签 `ts+METHOD+path` 去 query） |
| `venues/kalshi/market_data.py` | Kalshi 公开行情读取器（免鉴权）：`best_prices` 双边 ask + 深度 |

### 运维 / 报告 / 前端层（`ops/` `jobs/`）

| 文件 | 说明 |
|------|------|
| `ops/schedule.py` | 赛程查看器：按美东 ET + 美西 PT 双时区列出未来比赛（跨午夜正确处理）；`--upcoming` / `--days N` / `--refresh` |
| `ops/monitor.py` | 健康报告：模型新鲜度、API 预算、校准状态、跨场价差、错误率 → `health.json` |
| `ops/performance_report.py` | **收益/准确度报告**：已结算场次 Brier/Log‑loss/命中率 + 纸面校准 P&L；`--pdf` 输出机构风格 PDF（沿用 `PnLReport.py` 字体/配色） |
| `ops/risk_report.py` | **风险报告**：交易闸门、仓位限额、各场余额（prod key 永不查）、敞口、API 预算、校准闸门、护栏；`--pdf` 同款 PDF |
| `ops/pdf_style.py` | PDF 样式模块（自包含复制根仓库报告风格：PingFang CJK 字体、深蓝表头+金线、隔行底纹、盈亏红绿；**不 import 根代码**） |
| `ops/system_overview.py` | 静态系统目录（接口/模式/调度/输入输出/价值）的单一数据源，供 PDF 与前端共用 |
| `ops/upcoming_export.py` | **逐场跨场报价**：对未来比赛真实拉取 Kalshi（公开）+ Polymarket US（读凭证）单场 3‑way（ask/bid）+ 去 vig + 模型边缘 + 跨场锁定套利 → `upcoming.json`（只读，绝不下单） |
| `ops/frontend_export.py` | **前端数据合约**：静态目录 + 实时快照（performance/risk/预测/upcoming）→ 单一 `frontend_overview.json`，前端读这一个文件即可 |
| `jobs/live_poller.py` | 盘中每分钟轮询：live 公允价 + 跨场套利 + 战术 → `inplay_signals.json` |

---

## 数据层：API‑Football 节流摄取

主数据源（官方世界杯指南见 plan file 11）。**纪律：统一拉一次、集中存储、绝不重复拉**——前端读后台 SQLite，不直连 API。

```bash
set -a && source prediction_market/.env && set +a
# 球队 + 赛程 + 积分榜（≈3 请求，一次性 / TTL 天级）
conda run -n someopark_run --no-capture-output python -m prediction_market.ingest.soccer_ingest --scope static
# 已完赛比分 + 事件（批量内嵌，每小时）
conda run -n someopark_run --no-capture-output python -m prediction_market.ingest.soccer_ingest --scope results
# 赛中实时（仅比赛日，~2 请求/轮，15s 节奏）
conda run -n someopark_run --no-capture-output python -m prediction_market.ingest.soccer_ingest --scope live
```

> 稳定性/省请求是强制的，不是口号：TTL 内重跑 **0 请求**；完赛事件 **20 场/请求** 批量；预算护栏拒绝任何超 7000/月的调用；`injuries`(本赛事 coverage=False) 直接跳过。整轮 bootstrap（队伍+赛程+积分+一比赛日事件）仅 **7 请求**。

---

## 模型层：从先验到概率

```bash
# 快速 50k 模拟
conda run -n someopark_run --no-capture-output python -m prediction_market.model.run_model
# 全量 20 万次 + 集成离散度 + 写前端 JSON
conda run -n someopark_run --no-capture-output python -m prediction_market.model.run_model --full --ensemble --emit-frontend
# OOS 体检（对已打比赛，零额外 API 调用）
conda run -n someopark_run --no-capture-output python -m prediction_market.model.oos_eval
```

输出：`data/output/model_run_<ts>.json` + `latest.json`（`champion` / `golden_boot` / `group_matches` / `meta` + 诚实的 `model_notes`）；`oos_report.json`。

---

## 场所接入与实盘安全

| 场所 | 角色 | 状态 |
|------|------|------|
| **Kalshi** | 执行 + 行情（RSA‑PSS） | demo 激活 + 鉴权验证 ✅；prod 真钱**保留且禁用** |
| **Polymarket US** | 执行 + 行情（Ed25519, NY 合法） | 凭证**已验证** ✅；`PMUS_TRADING_ENABLED` 闸 |
| **Polymarket Global** | 只读参考（美 geoblock） | 免凭证，待接 |

> **实盘安全（真钱）**：`guard.assert_trading_enabled()` 分环境——demo 下单放行（模拟）；**Kalshi prod / Polymarket US（真钱）下单硬拦截**，除非 `*_TRADING_ENABLED=true` 且用户显式授权。任何向 Polymarket Global 的下单被 `venue_guard` 拦截。私钥/secret 存仓库外，`.env` 双重 gitignore。

---

## 输出目录

| 路径 | 内容 | 版本控制 |
|------|------|----------|
| `data/priors/` | 静态先验（`ext_sim_v0.json` file 10）+ 金靴种子球员 | ✅ 跟踪 |
| `data/output/` | 模型运行 JSON（`latest.json` / `model_run_*` / `oos_report`） | gitignore |
| `data/raw/` | API 原始响应快照（带 `fetched_at`，可重放） | gitignore |
| `data/wc.db` | SQLite 业务库（teams/fixtures/events/standings/h2h/squads…） | gitignore |
| `~/.config/someopark/*.key` | Kalshi PEM 私钥（**仓库外**） | 永不进 git |

---

## 测试

```bash
conda run -n someopark_run --no-capture-output python -m pytest prediction_market/tests/ -q
```

90 passing：先验校验、Dixon‑Coles、强度标定、锦标赛/金靴分布、in‑play、校准、OOS、集成、de‑vig/edge/sizing、风控、跨场套利、订单翻译、场所守卫、Kalshi 签名/盘口解析、数据层（store/预算/解析）、运维报告（performance/risk 报告 + PDF 渲染 + 前端 export 合约）。

---

## 设计文档索引（`.claude/plan/prediction market plan/`）

| 文件 | 内容 |
|------|------|
| `00_README_总览` | 三品类、架构、场所机制、合规、OOS |
| `01–05` | Kalshi 对接 / 数据管道 / 建模 / 策略执行 / 工程运维 |
| `06–10` | 路线图 / Polymarket 对接 / 跨场策略 / 场所微结构规则 / 球队先验全量 |
| `11` | **API‑Football 对接**（官方世界杯指南 + 本项目实现） |
| `12` | **Kalshi Trade API 深度摘要**（全文精读 → 环境/鉴权/定点价/下单V2/限速/WS/映射） |

---

## 系统总览 — 接口 / 模式 / 频率 / 价值（前端搬运清单）

> **单一数据源**：`python -m prediction_market.ops.frontend_export` → 生成 `data/output/frontend_overview.json`，前端直接读这一个文件即可，无需在客户端重写任何逻辑。下面每一节都对应该 JSON 的一个 key。
>
> **诚实结论**：系统现为「只看不买」状态——纪律闸门（calibration gate）在主动拦截：模型在已结算小组赛上 Brier 仍劣于均匀基线（0.667），尚未达到可交易等级，故拒绝下任何真钱单。宁可不交易，也不拿没验证过的边缘去亏钱。

### 1. 接口（`interfaces`，13 条 CLI）

| 类别 | 命令 `python -m prediction_market.<x>` | 作用 |
|------|----------------------------------------|------|
| 数据 | `ingest.bootstrap` | 一次性拉全量（球队/球员/对阵/赛程），建增量水位 |
| 数据 | `ingest.refresh` | 增量刷新（赛果、比分、live 状态） |
| 预测 | `model.match_pricing` | 单场 3‑way 公允价（主/平/客），含点球大战建模 |
| 预测 | `model.tournament` | 蒙特卡洛冠军概率（48 队） |
| 预测 | `model.golden_boot` | 金靴（进球王）概率 |
| 策略 | `strategy.compare` | 模型 vs 市场偏离扫描（赛前） |
| 策略 | `strategy.inplay_arb` | 盘中每分钟：套利 / 相对价值 / 战术 |
| 运维 | `ops.schedule` | 赛程表（美东 ET + 美西 PT） |
| 运维 | `ops.monitor` | 健康报告 |
| 运维 | `ops.performance_report` | 收益/准确度报告（`--pdf`） |
| 运维 | `ops.risk_report` | 风险报告（`--pdf`） |
| 调度 | `jobs.hourly_job` | 整点任务（刷数据+扫偏离+健康） |
| 调度 | `jobs.live_poller` | 盘中每分钟轮询（→ inplay_arb） |

### 2. 模式（`modes`，5 个）

- **demo（当前）**：Kalshi demo，假钱 $10，可完整测试下单
- **prod（未启用）**：真钱；`KALSHI_TRADING_ENABLED` + `PMUS_TRADING_ENABLED` 双闸全关
- **read‑only**：Polymarket Global 只读价
- **纪律闸门**：模型未达标 → 所有边缘信号被拦
- **$1 硬顶**：任何单 notional ≤ $1.00，代码层 `enforce_order_cap()` 强制

### 3. 怎么运行 / 什么时候运行 / 频率（`schedule`）

| 时机 | 跑什么 | 频率 |
|------|--------|------|
| 每天一次（赛前） | `refresh` → `tournament` → `compare` | 1×/天 |
| 整点 | `jobs.hourly_job` | 每小时 |
| 比赛进行中 | `jobs.live_poller`（→ inplay_arb） | 每分钟 |
| 随时查看 | `schedule` / `performance_report` / `risk_report` | 按需 |

> 运行前缀统一：`set -a && source prediction_market/.env && set +a && conda run -n someopark_run python -m ...`

### 4. 预测什么

单场主/平/客 · 冠军概率（48 队）· 金靴 · 盘中实时公允价 + 套利/战术。

### 5. 输入是什么 / 在哪里（`inputs`）

- 行情/赛事数据 → `data/wc.db`（API‑Football 拉一次存中央）
- 密钥 → `prediction_market/.env`（已 gitignore；PEM/secret 在 `~/.config/someopark/`）
- API 预算 → 7000 req/月（当前 ~1459/7000，21%）

### 6. 输出是什么 / 在哪里（`outputs`，全在 `data/output/`）

- `xv_champion.json` / `xv_matches.json` — 冠军概率 / 赛前偏离
- `upcoming.json` — **逐场跨场报价**（模型 + book + 真实 Kalshi/Poly US ask/bid + 边缘 + 锁定套利）
- `inplay_signals.json` — 盘中套利/战术
- `performance_report.json` + `.pdf` — 收益/准确度
- `risk_report.json` + `.pdf` — 风险
- `oos_report.json` — 样本外校准（闸门依据）
- `frontend_overview.json` — **前端总入口**

### 7. 给用户带来的价值（`value`）

① 校准过的赛事概率模型（已修巴西高估：France 18.5% / Brazil 11.2%）　② 跨 Kalshi/Polymarket 实时错价发现　③ 强制纪律——只在模型达标且真有边缘时动钱，每单硬顶 $1。

### 8. 怎么能看到这个价值（`performance` + `risk`）

- **准确度**：16 场已结算，Brier 0.7225 vs 均匀 0.6667 → 当前 **BLOCK**（模型还没达标，这是纪律在生效）
- **校准 P&L**：‑3.11u 纸面（对大热门过度自信）
- **风险**：demo 环境，Kalshi $10 / PMUS $0 / prod 永不查，0 敞口，4 条护栏全亮

---

## 前端集成

**单一数据合约**：`python -m prediction_market.ops.frontend_export` → 写 `data/output/frontend_overview.json`（含上面 8 节全部内容 + 实时 performance/risk 快照）。同步脚本把它与 `xv_*.json` / `predictions_*.json` / 两个 `.pdf` 拷入 `someo-park-investment-management/public/data/`。

> 前端「Prediction Market 模式」开发计划见 `.claude/plan/prediction market plan/16_frontend_prediction_mode.md`：点切换按钮整站反色进入预测视图（中间 artifact、Active Pairs→未来比赛、右侧 panel 换预测内容），**绝不修改任何股票策略元素**。
>
> 历史兼容：`run_model.py --emit-frontend` 仍写 `worldcup_model.json`（本项目对前端的写入仅限 `public/data/` 下的预测文件，绝不触碰其它前端文件）。

---

## 已建 vs 待接

**已建并验证**：数据层全链路（节流摄取 + SQLite + 增量）、建模引擎（强度/Dixon‑Coles/锦标赛/金靴/in‑play/集成/OOS/校准）、策略数学（de‑vig/edge/sizing/风控/跨场）、订单翻译 + 场所守卫、Kalshi/Poly US 凭证验证。

**待接（需凭证或按路线图 Demo 优先）**：Kalshi 实盘订单/WS（已对齐文档，受实盘闸约束）、`PolymarketUSVenue` 适配器 + Global 只读 reader、跨场套利执行、每小时调度、真实金靴球员速率（topscorers）。完整逐条状态见 [`PLAN_AUDIT.md`](PLAN_AUDIT.md)。
