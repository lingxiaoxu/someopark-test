# AEUS — AI Electric Utilities Strategy 建设计划

> **全名**: AI Electric Utilities Strategy(缩写 **AEUS**,与 AEUS 完全对等)
> **状态**: ⛔ **纯计划,未动工** — 用户说"开始"之前,本目录除本文件外一个字节不改
> **日期**: 2026-08-28
> **原型**: `qlib-main/electric_utilities_strategy/`(AEUS)整体复制而来(rsync 全量,文件数已核对一致,原目录零改动)
> **环境**: `conda run -n qlib_run`(与 AEUS 相同;**绝不用 someopark_run**)
> **隔离红线**: 只写本目录 + add-only 新建 `price_data/elec_strategy/`;AEUS 原目录、`price_data/elec_strategy/`、`price_data/macro/`(只读)一律不碰

---

## 0. 投资论点(为什么做电力)

AI 数据中心电力需求三年 CAGR ≈ 73%,把公用事业从防御收息板块变成成长板块。目标是**捕捉 AI 基建的成长红利**(非 10 年长持),因此:
- 宇宙覆盖从上游(核燃料/设备)到下游(数据中心供电)的**全产业链 8 个子板块**,不是只买 XLU;
- 信号核心与 AEUS 同构:**AI capex 是最上游驱动**,沿产业链有先后传导(IPP 签 PPA 最快 → 变压器订单 → 电网施工 → 受监管电价基数增长最慢);
- AEUS 的 4 hyperscaler capex 信号(MSFT/GOOGL/META/AMZN)**原样复用** —— 半导体和电力共享同一个最上游需求源,这是两策略的天然对称性。

---

## 1. AEUS 架构深度解读(迁移的依据)

AEUS 全链分六层,每层 AEUS 都要有等价物:

```
┌─ 运维层  aeus_pipeline.sh(15 模式) + daily_backtest.sh + 3 个 OpenClaw cron
│          (aeus-daily-backtest 19:10 ET / aeus-daily 20:20 ET / aeus-weekly 周日 03:30 ET,与 AISS 17:55/19:00/02:00 错开)
├─ 生产层  AEUSdailySignal.py:0-14 步主流程(资本=真实账本 equity → 信号 → 优化 →
│          风控四叠层 → 调仓判定 → 子板块→个股两层分解 → inventory/报表落盘)
├─ 选参层  AEUSBatchRun(42 param sets,A-H+M+N 共 10 组:6/4/9/4/3/2/4/4/3+3) → walk_forward(45+ folds,
│          DSR/WFE/oracle) → macro_clusters(23维→AE 12维 latent→6 KMeans 簇,encoder
│          持久化) → smart_select(P2 日度选参 + P3 宏观 tilt ±5% + P5 版本切换,防抖)
├─ 信号层  composite 4 因子(cs_momentum .30 / supply_chain .35 / capex_pulse .25 /
│          cycle_regime .10)× 4 状态 regime 权重矩阵 + 防御板块 RISK_OFF 加成
│          ── 核心 alpha = supply_chain 知识图谱(11-14 条边,lag 实证校准)
├─ 数据层  隔离 store price_data/elec_strategy/{prices,industry,company,altdata,cache}
│          PIT 纪律:availability date 键控 / merge_frozen 只追加 / XBRL YTD 去累计 /
│          IPO 分档门控 effective_weights(24 个月) / 分频率 staleness 阈值
└─ 回测层  qlib 主路径(SectorETFExchange/USTradeCalendar/AEUSWeightStrategy)+ 原生
           回退环;DD 断路器(-25% halve / release);portfolio_record 26-sheet Excel
```

关键既有教训(来自项目记忆,AEUS 必须继承不能重犯):
- composite **月末标签+次月首日调仓 = 天然 PIT 干净**,不要画蛇添足加 shift(crypto 拷贝才需要);
- DD 断路器曾是死代码(qlib 路径 equity_curve=None),2026-07-21 修好 —— 复制来的已是修复版;
- macro_clusters 三病灶(喂数残缺/私掏 API/基底漂移)已修:**encoder 训练一次持久化,serving 不重训**;
- 批测/WF 测试输出**严禁写生产文件夹**(--no-prod-write + /tmp)。

---

## 2. AEUS 宇宙设计(10 子板块,上游→下游)✅ 2026-08-28 定稿 + 2026-08-29 扩容

> 与 AEUS 的关键差异:**10 个板块(AEUS 8 个;板块数无硬编码,SUBSECTORS dict 驱动,已核验)、
> 每板块成员更多(4-5 只加权 + reserve),板块内配比不再是静态 80/15/5,
> 而是「静态先验 + 知识图谱驱动的动态纯度倾斜」**(机制见 §2.5)。
> 同时持仓的板块数照搬 AEUS 机制:top_n 选优,见 §2.4。

### 2.1 子板块与成员(base_w = 静态先验权重;IPO 门控/事故级联机制照搬 AEUS)

| # | key | 显示名 | 链位 | 加权成员(base_w) | reserve(0%) | 裁决备注 |
|---|---|---|---|---|---|---|
| 1 | `nuclear_fuel` | 核能与铀燃料 | 最上游 | BWXT .45 / LEU .20 / UUUU .15 / OKLO .10 / SMR .10 | NXE(首选;DNN 候补名单) | BWXT 长历史锚定;OKLO(2024-05)/SMR(2022-05)walk 门控进场,纯度分最高 |
| 2 | `grid_equipment` | 输配电设备/变压器 | 上游设备 | **ETN .40** / EMR .25 / **GEV .20** / POWL .15 | VMI(候补:AZZ, **HUBB**, ATKR) | ✅ 定稿:**ETN 当 primary 锚**;HUBB=公用事业 T&D 元件占营收过半、美股最纯电网元件商之一(候补池首位,D1 时与 POWL 比较定夺)(Electrical Americas 十年主业,历史完整,AI 纯度高于 EMR);GEV = 2024-04 截面重构后的「高纯度 alpha 放大器」,base .20 + 纯度分 1.0 靠动态倾斜放大 |
| 3 | `grid_epc` | 电网施工 EPC | 中游服务 | PWR .45 / FIX .25 / STRL .15 / MYRG .15 | DY(首选;ACM 候补名单) | 全长历史 |
| 4 | `ipp_wholesale` | 独立发电/批发电 | 中游发电 | VST .40 / CEG .30 / NRG .20 / TLN .10 | ORA | AI-PPA 主战场;CEG 2022-02、TLN 2023-07 门控 |
| 5 | `regulated_mega` | 受监管龙头 | 中游电网 | NEE .35 / SO .25 / DUK .25 / AEP .15 | D | 数据中心长约承载者 |
| 6 | `regional_utility` | 区域中小公用 | 下游配电 | AEE .35 / LNT .25 / OGE .20 / ATO .20 | BKH(首选;AVA 候补名单) | ATO=纯天然气公用(用户参考:天然气是 DC 最直接基载) |
| 7 | `dc_power_cooling` | 数据中心电力设备 | 最下游 | VRT .45 / TT .25 / CARR .20 / BE .10 | AOS(候补:**NVT**, CAT, CMI) | VRT 2020-02 锚定(可接受);BE=现场燃料电池(DC on-site 供电协议),高波动小权重;NVT=DC 母线/配电连接(VRT 近亲,候补首位);CAT/CMI=DC 备用柴发双龙头但工业巨头稀释(purity ~0.2,仅低纯度候补) |
| 8 | `renewables_storage` | 电网级绿电储能 | 中游绿电 | **NXT .40 / FSLR .25 / FLNC .20 / ARRY .15** | SHLS(候补:CSIQ, CWEN, BEPC) | ✅ 定稿:**ENPH 剔除**(纯户用逻辑=房贷利率+渠道库存,非相关噪音);NXT=Utility-scale 确定性第一;**FSLR 已按 2026-08-29 裁决补入**(美国最大 utility-scale 组件商,长历史,还能当本板块的截面锚);FLNC=电网级 BESS 纯度最高;CWEN/BEPC=YieldCo(持带 PPA 运营绿电,BEPC 2020-07 上市需门控) |
| 9 | `gas_midstream` | 天然气中游管道 | 最上游(燃料) | **KMI .35 / WMB .30 / OKE .20 / TRGP .15** | LNG | ✅ 2026-08-29 增设:气电燃料链此前是全链最大缺口("天然气是 DC 最直接基载");KMI/WMB 已签 DC 驱动的管道扩容;全员长历史 c-corp 无 K-1;与 power_price_proxy(Henry Hub)天然成边;LNG=Cheniere(出口逻辑,纯度低,只当 reserve) |
| 10 | `water_cooling` | 水务与冷却水 | 辅助基础设施 | **AWK .40 / WTRG .30 / AWR .15 / CWT .15** | SJW | ✅ 2026-08-29 增设:AWK 与高耗水数据中心冷却水绑定(用户参考材料第 1 块);本质防御性受监管水务(purity 低),给组合添第三个 RISK_OFF 防御位;全员长历史 |

- 共 **41 只加权成员 + 每板块 1 只 reserve(共 10)+ 候补名单(纯文档)**。
- 新增票 IPO 核查:KMI(2011)/WMB/OKE/TRGP/AWK/WTRG/AWR/CWT/SJW/LNG/HUBB/CAT/CMI 全长历史;
  **NVT 2018-05 分拆、ATKR 2016-06、BEPC 2020-07** 若入选需走 24 个月门控(候补票,不影响现役)。
  ⚠️ 逐行核验(2026-08-29)修正:AEUS 代码里 **reserve 是单票槽**(`universe.subsector_reserve`
  返回 Optional[str],loader 事故级联按单 reserve 写)—— 忠实镜像 = 每板块 1 个 reserve,
  多余候选降级为注释里的候补名单,不改级联机制。
- **三档→N 档泛化的真实改动面**(逐行核验):`stock_decompose.py:121` 已天然支持 N 档
  (超 3 档自动命名 tier4+)、`loader.build_subsector_prices` 遍历 tickers 列表 + primary
  锚(N 档兼容)、`effective_weights` 主体对 dict 长度无假设;**真正要改的只在 universe.py
  内部**:`_TIER_ORDER` 3 元组、`SubsectorMeta.tickers/weights`(3 元组)、`_meta`、
  `subsector_tickers/subsector_weights` 文档语义、`universe_as_dataframe` 的
  primary/backup1/backup2 三个硬列 —— 全部 N 档化,80/15/5 成为 3 档特例。
- `universe_start` 2019-01-01;`price_start` 2016-01-01(同 AEUS)。

### 2.2 基准 ✅ 定稿:哑铃型双基准 + 50/50 混合

角色映射(用户裁决):

| 角色 | 半导体(AEUS) | 电力(AEUS) | 含义 |
|---|---|---|---|
| 基座 Beta | XLU | **XLU** | "只要运行就要买电"的底层现金流 |
| 暴利 Alpha 卖铲人 | GRID | **GRID**(First Trust 智能电网) | 交期最长议价权最高的 capex 环节 |

- **win criterion(validate.py)**:Sharpe **且** CAGR 同时打败 XLU **和** GRID(严格双杀,同 AEUS 对 XLU/GRID)。
- **Active Return / IR 的基准**:**50% XLU + 50% GRID 日度再平衡混合曲线**(loader 合成,回测与 tearsheet 展示用)。
- GRID 含欧洲巨头(施耐德/西门子/ABB)→ 其成分股是后备 alpha 池;phase-1 不碰 ADR,列 §8 远期。
- SPY 仅展示。

### 2.3 成本分档(真源 = universe.STOCK_TIER;config costs.tier_* 仅文档同步)

| 档 | bps | 成员 |
|---|---|---|
| tier1 | 3 | NEE, SO, DUK, AEP, VST, CEG, NRG, ETN, EMR, GEV, PWR, VRT, TT, CARR, FSLR, KMI, WMB, OKE, AWK, CAT, CMI |
| tier2 | 5 | D, AEE, LNT, ATO, FIX, BWXT, NXT, TLN, STRL, TRGP, WTRG, HUBB, NVT, LNG |
| tier3 | 8 | OGE, BKH, AVA, POWL, AZZ, VMI, MYRG, DY, ACM, LEU, UUUU, OKLO, SMR, NXE, DNN, ORA, FLNC, ARRY, SHLS, CSIQ, BE, AOS, AWR, CWT, SJW, ATKR, CWEN, BEPC |

### 2.4 同时做哪几个板块 —— 逐字镜像 AEUS 的板块选择机制

用户问题"我们同时只做其中几个板块对吧,AEUS 怎么决定"——AEUS 的答案(已核验代码),AEUS 原样照搬:

1. **每月打分排序**:composite 4 因子给全部板块打 z 分(现在是 10 个而非 8 个,截面反而更厚);
2. **top_n 截断**:`portfolio.top_n_sectors = 3` 只取分数最高的 3 个,且 z < `min_zscore(-0.30)`
   的一票否决(哪怕排进前 3 也不要)——**任一时点只持有 ~3 个板块 ≈ 12-15 只股票**;
3. **优化器配权**:入选板块间用 inv_vol 分钱(单板块 ≤ max_weight 0.55);
4. **浓度参数不拍脑袋**:Group B 参数组扫 top_n ∈ {2,3,4,5} × max_weight {0.65/0.55/0.45/0.35},
   **由 walk-forward + smart_select 用数据决定当前该集中还是分散**——10 板块下 top_n=4/5 的组
   合法性更强,WF 会自己发现;
5. **调仓防抖**:z 分变化 < 0.5σ 的板块不动;月换手上限 80%;
6. **RISK_OFF 防御位**:regulated_mega / regional_utility / water_cooling 三个防御板块在
   risk_off regime 下获 +0.40 加成(AEUS 的 analog_defense 机制,defensive 名单见 §3.4)。

### 2.5 板块内动态配比(AEUS 对 AEUS 的核心机制扩展)⭐ 新设计

**AEUS 现状**:板块内权重是静态 80/15/5,只有 IPO 门控和事故级联会改它。
**AEUS 要求**(用户 2026-08-28):成员更多,且**内部配比按知识图谱动态调整**。

设计(叠加在既有机制之上,不推翻):

```
第 1 层  base_w            静态先验(§2.1 表),等价于 AEUS 的 80/15/5
第 2 层  effective_weights  IPO 24 个月门控 + stale 事故级联 + reserve 顶班 —— 逐字沿用
第 3 层  purity_tilt(新)   w_i ∝ eff_w_i × (1 + κ × purity_i × g_s(t))     再归一
```

- **purity_i ∈ [0,1]**:该股对"AI 电力"主题的纯度分,universe.py 静态维护(如 GEV=1.0,
  EMR=0.35,ETN=0.7,VRT=1.0,TT=0.5,NXT=0.9,ARRY=0.7,BE=0.8,NEE=0.6,SO=0.3 …
  完整表开发时定稿并写注释说明依据);
- **g_s(t)**:该子板块当月的 supply_chain 图谱分(composite 已算好的 z 分,**信号层现成产物,
  无新数据依赖,PIT 性质与调仓决策同源**——月末标签、次月初生效,不引入前视);
- **κ**:倾斜强度,默认 0.30,单票倾斜幅度截断在 ±40%;κ=0 时**逐 bit 退化为 AEUS 静态行为**
  (这是回归测试的锚:κ=0 必须与纯 effective_weights 输出完全一致);
- **生效位置(2026-08-29 逐行核验后修正——必须两层同步,否则回测测不到 κ)**:
  AEUS 里"篮子收益构造(loader.build_subsector_prices,回测的资产)"与"个股分解
  (stock_decompose,执行层)"用同一套 80/15/5,两层天然一致。AEUS 的倾斜若只进分解层,
  回测交易的还是 base_w 篮子 → **WF 根本无法裁决 κ**。因此:
  (a) `loader.build_subsector_prices` 的逐日权重矩阵乘同一 purity_tilt(g_s 用**上月末**
      图谱分,月度阶梯,PIT 与调仓同源);
  (b) `stock_decompose.decompose_to_stocks()` 增加可选参数 `graph_scores + κ`;
  两处调同一个纯函数 `apply_purity_tilt(eff_w, purity, g_s, κ)` 保证逐 bit 一致;
  子板块层(优化器输入)不受影响——动态配比只改"板块内怎么分",不改"板块间怎么配"。
- **κ 进参数网格**:AEUSStrategyRuns 新增 Group N(purity_tilt_off κ=0 / purity_tilt_03 /
  purity_tilt_05),由 walk-forward 用数据裁决而非拍脑袋——AEUS"39 组参数即战略资产"
  方法论的直接延伸。
- 典型效果:AI capex 走强 → grid_equipment 图谱分高 → GEV(purity 1.0)从 base .20 被
  放大至 ~.28,EMR(0.35)被压缩 —— 即用户所说"GEV 作为 2024.04 后的 alpha 放大器"。

### 2.6 业绩曲线:回测段拼接实盘段(逐条镜像 AEUS 机制)⭐ 2026-08-29 补

AEUS 的展示曲线不是纯回测也不是纯实盘,是**三段式拼接**,AEUS 全套学习模仿:

**AEUS 现状(已读源码 UpdateMasterPerformance.py + aeus_ssrs_splice_freeze.json 确认)**:

```
2025-11-11 ──────────────────► 2026-05-29 │ 2026-06-01(live_start)────► 今天
   固定回测段(frozen)                      │        实盘段(live)
   = 冻结 param(pure_momentum)            │  = portfolio_ledger 账户每日 equity
     × 冻结 vintage CSV                    │    的**日收益率**,链在固定段末值上:
     × 归一 scale 0.09132(对齐 master)    │    cur = bt_last × Π(1 + r_live)
   逐日数值直接冻进 splice_freeze.json     │  (无账本时回退 inventory-MTM,响亮告警)
```

三条来自 AEUS 的血泪教训(splice-freeze v2 的由来,2026-07-09):
1. **固定段曾每晚用"最新回测 CSV + auto-select 最优列"动态重建** → 参数 5 周换 5 次赢家,
   且都不是实际交易的那个;
2. **AdjClose 追溯调整**(KLAC 10:1 拆股、ARM/LRCX/WDC/INTC 分红触发 Polygon 全历史
   refetch)→ 同一 param 同一历史日漂移 +134%,回测不可复现;
3. 修法:把固定段**钉死到真实交易的 param + 当日 vintage CSV,且把逐日数值字面量
   冻进 JSON**(`fixed_segment` 字典,137 天),往后即使回测 CSV 漂移,固定段纹丝不动。
   拼接点之前 = 冻结;拼接点之后 = 账本真实持仓。

**AEUS 镜像方案**:

| 机制件 | AEUS 实现 | AEUS 对应 |
|---|---|---|
| 展示曲线起点 | 2025-11-11(master 归一起点,scale 对齐 MRPT+MTFS 合并起点) | AEUS 上线时由 master 脚本同款逻辑确定(D 级接线时定) |
| live_start(拼接日) | **2026-06-01 = 第一天持仓日**;`AEUS_LIVE_START` 常量 | **AEUS 第一天真实建仓日**(go-live 时定死,写成 `AEUS_LIVE_START`);⚠️ 拼接日选错=整条曲线作废,上线 checklist 单列 |
| 固定段来源 | `aeus_batch_equity_<vintage>.csv` 的 frozen_param 列 | `aeus_batch_equity_<vintage>.csv` 同款;**上线当天就冻结**(AEUS 是吃了 5 周亏才冻的,AEUS 直接抄修复后的做法,不重走弯路) |
| 冻结元数据 | `aeus_ssrs_splice_freeze.json`(前端 public/data/,含 frozen_param / frozen_vintage_csv / live_start / scale / fixed_segment 逐日字面量) | phase-1:本目录 `backtest_results/aeus_splice_freeze.json`(同 schema);D 级接线时并入前端那份(加 "aeus" 键,add-only) |
| live 段取数 | 优先 `_load_account_equity('aeus')`(account_history/ 每日 equity,含分红/费用/复利)→ `_chain_account_live` 日收益率链接;无账本回退 inventory-MTM + 响亮告警 | 同款双层:portfolio_ledger 挂 aeus 后走账本;phase-1 账本未接时只有 inventory-MTM 路径(告警属预期) |
| 固定段防漂移 | `_freeze_backtest_segment`:每次跑 master 都用冻结元数据逐日覆写固定段 | 同款逻辑,镜像进 AEUS 的 D 级接线 |
| 调仓节奏 | **生产历史上全是 V1(月度,每月首个交易日)**;V2(半月)只活在研究/daily_backtest 双轨选参里,daily_backtest.sh 四步后强制 restore V1 | 完全一致:V1 生产、V2 研究、restore-V1 机制照搬 |
| pairs 的另一半先例 | pairs 冻结在 strategy_performance.json 内部(UpdateStrategyPerformance 只改 --start 之后的行,3/19 拼接) | 仅作认知参照;AEUS 走 AEUS/SSRS 这条 master 拼接路线 |

**AEUS 上线时间线(镜像 AEUS 的 2025-11-11 → 2026-06-01 结构)**:
1. D6 完成后,选参链产出 `selected_param_set.json` + batch equity CSV;
2. go-live 前一日:用**实际要交易的 param** + 当日 vintage CSV 生成固定段,起点=master
   归一起点,终点=建仓前一交易日,逐日冻入 `aeus_splice_freeze.json`;
3. 建仓日(= AEUS_LIVE_START):account_aeus.json 以 **$1,000,000** 起账(✅ 已裁决,
   同 AEUS C0=1M;注意 master 归一后固定段起点显示值≈$921,498.79 是 scale 对齐的结果,
   不是初始资本,勿混淆——AEUS 冻结 JSON 里首日就是这个数);
4. 之后每月 V1 调仓,live 段逐日由账本收益率延伸,固定段永不再动。



---

## 3. 知识图谱设计(supply_chain 的电力版)⚠️ 核心设计,需确认

### 3.0 与本仓库既有 BDC「知识图谱」的关系

AEUS 的图谱 = supply_chain.py 的 **SUPPLY_CHAIN_GRAPH**(子板块传导图)。用户所说"aeus 相关知识图谱同样镜像"即指此 + graph_calibration 校准链 + config graph_config,全部在本目录内,**无目录外图谱设施**(已全仓 grep 确认)。

### 3.1 外部节点(特殊源,对应 AEUS 的三个 proxy)

| 节点 | AEUS 对应 | 数据源 | 说明 |
|---|---|---|---|
| `ai_dc_capex_proxy` | ai_capex_proxy | **原样复用** capex_pulse(MSFT/GOOGL/META/AMZN 3M 动量 z)+ hyperscaler_capex_actual(SEC XBRL 真实季度 capex) | 最上游驱动,两策略共享 |
| `power_price_proxy` | (新增) | FRED `DHHNGSP`(Henry Hub 天然气日价)z-score | 电价/毛差代理;IPP 直接受益 |
| `rate_env_proxy` | (新增) | macro store 已有 `DGS10` z-score **取负** | 利率环境;受监管公用是债券代理,加息杀估值 |
| `industrial_demand_proxy` | pmi_proxy | FRED `IPUTIL`(公用事业工业生产指数)YoY z | 传统用电需求 |

### 3.2 边(v1 先验图,lag 单位=月;上线前必跑 graph_calibration 实证校正)

```yaml
edges:
  # AI capex 沿链传导(由快到慢)
  - {source: ai_dc_capex_proxy, target: ipp_wholesale,      weight: 1.0,  lag_months: 0, desc: "PPA 签约最快兑现——CEG/VST 微软亚马逊合同"}
  - {source: ai_dc_capex_proxy, target: dc_power_cooling,   weight: 0.9,  lag_months: 0, desc: "VRT 电源/液冷订单与 DC 建设同步"}
  - {source: ai_dc_capex_proxy, target: grid_equipment,     weight: 0.8,  lag_months: 3, desc: "变压器/开关柜订单滞后于 DC 决策"}
  - {source: ai_dc_capex_proxy, target: grid_epc,           weight: 0.7,  lag_months: 5, desc: "接网/变电站施工再滞后"}
  - {source: ai_dc_capex_proxy, target: regulated_mega,     weight: 0.6,  lag_months: 9, desc: "负荷增长→rate base 提升最慢"}
  - {source: ai_dc_capex_proxy, target: nuclear_fuel,       weight: 0.5,  lag_months: 6, desc: "核电 PPA/SMR 订单(MSFT-CEG 式交易)"}
  - {source: ai_dc_capex_proxy, target: renewables_storage, weight: 0.5,  lag_months: 3, desc: "绿色算力承诺→电网级储能/光伏"}
  # 链内传导
  - {source: ipp_wholesale,     target: nuclear_fuel,       weight: 0.5,  lag_months: 3, desc: "核电溢价传导至燃料链"}
  - {source: grid_equipment,    target: grid_epc,           weight: 0.6,  lag_months: 2, desc: "设备交付先于施工"}
  - {source: regulated_mega,    target: grid_equipment,     weight: 0.5,  lag_months: 4, desc: "公用事业 capex 周期→设备订单"}
  # 宏观节点
  - {source: power_price_proxy, target: ipp_wholesale,      weight: 0.7,  lag_months: 0, desc: "批发电价→商户电厂毛差"}
  - {source: rate_env_proxy,    target: regulated_mega,     weight: 0.6,  lag_months: 0, desc: "利率(取负后)→债券代理估值"}
  - {source: rate_env_proxy,    target: regional_utility,   weight: 0.7,  lag_months: 0, desc: "同上,中小盘弹性更大"}
  - {source: rate_env_proxy,    target: renewables_storage, weight: 0.5,  lag_months: 2, desc: "融资成本→绿电项目 IRR"}
  - {source: industrial_demand_proxy, target: regional_utility, weight: 0.5, lag_months: 2, desc: "工业用电→区域售电量"}
  # 2026-08-29 扩容板块(第 9/10)
  - {source: power_price_proxy,  target: gas_midstream,     weight: 0.7,  lag_months: 0, desc: "气价→管输/加工价差与产量"}
  - {source: ai_dc_capex_proxy,  target: gas_midstream,     weight: 0.5,  lag_months: 4, desc: "DC 驱动管道扩容(KMI/WMB 已签约)"}
  - {source: gas_midstream,      target: ipp_wholesale,     weight: 0.4,  lag_months: 1, desc: "燃料可得性→气电出力"}
  - {source: ai_dc_capex_proxy,  target: water_cooling,     weight: 0.3,  lag_months: 6, desc: "DC 冷却水需求(慢变量)"}
  - {source: rate_env_proxy,     target: water_cooling,     weight: 0.7,  lag_months: 0, desc: "利率(取负)→水务债券代理,弹性最大"}
```
(共 21 条边;AEUS V1 11 条/V2 14 条 —— 图更大是板块更多的自然结果,校准与 0.05 IC 门槛照旧)

### 3.3 校准流程(照搬 AEUS)

`graph_calibration.py` 机制不变:截面去均值的**因子残差**收益上扫 lag 0-12 的 IC,`KEEP_IC_THRESHOLD=0.05`,输出 v2 边贴回 config。候选边池(对应 AEUS 的 logic_cpu 三条入边)预置:`power_price→regional_utility`、`grid_epc→regulated_mega`(反哺)、`industrial_demand→ipp_wholesale`。

### 3.4 composite 层的板块属性(composite.py 硬编码字典的重写)

```python
CAPEX_BETA = {   # 对 AI capex 的敏感度(对应 AEUS 同名字典)
    "ipp_wholesale": 1.0, "dc_power_cooling": 0.9, "grid_equipment": 0.7,
    "grid_epc": 0.6, "nuclear_fuel": 0.5, "gas_midstream": 0.4,
    "renewables_storage": 0.4, "regulated_mega": 0.1,
    "regional_utility": -0.2, "water_cooling": -0.3,
}
DEFENSIVE_TICKERS = ["regulated_mega", "regional_utility", "water_cooling"]  # RISK_OFF +0.40 加成
AI_CYCLE_SUBSECTORS = ["ipp_wholesale", "dc_power_cooling", "grid_equipment"]  # 高β组
```
regime 4 状态机、权重乘数矩阵、momentum(12-1, z36)、risk_overlay:**逐字保留**(板块无关)。

---

## 4. 数据层映射(每个 AEUS PIT 源 → AEUS 等价物)

| AEUS 源 | 文件 | AEUS 等价物 | 数据源/标识符 | PIT 键 |
|---|---|---|---|---|
| capex_pulse(4 巨头价格动量) | company/capex_pulse.json | **原样复用**(同 4 票) | yfinance MSFT/GOOGL/META/AMZN | 当日 |
| hyperscaler_capex_actual | company/hyperscaler_capex_actual.json | **原样复用**(同 CIK 同 concept 同去累计逻辑) | SEC XBRL PaymentsToAcquirePPE / ProductiveAssets | filed |
| MU DIO(存货/COGS) | company/mu_dio_proxy.json | **utility_capex_proxy**:NEE/DUK/SO 季度 capex YoY(rate base 增速) | SEC XBRL `PaymentsToAcquirePropertyPlantAndEquipment`,CIK: NEE=753308, DUK=1326160, SO=92122(**开发时逐一核验**) | filed |
| ASML guidance(6-K 解析) | industry/asml_quarterly_guidance.json | **backlog_rpo**:GEV/VRT/PWR 在手订单 | SEC XBRL `RevenueRemainingPerformanceObligation`(10-Q,标准 tag,**不用啃 HTML,比 ASML 版更稳**);CIK: GEV=1996810, VRT=1674910, PWR=1050915(核验) | filed |
| TSMC 月度营收(TWSE) | industry/tsmc_monthly_revenue.json | **elec_gen_monthly**:美国月度售电量/发电量 YoY | ✅ **EIA v2**(`EIA_API_KEY` 已在根 .env 确认存在;`prediction_market_macro/ingest/eia.py` 有打通的 v2 模式可照抄:length≤5000 分页、facet 无 .W 后缀、PIT knowledge_time、`price_data/eia/` 原始镜像)。路由:`electricity/retail-sales`(月度售电量+电价,分部门)为主,`electricity/electric-power-operational-data`(月度净发电)为辅;EPM 发布≈月+2 月的 25 日,PIT 滞后按发布日历 | EPM 发布日 |
| DRAM spot proxy(MU/XLU RS) | industry/dram_spot_proxy.json | **gas_price_proxy**:Henry Hub 日价 z(252d);备选 VST/XLU RS | FRED `DHHNGSP`;另 EIA 周度天然气库存(`NG_STORAGE_WEEKLY`)**已由 macro 模块每日镜像到 `price_data/eia/`,直接只读复用零成本** | 当日/周四 10:30 ET |
| PMI(IPMAN YoY) | industry/pmi_series.json | `IPUTIL` YoY(公用事业 IP) | FRED `IPUTIL`,release lag 20d 同款 | release |
| hiring alt-data | altdata/fred_altdata.json | Indeed 建筑/安装类目(电网施工用工) | FRED `IHLIDXUSTPCONS`(construction,**开发时核验系列 ID**);无则砍掉该信号(graceful 0) | +7d |
| semi_ip/ppi/electronics_no | 同上 | `IPUTIL` / `PCU2211--2211--`(电力 PPI,核验)/ 砍 | FRED | +45d |
| 韩台出口 | 同上 | EIA 全美/分区售电需求 YoY(key 已有,做) | EIA v2 `electricity/retail-sales` 分州聚合 | EPM 发布日 |
| GPU 云价格 | altdata/gpu_pricing_history.parquet | **原样保留**(AI 需求代理对电力同样有效,forward-only 继续积累) | computeprices.com | 当日 |
| 宏观 store | price_data/macro/(只读) | **原样复用**(vix/hy_spread/DGS10/IPUTIL 全在 FRED 域) | MacroStateStore | — |
| (新板块预备)gas 链确认序列 | — | **elec_gas_burn**:电力部门天然气消耗量月度 YoY(gas_midstream→ipp 边的确认 tilt,对应 TSMC 营收确认 foundry 边的角色) | EIA v2 `natural-gas/cons/sum`(电力部门交付量);key 已有;`NG_STORAGE_WEEKLY` 周度库存已由 macro 模块每日镜像,直接只读 | EIA 发布日 |
| (新板块预备)水务 capex | — | **water_capex_proxy**:AWK/WTRG 季度 capex YoY(与 utility_capex_proxy 同引擎同 concept,只是换 CIK) | SEC XBRL PaymentsToAcquirePPE;CIK 开发时核验 | filed |

数据落盘全部到新根 **`price_data/elec_strategy/{prices,industry,company,altdata,cache}`**(add-only 新建,对应 elec_strategy)。merge_frozen、staleness(5td/45d/120d)、SEC 限速 0.15s、`AEUS_SEC_USER_AGENT` 环境变量,全部照搬。

### 4.1 电力专属 altdata 全谱(2026-08-29 用户追加:发电量/需求/装机/季节性/电价/地区价格;**决策挂接见 §4.2**)

> 原则:全部走 **EIA v2(key 已有)+ FRED(key 已有)**,零新增供应商;取数模式照抄
> `prediction_market_macro/ingest/eia.py`(分页/limit 5000/PIT knowledge_time)+ AEUS 的
> merge_frozen 只追加落盘。每条都标 PIT 发布滞后与消费方(不做"取了没人用"的数据)。

| # | 信号 | 源/路由 | 频率 | PIT 滞后 | 消费方(图谱边/因子) |
|---|---|---|---|---|---|
| A1 | **elec_demand_daily** 全美+分区用电需求 | EIA `electricity/rto/region-data`(frequency=daily,respondent=US48 + TEX/MIDA/CAL 等 DC 重镇区域) | 日 | ~2-3 天 | 新外部节点 `power_demand_proxy`:YoY z 后喂 `→ipp_wholesale(0)` `→regulated_mega(1)` `→regional_utility(1)` 三条边 —— **电力版的"终端需求脉冲"**,频率远高于 AEUS 任何需求代理 |
| A2 | **elec_gen_monthly** 分燃料发电量 | EIA `electricity/electric-power-operational-data`(月度,分 fuel:gas/nuclear/solar/wind/coal) | 月 | EPM ≈55 天 | 主 industry 信号(§4 主表已列);分燃料切片另供:gas 发电份额→gas_midstream 确认,nuclear 出力→nuclear_fuel 确认 |
| A3 | **retail_price_regional** 分州零售电价+售电量 | EIA `electricity/retail-sales`(月度,by state × sector) | 月 | EPM ≈55 天 | 两用:①全美电价 YoY z=电价通胀脉冲(ipp 毛差确认);②**DC 重镇州(VA/GA/TX/AZ/OH)电价相对全美的溢价差**=AI 负荷压力的地区性证据 → regional_utility 确认 tilt |
| A4 | **installed_capacity** 装机容量分来源 | EIA `electricity/operating-generator-capacity`(月度 EIA-860M,by energy source × state) | 月 | ≈60 天 | 装机增速 YoY:solar+battery 新增→renewables_storage 确认(NXT/FLNC 订单的兑现证据);gas 新增→gas_midstream/ipp;**总装机增速 vs 需求增速的缺口 = 全链"缺电度"标量**,可作 supply_chain 因子的全局强度乘数(设计上等价 AEUS 的 AI demand cycle 放大器,复用其 graceful-1.0 机制) |
| A5 | **degree_days** 制冷/制热度日 | EIA `steo`(STEO 月度,CDD/HDD 历史+预测) | 月 | 发布 ≈ 月 6-12 日 | **季节性引擎**:①需求信号的"去天气"版=实际需求 − CDD/HDD 回归拟合值,剩余=结构性(AI)需求——这是把 AI 负荷从天气噪音里剥出来的关键;②夏季制冷峰值前瞻(STEO 含预测值,PIT 用发布日) |
| A6 | **elec_gas_burn** 电力部门气耗 | EIA `natural-gas/cons/sum`(月度) | 月 | ≈60 天 | gas_midstream→ipp 边确认(§4 主表已列) |
| A7 | **ng_storage_weekly** 气库存 | `price_data/eia/` **macro 模块每日已镜像,只读复用** | 周 | 周四 10:30 ET | gas_price_proxy 的库存维度(偏离 5 年均值 z) |
| A8 | **电价通胀双系列** | FRED `CUSR0000SEHF01`(CPI 电力)+ `PCU2211--2211--`(电力 PPI,ID 开发时核验) | 月 | ≈45 天 | 电价趋势的独立第二源,与 A3 互证;PPI 领先 CPI |
| A9 | **IPUTIL** 公用事业 IP | FRED(已在主表) | 月 | ≈20 天 | industrial_demand_proxy |

**季节性处理纪律**(电力数据与半导体最大的不同,单独立规):
1. 所有月度量类序列(需求/发电/气耗)**一律先 YoY 再 z**——YoY 天然消灭季节周期,与 AEUS
   对 semi_ip/korea_exports 的处理完全同构,不引入季调模型依赖;
2. 日频需求(A1)额外做**52 周同期对比**(vs 去年同一周均值)避免周内/节假日噪音;
3. A5 度日数据专门用于"去天气":`结构性需求 = 实际需求 − f(CDD,HDD)` 的残差(f=滚动 5 年
   线性拟合,只用截至 as-of 的数据,PIT 干净)——**AI 负荷信号的本体**;
4. 电价的地区溢价(A3-②)用"DC 州均值 − 全美均值"的差分序列,天然去掉全国性季节因子。

**落盘与刷新**:
- `price_data/elec_strategy/altdata/eia_{route_name}.json`(原始 payload 快照,照抄 macro
  模块 RAW MIRROR 两层法)+ merge_frozen 的 PIT 信号序列;
- `aeus_pipeline.sh update_data` 从 AEUS 的 7 步扩为 **9 步**:prices / capex(复用) /
  utility_capex(XBRL) / backlog_rpo(XBRL) / eia_monthly(A2+A3+A4+A6 一次批量) /
  eia_daily_demand(A1) / steo_dd(A5) / gas_price(FRED) / fred_altdata(A8+A9);
  每步独立失败不阻断(照搬 AEUS run_update_data 的 WARN-and-continue);
- EIA 限速:官方 5000 行/请求 + 无严格 QPS,但自律 0.5s/请求;9 步全量日增量 <30 请求;
- 全部信号走 graceful-0/graceful-1.0 回退(AEUS 机制):任一 EIA 路由断供,对应确认 tilt
  归零、主链(价格动量)不受影响。

### 4.3 ERCOT Data Access Portal 深挖(2026-08-30;116 报告过筛,凭证已入 .env)

ERCOT Public API 已注册(订阅 key 在根 .env,`ERCOT_API_*` 四变量;密码占位待用户补)。
116 个公开报告按"能否挂进 §4.2 四通路"过筛,选 7 类(product ID 于 D3 从 API 目录钉死):

| 报告族 | 频率 | 独家价值(vs EIA) | 通路挂接 |
|---|---|---|---|
| DAM/RTM Settlement Point Prices | 日 | 得州枢纽电价,快 EIA 零售价 55 天 | ②→ipp_wholesale(hub_power_price 的 ERCOT 腿,与 PJM West 并列) |
| DAM Ancillary Services 价格 | 日 | **AS 价格=电网紧张度最灵敏温度计**,领先能量价格;EIA 无此维度 | ②→ipp + 并入缺电度③ |
| Fuel Mix / Real Time Gen | 日 | 实时分燃料出力,快 EIA 月度 2 个月 | ②确认提速:gas_midstream/renewables_storage/nuclear_fuel |
| Actual System Load + 7-Day Forecast(分天气区) | 日 | TX 需求实测+官方预测;West TX 区可单独跟踪 DC/矿场负荷 | ①power_demand_proxy 的 TX 分量 |
| Wind/Solar Production(实际+预测) | 日 | 绿电兑现率(出力/装机)=NXT/FLNC 订单转化证据 | ②→renewables_storage |
| Unplanned Resource Outages / PRC | 日 | 供给侧紧张度,与 AS 价格互证 | 并入缺电度③ |
| Large Load Interconnection 队列 | 月 | AI 负荷排队接电第一现场(已在 §4.2) | ①/② |

筛掉:60-Day 逐资源披露(细+滞后)、CRR/拥塞权、全节点价格、2-Day bids/offers 曲线
(市场微观结构,月度策略用不上)。

**纪律重申**:ERCOT 日频数据只在月度采样点进决策 —— 价值是采样零滞后 + z 分布更稳,
不做日内触发(四通路纪律不变)。

**对 AEUS 的外溢(只报告不实施)**:TX 大负荷队列 + West TX 需求 = hyperscaler capex 的
落地兑现证据,可作 AEUS ai_capex_proxy 的确认信号;AEUS 是在产系统,待 AEUS 验证有效后
由用户单独立项决定。

### 4.2 altdata → 持仓改变的精确通路(2026-08-29;每路信号必须挂进决策,挂不上就删)

**AEUS 里外部数据改变持仓的通路只有 4 条(已核验代码),AEUS 不发明第 5 条**:

```
通路① 图谱节点源     节点月度序列 × 边权 × lag → 板块传导分 → 截面 z → 占 composite 35%
                     → 改变板块排名 → 改变 top_3 选谁      【选板块】
通路② 确认 tilt      +0.30 × z(外部 PIT 序列) 加到特定板块的 supply_chain 分上
                     (AEUS 的 TSMC/ASML/DRAM/MU-DIO 同款) 【调分数】
通路③ 敞口放大器     标量 → 总仓位乘数(AEUS load_ai_demand_cycle Path A,缺数=1.0)
                     【调 gross:现金 vs 持仓】
通路④ purity_tilt    板块图谱分 g_s(t)(含①②的结果)→ 板块内成员权重倾斜(§2.5)
                     【板块内谁多谁少】——①②算出来的分自动流进④,无需另接
```

**九路信号的逐一挂接**(变换全部照 AEUS 惯例:月度 YoY → 36 个月滚动 z):

| 信号 | 通路 | 精确定义 | 对持仓的作用 |
|---|---|---|---|
| A1 需求(日频) | **①节点** | `power_demand_proxy = z36(去天气结构性需求 YoY)`,日频月末重采样(日频价值=月末采样时滞后仅 2-3 天,AEUS 的月度代理要等 45 天) | 喂 3 条边(→ipp 0 lag/→regulated 1/→regional 1);需求走强→这三个板块传导分升→更可能进 top_3 |
| A5 度日 | **A1 的预处理** | `结构性需求 = 实际需求 − f(CDD,HDD)`(滚动 5 年拟合,as-of 前数据) | 不直接进决策——它决定 A1 的质量:热浪导致的用电涨不改变持仓,天气解释不了的涨(=DC 负荷)才改变 |
| A2-gas 份额 | **②tilt** | `+0.30 × z36(气电发电量 YoY)` → gas_midstream 分 | 气电出力实增→gas_midstream 排名升 |
| A2-核电出力 | **②tilt** | `+0.30 × z36(核电发电量 YoY)` → nuclear_fuel 分 | 同上逻辑 |
| A3-①全美电价 + A8 双系列 | **②tilt**(三源合一) | `电价脉冲 = mean_z(零售价 YoY, CPI电力 YoY, PPI电力 YoY)`(z 空间平均,照抄 _asml_tilt 拼接法)→ `+0.30` → ipp_wholesale 分 | 电价通胀→商户电厂毛差扩→ipp 排名升;三源平均抗单源噪音,PPI 领先性天然前置 |
| A3-②DC 州溢价 | **②tilt** | `+0.30 × z36(mean(VA/GA/TX/AZ/OH 电价) − 全美均价 的差分序列)` → regional_utility 分 | AI 负荷压出地区性电价溢价→区域公用排名升 |
| A4 装机 vs 需求缺口 | **③放大器** | `缺电度 = z36(需求 YoY − 总装机 YoY)`;`敞口乘数 E = clip(1 + 0.10×缺电度, 0.85, 1.15)` | **全链逻辑**:缺电=整条链都受益→少留现金;过剩=全链毛差承压→多留现金。改的是 gross,不改选谁;缺数=1.0(照抄 AEUS graceful) |
| A4-solar/battery 切片 | **②tilt** | `+0.30 × z36(solar+battery 新增装机 YoY)` → renewables_storage 分 | NXT/FLNC 订单的**兑现证据**(装机=交付完成) |
| A6 气耗 + A7 库存 | **②tilt**(合成) | `gas 链确认 = 0.6×z(电力部门气耗 YoY) − 0.4×z(库存对 5 年均值偏离)`(库存高=利空)→ `+0.30` → gas_midstream 分 | 与 A2-gas 份额并列的第二确认;两者 z 空间平均后再乘 0.30(单板块 tilt 总量不超 AEUS 惯例) |

**一个完整的传导实例**(需求激增月):
```
7 月:热浪 + DC 上量 → A1 原始需求 +8% YoY
  A5 去天气:CDD 拟合解释 +5%,残差 +3% = 结构性 → power_demand_proxy z = +1.8
  ① ipp/regulated/regional 三板块传导分各 +1.8×边权
  ② A3-①电价脉冲 z +1.2 → ipp 再 +0.36;A2 气电份额 z +0.9 → gas_midstream +0.27
  ③ 装机没跟上:缺电度 z +1.5 → E = 1.15,现金压到下限
  → 月末打分:ipp_wholesale 从第 4 升到第 2 → 调仓日换入
  ④ ipp 的 g_s 分高企 → 板块内 VST(purity .95)从 base .40 放大到 ~.47,NRG 被压
```

**纪律(明确不做的事,防止数据多了手痒)**:
- ❌ 不新增任何**日内/日频触发器**——EIA 数据只在月度调仓采样点进决策(AEUS 的应急层只有
  VIX/DD/事件风险三种,保持原样);A1 日频的价值是**降低采样时的发布滞后**,不是天天交易;
- ❌ 不进 regime 判定——regime 仍用 VIX/HY/收益率曲线/ISM(宏观域),电力数据是行业域;
- ❌ 单板块确认 tilt 总权重不超 0.30×2 条(AEUS memory_hbm 有 DRAM+MU-DIO 两条的先例,
  照此封顶);
- ✅ 每条 tilt 进 **Group F 参数组**(external_on/off,AEUS 现成)——外部数据整体有没有用,
  由 walk-forward 裁决,而不是想当然。

**历史深度核查(能否支撑 z36 + 2019 回测起点)**:retail-sales/operational-data 2001+ ✓、
rto 日频需求 **2015-07 起** ✓(2019 回测起点时已有 42 个月,z36 刚好够)、860M 2015+ ✓、
STEO 度日更长 ✓ —— 全部满足;唯一薄的是 rto 分区序列早年口径变动,D3 时逐区核验。



---

## 5. 逐文件改造清单(复制目录内 45 个源文件,每个都有裁决)

图例:🟢 逐字保留(仅 import 路径/日志名) | 🟡 参数级修改 | 🔴 实质重写 | ⚫ 删除/清空

### 5.1 根目录

| 文件 | 裁决 | 具体改动 |
|---|---|---|
| `AEUSdailySignal.py` → `AEUSdailySignal.py` | 🟡 | 20 个函数逻辑全保;改:文件/logger 名、`inventory_aeus`→`inventory_aeus`、`account_aeus`→`account_aeus`、报表前缀 `aeus_daily_report_`→`aeus_daily_report_`、三个 scope 串 'aeus'→'aeus'。⚠️ 核验修正:**无需加任何旁路开关** —— CorporateActions(:912)/ledger 复利 sizing(:930)/ledger daily_update(:1610)三处调用**已是 try/except 失败降级不阻断**,C 级接线前用 'aeus' 调会自动响亮降级(名义 capital 模式),这本身就是 AEUS 的既有行为;EventRiskDetector 走 config `risk.event_derisk.enabled: false` 即可 |
| `AEUSBatchRun.py` → `AEUSBatchRun.py` | 🟡 | 输出名 `aeus_batch_*`→`aeus_batch_*`;historical_runs 输出改 `historical_runs/electric_utilities_strategy/`(§7B);MCPS/MacroStateStore/SimilarityEngine 只读 import 原样;batch equity CSV 是 §2.6 固定段的 vintage 来源,命名保持 `aeus_batch_equity_<ts>.csv` 以兼容 master 脚本 glob 模式 |
| `AEUSStrategyRuns.py` → `AEUSStrategyRuns.py` | 🟡 | 39 组参数(已实测 len(PARAM_SETS)=39;此前"59"为读码誤报)全部基于因子名与通用旋钮——**逐字保留**;**新增 Group N**:purity_tilt_off(κ=0)/ purity_tilt_03 / purity_tilt_05(§2.5 动态配比强度,交给 WF 裁决) |
| `walk_forward.py` | 🟢 | 折叠方案(IS≥3y/OOS 6m/step 10td/embargo 5td/anchored+rolling)、DSR/WFE/oracle 全保;仅 import 名 |
| `smart_select.py` | 🟢 | P2/P3/P5 三层、防抖(3-5 天/月限)、23 维质量门全保;log 前缀 AEUS |
| `macro_clusters.py` | 🟢 | encoder 持久化链原样;产物落本目录 backtest_results/ |
| `multi_horizon_backtest.py` | 🟢 | 4 horizon 权重不变 |
| `weekly_review.py` | 🟡 | 6 步全保;`inventory_aeus.json` 路径改名 |
| `validate.py` | 🟡 | **XLU/GRID → XLU/GRID**(双杀标准保留;核验:硬编码在 :75/:80/:98/:110 四处 + 模块 docstring,逐处换);另报 50/50 混合基准的 active return/IR |
| `portfolio_record.py` | 🟡 | 26/5/5-sheet 结构全保;文件名前缀 `aeus_portfolio_`→`aeus_portfolio_`、`monitor_aeus_`→`monitor_aeus_`、`wf_diagnostic_aeus_`→`wf_diagnostic_aeus_` |
| `stock_decompose.py` | 🟡 | 既有分解逻辑保留;**新增第 3 层 purity_tilt**(§2.5):可选参数 `graph_scores, kappa`,κ=0 逐 bit 等价旧行为(回归锚) |
| `config.yaml` | 🔴 | universe 全重写(§2)、graph_config 全重写(§3)、costs 分档(§2.3)、benchmarks XLU/GRID/SPY + 50/50 混合基准开关、universe.purity 表与 κ 默认值、data 路径 `../../price_data/elec_strategy/*`、report.pdf_filename `aeus_tearsheet.pdf`;**signals 权重/regime 阈值/portfolio/rebalance/risk/stop_loss 初值照抄 AEUS**(电力波动低于半导体,vol_target 30% 等靠 39 组参数网格自己选,不拍脑袋改) |
| `aeus_pipeline.sh` → `aeus_pipeline.sh` | 🟡 | 15 模式全保;模块路径 `electric_utilities_strategy`→`electric_utilities_strategy`、日志前缀 `aeus_`→`aeus_`、update_data 从 7 步扩为 9 步(§4.1 落盘与刷新);⚠️ 核验补漏:daily 模式 step2(:169)调**根目录 RefreshEventRiskData.py**(刷新半导体事件 store)—— phase-1 注释掉此步(留 TODO),C 级电力版事件 store 接好后恢复 |
| `daily_backtest.sh` | 🟡 | V1/V2 四步+幂等门全保;改名同上 |
| `README.md` / `RUNBOOK.md` | 🔴 | 按 AEUS 重写(结构模板照抄) |
| `DELETED_FILES.md` | ⚫ | 删(AEUS 历史包袱记录) |
| `.Rhistory` / `.DS_Store` | ⚫ | 删 |
| `__init__.py` | 🟢 | 保留 |

### 5.2 状态/产物文件(复制来的 AEUS 实盘状态 = 污染源,必须重置)

| 文件/目录 | 处置 |
|---|---|
| `account_aeus.json` → `account_aeus.json` | **重置**:initial_cash 待定(建议 $1,000,000 同 AEUS C0),positions 清空 |
| `inventory_aeus.json` → `inventory_aeus.json` | **重置**为空库存结构 |
| `trade_ledger_aeus.jsonl` / `_seed.jsonl` | **删**,AEUS 上线时新建 seed |
| `selected_param_set.json` | **删**(首次 `--select` 重新生成) |
| `account_history/` `inventory_history/` | **清空** |
| `trading_signals/`(实测 162 个日报 + risk_management 252 个 = 414+ 文件) | **清空**(保目录) |
| `backtest_results/`(164M) `mlruns/`(565M) `logs/`(321M) `report/output`(79M) | **清空**(保目录骨架);共释放 ~1.1G |
| `pipeline_state/` `__pycache__/` | 清空 |
| `notebooks/` 3 本 | 保留作模板,内容后续按 AEUS 重跑 |

### 5.3 data/

| 文件 | 裁决 | 具体改动 |
|---|---|---|
| `universe.py` | 🔴 | SUBSECTORS 改为"base_w 向量 + purity 分"双字典(§2.1/§2.5);IPO_DATES(GEV 2024-04-02、CEG 2022-02-02、VRT 2020-02-10、OKLO 2024-05-10、SMR 2022-05、NXT 2023-02-09、FLNC 2021-10-28、TLN 2023-07、ETN/EMR/PWR/NEE 等长史票不需——**开发时逐一从价格数据核实首日**);STOCK_TIER、cycle_link 元数据、显示名;`effective_weights`/`build_subsector_prices` 机制函数保留,仅从三档泛化为 base_w 向量(80/15/5 成为特例) |
| `aeus_fetch_prices.py` → `aeus_fetch_prices.py` | 🟡 | Polygon+yfinance 回退、股息回乘、_fetch_meta 全保;store 根改 elec_strategy;ticker 清单自动来自 universe |
| `aeus_fetch_sec_data.py` → `aeus_fetch_sec_data.py` | 🟡 | HTTP 层/限速/companyfacts/submissions 全保;fallback CIK 表换成 AEUS 名单(§4);UA 环境变量改 `AEUS_SEC_USER_AGENT` |
| `aeus_pit.py` → `aeus_pit.py` | 🟢 | merge_frozen/staleness/ROC 日期(删 ROC,台湾特有)——其余逐字 |
| `company_signals.py` | 🟡 | capex_pulse + hyperscaler_capex **原样**;mu_dio 整段替换为 utility_capex_proxy(复用 `_standalone_quarters` 去累计引擎,换 CIK+concept) |
| `industry_signals.py` | 🔴 | tsmc→elec_gen_monthly(FRED IPUTIL 起步)、asml 两件→backlog_rpo(XBRL RPO,弃 HTML 解析)、dram→gas_price_proxy、pmi→IPUTIL;CLI 子命令名同步(`--check-backlog` 等) |
| `altdata_signals.py` | 🟡 | FRED 系列表替换(§4);gpu_pricing 原样;信号缺失 graceful-0 机制保留 |
| `loader.py` | 🟢 | 全部由 config 驱动,仅宏观系列映射核对 |
| `cache/` | 🟢 | .gitignore/.gitkeep 保留 |

### 5.4 signals/

| 文件 | 裁决 | 具体改动 |
|---|---|---|
| `composite.py` | 🟡 | 7 步流程/权重/regime 乘数矩阵逐字;重写三个字典:CAPEX_BETA / DEFENSIVE_TICKERS / (从 universe 引的 AI_CYCLE);`_load_aux_signals` 的外部序列映射到 §4 新源 |
| `supply_chain.py` | 🔴 | 传导数学(`_lagged_with_decay`/`_ts_zscore`/CS-z)逐字保留;**双轨图都要放**(核验:AEUS 是代码内 V1 硬编码 11 条边 + config graph_config V2 14 条边,graph_version 切换)—— AEUS 的 §3.2 先验图同时写进 SUPPLY_CHAIN_GRAPH(V1)与 config(V2 初版=V1,校准后覆写 V2);NODE_* 常量 3→4 节点;`_asml_tilt` → `_backlog_tilt`(z 空间拼接逻辑保留,RPO 序列直接喂);外部确认 tilt 0.30 权重保留,挂接 elec_gen/gas_price/utility_capex |
| `graph_calibration.py` | 🟡 | IC 扫描/因子残差/Katz/0.05 阈值逐字;CANDIDATE_EDGES 换 §3.3 候选池;文献带保留 |
| `momentum.py` | 🟢 | 零改动 |
| `regime.py` | 🟢 | 零改动(阈值走 config) |
| `risk_overlay.py` | 🟢 | 零改动(默认 off,同 AEUS) |

### 5.5 backtest/ 与 portfolio/

| 文件 | 裁决 | 具体改动 |
|---|---|---|
| `backtest/engine.py` | 🟡 | 事件环/qlib 适配/原生回退/DD 断路器逐字;实验名 `electric_utilities_strategy_backtest`→`electric_utilities_strategy_backtest`;类 `AEUSBacktest`→`AEUSBacktest` |
| `backtest/costs.py` | 🟢 | 分档机制不变;⚠️ 核验修正:**权威名单 = `universe.STOCK_TIER`**(costs.py:41 注释原话 "single source of truth"),config 的 costs.tier_*_tickers 全仓 **零消费方**(纯文档)——改名单只改 universe.py,config 列表同步更新防误导 |
| `backtest/metrics.py` `qlib_adapter.py` `dd_analysis.py` `robustness.py` `sensitivity.py` `trade_audit.py` | 🟢 | 零逻辑改动;基准名/输出路径随 config |
| `portfolio/optimizer.py` `rebalance.py` `risk.py` `stop_loss.py` | 🟢 | 零改动 |
| `portfolio/strategy.py` | 🟡 | 类名 `AEUSWeightStrategy`→`AEUSWeightStrategy`,其余逐字 |
| `report/plots.py` `tearsheet.py` | 🟡 | 标题/文件名 AEUS→AEUS;PALETTE/版面不动 |

### 5.6 tests/(107 个测试全部保活)

| 文件 | 裁决 | 具体改动 |
|---|---|---|
| `test_engine_smoke.py` `test_backtest.py` `test_optimizer.py` `test_signals.py` `test_macro_clusters.py` | 🟡 | 合成 fixture 的子板块名/票名替换,断言逻辑不动 |
| `test_universe.py` | 🔴 | 按新 8 板块重写断言 |
| `test_supply_chain.py` | 🟡 | 无泄露/传导矩阵/graceful 回退三类断言保留,图结构断言换新图 |
| `test_asml_guidance.py` → `test_backlog_rpo.py` | 🔴 | ASML HTML 特化测试删,新写 RPO XBRL 提取测试(多 tag 版本/单位/去重) |
| `test_xbrl_decumulation.py` `test_pit_staleness.py` `test_aeus_fetch_prices_truncation_guard.py`(→aeus) | 🟢 | 机制测试,仅改名 |
| `aeus_matrix.py` → `aeus_matrix.py` | 🟡 | 39 组×V1/V2 矩阵机制保留(aeus_matrix 实际取 PARAM_SETS 全量);win 基准 XLU/GRID→XLU/GRID |
| `aeus_verify_excel.py` → `aeus_verify_excel.py` | 🟢 | 逐 sheet 校验逻辑通用 |
| `test_pipeline_integration.sh` | 🟡 | 6 阶段保留;路径/脚本名替换 |

### 5.7 全局命名映射(mechanical rename 清单)

```
文件名:   AEUS*  → AEUS*        aeus_fetch_* → aeus_fetch_*      aeus_pipeline.sh → aeus_pipeline.sh
类名:     AEUSBacktest → AEUSBacktest      AEUSWeightStrategy → AEUSWeightStrategy
包引用:   electric_utilities_strategy → electric_utilities_strategy(全部 `python -m` 与相对 import)
状态文件: inventory_aeus / account_aeus / trade_ledger_aeus → *_aeus
产出前缀: aeus_daily_report_ / aeus_batch_ / aeus_portfolio_ / monitor_aeus_ / wf_diagnostic_aeus_ / aeus_tearsheet → aeus_*
数据根:   price_data/elec_strategy → price_data/elec_strategy
env:      AEUS_SEC_USER_AGENT → AEUS_SEC_USER_AGENT(新增,POLYGON/FRED key 复用)
日志:     logs/aeus_<mode>_*.log → logs/aeus_<mode>_*.log
scope 串: 'aeus'(CorporateActions/ledger/QC)→ 'aeus'(daily 代码先换,接线时外部注册)
幂等标记: "AEUS DAILY BACKTEST COMPLETE" → "AEUS DAILY BACKTEST COMPLETE"
          (daily_backtest.sh :57 grep 此串做幂等门 + :127 写入 —— 两处必须同改,漏一处幂等门失效)
```
执行方式:先脚本化全局替换(大小写三种形态 aeus/AEUS/Aiss 分别处理,`electric utilities`/`elec_strategy` 只替换路径语义处,**不碰注释里对 AEUS 的历史引用**——改为"(承自 AEUS)"),再人工逐文件过一遍 diff。

---

## 6. AEUS 的目录外基础设施镜像清单(全仓 grep 得出,共 10 个系统)

⚠️ **纪律**:以下都在"只能动 electric_utilities_strategy"红线之外。分三级处理:

### A 级 — 只读调用,零修改,phase-1 即用
| 设施 | 用法 |
|---|---|
| `MacroStateStore.py` + `price_data/macro/` | 宏观 23 维,只读 |
| `MCPS.py` | macro_cond_sharpe 打分,import 即用 |
| `SimilarityEngine.py` | AUTOENCODER_FEATURES/AutoencoderMethod,import 即用 |
| qlib 本体 | 同 AEUS 适配层 |

### B 级 — 纯新增(add-only,不改任何现有文件;有 elec_strategy 先例,默认随 D 阶段做,做前逐项报备)
| 新增物 | 对应 AEUS 物 |
|---|---|
| `price_data/elec_strategy/`(五子目录) | price_data/elec_strategy/ |
| `historical_runs/electric_utilities_strategy/` | historical_runs/electric_utilities_strategy/ |
| `price_data/electric_utilities_universe.json`(事件风险两级清单:tier1=电力/AI 基建直接,tier2=下游) | price_data/electric utilities_universe.json |

### C 级 — 要改共享文件(每处一行级 add-only;**逐项单独请示,phase-1 全部先旁路**)
| 文件 | 需要的改动 | phase-1 旁路方案 |
|---|---|---|
| `qlib-main/portfolio_ledger/ledger.py` | STRATEGIES 加 `"aeus"` 条目:dir=electric_utilities_strategy / snap_glob=inventory_history/inventory_aeus_*.json / holdings_key=stock_holdings / store_dir=price_data/elec_strategy/prices / live_start=AEUS_LIVE_START / benchmarks=["XLU","GRID","SPY"] —— 这一条同时解锁 §6.7 报表与 §2.6 live 段账本链 | 无需开关:Account.load 已 try/except(核验 :926-940),自动退名义 capital + WARNING |
| `CorporateActions.py` | run_for 加 `'aeus'` 分支 + AEUS_INVENTORY 常量 | 无需开关:daily 调用已 try/except 降级(核验 :908-916),接线前每天一条 WARNING,拆股窗口人工盯 |
| `EventRiskDetector.py` | 理想:universe 路径参数化 | config `event_derisk.enabled: false`(AEUS 本来就有开关) |
| `RefreshEventRiskData.py` | 电力宇宙刷新入口 | 同上,先不接 |

### D 级 — 后期集成(AEUS 回测/试运行验收后,每项独立立项批准;详案见 §6.5-§6.7)

| 系统 | 内容 | 详案 |
|---|---|---|
| QC 实盘镜像 | **中途注资 + 第六策略挂载**(2026-08-29 用户规格) | §6.5 |
| 前端四件套 | NAV 实盘面板 / strategy performance / 全部 artifact 策略选择器 / PnL+risk 报表 | §6.6 |
| portfolio_ledger 报表 | AEUS 的 pnl_report / risk_report 自动生成(格式同 AEUS,内容按 AEUS) | §6.7 |
| `UpdateMasterPerformance.py` | master 曲线加 aeus 组件,**完整镜像 AEUS 五件套**:`AEUS_EQUITY_DIR`/`AEUS_INVENTORY_DIR`/`AEUS_LIVE_START` 常量、`load_aeus_equity_backtest`(冻结 vintage 优先+best-column 响亮回退)、`load_aeus_equity_live`(账本优先+inventory-MTM 回退)、`_freeze_backtest_segment('aeus')`、splice_freeze.json 加 "aeus" 键(§2.6) | — |
| `controller/`(registry.py STRATEGIES 五元组→六元组、model.py `_assemble_aeus`) | 中央估值第六策略节点(NAV 面板的后端真源) | §6.6.1 |
| `VolumePrediction/strategy_adapters/aeus_adapter.py` | 照 aeus_adapter 30 行薄封装 | — |
| `conductor/backup_to_external.sh` 等 4 个 | BUSY_PATTERNS 加 aeus_batch;备份/清理范围加新目录 | — |
| OpenClaw cron 三件套 | ✅ 时间已定:`aeus-daily-backtest` **19:10** / `aeus-daily` **20:20** / `aeus-weekly` **周日 03:30 ET**(与 AISS 17:55/19:00/周日 02:00 完全错开;由你在 OpenClaw 建) | — |

### 6.5 QC 实盘镜像:中途注资挂载 AEUS(2026-08-29 用户规格)⭐

**QC 现状(已读 inventory_source.py 源码确认)**:
- go-live(2026-08-17)一次性锁定 **C0 = $6,000,322 = Σ(五策略官方 equity)**,scalars =
  官方/账本(AEUS≈2.68)**构造后永不重算**;QC 是从 C0 起步的独立自复利账户,官方 perf 永不再读;
- pairs 有 L/S/F 三队列(legacy 有机退场);AEUS 真源 = **account_aeus.json 非 inventory**
  (inventory 是子板块合成层不可执行 —— AEUS 完全同构,真源 = account_aeus.json);
- 有先例机制:`QUANTCONNECT_MIRROR_PLAN.md` §已有"一次性 CashBook 校准
  (deposit/withdraw 金额=K)"做法。

**AEUS 挂载规格(用户 2026-08-29)**:C0 不含 AEUS;若 AEUS 于 9/1 建仓,则 9/1 当日:

```
注资额 K = AEUS 官方 equity(master aeus_equity 末行 ≈ $1,158,818)
QC 动作  = CashBook deposit(K)            # 一次性,沿用既有校准先例
         + 按 account_aeus 股数 × scalar_aeus 买入(exporter 常驻循环自动)
         + 余下现金留账(cash ≥ 0)
QC 总资本 = 6,000,322 + K(此后照旧自复利,K/scalar 永不重算)
```

- **scalar_aeus = 1.0 且冻结**:AEUS 从 live 首日起官方口径=账本口径(没有 AEUS 那种
  回测期错位),官方/账本 = 1;挂载时写死,与其他五策略的 scalars 同表同纪律;
- 改动点(每处 add-only):`inventory_source.py` 文件映射加 `"aeus": .../account_aeus.json`
  + `build_target` 的策略清单 + scalars 表;`ops/rolloff.py` EOD 闸门映射加
  `"aeus": ("master_portfolio_performance.json", "aeus_equity")`;`reconcile/qc_reconcile.py`
  对账范围加 aeus;exporter 无需改(遍历注册表);
- **QC holdings 键 = 历史首名**纪律(OBDC→ORCC 先例)对 AEUS 从第一天就适用:若未来
  GEV/NXT 等改名,QC 键保持首名,由 ticker_aliases механизм处理;
- 验收:注资当日 QC 侧 Σ(aeus 持仓市值+cash) 与 account_aeus equity 差 < 成交价差量级;
  §9.4 差值闭环报告把 aeus 并入。

### 6.6 前端四件套(2026-08-29 用户规格)⭐

> 前端改动全部属 D 级,受"[[feedback-frontend-three-routes]] 只动 Someo-Agent-ON 路由"约束外
> 的展示层(artifact/viewer 层不在禁触名单,但仍逐项报备)。已 grep 现状,改动点如下:

**6.6.1 NAV 实盘面板(RealtimeNavViewer + controller)**
- 后端真源:controller `registry.py:40` `STRATEGIES = ("mrpt","mtfs","aeus","ssrs","bdc")`
  → 加 `"aeus"`;`model.py` 加 `_assemble_aeus`(照 `_assemble_aeus` 132-160 行同构:
  account_aeus 实仓/cash + inventory_aeus 子板块权重 attrs + 最新 aeus_daily_report
  stock_breakdown;恒等式②同款:Σbranch shares ≡ account.positions 逐票);
- `routes/controllerNav.ts` 若有策略白名单则加 aeus;
- `RealtimeNavViewer.tsx:80` 映射表 `MRPT/MTFS/SSRS/AEUS/BDC` → 加 `AEUS: 'aeus'`;
- ⚠️ 记忆里的跨层对账纪律直接适用:controller 每层都挂 holdings,深度遍历会双计;
  AEUS 残差基线在 M4 对账中单独建立。

**6.6.2 strategy performance**
- 数据:`master_portfolio_performance.json` 由 UpdateMasterPerformance 写入 `aeus_equity`
  列(见 D 级表第 4 行;master = MRPT+MTFS+SR+AEUS+AEUS);
- `StrategyPerformanceViewer.tsx`:`STRAT_KEYS`(:56)、`MASTER_KEYS`(:57)、
  `TOOLTIP_ORDER`(:61)、默认 `activeStrategies`(:131)四处加 `'aeus'`;
  TOOLTIP_ORDER 的基准段加 `xlu`/`grid`(对应现有 spy/smh/soxx 位);
- 配色沿用现有调色板下一顺位,五语 locale 加 "AEUS" 显示名。

**6.6.3 每个 artifact 的策略选择器(全量清单,grep 实测)**
| 文件 | 现状 | 改动 |
|---|---|---|
| `src/lib/api.ts:37` | `QLIB = (s) => s==='ssrs'||s==='aeus'` 类型卫兵 | 加 `'aeus'`;`:113` universe 分支加 `/api/aeus/stock-universe` |
| `EquityChart.tsx:70` | `['mrpt','mtfs','ssrs','aeus']` | 加 `'aeus'` |
| `InventoryViewer` / `InventoryHistoryViewer` / `SignalTable` / `PortfolioHistoryViewer` / `WFGridViewer` / `WFStructureViewer` / `WalkForwardSummaryViewer` / `RiskReportViewer` / `PnlReportViewer` | 各自的策略枚举/下拉 | 逐个加 aeus(开发时 grep `'aeus'` 全量扫,**一个不漏**——共 15+ 组件文件已在 §6 触点清单) |
| `src/i18n/subsectors.ts` | 8 个半导体 key 硬编码 | 架构决定:**改成按策略命名空间**(`subsectors.aeus.*` / `subsectors.aeus.*`)或平铺追加 8 个电力 key(与现有 fallback 机制兼容者优先);五语 locale(en/es/fr/ja/zh)各加 8 板块显示名 |
| `server/index.ts:27/73` | `/api/aeus` → electric utilitiesRoutes | 加 `import electricRoutes` + `app.use('/api/aeus', electricRoutes)` |
| `server/routes/electric.ts`(新) | — | 照 electric utilities.ts 整文件同构:AEUS_DIR→AEUS_DIR 等 6 个路径 helper 指向 electric_utilities_strategy |

**6.6.4 PnL report + risk report(格式同 AEUS,内容按 AEUS)**
- 服务端:`routes/pnlReport.ts:17` `PNL_DIRS` 加
  `aeus: 'qlib-main/electric_utilities_strategy/trading_signals/pnl_reports'`;
  `routes/riskReport.ts:18` `RISK_SOURCES` 加 aeus 同构条目;
- 生成端见 §6.7;前端零格式工作 —— 两个 viewer 的策略选择器加 aeus 即可。

### 6.7 portfolio_ledger 报表:AEUS 的 pnl/risk 报告 ⭐

**机制(已读 reports.py/ledger.py 确认)**:AEUS 的 `pnl_report_YYYYMMDD.pdf` 与
`risk_report_YYYYMMDD.{json,txt,pdf}` 都由 `qlib-main/portfolio_ledger/reports.py`
按 `strategy` 参数生成,落到 `{strategy_dir}/trading_signals/{pnl_reports,risk_management}/`
—— **格式代码是共享的,AEUS 挂上 STRATEGIES 注册表后格式自动与 AEUS 同款**。

内容按 AEUS 的改动点(全部数据驱动,不动版式):
1. `ledger.py` STRATEGIES 加 aeus 条目(C 级已列):`benchmarks: ["XLU","GRID","SPY"]`
   (AEUS 是 ["GRID","SPY"])→ 报表基准段自动换;
2. `reports.py:37` `BENCH_STORE_STRATEGY = "aeus"`(SPY/GRID 在 semi store 的历史遗留)
   —— AEUS 的 XLU/GRID 落在 elec store,此处需泛化为**按策略取各自 store**
   (小改,向后兼容:aeus 行为不变);
3. `reports.py:584` 波动率阈值 `aeus→(50,80) 其他→(18,30)`:AEUS 是个股策略但公用事业
   波动远低于半导体,建议 **(30,50)**,同处加 `aeus` 分支——具体值以 D5 回测实测
   年化波动分布定,写死前给你过目;
4. 报表内的 subsector 分组/持仓明细全部读 inventory_aeus/account_aeus,自动是电力内容。

生产接线:AEUS 的报表由其 daily 流程末尾经 portfolio_ledger 触发(daily_update + 
subprocess 报表),AEUSdailySignal 同位置同构调用 —— phase-1(账本未挂)自动跳过,
挂上后无需再改 daily 代码。

---

## 7. 开发阶段(你说"开始"后按序执行,每阶段验收后进下一阶段)

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **D0 清理+改名** | §5.2 清理 1.1G 残留;§5.7 全局改名;config 重写 | `conda run -n qlib_run python -c "import electric_utilities_strategy"` 及各子模块 import 冒烟通过;`grep -ri aeus` 仅剩历史注释 |
| **D1 宇宙落地** | universe.py 重写(**待你确认 §2 股票清单后**);IPO 首日以真实价格数据核实 | test_universe 绿;effective_weights 在 GEV/CEG/VRT 门控日期断言正确 |
| **D2 价格层** | price_data/elec_strategy 建目录;`aeus_fetch_prices --init --start 2016-01-01`(约 35 票+XLU/GRID/SPY);--verify 全绿 | 全票 parquet 落盘,_fetch_meta 完整,AEUS store 零触碰(哨兵) |
| **D3 PIT 信号源** | utility_capex(XBRL)→ backlog_rpo(XBRL)→ EIA 全谱(§4.1 A1-A7:需求/发电/装机/度日/气耗)→ gas_price → FRED 补充(A8/A9);逐源 --init + --verify;CIK/系列 ID 逐一核验 | 每源有数据、PIT 键正确、staleness 通过;RPO tag 在 GEV/VRT/PWR 三家 10-Q 实测取到;A5 去天气残差在历史夏季峰值上肉眼合理 |
| **D4 信号层** | supply_chain 新图 + composite 字典;信号冒烟(get_current_signals) | test_supply_chain/test_signals 绿;无未来泄露断言通过 |
| **D5 回测+校准** | 单回测跑通;graph_calibration 实证校准 → v2 图回填 config;validate vs XLU/GRID | BacktestResult 完整;IC 报告落盘;2019-2026 回测无 NaN 断链 |
| **D6 选参全链** | batch 39 组 → WF → macro_clusters → smart_select → daily_backtest.sh | selected_param_set.json 生成;encoder/centroids/cluster_oos 产物齐;幂等门工作 |
| **D7 测试套件** | 107 个测试全改造;test_pipeline_integration.sh --quick 全绿 | pytest 全绿(合成数据,无网络);matrix 66 回测零错误 |
| **D8 试运行** | AEUSdailySignal --dry-run 连续数日;C 级接线逐项请示;D 级立项 | dry-run 报表合理;三只 cron(19:10/20:20/周日 03:30)由你在 OpenClaw 创建后转正 |
| **D9 go-live 拼接** | §2.6 时间线:冻结固定段(真实交易 param+vintage+逐日字面量)→ 定死 AEUS_LIVE_START=首日建仓日 → account_aeus $1M 起账 → master 接线(D 级)→ **QC 同日注资挂载(§6.5)** | aeus_splice_freeze.json 落盘且此后固定段逐日不变(连续 3 天 md5 相同);live 段首日收益来自真实账本;QC 侧 aeus 市值+cash 与账本 equity 对上 |

**测试纪律(每阶段适用)**:输出一律 /tmp 或 `--no-prod-write --output-dir`;跑批前后对 `electric_utilities_strategy/`、`price_data/elec_strategy/`、`price_data/macro/` 做 md5 哨兵;绝不在 AISS 夜间窗口(17:55-20:00 ET / 周日 02:00)做重 IO 操作(两策略共享 qlib_run 与 Polygon 限速)。

---

## 8. 已裁决 & 剩余开放问题

**✅ 2026-08-28 已裁决**:
1. grid_equipment primary = **ETN**(GEV 走 2024-04 门控 + 纯度倾斜放大)——采纳"EMR/ETN 锚定 + GEV alpha 放大器"方案,并明确 ETN 优于 EMR;
2. renewables_storage = **NXT/FLNC/ARRY/SHLS,ENPH 剔除**(户用逻辑=非相关噪音);
3. 基准 = **XLU + GRID 双杀 + 50/50 混合作 active 基准**;
4. **EIA key 已有**(根 .env `EIA_API_KEY`),elec_gen 走 EIA v2,复用 macro 模块已打通的取数模式;
5. 板块内配比 = **base_w + 图谱驱动纯度倾斜**(§2.5),κ 进参数网格。

**✅ 2026-08-29 追加裁决**:
6. 初始资本 **$1,000,000**(同 AEUS C0);
7. cron 错峰定稿:**aeus-daily-backtest 19:10 / aeus-daily 20:20 / aeus-weekly 周日 03:30 ET**(AISS 为 17:55/19:00/周日 02:00,完全错开);
8. **FSLR 补入** renewables_storage 加权成员(§2.1);
9. **业绩曲线拼接机制全套镜像**(§2.6):固定回测段(冻结 param+vintage+逐日字面量)→ live_start=第一天建仓日 → 账本日收益率链接;V1 月度生产、V2 研究;上线当天即冻结,不重走 AEUS 的 5 周弯路;
10. **QC 中途注资挂载**(§6.5):QC C0=$6,000,322 不含 AEUS;建仓日 CashBook deposit K=AEUS 账本 equity(≥$1M),买入持仓+cash≥0,scalar_aeus=1.0 冻结;
11c. **altdata→决策通路**(§4.2):九路信号逐一挂进 AEUS 的 4 条既有通路(图谱节点/确认tilt/敞口放大器/purity_tilt),含精确公式与传导实例;三条"不做"纪律(无日频触发/不进regime/tilt 封顶);外部数据整体效用由 Group F(external_on/off)WF 裁决;
11b. **altdata 全谱**(§4.1):发电量/需求/装机/季节性/电价/地区价格九路信号全部规划——EIA v2 五路由 + FRED 双系列 + macro 模块已镜像的气库存;度日"去天气残差"作为 AI 负荷信号本体;update_data 7→9 步;
11a. **2026-08-29 扩容**:板块 9(gas_midstream:KMI/WMB/OKE/TRGP,reserve LNG)与板块 10(water_cooling:AWK/WTRG/AWR/CWT,reserve SJW)**都做**;NVT/HUBB/CAT/CMI/ATKR/CWEN/BEPC 补进各板块候补池;板块选择机制照搬 AEUS top_n(§2.4);gas/water 的 altdata(EIA 电力部门气耗、水务 capex XBRL)提前入数据层建设清单(§4);
11. **前端四件套**(§6.6):NAV 实盘面板 / strategy performance / 全部 artifact 策略选择器 / PnL+risk 报表全部加 aeus;报表格式走 portfolio_ledger 共享代码自动同款,内容按 AEUS(基准 XLU/GRID、波动阈值另定)(§6.7)。

**⏳ 仍开放(不阻塞开发)**:
1. purity 分完整表(§2.5)开发时给你过目一次;
2. 远期:GRID 欧洲成分(施耐德/西门子/ABB ADR)作 alpha 池扩展。


---

## 9. 风险登记

- **GEV/CEG/VRT/NXT/FLNC/OKLO/SMR/TLN 历史短**:多板块锚定或门控依赖 2020-2024 上市股 → WF 早期折叠里降档,回测 2019-2022 段截面不满。ETN/BWXT/PWR/VST/NEE/AEE 六个长史锚保证 8 板块中至少 6 个全程在场;**早期结果解释时记得截面不满**。
- **动态配比是新代码**(§2.5,AEUS 无先例):必须带 κ=0 逐 bit 回归锚 + 独立单测;若 WF 显示 purity_tilt 组不占优,production 保持 κ=0 = 纯 AEUS 行为,机制零风险保留。
- **利率因子缺口**:AEUS 4 因子没有显式利率因子,电力板块利率敏感度远高于半导体 → v1 用图谱 rate_env 节点承接;若回测显示不足,再议第 5 因子(改 composite 架构 = 大动作,单独批)。
- **RPO tag 覆盖不确定**:RevenueRemainingPerformanceObligation 各家披露口径有差(部分只披露 12 个月内部分)→ D3 实测三家,不行退价格动量代理(graceful 机制现成)。
- **电力板块日内与半导体相关性**:robustness.py 的 someopark 互补性检查(目标 ρ<0.3)对 AEUS 尤其重要——若与 AEUS 高度相关,组合层面价值打折,验收时必看。
- **两策略共享 Polygon/FRED 限速与 qlib_run**:错峰调度写进 cron 设计(§6D)。

---

## 10. 逐行核验记录(2026-08-29,对照 electric_utilities_strategy/ 实际代码)

对 plan 全部关键声明逐条验证后修正 7 处错误 + 1 个设计洞:

| # | 类型 | 发现 | 处置 |
|---|---|---|---|
| 1 | 设计洞 | **purity_tilt 只挂 stock_decompose 则回测测不到 κ**——AEUS 篮子收益(回测资产)与个股分解同权重,两层一致;只倾斜分解层,WF 无法裁决 κ | §2.5 改为双层同步:loader 篮子构造 + stock_decompose 调同一 `apply_purity_tilt` 纯函数 |
| 2 | 机制误读 | reserve 是**单票槽**(subsector_reserve → Optional[str]),plan 曾每板块给 2 个 | §2.1 改为每板块 1 首选 reserve + 候补名单(纯文档) |
| 3 | 事实错 | param sets = **39**(A6/B4/C9/D4/E3/F2/G4/H4/M3,实测 len),"59"系读码誤报 | 全文修正 |
| 4 | 事实错 | 成本分档真源 = `universe.STOCK_TIER`(costs.py:41 原话),config costs.tier_* **零消费方** | §2.3/§5.5 修正 |
| 5 | 过度设计 | CorporateActions/:912、ledger sizing/:930、ledger update/:1610 **已是 try/except 降级不阻断**,phase-1 无需加开关 | §5.1/§6C 简化 |
| 6 | 补漏 | pipeline daily step2(:169)调根目录 RefreshEventRiskData.py(半导体事件 store)——AEUS 若不处理会去刷半导体数据 | §5.1 pipeline 行:phase-1 注释,C 级恢复 |
| 7 | 补漏 | daily_backtest.sh 幂等门 = grep "AEUS DAILY BACKTEST COMPLETE"(:57/:127 两处),漏改则幂等失效 | §5.7 改名映射表补入 |
| 8 | 数字 | trading_signals 实测 162+252=414+ 文件(非 541);V1 图 11 条边 + config V2 14 条边(双轨确认);walk_forward 默认 step_days=10、multi_horizon 权重 .15/.25/.35/.25、macro_clusters 产物路径、smart_select 双路径、validate 四处硬编码、engine 实验名 :189——**全部与 plan 一致**(核验通过) |

同时确认的无误项:config 真值(top_n=3 / max_weight=0.55 / vix 36 / dd −0.25/−0.12 / vol 0.30,
某读码 agent 报的 4/0.40/35/−0.15/0.12 是错的,plan 未采用✓);capex tickers 单源
`universe.CAPEX_PULSE_TICKERS`(company_signals:62 引用)✓;IPO_DATES 仅 4 票需门控✓;
文件覆盖清单齐(45 源文件 + 14 测试全部有裁决行)✓。

## 11. PJM 扩展接入核验记录(2026-09-02 01:00–02:10 ET,用户令"马上、最高标准、明天带新数据")

**范围**:在 09-01 西枢纽 DA LMP 接线之上,新增 5 个 Data Miner 2 feed → 6 个 PIT 冻结店 → 7 条派生序列,
**不新增 tilt**(ipp_wholesale 已满 2 条),只进既有 z 均值:`price_pulse`(+DOM 基差)、power_demand
节点(+分区计量负荷 YoY)、`shortage_score`(+`shortage_east` = mean_z(−备用裕度, 日 0 强迫停机, DA 预报误差))。
门控 `external_sources.pjm.extended`(默认 true;false → 与 09-01 接线逐字节等价)。

| 项 | 实测 |
|---|---|
| 字段/命名(实时探测)| DOM 区 pnode_id **34964545**(type=ZONE);计量负荷 load_area = DOM / PEPCO / **BC**(=BGE)/ AEP 四分区(AEPAPT/AEPIMP/AEPKPT/AEPOPT)+ RTO;停机 region = "PJM RTO" / "Mid Atlantic - Dominion" / "Western";预报每 6h 一次评估(05:45/11:45/17:45/23:45)|
| 存档墙 | 2024-08-15 查询 → 400/0 行;`EXT_START=2024-09-15`;已入店数据 append-only,不随时间流逝丢失 |
| 可得性滞后 | 计量负荷 9/2 只到 8/30(~3-11 天浮动)→ `ZONE_LOAD_LAG_DAYS=12`;预报误差同滞后;LMP/裕度/停机当日 |
| 回填结果 | hub 640d(2024-12-02→9/2)/ DOM 718d / 负荷 715d×8 区 / 裕度 716d / 停机 717d×4 列 / 预报 718d×2 区;6 店 16–165KB |
| `--verify` | 7/7 序列 OK、STALE 0、exit 0(显示日期已按可得性截到今天)|
| 测试 | 新 `tests/test_pjm_extended.py` **8/8**(tmp_path 店 + 注入 fetch,零网络零生产写入);回归 **173/173** |
| cron 同环境 | `env -i` 无 conda PATH、不预 source .env 跑 `update_data` → 9/9 OK(见 §4b 加固)|
| 历史不变性(只读对比 extended 开/关)| price_pulse / demand 节点 / shortage_score 在各自新腿首日之前 **max\|Δ\| = 0.0000,长度不变**(7002 / 2153 / 1651)|
| 新腿生效后 | price_pulse 末值 −0.198→−0.026(post corr .95,max\|Δ\| 1.18z);demand 节点 +0.397→+0.170(新腿仅自 2026-02-21,corr .77,max\|Δ\| 1.91z);shortage_score −0.372→−0.486(corr .79,max\|Δ\| 2.22z)|
| 期间修掉的两个自伤 | ① shortage 融合初版对混合序列重做 `_ts_z` → 历史被裁 251 行并重标定(n 1651→1400),改为 z 空间行均值不重 z;② 首版对比脚本用固定 2025-10 截点误判"历史变了",改按各新腿真实首日 |
| 退出码 | `--update`:wired 下 hub 0 行 → exit 1;≥2 扩展 feed 0 行 → exit 1;单个 miss 只 WARN(weekly `--verify` 兜底时效)|

**对 9/2 20:20 daily 的含义**:signal 将首次带 DOM 基差 / 分区负荷 / 东部紧缺三条新信息;新腿只改 2025-04 之后的
输入,生产参数 `pure_supply_chain v1` 的回测结论在那之前不受影响。若要回到 09-01 状态:`extended: false`。

## 12. 三条数据→仓位通路的接线与裁决(2026-09-02,用户令"三条全做、最高标准")

用户的问题一直是同一个:EIA 装机、ERCOT 稀缺、PJM 东部紧缺这些数据**到底有没有进仓位**。
逐条查完的结论是:①②④ 三条通路都活着(节点 z / confirmation tilt / purity),
**通路③(敞口放大器)从建成起就没接线**;EIA-860M 的 by_source 永远是空的,
`renewables_storage` 的第二条 tilt 因此从来没生效过;ERCOT 三条 macro accrual 序列也没有消费者。

### 12.1 通路③ 敞口放大器(AEUS 已翻开 / AISS 接好但不开)

**做法**:纯函数 `portfolio/risk.compute_exposure_amplifier(z, weights, sensitivity, k, lo, hi)`
→ `E = clip(1 + k·z·φ, 0.85, 1.15)`,作为 `apply_risk_controls(exposure_mult=E, …)` 的入参,
在**所有风控档之后**只改 gross、不改选谁。

**知识图谱怎么用的(φ)**:缺电不是对所有子板块一视同仁——它沿图谱里 `power_demand_proxy` /
`power_price_proxy` 的出边传导。`signals/supply_chain.shortage_sensitivity()` 把这两个节点的入边
权重按板块汇总、归一到均值 1(无入边的给地板值 0.5,"缺电时整条链都受益,只是幅度不同"),
再用**当前组合权重**加权:φ = Σ w_s·sens_s / Σ w_s。生产图谱(v2)算出来是
`ipp_wholesale 2.14 / gas_midstream 1.57 / regional_utility 1.29 / 其余 0.71`。
于是同一个缺电度 z:压在 IPP/中游燃气的组合被放大得多,躲在 water_cooling 的组合几乎不动。
全期实测 φ ∈ [0.71, 1.29],E ∈ [0.85, 1.15],61 次调仓带放大器。

**接线位置(四处,一个纯函数)**:`portfolio/strategy.py`(**qlib = 真生产路径**,两个调用点)、
`backtest/engine.py::_run_native`(fallback)、`backtest/trade_audit.py`(审计重放)、`AEUSdailySignal.py`(实盘)。
> 踩过的坑:先只接了 `_run_native`,沙盒 batch ON≡OFF 逐字节相同——**native 根本不被调用**。
> RUNBOOK 里"native 是生产引擎"的旧说法据此已更正。

**两条安全约束**:① 不许杠杆(默认)时 `target = min(gross·E, 1.0)`,而调仓时 gross 恒为 100%
→ **E>1 实际无效,放大器是单向减仓器**(想要上行必须显式开 `allow_leverage`);
② **防守优先**:vol/VIX/DD/事件任一档已触发时 E>1 被钳到 1.0,缺电尖峰不得把防守现金买回去(E<1 仍可继续减)。
单板块 `max_weight` 上限在放大后依然成立。E=1 → 结果逐字节等于旧行为。

**沙盒裁决(全部 /tmp,mlflow 打桩,零生产写入;生产参数集 pure_supply_chain v1)**

| | batch sharpe | batch calmar | batch maxdd | WF sharpe | WF calmar | WF maxdd | 逐 fold OOS sharpe |
|---|---|---|---|---|---|---|---|
| AEUS OFF | 1.6979 | 1.2749 | −26.31% | 1.578 | 1.470 | −26.38% | — |
| AEUS **ON** | **1.7207** | **1.3534** | **−24.55%** | **1.618** | **1.554** | **−24.62%** | **48/70 胜**,mean +0.0414,wilcoxon p=**4.65e-05** |

代价:总收益 8.12→7.96(缺电度低时留现金)。**判定:赢 → `risk.exposure_amplifier.enabled: true`(2026-09-02 起生效)。**

**AISS 镜像**:同一套(`compute_exposure_amplifier` + `demand_sensitivity` 多跳传导 + 四处接线 + 6 项测试),
输入换成 `altdata_signals.load_ai_demand_cycle()`(hyperscaler capex / 韩国出口 / 电子新订单 z 混合),
φ 从 `ai_capex_proxy` 沿供应链**多跳**传导(每跳 decay 0.6,地板是所有人的下限):
`ai_gpu 1.99 / foundry 1.23 / custom_asic 1.06 / memory_hbm 0.96 / equipment 0.92 / 其余 0.62`。
沙盒:WF sharpe 1.315→1.297、calmar 1.484→1.421、逐 fold 26/70 胜(p=0.17)、总收益 −7%。
**判定:没赢 → AISS `enabled: false` 保持不动**(代码接好、随时可开,关闭时逐字节等价旧行为)。

### 12.2 EIA-860M 分来源(by_source)

两个 bug 叠在一起,导致 `load_renewables_adds_yoy` 自建店起永远空、`renewables_storage` 第二条 tilt 从未生效:
1. **字段名**:EIA v2 的 facet 列是下划线 `energy_source_code`(连字符版只存在于 desc 列),代码读的是连字符 → `by_source={}`。
2. **抓取方式**:`--refreeze` 用「2019-01 到今天」一个大窗口翻页,EIA 的 offset 分页在深处急剧退化
   (offset 1.5M 单页 23 s)→ 2019 年起的重建跑了 13 小时没完。改为**按月一个请求窗口**
   (每月 ~20–26k 行 = 5–6 页,单页 <25k offset,约 20 s/月),并按 `period == 窗口月`过滤,
   即使 stub 忽略 start/end 也不会重复计数。增量路径(最后一个已存月 → 今天)只有 2–4 个月,成本与旧版相当。
3. **top-12 截断**(2026-09-02 当晚二次发现并修复):`by_source` 只保留每月装机最大的 12 个来源码。
   电池 MWH 早年排不进榜(实测 2019-01 排**第 24 名**、898 MW),于是被静默丢掉;而 renewables 求和要
   SUN+WND+MWH → **t 有电池、t-12 没有**的那 12 个月(2021-10~2022-09)分子含电池分母不含,
   YoY 是"数据开始记录电池"的假象。MWH 首次入店的 2021-10,它的值 3741.8 **恰好等于当月第 12 名的值** —— 铁证。
   改法:去掉 `[:12]` 全量保留(每月 33–36 个码,store 39KB→82KB),排序只为可读性。
   **实测修正**(BEFORE/AFTER 逐月对照,脚本 `scratchpad/verify_truncation_fix.py`):

   | 验收项 | BEFORE | AFTER |
   |---|---|---|
   | 月份集合 / `total_mw` 逐月 | 90 月 | **完全一致(最大偏差 0.0 MW)** |
   | 每月来源码数 | 12 | 33–36 |
   | 缺口 (总量−分来源和)/总量 | 最大 **2.198%** | **0.000%** |
   | MWH 覆盖月数 | 57/90(首现 2021-10) | **90/90(首现 2019-01)** |
   | 污染段 YoY | 虚高 | 下修 1.10~2.05 pp(均 −1.47) |
   | 末值 / tilt z | 17.05% / +0.157 | **17.05% / +0.157(未动)** |

   历史 tilt z 最大改动 0.551 @2022-09。今天的实盘值不受影响(36 个月 z 窗口早已把污染段甩出),
   受影响的是**回测/参数标定**看到的 2021-2022 那一段。
   **防复发**:`verify()` 新增不变式守卫 —— 分来源之和必须 ≈ 总量(缺口 >0.5% 或来源码 <20 个即判 INCOMPLETE)。
   这个 bug 能活一辈子,正是因为体检只问"非空"、不问"全不全"。

### 12.3 ERCOT macro accrual 三条序列

`ercot_demand_yoy`(EIA 德州用电 28 日 YoY 的 z)、`ercot_gas_share`、`ercot_rt_price` 三条此前没有任何消费者。
现在:gas_share 与 rt_price 并进 `price_pulse` 的 z 均值,demand_yoy 并进 power_demand 节点的 z 均值
(与 PJM 分区负荷 YoY 同一层)。`--verify` 会打印这三行;rt_price 需 ≥126 点才出值(当前 4/126,累积中)。

### 12.4 顺带修掉的一件

**AEUS 首日没有 PnL/Risk 报告**:9/1 daily 在 20:41 跑,而 `account_aeus.json` 20:44 才由建账生成 →
`portfolio_ledger.daily_update("aeus")` 走"无账户文件"分支返回 0 → 旧闸门 `if _n_led > 0` 把报告子进程整个跳过。
模块与 AISS 完全一致,不是缺件。闸门改为「账本推进了 **或** 当日报告还不存在(且账本已存在)」,幂等。
