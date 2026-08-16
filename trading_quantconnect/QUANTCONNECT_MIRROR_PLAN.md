# QuantConnect 模拟盘镜像 — 架构设计(PLAN,2026-08-16)

> 状态: **PLAN ONLY,未写代码**。批准后按里程碑实施。
> 纪律: 不做 fallback 式简化 —— 每个已知难点给出**明确的工程解**;测试全进
> /tmp,生产文件只读;本目录(`trading_quantconnect/`)是唯一新增写入面。

---

## 0. 目标与不变量

用 QuantConnect(QC)云端 paper trading 做五策略组合的**实盘追踪镜像**:

- **镜像保真**: QC 模拟账户的持仓在稳态时与本地 golden 持仓文件**逐票逐股一致**
  (含空头负股数、BDC 小数股);差异只允许来自"在途订单"这一个瞬态。
- **时序规则(用户规格,§4 形式化)**: 持仓文件变化发生在盘中(9:30–16:00 ET)
  → 立即市价下单同步;盘前 → 等 9:30 开盘下单;盘后 → 次日开盘下单;
  节假日 → 顺延到下一交易日。交易日历以 QC 交易所日历为唯一权威。
- **NAV 一致性**: QC 账户 equity 与本地 controller 实时 NAV(账本口径)之间的
  偏差,除已知的 15 分钟行情延迟外,预算内可分解、可对账、超限报警(§6)。
- **零策略逻辑外移**: QC 端**不做任何决策**,只做"目标持仓执行器"。所有信号、
  选参、风控仍在本地管道;QC 是执行与滑点现实性的镜子。

---

## 1. 事实基础: 持仓文件深度解剖(2026-08-16 实测)

### 1.1 每策略的"可执行真源"(executable truth)

镜像的第一原则: **QC 追的是"股票层实际可执行持仓"**,不是策略内部记账层。
五策略的股票层真源各不相同,必须逐个点名:

| 策略 | 股票层真源 | 层级说明 | 更新者/节奏 |
|---|---|---|---|
| MRPT | `inventory_mrpt.json` → `pairs.{P}.s1_shares/s2_shares`(仅 `direction≠null` 的对) | 对→双腿展开;short 对 s1 为负 | 凌晨 DailySignal;盘中 monitor 可 CLOSE_STOP |
| MTFS | `inventory_mtfs.json` 同上(现 9 对 14 腿) | 同上 | 同上 |
| AISS | **`account_aiss.json.positions`**(个股 9 票) | ⚠️ inventory 是**子板块合成资产层**(equipment 41 股≠个股),不可直接执行;account 才是实仓 | qlib 晚间管道;月度调仓才变 |
| SSRS | `inventory_sector_rotation.json.holdings`(ETF 整数股,与 account 逐票一致,二选一以 account 为准同 AISS 对齐) | ETF 层即可执行层 | 同上 |
| BDC | `inventory_bdc.json.holdings + cash(BIL)` | **小数股**(DRIP 演化,如 GBDC 28,686.3968);现金以 BIL 持仓存在 | 凌晨管道;仅分红日变 |

### 1.2 关键 schema 字段(镜像相关)

```text
pairs inventory (mrpt/mtfs):
  pairs.{“A/B”}: direction(long|short|null), s1_shares, s2_shares(带符号),
                 open_date, open_s1_price/open_s2_price(真实入场价,QC 对账基准),
                 days_held, monitor_log
  纪律: HOLD 不改 shares;monitor(盘中)只写 CLOSE/CLOSE_STOP;
        direction=null 的是候选/化石槽(如 ALL/BK),必须过滤。

account (aiss/ssrs/mrpt/mtfs, schema 同族):
  as_of, cash, equity, positions.{T}:{shares, avg_cost, entry_date},
  cumulative_realized/dividends/fees, lots(pairs: “P|s1” 腿级 lot)
  cumulative_fees 恒为 0 → QC 必须配零费率模型才可比(§5.6)。

inventory_bdc: holdings.{T}:{weight, shares(小数), drip_events, entry_date},
  cash:{ticker:"BIL", shares}; 三层强校验(ledger 重放==inventory)已内建。
```

### 1.3 已知怪癖(设计必须吸收,不是绕开)

1. **半更新窗**: DailySignal 先写 inventory、约 1 分钟后写 account —— 直接
   watch 原始文件会读到中间态。**解**: 不自己重解这个问题 —— 目标源直接用
   controller 的装配输出(§3.1),controller 的 `_maybe_rebuild` 守门 + 双引擎
   对拍已经解决了中间态(装配失败保守沿用旧结构)。
2. **文件 mtime ≠ 持仓变化**(HOLD 重写文件但 shares 不变)。**解**: 变化检测
   基于**规范化持仓内容哈希**(controller `structure_hash` 现成)。
3. **AISS 实仓 vs 报告目标漂移**(rebalance 未执行完,account 才是真仓)——
   镜像 account 即自动正确。
4. **跨策略同票**(如 MU 可同时在 AISS 与某 pair): QC 单账户天然按净额持仓。
   **解**: 目标端先做**净额扁平化**(controller flatten 已做,含反向索引
   `risk_matrix_latest.json: leaf → [(node, eff_shares)]`),归因信息进订单 tag。
5. **改名**: 本地已有 `ticker_aliases`(日期窗口+身份锚);QC 侧用其 SID 体系
   自动跟踪改名(BK→BNY 型在 QC 内部是同一 Security)。两侧各自正确,对账层
   做一次 ticker 归一即可。
6. **盘中变仓真实存在**(monitor CLOSE_STOP、event-risk 去风险 overlay)——
   "盘中立即下单"路径不是理论分支,是必测主路径。

---

## 2. 总架构: 三平面

```
┌─ Target 平面(本地)────────────────────────────────────────────┐
│ controller 常驻循环(已在产, 1min, 双引擎对拍, watcher 守门)      │
│   └─ exporter(新): 读 risk_matrix_latest + nav_latest(只读)     │
│        → target_portfolio.json {version↑, structure_hash, ts,   │
│           targets:{ticker: net_shares}, attribution, guards}    │
│        经既有 express server + cloudflare tunnel 暴露           │
│        GET /api/qc/target(独立 bearer key,与面板 key 分离)      │
├─ Execution 平面(QC 云)────────────────────────────────────────┤
│ 单一 Live Paper 算法 “SomeoPark Mirror”                          │
│   OnData/Scheduled: 拉 target(RTH 每 1min;09:28 预拉一次)       │
│   版本号幂等 + ObjectStore 持久化 last_applied_version           │
│   diff(target, Portfolio) → 市价单(带策略归因 tag)              │
│   时序状态机(§4): 盘中即时 / 非盘中挂起至下一开盘(QC 日历)       │
├─ Verification 平面(本地)──────────────────────────────────────┤
│ 每交易日 16:20 ET: QC Read API 拉 持仓/现金/equity/成交流水        │
│   ①持仓 vs target: 必须 0 差(在途单除外)                        │
│   ②equity vs controller 16:00 账本收盘: 分解对账(§6 预算)        │
│   ③成交 vs 本地 ledger 入场价: 滑点归因                          │
│   → trading_quantconnect/reconcile/qc_reconcile_{date}.json     │
│   超限报警进 daily 报告;后续里程碑接前端 quality checks          │
└────────────────────────────────────────────────────────────────┘
```

### 3.1 为什么目标源是 controller 而不是直接读 5 组文件(核心决策)

controller 已经解决了本方案 80% 的难题,且**每天在生产被双引擎对拍验证**:

- 半更新窗守门、内容哈希变更检测、跨策略净额扁平化、ISIN 身份锚、
  装配失败保守沿用(fail-static)、7×24 watcher、心跳可观测。
- exporter 因此薄到只做: 读两个 json(只读)→ 映射 ISIN→ticker(security
  master `render`)→ 原子写 target 文件 + 版本号自增。
- **风险与解**: controller 挂 → target 停更。exporter 在 target 里带
  `controller_heartbeat_age`;QC 算法读到 age>10min 时**冻结在最后已知目标**
  并打 QC 端日志报警(fail-static + 大声,绝不猜)。本地已有 launchd 守护 +
  前端心跳报警兜底 controller 本身。

### 3.2 QC 账户与资金映射

- **口径**: 镜像**账本口径**(ledger basis)—— 持仓文件里的 shares 字面就是
  这个口径($1M/策略起账)。官方口径是展示层变换,不进执行。
- QC 初始资金 = go-live 当日 Σ5 策略 account equity(精确值,≈$5.07M),
  一次性设定,此后 QC 自演化。
- **保证金**: 全书 gross(现约 $6.3M: pairs gross 2.2M + 多头 4.1M)/net 5.07M
  ≈ 1.25×,Reg-T 2:1 之内;显式配 `SecurityMarginModel(2.0)` + 拒单即报警
  (§7-F4),不静默缩单。
- **费用/利息**: QC 配零费率 FeeModel(与本地 `cumulative_fees=0` 口径对齐);
  空头借券费/保证金利息 QC paper 不计,与本地一致 → 不引入口径差。

---

## 4. 时序规则(用户规格的形式化)

QC 算法内单一状态机,交易日历/时钟以 **QC 交易所日历为唯一权威**(节假日、
半日市、DST 全部内生解决,本地不再自建日历):

| 事件 | 市场状态 | 动作 |
|---|---|---|
| 新 target version | RTH(9:30–16:00 ET) | 立即 diff → 市价单(得最新成交价) |
| 新 target version | 盘前(含 9:30 前任意时刻) | 存 `pending_version`;09:30 开盘事件触发执行 |
| 新 target version | 盘后(>16:00)/周末/节假日 | 同上,顺延到**下一交易日** 09:30 |
| 开盘时刻 | — | 若有 pending: 09:30:00 触发拉取最新 target(不是执行旧缓存 —— 隔夜可能多次变更,**只执行最新版**),diff → 市价单 |
| 多次变更同窗口 | 任意 | 版本号单调,永远只追最新;中间版本天然合并(目标态语义,非增量单) |
| 半日市(13:00 收) | QC 日历判定 | "盘中"窗口自动缩短,13:00 后规则同盘后 |
| 个股停牌 | QC 拒单/不成交 | 该票挂起重试 + 报警,**其余票照常**(不因单票阻塞全书)(§7-F6) |

开盘执行细节: 09:30:00 用市价单(用户规格"开盘下单取最新成交价")。开盘首分钟
价差较宽是**真实执行成本**,镜像的目的正是把它量出来 —— 不做"等 5 分钟"之类
的美化。备选 MOO(参与开盘竞价)列入 R3 研究项,由对账数据决定是否切换。

**目标态语义**(重要): QC 端执行的是"把账户调到 target 状态",不是重放本地
逐笔。这使任意错过/重启/乱序都自愈 —— 收敛到最新 target 即正确。

---

## 5. 关键工程决策清单

| # | 议题 | 决策 | 理由 |
|---|---|---|---|
| D1 | 一个算法 vs 五个算法 | **单算法净额镜像** + 订单 tag 归因 | 跨策略同票净额唯一正确;五账户会把 pairs 空头与 AISS 多头拆成两笔虚假对敲 |
| D2 | 目标传输 | QC 算法主动拉(HTTPS `Download()`,bearer key) | 复用既有 tunnel 基建;推送(ObjectStore API)列 R2 备选,拉模式无本地→QC 依赖 |
| D3 | 幂等 | target 带单调 version;算法 ObjectStore 记 last_applied | 算法重启/重部署/网络抖动全自愈 |
| D4 | 订单类型 | MarketOrder(RTH)| 用户规格;paper 按 NBBO+QC 滑点模型成交,即所求"最新成交价" |
| D5 | **BDC 小数股** | 首选 QC 原生小数股下单(R1 验证);若 paper брokerage 限整数 → **显式残差账**: 整数股执行 + `fractional_residual.json` 逐票记差,残差市值>1 股价值时并入下次单 | 这是精确的会计设计,不是舍入了事 |
| D6 | 股息/DRIP | QC 收现金股息;BDC 的 DRIP 在本地 inventory 加股 → target 增股 → QC 用股息现金买入 —— **闭环自洽**,支付日差异进对账分解 | 不在 QC 复刻 DRIP 逻辑(单一真源纪律) |
| D7 | 拆股 | QC 自动调整持仓;本地 controller 标注 + 策略文件为 golden → 拆股日 target 与 QC 同步跳变,对账日做拆股感知比对 | |
| D8 | 数据订阅 | Minute 分辨率、Raw 归一化(与真实成交价对齐;Adjusted 会造成历史比价失真) | |
| D9 | 凭证 | `.env`: `QC_USER_ID`/`QC_API_TOKEN`(user 提供);`QC_TARGET_KEY`(endpoint 独立 bearer);永不入 git | |
| D10 | 首次建仓 | go-live 用同一状态机: 部署时拉 target 全量建仓(= 一次"大变更"),从空账户收敛 | 不写特殊初始化路径 |

---

## 6. NAV 一致性预算(与 controller 实时 NAV)

对账恒等式(每日 16:20 分解,逐项落 json):

```
QC_equity − controller_ledger_close =
    Σ 滑点项      (QC 真实成交价 vs 本地 ledger 记账价; 逐单归因)
  + Σ 时点项      (下单时刻 vs 本地换仓记账时刻的市场移动; 盘前变更→开盘执行的隔夜跳空是最大项, 这是镜像的“真实执行成本”信息, 不是误差)
  + Σ 股息时点项  (QC 按支付日入现金 vs 本地各策略记账口径)
  + Σ 小数残差项  (D5, 仅当 QC 不支持小数股)
  + ε             (行情源差: QC NBBO vs Polygon; 预期 <2bp/gross)
```

- **阈值**: 稳态日(无换仓)|drift 增量| > 5bp/gross → 报警;换仓日各分解项
  必须能解释总差,无法归因的残差 > 3bp/gross → 报警(与 controller reconcile
  v4 的"时点同步后残差"哲学一致)。
- 15 分钟延迟**不进**此对账: 对账两侧都取各自的 16:00 后确定值(QC 收盘态 vs
  controller EOD 官方收盘补写后的账本值),延迟在收盘对齐后自然消失。
- 前端集成(M5): 面板 quality checks 加一项 `QC 镜像对账`,复用 reconcile
  verdict 语义(ok/breach/incomplete)。

---

## 7. 故障矩阵(每格都是明确行为,无静默)

| # | 故障 | 行为 |
|---|---|---|
| F1 | controller 停跳(target 停更) | QC 冻结最后目标 + QC 日志报警;本地 launchd/面板已双重报警 |
| F2 | tunnel/endpoint 不可达 | QC 端连续 N 次失败 → 冻结 + 报警;恢复后按版本号追平 |
| F3 | QC 算法崩溃/重部署 | ObjectStore 恢复 last_applied;目标态语义自愈 |
| F4 | 保证金拒单 | 不缩单不重试小单;整批标记 failed + 报警,人工裁决(镜像失真必须被看见) |
| F5 | 部分成交跨收盘 | 残量记 pending,次日开盘续追(目标态语义);对账把在途量单列 |
| F6 | 个股停牌/不可交易 | 单票挂起重试+报警,其余照常;对账单列该票 |
| F7 | target 文件损坏/schema 不符 | QC 端 schema 强校验,拒绝应用 + 报警(绝不部分应用) |
| F8 | QC 平台故障 | 恢复后目标态自愈;对账日志记录中断窗口 |

---

## 8. 目录与产物规划

```
trading_quantconnect/
  QUANTCONNECT_MIRROR_PLAN.md      ← 本文件
  exporter/                         M1: target 导出器(读 controller 输出)
  server_route/                     M1: /api/qc/target(挂进既有 express)
  lean/                             M2: QC 算法工程(本地 LEAN CLI 同步开发/回测)
  ops/                              M3: 部署/监控脚本、QC API 封装
  reconcile/                        M4: 每日对账 job + 报告 json
  tests/                            全程: pytest(tmp 沙箱 + 只读生产)
```

## 9. 里程碑与验收

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **M0 研究 spike**(先行,QC 账号到手后 0.5–1 天) | R1 小数股 paper 支持;R2 Download vs ObjectStore 推送与频控;R3 开盘单语义(Market@09:30 vs MOO);R4 Read API 拿持仓/成交的字段与延迟;R5 live 算法热更 target 不重部署的正确姿势;R6 零费率+滑点模型配置 | 每项有实测结论写进本文件附录 |
| M1 Target 平面 | exporter + endpoint + 强 schema | 单测: 半更新窗/化石槽/净额/改名票逐案;target 与 controller flatten 逐票一致 |
| M2 QC 算法(先回测环境) | 状态机 + diff 执行 + 幂等,LEAN 本地回测重放历史 target 序列 | 回测重放 8 月全月 target 变更,期末持仓与 golden 逐票一致 |
| M3 Paper 上线(小书) | 先只镜像 BDC+SSRS(低频、多头、无小数争议面小)| 稳态持仓 0 差连续 3 日;时序规则三分支(盘中/盘前/盘后)各实测一次 |
| M4 全书 + 对账平面 | pairs 空头 + AISS 上线;每日对账 job | 换仓日对账分解可解释;连续 5 日无未归因残差报警 |
| M5 前端集成 + 运维交接 | 面板 QC 对账项;运维台账进 memory | 与既有 quality checks 同视觉/同语义 |

## 10. 明确不做

- 不接真钱经纪商;不在 QC 写任何策略/信号逻辑;不改任何策略生产文件;
- 不为"简化"合并口径: 官方口径展示归展示,执行镜像只认账本口径;
- 不自建交易日历/改名映射的第二套实现(QC 日历 + 本地 ticker_aliases 各司其职)。

---
*作者注: 本方案刻意把全部"聪明"留在本地(已被生产验证的 controller),QC 端
只有一个哑执行器 + 状态机。镜像系统的价值在于它简单到不可能错,而所有会错的
地方都有对账在等着。*
