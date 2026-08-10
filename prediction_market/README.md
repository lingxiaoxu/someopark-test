<p align="center">
  <img src="../public/SOMEO PARK矢量源文件 Big Square.svg" alt="Someopark" width="160"/>
</p>

<h1 align="center">prediction_market</h1>
<p align="center"><b>世界杯 2026 预测市场量化系统 · Kalshi + Polymarket</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/conda-someopark__run-green?logo=anaconda&logoColor=white"/>
  <img src="https://img.shields.io/badge/venues-Kalshi%20%7C%20Polymarket-orange"/>
  <img src="https://img.shields.io/badge/model-Dixon--Coles%20%7C%20MC%201M-purple"/>
  <img src="https://img.shields.io/badge/matches-104%20%2F%20104%20settled-teal"/>
  <img src="https://img.shields.io/badge/tests-261%20passing-brightgreen"/>
  <img src="https://img.shields.io/badge/mode-paper%20only-red"/>
</p>

---

把这套系统当作一张**小型自营交易台**：跨 **Kalshi** 与 **Polymarket US** 两个受 CFTC 监管的场所，为 2026 世界杯（48 队 / 104 场 / 2026-06-11 → 07-19）报出自己的概率与合约价，与市场比对后下注。研究端按足球分析的标准建模，执行端按量化工程的标准做数据、风控、回测与跨场所执行。

赛事已于 **2026-07-19** 结束（决赛 **西班牙 1–0 阿根廷**），104 场全部结算。本文档同时是**赛后复盘**：所有战绩数字都来自 `data/output/performance_report.json`，可复现。

> **隔离铁律**：本项目完全自包含于 `prediction_market/` —— 读自己的 `.env`、只写自己的 `data/`、
> **绝不 import 根仓库任何代码**。唯一对外写入是可选的前端 JSON（`--emit-frontend`）。
> 与宏观系统 [`prediction_market_macro/`](../prediction_market_macro/) 平行且互不 import。

> **⚠ 当前状态：纸面（paper）模式。** 两个场所的下单开关都硬默认关闭
> （`KALSHI_TRADING_ENABLED` / `PMUS_TRADING_ENABLED`），单笔硬上限 $1，
> 除了一笔"下单即撤单"的 demo 连通性测试外没有真实成交。见 [§ 交易状态](#交易状态与实盘纪律)。

设计文档：`.claude/plan/prediction market plan/`；逐条实现核对：[`PLAN_AUDIT.md`](PLAN_AUDIT.md)。

---

## 四类机会，一套模拟引擎

| 类别 | 核心问题 | 方法 | 标的 |
|---|---|---|---|
| **① 单场比赛** | 这一场谁赢 / 进几个 | Dixon-Coles 双泊松比分模型（小组 / 淘汰分别建模）+ 赛中实时模型 | 3-way 胜平负、大小球、双方进球 |
| **② 晋级与夺冠** | 谁出线 / 谁举杯 | 锦标赛蒙特卡洛（2026 新赛制 48 队 12 组 + 最佳 8 个第三名），生产 N = 1,000,000 | `KXWCGROUPQUAL` / `KXWCROUND` / `KXMENWORLDCUP` |
| **③ 金靴** | 谁进球最多 | **嵌套在同一批锦标赛路径里**的球员进球模拟 | `KXWCGOALLEADER` |
| **④ 跨场所** | 同一标的两边价差 | 锁定套利 / 相对价值 / 荷兰锁 | Kalshi vs Polymarket US |

三个预测品类共用**同一个队伍强度底座**和**同一批模拟路径**——这不是省算力，而是自洽性要求：夺冠概率、晋级概率、金靴概率必须来自同一个世界，否则它们之间会出现自相矛盾的套利。

---

## 概率 ≠ 合约价：¢ 口径

概率（0–1）是研究语言，但下注与结算发生在**每合约价格（cents, ¢）**上。系统在每一处概率旁同步显示 ¢，并把两者的关系讲准确：

- **我们的模型**：¢ = 公允概率 × 100，三项和恰好 100¢（归一化）。
- **场馆报价含 vig**：三个 ask 之和 > 100¢（实测约 101–102¢），其隐含概率是**去 vig 价**（`¢ ÷ 三项之和`），**不是 `¢ ÷ 100`**；且 ask ≠ bid。
- **`edge = 模型概率 − 场馆去 vig 概率`**。
- **科学边界**：只有"概率对应可成交合约"的地方才配 ¢。**纯准确度指标（Brier / log-loss / 校准曲线）不配 ¢**——它们没有金融含义。

### 里程碑盯市

每场比赛在六个里程碑 **PRE / 15′ / 30′ / 中场 / 60′ / 75′** 加上**终场**，共七个点位上钉下每个 outcome 的 ¢ 与概率（Kalshi + Polymarket 双源），形成"入场 → 结算"的价格轨迹，用来验证赛前判断是否被市场逐步确认（converging / diverging）。比赛进行中实时记录（带 GRACE 窗口防迟启动乱码）；已结束的比赛用场馆历史**回填**（`ops/backfill_milestones.py`，对刚结束尚未归档的比赛持续重试）。当前 725 条里程碑快照中，Polymarket 覆盖全部 725 条，Kalshi 只有 24 条（WC 合约在 Kalshi 上挂牌晚且薄）。在这 **24 条双源重合**的样本上，两源同一 outcome 的 ask 平均差 **0.2–0.3¢、最大 2¢**——这是真正交叉验证过的部分；其余 701 条是 Polymarket 单源，不应被读成"双源互证"。

> **三视图对账（单一真值）**：PnL 报告（PDF）、准确度与盈亏、价格轨迹，三者的逐场输赢
> 全部来自同一个 `performance_report.match_pick`，**by construction 永远一致**。
> 结算时 `live_refresh` 同步重生成 performance（JSON + PDF）与里程碑，杜绝"一个变了另一个没变"。

---

## 环境配置

### 1. Python 环境

```bash
conda activate someopark_run
```

### 2. 密钥（`prediction_market/.env`，gitignored）

| 变量 | 用途 |
|---|---|
| `API_FOOTBALL_KEY` | API-Football（league=1 世界杯）：赛程、比分、事件流、阵容、赔率、球员统计 |
| `KALSHI_*` | Kalshi 鉴权（RSA-PSS）。**demo 与 prod 分开两套 key** |
| `PMUS_*` | Polymarket US 鉴权（Ed25519） |
| `KALSHI_TRADING_ENABLED` / `PMUS_TRADING_ENABLED` | 下单总开关，**硬默认 `false`** |
| `FIREBASE_TOKEN` | 无人值守部署前端（`firebase login:ci` 生成一次） |

> Polymarket Global 是**公开只读**源，不需要凭证，仅用于历史价格回填与交叉验证；
> `venues/guard.py` 会拦截任何指向 Global 的下单意图。

### 3. 运行所有脚本的正确方式

```bash
cd /Users/xuling/code/someopark-test && \
  set -a && source .env && source prediction_market/.env && set +a && \
  conda run -n someopark_run --no-capture-output \
  python -m prediction_market.model.run_model --full --ensemble --emit-frontend
```

> **两个 `.env` 都要 source**：根 `.env` 提供 Polygon/通用 key，`prediction_market/.env` 提供
> API-Football 与场馆凭证。只 source 根目录会静默导致场馆价一栏全空。

---

## 目录与核心文件

### `ingest/` — 数据接入（6 文件 / 1,868 行）

| 文件 | 说明 |
|---|---|
| `api_football.py` | API-Football 客户端。**每次调用都记账**（`api_call` 表，38,291 条），带日/月预算上限与分页控制 |
| `soccer_ingest.py` | 赛程 / 比分 / 事件流 / 阵容 / 逐球员统计 / 赔率 → SQLite。`--scope results\|live\|form` 分档，日常只拉增量 |
| `prior_ingest.py` | 赛前先验：12 组 × 4 队、每队 10 万次晋级模拟、FIFA 排名、期望积分。加载时按 ±2pp 的恒等式容差校验 |
| `fc_ingest.py` | EA FC26 球员评分 → 阵容强度（9,853 名球员） |
| `store.py` | SQLite 封装 + `watermark` 增量水位 + 原始快照 `raw_index`（append-only，可完整重放） |

**数据规模**（`data/wc.db`，36 张表）：Kalshi 逐笔成交 **282 万条**、价格 tick **95.9 万条**（另有 3.3 万条细粒度 + 7,692 条晋级市场）、金靴模拟 5.9 万、夺冠模拟 1.26 万、里程碑快照 725、赔率 1,381、比赛事件 1,640、跨场所价差 144。`data/raw/` 另存约 **1.3 GB** append-only 原始 API 快照（`raw_index` 38,291 条索引，可完整重放）。

### `model/` — 建模（24 文件 / 4,028 行）

从数据到一个比赛概率，走这条链：

```
先验(12组×4队, 10万次/队)  ──┐
FIFA 排名 / 期望积分         ─┤
近期战绩 (贝叶斯更新+时间衰减) ─┼──→  strength.py  逆解攻防强度 λ
对手加权攻防形态 / xGA        ─┤
EA FC26 阵容评分             ─┘
                                      │
                                      ▼
                      dixon_coles.py  双变量泊松（低比分相关系数 ρ）
                                      │
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
        单场 3-way / 大小球     tournament.py 1M 路径     golden_boot.py
        + inplay.py 赛中实时     → 晋级/夺冠概率          （嵌套在同一批路径里）
              │                       │                       │
              └───────────────────────┴───────────────────────┘
                                      ▼
                    ensemble.py  12 个参数变体 → 均值 + **离散度 σ**
                                      ▼
                    calibrate.py 温度标定 → oos_eval.py Brier 闸门
```

| 文件 | 说明 |
|---|---|
| `strength.py` | **逆解**：把先验的期望积分反推成 Dixon-Coles 的攻防参数（最小化先验与模拟期望积分之差）。λ_主 = 主队攻 × 客队防 × 主场优势 |
| `dixon_coles.py` | 双变量泊松比分模型，ρ 修正低比分格（0-0 / 1-1 在足球里显著高于独立泊松） |
| `match_pricing.py` | 从比分分布里读出所有可交易 outcome：3-way、大小球、双方进球、晋级 |
| `tournament.py` | 2026 新赛制向量化模拟：12 组循环赛 → 官方 tie-break（净胜球、进球数）→ **最佳 8 个第三名** → 淘汰赛树 |
| `penalties.py` / `knockout_bracket.py` | 淘汰赛 90′ → 加时 → 点球的分段建模；90′ 平局概率按 `knockout_lambda_factor` 上调 |
| `golden_boot.py` | 在**同一条**锦标赛路径里累计球员进球（泊松），因此金靴概率天然与该队晋级深度一致 |
| `inplay.py` | 赛中实时模型：分钟 + 比分 + xG + 红牌 → 更新 λ、胜平负、公允平局价、剩余期望进球 |
| `inplay_advance.py` | 淘汰赛赛中的 **2-way 晋级模型**（不是 90′ 比分，而是"谁最终过关"，含加时点球） |
| `motivation.py` | **小组赛出线心理学**对 λ 的微调，两个效应：① 强队（FIFA 前 24）首轮丢分后对阵较弱且非东道主的对手时全力出击（攻 λ ×1.12、对手 ×0.96）；② 已提前出线的队末轮小幅轮换（×0.93）。**只在实时下注路径生效**（`upcoming_export` / `performance_report`），淘汰赛与第 1 轮自动 no-op；校准 / OOS / 交易闸门一律跑**不带动机的裸模型** |
| `altdata_adjust.py` | 对手加权攻防形态（PIT，不看未来）。**当前实际生效的两个权重**：`oppadj_def=0.45`、`oppadj_off=0.25`（19 场调优：命中 8→11/19，Brier 0.571→0.498） |
| `ensemble.py` | 扰动结构参数生成多个变体各跑一遍 → 均值 + **跨变体标准差**。模块默认 16 个变体，**生产路径 `run_model.py --ensemble` 实跑 12 个**、每个 `max(15000, n_sims/4)` 条路径（用变体数换单变体精度）。这个 σ 不是装饰：它替换了 sizing 层原本的占位符，模型内部分歧越大、下注越小、门槛越高 |
| `calibrate.py` / `probability_calibration.py` / `oos_eval.py` | 温度标定 + Brier / log-loss / 可靠性曲线 / CLV / bootstrap 95% CI |
| `run_model.py` | 编排入口：`--full`（1M 路径）/ `--ensemble` / `--emit-frontend` / `--club-blend` |

### `strategy/` — 定价、下注与赛中战术（17 文件 / 4,384 行）

| 文件 | 说明 | 关键常数 |
|---|---|---|
| `devig.py` | 三种去 vig：乘法（线性）、幂（修正冷热门偏差）、Shin（长尾） | |
| `edge.py` | `p_eff = p_model − k·σ` 收缩后与市场去 vig 价比较，扣费扣滑点 | `shrink_k=1.0` |
| `sizing.py` | 分数 Kelly | `kelly_fraction=0.25`；单市场 ≤ 5% 本金、单主题 ≤ 10% |
| `decision_model.py` | **选边规则**：选 `模型 − 市场去vig` 最大且过阈值的那一边；最优边不过阈值则退到次优边 | `min_net_edge=0.03`；**平局额外加码 `draw_extra_theta=0.06`** |
| `smart_exit.py` | **模型感知的中途兑现**：市场价高出实时公允 8¢ 即锁定，不等终场 | `OVERSHOOT_MARGIN=0.08`，比赛时钟 ≤ 95′ |
| `inplay_tactics.py` | 17 条赛中信号（下表） | 见下 |
| `inplay_confidence.py` | 信号置信分层与闸门：领先方 +、Top-10 强队 +、直传型球队 −16%、弱队 −40% | 🟢 ≥80 / 🟡 50–80 / 🔴 <50 |
| `inplay_hedge.py` | **纯数学、零副作用**的对冲解算：`break_even_b` 保本手数、`maximin_hedge` 极小化最坏情形、`full_hedge_b` 全额对冲、`delta_neutral_b`、`dutch_lock` 荷兰锁、`partial_cashout` 部分兑现、`lay_hedge` | 输出三情形收益矩阵 |
| `cross_venue.py` | 跨场所锁定：`P(A)_Kalshi + P(¬A)_Poly − 1 > 0` 才是真锁 | 套利阈值 `0.02` |
| `risk.py` | 单市场 / 单主题 / 总敞口 + 单日亏损熔断 | `daily_loss_killswitch_frac=0.08` |
| `xv_monitor.py` | 跨场所价差持续监控（`xv_spread` 表） |
| `*_advance.py` 四件套 | `inplay_tactics_advance` / `inplay_arb_advance` / `inplay_hedge_advance` / `smart_exit_advance`：把整套赛中栈 fork 到 2-way 晋级市场 | 见[休眠章节](#休眠代码试过但暂未启用的模型与统计手段) |

#### 17 条赛中信号

**A. 时间价值 / 事件驱动（9 条）**

| 信号 | 触发 | 常数 |
|---|---|---|
| `convergence_take_profit` | 公允值已达 88% 上限，或较入场涨了 12¢ → 兑现 | `LOCK_FRACTION=0.88` |
| `model_overshoot_take_profit` | 市场比实时公允高 12¢ → 卖给过度反应的人 | `OVERSHOOT_MARGIN=0.12` |
| `draw_trade_signal` | 早段便宜买平局；晚段仍平局且公允价到 0.74 → 卖出锁定 | `EARLY_MINUTE=35`、`DRAW_LOCK_FAIR=0.74` |
| `totals_time_decay` | 时间流逝而球未进 → Under 公允值见顶时卖出 | |
| `momentum_value` | xG 领先但比分未领先 → 下一球被低估 | |
| `goal_overreaction_fade` | 刚进球一方在数分钟内被过度定价 | `GOAL_FADE_WINDOW=4′` |
| `favourite_comeback` | 赛前热门落后 1 球但实时模型仍看好 | `≤70′`、赛前概率 `≥0.55`、残余权益 `≥0.15` |
| `red_card_value` | 红牌后对手 λ 前置，市场常反应过度 | `RED_CARD_WINDOW=12′` |
| `knockout_late_draw` | 淘汰赛 + 平局 + 晚段 → 平局公允价上升 | `LATE_MINUTE=75` |

**B. 数据挖掘得到（8 条，源自 26 场逐分钟研究，`docs/INPLAY_FINDINGS.md`）**

| 信号 | 触发 | 常数 |
|---|---|---|
| `dormant_explosion` | 40–70′、总进球 ≤1 但总 xG ≥1.0、模型仍预期有球 → "闷但装满了" | `DORMANT_REMAINING_GOALS=0.8` |
| `finishing_uplift_over` | 高 xG 队里有把握型终结者 | `FINISHING_UPLIFT=0.4`（挖掘值 +0.87、sd 1.78、n=26，**大幅收缩后使用**） |
| `xg_dominance_chase` | xG 领先 ≥1.0 却 0 进球（"应得未得"） | `XG_CHASE_MAX_MIN=80` |
| `possession_trap_fade` | 控球 ≥58% 但 xG ≤0.8 —— 无效控球，反手做空该队 | |
| `formation_fragility` | 检测到脆弱阵型 | `{5-3-2, 3-4-2-1}` |
| `lone_threat_removed` | 占全队射门 ≥50% 的单点威胁被换下 | `LONE_THREAT_SHARE=0.50` |
| `late_goal_bias` | 70′+ 是进球富集窗（34% 的进球发生在 75′ 后） | `LATE_GOAL_FROM_MIN=70` |
| `live_odds_crossval` | 博彩赔率与模型反向移动时的交叉校验（保守闸门，不是信号源） | |

> **一条纪律**：挖掘出来的效应量一律**重度收缩**后才写进常数。
> `finishing_uplift` 实测 +0.87、标准差 1.78、样本 26 —— 直接用等于把噪声当 alpha，所以取 0.4。

### `venues/` `exec/` — 场所与执行（17 文件 / 1,908 行）

| 场所 | 角色 | 鉴权 | 状态 |
|---|---|---|---|
| **Kalshi** | 可执行 + 读 | RSA-PSS（毫秒时间戳 + 路径签名） | demo 已连通（$492.65）；**prod 硬关闭** |
| **Polymarket US** | 可执行 + 读 | Ed25519 | 凭证已验证；**硬关闭** |
| **Polymarket Global** | **只读** | 无（公开） | 历史价格回填 + 交叉验证；`guard.py` 拦截任何下单 |

执行链：`edge` → `sizing`（分数 Kelly）→ `exec/order_translation.py`（目标净头寸 → 各场所合法订单：Kalshi 走净额、Poly US 走 intent）→ `exec/executor.py`（**$1 硬上限**）→ `venues/guard.py`（总开关）→ 才轮到真正的 POST。

**队名对齐**：三个场所加数据源对同一支球队有四种拼法（Türkiye / Turkey / TUR、IR IRAN / Iran…）。`ingest/prior_ingest.py::TEAM_ALIASES` 维护"规范名 → 已知拼法"的显式别名表，实时路径**只走精确别名**（错配会导致下错单），仅历史回填允许模糊匹配。前端 `upcoming.json` 里 `poly_us`/`kalshi` 为 `null` 表示"该场所尚未挂牌"，此时信号照常生成但闸门标记 `no_tradable_contract`，不产生订单。

### `ops/` — 报表、导出与运维（46 文件 / 8,859 行）

| 文件 | 说明 |
|---|---|
| `refresh_all.py` / `refresh_and_deploy.sh` | 全流程：摄入 → 强度更新 → 重模 → 评估 → 导出 → 同步前端 → `npm build` → Firebase 部署 |
| `performance_report.py` | **战绩单一真值**：Brier / log-loss / CLV / 逐场 `match_pick` / 五条并行赛道的盈亏 |
| `risk_report.py` | 每日限额、敞口、场馆余额、API 预算、校准闸门状态 |
| `upcoming_export.py` / `inplay_export.py` / `milestone_export.py` | 赛前 3-way（ask/bid/¢/去 vig/edge/锁标记）、赛中实时、里程碑轨迹 |
| `live_refresh.py` + `live_refresh.sh` | 比赛中每 30 秒刷新；结算时同步重生成 performance + 里程碑 |
| `match_trigger.py` | 事件驱动闸门：每 15 分钟问一次"有新结果吗"，没有就立刻退出（省算力） |
| `param_sweep.py` | **6,912 组结构参数**在 104 场上逐组重打分（每组都过同一套温度标定，比的是标定后 Brier）。只在日更里跑、且先 `sleep 60` 与实时任务错峰；`--trigger` 模式跳过 |
| `team_styles_export.py` | **48 队 × 10 种固定风格**的矩阵（控球 / 直传 / 高压 / 低位防反 / 强攻 / 高效终结 / 高射门量 / 定位球 / 均衡 / 被压制）。每队 1–2 种，由人工整理的球队风格**先验**与 API-Football 实时控球/传球/射门指标融合——先验覆盖尚未出场的球队，实时指标随比赛推进修正。描述性输出，周更 |
| `settle_bets.py` / `backfill_*.py` / `decision_backtest.py` / `walkforward_eval.py` | 结算、价格回填、决策回测、走查评估 |
| `online_microfootball.sh` | box B 新模拟一键上线：预检 → 同步 → DFM → 三道校验 → 构建 → 部署（`--check` 可演练） |
| `_*.py`（16 个下划线前缀） | **一次性研究脚本**，刻意用下划线标记为"非生产路径"：`_smart_exit_research`、`_xg_alpha_research`、`_double_bet_research`、`_style_classify`、`_validate_signals` 等 |

### `jobs/` `backtest/` `analysis/` `research/` `tests/`

| 目录 | 说明 |
|---|---|
| `jobs/` | `hourly_job.py` 全流程编排（`--dry-run` / `--loop`）；`live_poller.py` 比赛中逐分钟拉取 + 出信号 |
| `backtest/` | 重放引擎与指标（156 行，刻意做薄——重活在 `ops/decision_backtest.py` 与 `walkforward_eval.py`） |
| `analysis/` | 赛中信号复盘、抢救性挖掘、信号叠加分析 |
| `research/` | 三篇论文的完整可复现代码与图表（见[§ 论文](#相关论文)）+ 角球史料 + 第三名分析 |
| `tests/` | 27 文件 / 3,395 行 / **261 项**：先验恒等式、Dixon-Coles、锦标赛赛制、赛中模型、置信分层、8 条挖掘战术、跨场所、订单上限、¢ 换算、淘汰赛结算 |

---

## 每日运行流程

```bash
# 全流程（launchd 每天 06:30 自动跑）
bash prediction_market/ops/refresh_and_deploy.sh

# 事件驱动模式（每 15 分钟；无新结果则秒退）
bash prediction_market/ops/refresh_and_deploy.sh --trigger
```

### 定时任务（`ops/*.plist`）

| plist | 频率 | 干什么 |
|---|---|---|
| `com.someopark.predictionlive` | 每 30 秒 | 比赛进行中：拉实时 → 重算 `inplay_live.json` / `upcoming.json` → 里程碑回填 |
| `com.someopark.predictionmatchtrigger` | 每 15 分钟 | 检查是否有新结果落地；有则触发全流程 |
| `com.someopark.predictionrefresh` | 每天 06:30 | 全流程 + 6,912 组参数扫描 + 前端构建部署 |

> **周日额外拉一次国家队近期战绩**（`--with-form`）；工作日跳过，省 API 预算。
> 当前用量：日 2 / 7,500，月 60 / 200,000。

---

## 战绩（104 场全部结算，2026-06-11 → 07-19）

### 预测准确度

| 指标 | 值 | 参照 |
|---|---|---|
| 已结算比赛 | 104 | |
| Brier（全 104 场，未标定） | 0.5621 | 均匀基准 0.6667 |
| **Brier（温度标定后 T=1.3）** | **0.4605** | 校准拟合样本上的未标定值 0.475 |
| log-loss | 1.0012 | |
| 最可能一方命中率 | **67.3%**（70W-34L） | |
| 赛前热门命中率 | 60.6% | |
| 校准闸门 | **PASS**（0.4605 ≤ 0.6667） | 不过则拒绝所有信号 |

> 表里两个"未标定 Brier"不是笔误：`0.5621` 是全部 104 场结算后回算的，
> `0.475` 是校准拟合当时那批样本上的值。前者是复盘口径，后者是闸门口径，各自有各自的样本，
> 不能混着比。**这里也不放"市场 Brier"作参照**——本仓库唯一一个市场 Brier 数字（0.471）
> 只在 DFM 研究的 9 场对照样本上算过，拿它跟 104 场的数字并列会是一次跨样本比较。

### 五条并行赛道（同一批比赛，不同规则）

刻意把"选边规则"和"退出规则"拆开各自记账，因为它们回答的是两个不同问题：

| 赛道 | 规则 | 战绩 | 盈亏 |
|---|---|---|---|
| **argmax（参照）** | 每场都下最可能的一边 | 70W-34L | **+744.7¢** |
| **decision（价值下注）** | 只下最被低估的一边，按置信度 $0.2–$2.0 定量 | 47W-57L | +$0.70（ROI **+0.73%**，投入 $96.29，0 场因无边而跳过） |
| **hold（持有到终场）** | decision 选边 + 不中途退出 | 47W-57L | **−46.5¢** |
| **realized（+ 智能兑现）** | decision 选边 + `smart_exit` 中途锁定（55 次触发） | 51W-53L | **+1,535.0¢** |
| **inplay（赛中）** | 17 条赛中信号，84 场参与 | **55W-29L** | **+2,872.2¢** |
| **combined** | realized + inplay | | **+4,407.2¢ ≈ $44** |

平均入场 46.5¢ / 平均 CLV **−1.7¢**。

### 三条从数据里读出来的结论

1. **退出规则比选边规则值钱得多。** 同一批下注，持有到终场是 −46.5¢，加上模型感知的中途兑现变成 +1,535¢。差额 **1,581¢ 全部来自 55 次 `smart_exit`**，选边一个字没改。
2. **赛中比赛前赚钱。** 赛中 55W-29L / +2,872¢，是总盈利的主要来源。赛前市场（尤其是流动性好的 3-way）已经相当有效；赛中因为信息更新快、场馆重定价慢，才留下缝隙。
3. **CLV 是负的（−1.7¢），必须诚实说。** 意味着平均而言我们的入场价并不比收盘价好。盈利来自退出时机与赛中，而不是"比市场更早发现赛前错价"。**这一条决定了这套系统的价值定位**：它不是一个更好的赛前预测器，而是一个更好的**持仓管理器**。

> `argmax` 赛道单独列出来是因为它是**旧的朴素规则**——每场都押最可能的一边。
> 它命中率 67.3%、盈亏 +745¢，看起来比 decision 赛道漂亮。
> 两者在完整样本上并排展示，不藏其中任何一条：命中率高不等于赚钱多，
> 押热门赢得频繁但每次赢得少，这正是价值下注要解决的问题——而它在本届赛事里**没有解决好**。

---

## 交易状态与实盘纪律

**没有下过真钱单。** 只在 Kalshi demo 上做过一次"下单即撤单"的连通性验证。

| 闸门 | 值 |
|---|---|
| `kalshi_env` | `demo` |
| `KALSHI_TRADING_ENABLED` / `PMUS_TRADING_ENABLED` | **`false`（硬默认，即使凭证齐全也拒单）** |
| 单笔硬上限 | **$1.00** |
| 分数 Kelly | 0.25 |
| 单市场 / 单主题上限 | 5% / 10% 本金 |
| 单边下注阈值 θ | 0.03（赛中 0.01，因为智能兑现提供了额外保护） |
| 跨场所锁定阈值 | 0.02 |
| 单日亏损熔断 | 8% |
| 校准闸门 | OOS Brier > 均匀基准 → **拒绝一切信号** |

已接通的部分：订单翻译（目标净头寸 → 各场所合法订单）、定量、去 vig、edge 计算、校准闸门、纪律闸门、置信分层。**未接通**：实时 WebSocket 常驻订阅；真实下单 POST。

> **prod key 的标准规矩**：`risk_report` 里 `kalshi_prod_usd` 一栏写的是
> "not queried（standing rule: prod key only on explicit instruction）"——
> prod 凭证不在任何自动流程里被使用，只在明确指令下手动动用。

---

## 休眠代码、试过但暂未启用的模型与统计手段

赛事已结束，系统现在处于**赛后状态**（`inplay_live.json` 里 `n_live: 0`）。以下这些模块能跑、有测试，但当前不参与任何实时定价决策——分清"没启用"与"坏了"是最贵的误解，所以逐条列明。

### A. 扩散因子模型（DFM）与 LLM 智能体模拟器

这是仓库里最大的一块**研究性**代码，也是三篇论文的主线。

| 组件 | 位置 | 状态 |
|---|---|---|
| **智能体模拟器**（22 个 LLM 智能体逐场推演，低秩球队身份 + 稳定化的涌现控球） | 仓库外部 box B；论文 `someopark-football-agentic-simulator` | 每场约 $2–4 的模型调用成本，**每场只能产出 10–15 次高保真模拟** |
| **DFM 放大器** | `../dfm/football/`（`extract.py` / `model.py` / `production.py` / `validate.py`） | 把 10–15 次模拟压成 **342 维片段张量**（9 段 × 2 方 × 19 通道），用带因子分解 score 网络的条件 OU 扩散建模，再放大成 **每场 5,000 场合成比赛** |
| **统计检验与出图** | `research/dfm_football/`（`stat_tests.py` / `referee_tests.py` / `make_figures.py` + 论文） | LOFO（留一赛事，n=12）在 W1 距离上 **11/12** 胜过池化基线（总变差口径 7/12，两个都报）；生成样本的跨通道相关阵比自身 split-half 噪声更接近语料（平均绝对相关误差 **0.074 vs 0.128**，独立基线 0.114） |

**为什么它是休眠的**：在 `referee_tests` 的 **9 场**对照里，DFM 放大把原始集成的 Brier 从 **0.839 改善到 0.645**（锚定版 0.589），均匀基准 0.667——改善是真的；但同一批 9 场上**市场是 0.471**，配对 bootstrap 里 DFM 只在 3/9 场赢过市场。它赢了自己的基线，没赢市场。因此定位为**参考信号**：前端与 Dixon-Coles 并列显示，持续监控预测精度，但订单由 Dixon-Coles + 赛中信号驱动。

**红牌问题（已解决一半）**：引擎其实一直在产生红牌，只是没有输出。红牌已从"罚下帧"恢复（99 场里 125 张），此前红牌率失真约 25 倍。**单黄牌不可恢复**——引擎需要补日志，待 box B 空闲时做。

### B. 赛中 2-way 晋级 fork（已建成，随赛事结束而静止）

`model/inplay_advance.py` + `strategy/{inplay_tactics,inplay_arb,inplay_hedge,smart_exit}_advance.py` + `ops/inplay_export_advance.py` —— 把整套赛中栈 fork 到"谁最终晋级"（含加时点球）而非"90 分钟谁赢"。

**不是纸上计划：它跑过。** 淘汰赛期间 `price_tick_adv` 累计了 7,692 条 tick，`inplay_live_advance.json` 的最后一次写入是 **2026-07-19T22:04**（决赛结束）。休眠原因是**没有比赛了**，而不是没做完。真正的技术欠账是：晋级概率依赖同组其它场次的同步状态，小组赛阶段的联动逻辑比淘汰赛复杂得多，那部分尚未完全接线。

### C. 角球市场（模型写完，市场没挂牌）

`model/inplay_corners.py` + `strategy/inplay_tactics.py::corner_total_signal`（`MIN_CORNER_MINUTE=12`、`MAX_CORNER_MINUTE=89`、`MIN_CORNER_EDGE=0.07`）+ `research/corner_history.py` / `corner_report.py` / `third_place_corners.py`。

从 xG 推角球率的泊松模型已完成并有历史校验，但 **Kalshi 与 Polymarket 都没有挂出角球合约**。纯粹是等标的，不是等代码。

### D. 默认权重为 0 的可选特征（接线完成，未调参）

这三个特征的代码路径全通、有测试、有 no-op 保证，但**权重默认 0**，即当前对定价零影响：

| 特征 | 位置 | 权重 | 为什么是 0 |
|---|---|---|---|
| **场地气候修正** | `model/venue_climate.py` | `venue_climate_weight=0.0` | 16 座球场的海拔/气温/顶棚 → 对称压低 λ（实测约多 2.5pp 平局）。效应方向可信但样本不足以定权重 |
| **对手 xGA** | `model/altdata_adjust.py` | `xga_weight=0.0` | 与已启用的 `oppadj_def=0.45` 高度共线，重复计价的风险大于增益 |
| **首发阵容影响** | `model/squad_strength.py` | `lineup_weight=0.0` | `lineup` 表只有 208 行（不是每场都提前公布），样本稀疏；权重 0 是安全的 no-op |

> 保持 0 而不是删掉，是因为它们的**数据管线**已经在跑（`lineup` / `venue` 表在填），
> 一旦样本够了只需改一个数字，不需要重写。

### E. 球会级球员形态（数据齐，PIT 校验未做完）

`model/club_aggregation.py` + `model/fc_strength.py` + `ingest/fc_ingest.py`：EA FC26 的 9,853 名球员评分已入库，`--club-blend` 开关可用，但作为 OOS 诊断项而非默认路径。堵点是 PIT 校验——俱乐部赛季数据的"何时可知"边界比国家队战绩复杂得多，没验完就不敢默认打开。

### F. BTTS / 大小球专项校准（已判定为"暂缓"）

- **大小球 2.5**：准确率约 85%，够用。
- **BTTS（双方进球）**：模型系统性**低估**——预测约 45%，实际约 70%。

当前只有 3-way 做了温度标定，总进球类市场**没有单独校准**。已判定为「事后单独校准 + 走查验证 + 默认关闭」的后续项，暂缓的原因是：这两个市场流动性远不如 3-way，而系统同期已经有相当多的旋钮在调，再加一组会让多重比较失控。

### G. 已试过、结论是"不这么做"的方向

| 方向 | 结论 |
|---|---|
| **`_double_bet_research.py`** 同场双边下注 | 研究后未采纳 |
| **`_fine_exit_research.py` / `_smart_exit_research.py`** 更细粒度的退出网格 | 收益不敌复杂度；最终落地的是单一 `OVERSHOOT_MARGIN=0.08` |
| **`_metric_alpha_sweep.py` / `_xg_alpha_research.py`** 指标 alpha 扫描 | 大部分候选在收缩后归零，只有 `oppadj` 两项与几条挖掘战术存活 |
| **`_deployed_xi_research.py`** 实际首发对强度的影响 | 见 D 项，样本不足 |
| **把动机修正也接进校准 / OOS / 交易闸门** | 拒绝。`model/motivation.py` **已在实时定价里生效**（见 `model/` 表），但**刻意只走实时下注这一路**：校准、OOS、trade-grade 闸门都跑不带动机的裸模型，这样闸门验证的一直是同一个基线，动机项不可能靠"顺便调高闸门通过率"混进来 |

---

## 相关论文

三篇论文的完整可复现代码、数据与图表都在 `research/` 下（中英双版本，英文为准）：

| 论文 | 位置 | 主题 |
|---|---|---|
| **AI-Driven Forecasting and Execution in Soccer Prediction Markets: A Production System with a Decision Ledger, Model-Aware Cash-Out, and Event-Gated In-Play Entry** | `research/wc_forecasting/` | 就是这套系统本身：决策台账、模型感知兑现、事件闸门化的赛中入场 |
| **Diffusion Factor Models for Agentic Football Simulation: Amplifying Small Simulation Ensembles into Calibrated Match Distributions** | `research/dfm_football/` | 把金融领域的扩散因子模型迁移到足球，用 150 次模拟放大成每场 5,000 场 |
| **A Local-Volatility Theory of Prediction Markets: Absorbed Martingales on the Simplex and an Identification Law for Transient Mispricing** | `research/martingale_pricing/` | 预测市场价格的局部波动率理论，基于 50 万 tick 的全菜单数据 |

配套的智能体模拟器论文 **someopark-football-agentic-simulator: A Multi-Agent LLM System for Football Simulation with Low-Rank Team Identity and Stabilised Emergent Possession** 描述 DFM 的上游数据来源。

---

<p align="center"><sub>Someo Park Investment Management · 研究用途 · 本页任何数字都不是投资建议</sub></p>
