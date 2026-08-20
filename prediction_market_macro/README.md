<p align="center">
  <img src="../public/SOMEO PARK矢量源文件 Big Square.svg" alt="Someopark" width="160"/>
</p>

<h1 align="center">prediction_market_macro</h1>
<p align="center"><b>宏观经济预测市场系统 · Kalshi 事件合约</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/conda-someopark__run-green?logo=anaconda&logoColor=white"/>
  <img src="https://img.shields.io/badge/venue-Kalshi-orange"/>
  <img src="https://img.shields.io/badge/series-13%20P0%20%2B%201%20P1-purple"/>
  <img src="https://img.shields.io/badge/data-FRED%20%7C%20ALFRED%20PIT-teal"/>
  <img src="https://img.shields.io/badge/tests-646%20passing-brightgreen"/>
  <img src="https://img.shields.io/badge/mode-paper%20only-red"/>
</p>

---

把这套系统当作一张**宏观数据发布的交易台**：在 **Kalshi** 上就美国官方宏观数字（CPI / 核心 PCE / 非农 / 失业率 / 初请 / FOMC 决议 / WTI / 天然气 / 汽油）报出自己的**概率阶梯**，与市场报价比对，只在自己认为被错误定价的腿上下注。

与世界杯系统 [`prediction_market/`](../prediction_market/) 平行、**完全隔离**：不 import 对方任何代码，各写各的 SQLite、各读各的 `.env`，只共享同一套架构约定与纪律（venue 封装、devig、分数 Kelly、append-only 决策台账、PIT 时点纪律）。

> **⚠ 当前状态：纸面（paper）模式，从未下过一笔真钱单。**
> `mode='paper'` 硬编码在 `ops/ledger.py` / `ops/decide_all.py` / `ops/exits.py` 三处写盘点；
> `exec/kalshi_exec.py` 的下单客户端已写完但**没有任何生产代码 import 它**。
> 资金曲线读的是 Kalshi **demo** 账户余额。见 [§ 交易状态与实盘纪律](#交易状态与实盘纪律)。

设计文档：`docs/PLAN.md`（主计划）+ `docs/PLAN_EXTENSION.md`（§25 系列，逐条问题的调查与结论）。

---

## 系统一句话

```
FRED/ALFRED 原始 vintage  ─┐
Kalshi 订单簿快照         ─┤
发布日历 / 期货 / 天气/EIA ─┼─→  逐系列模型 → 概率阶梯 pmf
新闻 + FOMC 声明(LLM)     ─┘                     │
                                                 ▼
                            阶梯 pmf  ×  市场 devig 后概率  →  每条腿的 edge
                                                 │
                       八道闸门（skill/校准/capture/conformal/一致性/…）
                                                 │
                                     ┌───────────┴───────────┐
                                     ▼                       ▼
                              edge 单（模型认为错价）   argmax 单（跟随市场最可能腿）
                                     └───────────┬───────────┘
                                                 ▼
                              append-only decisions 台账 → 盯市 → 平仓/结算
```

**规模**：161 个 Python 文件 / 29,060 行；646 项 pytest 全绿；SQLite 34 张表，其中 FRED 观测 12.4 万行、Kalshi 合约 8,849 个、结算 8,225 条、K 线 16,874 根、Cleveland 通胀 nowcast vintage 60,182 行（2013-07 起）。

---

## 十四个系列（`config/registry.py` 是唯一真值）

每个可交易系列是一个 `SeriesSpec`，`settle_source` 是**人工逐条核对 Kalshi 规则书**抄下来的结算口径（2026-07-27 实测）——精确到取整规则、首次发布（first print）约定、以及 `>` 与 `>=` 的差别。`tests/test_m0.py` 断言其完整性，`settle_source` 为空即构建失败。

| Ticker | 家族 | 频率 | 结算口径要点 | 结构 | 模型 |
|---|---|---|---|---|---|
| `KXFEDDECISION` | fed | 每次 FOMC | 目标区间相对上次会议的变动（加息 25 / >25 / 降息 25 / >25 / 维持） | 分类 | `fed` |
| `KXFED` | fed | 每次 FOMC | 会后联邦基金**目标区间上沿**，25bp 网格，"Above X%" 严格 `>` | 阶梯 | `fed` |
| `KXCPI` | inflation | 月 | BLS CPI-U 整体 **MoM %**，首次发布，按发布值取整 0.1 | 阶梯 | `cpi` |
| `KXCPICORE` | inflation | 月 | CPI-U 核心（除食品能源）MoM % | 阶梯 | `cpi` |
| `KXCPIYOY` | inflation | 月 | CPI-U 整体 **YoY %** | 阶梯 | `cpi` |
| `KXCPICOREYOY` | inflation | 月 | CPI-U 核心 YoY % | 阶梯 | `cpi` |
| `KXPCECORE` | inflation | 月 | BEA 核心 PCE 价格指数 MoM %，首次发布 | 阶梯 | `pce` |
| `KXJOBLESSCLAIMS` | labor | 周 | DoL 初请失业金 SA **预估值（advance）**，千人；注意是 `>=` 不是严格 `>` | 阶梯 | `claims` |
| `KXPAYROLLS` | labor | 月 | BLS 非农就业 MoM 变动，首次发布，千人 | 阶梯 | `payrolls` |
| `KXU3` | labor | 月 | BLS U-3 失业率 SA，首次发布，取整 0.1 | 阶梯 | `u3` |
| `KXWTIW` | energy | 周 | NYMEX WTI 近月合约周五收盘，$1 分桶 | 阶梯 | `energy` |
| `KXNATGASW` | energy | 周 | Henry Hub 近月周五收盘 | 阶梯 | `energy` |
| `KXAAAGASW` | energy | 周 | AAA 全国汽油均价周一值，取整 0.001 | 阶梯 | `energy` |
| `KXGDP` | gdp | 季 | BEA 实际 GDP 年化增速，首次发布 | 阶梯 | `gdp` **(P1，仅纸面观察)** |

> **铁律 2**：新系列在积累**至少两次纸面 print** 之前不进入任何实盘讨论。`KXGDP` 优先级挂 P1：它**在**决策循环里（与其他系列同一套闸门），但季频 + `max_days_to_close=7` 意味着绝大多数日子直接 pass——台账 116 条全部是 pass，纸面从未开过仓；走查回测里它开过 1 笔（+$5.03）。

---

## 环境配置

### 1. Python 环境

```bash
conda activate someopark_run     # 与 prediction_market/ 共用同一个 env
```

### 2. 密钥

| 变量 | 位置 | 用途 |
|---|---|---|
| `FRED_API_KEY` | 仓库根 `.env` | FRED / ALFRED vintage 拉取 |
| `POLYGON_API_KEY` | 仓库根 `.env` | 期货 / 外汇 / 新闻 |
| `EIA_API_KEY` | 仓库根 `.env` | 天然气库存 |
| `KALSHI_*` | `prediction_market/.env` | 账户余额（只读）；**公开行情无需鉴权** |

> `.env` 全部 gitignored。Kalshi 公开行情端点 `https://api.elections.kalshi.com/trade-api/v2` 不需要 key，
> 行情落库这一路即使没有任何凭证也能跑通。

### 3. 运行所有脚本的正确方式

```bash
cd /Users/xuling/code/someopark-test && \
  set -a && source .env && set +a && \
  PYTHONPATH=$PWD conda run -n someopark_run --no-capture-output \
  python -m prediction_market_macro.ops.refresh
```

> **`cd` 到仓库根是必需的**，不是习惯问题：`.env` 与 `PYTHONPATH` 都锚在根目录，
> 在子目录里跑会静默丢掉 FRED key，模型退化成"用缓存数据算今天"。

---

## 目录与核心文件

### `config/` — 配置与注册表（3 文件 / 221 行）

| 文件 | 说明 |
|---|---|
| `registry.py` | **系列注册表**，上表的来源。每条含 ticker / 家族 / 频率 / 日历键 / 结算口径 / 结构 / 单位 / 取整步长 / `strict_gt` / 绑定模型 / 优先级 / 调度 lane / 对应 ALFRED 首发序列 |
| `settings.py` | 路径、API key、`db_path`、前端导出目录、`trading_enabled`（硬默认 `False`） |

### `ingest/` — 数据接入（11 文件 / 1,806 行）

**所有落库都带 `knowledge_time`**：这条数据在真实世界的哪一刻可被知晓。整套系统的 PIT 纪律建立在这个字段上——模型 `predict(asof=...)` 只能看到 `knowledge_time <= asof` 的行。

| 文件 | 外部源 | 说明 |
|---|---|---|
| `store.py` | — | SQLite 单文件（WAL + `busy_timeout=30s`）。31 张表的 DDL 全在这里，且全是 `CREATE TABLE IF NOT EXISTS`——每个 live 入口都调 `init_db()`，所以新表 DDL 会自动到达生产。**从不 DELETE，只按自然键 `INSERT OR REPLACE`** |
| `fred.py` | FRED / ALFRED | 带 vintage 的时点拉取。`knowledge_time = vintage_date + 官方 ET 发布时刻`（转 UTC）。核心序列含 CPIAUCSL / CPILFESL / PCEPILFE / UNRATE / PAYEMS / ICSA / DFEDTARU / DGS2·5·10·30 / T5YIE / GASREGW / DCOILWTICO / GDPC1 |
| `kalshi_md.py` | Kalshi 公开行情 | 订单簿快照（yes_bid/yes_ask + 深度）、合约元数据、结算结果、K 线。404 时写"哨兵行"（`end_ts=0` + NULL 价格）表示"问过了，没有" |
| `calendars.py` | BLS/BEA/DoL/EIA/FOMC | 14 个发布日历，`scheduled_ts` 与 `actual_ts`；`refresh_from_web()` 每周核对一次官网 |
| `market_data.py` | Polygon | CL/NG 近月期货日线（`knowledge_time = 收盘 UTC`）、外汇、新闻 |
| `fed_text.py` | Federal Reserve | FOMC 声明全文，`knowledge_time = 发布瞬间`。v2 结构按 `(period, release_date)` 主键，支持一个月两份声明 |
| `weather.py` | ERA5 / ERA5T | 滚动 45 天气温（供天然气模型的度日）。**只拉 45 天尾巴**，因为这个窗口也正好覆盖 ERA5T 后来沉淀为 ERA5 的重写区 |
| `eia.py` / `aaa_daily.py` / `nowcast.py` | EIA / AAA / GDPNow | 天然气库存、汽油日均价、GDPNow vintage |
| `cleveland_nowcast.py` | Cleveland Fed | **日频通胀 nowcast 全 vintage**（CPI/核心CPI/PCE/核心PCE × MoM/YoY/Q，2013-07 起 6 万行，CC-BY-4.0）。`knowledge_time = nowcast 日 18:00 UTC`（保守方向）；`latest()` 是 PIT 读取口；`refresh_if_stale()` 是 tick 内的盘中尾守卫（只拉 yoy 文件、55 分钟节流）。**cpi/0.3.0 起 YoY 双系列的 mu 锚定在它上面** |

### `model/` — 逐系列模型（16 文件 / 2,927 行）

每个系列一个模块，输出是**结算网格上的概率阶梯（pmf）** + `inputs_json` 依据链 + `data_horizon`（用到的最晚 `knowledge_time`）。所有 P0 模型 `frozen=True`——不做在线重训，唯一的旋钮是每日 DSR 选参器。

| 模块 | 方法 | 备注 |
|---|---|---|
| `claims.py` | 对数水平加权均值 + ISO 周季节性偏离 + 26 周 MAD 波动率（带 2% 下限）→ 高斯阶梯 | 唯一的周频劳动力系列 |
| `cpi.py` | **未取整指数**的 MoM 连续性 + 汽油泵价（GASREGW）传导（RB 系数 0.55）+ YoY 精确递推；**0.3.0 起 YoY 双系列 mu 锚定 Cleveland nowcast**（headline 读 `cpi`、core 读 `corecpi`，σ 不动，缺失/陈旧 >7 天回退内部链） | 一个模块喂四个系列；锚定依据不对称：headline 是 45 事件泄漏免疫回放 Brier −33% 的证据判决，core 是 44 事件平局后的用户决定（PREREGISTER PR-8 有案） |
| `pce.py` | CPI 核心 → PCE 核心的桥接回归（OLS + 残差 σ） | 已知风险：CPI/PCE 权重分歧、年度基准修订 |
| `payrolls.py` | 首发变动重建 + 初请信号 + 胖尾混合（高斯 + Student-t） | 经验阶梯，非解析式 |
| `u3.py` | 经验 Δ 核 + 多月卷积；显式建模 0.1 取整边界 | |
| `fed.py` | **两源对数池化**：从 51 次历史加息核出来的规则判别式 + ZQ 联邦基金期货链 devig，按 `λ = 月数/(月数+9)` 向先验收缩 | 超出 ZQ 流动性范围后接 DGS2 斜率 |
| `energy.py` | 无漂移 GBM，20,000 次 MC，20 日 MAD 波动率（下限 0.8–1.5%）→ 分位映射成阶梯 | WTI / NG 两条；AAA 走 4 周阻尼趋势 + GASREGW 代理 |
| `gdp.py` | GDPNow 锚 + 历史 "nowcast vs 首发" 误差 σ | P1，纸面 |
| `features.py` | `FeatureStore`：统一的 as-of 读取口 | 见[休眠章节](#休眠代码试过但暂未启用的模型与统计手段)——部分模型仍绕开它直查 `fred_obs` |
| `registry.py` | 模型卡（model card）：版本、训练截止、`feature_set`、**已知失效场景**（中英双语） | `ensure_registered()` 幂等写入 `models` 表 |

### `strategy/` — 定价与闸门（12 文件 / 1,558 行）

| 文件 | 作用 | 关键常数 |
|---|---|---|
| `edge.py` | 腿定价、结算算术、成交价滑点。**`settle_struct()` 是回测与实盘共用的同一个函数**——这是 #141 之后立的规矩 | `PAPER_TICK=0.01`、`WIDE_SPREAD=0.15`；Kalshi taker 费 `ceil(0.07·n·p·(1−p))` 分 |
| `devig.py` | 从含 vig 的报价还原市场隐含概率 | |
| `decision.py` | 主闸门流水线 + 四分之一 Kelly 定量 | `min_net_edge=0.04`、`min_leg_depth_usd=50`、`max_depth_frac=0.20`、`max_entropy_norm=0.95`、`max_days_to_close=7`、`min_leg_price=0.10` |
| `skill.py` | 模型 OOS Brier 落后市场就自我降级 | 落后 >5% → 防守（门槛翻倍、仓位减半）；落后 >50% → **完全封禁模型路径下单**；`TRAIL=20`、`MIN_PAIRED=6` |
| `calibration.py` | 等距回归（isotonic）事后校准 | `MIN_PAIRS=200`，样本不足时保持恒等映射 |
| `conformal.py` | 共形预测覆盖率检查，超限则收缩仓位 | `TARGET=0.10`、`TRAIL=40`、`GAMMA=0.05` |
| `capture.py` | "这个 edge 历史上真的兑现过吗" | `MIN_N=8`、`MIN_CAPTURE=0.4` |
| `consistency.py` | **无模型**的跨市场一致性检查：FOMC 阶梯隐含变动 vs 直接决议市场；MoM 递推出的 YoY vs YoY 市场 pmf | `FED_MOVE_GAP_ALERT=0.125pp`、`CPI_TV_ALERT=0.20` 总变差 |
| `arb.py` | 同一事件内互斥腿的三角套利 | `MAX_ARB_USD=2.0`、`MIN_NET=0.005/张`（费后） |
| `snipe.py` | 发布后市场尚未反应完的"最后一眼"机会 | `MAX_SNIPE_USD=2.0`、`MAX_PRICE=0.95`、跳过距 print `0.5` 个网格步以内的腿 |
| `series_enable.py` | **§25.4 单系列 ROI 开关**（滚动 12 笔）：跌破盈亏平衡则关，回到 +2.6% 以上才开（迟滞） | `SHADOW=True` — 见[休眠章节](#休眠代码试过但暂未启用的模型与统计手段) |

### `ops/` — 每日运行（15 文件 / 3,362 行）

| 文件 | 说明 |
|---|---|
| `refresh.py` | **日更主入口**。"步骤表"模式：每步独立 try/except，一步失败不杀全流程，逐步打印 ✓/✗ 并把失败写进 `alerts` |
| `predict_all.py` | 遍历所有到期 (series, period)，跑注册模型，写 `preds` |
| `decide_all.py` | 过闸门，选 edge 单还是 argmax 单，写 append-only `decisions` 台账；同时记录影子行 |
| `exits.py` | 持仓边际反转平仓：`hold_edge = Σ腿[fair(side) − mid(side)]`，`< −0.06` 且每条腿深度 ≥ 20 张才平。缺报价 / 非双边盘 / 无法定价的腿一律返回"继续持有" |
| `pnl.py` | 盯市（`marks`）与结算入账（`fills` 的 `realized_usd`） |
| `risk.py` | 敞口上限：单事件 $5 / 单家族 $20 / 同族同期簇 $8 / **单日新开 $30** / 总敞口 $100 |
| `archive_candles.py` | **K 线归档**：Kalshi 约 75 天后永久丢弃 K 线。保留窗口是实测出来的（73 天仍有数据、76 天首次 404，四个系列一致），常数取保守端 `RETENTION_DAYS=74`；`WARN_AGE_DAYS=55` 留 19 天余量 |
| `frontend_export.py` | **唯一**写 `public/data/macro_*.json` 的地方（13 个 JSON，其中 `macro_reports.json` 是 PDF 索引） |
| `report.py` | 日报 / 周报 PDF |
| `ledger.py` | 台账读写内核；`OPEN_KINDS = (open, argmax, arb, snipe)`、`CLOSE_KINDS = (exit, cancel, settle_note)` 的**唯一定义处** |
| `freeze_track.py` | 把某次回测冻结成前端展示段 |
| `install_launchd.sh` / `launchd/` | 四个 plist 的安装脚本 |

### `research/` — 回测与评估（20 文件 / 6,969 行）

| 文件 | 说明 |
|---|---|
| `walkforward.py` | **策略回测的唯一真值**。逐个模拟日重建当天的闸门状态（严格只用更早的收盘），跑开仓/平仓/结算全流程。`fair_mode`（model/pooled）、`model_exits`、`shadow_blocked` 可组合 |
| `pit_gates.py` | 回测侧的闸门状态机，与 `decide_all` 读**同一个** `series_enable.SHADOW` 开关 |
| `backtest.py` | 预测准确度重放（Brier / CRPS），与交易 PnL 分开 |
| `param_select.py` + `dsr.py` | 每日 **DSR（Deflated Sharpe Ratio）门控的选参**。`MIN_OBS=12`：观测不足就退回注册默认参数，绝不"挑一个看起来最好的"。另含 `manual_params` 覆盖机制（带 PIT 采纳时间戳的历史保留行,三个读取门 `current`/`select_for`/`params_asof` 全部咨询它） |
| `param_argmin.py` | **每日 raw-argmin 重选**（用户 2026-08-11 常设指令,与上一行的 DSR 立场相反且明知如此）:trailing 75 天 prod 规则 PnL 上逐市场 argmin → `set_manual` 采纳,每行都写明 DSR 的反对意见。指纹缓存含模型版本,版本 bump 强制重打分 |
| `pnl_score.py` | 选参器的目标函数——**复现 `walkforward` 的同一套闸门与平仓规则**（#133/#144 之后强制对齐） |
| `param_wf.py` / `param_space.py` / `param_grid.py` | 参数网格与逐系列窗口（按各自数据量定，不是一刀切 200） |
| `eval.py` | 逐来源（model / market / bridge / ensemble）OOS 记分板 + §25.4 的每周 `enabled` 判定 |
| `confidence.py` | §25.3 逐笔置信度模型："这一笔会赚钱吗" |
| `health.py` | 每日健康检查（新鲜度 / 阶梯质量 / 回滚确定性 / 滚动 OOS Brier） |
| `attribution.py` | 每周归因：模型错、市场错、还是运气 |
| `martingale.py` | 鞅性质检验（漂移检测） |
| `shadow_claims.py` / `shadow_pr2.py` / `shadow_s2.py` | 三个**预注册前瞻检验**的记分器，见下 |
| `DECISION_RULE_113.md` / `DECISION_RULE_119.md` / `TRADEABILITY_129.md` | 三份决策记录：判据在**看到结果之前**就写死 |

### `jobs/` `venues/` `exec/` `analysis/` `tests/`

| 目录 | 说明 |
|---|---|
| `jobs/` | `scheduler.py` 把日历事件展开成 `runs` 表（lane / series / period / task / due_ts）；`tick.py` 定时刷行情+盯市+平仓检查；`watchdog_job.py` 扫过期未完成的 run |
| `venues/kalshi/` | 账户余额（RSA-PSS 鉴权，**只读**）。80 行，刻意做薄 |
| `exec/` | 下单客户端 —— 已写完，**没有任何生产代码调用它** |
| `analysis/llm.py` | 新闻结构性断点标注（大规模裁员 / 能源冲击 / 罢工…）→ `event_flags` 表；FOMC 声明鹰鸽打分 |
| `tests/` | 65 个文件 / 10,127 行 / **646 项**。覆盖 PIT 单调性、回测与实盘的逐 bit 一致性、每条闸门、每个影子记分器 |

---

## 每日运行流程

```bash
# 日更（launchd 每天 05:00 自动跑）
python -m prediction_market_macro.ops.refresh

# 周更（周日 06:30；日更全部步骤 + 评估/归因/走查扫描/周报）
python -m prediction_market_macro.ops.refresh --weekly
```

`refresh.py` 的步骤顺序不是随意的，有三处**顺序红线**：

```
① 摄入
   calendars → bankroll → fred_core
   → calendar_actuals         ★ 必须在 fred_core 之后（对账要拿当天刚拉的首发行，
                                 反过来排会把"已发布"误判成"未发布"）
   → gdpnow → aaa_daily → eia_storage → cleveland_nowcast(通胀 nowcast 全量 upsert)
   → weather(45d) → futures → fx → news → fed_statements
   → 逐系列 kalshi 快照 + 结算同步
   → archive_candles          ★ 必须在 settle 之后（settle 才把刚收盘的合约写进 settlements，
                                 反过来排就永远晚归档一天，而这一步有外部截止日）
② 调度
   materialize（日历事件 → runs 表）  →  models_registry（模型卡幂等写入）
③ 预测与决策
   param_select               ★ 必须在 predict_all 之前（predict_all 读它写的那一行）
   → param_argmin             （每日 raw-argmin 重选,用户 2026-08-11 常设指令;指纹缓存,
                                 含模型版本——版本 bump 强制全体重打分）
   → predict_all              （开头带 cleveland_nowcast.refresh_if_stale 盘中尾守卫）
   → decide_all
   → s2_shadow                ★ 必须在 exits 之前（被实盘规则本轮平掉的仓位，
                                 到 exits 返回时已不在 open_positions 里，
                                 而阈值更松的 S2 必须在最后那天也被看见）
   → exits
④ 盯市与结算
   marks → settle_pass
⑤ 影子成员（只写预测，永不进决策）
   chronos_shadow → bridge_shadow → ensemble_shadow
⑥ 无模型一致性检查 → LLM 标注 → 健康检查 → 前端导出 → 日报 PDF
   （周更另有：prereg 影子记分 → 归因 → 30d/60d 走查 → ML 选择器 → 周报 → 二次导出）
```

### 定时任务（`ops/launchd/`）

| plist | 频率 | 干什么 |
|---|---|---|
| `com.someopark.macrorefresh` | 每天 05:00 | 全量日更 |
| `com.someopark.macrotick` | 每 900 秒 | 刷行情、盯市、跑平仓检查 |
| `com.someopark.macrowatchdog` | 每 3600 秒 | 扫 `runs` 表里过期未完成的任务 |
| `com.someopark.macroweekly` | 周日 06:30 | `--weekly`：评估闸门 + 归因 + 30d/60d 走查 + ML 选择器 + 周报 |

---

## 时点纪律（PIT）—— 这套系统真正的地基

宏观数据的坑不在建模，在**时间**。同一个 5 月 CPI，官方会发布很多次（首发、后续修订、年度重算），而合约只按**首发**结算。任何一处不小心读到"今天的最新值"，回测就会凭空多出未来信息。

系统的做法：

1. **一切入库带 `knowledge_time`**——不是数据描述的时间（`event_time`），而是它可被知晓的时间。
2. **模型只有 `predict(conn, asof, period)` 一个入口**，内部读取一律 `knowledge_time <= asof`。
3. **标签用首发**：`registry` 里每个系列都绑了 `fred_first_release` 的 ALFRED 序列，评分与结算取首发值，不取修订后。
4. **回测按天重建闸门状态**（`research/pit_gates.py`），用严格更早的收盘计算 skill / 校准 / capture 状态，绝不用"整段样本算一次"。
5. **金丝雀测试**：`tests/` 里有单调性检验——把 `asof` 往前推，可见数据只能变少不能变多。

> 曾经咬过两次的坑，写在这里免得第三次：**FRED 观测的"就近匹配窗口"对日频序列必错**。
> `DFEDTARU` / `DCOILWTICO` / `GASREGW` 这类日频/周频 sid，用宽松窗口就近取值等价于偷看未来。
> 必须严格 `<=`。

---

## 回测与前瞻检验

### 走查回测（walk-forward）

```bash
python -m prediction_market_macro.research.walkforward --days 75 --end 2026-08-04
```

不是"跑一遍历史看收益"，而是**逐日重放**：每个模拟日只用当天之前的信息重建选参、重建闸门、重建报价，然后按实盘完全相同的规则开仓与平仓。三个可组合维度：

- `fair_mode`：`model`（用自己的模型定价）/ `pooled`（模型与市场池化）
- `model_exits`：是否启用平仓规则（关掉即"持有到结算"）
- `shadow_blocked`：把被闸门挡下的区域也算出来，用于读"如果不挡会怎样"

> **纪律**：走查是**评估工具，不是调参工具**。
> 在这个窗口上反复调阈值直到好看 = 保证过拟合。所有阈值必须有先验依据（费用几何、
> 已测偏差、借用自其他模块的常数），不能是"试出来的"。

### 预注册前瞻检验（pre-registration）

想改策略时，不改代码去证明它更好，而是**先把判据写死、再等前瞻样本**。判据一旦注册**不许改**，多重比较次数 K 必须计数上报。

| 编号 | 假设 | 判据 | 状态 |
|---|---|---|---|
| **PR-1** | 初请模型改用激进近因权重 `(0, 0, 0.3, 0.7)` 更准 | K=1；≥8 次结算；配对 Brier(候选) < Brier(市场) | 前瞻中 |
| **PR-2** | argmax 单加"贵于公允就不下"的过滤更赚 | K=1；≥20 条 argmax 腿；ROI 差 ≥ 5pp | 前瞻中（双臂已接线，等样本） |
| **PR-7/S2** | 更紧的平仓阈值（`hold_edge <= 0` 而非 `< −0.06`） | K=3；≥30 笔；ROI 差 ≥ 5pp 且事件聚类 95% CI 不跨零 | 前瞻中（08-11 台账重置后计数重启） |
| **PR-8** | CPI-YoY 族 mu 锚定 Cleveland nowcast | K=1；前向 6 个结算事件,T-26h 配对逐腿 Brier | **登记数小时后被用户指令改道**：改跑判据的历史等价物（45/44 事件泄漏免疫回放）。headline 判决性通过（Brier −33%）→ 已上线；core 平局 → 用户决定也上线（依据=决定非证据,原文有案）。前向计数降级为确认监控 |

检验共用一条实现纪律：**候选臂与对照臂必须走同一个 `settle_struct()`**，否则比较的是两套算术而不是两个策略。

---

## 交易状态与实盘纪律

**从未下过真钱单。** 三道闸门串联，全绿才可能下单——今天没有一道是绿的：

| 闸门 | 位置 | 谁写它要读的行 | 现状 |
|---|---|---|---|
| ① `settings.trading_enabled` **且** 环境变量 `KALSHI_TRADING_ENABLED=1` | `config/settings.py:47` | — | 硬默认 `False`，环境变量从未设过 |
| ② 逐系列 `series_gate` 行（`experiments` 表，`real=true`） | `exec/kalshi_exec.py:48` | 周更的 `research/eval.py:499` | **14 个系列全部 `real=false`**（最近一次评估 2026-08-16）→ 拒单 |
| ③ 近 7 天无 `source='circuit_breaker'` 告警 | `exec/kalshi_exec.py:54` | `research/health.py` → `ops/risk.circuit_breaker()` | 历史上跳闸过 24 次（replay 不一致、结算标签不符），当前均已 ack |

更根本的是：**`exec/kalshi_exec.py` 没有被 `ops/` 或 `jobs/` 里任何模块 import**——全仓库只有两处注释提到它。所有成交都由 `decide_all` / `exits` / `arb` / `snipe` 以 `mode='paper'` 直接写进 `fills`。真钱路径存在，但没有接线。

### 当前战绩（诚实口径，2026-08-16）

前端展示分两段，**刻意不合并**：

| 段 | 口径 | 数字 |
|---|---|---|
| **历史段** | PIT 走查回测（`frozen:d75:model:end2026-08-04:adopted0811`，hybrid 流，**cpi/0.3.0 nowcast 锚定模型**下重放的 08-11 采纳参数模拟），冻结于试运营重置日 2026-08-11 | 44 笔 / 29 胜（65.9%）/ 投入 $35.33 / 已实现 **+$9.34** / **ROI +26.4%** |
| **实盘段** | 2026-08-11 台账清零重启之后的纸面单 | 5 笔已结算（3 胜，投入 $2.65，**+$0.52**，ROI **+19.6%**）；2 笔在持（均为 KXNATGASW 08-21 事件） |

实盘段的盈利来源：NATGASW 两笔 +$0.55（主要贡献）、WTIW +$0.02、CPI 族两笔小亏 −$0.05——
**那两笔 CPI 亏损仓恰好开在 nowcast 锚定上线之前**，锚定模型在同一事件的回放里站在对边。
真正的实弹检验在 9/11 的 8 月 CPI 结算。

> **同窗三个数字的关系必须一起读**（披露在冻结源 `source.disclosure`）：采纳参数定格模拟
> **+26.4%**（锚定模型下重放；参数为同窗最优，从未实际交易过该窗）/ 每日 PIT 滚动选参的
> 诚实回测（`:argminsel`）当时为 **−25.21%** / 旧默认参数 **−18.02%**。定格模拟与滚动选参
> 的差距 = raw argmin 在 n=2-11 事件上的过拟合代价实测。**最终裁判是实盘段的前向累积**，
> 用户已明确以新账本数周的真实表现为准。
>
> **为什么不把两段拼起来、也不把回测标成"实盘业绩"**：回测是假设性表现（hypothetical performance）。
> 在面向客户的资产管理页面上把它呈现为实际业绩是实质性误述——这是 SEC Marketing Rule 的地界。
>
> 2026-08-11 之前的纸面单**不进展示也不进台账**（完整备份 `data/macro_backup_20260811_prereset.db`）：
> 试运营阶段只保留最后一个制度状态,这是用户的明确决定;更早那段还混着已知 bug 时代的成交
> （#141/#148/#149/#150）,无法代表现在这套代码的行为。

### 结论 #129：这本账上还没有被证明可盈利的模型

初请模型 OOS Brier 0.165–0.172，市场 0.090–0.097——**模型明显输给市场**。这不是一句丧气话，而是整个 `skill.py` 闸门存在的理由：模型落后市场超过 50% 时，走模型路径下单就是在给交易所捐手续费，所以直接封禁。系统当前的价值在于**这套纪律本身**（PIT、预注册、影子验证、逐 bit 对账），而不在于任何一个模型的 alpha。

两处后续演化（#129 的大结论未被推翻）：**能源系列**是唯一在实盘段持续正贡献的家族；
**cpi/0.3.0 的 Cleveland nowcast 锚定**是第一个用泄漏免疫历史回放证明的模型级改进
（headline YoY 逐腿 Brier −33%），但它改善的是"落后市场的程度"，尚无证据表明已反超——
13 系列的周度 replay 仍然整体输给市场（`brier_behind_market_2win` 每周照发）。

---

## 休眠代码、试过但暂未启用的模型与统计手段

这一节是刻意写的：仓库里有相当一部分代码**能跑、有测试、但不参与实盘决策**。分不清"没启用"和"坏了"是最贵的误解，所以逐条列明。

### A. 影子模型（写预测，永不进决策）

三个模型每天都在 `refresh` 里跑、往 `preds` 表写行（日更日志：4 / 15 / 47 行），但 `decide_all:284` 有一道守卫——只有**注册表绑定的那个模型**（`model_version LIKE spec.model + '/%'`）的预测能驱动下单，影子成员按构造读不到。

| 模型 | 方法 | 覆盖（`preds` 实测） | 为什么休眠 |
|---|---|---|---|
| **`model/bridge.py`** (`bridge/0.1.0`) | **MIDAS / Almon 多项式滞后加权 OLS** ——用高频指标（汽油泵价、周度初请）桥接低频月度目标 | KXCPI / KXPAYROLLS / KXU3（210 行） | 采纳闸门（`research/eval.py` 的逐来源晋级判据）**还没写成可执行代码**，只有散文描述。没有判据就没有晋级路径 |
| **`model/ensemble.py`** (`ensemble/0.1.0`) | **逆 MSPE 对数池化**：按滚动 Brier 学习权重，权重截断后重归一；宽价差时把市场那一路的权重砍半（§23.2-3a） | 13 个阶梯系列（702 行）；分类结构的 `KXFEDDECISION` 跳过——`fed` 模型内部已经在做对数池化 | 同上。另外池化成员本身尚未证明优于市场，池化一堆输家不会赢 |
| **`model/ts_foundation.py`** (`chronos2/zero-shot-0.1.0`) | **Amazon Chronos-2 时序基础模型**，零样本、不微调，260 天上下文 → 分位网格 | KXWTIW / KXNATGASW / KXAAAGASW / KXJOBLESSCLAIMS（87 行） | 采纳判据要求**前瞻**样本（周频 ≥8 期 / 月频 ≥3 期）。**回测证据被主动判为无效**——基础模型的预训练语料几乎必然包含这些宏观序列，历史回测等于开卷考试 |

> 三者共同的堵点是同一个：**"晋级"目前是一段文字，不是一个函数**。
> 这在 `docs/PLAN_EXTENSION.md` 里被标为承重级缺口，因为它同时冻结了三个模型。

### B. 影子策略层（记录判决，永不执行）

| 机制 | 表（当前行数） | 做什么 | 为什么休眠 |
|---|---|---|---|
| **§25.4 单系列开关** (`strategy/series_enable.py`) | `shadow_series_enable`（120） | 按滚动 12 笔 ROI 决定某系列是否还值得下注；`OFF_ROI=0.0` 关、`ON_ROI=0.026` 开（迟滞恰好等于一个来回的净 taker 成本） | `SHADOW=True`。**当初的休眠论证（08-05 冻结窗 41 笔实测）：它会关掉 KXJOBLESSCLAIMS/KXNATGASW/KXWTIW——而后两者正好在 PR-2 与 PR-7/S2 的采样总体里**。在预注册检验中途改变总体等于毁掉检验。所以先记录、不执行，等前瞻样本跑满再由人拍板。（注意时效：那是旧规则时代的窗口；08-11 重置后的新台账里 NATGASW 反而是主要盈利源——正反两面都记着，判决仍归前瞻样本。）缺 artefact 时 `blocked()` **fail-open**（不挡），并由 `decide_all` 发一条 `series_enable` 告警 |
| **PR-7/S2 平仓规则** | `shadow_exits`（18） | 记录"如果阈值是 `hold_edge <= 0` 会在哪天平掉" | 等 30 笔前瞻样本（08-11 重置后计数重启）。S2 只可能比实盘规则**更早**平仓，所以是安全的嵌套比较；两条臂共用 `hold_state()` / `exit_realized()`，不允许重写一份 |
| **PR-2 argmax 过滤** | `shadow_argmax`（5） | 同时记录"下了"与"因为贵于公允而没下"两条臂 | 等 20 条腿（08-11 重置后计数重启）。两臂是嵌套关系（ON ⊂ OFF），构造上可配对 |

> §25.4 的所有常数都是**借来的，不是拟合的**：`WINDOW=12` 借自 `research.dsr.MIN_OBS`，
> `MIN_N=6` 借自 `strategy.skill.MIN_PAIRED`，`ON_ROI=0.026` 是费用几何算出来的一个来回净成本。
> 这样做的原因很直接：在一个已知负 ROI 的样本上拟合"何时关掉亏钱的系列"，
> 拟合出来的一定是"关掉所有东西"。

### C. 写完了但没接线的子系统

| 子系统 | 位置 | 状态 |
|---|---|---|
| **实盘下单客户端** | `exec/kalshi_exec.py` | 三道闸门 + RSA-PSS 鉴权全写完，但 `ops/` 与 `jobs/` **无人 import**（全仓库只有两处注释提到它）。闸门读的行有人写（见[上一节](#交易状态与实盘纪律)），只是从来没绿过 |
| **DFM 联合情景 VaR** | `model/dfm_bridge.py` → `ops/risk.scenario_var()` | 接线是通的，**采纳闸门自己判了不通过**：2026-08-04 的 `dfm_gate` holdout 里扩散模型协方差误差 **377.75**，样本协方差 379.29，Ledoit–Wolf **161.08**——要求"同时打赢两者"，实际输给 LW 一倍多。于是 `macro_risk.json` 停在 `independent_stake_sum`（独立敞口求和）基线。**这是设计中的可逆降级，不是故障** |
| **`FeatureStore.frame()` 统一特征帧** | `model/features.py` + `features` 表 | 所有模型确实都经 `FeatureStore` 读数（PIT 保证在这里），但**成帧并落库**这一层零调用方：`frame()` / `_persist()` 从未被调用，`features` 表 0 行。特征可复现性写在设计里，实际靠每次重算 |
| **§25.3 逐笔置信度模型** | `research/confidence.py` | 训练集由 `walkforward` 每次回测顺带产出，模型能训能评，但 `decide_all` **不读它**——它不是闸门，只是诊断 |
| **鞅性质检验** | `research/martingale.py` | 能跑，但没有任何调用方（refresh / weekly 都不跑它）。写来回答"价格路径是否漂移"，问完就搁置了 |

### D. 数据源退化 / 用代理顶替

| 项 | 现状 | 影响 |
|---|---|---|
| **AAA 汽油日均价** (`ingest/aaa_daily.py`) | 抓取已上线但**没有历史**——AAA 不免费提供，只能一天攒一行。至今 17 行（2026-07-31 起） | `KXAAAGASW` 结算的就是这个日读数，所以 `energy.py` 在读数够新时直接锚它，过期则退回 EIA `GASREGW` 周度代理（采样口径不同，模型以 σ 放大补偿）。**skill 闸门目前仍封禁该系列**——那是代理时代攒下的记录，日读数的优势只能向前累积 |
| **事件窗口快速重定价** (`jobs/tick.py`) | 固定 900 秒；设计要求发布前后切到更细粒度 | 发布瞬间的重定价窗口观测不到，`snipe` 能看到的只是 15 分钟后的残余 |
| **`event_flags` 结构性断点** (`analysis/llm.py`) | 已接线（`decide_all` 读 `active_flags()`，回测按 PIT 过滤读），整库仅 **5 行** | 停摆 / 大规模裁员 / 能源冲击这类"模型卡上写明会失效"的场景，实际上还没有被标注出足够样本去验证这条路径有没有用 |

### E. 试过、结论是"不做"的方向（保留记录以免重做）

| 编号 | 试了什么 | 结论 |
|---|---|---|
| **#140** | 价格止损（跌到某个价位就砍） | **否决**。回撤格的收益分布跨零——没有任何证据支持存在一个有效止损位。反方向（涨了回吐就跑）也被 PR-7 step 0 否掉，只剩 S2 进了预注册 |
| **#146** | §25.4 的原始论证"亏损 86% 集中在 5 个系列" | **前提不成立**。18 折 LOEO 里 `ser_roi` 全部被截断到零，集中度是统计假象。§25.4 因此被重写成纯迟滞规则并压进影子模式 |
| **#128 / #131** | 展示 +9.98% 的那次回测 | **是关闭闸门跑出来的**。同窗口开闸门是 −6.84%，逐轮修 bug 后一路走到 −23.26% → −25.64% → **−28.02%**。每修好一个 bug 数字就更难看一次，这本身就是"原来的数字在哪里虚高"的答案。展示段已按最新真值重冻 |
| **#124** | "skill 闸门错误封禁了 KXAAAGASW" | **复核后判定闸门是对的**（ratio 8.35）。不是 bug，不改 |
| **`research/selector.py`** | ~~早期的 ML 超参选择器~~ | **归类错了，2026-08-20 更正**。它跟 `param_select` 不在同一根轴上，谈不上"被取代"。`param_select` 选**模型参数**（喂给 `predict_all`）；`selector.py` 选**下哪一腿**——扩窗 LogisticRegression 预测每个候选结构的 p(win)，按 `EV = p̂ − cost − fee ≥ 0.03` 下注（`ml` 腿），`blend` 再按滚动 10 笔 PnL 在腿之间逐事件切换。**它是第 6、第 7 条流，不是选参器**。注意它跑在自己那套简化 harness 里：无 db_gates/pit_gates、无离场、固定 $1、每事件一注——所以它的 ROI 跟前端 WF Lab 同表并列的 `argmax`（带闸门、带离场）**不可直接比大小**，`data.ml.baseline` 才是它的对照组 |
| **试运营重置 + 展示切换（2026-08-11,用户指令）** | 账本清零重启（`decisions`/`fills`/`marks`/影子表;完整备份 `data/macro_backup_20260811_prereset.db`;市场数据/预测/结算全保留）;cutover → 08-11;展示历史段替换为**采纳参数定格模拟**（44 笔,hybrid,prod 形态;当时 +45.38%,08-15 锚定模型重放后为 **+26.44%**——见下一行） | **三个数字的关系必须一起读**（披露已写进冻结源 `source.disclosure`）:事后定格模拟（参数为同窗最优,从未实际交易）/ 每日 PIT 滚动选参 **−25.21%**（生产真实制度的诚实回测,run `:argminsel`）/ 旧默认参数 **−18.02%**。定格与滚动的差距 = raw argmin 在 n=2-11 事件上的过拟合代价实测。**真实前向记录 = 实盘段,2026-08-11 从零起算**——用户选择以新账本 4-8 周的真实表现作为最终裁判。PR-2/PR-7 前向计数随重置重启（判据未动,PREREGISTER 有案） |
| **Cleveland nowcast 锚定（2026-08-15,已上线,cpi/0.3.0）** | 用户指令"拿历史 nowcast 做 PIT 回测,好使就接入"。泄漏免疫回放（T-26h、结算 T-阶梯逐腿 Brier、strict survival）:headline 45 事件 0.0904→0.0610（−33%,29/45,每个年切片都改善）;core 44 事件 Δ−0.0005 平局。headline 按证据接线;**core 按用户决定接线**（依据不对称,PR-8 与模块 docstring 均有案）;claims 无等价源维持 skill-blocked | 配套(每项对应一个既往事故类):health 金丝雀跳过版本翻转 byte-compare;`param_argmin` 指纹含模型版本;d75 pin 行原位重生成;`predict_all` 开头挂 `refresh_if_stale` 盘中尾守卫。**锚定后 argmin 重选:CPIYOY/COREYOY 双双改采 `{}` 默认参数**——mu 被 nowcast 钉住后旧拟合集不再赢窗口,这是锚定生效的旁证。全部回测面（d30/d60/d75/冻结展示行）已在锚定模型下原位替换 |
| **manual_params 采纳（2026-08-11,生效中）** | 用户明示指令：将 75 天叉积（参数 × 流 × 离场,PnL 打分）在 **prod 规则约束下**（hybrid+离场开）的每市场 argmin 参数采纳进实盘。机制 `param_select.manual_params`：覆盖行优先于日选器、带 PIT 采纳时间戳（早于采纳时刻的模拟日不受影响）、`clear_manual` 一键回退、审计留痕 | **10 市场已写入**（claims/CPI×4/payrolls/U3/Fed/WTIW/NATGASW）,4 市场最优=默认未动（PCECORE/FEDDECISION/AAAGASW/GDP）。**DSR 反对意见在案**：全部市场 n_obs 1-11 < 12,搜索宽度 30-654 格,这些是折减门拒绝采纳的样本内 argmin,按用户指令上线。核对点:暴力全量扫描（`docs/PLAN_BRUTE_SWEEP.md`）完成后回头验证这批采纳在组合真值下是否仍是 argmax |
| **75 天全市场参数网格搜索** | 2026-08-11：14 市场逐一定制参数空间（读全部模型的 DEFAULT_PARAMS 面设计；claims 91 组 / CPI 族 82-109 / payrolls 109 / u3·fed 82·10 / WTIW·NG 21 / AAA 7 / PCE 5；live-key 探测用窗前事件,网格设计不见窗口）,banded Brier 逐事件打分,argmin 报最优 + DSR 折减判adoption | **argmin 每个市场都能"赢"（Δ+0.006~+0.18）,但 14/14 全部未过 DSR 门**——75 天窗内可打分事件仅 1-11 个/市场,全部低于 `dsr.MIN_OBS=12`,折减检验根本无法启动;在 2 个事件上搜 109 组参数是查表不是学习。有价值的副产品:claims 的 argmin 赢家恰是 **PR-1 已注册的候选权重 (0,0,0.3,0.7)**(外加 seasonal_years=15),与预注册线索独立收敛——判决仍归 PR-1 前向检验,不归本搜索。结果存 `/tmp/grid75_results.json`（脚本规格在 commit message） |
| **标定设计研究** | 2026-08-11：毒图事故后测 4 种标定设计（Platt/beta/事故版等渗/守卫版等渗）× 2 协议，跨 14 系列池化前向链，逐腿 Brier 判分 | **identity 全胜**（raw 0.0767 < 全部候选 ≤0.0806；市场 0.0375 斩半所有人）。带盘口事件每系列仅 2-12 个，任何单系列标定都是在假 n 上拟合；模型的病是落后市场，不是存在可学习的单调失准——能治那个病的变换是向市场价池化（`fair_mode='pooled'`，另行预注册），不是重塑模型概率。**identity 钉是测量出的最优，不是权宜**。详见 `strategy/calibration.py` docstring |
| **§29.9** | 预报 vintage：CPC 6-10/8-14 天展望存档（新源 `ingest/weather_fcst.py`，136k 行，2012→今） | **五规格全否，天气全案终审闭合**（累计 K=42）。Stage 0 证明预报技巧真实（冬季 corr +0.818），但官方发布（21Z）本身就落后于市场直接看的模式原始运行 12-72 小时——冷修正的样本内系数 **−2.375 方向反了**（买预期卖事实）。表保留 shadow；唯一潜在用途是 Kalshi 城市温度类市场（新 family，需拍板）。详见 §29.9 |
| **§29.7** | 替代数据二轮复核：负荷加权天气→NG、天气→初请、汽油库存→AAA 零售 | **三条全否**。天气那条堵死了"当年是人口加权用错了"这个退路——改成负荷加权后每个数字都**更负**；天气→初请的滞后污染机制被系数符号证伪。汽油库存兑现了 §269 的 `复评条件 n≥56`（改用 n=1022 的 walk-forward），维持不接。**其中第一版跑出的 +0.0109 是自造的 look-ahead**：按 `event_time` 取 EIA 周报＝拿到 2 天后才发布的数字，按 `knowledge_time` 设门后主特征直接翻号。详见 `docs/PLAN_ALTDATA_EXEC.md` §29.7 |

---

## 已知边界（写在前面，免得当成新发现）

- **模型卡里逐条列了失效场景**（`model/registry.py`，中英双语）：初请对突发大规模裁员与政府停摆无感；非农对罢工月、天气月、基准修订月无感；FOMC 规则基于 1990–2026 基准率，对会间紧急行动与主席更替无感；能源模型对 OPEC 冲击、地缘事件、换月无感。
- **入场侧熔断是一个已披露的结构性缺口**：`alerts` 表没有 ack 时间戳，因此"告警被确认的时刻"无法在回测里复现。实测影响为零（`offset_hour=16` 下无一笔受影响），选择**披露而非修补**。
- **周频系列在 API 窗口内永远凑不满选参门槛**：Kalshi 只保留约 75 天 K 线，一个周频系列最多 10.7 期，而 `dsr.MIN_OBS = 12`。这正是 `archive_candles` 存在的意义——本地库是永久的，计数单调增长，两周左右就能越过门槛。

---

## 相关论文

同一套数据与纪律衍生出的研究（`../prediction_market/research/martingale_pricing/`）：
**《A Local-Volatility Theory of Prediction Markets: Absorbed Martingales on the Simplex and an Identification Law for Transient Mispricing》** —— 把预测市场价格建模为单纯形上的吸收鞅，给出暂时性错价的识别律。中英双版本，英文版为准。

---

<p align="center"><sub>Someo Park Investment Management · 研究用途 · 本页任何数字都不是投资建议</sub></p>
