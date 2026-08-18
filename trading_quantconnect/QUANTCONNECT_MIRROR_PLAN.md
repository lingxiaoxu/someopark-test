# QuantConnect 模拟盘镜像 — 架构设计(PLAN,2026-08-16)

> 状态: **v2(2026-08-16 用户四条硬性规格后修订)——开发已开始**。
> 纪律: 不做 fallback 式简化;测试全进 /tmp,生产文件只读;
> **全部开发不出本文件夹**(`trading_quantconnect/` 是唯一代码与状态写入面)。

## v2 关键修订(用户规格,覆盖 v1 的两个架构决策)

1. **数据源改版 —— 直读五策略持仓文件**(v1 的"controller flatten 为源"作废):
   下单链路 = 只读五策略的实时持仓文件 → 本目录内 exporter 扁平化/过滤 →
   QC。不依赖、不修改 controller/express 任何代码(开发不出文件夹的硬约束)。
   v1 靠 controller 解决的问题改由本目录自己解决(见 §2.1v2),其中最关键的
   legacy 原子性在直读设计下**反而变简单**: pair 的存在性与腿数来自同一次
   json 读取,天然同快照。
2. **防火墙(用户逐字)**: 只能由持仓文件影响下单,**绝不能反向**。下单模块
   与外界的关系仅限于: ①五个持仓文件(只读)②API 凭证(.env 只读)。
   本目录代码零 import 仓库其他模块、零写入仓库其他路径。
3. **传输改版**: 弃 tunnel/express 路由(在文件夹外),改 **QC ObjectStore
   推送**(exporter 用 API 把 target 写入 org ObjectStore,算法端读同一
   对象)—— 只用 API key,完全落在防火墙允许面内。
4. **时序语义确认**: 8:00 变化→9:30 下单;10:00 变化(策略出仓晚)→10:00
   即时下单;盘后→次日开盘;节假日顺延(§3 状态机不变)。

---

## 0. 目标与不变量

用 QuantConnect(QC)云端 paper trading 做五策略组合的**实盘追踪镜像**:

- **镜像保真**: QC 模拟账户的持仓在稳态时与本地 golden 持仓文件**逐票逐股一致**
  (含空头负股数、BDC 小数股);差异只允许来自"在途订单"这一个瞬态
  (中流上线的 legacy 过渡期除外,该期偏差显式建模,见 §9)。
- **时序规则(用户规格,§3 形式化)**: 持仓文件变化发生在盘中(9:30–16:00 ET)
  → 立即市价下单同步;盘前 → 等 9:30 开盘下单;盘后 → 次日开盘下单;
  节假日 → 顺延到下一交易日。交易日历以 QC 交易所日历为唯一权威。
- **NAV 一致性**: QC 账户 equity 与本地 controller 实时 NAV(账本口径)之间的
  偏差,除已知的 15 分钟行情延迟外,预算内可分解、可对账、超限报警(§5)。
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
  cumulative_fees 恒为 0 → QC 必须配零费率模型才可比(§2.2/附录 A-1)。

inventory_bdc: holdings.{T}:{weight, shares(小数), drip_events, entry_date},
  cash:{ticker:"BIL", shares}; 三层强校验(ledger 重放==inventory)已内建。
```

### 1.3 已知怪癖(设计必须吸收,不是绕开)

1. **半更新窗**: DailySignal 先写 inventory、约 1 分钟后写 account —— 直接
   watch 原始文件会读到中间态。**解**: 不自己重解这个问题 —— 目标源直接用
   controller 的装配输出(§2.1),controller 的 `_maybe_rebuild` 守门 + 双引擎
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
│   时序状态机(§3): 盘中即时 / 非盘中挂起至下一开盘(QC 日历)       │
├─ Verification 平面(本地)──────────────────────────────────────┤
│ 每交易日 16:20 ET: QC Read API 拉 持仓/现金/equity/成交流水        │
│   ①持仓 vs target: 必须 0 差(在途单除外)                        │
│   ②equity vs controller 16:00 账本收盘: 分解对账(§5 预算)        │
│   ③成交 vs 本地 ledger 入场价: 滑点归因                          │
│   → trading_quantconnect/reconcile/qc_reconcile_{date}.json     │
│   超限报警进 daily 报告;后续里程碑接前端 quality checks          │
└────────────────────────────────────────────────────────────────┘
```

### 2.1 【v1,已被 v2 修订 1 作废——保留供审计】为什么目标源曾选 controller

> **v2 注**: 用户防火墙规格要求下单链路只依赖持仓文件+API key,且开发不出
> 本文件夹 → 改为直读。v1 顾虑的逐项 v2 对策:
> - 半更新窗(inventory→account 差 1 分钟): pairs/SSRS/BDC 只读 inventory
>   单文件,AISS 只读 account 单文件 —— **每策略单文件自洽,跨文件原子性
>   不再需要**(五本书相互独立);单文件写入瞬间的撕裂读用"双读稳定判定
>   (两次读间隔 300ms 哈希一致才采纳)+ JSON 解析失败重试"解决;
> - mtime≠变化: 规范化持仓内容哈希(自实现,±0 依赖);
> - 净额扁平化: 自实现(逐票求和,含跨策略同票);
> - 改名: 持仓文件里永远是现行代码(BK 型化石槽 direction=null 被过滤),
>   QC SID 自行跟踪 —— 不 import ticker_aliases(防火墙零依赖);
> - legacy 原子性红线: pair 存在性+腿数来自**同一次读取** → 构造上同快照。

controller 已经解决了本方案 80% 的难题,且**每天在生产被双引擎对拍验证**:

- 半更新窗守门、内容哈希变更检测、跨策略净额扁平化、ISIN 身份锚、
  装配失败保守沿用(fail-static)、7×24 watcher、心跳可观测。
- exporter 因此薄到只做: 读两个 json(只读)→ 映射 ISIN→ticker(security
  master `render`)→ 原子写 target 文件 + 版本号自增。
- **风险与解**: controller 挂 → target 停更。exporter 在 target 里带
  `controller_heartbeat_age`;QC 算法读到 age>10min 时**冻结在最后已知目标**
  并打 QC 端日志报警(fail-static + 大声,绝不猜)。本地已有 launchd 守护 +
  前端心跳报警兜底 controller 本身。

### 2.2 QC 账户与资金映射

- **口径**: 镜像**账本口径**(ledger basis)—— 持仓文件里的 shares 字面就是
  这个口径($1M/策略起账)。官方口径是展示层变换,不进执行。
- QC 初始资金 = go-live 当日 Σ5 策略 account equity(精确值,≈$5.07M),
  一次性设定,此后 QC 自演化。
- **保证金**: 全书 gross(现约 $6.3M: pairs gross 2.2M + 多头 4.1M)/net 5.07M
  ≈ 1.25×,Reg-T 2:1 之内;显式配 `SecurityMarginModel(2.0)` + 拒单即报警
  (§6-F4),不静默缩单。
- **费用/利息**: QC 配零费率 FeeModel(与本地 `cumulative_fees=0` 口径对齐);
  空头借券费/保证金利息 QC paper 不计,与本地一致 → 不引入口径差。

---

## 3. 时序规则(用户规格的形式化)

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
| 个股停牌 | QC 拒单/不成交 | 该票挂起重试 + 报警,**其余票照常**(不因单票阻塞全书)(§6-F6) |

开盘执行细节: 09:30:00 用市价单(用户规格"开盘下单取最新成交价")。开盘首分钟
价差较宽是**真实执行成本**,镜像的目的正是把它量出来 —— 不做"等 5 分钟"之类
的美化。备选 MOO(参与开盘竞价)列入 R3 研究项,由对账数据决定是否切换。

**目标态语义**(重要): QC 端执行的是"把账户调到 target 状态",不是重放本地
逐笔。这使任意错过/重启/乱序都自愈 —— 收敛到最新 target 即正确。

---

## 4. 关键工程决策清单

| # | 议题 | 决策 | 理由 |
|---|---|---|---|
| D1 | 一个算法 vs 五个算法 | **单算法净额镜像** + 订单 tag 归因 | 跨策略同票净额唯一正确;五账户会把 pairs 空头与 AISS 多头拆成两笔虚假对敲 |
| D2 | 目标传输 | **v2: ObjectStore 推送**(exporter 经 API 写 `mirror/target.json`,算法端 ObjectStore 读)| 防火墙合规(只用 API key);tunnel/express 在文件夹外,作废;Download 拉模式降为备选 |
| D3 | 幂等 | target 带单调 version;算法 ObjectStore 记 last_applied | 算法重启/重部署/网络抖动全自愈 |
| D4 | 订单类型 | MarketOrder(RTH)| 用户规格;paper 立即全额按 bid/ask 成交(附录 A-2: **无滑点模型无部分成交**),即所求"最新成交价" |
| D5 | **BDC 小数股** | 首选 QC 原生小数股下单(R1 验证);若 paper брokerage 限整数 → **显式残差账**: 整数股执行 + `fractional_residual.json` 逐票记差,残差市值>1 股价值时并入下次单 | 这是精确的会计设计,不是舍入了事 |
| D6 | 股息/DRIP | QC 收现金股息;BDC 的 DRIP 在本地 inventory 加股 → target 增股 → QC 用股息现金买入 —— **闭环自洽**,支付日差异进对账分解 | 不在 QC 复刻 DRIP 逻辑(单一真源纪律) |
| D7 | 拆股 | QC 自动调整持仓;本地 controller 标注 + 策略文件为 golden → 拆股日 target 与 QC 同步跳变,对账日做拆股感知比对 | |
| D8 | 数据订阅 | Minute 分辨率、Raw 归一化(与真实成交价对齐;Adjusted 会造成历史比价失真) | |
| D9 | 凭证 | `.env`: `QC_USER_ID`/`QC_API_TOKEN`(user 提供);`QC_TARGET_KEY`(endpoint 独立 bearer);永不入 git | |
| D10 | 首次建仓 | go-live 用同一状态机: 部署时拉 target 全量建仓(= 一次"大变更"),从空账户收敛 | 不写特殊初始化路径 |

---

## 5. NAV 一致性预算(与 controller 实时 NAV)

对账恒等式(每日 16:20 分解,逐项落 json):

```
QC_equity − controller_ledger_close =
    Σ 滑点项      (QC 真实成交价 vs 本地 ledger 记账价; 逐单归因)
  + Σ 时点项      (下单时刻 vs 本地换仓记账时刻的市场移动; 盘前变更→开盘执行的隔夜跳空是最大项, 这是镜像的“真实执行成本”信息, 不是误差)
  + Σ 股息时点项  (R7 确证两侧同用除息日口径; 残余=本地凌晨 DRIP→次日 QC 买入的 ~1 交易日再投滞后, 量级可忽略但照记)
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

## 6. 故障矩阵(每格都是明确行为,无静默)

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

## 7. 目录与产物规划

```
trading_quantconnect/                v2 实际布局(全部开发在此,不出文件夹)
  QUANTCONNECT_MIRROR_PLAN.md       ← 本文件
  M0_MONDAY_RUNBOOK.md              周一早测试手册
  qc_api.py                         QC API 客户端(认证/项目/编译/部署/ObjectStore/live 读)
  inventory_source.py               五持仓文件直读+双读稳定+扁平化+legacy 过滤+BDC 残差(纯函数核心)
  exporter.py                       变更检测→target 版本化→ObjectStore 推送;--golive/--once/--loop/--dry
  lean/main.py                      QC 镜像算法(状态机+diff+零费+现金初始化+幂等)
  lean/m0_probe.py                  M0 探针算法(R1 小数股/R6 零费/R2 ObjectStore 读)
  ops/deploy.py|status.py|stop.py   部署/状态/停止(经 API)
  state/                            exporter 状态(version/legacy/残差/target 副本;gitignore)
  reconcile/                        M4: 每日对账
  tests/                            pytest(tmp 沙箱 + 只读生产)
```

## 8. 里程碑与验收

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **M0 研究 spike**(先行,QC 账号到手后 0.5–1 天) | R1 小数股 paper 支持;R2 Download vs ObjectStore 推送与频控;R3 开盘单语义(Market09:30 vs MOO);R4 Read API 拿持仓/成交的字段与延迟;R5 live 算法热更 target 不重部署的正确姿势;R6 零费率+滑点模型配置 | 每项有实测结论写进本文件附录 |
| M1 Target 平面 | exporter + endpoint + 强 schema | 单测: 半更新窗/化石槽/净额/改名票逐案;target 与 controller flatten 逐票一致 |
| M2 QC 算法(纯逻辑先行) | 状态机 + diff 执行 + 幂等;**不做任何回测**(策略回测全在本地,QC 只是执行器)—— diff/幂等/legacy 减法抽成纯函数,本地 pytest 钉死;QC 框架行为(开盘调度/日历)由 M3 paper 灰度直接验收(paper 即零成本沙箱,错了重置重来) | 纯函数单测全过: 历史 target 序列(8 月真实变更)喂 diff 引擎,期末状态与 golden 逐票一致;幂等/乱序/重启用例全绿 |
| M3 Paper 上线(小书) | 先只镜像 BDC+SSRS(低频、多头、争议面小)| 稳态持仓 0 差连续 3 日;**盘前/盘后**两分支各实测一次(复审修正: 盘中变更在月频小书阶段自然不可达,不设做不到的验收)|
| M4 全书 + 对账平面 | pairs 空头 + AISS 上线;每日对账 job | 换仓日对账分解可解释;连续 5 日无未归因残差报警;**盘中分支**随首次真实 CLOSE_STOP/盘中去风险实测(pairs 上线后自然发生)|
| M5 前端集成 + 运维交接 | 面板 QC 对账项;运维台账进 memory | 与既有 quality checks 同视觉/同语义 |

## 8.5 开发完成态与剩余清单(2026-08-17 深检后)

**已完成(代码+测试,19/19)**: exporter 全链(直读/防火墙/legacy/缩放建仓
常数/版本化/ObjectStore 推送)、QC 算法(状态机/收敛式 apply/零费/C0 现金
初始化)、mirror_logic 纯函数(M2 diff 单测兑现)、M0 探针、ops 三件、runbook。
深检修复: 旧 _apply 提交即标版本 → 新订阅票无行情被拒后**永久丢腿**
(go-live 必翻车);改收敛循环(blocked 跳过重试,全清零才标版本)。

**剩余(按先后)**:
1. **周一 M0 现场联调**(唯一无法本地验证的面): live/create payload 实配、
   R2 算法端 ObjectStore 读、R1 小数股、R6 零费生效 —— runbook T-2;
2. golive + 首建仓收敛观察(T-1/T-3);
3. exporter 生产化: launchd 常驻(现为前台 --loop,周一先人工值守);
4. **M4 对账平面**(未建): 每日自动 qc_reconcile job(持仓 0 差/€分解/
   legacy 项/官方引擎差)+ transition 结束日的 K 再基准工具;
5. M4 附属: BDC 残差 >1 股并入下单(接口已留);
6. **M5 面板集成**: quality checks 加 QC 镜像项 + 头条旁 QC equity 展示;
7. 运维交接: 台账进 memory,openclaw 排程。

**"完全可用"判定**: 1-3 完成即可**日常镜像运行**(人工 status 对账);
4-6 完成才算 plan 全量交付(自动对账+面板可见)。预估: 1-3=周一当天;
4=1-2 天;5-6=1 天。

## 9. 冷启动 / 中流上线(Bootstrap,2026-08-16 用户规格)

**问题**: 信号已运行数月,inventory 里已有大量既有仓位(含空头)。QC 从零起步,
不能对"从未在 QC 建立过的仓位"执行平仓。

**用户原则(逐字)**: 既有仓的关闭一律不交易(无仓可平);只有**新建仓**才开始
交易;既有仓全部自然关闭后,进入完全镜像态。分策略:
- **AISS / SSRS / BDC**(月初建仓/被动,仓位长寿): **立即全量建仓**——现在就
  按当前持仓在 QC 建立(否则要等下月初,且这三本书的仓位本来就该在);
- **MRPT / MTFS**(pairs,日频开平): **有机上线**(organic onboarding)——
  QC 从空书开始,只镜像 go-live 之后新开的对;既有对的 CLOSE 在 QC 端为
  无操作;既有对逐个自然死亡后,pairs 书收敛到完全镜像。

### 9.1 实现: Legacy 过滤在 exporter(QC 端零感知)

保持"QC=哑目标态执行器"不变 —— bootstrap 全部在 Target 平面表达:

```
go-live 时刻(一次性):
  legacy_positions.json = 冻结快照
    { "mrpt": [{"pair":"ACGL/HIG","open_date":"2026-08-14"}, ...],
      "mtfs": [ 9 个既有对 ... ] }        ← 只减不增,永不回填

每次导出:
  target = controller 全书 flatten
         − Σ(仍存活的 legacy 对的腿贡献)     ← risk_matrix 的 leaf→(pair_node,eff)
                                              反向索引精确给出每对的腿级贡献
  legacy 存活判定: **必须与 flatten 同源** —— 以 controller 当前
    structure snapshot 里"pair 节点存在且 open_date attr 与冻结值相同"为准
    (对名相同但 open_date 变了 = 同名新实例 → 按新仓镜像,legacy 项作废)。
```

⚠️ **原子性红线(复审 2026-08-16 抓出的漏洞)**: 存活判定**禁止**直接读
inventory —— 若从 inventory 读(半更新窗/写入瞬间),可能出现"减项已判死、
flatten 里 pair 还在"的错位,target 会突然包含 legacy 腿 → **QC 误开既有仓**,
恰好违反用户第一原则。flatten 与存活判定取自**同一个** controller snapshot
(同一 structure_hash),"双边同时消失"由快照原子性保证,错位在构造上不可能。
**v2 化简**: 直读 inventory 后,pair 的存在性/open_date/腿数来自同一次
`json.load` —— 同快照由构造保证,无需 controller 改动(v1 前置条件作废)。

**这个减法自动给出全部正确行为,无需特殊分支**:
- legacy 对被策略平仓 → 它同时从 flatten 与减项中消失 → target 无变化
  → QC 无操作 ✓(正是"无仓可平不交易");
- 新对开仓 → 不在 legacy 集 → 进 target → QC 开仓 ✓;
- 新对与某 legacy 对**同票**(如 legacy 空 PFE、新对多 PFE)→ 减法后的净额
  只含新对贡献 → QC 只执行新对的腿 ✓(逐票净额仍然唯一正确);
- pairs 纪律"HOLD 不改股数、只有整对 CLOSE"保证 legacy 对不存在部分变形;
  若未来出现"同对调仓",其形态必是 CLOSE+重开 → open_date 变 → 自动按新仓。

### 9.2 资金与 NAV 对账的过渡期语义

- **QC 初始资金 C0 = go-live 日 Σ 五策略 account equity**(全书资本,不因
  pairs 空书而少配 —— pairs 本近自融资: 实测 account_mrpt cash 1,043,388 vs
  equity 1,040,952,净持仓市值仅 −$2.4k;既有对未镜像 ≈ QC 多持等额现金)。
- 过渡期恒等式(Verification 平面新增项):
  `QC_equity ≈ controller_ledger_NAV − Σ legacy 对的 (value_t − value_golive)`
  即减去"既有对自 go-live 起的未镜像 P&L"(controller pair 节点逐对值现成,
  逐日精确可算,不是估计)。该项随 legacy 对逐个死亡而封闭,全部死亡后恒为
  常数(计入历史)→ 进入完全镜像态,对账回到 §5 原式。
- 对账报告增加 `bootstrap: {transition: true, legacy_remaining: n, ages: [...]}`;
  transition 结束(n=0)当日显式记录里程碑。

**缩放镜像定案(2026-08-17 用户拍板,取代下文"账本口径对比"的部分表述)**:
用户意图=QC 按**官方口径资本规模**建仓("AISS 就该是 ~$3M 的书")。事实前提
先澄清: inventory 股数字面对应**账本**资本(AISS 引擎日志: ledger sizing
$1M→$1.13M),面板 $3.0M 是官方绩效锚非持仓市值。实现:
- go-live 冻结每策略常数 `scalar_s = 官方equity_s/账本equity_s`
  (实测 2026-08-17: mrpt 0.592 / mtfs 0.381 / aiss 2.682 / ssrs 0.995 /
  bdc 1.000),target = inventory 股数 × scalar_s(legacy 腿同缩放);
- C0 = Σ 官方 equity(实测 $6,000,322);缩放产生全策略小数 → 残差账全票化
  (逐票 |残差|<0.5 股,预算 ~数百美元级,对账单列);
- **零 ratio 依赖(2026-08-17 用户再拍板)**: 上述常数只是 go-live 那一刻
  "按 $6M 持股细节建仓"的**一次性构造算术**(KLAC 4500 股这类具体数字),
  构造完成后系统里**不存在任何比率追踪**: 官方 perf json 永不再读、
  scalar 永不重算、**永不再基准**。此后 QC 是一个从 $6M 起步的**独立自复利
  账户**: 未来每笔换仓沿用同一构造常数,恰使 QC 各子账与该策略持仓保持
  严格等比 —— 账本内部永远自洽(不会"乱");若未来不缩放新单,反而会把
  $6M 的存量书和 $1M 尺寸的新单混在一起,权重立刻失真(那才是乱)。
- QC vs 面板头条的偏差(官方引擎的费用/分红/复利处理 vs 纯持仓收益)由
  对账**只报告、不纠偏** —— 两边都是真实复利世界,信息不是误差。
- 防火墙注: 官方 perf json 仅 go-live 构造时读一次;常驻循环只读五持仓文件。
- 收敛结论: legacy 清零 + K 校准后,QC equity ≈ 面板头条,
  此后各自复利,偏差项(执行成本/残差/官方引擎差)对账逐日可见。

**过渡结束后的收敛语义(2026-08-16 用户预期,两点精确化)**:

1. **水平差不会自动归零 —— 计划做一次性再基准**。legacy 全死后,两边**日度
   变化**从此 1:1,但**水平**差一个常数 K = legacy 对在过渡期内产生的未镜像
   P&L(已封闭进历史)。用户预期"legacy 清零后两边一样" → transition 结束日
   执行**一次性 CashBook 校准**(deposit/withdraw 金额=K,来源=对账报告的
   legacy 项终值,逐笔留痕、显式操作): 此后 QC equity 与本地 NAV **水平也
   对齐**,残余漂移只剩执行成本(价差+时点,对账持续量化)与 BDC 小数残差
   (~$150)。
2. **对比基准(缩放镜像定案后更新)**: QC 从 $6M 官方口径规模建仓
   → 对账与 M5 面板集成**直接对齐面板头条**(官方口径 Σ)。账本口径值
   降级为对账分解的中间参考(QC 各子账 ≈ 账本子账 × 建仓换算常数,
   该恒等式本身就是持仓级对账的强校验)。
- AISS/SSRS/BDC 立即建仓的成本基差(QC 按 go-live 市价、本地是历史成本)
  **不影响 NAV 追踪**: 股数相同 → 此后 ΔNAV 逐日 1:1;建仓本身只付一次
  spread(进对账价差项,一次性,预计全书 <2bp)。

### 9.3 过渡期运营

- 观测: 每日对账列出存活 legacy 对与账龄;pairs 典型持有期数日~数周,预计
  过渡 2–6 周自然完成。
- **人工收养(可选,显式操作非自动)**: 若个别 legacy 对长期不死且用户想提前
  进入完全镜像,提供 operator 命令把指定对按市价在 QC 建立(等同把它移出
  legacy 集)。默认不启用 —— 遵循用户"等自然关闭"原则。
- go-live 建仓执行遵循 §3 同一状态机(盘中立即、否则下一开盘),不写特殊
  初始化路径(D10 不变,只是首个 target 已含 legacy 减法)。

### 9.4 面板 ↔ QC 逐格对应(2026-08-17 截图级核对;源码+算术双证)

**面板子层显示口径(前端源码 RealtimeNavViewer L445 实证)**:
`scale = 官方live值/账本值`;股数列显示**账本股数**原样,美元列显示
`账本市值 × scale`(子层等比换算)。因此:

- **面板美元数 = QC 要持有的市值;面板股数 × 建仓换算常数 = QC 实际股数**。
  例(2026-08-17 截图核对): 面板 KLAC +1,678 / $914,882 → QC 持 4,500 股
  (1678×2.68155),市值=面板值 ±1 股残差(≤$203);
- 五卡 + 各策略现金合计 = 面板头条 $6,000,322 = C0(dry-run 逐分闭环)。

**现金 = 自动余额,无需任何分仓机制**(数学恒等,同价下精确):
`QC_cash = C0 − Σ缩放净持仓市值 = Σ(账本现金_s × 常数_s)`。
实测(2026-08-17): 缩放净持仓 $5,237,439;现金 = MRPT $618,129 +
MTFS $127,161 + SSRS $8,423 + AISS $9,170 + BDC $0 = **$762,884**。
pairs 空头卖出所得自动入同一现金池;gross/equity ≈ **1.0×**,距
Reg-T 2× 上限余量大;逐策略虚拟现金仅在对账层按订单 tag 归因拆出。

**BDC 的"另一半"= BIL**(SPDR 1–3 月国库券 ETF,50/50 配置的 cash sleeve,
2025-11-11 起 DRIP 9 次): 面板 BIL +5,173.083 / $471,254 ≈ BDC 五票合计
$476,848 的对半。对 QC 就是普通 ETF(target 含 5,173 股);月付分红走
R7 已证的 PaperBrokerage 派息 → 本地 DRIP 加股 → target 增 → QC 买回,
闭环无死角。

## 10. 明确不做

- 不接真钱经纪商;不在 QC 写任何策略/信号逻辑;不改任何策略生产文件;
- 不为"简化"合并口径: 官方口径展示归展示,执行镜像只认账本口径;
- 不自建交易日历/改名映射的第二套实现(QC 日历 + 本地 ticker_aliases 各司其职)。

---

## 附录 A. QC Paper Trading 官方文档核对(2026-08-16,用户提供链接)

来源: docs/v2/cloud-platform/live-trading/brokerages/quantconnect-paper-trading

**已确认支持(计划成立):**
- US Equities ✓(全书皆美股/ETF);Market / **Market-on-Open** / Limit / Stop
  等全序 ✓ —— **R3 已答**: MOO 原生存在,开盘执行可在 Market09:30 与 MOO
  之间由 M3 实测数据选择;
- 现金/保证金账户 + 买力与 margin call 建模 ✓(D 保证金设计成立);
- **raw 归一化下拆股自动调整持仓与在途单的数量/限价/触发价** ✓(D7/D8 的
  raw 选择被文档直接背书);
- CashBook 存取款 API ✓ → 初始资金设定(§2.2/§9.2 C0)有正规通道;
- 订单可更新、TIF(Day/GTC/GTD)✓;paper 用真实 live 数据源。

**按文档修正的三处设计:**

1. **费用(修正 §2.2/§5)**: Paper 默认**非零费率**(股票 $0.005/股、最低 $1)。
   方案: 算法内显式 `SetFeeModel(ConstantFeeModel(0))` 覆盖为零费(LEAN
   security 级 fee model 可覆盖,M0 实测确认在 paper 生效);若覆盖不生效,
   则对账分解新增"费用项"(逐单精确可算,非估计)。二选一都不引入未归因残差。
2. **成交语义(修正 §5 叙述)**: 文档明示 paper "市价单立即全额成交,按
   bid/ask spread 定价,**无滑点模型、无部分成交**"。因此:
   - 对账分解里的"滑点项"精确化为"**价差项**(过 spread 成本)+ 时点项",
     不含冲击 —— 镜像量到的是 spread+timing 成本,冲击成本另由 VP econ 层
     (λ 标定)覆盖,两者互补不重复;
   - §6-F5(部分成交跨收盘)降级为防御性路径(默认全成交),保留不删。
3. **BDC 小数股(修正 D5 主次)**: 文档未提小数股;LEAN 股票 LotSize=1 的
   默认校验大概率拒绝小数单 → **残差账升为主方案**(整数股执行 +
   `fractional_residual.json` 逐票记差,残差市值>1 股并入下次单),QC 原生
   小数降为 R1 验证的 upside。BDC 六票残差上界 = 6 股市值 ≈ $150,对
   $950k sleeve 为 1.6bp,在 §5 预算内单列。

**新增研究项:**
- **R7 股息入账 — 已答(2026-08-16,源码级确证): paper 派息 ✓**。证据链:
  1. corporate-actions 文档: raw+live 下"派息金额自动入 cashbook"(表面陈述);
  2. LEAN `SecurityPortfolioManager.ApplyDividend`: **live 模式提前 return**
     ("不精确建模 payable date,live 依赖券商现金同步")—— 核心引擎不记息;
  3. **`PaperBrokerage.Scan()` 自己派息**(决定性):
     `distribution = Holdings.Quantity × dividend.Distribution;`
     `security.QuoteCurrency.AddAmount(distribution)`,每时间循环去重防双记。
  推论与设计影响:
  - BDC DRIP 现金腿闭环**成立**,月度 CashBook 校准从方案降级为对账核对项;
  - 入账时点 = live 公司行动数据到达时(6–7AM ET,**除息日**口径)。本地
    bdc_inventory 的 DRIP 同样按除息日收盘价再投 —— **两侧同用 ex-date 口径**,
    仅剩"本地凌晨处理→次日 target 增股→QC 次日开盘买入"的 ~1 交易日再投滞后,
    计入 §5 时点项(量级: 单次分红额×1 日 BDC 波动,≈$50×数千分之一,可忽略
    但照记);
  - M0 仍保留一次实测(临近除息的持仓票)做上线前的行为验收,非存疑复查。
- **R8 节点资源**: live 部署需一个可用 live trading node —— M0 确认账号
  配额,不够则明确升级成本再动手。

---

## 附录 A-3. M0 实测结论 + Go-Live 实录(2026-08-17)

### M0 三项实测结论(周一开盘后探针,全部落定)
| 项 | 结论 | 证据 |
|---|---|---|
| R1 小数股 | **拒绝**:`quantity (0.5) less than lot size (1)` → 残差账为唯一方案(全票整数化 + `fractional_residual.json`,\|残差\|<0.5 股/票) | m0_probe 订单回执 |
| R2 ObjectStore 传输 | **通**:API `object/set`(multipart)推送 → 算法端 `ObjectStore.Read` 同 key 读到 → 传输面成立 | 探针读回一致 |
| R6 零费率 | **通**:`SetFeeModel(ConstantFeeModel(0))` 后成交 fee=0.0,与本地零费口径对齐 | 订单事件 fee 字段 |
| (附)object/get API | 平台侧下载 export 为 Institutional-only;**算法端读取不受影响**,仅影响外部审计路径(M4 用 live/portfolio 代替) | API 报错原文 |
| (附)SID 命名 | QC holdings 键用**历史首名**(OBDC 显示为 "ORCC …"),同我方 ISIN 锚定;M4 对账需 SID→现名映射 | portfolio 读回 |

### Go-Live 实录(2026-08-17 周一)
- 08:53 ET `golive`:冻结 legacy(mrpt 1 对 + mtfs 9 对)+ 构造常数
  scalars(mrpt .5924 / mtfs .3814 / aiss 2.6816 / ssrs .9950 / bdc 1.0),
  C0 = **$6,000,322.19**(五策略官方口径和,永不重读)。
- 09:3x 部署 mirror(main.py + mirror_logic.py),v1 target 21 票。
- 09:36 首轮:21 票全 blocked(新订阅同 tick 无行情)—— **收敛循环设计被
  生产验证**(部署前深检修的 bug:旧版会在此刻标记已应用并永久丢腿)。
- 09:37 20 票成交;TSLX 连续 blocked 两轮;09:39 TSLX 成交;
  09:40 `applied v1 CONVERGED`。全程零人工。
- 现金自动残差:$945,378(= C0 − 成交成本;成交价低于 8/14 估值,省 ~$182k)。
- EOD 核对(16:1x ET):持仓 21/21 逐票精确一致;equity $5,986,312;
  面板头条估算 $6,039,033,差 $52.7k = legacy 对折算市值 $232.5k −
  建仓成本节省 $182.5k + 楔子 ~$2.7k —— **§9.4 过渡期语义闭环验证通过**。
- 当日代码修正:qc_api `live_logs` 需 deployId→algorithmId 参数(endLine≤250);
  stop.py 容忍 liquidate 自停后的 "No running deployment"。
- 运维化:exporter 落地 launchd 常驻 `com.someopark.qcmirror.exporter`
  (KeepAlive,日志 `logs/exporter_launchd.log`;坑:wrapper 无 +x → EX_CONFIG 78)。
- 剩余:M4 日对账(SID 映射/legacy P&L 项/官方引擎楔子/过渡期末 K-rebase 工具
  + BDC 残差>1 股并单)、M5 面板集成。

### 8/18 事故与两项平台事实(生产实证 + 已修)
**事故**:首次运行中更新(v2,VLO/YUM 新对)推送成功但算法 40 分钟不可见。
**根因(平台事实 #1)**:LEAN ObjectStore 对已读 key 有算法侧缓存,外部 API
推送的更新对运行中算法不可见(M0-R2 只验证了"推送→首次读",未覆盖
"运行中更新→重读")。**修复**:`_read_target` 先 `ObjectStore.Clear()`
(LEAN 源码注释确认 Clear 只清状态缓存、专为多节点共享 key 场景;每分钟
重拉 ~1.6KB)。**活体验证**:修复部署后 `export_once(force=True)` 强推
同内容 v3,运行中算法 1 分钟内看到并 CONVERGED —— 根修证实,今后任何
inventory 变更无需重部署。
**平台事实 #2**:**重部署会重置 paper 账户**(现金=上一部署清算 equity,
持仓清零)。两个后果:①重部署 = 全书按当日市价重建(成本价重置,equity
连续但有分钟级重建滑差);②**危险态**:若重部署时 applied_version 已等于
最新 target 版本,算法认为无事可做 → 空账户卡死(8/18 10:01 部署实发生,
由 v3 强推救回)。**运维规约**:尽量不重部署;任何重部署后必须
`export_once(force=True)` 强推一版触发重建。代码级 drift 自愈守卫
(version 相同但持仓偏离 → 重进收敛环)列入 M4 一并上线,避免为它单独
再触发一次重置。

---
*作者注: 本方案刻意把全部"聪明"留在本地(已被生产验证的 controller),QC 端
只有一个哑执行器 + 状态机。镜像系统的价值在于它简单到不可能错,而所有会错的
地方都有对账在等着。*
