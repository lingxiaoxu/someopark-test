# Controller 中央估值引擎 — 完整开发计划

> 2026-08-12。依据:用户 PDF《组合定价 take-home 笔记》+ demo(拍平/反向索引扇出
> 可视化)+ `controller/PORTFOLIO_DATA_MAP.md`(五策略三层持仓数据,已实测复核)。
> **本文件只是计划,未写任何实现代码;实现按本计划逐阶段进行,每阶段可独立验收。**

---

## 〇、目标与最高纪律

做一个**中央估值 controller**:把整本书(PORTFOLIO → 5 策略 → 中间层 → 股票)
建成组合层级,用 **PDF 里的两种方法(拍平+反向索引扇出 / 保留树+变化上推)各实现
一遍**,以可配频率(1/5/15/60 分钟)用 Polygon 价格增量估值,结构变化即时重同步,
并在日终与三个 performance json 对账。

纪律(用户令,逐条落进验收):
1. **两种方法都做**,同一输入必须**逐字节同输出**(PDF 的核心测试纪律:
   `both()` 跑两引擎 assert 相等)。不做单引擎"够用了"的简化。
2. **价格只用 Polygon**(唯一数据源;不引入 yfinance/其它,失败重试而非换源)。
3. **只读持仓数据**:从 PORTFOLIO_DATA_MAP 的三层文件取结构与 shares,
   **绝不修改任何生成持仓文件的功能**。
4. **不动三个 perf json 的生成脚本与拼接日期逻辑**(UpdateMasterPerformance 的
   backtest+live 拼接、SR_LIVE_START/AISS_LIVE_START、BDC 惯例)——controller
   与它们**对账**,不替代、不重写。
5. 不 fallback 不敷衍:遇到结构/数据边角(空持仓、空腿、篮子份额)要正面建模。
6. **代码边界(用户令 2026-08-12)**:controller 的全部代码、reference data、
   输出、测试**只放 `controller/` 目录下,绝不超出**——不在 repo 根或其它目录
   放任何 controller 文件;不 import 其它策略目录的模块(qlib 等,连只读 import
   都不做——跨 conda env 本也不可行);对外只有两种交互:**只读**其它目录的
   数据文件(持仓/三 json)、调用 Polygon API。唯一例外:测试临时输出进 /tmp
   (repo 测试纪律)。

---

## 一、两种方法的精确定义(从 PDF/demo 提炼,作为实现规范)

两法共享同一读入(结构 dict:名字 → [(持有的东西, shares)]),同一条不变量:
**组合值是价格的线性函数** `V_p = Σ w_ps·price_s`,一条价格来只动它触及的部分。

### 方法 1 — 拍平 + 反向索引 + 扇出(flatten)
- **预处理**:DFS 后序把每个组合展开成**底层股票有效暴露**
  `portfolio_to_stocks[P] = {stock: effective_shares}`;
  菱形(同一股票经多路径到达)**预先合并**;多空**预先抵消**,净 0 的名字删除
  (`no_zeros`);递归中 `being_worked_on` 集合检测循环并大声报错;
  DFS 完成顺序即**拓扑序**(子先于父),给每个组合一个 `print_position`。
- **反向索引**:`stock_to_portfolios[s] = [(print_position, P, effective_shares)]`,
  预排序(父永远排在子后)。
- **运行时**(每条价格):`Δ = new - last`(首次 Δ=new);查反向索引一步扇出:
  `value[P] += eff_shares × Δ`;首见该股票时 `seen[P] += 1`;
  `seen == required`(该组合全部底层股票都见过价)才可发;发出按预排序;
  **值与上次发出相同则不发**(`value_last_printed`)。
- **特性**(PDF 原文归纳):每次更新便宜(一跳、无爬树);多空自动抵消——被
  完全对冲的名字在拍平里不存在,动它不触发重估;反向清单**就是风险矩阵**
  (每组合对每股票的 delta,可 `value = M·prices` 矩阵化);代价是内存与
  **看不到中间层的值**。

### 方法 2 — 保留树 + 变化上推(tree)
- **预处理**:`held_directly_by[x] = [(直接父, shares)]`(反向父边);
  DFS visit 得拓扑序 + `print_position`;`still_waiting_for[P] = 直接持有数`。
- **运行时**:价格 Δ 先更新直接父;用 **heap(按 print_position,先深后浅)**
  把"有变化要上推"的组合排队;弹出时若 `still_waiting_for != 0` 跳过(gating);
  **同一组合可从多条路径到达(diamond)——变化两边都累加,但每轮只发一次**
  (`printed_this_time`);发出后把 `change_to_pass_up` 推给它的父;值没变不发。
- **特性**:内存小;**每一层都有值**(look-through 报告、sleeve 归因要的正是这个);
  结构常变时好维护(改一个持仓只动该点与其父,不用重开拍平);
  代价是每 tick 干活多(走部分树 + heap log)、diamond 要小心。

### 选择矩阵(PDF)→ 恰好映射我们的五策略
| PDF 场景 | 我们的对应 |
|---|---|
| 价格快、仓几乎不动、要顶层、多空抵消 → **拍平** | BDC(结构几乎不变)、盘中高频净值 |
| 仓常变、要每一层的值、报告向 → **树** | MRPT/MTFS(pairs 常变甚至清空)、AISS 子板块归因、日终报告 |
| 两个都大 → 组合标记/批量/矩阵 | 我们规模远未到,不做过度设计,但拍平引擎保留矩阵化出口 |

**我们两个都做、并跑对拍**——不是二选一:拍平做高频主引擎+风险矩阵,树做
look-through 层值+结构频变容忍,且互为正确性证明(PDF 的 `both()` 纪律)。

---

## 二、层级模型(把五策略统一进一个组合代数)

### 2.1 层级(用户定义,顶层→叶)
```
PORTFOLIO                              ← 整本书
├─ MRPT      ─ pair("DG/MOS") ─ 股票   ← 两腿,s2_shares 为负(空腿)
├─ MTFS      ─ pair("DGX/NKE") ─ 股票
├─ AISS      ─ subsector("memory_hbm") ─ 股票
├─ SSRS      ─ 股票(ETF: XLB…)
└─ BDC       ─ 股票(GBDC… + BIL)
```

### 2.2 节点模型
```
Node = { name, kind: portfolio|strategy|pair|subsector|stock,
         children: [(child_name, shares)],      # 组合代数,shares 可为负/小数
         attrs: {...} }                          # 中间层自带属性,估值不消费但保留
```
- **中间层 attrs 必须保留**(用户令):pair 的 `direction/open_date/days_held/
  param_set/hedge_ratio…`;subsector 的 `weight/days_held/action_today…`;
  BDC holding 的 `weight/cik/drip_events`。controller 输出的每层值要能带出这些
  属性(报告/风控直接用),但估值内核只吃 `(child, shares)`。
- **株数语义**:一律"绝对股数"(不是权重)。`V = Σ shares×price` 直接成立,
  与 demo/PDF 完全同代数。策略→中间层的 shares 恒为 1(pair/subsector 是容器);
  中间层→股票才是真实股数。
- **现金腿**:AISS/SSRS 的 `cash`、BDC 的 BIL——BIL 是可定价股票(Polygon 有价);
  纯现金(AISS/SSRS 的 cash 美元数)建模为 attrs 上的常数项
  `Node.attrs.cash_const`,估值时 `V += cash_const`(两引擎同样处理,
  不伪造"CASH 股票价格=1"之类的 hack——正面建模)。
  **cash 的引擎语义(review 修 A,精确定义)**:
  - 拍平:cash 像 shares 一样沿持有链拍平——`cash_flat[P] = own_cash +
    Σ q_c × cash_flat[c]`(我们场景容器 q 恒为 1,引擎按一般式实现);
    `value[P]` 以 `cash_flat[P]` **初始化**(不是 0),价格增量照常累加;
  - 树:`running_total[P]` 以自身 `own_cash` 初始化,子组合值经上推自然携带;
  - cash 不进 `seen/required`(它不是价格输入);
  - **cash 的时变语义**:`cash_const` 在 **tick 之间是常数**(五策略都是日频
    调仓,盘中 cash 不动),在**每次结构重建时从 account/inventory 文件重读刷新**
    ——"const" 指 tick 间不变,不指永恒;cash 可为负(账本 `liabilities`
    存在时),两引擎按带符号常数处理,无特殊分支。
  **空节点/纯现金节点的初始发出(review 修 A,致命项)**:PDF 语义下组合只在
  "持有的股票来价"时被触及——`required==0` 的组合(空仓 MRPT)在两引擎里都
  **永远不会被价格触及**:拍平版它不在任何反向索引;树版它永不首发,导致父节点
  `still_waiting_for` 永不清零 → **整本书永久 gating**。因此规定:**结构构建/重建
  完成后立即执行一轮"初始化发出"**——所有 `required==0` 节点视为已定价,
  值=cash_flat/own_cash,按拓扑序发出并(树)向父传播 first-price 递减 waiting。
  这是对 PDF 的必要扩展,两引擎同样实现,测试 15 直接断言:空 MRPT 有值、
  PORTFOLIO 不被它 gating。

### 2.3 每策略的结构装配(数据源=PORTFOLIO_DATA_MAP,只读)
| 策略 | 结构来源 | 装配规则 |
|---|---|---|
| MRPT/MTFS | 结构=`inventory_{mrpt,mtfs}.json`;**cash=`account_{mrpt,mtfs}.json`.cash**(2026-08-12 调查定案) | 只取 `direction != null` 的 pair;`pair → [(s1, s1_shares), (s2, s2_shares)]`(s2 为负);**空仓语义已定案(用户+实测)**:0 对持仓 → 策略值 = cash_const(MRPT 实测 equity=cash=1,042,112 ✓);装配自检:`cash + Σ两腿市值 ≈ account.equity`(容差内),`inventory` 开仓对与 `account.lots` 互验;⚠️ `inventory.capital=500k` 是回测 scaling 分母**不是现金**,绝不当现金用 |
| AISS | `inventory_aiss.json`(subsector 层)+ `account_aiss.json`(股票层)+ daily report `stock_holdings`(by_ticker 聚合含 `subsectors` 列表) | **ARM 双板块=经典 diamond,不消歧(用户令,2026-08-12 定案)**:同一股票在两个 subsector branch 各自持有各自的 shares——拍平引擎**预合并**(有效暴露相加,demo 的 JPM 300=200+100)、树引擎**两路各自上推、每轮只发一次**(printed_this_time),照 PDF 语义原样实现。**per-branch 股数直读 daily report 的 `stock_breakdown`**(2026-08-12 代码核查修正:此前误以为分解不落盘——实际 report 顶层就有 `stock_breakdown`,每 (subsector,ticker) 一行含 tier_role/within_weight/portfolio_weight/target_shares,ARM 双持时天然两行)。装配:branch shares = breakdown 行股数,按 account.positions 实际总数**归一校验**(差=取整,超容差报警);重构公式 `total×w_s·within/Σ` 降级为**交叉校验**(within 取自直读 config.yaml 的 subsectors+tier_weights——yaml 是数据文件,不 import qlib 模块,纪律 6 合规)。`stock_holdings.subsectors` 列表为 diamond 证据。cash=account.cash 常数项 |
| SSRS | `account_ssrs.json` positions | 策略→ETF 直连;cash 常数项 |
| BDC | `inventory_bdc.json` | 策略→5 BDC+BIL,shares 直接读(4 位小数);结构几乎不变 |
| PORTFOLIO | 固定 | → 5 个策略,shares=1 |

- **注意菱形是真实存在的**:同一股票可同时出现在 MTFS 两个不同 pair、或某股票
  同时在 AISS 与 pairs 持有(如 KLAC 出现在 AISS 和某 pair)——PDF 的 diamond
  语义(拍平预合并/树两路都加、只发一次)不是理论装饰,是必须正确的路径。
- pairs 空腿=负 shares:拍平的多空抵消与"全对冲名字不触发重估"直接适用
  (PDF pair-trading 实例节就是作者本人这么用的)。

### 2.4 结构变化即时同步(watcher)
- **watcher 独立于行情 tick、7×24 持续运行**(用户点明的关键需求 2026-08-12:
  持仓变化必须"马上变",而五策略持仓文件的更新时刻**全部在盘后**且各不相同——
  AISS ~17:45 / SSRS ~17:45 / BDC 凌晨早段 / MRPT+MTFS 晚至次日上午。若 watcher
  绑在盘中 tick 循环上,盘后即失明,次日开盘才一次性发现=违背需求)。实现:
  独立 5s 级轻轮询线程/循环,交易时段与否都在跑;每个行情 tick 前再查一次兜底。
  检测对象=`mtime + 内容摘要`:`inventory_{mrpt,mtfs,bdc}.json`、**`account_{mrpt,mtfs}.json`**
  (pairs cash 的 golden 源——2026-08-12 cash 复查补入,凌晨平仓回笼/股息入账
  改变 cash 必须触发重建)、`inventory_aiss.json`、`account_{aiss,ssrs}.json`、
  `inventory_sector_rotation.json`。**规则:任何进入装配的文件必须同时进
  watcher 列表**(装配读什么、watcher 就盯什么,二者由同一份清单常量驱动,
  防止再次漂移)。
- 变化 → **立即重建结构(review 修 C:两引擎统一"全量重建 + last_price 重放"
  语义)**:重建 flatten/tree 全部预处理 → 执行初始化发出(修 A)→ 对已有
  last_price 的叶子做一轮合成重放(批量 tick 语义)→ 两引擎恢复到"新结构×旧价格"
  的一致状态。规模 ~200 叶重建+重放 <10ms,PDF"重开拍平贵"的顾虑不存在。
  树引擎的 diff 增量补丁**降级为后期优化项**(热更新路径上两引擎状态必须由同一
  确定性过程产生,增量补丁引入两侧状态分歧风险,先正确后快);
  - 两引擎重建后**互相校验**(同一结构 hash、同一 required 集、重放后全层级值
    相等),不一致即 abort。
- **盘后变化的重建语义**:盘后无新行情,重放用**当日收盘 last_price**——
  `nav_latest` 立即变为"新持仓 × 最后已知价格"(这正是用户要的"马上变":
  19:00 AISS 调仓落文件 → ≤5s 内重建 → 前端下次轮询即见新结构与新值);次日
  开盘行情 tick 自然接续。**逐策略滚动更新是常态**:一夜之间会发生多次重建
  (AISS→BDC→pairs 各自触发),每次全量重建+重放(修 C 语义,便宜且确定),
  `structure_snapshot_{hash}` 逐次留痕。
- 重建后当前 tick 的所有 last_price 保留(价格状态与结构状态解耦),
  seen/required 按新结构重算,**新增叶子在下一价格到达前 gating 该组合不发**
  ——这正是 PDF 的 seen/required 语义在结构热更新下的自然推广。

### 2.5 标识体系(Identifier Scheme)——引擎内部一律编码,名字只在显示边界

> 用户令(2026-08-12):股票用 ISIN/CUSIP;策略与中间层没有标准编码且名字格式
> 不统一(pair "DG/MOS"、subsector "memory_hbm" 都是随手起的字符串)→ 自创一套
> 格式统一的层级编码;中间过程全用编码,到显示/前端才翻回名字;名字↔编码 mapping
> 集中维护。以下为 reference-data 级设计(security master + node registry 双表)。

#### 2.5.1 叶子层(股票/ETF/BDC 股):ISIN 为主键
- **主键 = ISIN**(12 位,含校验位)。美股 ISIN 可由 CUSIP 确定性派生:
  `US + CUSIP(9) + Luhn 校验位`——实现该派生并单测(拿已知票对拍公开 ISIN)。
- **security_master**(`controller/registry/security_master.json`,git 版本化):
  ```
  { "US4581401001": { "cusip": "458140100", "ticker": "INTC",
      "figi": "BBG000C0G1D1", "cik": 50863, "name": "Intel Corp",
      "asset_class": "equity|etf|bdc_equity",
      "polygon_ticker": "INTC",
      "ticker_history": [ {"ticker":"INTC","from":"…","to":null} ],
      "status": "active", "registered_at": "…" } }
  ```
- **构建与补录**:装配层收集五策略持仓 ticker 并集 → Polygon
  `/v3/reference/tickers/{t}`(cusip/composite_figi/cik/name)→ 派生 ISIN 注册。
  **Polygon 未返回 cusip 的票**(权限/OTC 边角):用 `XP` 前缀的 FIGI 占位 ISIN
  (`XP` 非法定国别码,机器可识别为占位)并**大声报警要求人工补录**——绝不静默
  编造合法形态的假 ISIN(不 fallback 纪律)。
- **ticker 漂移**:ticker 是会变的(公司行为),ISIN 不随名变。Polygon 返回与
  master 不一致 → 报警 + 人工确认后把旧 ticker 关入 `ticker_history`、更新
  `polygon_ticker`。价格层永远以 master 的 `polygon_ticker` 发请求、以 ISIN 回填。

#### 2.5.2 非叶层(portfolio/strategy/pair/subsector):自创 SPID
- **格式(定长 11 字符,仿 CUSIP/ISIN 工程学,含校验位)**:
  ```
  SP <TT> <XXXXXX> <C>
  │   │      │      └ 1 位校验(base36-Luhn,防抄写/手误——与 ISIN 同等纪律)
  │   │      └ 6 位 base36 payload(确定性派生,见分配规则)
  │   └ 2 位类型码: PF=portfolio  ST=strategy  PR=pair  SS=subsector
  │                 SL=sleeve/预留容器
  └ 恒定前缀(SomeoPark)
  例: SPPF000001X  SPSTMRPT01A  SPPR7K2M9QF  SPSS4B8N2LC
  ```
- **身份锚定在"是什么"而非"叫什么"(canonical key)**——直接解决名字不标准问题:
  | kind | canonical key(决定 SPID 的唯一依据) | 效果 |
  |---|---|---|
  | PF | 常量 "PORTFOLIO" | 固定 1 个 |
  | ST | 策略代号(mrpt/mtfs/aiss/ssrs/bdc,系统内生标识) | 固定 5 个,注册表手工 seed |
  | PR | **见 §2.5.2a 完整规范**(方向敏感的有序对,经规范化函数归一) | pair 身份 = 谁多谁空,与名字书写顺序和 direction 记账形式解耦 |
  | SS | `strategy + qlib config 的 subsector 键`(如 memory_hbm——代码键,非显示名) | 显示名可改,SPID 不动 |
- **payload 派生**:`base36( sha1(canonical_key)[:N] )` 取 6 位 → 注册表内冲突则
  确定性线性探测(+1)。确定性派生 + 注册表落盘双保险:重建 registry 也得到同一批
  ID;registry 是权威(append-only,**ID 永不复用、永不改、退役不删**)。

#### 2.5.2a Pair 标识完整规范(方向变异,2026-08-12 前端截图实证后定稿)

**问题**:pairs 系统的记账形式是 `(名字 "S1/S2", direction, s1_shares, s2_shares)`,
且 direction 改变名字的方向语义(前端截图实证):
- `direction=long` → **s1 做多**(shares>0)、s2 做空(shares<0)。实例:
  `XOM/V, long` = 多 XOM 1,064 股、空 V −541 股;
- `direction=short` → **s1 做空**(shares<0)、s2 做多(shares>0)。实例:
  `ESS/EXPD, short` = 空 ESS −130 股、多 EXPD +227 股。

同一经济方向存在两种书写:`(ESS/EXPD, short)` ≡ `(EXPD/ESS, long)`(都是
多 EXPD 空 ESS)→ **必须同一 SPID**;而 `A/B,long` 与 `A/B,short` 是相反交易
→ **必须不同 SPID**。ID 若绑名字字符串,两条都会做错。

**规范化函数(装配层唯一入口,伪代码)**:
```
def canonical_pair_key(strategy, s1_isin, s2_isin, direction, s1_shares, s2_shares):
    # 1) 由 direction 解出经济方向
    if direction == "long":   long_leg, short_leg = s1_isin, s2_isin
    elif direction == "short": long_leg, short_leg = s2_isin, s1_isin
    else: ABORT("unknown direction")            # 不猜
    # 2) 交叉校验:shares 符号必须与 direction 一致(截图数据即此规律)
    #    long → s1_shares>0 且 s2_shares<0;short → s1_shares<0 且 s2_shares>0
    if sign(s1_shares, s2_shares) 与 direction 矛盾: ABORT("direction/shares 不一致")
    # 3) 身份 = 有序方向对(与书写彻底解耦)
    return f"{strategy}|L:{long_leg}|S:{short_leg}"
```
- **ABORT 而非容错**:direction 与 shares 符号矛盾 = 上游数据异常,装配失败
  大声报错(不 fallback 纪律)——宁可停,不可把方向搞反。

**等价类真值表**(测试矩阵直接照此断言):
| 记账形式 | 经济方向 | canonical key | SPID |
|---|---|---|---|
| `A/B, long` | 多A空B | `L:A\|S:B` | **X** |
| `B/A, short` | 多A空B | `L:A\|S:B` | **X**(同上,共享) |
| `A/B, short` | 空A多B | `L:B\|S:A` | **Y** |
| `B/A, long` | 多B空A | `L:B\|S:A` | **Y**(同上,共享) |
| X vs Y | 相反交易 | 不同 key | **X ≠ Y** |

**shares 与 ID 的边界**(用户令):做多多少股、做空多少股**不进 ID**——那是
children 层的 shares 记录(每层已有);同方向对不同批次/不同股数 → 同 SPID,
数量差异体现在当期结构与历史快照。hedge ratio、param_set 等同理入 attrs 不入 ID。

**display 层**:`display_name` 保留系统原始书写(如 "ESS/EXPD"+direction=short
的原样),`aliases` 收集同一 SPID 见过的全部书写变体——前端永远显示用户熟悉的
写法,内核永远只认方向对。

#### 2.5.3 node_registry(名字↔编码 mapping 的唯一维护点)
`controller/registry/node_registry.json`(git 版本化,append-only):
```
{ "SPPR7K2M9QF": { "kind": "pair", "strategy": "SPSTMTFS01…",
    "canonical_key": "mtfs|US26312P1057|US6541061031",
    "display_name": "DGX/NKE",          ← 只有显示层消费
    "legs": ["US26312P1057","US6541061031"],
    "status": "active|retired", "first_seen": "…", "retired_at": null,
    "aliases": ["DGX/NKE"] } }
```
- **生命周期**:装配层遇到新结构名 → 算 canonical key → registry 查有 → 复用;
  无 → 派生分配 + append 注册。pair 平仓 → `retired`(不删——历史 nav 流引用它;
  重开同两腿 → 同 SPID 回到 active)。
- **解析失败即失败**:装配时任何名字解析不出 ID → **装配 abort 大声报错**,
  绝不带着裸名字进引擎(这是"中间过程只用编码"的强制执行点)。
- **维护制度**:①每次装配自动 reconcile(新增自动注册、漂移报警);
  ②`registry/changelog.jsonl` 记每次注册/退役/别名变更(审计轨);
  ③每周校验 job:master 全量对 Polygon reference 重验 + registry 孤儿检测
  (retired 但仍被持仓文件引用 = 异常);④registry 文件随 controller/ 入 git,
  变更走 commit(reference data 的变更历史=git 历史)。

#### 2.5.4 显示边界(唯一允许出现名字的地方)
- 引擎(两个)、寄存器、风险矩阵、状态文件:**只见 ISIN/SPID**。
- `nav_stream_*.csv` 输出双列:`node_id, display_name`(display 由 registry 即时
  渲染,名字变更不污染历史流的 id 列);
- 前端/报告/日志的人读行统一经 `registry.render(id)`;
- Polygon 请求是另一个边界:出引擎前 ISIN→polygon_ticker,回来即换回 ISIN。

#### 2.5.5 对既有设计的传导修改
- §2.2 Node:`name` 字段改为 `id`(ISIN/SPID),原名字进 `attrs.display_name`;
- §2.3 装配表:各策略装配第一步即"名字→ID 解析"(经 registry);
- §4.2 输出与 §六测试:所有断言键改 ID;新增测试——check digit 单测、
  canonical key 幂等(重建 registry 得同 ID)、pair 平仓重开同 ID、
  **pair 方向真值表全量断言(§2.5.2a:四种记账形式两两归入 X/Y 两个等价类,
  X≠Y)+ direction/shares 符号矛盾必须 ABORT**、
  ticker 漂移场景、ISIN 派生对拍公开值、解析失败必须 abort;
- §七目录:新增 `registry.py`(派生/校验/解析/渲染)与 `registry/` 数据目录
  (master + node_registry + changelog,**入 git**;与 output/ 的 gitignore 区分);
- §八里程碑:M1 拆为 M1a(registry+security master,先行)与 M1b(结构装配,
  依赖 M1a)——ID 体系是地基,先于一切装配。

---

## 三、价格层(Polygon 唯一源)

- **主通道**:`GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=...`
  一次批量拿全宇宙 last trade/minute bar(我们全书叶子 ~200 只,一次调用足够,
  远离限速);盘中每 tick 一次批量快照。
- **备通道(同 Polygon,不算换源)**:分钟聚合
  `GET /v2/aggs/ticker/{t}/range/1/minute/...` 用于补一分钟缺口/回填当日序列。
- **交易日历与时段**:复用 repo 现有 NYSE 日历习惯(pandas_market_calendars /
  现有 `trading_days` 工具);非交易时段 tick 跳过(记录 skip 原因,不静默)。
  **实现定案(2026-08-12 用户令,取代"跳过")**:闭市 tick 不跳过而是
  **平移续写**(Robinhood 式)——不拉快照(零 API 开销),沿用 last_price
  每 interval 照常落一行,价格不变即平线匀速右移;开市/extended-hours 恢复
  正常拉快照。闭市平移不算 stale(payload 的 market=closed 已说明状态)。
- **失败语义**:Polygon 请求失败 → 指数退避重试;连续失败 N 次 → 该 tick 标
  `stale`,**沿用 last_price 不注入假价**,并在输出行打 `stale=true` 标记;
  绝不切换数据源(纪律 2)。
- key:`.env` 的 `POLYGON_API_KEY`(与全 repo 同惯例)。
- **新鲜度验证(M4 一次性)**:更新频率 ≠ 价格新鲜度——snapshot 是实时还是
  延迟 15 分钟取决于 Polygon 订阅等级。实测一次返回时间戳确认;若 delayed,
  看板与 nav 流如实标注 `feed_delay_min`,频率照常,**不因此换源**(纪律 2)。

---

## 四、调度与输出

### 4.1 调度
- `controller/run_controller.py --interval {1m|5m|15m|60m}`:循环 =
  (结构 watcher)→(Polygon 批量快照)→(两引擎并跑)→(对拍 assert)→(落盘)。
- 多频率同跑:单进程一个 1m 主循环,5/15/60m 是主循环的整分对齐子采样
  (不开四个进程抢 Polygon)。
- 与现有系统隔离:独立进程,不进 conductor 生产流水线;由 openclaw/cron 外部
  调度(与 vp/bdc 同惯例),脚本自身不装 cron。

### 4.2 输出(controller/output/,append-only)
- `nav_stream_{YYYYMMDD}.csv`:`ts, node_id, display_name, kind, value, stale`
  ——**双引擎对拍通过后才落盘,前端只见一套数字**(2026-08-12 定稿,取代早先
  "逐字节"的过强提法):
  1. **发布来源 = 树引擎**(天然全层级+attrs 挂载,喂看板层级展开);
     **拍平引擎 = verifier**(每 tick 全层级对拍)+ 独家供风险矩阵;
  2. **对拍容差**:数学上两引擎同一线性函数必然相等,但浮点不结合——两引擎
     累加顺序不同,真实小数价下有 ulp 级表示差异。判定:`|Δ| ≤ max(1e-6 美元,
     1e-9 相对)` 视为相等;超出 = 真 bug → **abort 不发布**(看板停留上一笔,
     绝不发可疑数字)并 dump 两侧寄存器;
  3. **定期 rebaseline(防增量漂移)**:两引擎都是增量累计,漂移会同向积累、
     对拍抓不到——每 30 分钟(及每次结构重建后)做第三重审计:
     直接全量求和 `Σ eff_shares×last_price + cash_flat` 与两引擎值三方核对,
     超容差以全量值 rebase 两引擎寄存器并记 warning。增量引擎的标准收敛手段。
- `risk_matrix_latest.json`:拍平引擎的反向索引快照
  (股票 → [(组合, 有效净暴露)])——PDF 说的"这张反向清单就是风险矩阵",
  直接给风控/"AAPL 跌 5%"式冲击用。
- `structure_snapshot_{hash}.json`:每次结构重建落一份(层级+attrs+时间),
  结构变化历史自然成审计轨。
- 寄存器状态(seen/required/value)进 `controller_state.json`(崩溃恢复:
  重启=重建结构+当日价格重放,状态可丢弃重算——**无状态优先**,不做复杂持久化)。
- `nav_latest.json`:最新一轮全层级值(供前端看板轮询,原子写)。

### 4.3 前端看板 artifact(用户确认 2026-08-12;M7 交付)
在 someo-park 现有 ~22 个 artifacts 体系里加一个 **RealtimeNavViewer**
(实时净值看板),照 StrategyPerformanceViewer 同模式三件套:
1. `src/components/artifacts/RealtimeNavViewer.tsx`:频率选择器
   [1m|5m|15m|60m](对 nav_stream 子采样)+ **层级展开视图**
   (PORTFOLIO → 5 策略 → pair/subsector 每层实时值与日内收益——树引擎全层级
   值的前端落地)+ 30-60s 轮询 + `stale/corp_action/feed_delay` 标注;
2. Express server 一个**只读**路由(`/api/controller/nav`),读
   `controller/output/nav_latest.json` 与当日 nav_stream——server 代码属前端侧,
   读 controller 输出文件,两侧边界都干净(controller 纪律 6 不破);
3. 侧栏按钮 + `artifactDetector` 注册(与既有 artifacts 同法)。
显示层规则:全部经 `registry.render(id)` 把 SPID/ISIN 翻回 display_name
(§2.5.4 显示边界的前端落地),id 保留在行数据里供跳转/调试。

**与 StrategyPerformanceViewer 的吻合契约(用户令 2026-08-12)**:
- **锚定点必须吻合,盘中允许节奏差**——两者 update 节奏不同(官方=日频 EOD,
  看板=分钟级),不可能也不要求逐点一致;但在锚定时点必须对上:
  1. **开盘锚**:看板每日基准 `V_base` = 三 perf json 各策略**最后一行 EOD 值**
     (§五同步契约的前端落地)——日内曲线的起点严格等于官方曲线的终点,
     两条曲线首尾相接无跳变;
     **实现定案(2026-08-12,依 §五口径事实)**:日内 % 的分母不能直接用官方
     EOD 绝对值(pairs/aiss 两口径绝对值不互比,aiss ratio≈0.38 会算出荒谬数),
     故:日内收益 r = V(t)/V_prev_close(controller 自身昨收,`/prev-close`,
     含隔夜跳空,与官方日收益同口径);"首尾相接"用换算落地——展示
     `官方锚 ≈ official_EOD × (1+r)`(即 §4.3.3 的"×官方基准换算展示"),
     两条曲线在官方口径下无缝衔接。首日无昨收退回当日首笔并如实标注;
  2. **收盘锚**:16:00 快照与当晚官方新 EOD 行的偏差 = §五对账值——看板头部
     常驻显示 `vs official EOD: ±x.xx%`(前一日对账结果 + 归因摘要 tooltip:
     股息/费用/口径),偏差超阈值标黄——**吻合不是假设,是每天被展示的校验**;
  3. 口径提示:pairs 卡片标注净值为账本口径日内收益(×官方基准换算展示),
     与 §五"日内收益率对账"一致,绝不显示两套打架的绝对值。
- **持仓变化的前端即时反映(用户点明的最关键需求)**:
  1. `nav_latest.json` 携带 `structure_hash`、`last_rebuild_ts`、
     **每策略 `positions_as_of`**(该策略结构文件的 as_of);
  2. 前端轮询发现 `structure_hash` 变化 → 层级视图立即刷新 + 显式提示
     ("持仓已更新 · MTFS 5→4 对 · 03:12",diff 摘要来自相邻两份
     structure_snapshot)——用户在盘后/凌晨打开看板,看到的永远是**当前真实
     持仓**,不是昨天的;
  3. **各策略 as_of 不齐是常态而非异常**(更新时刻天然错开):每张策略卡片
     显示自己的 `positions_as_of`,过夜窗口(部分已更新、部分未更新)如实
     呈现混合状态,不假装整齐;
  4. **日切(rollover)**:每策略独立——该策略的官方 json 长出新 EOD 行时,
     其 `V_base` 即切换到新行(开盘锚随之推进);看板无需全局"换日时刻"。
- **UI 借鉴 StrategyPerformanceViewer**(同一视觉语言,用户已确认可模仿):
  顶部模式/策略 toggle 条(STRATEGIES/MASTER 式样 → 这里为 层级/策略 筛选 +
  频率四档)、scorecard 网格(每策略卡:当前值/日内收益/vs official)、
  主曲线区(日内 equity 线,可叠多策略;复用其配色 COLORS 与轴样式)、
  日期不可选(永远今天,历史看官方 viewer——两个 artifact 分工:官方=历史日频,
  本看板=当日分钟级,互补不重复)。

---

## 五、与三个 performance json 的同步契约(不动它们)

**真源划分(关键设计,避免两套净值打架)**:
- **日终(EOD)真源 = 现有三个 json**:`strategy_performance.json`(MRPT/MTFS,
  凌晨 pipeline 产)、`master_portfolio_performance.json`(UpdateMasterPerformance
  拼接 SR/AISS backtest+live + BDC,**拼接日期逻辑原样不动**)、
  `private_credit_bdc_performance.json`(UpdateBDCPerformance)。
- **盘中真源 = controller**:分钟级净值是这三个 json 完全没有的能力,不冲突。

**口径事实(2026-08-12 调查定案,对账设计的前提)**:pairs 存在**两套 equity
口径**——账本口径(`account_{s}.json`,$1M 起账+realized+unrealized;controller
盘中估值用它,因 cash/positions 都在此)与官方口径(`strategy_performance.json`
= `regime_capital × sim_equity/500k`,UpdateStrategyPerformance.py:44-45)。
绝对值不可互比,但同一持仓的**日收益同源**。故对账一律用**日内收益率**
(`V(t)/V_prev_close − 1` vs 官方曲线次日日收益),绝对值只在各自口径内部自洽。

**同步点与动作**:
1. **开盘前(标定)**:controller 读三 json 的**最后一行 EOD 值**作为各策略
   基准值 `V_base`——**每策略的精确锚定列(2026-08-12 实测定案)**:
   | 策略 | 锚定 json | 列 |
   |---|---|---|
   | MRPT/MTFS | `strategy_performance.json`(原始源) | `mrpt_equity`/`mtfs_equity` |
   | SSRS/AISS | `master_portfolio_performance.json`(其 live 段唯一落盘处) | `sr_equity`/`aiss_equity` |
   | BDC | `private_credit_bdc_performance.json`(原始源) | `bdc_equity` |
   (master 里的 mrpt/mtfs/bdc 列是拼接副本,不作锚——锚定永远用原始源。)
   当日盘中输出同时给绝对值与 `V/V_base-1` 日内收益,保证与官方曲线无缝衔接。
2. **收盘后(对账,不是覆写)**:等三个 json 被各自 pipeline 更新后,
   `controller/reconcile_eod.py` 把 controller 的 16:00 快照与 json 新 EOD 行
   **对账**。**方法修订(2026-08-12 用户令:不依赖"ratio 恒定"假设——ratio
   会因合法原因移动:pairs regime capital 日变、分红、费用;ratio 只做信息
   记录,不做判定)**:判定用**同日期对齐的日收益差**——
   `r_off = off(D)/off(D_prev)−1`(官方最后两行)vs
   `r_ctl = ctl_close(D)/ctl_close(D_prev)−1`(nav_stream 各日 16:00 ET 截断
   末笔),`diff_bp = (r_ctl − r_off)×1e4` 超阈值(初设 20bp,影子周实测校准)
   → 报告差异并归因(已知合法差异源:json 侧含股息入账/费用/回测-live 拼接段、
   pairs regime 资本重标定、AISS 篮子理论权重 vs account 实仓、BDC DRIP 日、
   corp_action split 日)。controller 侧尚无对应两日收盘数据 → baseline 只记录。
   **对账报告落 `controller/output/reconcile_{date}.json`,绝不回写三个 json。**
3. 拼接日期(SR_LIVE_START/AISS_LIVE_START 等)对 controller 透明——controller
   只消费三 json 的**最终输出行**,永不复算它们的拼接;这保证"拼接逻辑不能变
   且 working"约束天然满足。

---

## 六、测试计划(移植 PDF 的 14 case + 我们的真实数据)

**PDF 用例逐条移植**(两引擎跑同输入 assert 完全一致——`both()` 纪律):
1. 精确小例(手算);2. 嵌套+gating(叶子未齐不发);3. 重定价只发变化的组合;
4. 不在任何组合的股票照记不扰动;5. 深链先发最深(拓扑);6. **diamond 两路合并
只发一次**(构造:同一股票在两个 pair + AISS);7. 直接持有+经容器持有 shares
相加;8. **负 shares 多空**(pair 空腿);9. 小数 shares(BDC DRIP 4 位);
10. 全对冲名字动价不触发重估(构造两 pair 净 0);11. 同价重复不重发;
12. 定义顺序无关;13. 扇出(一股票在多组合);14. 循环报错停下(防御,虽然我们
结构装配不会产生循环)。
**新增我们特有的**:
15. 空策略(MRPT 0 对)——策略值语义正确、不 gating 别人;
16. 结构热更新:tick 之间 pair 开/平仓 → 两引擎重建后与"冷启动装配同一结构"
    逐字节一致;新叶子 gating 语义正确;
17. cash_const 正确进值不进 seen/required;
18. **真实数据端到端**:用某历史日 EOD 价格喂两引擎,五策略值与三 json 当日
    EOD 行差 < 对账阈值(归因文档化);
19. 规模/速度:PDF 的 5000 股×5 万更新 0.1s 基准移植(我们规模小,该过);
20. Polygon 失败注入:stale 标记、不换源、恢复后自愈。
全部测试写 `controller/tests/`,**输出只进 /tmp**(repo 测试纪律)。

---

## 七、目录与模块(controller/,不 gitignore,可上传)

```
controller/
  PORTFOLIO_DATA_MAP.md          # 已有(本计划的持仓数据依据)
  CONTROLLER_DEVELOPMENT_PLAN.md # 本文件
  registry.py        # §2.5 标识体系:SPID 派生/校验位/解析/渲染 + security master 维护
  registry/          # reference data(入 git):security_master.json + node_registry.json + changelog.jsonl
  model.py           # Node/结构装配(五策略 adapter,只读三层持仓文件;第一步名字→ID)+ 结构hash
  engine_flatten.py  # 方法1:flatten/reverse-index/fan-out + 风险矩阵导出
  engine_tree.py     # 方法2:held_directly_by/heap 上推/每层值
  prices.py          # Polygon 批量快照+分钟回填+重试/stale 语义(唯一价源)
  scheduler.py       # tick 循环/多频率子采样/结构 watcher
  reconcile_eod.py   # 与三 perf json 的日终对账(只读它们)
  run_controller.py  # 入口
  tests/             # §六 全部用例(两引擎对拍为核心断言)
  output/            # nav_stream/risk_matrix/reconcile(gitignore 数据,代码入库)
```

---

## 八、分阶段实施(每阶段可独立验收,建议节奏)

| 阶段 | 内容 | 验收 |
|---|---|---|
| M1a | `registry.py` + `registry/`:SPID 体系(派生/check digit)+ security master(Polygon reference 拉全宇宙,ISIN 派生)+ node registry seed(PF×1/ST×5/当前 pair·subsector 全量注册) | check digit 与 ISIN 派生单测过;重建 registry 幂等(同 canonical key→同 ID);全宇宙无 XP 占位(或占位清单人工确认) |
| M1b | `model.py`:五策略结构装配(第一步名字→ID,解析失败 abort)+ attrs 保留 + 结构 hash(口径已定案:空仓=cash_const(account.cash);ARM=diamond 双路不消歧) | 装配叶子股数与 PORTFOLIO_DATA_MAP 逐票一致 + pairs `cash+Σ腿≈account.equity` + AISS `Σbranch=account 总数` + 引擎输入零裸名字 四条自检过 |
| M2 | `engine_flatten.py` + `engine_tree.py` + tests 1-14(PDF 用例) | 两引擎全用例逐字节一致 |
| M3 | tests 15-17(空策略/热更新/cash)+ `prices.py`(Polygon) | 结构热更新对拍;真实快照拉通 |
| M4 | `scheduler.py` + 输出流 + tests 19-20 | 1m 循环连续运行一交易日无 stale 误报 |
| M5 | `reconcile_eod.py` + test 18 | 与三 json 对账差异归因报告可读、阈值内 |
| M6 | 影子运行一周(与三 json 每日对账),然后交 openclaw 排程 | 一周对账全绿 |
| M7 | 前端看板 artifact(§4.3 三件套)+ Polygon 新鲜度标注 | 看板四档频率可切、层级可展开、名字经 registry 渲染;build+deploy 后线上可用 |

---

## 九、已识别难点(正面解,不绕过)

1. **AISS subsector→股票归属**:inventory 是子板块层、account 是股票平铺,
   中间映射在 qlib universe(80/15/5 档位,ARM 双板块)。解法:M1 把归属规则
   显式建模(universe 定义为准、当期权重消歧),用 account 实仓股数做叶子
   (真实可交易数),subsector 层值=其成分股实仓值之和——**层值可与 inventory
   的 weight×capital 对账**,差异=篮子理论权重 vs 实仓漂移,as 报告输出而非吞掉。
2. **pairs 的 capital/空仓语义**:inventory 有 `capital` 字段;pair 值=两腿净值
   (可为负);策略净值 = capital + Σ未实现?——与 strategy_performance 的
   mrpt_equity 口径对齐是对账关键,M1 先实测 json 口径再定式(不臆想)。
3. **BDC DRIP 日结构变**:shares 当日凌晨被 UpdateBDCPerformance 回写——
   watcher 自然捕获;对账日 BDC 差异应恰为当日股息,归因规则写死。
4. **股息/费用**:controller 是纯价格×股数视图,不含股息现金;三 json 含。
   对账归因必须显式列此项(pairs DIV 行、AISS/SSRS cumulative_dividends、
   BDC DRIP),否则阈值会误报。
5. **Polygon 限速**:批量 snapshot 单调用覆盖全宇宙,1m 频率 = 每分钟 1-2 次
   调用,余量巨大;分钟回填批次化。
6. **时区**:全部 ET,与 repo 惯例一致;tick 时间戳写 ET ISO。
7. **盘中公司行为(review 补)**:split 生效日,Polygon 盘中价已是 split 后,
   而持仓文件的 shares 要到当日各策略 pipeline 才调整——controller 当日该票
   市值会跳变。处理:security_master 每日开盘前拉 Polygon splits 日历,当日有
   split 的票在 nav 流打 `corp_action=true` 标记(不改 shares——持仓文件是
   golden,不悖逆);对账归因规则把当日该票差异归入 corp_action 桶。

---

## 十、明确不做(scope 界限)

- 不改五策略持仓文件的生成方(纪律 3);不改三 perf json 脚本与拼接(纪律 4);
- 不做 tick 级(<1m)流式/websocket——分钟快照满足当前频率需求,接口留扩展;
- 不做数据库——append-only 文件与 repo 生态一致;
- 不在 controller 里做任何交易/信号逻辑——纯估值与对账。
