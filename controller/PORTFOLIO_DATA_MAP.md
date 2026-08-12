# 五策略持仓数据地图(MRPT / MTFS / AISS / SSRS / BDC)

> 2026-08-11 全面实测复核后编写。供统一持仓更新功能与任何下游消费方使用。
> 三层模型:**第一层 = 当前态文件**(单时点快照)· **第二层 = 快照历史目录**
> (每份是某时刻的完整快照)· **第三层 = 逐笔交易流水 ledger**(jsonl,一行一笔)。
> 所有路径相对 repo 根(`someopark-test/`)。

---

## 0. 汇总总表(2026-08-11 实测)

### 第一层:当前态文件(全部是单时点,`as_of` 一个日期)

| 策略 | 文件 | 粒度 | 写入者 / 更新时刻 |
|---|---|---|---|
| MRPT | `inventory_mrpt.json` + `account_mrpt.json` | pair 级 + 股票级/现金(账本口径) | DailySignal 凌晨 pairs pipeline(~10:00) |
| MTFS | `inventory_mtfs.json` + `account_mtfs.json` | 同上 | 同上 |
| AISS | `qlib-main/semiconductor_strategy/inventory_aiss.json` + `account_aiss.json` | 子板块级 + 股票级 | AISSdailySignal(~19:00) |
| SSRS | `qlib-main/sector_rotation/inventory_sector_rotation.json` + `account_ssrs.json` | ETF 级(双文件) | SectorRotationDailySignal(~17:45) |
| BDC | `inventory_bdc.json` | 股票级(5 BDC + BIL cash 腿) | UpdateBDCPerformance 凌晨 pipeline 自动回写 |

### 第二层:快照历史(每份=完整快照)

| 策略 | 目录/模式 | 份数 | 覆盖 | 触发 |
|---|---|---|---|---|
| MRPT | `inventory_history/inventory_mrpt_YYYYMMDD_HHMMSS.json` | 183 | 2026-03-19 → 今 | 事件驱动(开/平仓) |
| MTFS | `inventory_history/inventory_mtfs_*.json` | 191 | 2026-03-19 → 今 | 同上(同日可有 2 份:开仓一份+平仓一份) |
| MRPT/MTFS(账本) | `account_history/account_{mrpt,mtfs}_YYYYMMDD.json`(repo 根) | 198 | 2026-03-19 → 今 | 每交易日(pairs_ledger 冻结快照) |
| AISS | `qlib-main/semiconductor_strategy/account_history/account_aiss_YYYYMMDD.json` | 50 | 2026-06-01 → 今 | 每交易日一份 |
| SSRS | `qlib-main/sector_rotation/account_history/account_ssrs_YYYYMMDD.json` | 70 | 2026-05-01 → 今 | 每交易日一份 |
| BDC | `inventory_history/inventory_bdc_*.json` | 21 | **2025-11-11(建仓日)→ 今** | 事件驱动(DRIP;历史 19 个事件日已回补,文件名合成时间戳 `_160000`) |

### 第三层:逐笔 ledger(jsonl)

| 策略 | 文件 | 笔数 | 覆盖 | 行类型(side) |
|---|---|---|---|---|
| MRPT | `trade_ledger_mrpt.jsonl` | 433 | 2026-03-19 → 今 | BUY/SELL(带 lot)+ DIV |
| MTFS | `trade_ledger_mtfs.jsonl` | 519 | 2026-03-19 → 今 | 同上 |
| AISS | `qlib-main/semiconductor_strategy/trade_ledger_aiss.jsonl` | 37 | 2026-06-02 → 今 | BUY/SELL/DIV/FEE |
| SSRS | `qlib-main/sector_rotation/trade_ledger_ssrs.jsonl` | 26 | 2026-06-01 → 今 | BUY/SELL/DIV/FEE |
| BDC | `trade_ledger_bdc.jsonl` | 32 | **2025-11-11 → 今(完整)** | BUY(action=OPEN/DRIP) |

---

## 1. MRPT / MTFS(pairs,结构完全相同)

### 第一层 `inventory_{mrpt,mtfs}.json`
```json
{ "as_of": "2026-08-10",
  "capital": <浮点, 策略资金>,
  "pairs": { "DGX/NKE": { ...见下... }, ... } }
```
`pairs` 是 dict,**开仓与未开仓条目混在一起**,判定开仓 = `direction` 非 null:
- **开仓条目**字段:`strategy / param_set / direction(long|short) / s1_shares /
  s2_shares(负数=空腿) / open_date / open_s1_price / open_s2_price /
  open_hedge_ratio / open_price_level_stop / days_held / peak_unrealized_pnl /
  open_signal(入场时信号快照) / wf_source / monitor_log`
- **未开仓条目**:`direction: null`,且若历史上开过仓会残留**最近一次**平仓痕迹:
  `last_close_date / last_close_pnl / last_close_action(CLOSE|CLOSE_STOP) /
  last_close_param_set`。⚠️ 只有最近一次,完整平仓史要看 ledger。
- 实况:MRPT 47 对全部未开仓;MTFS 124 对中 5 对开仓
  (CRL/MLM, DGX/NKE, HPE/LKQ, NTAP/TXT, PANW/DECK)。
- 纪律:shares 入场固定,HOLD 不改仓;monitor(Step1)只写 CLOSE/CLOSE_STOP。

### 第一层补充(2026-08-12 调查发现,此前遗漏):`account_{mrpt,mtfs}.json`(repo 根)
pairs_ledger 账本文件,结构与 AISS/SSRS 的 account 同构且更全:
`cash / positions(股票级,pair 两腿平铺)/ equity / unrealized / lots(带 pair
归属)/ cumulative_realized/dividends/fees / price_basis_state`。
- **cash 的 golden 源在这**:MRPT 当前 0 持仓 → `equity = cash = 1,042,112.28`
  (空仓即全现金,实测印证)。
- ⚠️ **两套 equity 口径并存(既定事实)**:account 账本口径($1M 起账 +
  realized + unrealized)≠ `strategy_performance.json` 官方口径
  (`regime_capital × (sim_equity/500k)`,sim 从 inventory_history 重放,
  UpdateStrategyPerformance.py:44-45)。绝对值不可直接互比;同一持仓的
  日收益同源。消费方对账应用**日内收益率**而非绝对值。
- `inventory_*.json` 的 `capital: 500000` 是**回测 scaling 分母,不是现金**。

### 第二层 `inventory_history/`
完整 inventory 的时刻快照;文件名时间戳=事件发生时刻。**同一天可有多份**
(新开仓存一份、平仓再存一份)。2026-03-19 之前无快照(机制该日上线)。
账本侧另有每交易日冻结快照 `account_history/account_{mrpt,mtfs}_*.json`(repo 根,
198 份,2026-03-19 起)。

### 第三层 `trade_ledger_{mrpt,mtfs}.jsonl`
行格式(lot 机制,单腿一行):
```json
{ "date": "2026-03-19", "ticker": "EVRG", "side": "BUY", "shares": 1313,
  "price": 81.54, "gross": -107062.02,
  "lot": "EVRG/AVB|s1", "lot_action": "OPEN", "price_basis": "open_price",
  "avg_cost_at_trade": ..., "realized_pnl": ..., "lot_cost": ..., "lot_realized": ...,
  "dedup_key": "2026-03-19-EVRG-BUY-EVRG/AVB|s1" }
```
- `lot` = `PAIR|s1或s2`(哪个对的哪条腿);`lot_action` OPEN/CLOSE 配对。
- 另有少量 `side: "DIV"` 行(持仓期股息现金,无 lot;空腿股息为负支出)。
- ✅ 实测校验:按 lot 回放 OPEN−CLOSE,未平 lots 与 inventory 开仓对**完全一致**
  (mtfs 5 对、mrpt 0 对)。

---

## 2. AISS(半导体)与 SSRS(sector rotation,结构相同)

### 第一层(双文件)
**`inventory_aiss.json` / `inventory_sector_rotation.json`(策略层)**:
```json
{ "as_of": ..., "last_updated": ..., "capital": ...,
  "holdings": { "<子板块|ETF>": { "weight", "shares", "last_price", "cost_basis",
                                   "entry_date", "last_rebalance_date",
                                   "days_held", "action_today" } },
  "cash_weight": ..., "prev_weights": {...}, "prev_composite_scores": {...} }
```
- AISS 的 holdings 键是**子板块**(equipment/memory_hbm/logic_cpu…),shares 是
  合成篮子份额;SSRS 的键是 **ETF**(XLB/XLI/…),shares 即真实 ETF 股数。
- ⚠️ 不对称:AISS 额外内嵌 `rebalance_history`(逐次调仓记录,2026-05-29 起,
  含 date/reason/regime/weights)——**SSRS 没有这个字段**。

**`account_aiss.json` / `account_ssrs.json`(执行层)**——真实股票持仓在这:
```json
{ "as_of": ..., "initial_cash": ..., "cash": ...,
  "positions": { "KLAC": { "shares": 1678, "avg_cost": 185.36, "entry_date": "2026-05-29" } },
  "cumulative_realized": ..., "cumulative_dividends": ..., "cumulative_fees": ...,
  "equity": ..., "unrealized": ..., "position_value": ... }
```
`cumulative_*` 是**累计标量**(不是序列);逐日序列要看 account_history。

### 第二层 `account_history/account_{aiss,ssrs}_YYYYMMDD.json`
每交易日一份的 account 完整快照(AISS 自 2026-06-01、SSRS 自 2026-05-01)。

### 第三层 `trade_ledger_{aiss,ssrs}.jsonl`
```json
{ "date": "2026-06-01", "ticker": "XLB", "side": "SELL", "shares": 1001,
  "price": 50.7301, "gross": 50780.81, "avg_cost_at_trade": 51.92,
  "realized_pnl": -1191.11, "dedup_key": "2026-06-01-XLB-SELL" }
```
- side 有 4 种:BUY / SELL / **DIV**(股息现金,`shares` 为派息基数)/
  **FEE**(费用行,`ticker` 为空、无 shares)。消费时务必按 side 过滤。

### ⚠️ 建仓缺口与 seed 文件(2026-08-12 已补,方式经下游审计)
主 ledger 机制 2026-06 才上线、**晚于建仓**——且这是账本引擎的**有意设计**
(`portfolio_ledger/replay.py:54`,plan §4.5-1:"opening balance,不合成虚拟交易";
`replay --force` 会删除重建主 ledger,补进去的行不持久)。因此建仓补录**不进主
ledger**,而是同目录的独立种子文件(主 ledger 一字未动,三个下游零影响):

| 文件 | 内容 |
|---|---|
| `qlib-main/semiconductor_strategy/trade_ledger_aiss_seed.jsonl` | 9 行 OPENING(2026-05-29 建仓,来源=首快照 20260601 positions) |
| `qlib-main/sector_rotation/trade_ledger_ssrs_seed.jsonl` | 4 行 OPENING(XLE/XLB/XLI 2026-04-27,XLV 2026-05-01,来源=首快照 20260501) |

seed 行 schema 同主 ledger(BUY),外加 `action:"OPENING"` + `source` 溯源字段;
price=建仓 avg_cost。**✅ 实测:seed + 主 ledger(BUY/SELL)回放 == 当前
positions,每票 0.0 差、无幽灵票**——AISS/SSRS 现在可纯流水从零重构。

下游审计结论(为何不能直接补主 ledger):
- `replay.py --force` 重建时会抹掉补行(不持久);
- `reports.py:314` 期间成交总额=全量 Σ|gross|,补行会虚增日报 PDF 数字;
- `tca_backfill` 按 side∈{BUY,SELL} 收 fills,种子行会混入 λ 校准样本;
- 全部消费方用**精确文件名**引用主 ledger(无 glob),seed 文件不会被误捞。

**重构任意时点持仓的方法**:
- AISS/SSRS: `seed 文件 + 主 ledger 回放`(或:最早快照 + 之后 ledger);
- pairs: 2026-03-19 前无流水无快照(策略 2025-11-11 起跑)——只能以 3/19 首份
  快照为起点 + 之后 ledger;
- BDC: 纯 ledger 从零回放即可(天然完整)。

---

## 3. BDC(PC sleeve;2026-08-11 起用文件索引,不再写死代码)

### 第一层 `inventory_bdc.json`(唯一真源,fail-loud 校验)
```json
{ "strategy": "bdc_sleeve", "as_of": "2026-08-11", "inception_date": "2025-11-11",
  "allocation": { "bdc": 0.5, "cash": 0.5 },
  "cash": { "ticker": "BIL", "shares": 5173.08, "entry_date": ..., "drip_events": 9 },
  "holdings": { "GBDC": { "weight": 0.80, "cik": 1476765, "shares": 28686.3968,
                           "entry_date": "2025-11-11", "drip_events": 3 }, ... } }
```
- `weight` = sleeve 内相对权重(和=1);sleeve 占全书 `allocation.bdc`。
- `cik` 供 SEC 抓取(RefreshBDCHoldings)——加/换 BDC 只改这个文件。
- **三个消费方从此文件索引**(不再写死):`UpdateBDCPerformance.py`(tickers/
  weights/alloc + 运行后回写 shares)、`RefreshBDCHoldings.py`(BDC_UNIVERSE)、
  `portfolio_of_private_credit_deals/bdc_lookthrough.py`(BDC_ALLOC)。
- 校验:holdings 权重和=1、allocation 和=1,load 失败直接 raise。

### 第二层 `inventory_history/inventory_bdc_*.json`
事件驱动(shares 变化=DRIP 时自动存)。**唯一覆盖到建仓日的历史**
(2025-11-11 起,建仓 + 19 个 DRIP 事件日已回补;回补文件时间戳为合成的
`_160000` 收盘时刻)。

### 第三层 `trade_ledger_bdc.jsonl`
```json
{ "date": "2025-11-11", "ticker": "GBDC", "side": "BUY", "shares": 26460.8405,
  "price": 13.93, "gross": -368599.52, "action": "OPEN",
  "dedup_key": "2025-11-11-GBDC-BUY-OPEN" }
{ "date": "2026-08-03", "ticker": "BIL", "side": "BUY", "shares": 15.402,
  "price": 91.42, "gross": -1408.05, "action": "DRIP",
  "div_per_share": 0.273, "div_cash": 1408.05, "dedup_key": "2026-08-03-BIL-BUY-DRIP" }
```
- 只有 BUY(被动 sleeve 从不卖);`action`: OPEN=建仓 6 笔、DRIP=股息再投 26 笔。
- **全自动同步**:UpdateBDCPerformance 每日全期确定性重放 → 一次运行同时产出
  净值 json、回写 inventory(+变化快照)、**原子重写整个 ledger**——三层出自同一次
  重放,数学上不可能不一致(实测 |Σledger−inventory| 每票 = 0)。
- ✅ BDC 是唯一可**纯 ledger 从零回放**到任意时点的策略。

---

## 4. 消费注意事项(踩坑清单)

1. **判定 pairs 开仓看 `direction` 非 null**,不要数 pairs dict 的条目数(候选池混在里面)。
2. **AISS/SSRS 的真实股票持仓在 `account_*.json` 的 `positions`**,`inventory_*` 是
   策略层(AISS 的子板块 shares 是合成篮子概念,不是可下单股数)。
3. **ledger 的 DIV/FEE 行没有(或不该当作)shares**——回放持仓只用 BUY/SELL。
4. **建仓缺口**(§2):AISS/SSRS/pairs 不能纯 ledger 回放;快照+ledger 组合才对。
5. pairs 空腿 `s2_shares` 为负;pairs DIV 行里空腿股息 gross 为负(付出)。
6. 各家 `as_of` 语义:pairs=信号日(可能落后日历日一天);AISS/SSRS/BDC=更新当日。
7. 第二层触发方式不同:pairs/BDC 事件驱动(有仓位变动才存),AISS/SSRS 每交易日
   固定存——做"任意日期查询"时 pairs/BDC 要取"≤该日最近一份"。
8. BDC 的 `inventory_bdc.json` 是**三个生产脚本的启动依赖**(fail-loud):改动它
   必须保证权重和=1,否则当日 BDC 管线与 look-through 全部拒绝启动。
9. 精度:BDC shares 4 位小数(DRIP 碎股);pairs/AISS/SSRS shares 为整数。
10. 历史回补溯源:BDC 的 2025-11-11→2026-08 历史快照与 ledger 由
    `bdc_inventory.py --backfill / --ledger` 重放生成(收盘价 DRIP 口径,与
    UpdateBDCPerformance.build_portfolio 逐位同口径,final shares 已对拍一致)。
