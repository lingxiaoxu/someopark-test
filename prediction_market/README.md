<p align="center">
  <img src="../public/SOMEO PARK矢量源文件 Big Square.svg" alt="Someopark" width="160"/>
</p>

<h1 align="center">prediction_market</h1>
<p align="center"><b>世界杯 2026 × Kalshi + Polymarket 量化交易系统</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/conda-someopark__run-green?logo=anaconda&logoColor=white"/>
  <img src="https://img.shields.io/badge/venues-Kalshi%20%7C%20Polymarket-orange"/>
  <img src="https://img.shields.io/badge/model-Dixon--Coles%20%7C%20Simulation%201M-purple"/>
  <img src="https://img.shields.io/badge/pricing-probability%20%2B%20per--contract%20%C2%A2-teal"/>
  <img src="https://img.shields.io/badge/data-API--Football%20(league%3D1)-lightgrey"/>
  <img src="https://img.shields.io/badge/tests-133%20passing-brightgreen"/>
  <img src="https://img.shields.io/badge/isolated-prediction__market%2F-red"/>
</p>

---

把这套系统当作一个**小型自营交易台（prop desk）**：跨 **Kalshi + Polymarket US** 两个 CFTC 监管场所交易 2026 世界杯（48 队 / 104 场，2026‑06‑11 → 07‑19）。研究端按顶级足球分析师的标准建模，执行端按量化工程的标准做数据、风控、回测与跨场执行。

> **隔离铁律**：本项目完全自包含于 `prediction_market/` —— 读自己的 `.env`、只写自己的 `data/`、**绝不 import 根仓库任何代码**。唯一对外写入是可选的前端 JSON（`--emit-frontend`）。所有命令在 **`someopark_run`** conda 环境下运行。

设计文档：`.claude/plan/prediction market plan/`（20 个文件，00–19）；逐条实现核对：[`PLAN_AUDIT.md`](PLAN_AUDIT.md)。

---

## 三个预测品类（共用同一模拟引擎，内部一致）

| 品类 | 核心问题 | 方法 | 标的 |
|------|----------|------|------|
| **① 单场比赛** | 这一场谁赢 / 比分 / 进球 | Dixon‑Coles 双泊松比分模型（小组/淘汰分别建模）+ 赛中 in‑play 实时模型 | 3‑way 胜平负、总进球、双方进球、晋级 |
| **② 冠军** | 谁举杯 | 锦标赛模拟（48 队 2026 赛制，生产 N=1,000,000，每场结束后自动重模） | "Men's World Cup winner?" 48 互斥 outcome |
| **③ 金靴** | 谁进球最多 | 嵌套在锦标赛路径里的球员进球模拟 | Kalshi `KXWCGOALLEADER` / Polymarket 对应市场 |

三品类共用**队伍强度底座**与**同一批模拟路径**，保证冠军、晋级、金靴概率自洽。其上叠加第四类**交易**机会：**④ 跨场所相对价值 / 套利**（同一标的 Kalshi vs Polymarket US 价差）。

---

## 每合约价格（¢）+ 里程碑盯市轨迹（plan 18，新）

概率（0–1）是研究语言，但下注/结算发生在**每合约价格（cents, ¢）**上。系统现在在**每一处概率旁同步显示 ¢**，并把两者的关系讲准确：

- **单合约**：¢ = 价格 × 100（二元合约赢结算 100¢、输 0¢，定义恒等）。
- **但 ¢ ≠ 概率 × 100**：只有**我们模型**的 ¢ = 公允概率×100（三项和正好 100¢，归一化）；**场馆报价含 vig**，三个 ask 之和 >100¢（约 101–102¢），其隐含概率是**去 vig 价**（¢ ÷ 三项之和），不是 ¢÷100；且 ask≠bid（价差，展示中间价 mid）。`edge = 模型概率 − 场馆去 vig 概率`。
- **科学边界**：只有"概率对应可成交合约"的地方才配 ¢（单场 3‑way、夺冠、totals）；**纯准确度指标（Brier/log‑loss/校准）不配 ¢**（无金融含义）。

**六个里程碑盯市（mark‑to‑market）**：对每场比赛在 **PRE / 15′ / 30′ / 中场 / 60′ / 75′ / 终场** 钉下每个 outcome 的 **¢ 与概率**（Kalshi + Polymarket 双源），形成"入场 → 终场结算"的价格轨迹——验证赛前押注是否被市场逐步确认（converging / diverging）。

- **数据源（实时 + 历史均实测可用）**：Kalshi 公开行情 + candlesticks 历史；Polymarket US 实时（`PMUS_*` 凭证）+ Polymarket Global 公开 `prices-history`（`fifwc-{home}-{away}-{date}` 持久事件，可回填已结束比赛）。两源逐场吻合度 ≤1–2¢。
- **记录 + 查询双轨**：比赛进行中实时记录里程碑（GRACE 窗口防止迟启动乱码）；已结束比赛用场馆历史**回填**（`backfill_milestones`，对刚结束未归档的比赛持续重试直到历史可用）。
- **夺冠 ¢**：Kalshi `KXMENWORLDCUP` + Poly `world-cup-winner` 真实合约价并列在夺冠概率旁。
- **实盘战绩 ¢**：bet log 增加 `入场¢ / 结算¢ / 每张盈亏¢ / 累计¢` + `平均入场¢ / 价格捕获率 / 平均 CLV¢`。

> **三视图对账(单一真值)**:PnL report(PDF)、准确度&盈亏、价格轨迹的逐场输赢全部来自同一个
> `performance_report.match_pick`(小组 3-way + 淘汰晋级),**by construction 永远一致**;结算时
> `live_refresh` 同步重生成 performance(JSON+PDF)+ 里程碑,杜绝"一个变另一个没变"。

---

## 赛前下注决策模型（plan 20，新）

把原本隐式的「押概率最高(argmax)」升级成一个独立、有研究支撑的**决策模型**（`strategy/decision_model.py`，加法式接入,未改既有同源结构):

- **选边按价值,不按最可能**:`decide()` 在 PRE 场馆报价上选**去 vig 后被低估最多**的一边(模型概率 − 市场隐含)——可能是平局或弱队,而不是单纯概率最高的那个。
- **置信度定额($0.2–$2)**:`stake = clip($1×(1+k), 0.2, 2)`,k 由 **edge(分数凯利 ¼)+ 模型校准 + 近期状态 + alt-data** 加权;信心足多下、不足少下。**真实下单仍受 $1 硬顶**(`max_test_order_usd`),决策模型只给「理想额」。
- **冷门高估偏差**:对 sub‑15¢ 的 longshot 边设更高 edge 门槛(favourite‑longshot bias,arXiv 1710.02824);无安全边际则**跳过不下**。
- **平局纪律(`draw_extra_theta`,默认 0.06)**:小组赛平局率异常高(本样本 38%,模型甚至**低估**至 26%、市场更低估至 22% → 平局确有价值),但这是会在淘汰赛回归的 regime。故对平局边加一截 edge 门槛,**只砍信心最低的平局**(本样本平局注 45%→37%,命中率不降),稳健性 hedge 而非扭曲模型。诊断/调参:`ops/_diag_draw_bias`。
- **全程 PIT**:`match_pick` 传入 `conn` 时按开赛时点重算模型+form+alt-data(无未来泄漏),`_bet_log` 与 `milestone_export` 传**同一份 PRE 报价**→ pick 必然一致 → 三视图仍同源。
- **考核口径**:以 **CLV** 为首要成绩(小样本下比胜率可靠),并保留 **argmax 预测准确率**(`model_pred_accuracy`)作模型质量参考。
- **PIT 回测**:`ops.decision_backtest` 在真实结算比赛 + 真实里程碑价上实测「argmax vs 价值 vs 价值+缩放」× 6 个退出时点(T15..T75 vs 持有到 FT)+ CLV。配置在 `config.DecisionConfig`。

> 三视图(准确度&盈亏 / PnL report / 价格轨迹 ¢)与 production 每日押注**全部经过 `decide()`**;无边际场次在 bet log 中被排除、在价格轨迹标「不下注」。

---

## 对手加权 form 提准 + alt-data 控制框架（plan 19，新）

诊断:早期错误高度集中在**"热门被对手逼平"**(8/11 输注),而现有 `form_strength` 明确"不看对手强弱"。
新增**对手强度加权、攻防分离**的近期状态信号,把它喂回 Dixon-Coles 的 λ:防守强的弱旅压低对手 λ
(单一 rating 做不到的"逼平"),进攻型抬自己 λ。**纯新 alpha,不靠全局加平局**(dc_rho 不动)。

- **`model/altdata_adjust.py`**:对手加权 def/off form + xGA,**PIT**(`as_of` 截断,无前视泄漏)。
- **`model/venue_climate.py`**:16 球场静态表(海拔/高温/闭顶空调)→ 比赛级对称 λ 抑制(墨西哥城海拔主导)。
- **控制框架(都是小的、有界的、参数控制的)**:`config.py` 新增
  `oppadj_def_weight` / `oppadj_off_weight` / `xga_weight` / `venue_climate_weight` / `lineup_weight`,
  每场 λ 调整 clip 在 `±adj_log_clip`,任何信号都不会失控;**xga/venue/lineup 默认 0**(数据薄/待摄取)。
- **应用点**:`StrengthModel.adj` + `pair_lambdas`(夺冠模拟也走这条 → 一致)。
- **当前线上**:`oppadj_def=0.45, oppadj_off=0.25`(样本内调,提准 8→11/19、Brier 0.571→0.498)。
  ⚠️ **样本内优化、会部分回吐**;权重**不进 in-sample 1152 sweep**(小样本拟合过拟合),改由
  `param_sweep --walk-forward`(真 PIT walk-forward,已修 form 前视泄漏)验证,样本够大再 fit。

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
| `ingest/store.py` | 本地存储：SQLite 业务表（含 `ob_snapshot` 盘口 + **`milestone_snapshot` 6 里程碑 ¢/概率/devig**）+ append‑only 原始快照 + 增量水位 + 月度用量；`data/wc.db` |
| `util/pricing.py` | 每合约 ¢ 换算（纯展示层，不改任何模型数值）：`to_cents`/`mid`（缺边回退）/`quote_to_cents`(ask_c/bid_c/mid_c)/`model_cents`/`settle_cents`/`pnl_cents` |
| `util/price_history.py` | 历史价格采样：`price_at(series, when_ts)` 取离里程碑时刻最近的 bar（含 gap 容差），喂回填/盯市 |
| `ingest/soccer_ingest.py` | 摄取编排：watermark/TTL 闸（TTL 内重跑 0 请求）、幂等 upsert、coverage 感知；`--scope {static\|results\|live\|h2h\|squads\|all}`。`live`/`results` 每场拉取 **xG + 阵型(lineup) + 逐球员战绩(fixture_player_stats) + 盘中庄家赔率(odds/live)**，喂给数据挖掘战术 |

### 模型层（`model/`）

| 文件 | 说明 |
|------|------|
| `model/strength.py` | 队伍强度底座：FIFA 排名 → 评分，**反解拟合**到先验期望积分（坐标下降 + Dixon‑Coles 解析） |
| `model/dixon_coles.py` | 单场内核：双泊松 + 低分相关修正 → 比分矩阵 → 胜平负/总进球/双方进球/晋级（含加时+点球） |
| `model/tournament.py` | 锦标赛模拟（2026 48 队赛制，best‑8‑thirds），向量化 → 冠军/晋级/各轮/E[场次]（快查 50k；生产 1M） |
| `model/golden_boot.py` | 金靴嵌套模拟：球员进球与球队走多远相关（同一批路径），Poisson(μ×已打场次) |
| `model/inplay.py` | 赛中实时模型（分钟+比分+红牌 → 实时胜平负、公平平局价、剩余进球），驱动赛中交易 |
| `model/match_pricing.py` | 单场定价：从比分矩阵导出任意单场市场（小组 72 场全量） |
| `model/ensemble.py` | 集成：参数变体 → 概率均值 + **离散度**（替换占位 sigma，喂仓位） |
| `model/calibrate.py` | 校准/评分：Brier / Log‑loss / 可靠性曲线 / CLV / bootstrap CI |
| `model/oos_eval.py` | OOS 体检：冻结赛前模型对已打比赛打分，查系统性偏差（放真钱前门禁） |
| `model/run_model.py` | 编排器：先验 → 强度 → 锦标赛 → 金靴 → 单场定价 → 前端 JSON；含**夺冠¢**注入；`--full` / `--ensemble` / `--emit-frontend` |
| `model/altdata_adjust.py` | **alt-data λ 调整(plan 19)**:对手强度加权 def/off form + xGA,z 标准化,**PIT**(`as_of`)。挂到 `StrengthModel.adj`,由 `pair_lambdas` 按权重 + clip 应用 |
| `model/venue_climate.py` | 16 球场静态气候表(海拔/高温/闭顶空调)→ 比赛级对称 λ 抑制(更多平局),`venue_climate_weight` 控制、默认 0 |

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
| `venues/polymarket_global/reader.py` | Polymarket Global 只读 reader（Gamma/CLOB/Data，免凭证）：盘口去 vig + **`prices-history` 逐分钟历史** + `list_wc_match_events`（持久 `fifwc-*` 事件，open+closed 全覆盖，回填已结束比赛） |
| `jobs/hourly_job.py` | 每小时编排：增量摄取 → 强度+赛果更新 → 模型 → 跨场监控 → OOS → 结构化日志（`--dry-run`/`--loop`） |
| `venues/base.py` | 统一 `Venue` / `ExecutionVenue` 接口 + `OrderBook`/`Balance`/`Position` 类型 |
| `venues/guard.py` | `venue_guard`：执行只允许 Kalshi/Poly US（拦截 Global）；**实盘交易硬闸**（prod/真钱 须显式授权） |
| `venues/kalshi/auth.py` | Kalshi RSA‑PSS 签名（毫秒时间戳，签 `ts+METHOD+path` 去 query） |
| `venues/kalshi/market_data.py` | Kalshi 公开行情读取器（免鉴权）：`best_prices` 双边 ask + 深度 + **`candlesticks` 逐分钟历史**（按 ticker，绕过只列 open 的事件索引） |

### 运维 / 报告 / 前端层（`ops/` `jobs/`）

| 文件 | 说明 |
|------|------|
| `ops/schedule.py` | 赛程查看器：按美东 ET + 美西 PT 双时区列出未来比赛（跨午夜正确处理）；`--upcoming` / `--days N` / `--refresh` |
| `ops/monitor.py` | 健康报告：模型新鲜度、API 预算、校准状态、跨场价差、错误率 → `health.json` |
| `ops/performance_report.py` | **收益/准确度报告**：已结算场次 Brier/Log‑loss/命中率 + 纸面校准 P&L；`--pdf` 输出机构风格 PDF（沿用 `PnLReport.py` 字体/配色） |
| `ops/risk_report.py` | **风险报告**：交易闸门、仓位限额、各场余额（prod key 永不查）、敞口、API 预算、校准闸门、护栏；`--pdf` 同款 PDF |
| `ops/pdf_style.py` | PDF 样式模块（自包含复制根仓库报告风格：PingFang CJK 字体、深蓝表头+金线、隔行底纹、盈亏红绿；**不 import 根代码**） |
| `ops/system_overview.py` | 静态系统目录（接口/模式/调度/输入输出/价值）的单一数据源，供 PDF 与前端共用 |
| `ops/upcoming_export.py` | **逐场跨场报价**：对未来比赛真实拉取 Kalshi（公开）+ Polymarket US（读凭证）单场 3‑way（ask/bid + **¢**）+ 去 vig + 模型边缘 + 跨场锁定套利 → `upcoming.json`；临近开赛落库 PRE 里程碑入场价（只读，绝不下单） |
| `ops/inplay_export.py` | **盘中导出**：每场 live 模型 3‑way + xG + 剩余进球 + 每个 outcome 的市价¢（Kalshi/Poly）+ 盘中机会（市价¢/公允¢/edge¢）→ `inplay_live.json` |
| `ops/milestone_export.py` | **价格轨迹**：聚合 `milestone_snapshot` → 每场 6 里程碑的 ¢+概率双口径 + 我们的赛前选边 + 入场→终场盯市（MTM）→ `milestone_marks.json` |
| `ops/backfill_milestones.py` | **历史回填**：用 Poly Global 持久事件 `fifwc-{h}-{a}-{date}` + CLOB `prices-history` 重建已结束比赛的 6 里程碑轨迹（date+队名匹配，含重音/别名）；REPLACE 自愈乱码行，缺 FT 行则重试 |
| `ops/live_refresh.py` | **盘中每周期刷新**（launchd 30s，窗口外低成本空转）：同步 live/赛果 → 重建 inplay/upcoming/xv/oos → 捕获里程碑（GRACE 窗）+ 回填 + 导出 → 写本地 + 前端目录（经 Cloudflare tunnel 实时上线，无需重新部署前端） |
| `ops/refresh_all.py` | **全量重生成**：把前端读的每个导出在当前样本上重算（含 milestone_marks）+ 重跑夺冠模拟（1M） |
| `ops/frontend_export.py` | **前端数据合约**：静态目录 + 实时快照（performance/risk/预测/upcoming）→ 单一 `frontend_overview.json`，前端读这一个文件即可 |
| `venues/champion_prices.py` | 夺冠盘真实合约价：Kalshi `KXMENWORLDCUP`（inline ask）+ Poly Global `world-cup-winner`（inline outcomePrices）→ 映射到 canonical team_id，失败容错 |
| `jobs/live_poller.py` | 盘中每分钟轮询：live 公允价 + 跨场套利 + 战术 → `inplay_signals.json` |
| `strategy/inplay_tactics.py` | 盘中战术库：基础(平局/收敛/动量/总进球)+ 事件(进球过反应/落后夺冠/红牌/淘汰赛平局)+ **8 个数据挖掘战术**(闷平爆发/临门修正/应得未得/无效控球/阵型脆弱/单点失效/晚段进球/庄家交叉验证，源自 26 场 intra-game 研究，见 `docs/INPLAY_FINDINGS.md`)。OVER/UNDER 类信号接入 **Kalshi `KXWCTOTAL` + Poly US `tsc-fwc-*` 总进球盘**实时价(`venues/*/discovery.py:totals_quotes`)：闷平/阵型/晚段附市场价,临门修正(#9)在模型已看高于市场时激活,并产出 totals relative-value |

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

## 数据存储结构（本地落盘）

> 设计原则：**单一真相源 + 派生产物 + 原始快照可回放**。所有动态数据都只落在
> `prediction_market/data/` 下（git 不跟踪）；备份只需 `wc.db` 一个文件，其余都能从它重算。
> 行数为某时点快照，随每日运行增长。

### 一、存储总览

```
prediction_market/
├── .env                    ← 密钥(API_FOOTBALL_KEY、Kalshi/Poly 凭据) ★永不入库
└── data/
    ├── wc.db   (~10 MB)    ★ 唯一真相源 (SQLite, WAL 模式)
    │   ├── wc.db-wal       预写日志(未 checkpoint 的写入)
    │   └── wc.db-shm       共享内存索引
    ├── output/  (~5 MB)    派生产物:前端用的 JSON + PDF(从 wc.db 计算导出)
    ├── raw/     (~215 MB)  原始 API 响应快照(append-only,可回放/审计)
    ├── priors/             手工种子:ext_sim_v0.json、seed_players.json (★唯一入库的数据)
    ├── logs/    (~6 MB)    运行日志 + 盘中复盘 jsonl(inplay_review_*.jsonl)
    └── kalshi_docs/        Kalshi API 文档缓存

someo-park-investment-management/public/data/   ← 前端服务层(output 的镜像子集,
                                                   经 Cloudflare tunnel 实时上线)
~/.config/someopark/*.key   ← Kalshi PEM 私钥(★仓库外,永不进 git)
```

### 二、`wc.db` 表结构（按逻辑分组）

| 分类 | 表 | 示例行数 | 内容 |
|------|----|---------|------|
| **原始/参考** | `team` `team_meta` `venue` `standing` | 48/48/3/48 | 球队、规范名映射、场馆、积分榜 |
| | `player` `squad` `fc_player` | 1248/1248/9853 | 球员、阵容、EA FC26 评分(金靴/强度用) |
| **赛程/比分/事件** | `fixture` | 72 | 全部 72 场赛程 + 比分 + 状态 |
| | `fixture_event` | 391 | 逐分钟 进球/红黄牌/换人 |
| | `nt_recent` `h2h` | 300/0 | 国家队近期战绩、交锋史 |
| **盘中细粒度** | `fixture_stats` | 52 | 每队 xG/射门/控球/角球(live + 终场) |
| | `lineup` | 54 | 阵型/首发/教练 |
| | `fixture_player_stats` | 1340 | **每球员每场** 评分/射门/传球/过人/对抗/抢断/犯规 |
| | `injury` | 0 | 伤停(WC 此 endpoint 暂无数据) |
| **市场/赔率** | `match_odds` | 917 | 庄家 1X2 去 vig(含盘中 `live_consensus` 实时盘口) |
| | `prediction` | 5 | API-Football 自带预测 |
| | `ob_snapshot` `xref` | 0/0 | 订单簿快照、跨场标的映射(预留) |
| **模型输出** | `sim_champion` `sim_golden_boot` | 2400/10274 | 蒙特卡洛 夺冠/金靴 分布 |
| | `model_run` `xv_spread` `milestone_snapshot` | 50/144/183 | 每次模型运行、跨场价差、里程碑快照 |
| | `player_stat` `calibration` `signal` | 1246/0/0 | 球员赛季统计、校准、信号(后两者按需) |
| **API 审计** | `api_call` `raw_index` `watermark` | 7228/7228/5 | 每次 API 调用日志、快照索引、TTL 水位 |

盘中细粒度四表 + `live_consensus` 实时盘口由 `ingest/soccer_ingest.sync_live` / `sync_results`
每场拉取,喂给数据挖掘盘中战术(见 `strategy/inplay_tactics.py`、`docs/INPLAY_FINDINGS.md`）。

### 三、`data/output/` 主要派生产物（前端消费）

| 文件 | 内容 |
|------|------|
| `latest.json` / `model_run_*.json` | 每次全量模拟结果(夺冠/晋级/各轮) |
| `frontend_overview.json` | 前端系统总览(接口/模式/频率/价值) |
| `upcoming.json` / `schedule.json` | 赛前卡片(决策+argmax+form) / 赛程 |
| `inplay_live.json` / `match_signals.json` / `inplay_signals.json` | 盘中实时模型 + 机会 + 信号 |
| `milestone_marks.json` | 每合约 ¢ 里程碑盯市轨迹 |
| `reach_round.json` | 晋级盘(48 队 × 5 轮,模型%/Kalshi¢/Poly¢/边缘) |
| `form.json` / `squad.json` | 对手加权 form / 队伍强度(联赛加权) |
| `performance_report.json` + `.pdf` / `risk_report.json` + `.pdf` | 绩效 / 风控报告 |
| `xv_champion.json` / `xv_matches.json` | 跨场冠军/单场比价 |
| `calibration.json` / `oos_report.json` / `backtest.json` / `param_sweep.json` | 校准 / OOS / 回测 / 参数扫描 |

### 四、`data/raw/` 原始快照（按 endpoint 分目录，可回放）

每次 API 调用的原始 JSON 落盘,文件名 `时间戳_参数哈希.json`,与 `api_call`/`raw_index` 一一对应:

```
fixtures(~176M)  odds(~15M)  players(~9M)  fc26(~7M)  fixtures_statistics(~4M)
fixtures_players  fixtures_lineups  odds_live  injuries  predictions  standings
teams  leagues  players_squads  players_topscorers  status
```

### 五、数据流向

```
API-Football ──(billed, 审计到 api_call)──► raw/*.json (原始)
                                              │
                                              └─► 解析 + 幂等 upsert ─► wc.db (真相源)
                                                                         │
                                  ops/*_export.py 计算导出 ◄─────────────┘
                                                │
                                data/output/*.json + *.pdf
                                                │  (live_refresh 镜像)
                                public/data/*.json ──(Cloudflare tunnel)──► someopark.web.app
```

### 六、版本库 vs 本地（`.gitignore`）

- **入库**:只有代码 + `data/priors/`(手工种子)。
- **不入库(纯本地)**:`wc.db`、`data/output/`、`data/raw/`、`data/logs/`、`.env`、`*.pem` / `*.key`。
- **备份**:核心是 `wc.db` 一个文件(其余都能从它重算,`raw/` 仅作回放/审计)。

---

## 测试

```bash
conda run -n someopark_run --no-capture-output python -m pytest prediction_market/tests/ -q
```

162 passing：先验校验、Dixon‑Coles、强度标定、锦标赛/金靴分布、in‑play、校准、OOS、集成、de‑vig/edge/sizing、风控、跨场套利、订单翻译、场所守卫、Kalshi 签名/盘口解析、数据层（store/预算/解析）、运维报告（performance/risk 报告 + PDF 渲染 + 前端 export 合约）、**每合约 ¢ 体系**（pricing 换算/历史采样/candlestick 解析/夺冠¢映射/里程碑捕获幂等+GRACE 窗/milestone 导出/PnL¢ 对账）、**alt-data 层**（对手加权 form PIT/默认零权重 no-op 不变 prod/clip 有界/价格轨迹↔bet log 对账一致），以及 **8 个数据挖掘盘中战术**（闷平爆发/临门修正/应得未得/无效控球/阵型脆弱/单点失效/晚段进球/庄家交叉验证，各含正例+反例,锚定真实比赛）。

---

## 设计文档索引（`.claude/plan/prediction market plan/`）

| 文件 | 内容 |
|------|------|
| `00_README_总览` | 三品类、架构、场所机制、合规、OOS |
| `01–05` | Kalshi 对接 / 数据管道 / 建模 / 策略执行 / 工程运维 |
| `06–10` | 路线图 / Polymarket 对接 / 跨场策略 / 场所微结构规则 / 球队先验全量 |
| `11` | **API‑Football 对接**（官方世界杯指南 + 本项目实现） |
| `12` | **Kalshi Trade API 深度摘要**（全文精读 → 环境/鉴权/定点价/下单V2/限速/WS/映射） |
| `13–16` | 跨场监控 / 前端集成 / 赌球边缘+盘中战术 / 前端 Prediction 模式 |
| `17` | 建模研究 + 参数搜索（7 旋钮 OOS 扫描，FC/squad/form 权重） |
| `18` | **每合约价格(¢) + 里程碑盯市轨迹**（双口径展示 + vig/devig + 6 里程碑 + Poly/Kalshi 历史回填 + 夺冠¢ + PnL¢ + 三视图对账） |
| `19` | **对手加权 form 提准 + alt-data 控制框架**（对手强度加权攻防 form + xGA + 球场气候,参数控制有界,PIT walk-forward 验证） |

---

## 系统总览 — 接口 / 模式 / 频率 / 价值（前端搬运清单）

> **单一数据源**：`python -m prediction_market.ops.frontend_export` → 生成 `data/output/frontend_overview.json`，前端直接读这一个文件即可，无需在客户端重写任何逻辑。下面每一节都对应该 JSON 的一个 key。
>
> **诚实结论**：系统现为「只看不买」状态——纪律闸门（calibration gate）在主动拦截：模型在已结算小组赛上 Brier 仍劣于均匀基线（0.667），尚未达到可交易等级，故拒绝下任何真钱单。宁可不交易，也不拿没验证过的边缘去亏钱。

### 1. 接口（`interfaces`，15 条 CLI）

| 类别 | 命令 `python -m prediction_market.<x>` | 作用 |
|------|----------------------------------------|------|
| 数据 | `ingest.bootstrap` | 一次性拉全量（球队/球员/对阵/赛程），建增量水位 |
| 数据 | `ingest.refresh` | 增量刷新（赛果、比分、live 状态） |
| 预测 | `model.match_pricing` | 单场 3‑way 公允价（主/平/客），含点球大战建模 |
| 预测 | `model.tournament` | 模拟冠军概率（48 队） |
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

- `worldcup_model.json` — 冠军/晋级/金靴（含**夺冠 Kalshi/Poly ¢**）
- `xv_champion.json` / `xv_matches.json` — 冠军概率 / 赛前偏离
- `upcoming.json` — **逐场跨场报价**（模型 + book + 真实 Kalshi/Poly US ask/bid + **¢** + devig + 边缘 + 锁定套利）
- `inplay_live.json` — 盘中 live 模型 + 市价¢ + 机会（edge¢）
- `milestone_marks.json` — **价格轨迹**：每场 6 里程碑 ¢+概率 + 入场→终场盯市（MTM）
- `inplay_signals.json` — 盘中套利/战术
- `performance_report.json` + `.pdf` — 收益/准确度（含**每合约 ¢ 盈亏 + 捕获率 + CLV**）
- `risk_report.json` + `.pdf` — 风险
- `oos_report.json` — 样本外校准（闸门依据）
- `frontend_overview.json` — **前端总入口**

### 7. 给用户带来的价值（`value`）

① 校准过的赛事概率模型（每场结束后重模，1M 路径）　② 跨 Kalshi/Polymarket 实时错价发现，**概率 + 每合约 ¢ 双口径**（含 vig/去 vig）　③ **6 里程碑盯市轨迹**：赛前押注入场 ¢ → 终场结算的实盘验证　④ **赛前下注决策模型**：价值选边 + 分数凯利 + 置信度定额（$0.2–$2），CLV 为首要成绩　⑤ 强制纪律——只在模型达标且真有边缘时动钱，每单硬顶 $1。

### 8. 怎么能看到这个价值（`performance` + `risk`）

> 下列数字随每场结束**动态更新**（已结算样本自动增长，模型每场后重模），以面板实时数为准。

- **准确度**：已结算约 18–19 场，原始 Brier vs 均匀基线 0.6667 + **校准后** Brier（交易等级以校准后为准）→ 当前仍 **BLOCK**（纪律在生效，未达标不动真钱）
- **实盘战绩(决策模型)**：押**最被低估**边、按置信度 $0.2–$2 定额的 track record（W‑L、$ PnL、ROI、跳过数）+ **每合约 ¢ 口径**（累计¢、平均入场¢、价格捕获率、CLV¢）；另列 argmax **模型预测准确率**作模型质量参考
- **校准 P&L**：纸面公允赔率诊断（过度/不足自信）
- **风险**：demo 环境，Kalshi $10 / PMUS / prod 永不查，0 敞口，护栏全亮，每单硬顶 $1

---

## 前端集成

**单一数据合约**：`python -m prediction_market.ops.frontend_export` → 写 `data/output/frontend_overview.json`（含上面 8 节全部内容 + 实时 performance/risk 快照）。`refresh_all` / `live_refresh` 把所有导出（含 `milestone_marks.json`）同步到 `someo-park-investment-management/public/data/`。

> **实时上线管线**：本地 Express（`:3001`，serve `public/data/*` + `/api`）经 **Cloudflare tunnel** 供给线上站点；`live_refresh`（launchd 30s）持续刷新 JSON → **数据改动无需重新部署前端即实时上线**。仅前端代码（组件/视图）变更才需 `npm run build` + firebase deploy。
>
> **前端 Prediction 模式**（plan 16）：点切换按钮整站反色进入预测视图，**19 个 dashboard 视图**（夺冠概率 / 单场定价 / 金靴 / 盘中套利 / **价格轨迹(¢)** / 实盘战绩 / 模型vs市场 / 校准 / 参数搜索 / 风险 …），每处概率旁同步显示 ¢，**绝不修改任何股票策略元素**。
>
> **Someo Agent**（前端 chat，预测模式默认开启）：5 个只读工具（`get_prediction_market` 含 `pricetrack` view、`get_wc_team`、`get_wc_match`、`compare_wc_teams`、`get_wc_track_record`）读全部结构化数据；prompt 知识涵盖 ¢/概率/vig/devig/里程碑（与股票四策略对等）。
>
> 历史兼容：`run_model.py --emit-frontend` / `refresh_champion` 仍写 `worldcup_model.json`（本项目对前端的写入仅限 `public/data/` 下的预测文件，绝不触碰其它前端文件）。

---

## 已建 vs 待接

**已建并验证**：数据层全链路（节流摄取 + SQLite + 增量）、建模引擎（强度/Dixon‑Coles/锦标赛 **1M**/金靴/in‑play/集成/OOS/校准）、策略数学（de‑vig/edge/sizing/风控/跨场）、订单翻译 + 场所守卫、Kalshi/Poly US 凭证验证、**每合约 ¢ + 6 里程碑盯市轨迹**（双源实时+历史、夺冠¢、PnL¢、价格轨迹视图、三视图对账）、**对手加权 form alt-data 层**（提准 8→11/19、参数控制有界、PIT walk-forward 验证）、Polymarket Global 只读 reader（**已用于逐分钟历史回填**）、launchd 30s 盘中刷新 + Cloudflare tunnel 实时上线、前端 Prediction 模式 + Someo Agent 工具（19 个 dashboard 视图，懂 ¢/概率/vig/devig）。

### 定时调度（3 个 launchd job,无 cron）

| Job | 频率 | 跑什么 | 跑 sweep? |
|-----|------|--------|----------|
| `predictionlive` | **30s** | `live_refresh.sh`:盘中刷新 + **结算时**重算夺冠(1M)/绩效(JSON+PDF)/里程碑回填(经 tunnel,不部署) | ❌ |
| `predictionmatchtrigger` | **15min** | `refresh_and_deploy.sh --trigger`:有新赛果才跑全量 `refresh_all` + sync + build + firebase 部署 | ❌ |
| `predictionrefresh` | **每日 06:30** | `refresh_and_deploy.sh`:全量 pipeline + 部署 + **时间隔离(sleep 60s)后跑 1152 sweep** | ✅(仅此) |

> 完整 pipeline(`refresh_all`)重生成**所有**导出:JSON 全套 + `milestone_marks`(价格轨迹)+ **两个 PDF** +
> `xv_champion`;1152 sweep **只在每日 06:30 跑**(慢、时间隔离),其余两个 job 不跑。`param_sweep.json`
> 带 `generated_at` 时间戳,前端"参数搜索"视图显示"上次更新"。

**待接（需凭证或按路线图 Demo 优先）**：Kalshi 实盘订单/WS（已对齐文档，受实盘闸约束）、跨场套利执行（真钱）、真实金靴球员速率（topscorers 已接，持续校准）。完整逐条状态见 [`PLAN_AUDIT.md`](PLAN_AUDIT.md)。
