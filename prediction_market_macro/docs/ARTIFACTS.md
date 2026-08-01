# ARTIFACTS.md — 前端 14 个 Macro 视图全解（每个视图：含义 / 输入 / 计算 / 性质 / 输出读法 / 参数）

> 2026-07-31 定稿。配套代码：`ops/frontend_export.py`（导出）、
> `someo-park-investment-management/src/components/macro/MacroArtifact.tsx`（渲染）、
> `server/tools/macroMarketTool.ts`（chat 接地）。
> 主次原则：**质量与风控四件套（校准 / WF 实验室 / 覆盖 / 风险）以"结论在上、机制在下"排版**；
> 各系列视图是生产实时快照；WF 实验室是 as-if 回填口径（见 §11）。

---

## 0. 总管线（所有视图的数据从哪来）

```
ingest（FRED/ALFRED、Kalshi 盘口+K线、yfinance 期货(含 ZQ 联邦基金)、EIA 库存、AAA 日度油价、Fed 声明）
   │  全部写入 SQLite，宏观观测带 knowledge_time + vintage_date（PIT 铁律：模型只读 kt ≤ asof）
   ▼
predict_all（每系列一个生产模型：fed/0.2.0 三源、inflation、labor、energy/0.4.0 …；shadow 成员另存不上板）
   ▼
decide_all（生产闸门：net_edge≥0.04、熵门、便士下限 0.10、模型-市场分歧≤0.25、技能分层、熔断、
            距结算≤7 天；四条腿：edge / 顺市大热 argmax / 套利 arb / 印后狙击 snipe）→ decisions 台账（append-only）
   ▼
marks（盯市）+ settle_pass（结算 → settle_note + z 归因）
   ▼
eval（每周日：Brier 重放、决策重放、DM 检验、校准表 → experiments 表 → series_gate 实盘门）
   ▼
frontend_export.run → public/data/macro_*.json（NaN 防御、生产模型过滤）
   ▼
前端（Firebase 静态托管；/data 在构建时打包 → 每日 06:30 世界杯管线 build+deploy 顺车上站）
```

**定时任务（launchd）**：`macrotick` 每 15 分钟（盘口/K 线/事件窗加密轮询）；`macrorefresh`
每日 05:00（全链 ingest→predict→decide→export→日报 PDF）；`macroweekly` 周日 06:30
（`refresh --weekly`：另加回测/eval/归因/**WF sweep→标准 30d run→ML 选注器→周报→再导出**）；
`macrowatchdog` 每小时（SLA 漏报侦测）。

**性质总述**：全系统 **paper**（纸面，$1 风险单位，Kalshi demo bankroll）；实盘门 13/13 未过
（模型 Brier 落后市场——这是门该说的话）。视图分三种性质：
- **生产实时**：看板/系列/决策/业绩/分歧/覆盖/风险/报告——展示的就是当下生产状态；
- **OOS 回放**：校准页——把历史结算重新打分/重放，回答"模型准不准、规则赚不赚"；
- **as-if 回填**：WF 实验室——假设生产 30 天前就上线，逐日 PIT 重建，回答"新规则的战绩"。

---

## 分组一：总览

### 1. 宏观看板（macro_board.json）
- **含义**：主入口。健康灯条 + 未来发布日历 + 每系列当前预测与最新决策。
- **生成**：`frontend_export.run` 看板段；数据 = registry 日历 `next_release` + 每个活跃
  (series, period) 的**最新生产模型** pred（`model_version LIKE '<spec.model>/%'`，shadow 永不上板）+
  最新决策行（open/pass/exit）。
- **更新**：每次 refresh（每日 05:00 + 手动）；`Generated` 时间戳 = 导出时刻。
- **输出读法**：
  - 顶部**健康灯条**读 `macro_health.json`：每系列 red/yellow/green。red 分两类——
    完整性红（replay_mismatch / pred_stale / ladder_mass，会触熔断）与质量红
    （brier_behind_market_2win 等，全系列常见、不触熔断）。
  - "下次发布"表：series / 期数 / 定时（UTC）/ 倒计时。
  - 每系列卡：`模型: μ ± σ`（gmix 均值±标准差；经验分布则显示分位）；决策 chip
    （PASS 灰 / open 绿 / argmax·arb 蓝 / snipe 黄）。
- **参数**：无用户参数。坑：日历来自 BLS/BEA 官方排期 + 周度漂移联网检查，2027 精确日程官方未发布前是估计。

### 2. 系统总览（macro_overview）
- **含义**：四段静态文案（i18n），讲系统是什么、怎么闸门、为何 paper。无数据文件，不会过期数字。
- chat 接地时 overview = board+performance+oos 三文件拼合摘要。

---

## 分组二：各系列（都是 board 的家族过滤二级视图，生产实时）

### 3. 美联储会议（macro_fed.json）
- **含义**：每次 FOMC 会议的五桶概率（C26 降≥26bp / C25 降 25bp / H0 不动 / H25 加 25bp / H26 加≥26bp）。
- **生成**：每会议期取最新 `fed/%` pred；**已结算的会议自动剔除**（对照 KXFEDDECISION
  settlements——已开完的会没有概率可言，其结果在决策/业绩视图里看）。
- **计算（fed/0.2.0 三源 log-pool）**：规则源（Taylor 型 + core_yoy/du12）权重 0.15、
  Kalshi 市场 devig 0.35、**ZQ 联邦基金期货 FedWatch 链式推算 0.50**
  （逐月合约 implied = 100−价格，按会议日在当月天数占比拆 pre/post 利率，链式前推）。
- **输出读法**：每卡=一次会议；横条=模型概率；下表"规则/市场/模型"三行对比五桶。`模式`
  行 `rule+market+ff` 表示三源齐备；缺 ZQ 时回退 `rule+market`（0.4/0.6）。
- **坑**：市场行概率来自合约盘口 devig，无盘口的远月会议市场行可能全 0——不是 bug，是没有报价。

### 4. 通胀 / 5. 就业市场 / 6. 能源
- **含义**：board 按 family 过滤（inflation：CPI/CPICORE/CPIYOY/CPICOREYOY/PCECORE；
  labor：初请 JOBLESSCLAIMS/非农 PAYROLLS/失业率 U3；energy：WTIW/NATGASW/AAAGASW）。
- **模型概况**（详见各 model/*.py 头注释）：通胀=月度分量模型（能源分量吃 AAA/RBOB 传导）；
  就业=初请状态外推 + 非农/U3；能源 energy/0.4.0 = **期货 GBM ±EIA 库存惊奇倾斜（±5%/周封顶）**，
  汽油另有 **AAA 日度锚**（新鲜≤3 天直接锚定）→ 否则走前 OLS 漂移回归（3 参数）→ 否则阻尼趋势。
- **输出读法**：与看板系列卡一致；`期数` 是合约结算期（周度=结算周五，月度=数据月份）。

---

## 分组三：交易与持仓（生产实时）

### 7. 决策与盯市（macro_decisions.json）
- **含义**：台账最后 200 行 + 最新一次盯市。**台账 append-only**——历史错误行不删除、读取端去重。
- **kind 词典**：`open` 模型边际开仓 / `pass` 过闸未下 / `argmax` 顺市大热（fair≤cost 才下，$1）/
  `arb` 无风险套利 / `snipe` 印后狙击 / `exit` 主动平 / `cancel` / `settle_note` 结算记录
  （含 realized、z 归因：|z|<1 运气区、1-2 灰区、>2 模型错）。
- **字段**：fair（模型公允）/ ask（成交价）/ net_edge（扣费净边际）/ size_usd / note。
- **盯市**：最新 marks 快照，`pnl_usd` = (mid−entry)×count−fee，未实现。

### 8. 业绩表现（macro_performance.json + macro_pricetrack.json）
- **含义**：paper 账本汇总 + 盯市曲线。
- **计算**：`pnl.report`——开仓按系列聚合；**结算按 (series,period) 取首个 settle_note**
  （防历史重复行）；按 origin（open/argmax/arb/snipe）分拆结算盈亏；open_by_kind = 当前未平仓。
- **输出读法**：bankroll（Kalshi demo）、unrealized（最新盯市合计）、mode=paper 常挂。
  pricetrack 图 = 每次 tick 盯市的组合 PnL 时间线（≤500 点）。
- **坑**：**未平仓多为 6-7 月旧规则开的仓**，红色未实现 PnL 是旧规则的遗产；新规则的水平看 WF 实验室。

### 9. 模型 vs 市场（macro_divergence.json）
- **含义**：模型与市场分歧排行，"哪里我们最不同意市场"。
- **计算**：`gap_norm = |模型阶梯均值 − 市场 devig 阶梯均值| / strike 区间宽`；市场端要求有限桶
  质量 >0.3 才算（防外推垃圾）；>0.15 视为显著。
- **读法**：分歧大 ≠ 机会大——生产闸门反而把 gap>0.25 的单拒掉（弱模型大分歧=模型错的概率更大）。

---

## 分组四：质量与风控（重点四件套；排版=结论在上、机制在下）

### 10. 校准（样本外）（macro_oos.json）— 性质：OOS 回放
**页面顺序（2026-07-31 重排）**：流水线说明 → **① 实盘门结论** → **② 决策重放** → ③ Brier 打分明细 → ④ 30d WF 摘要。

- **① component_gates**：`series_gate`（每系列 real/paper 判定 + 理由清单：n≥12、Brier 胜市场、
  ROI>0、edge_capture>0.4、DM p<0.10）、`dfm_gate` 等。**这是整页的结论**：13/13 paper。
- **② 决策重放（decision_replay）**：把**生产 decide()** 原样跑在全部已结算历史上
  （入场扫描 close−7d…−1h，熵门生效；**排除**校准/技能/捕获等"吃库状态"的自引用门），
  回答"这套规则历史上会不会赚钱"。ROI 已扣费。**设计上是照妖镜：红=诚实**，
  它测的是模型边际旧路线（AAAGASW +111% 是唯一常绿），新路线的战绩看 WF 实验室。
- **③ scoring replays（\*_replay 卡）**：逐系列 Brier（模型 vs 市场）分 horizon（24h/1h）+ CRPS。
  `落后市场` 红 chip = 该 horizon 模型不如市场——目前普遍如此，正是 paper 的原因。
- **④ 30d WF 摘要**：`daily_walkforward` 实验行的压缩版（笔数/胜率/PnL/分系列），
  与 WF 实验室同源；完整版去 WF 实验室看。
- **更新**：experiments 行每周日全量重写；导出每 refresh。两页时间戳可能不同（同源不同刻）。
- **参数**：重放窗口 = 全部已结算样本；DM 检验 HLN 小样本修正；bootstrap CI。

### 11. Walk-Forward 实验室（macro_walkforward.json）— 性质：as-if 回填（本项目的"战绩页"）
**页面顺序（2026-07-31 重排）**：**"30 天前上线·实盘口径"标题 → 窗口/覆盖/最优提前量 →
① 三线对比表 → ② Bet 历史五标签** → ——方法学分割线—— → ③ 入场提前 sweep → ④ 净值曲线 → ⑤ 覆盖清单。

- **核心口径**：假设生产 **30 天前上线**。每天 16:00 UTC 用当日 PIT 可知信息重建盘口、
  跑生产模型 + decide()，一期一仓，按真实结算结果结账。"回填"指历史是补跑的，
  但每一步只用当天已知信息（kt≤asof），不偷看未来。
- **① 三线对比表**（`ml` 块，同一逐结构数据集上的三条走前线，窗口 last30/last60）：
  - **大热线（基线）**= 顺市大热复刻：每事件取模型最大 fair 的结构（价格 0.10-0.90、fair>0.5），
    **仅当市场信心≥模型（fair≤cost）才下**（市场是被证明更强的预测者，弱模型说大热被低估=逆向选择）。
  - **ML 线**= 扩窗逻辑回归选注器（`research/selector.py`）：特征 fair/cost/edge/|edge|/
    is_argmax/market_backed/点差/熵/提前量/腿型/家族 one-hot；标签=该结构扣费后是否盈利
    （**bucket 按真实 outlay=1+eff 结账**，settle_cash()，防彩票假象）；每事件等权、无类平衡、
    训练集只含入场日前已结算事件；EV=p̂−cost−费≥0.03 才下。参数：MIN_TRAIN=150、EV_MIN=0.03、
    价格窗 0.10-0.90、风险≤$1/注。
  - **智能切换（blend）**= 逐事件跟随"入场前已结算滚动战绩（近 TRAIL=10 注）"更好的那条线；
    ML 需 MIN_SWITCH=5 注结算才可领先；被选线弃权则落到另一线。
  - **采纳规矩**：挑战者须 **last30+last60 两窗同时**胜基线才可晋级；blend 当前形式过线但
    样本薄（8/22 注）且盈利集中于单笔等风险注 → **仅展示为候选，不上实盘**。
- **② Bet 历史**：五标签 = 混合（实盘规则 edge+argmax）/ 边际 / 大热 / ML / 智能切换；
  逐注：入场日/系列/结构/提前量/投入/胜负/盈亏/累计。混合/边际/大热来自 `daily.streams`
  （30d canonical run），ML/切换来自 `ml.last30` 与 `ml.blend.last30`。
- **③ 入场提前 sweep**：**只统计边际线**（这是方法学试验：同一 30 天窗口按提前 1/3/5/7 天
  各跑一次完整 PIT 回测）；7d 绿 = 信息刷新周期结论，生产 `max_days_to_close=7` 由此而来。
  短提前期红是真实结论（信息陈旧则边际线亏），**不是系统亏钱的意思**。
- **更新**：每周日 weekly 依次跑 sweep → **标准 30d run（必须最后，sweep 分 lead 会覆盖同一行）** →
  ML 选注器 → 再导出；平时导出取 experiments 最新行。
- **坑**：撮合按当日 K 线 close 近似（无深度冲击）；30d/60d 窗口起点不同会导致同名线注数略异。
  **口径注意（2026-07-31 二次修订）**：三线对比表的"大热线"两行 = **实盘口径**，与 Bet 历史
  "大热线"标签完全同一组数字（last30 取 30 天跑、last60 取 60 天跑的 `streams.argmax`；
  周程为此新增 `weekly_walkforward_60d`，导出按 window 显式过滤取行，不依赖运行顺序）。
  ML/智能切换跑在选注器数据集上（每事件单一 PIT 入场点，机会少于逐日扫描），笔数天然更少，
  比较看 ROI 与胜率；选注器内部的"基线复刻"仍存于 `ml.baseline`（供同轨方法学参考），不再上表。

### 12. 覆盖矩阵（macro_coverage.json）— 性质：生产实时（运维）
**页面顺序（2026-07-31 重排）**：矩阵 → 漏报 → 一致性/模型告警（error/warn 常显，**info 巡检折叠**）→ 运维告警。

- **矩阵**：行=14 系列，列=期数；状态机 chip：`SCHE` 已排期 → `PASS`/`RECO` 已预测已决策 →
  `open` 持仓 → `reconciled` 已对账；`missed` 红=错过 SLA。
- **告警语义**：`error` 红（熔断/健康红/结算保险丝）→ 要处理；`warn` 黄（质量降级）→ 要知道；
  `info` 灰（TERM-STRUCTURE 期限结构斜率、FED-MUTUAL 阶梯互证等**例行巡检读数**）→ 仪表非异常，默认折叠。
- **ack 机制**：`alerts.acked=1` 即从面板消失；**ack 一条 circuit_breaker 告警 = 人工复核释放熔断**（铁律 10）。
- **已知案例**：2026-07-31 KXWTIW `replay_mismatch` 红——EIA 原油库存深历史回填（至 1982）
  改变了回填前预测的重放；回填后预测逐字节复现，属一次性数据事件，已 ack 释放。

### 13. 风险限额（macro_risk.json）— 性质：生产实时
**页面顺序（2026-07-31 重排）**：**当前敞口** → 情景压力 → 限额配置 → 执行说明。

- **当前敞口**：未平仓 (series, period, size)。**先看你现在押了什么**。
- **情景压力 scenario_var**：当前持仓在极端情形（全输）下的最大损失估计。
- **限额 LIMITS**：per_event / per_family / per_cluster($8，比计划 $40 保守) / gross /
  per_release_day($30) / max_size_usd($1) 等。
- **执行**：每次下单前 `risk.check` 逐单强制（超限=拒单，写 pass 行入台账）；另有滚动 20 单
  回撤熔断（结算+主动平仓都计入）。

---

## 分组五：报告

### 14. 报告（macro_reports.json + /data/macro_reports/*.pdf）
- **含义**：日报（board 快照+持仓盯市+告警）与周报（另加校准十分位表+闸门+归因）PDF。
- **生成**：refresh 每日 `report_daily`，weekly 加 `report_weekly`；渲染 bug 修复日（2026-07-31 12:00）
  之前的 PDF 留盘不上面板（每份都是同一 DB 的快照，不丢信息）。
- **查看器**：世界杯 Pdfs 套件原样复刻（2px ink 标签、cache-buster、整高 iframe）。
- **坑**：Open positions 表的红色 uPnL 是**旧规则存量仓位**的未实现盯市（见 §8 坑）。

---

## 附：本次撰写自查发现并已修复的问题（2026-07-31）

1. **chat 接地 ABOUT 两处过时**：`macro_energy` 还写着"未建模（P1）"（实际 energy/0.4.0
   已投产），`macro_reports` 写着"M8 后才有"且文件映射错指 performance —— 均已更正。
2. **美联储视图显示已结算会议**（2026-07 开完仍挂概率板）→ 导出侧对照 settlements 过滤（commit 10eaee4）。
3. **WF 实验室时间戳取 sweep 的生成时刻**导致"数据生成于"显示偏旧 → 改为整文件导出时刻优先。
4. **sweep 表主次误导**（全红置顶像"系统亏钱"）→ 移入"方法学明细"分区并在本文档写明它只统计边际线。
5. **info 巡检刷屏**淹没真告警 → 覆盖矩阵折叠 info 级，error/warn 常显。
6. **已知重复**：校准页的 30d WF 摘要与 WF 实验室同源不同刻，数字可能短暂不一致——按导出时间戳判断新旧，属设计内。
7. **已结算市场不下架**（KXFED 26JUL 全腿结算后仍上板）：根因是 `sync_settlements` 对 contracts
   用 INSERT OR IGNORE——已存在的 'active' 行不会被改写，而 Kalshi 列表 API 结算后不再返回该合约，
   状态永远停在结算前。修复：结算落库时显式 UPDATE status='settled'（kalshi_md.py）+ 一次性回填
   30 行存量；看板/分歧等按 status='active' 取期数的视图自动痊愈。
8. **对比表口径二次修订**：三线表"大热线"改为实盘口径（与 Bet 历史同数字，last30/last60 取
   30/60 天跑的 streams.argmax；weekly 新增 60d 跑）；选注器数据集复刻退居 `ml.baseline`。
