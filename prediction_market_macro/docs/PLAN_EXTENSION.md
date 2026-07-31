# PLAN_EXTENSION.md — macro 预测市场 v3 扩展计划(2026-07-30 初稿;2026-07-31 盲测交叉验证后更新)

> **⚡ 实施状态(2026-07-31)**:§21/§22/§23 已按 §23.3 的 0-11 步集中实施完毕,
> 完成/勘误/有意偏离/剩余项的权威总账见 **docs/PLAN_AUDIT.md**(以其为准)。
> 本文档保留为规划历史记录。

> **审计方法说明**:缺口清单经过三轮审计——两轮增量审计(§22 #1-#31)+ 一轮**盲测交叉验证**
> (三个零上下文 agent 从零独立读代码,§21.4-bis 与 §22-补遗二 #32-#48、§22-勘误)。
> 盲测与增量审计结论高度吻合,且盲测额外纠正了 3 处此前写得不准的地方(见 §22-勘误)。

> 本文档是 `docs/PLAN.md`(v3)的**追加扩展**,不修改、不删减 PLAN.md 任何内容。
> 三块新增内容:
> **§21 前端信息架构重组**(只重新分组导航,现有13个方块信息一个不删)、
> **§22 开发缺口总账**(合并历次深度审计,去重后的完整清单,含本次对 §19 的逐项代码核查)、
> **§23 准确率提升路线 v2**(在 PLAN.md §19 基础上补充学术文献支撑的新方法)。
> 状态:仅记录,尚未开工。后续按此文档排期开发。

---

## §21. 前端信息架构重组(仅组织,不删减)

### 21.1 现状——13 个方块的读法(存档,作为用户使用说明)

进入「宏观预测市场」有 13 个可点击方块 + 顶部「近期数据」横条,数据来自后端每天 05:00 ET
自动生成的 JSON 快照。

- **近期数据横条**:不是方块,是快捷入口——按时间排序显示接下来 8 个数据发布/会议,色点=
  已有持仓判断(绿=开仓,灰=观望),点击跳转到对应方块。
- **1. 宏观看板(Board)**:总览。「下次发布」=各系列下次发布时间+倒计时;「各系列状态」
  每行=期数/模型预测/决策(灰=观望,绿=真实开平仓)/净边际(模型公允概率-市场卖价-手续费,
  正数才有交易价值)/规模(纸面美元,非真钱)。数值型系列显示均值±标准差或中位数[5%,95%],
  Fed 类显示概率最高前两档。
- **2. 美联储会议(Fed)**:每次会议一张卡,C26/C25/H0/H25/H26 概率条 + 规则/市场/模型
  三行表(模型=对数池化混合,非算术平均)+ 会后利率梯子概率。
- **3. 通胀(Inflation)**:CPI/核心CPI/CPI同比/核心CPI同比/PCE核心,与看板同布局,过滤成通胀类。
- **4. 就业市场(Labor)**:过滤成初请失业金/非农新增/U3失业率。
- **5. 能源(Energy)**:⚠️ 见 21.4 第1条,提示文案已过时。
- **6. 模型 vs 市场(Divergence)**:分歧度=模型均值与市场隐含均值的标准化距离,>0.15 高亮
  (不代表谁对,只提示值得多看);「腿数」=反推市场曲线用了几档 Kalshi 合约,越多越可信。
- **7. 决策与盯市(Decisions)**:「决策」=最新60条操作流水账;「最新盯市」=当前持仓浮动盈亏。
- **8. 业绩表现(Performance)**:资金余额=Kalshi 模拟账户(恒为 paper),下接分系列持仓表+已结算表。
- **9. 校准(样本外)(Calibration/OOS)**:全系统最关键面板——唯一直接回答"模型行不行"的地方。
  每系列每窗口对比 Brier 分数(模型 vs 市场,0=完美,0.25=瞎猜);红=模型落后继续纸面,
  绿=真赢市场;n 偏小时结论不能太当真(§7-bis 采纳门,13 个系列目前全部未过门)。
  ⚠️ 见 21.4 第2条,`component_gates` 已算出但未渲染。
- **10. 覆盖矩阵(Coverage)**:系列×期数矩阵,绿=已出预测/灰=已排期未到/蓝=决策中/红=漏报。
  ⚠️ 见 21.4 第4条,一致性检查告警混在漏报告警里。
- **11. 风险限额(Risk)**:限额=每笔/系列/板块/总敞口美元上限;情景=当前压力测试值
  (独立±2σ算法,非联合情景——因 DFM 版本上周闸门测试未过基准,按设计保持保守版本,非 bug);
  持仓敞口=当前实占额度。
- **12. 系统总览(Overview)**:纯文字,讲清系统定位、PIT 纪律、每日刷新流程、
  §7-bis 上真钱的双重条件(样本外打赢市场 + 风控限额内)。
- **13. 报告(Reports)**:⚠️ 空占位符,"M8 上线"未实现。

### 21.2 为什么"乱"——根因

13 个方块扁平并列,但实际分属 4 种不同性质,信息架构没有分组:

1. **总览类**(Board、Overview)——两个入口内容重叠
2. **系列过滤类**(Fed、Inflation、Labor、Energy)——同一张看板按类别切了 4 刀,变成 4 个顶层按钮
3. **交易运营类**(Decisions、Performance、Risk)
4. **质量/健康类**(Calibration/OOS、Coverage)——回答"系统行不行"的核心,却和
   `component_gates`、`macro_health.json`、一致性告警三块本该在一起的信息散落在三处

### 21.3 重组方案——13 个方块收成 5 个分组入口

**不删减任何现有信息**,只加一层分组导航(二级 tab),并把散落的三块信息汇聚成一个
"系统健康"入口:

| 新分组 | 收纳的旧方块 | 新增内容 |
|---|---|---|
| **总览** | Board + Overview | 顶部加「系统健康」红绿灯条,消费现有但零展示的 `macro_health.json` |
| **各系列** | Fed / Inflation / Labor / Energy | 合并成一个入口,内部二级 tab 切系列类别(而非4个顶层按钮);同步修 Energy 过时文案 |
| **交易与持仓** | Decisions + Performance | 合并,流水与盯市/累计业绩本就该并排看 |
| **质量与风控** | Calibration/OOS + Coverage + Risk | 合并成"系统靠不靠谱"统一面板:Brier红绿判定 + 补渲染 `component_gates` + 一致性告警独立成小节 + 风险限额 |
| **报告** | Reports | 建议在 M8 真正建好前直接隐藏此入口,而非展示空占位 |

顶部「近期数据」横条不变。

### 21.4 应同步修复的具体问题(独立于 IA 重构,工作量小)

1. **Energy 过时文案**:`someo-park-investment-management/src/components/macro/MacroArtifact.tsx:266-278`
   仍渲染"能源系列尚未建模"(`macro.energyPending`),但后端 `model/energy.py` +
   `model/registry.py`(KXWTIW/KXNATGASW/KXAAAGASW)确认三个能源模型早已注册在跑——纯前端文案未跟进。
2. **`component_gates` 未渲染**:`ops/frontend_export.py:158` 已把 `component_gates`
   写进导出 JSON,但 `src/` 全目录搜索零引用——Calibration/OOS 视图应补一个小节渲染它。
3. **`macro_health.json` 零消费**:文件真实生成且已同步进 `public/data/macro_health.json`
   与 `dist/data/`,但 `src/components/macro/*.tsx` 里搜"health"零命中——应作为 21.3
   表格里"系统健康"红绿灯的数据源接入。
4. **一致性告警未独立**:目前混在 Coverage 面板的「告警」列表里(与普通漏报告警不分),
   应在"质量与风控"分组下单独列一个小节。

### 21.4-bis 盲测交叉验证新发现的前端缺陷(2026-07-31,从零读代码复核,追加 5-11)

5. **[真 bug] 已结算 PnL 永远显示"—"**:`macro_performance.json` 的 settled 行字段叫
   `realized`,但 PerformanceView 读的是 `r.pnl_usd ?? r.pnl` → 4 笔已结算(实际
   +1.38/+1.44/−2.12/−2.40)全部渲染成"—";且该表复用了"Open N"列头标注结算笔数。
6. **Coverage 状态色映射失配**:`STATE_COLOR` 定义了 predicted/scheduled/decided/open/missed,
   但数据实际状态是 passed(57)/scheduled(14)/decided(2)/reconciled(1)——占 78% 的 passed 与
   reconciled 落入默认灰,定义的三种颜色在数据里根本不出现。
7. **Calibration 只渲染 Brier**:`metrics_json` 里已算出的 CRPS(24h/1h)与 `skipped` 字段
   不显示。
8. **Decisions 截断无提示**:200 条只显示前 60,无分页也无"已截断"标记。
9. **全站无数据时间戳**:各 JSON 的 `generated_at` 没有任何地方显示,用户无从判断数据新旧。
10. **顶部横条硬编码副标题** "Fed · CPI · Jobs · Kalshi" 遗漏能源系列;"近期数据"标题实际
    展示的是未来发布倒计时,措辞与内容不符。
11. **Fed 利率阶梯是 ad-hoc 派生**:不是读 `macro_fed.json`,而是从 board 里 KXFED 的
    empirical 分位数数组现场计数反推概率——应在 21.3 重构时给 fed 导出补上原生阶梯数据。

### 21.5 实施优先级建议

21.4 的 4 项修复工作量小、零风险,可独立于 IA 重构先做;21.3 的 5 组重构涉及
`MacroArtifactGrid.tsx` + `MacroArtifact.tsx` 的导航结构调整,工作量较大,建议单独排期、
构建后走一次完整 `tsc --noEmit` + `npm run build:wc` + 预览确认再部署。

---

## §22. 开发缺口总账(合并两轮深度审计,2026-07-30 定稿)

逐条对代码验证,不采信 PLAN.md 文字描述。状态记号:❌NOT DONE / ⚠️PARTIAL / 💀DEAD CODE
(写了但没接线)/ ⏸PLANNED-LATER(计划里本就写明是后续里程碑,不算缺口)。

| # | 模块/项 | 状态 | 证据 | 影响 |
|---|---|---|---|---|
| 1 | `model/fed.py` 绕开 `FeatureStore` | ⚠️PARTIAL | fed.py 已用 FeatureStore(24,140行)但仍有直连SQL(79-99,174-175行) | 单一数据口纪律不彻底,PIT 审计口径不统一 |
| 2 | `research/health.py` 5种异常检测 | ❌NOT DONE | 只实现新鲜度/ladder质量/回放确定性/滚动OOS Brier 4项,PLAN §9.6 要求的 Brier双窗/CRPS+2σ/熵收敛/特征z=4越界/Chronos NaN检测一个没有 | 每日健康巡检名不副实,红灯判定不够灵敏 |
| 3 | `ops/exits.py` 健康降级强制平仓 | ⚠️PARTIAL(2/3) | 边际反转平仓(64-88行)+冻结窗阻止(35-40行)已实现;健康降级平仓只是注释"钩子已就位"(5行),`run()` 从不查 alerts/health 表 | 红灯不会真的触发平仓,风控最后一环缺失 |
| 4 | `ops/risk.py` 无 `circuit_breaker` 函数 | 💀DEAD CODE | 全仓库无此函数;概念以 `exec/kalshi_exec.py:54-57` 查 alerts 表形式存在,但该文件从未被 refresh/decide_all/tick 导入 | 熔断"写了但没插电",不是完全不存在 |
| 5 | `model/gdp.py` | ❌NOT DONE | 文件不存在,PLAN §7 P0/P1 交付项落空,13系列里无GDP | GDP/GDPNow 对照完全空缺,直接影响 §23 C.2-2 的 bridge nowcast |
| 6 | `tests/test_pit.py` | ❌NOT DONE | 不存在;§5-bis.4 要求的4件套(canary/单调性/发布当天/标签vs首版vintage)只有 canary 做了且仅覆盖 claims 一个系列 | PIT 安全网基本空转,是承重结构缺口 |
| 7 | `docs/PLAN_AUDIT.md` | ❌NOT DONE | §17 文档契约要求的文件不存在 | 文档纪律缺口 |
| 8 | `exec/kalshi_exec.py` | 💀DEAD CODE | 真实RSA签名下单逻辑已写全,但 refresh.py/decide_all.py/tick.py 均未导入 | 与"目前只做paper"事实一致,但要知道是"没接线"而非"接了但关掉" |
| 9 | launchd 装机脚本 | ❌NOT DONE | 4个plist已装载运行,但仓库内无可复现的安装脚本 | 换机/灾难恢复无法一键重装 |
| 10 | `macroweekly` 首次真实触发 | ⏸待验证 | 已注册、`launchctl` 显示 exit 0,但日志文件不存在(周日06:30尚未到期),非bug,只是未验证 | DFM周度闸门+周报的自动触发路径尚未见过真实首跑证据 |
| 11 | `features/nowcast_vintages` 表 | ❌NOT DONE | 表结构已建但 0 行数据,GDPNow等外部nowcast摄取完全没做 | 直接卡死 §23 C.2-2 的三源集成 |
| 12 | Chronos 采纳晋升门槛 | ❌NOT DONE | "周度≥8期/月度≥3期双重打赢"规则只在注释描述,无计数/执行代码;只有 DFM 有真正可执行闸门 | Chronos 影子模型无法自动转正,永远停在shadow |
| 13 | 前端专属数据文件 | ⚠️PARTIAL(8/13) | macro_labor/inflation/energy/overview/reports 无专属JSON,现取现凑自 macro_board.json | 后端导出与前端展示耦合度过高,后续加系列易出错位 |
| 14 | 三源 log-pool 集成(§19-2) | ❌NOT DONE | 全仓库0命中ensemble/log-pool代码;`strategy/decision.py::decide()` 的 `market_implied` 形参从未在函数体内使用——死参数 | 目前只有 Fed 一个系列做了 rule+market 二源 log-pool,其余系列没有集成机制 |
| 15 | Kelly 前 isotonic 校准层(§19-3) | ❌NOT DONE | 不存在 `calibration.py`;唯一 isotonic 代码(`strategy/devig.py`)校准的是市场隐含曲线用于套利检测,不是模型预测;Kelly直接消费未校准的 `grid_pmf` | 未校准的过自信直接喂给 Kelly,是 PLAN §19 明确点名的"Kelly 的毒药" |
| 16 | 熵门 + edge_capture(§19-4) | ❌NOT DONE | 分歧门(disagreement gate,阈值0.25)已接线;熵门(entropy gate)0命中;逐strike edge_capture记录0命中 | "选择性下注=有效准确率"只做了1/3 |
| 17 | 误差归因反馈闭环(§19-6) | ❌NOT DONE | `research/health.py::daily_health` 无 \|z\|>2 聚类分析,无特征新增机制 | 结算后学习闭环完全空缺 |
| 18 | LLM结构断点层(§19-8) | ⚠️PARTIAL(仅脚手架) | `analysis/llm.py::news_risk_tags` 存在但从未被下游消费,无方差/均值偏移应用;"postponed"状态仅是注释,从未被赋值;"shutdown"处理0命中 | 大事件(罢工/停摆/飓风)目前对模型完全不可见 |

### §22-补遗(2026-07-31 第三轮全章节扫查新发现,#19-#31)

| # | 模块/项 | 状态 | 证据 | 影响 |
|---|---|---|---|---|
| 19 | **`research/eval.py` 不存在** | ❌NOT DONE | 全仓库 grep `calibration_table`/`drift_check`/`edge_capture` 零命中;§9.5 指定的上线闸门评估器本体没写(唯一可执行闸门是 DFM 自己的 `dfm_bridge.gate_check`) | **§7-bis"上真钱之门"没有评估器**——前端校准面板显示的 Brier 对比来自 backtest 打分,但"何时可转真钱"的正式判定链路是空的;承重级 |
| 20 | `research/backtest.py` 只做打分重放 | ⚠️PARTIAL | `replay_series`(133行)只算 −24h/−1h 的 Brier/CRPS;§9.4 要求的 scan/decide/PnL 全链路重放、roi/pnl_curve/calib_bins/edge_capture、DM检验/bootstrap CI/置换检验全部没有;`research/oos_eval.py`(§15 母版移植项)也不存在 | 回测答不了"这策略赚不赚钱",只答"预测准不准";统计显著性检验(§9.4"标配")完全缺失 |
| 21 | 节奏管线只有 3/6 条 lane | ⚠️PARTIAL | `jobs/scheduler.py:42-43` 只有 release_day/fomc_week/weekly_close;daily_snapshot(KXWTI/美债/FX)、quarterly、annual_watch 三条 lane 未建,对应系列也未注册 | 日频/季频/年频市场整体缺席,与 §8.1 分道设计不符 |
| 22 | 事件窗加密轮询未做 | ❌NOT DONE | 计划要求 T-2h 起 5 分钟快照、发布后 30 分钟快速轮询;实际 tick 固定 900s(plist StartInterval=900),`jobs/tick.py:21-22` 每次只拍一张;`scheduler.py:10` 文档字符串声称"tick 5分钟快照"与事实不符 | 发布窗口内的重定价 edge(§19-9 的 3 分钟重估)实际抓不到,文档还在撒谎 |
| 23 | Fed 声明文本抓取整条缺失 | ❌NOT DONE | 无任何声明文本摄取;`analysis/llm.py:88 fomc_statement_diff` 写好了但零调用者 | M3 的 statement_risk 腿空转;Fed 模型只有规则+市场两路,文本信号缺位 |
| 24 | 日历硬编码不全 + `releases.actual_ts` 死字段 | ⚠️PARTIAL | `ingest/calendars.py:97-115` 缺 BEA_GDP/EIA_NG/EIA_PETRO/MARKET_DAYS,无 `refresh_from_web()` 校验;`actual_ts` 建了字段但没人写入 | 排期漂移/发布延期(postponed)侦测链路是死的——与 §22-18 的 postponed 状态未赋值同根 |
| 25 | 标签双列 y_first/y_latest 未按规格建 | ⚠️PARTIAL | 标签用 `MIN(knowledge_time)` vintage 推导(能用),但 §5-bis.2 规格的双列没建;铁律2"y_first 必须与 Kalshi 结算对账,不符即停该系列"全仓库无此断言 | 标签 PIT 能用但对账保险丝缺失 |
| 26 | `size ≤ 20% depth` 流动性帽缺失 | ❌NOT DONE | 只有绝对 $50/腿深度门(`strategy/decision.py:22`),20% 比例逻辑全仓库零命中 | 铁律5 前半条空转;流动性薄的系列会超吃盘口 |
| 27 | `edge_capture<0.4` 降级 | ❌NOT DONE | 指标本身不存在(见#19),降级规则自然空转 | 铁律5 后半条空转 |
| 28 | 风险限额与计划口径不符 | ⚠️PARTIAL | `ops/risk.py:16-21`:缺 `per_release_day $30` 限额;cluster 限额 $8(比计划 $40 保守——方向安全但口径未对齐) | 限额矩阵与 §12 规格有出入,需明确是"改了计划"还是"漏了" |
| 29 | PnL 结算归因未做 | ⚠️PARTIAL | `ops/pnl.py:4-5` 文档字符串声称有 \|z\| 归因(z<1=运气/z>2=模型问题),实际 `settle_pass`(45-77行)只记盈亏无归因;`report` 无 ROI/命中率/Brier/edge-captured | 与 §22-17 误差归因反馈同根;文档字符串又在撒谎 |
| 30 | PDF 报告未导出前端 | ⚠️PARTIAL | `data/output/reports/macro_daily_*.pdf` 真实生成,但 `public/data/` 下无任何 PDF;前端 `macro_pdfs` artifact 无内容可服务 | Reports 方块空占位的另一半原因:不只是前端没建,后端也没把 PDF 送过去 |
| 31 | `run_vix_forecast` 风险 regime 特征未接 | ❌NOT DONE | §7(b) 计划引入 VIXForecast 只读调用作风险 regime 特征;全仓库无 import | 风险情景层少一路已计划好的输入 |

### §22-补遗二(2026-07-31 盲测交叉验证:三个零上下文 agent 独立重审,新发现 #32-#48)

| # | 模块/项 | 状态 | 证据 | 影响 |
|---|---|---|---|---|
| 32 | **整个 `prediction_market_macro/` 未纳入 git 跟踪** | ❌ | `git status` 显示 `?? ./` | §0-bis"审计=遍历 git status"形同虚设;代码无版本保护,误删无法恢复 |
| 33 | `FeatureStore.frame()` 零调用者;直连SQL不止 fed | ⚠️ | claims.py:43-47、payrolls.py:24-29、u3.py:23-27 也直接 SELECT 原始表;特征证据链表实际不落库 | 扩展 #1:单一数据口违规是普遍现象,不是 fed 个例;§5-bis.4-1 执法机制整体空转 |
| 34 | 模型卡/注册表 3 处虚标 | ⚠️ | registry.py:5 引用不存在的 test_registry.py;cpi 卡列 T5YIE 特征但 cpi.py 未用;fed 卡记 w_rule=0.55 而代码 W_RULE=0.4 | 文实不符,模型卡不可信 |
| 35 | DFM scenario_var 消费链整条死代码 | 💀 | `scenario_var` 只被从未调用的 `run_extended`(frontend_export.py:165)引用;run_extended 的 6 个前端 json 也从不生成 | 即使 DFM 将来过了闸门,升级链也接不上电;"每晚跑 scenario_var"未接入 refresh |
| 36 | 新鲜度硬门(stale→PASS)未实现 | ❌ | decide_all 不检查 staleness;§8.2-5 要求过期数据强制 PASS 降级 | 可能用旧料做新决策,§19-5 此前标 DONE 有误(见 §23.1 更正) |
| 37 | `series_gate` 永远没人写入 | 💀 | kalshi_exec.py:48 读取的行全库无写入代码 | 即使 exec 接线,门检查读到的永远是空 |
| 38 | decision.py 分歧门口径偏离 | ⚠️ | decision.py:60 实为 \|fair−cost\| 而非计划的 \|fair−market_devig\|(注释自认) | 分歧门比较对象不对,极端盘口下门会失灵 |
| 39 | P0 模型相对 §7 规格系统性简化 | ⚠️ | fed 无51次史判别式/有序logit/statement_risk/点阵/油价动量(实为4组硬编码base rates,fed.py:63-69);claims 无状态空间/新闻冲击项;payrolls 无 ADP;energy 无 AAA 传导回归与 EIA 库存方差项 | 各模型实际是"简版",与计划书描述的复杂度差一代——准确率上不去的结构性原因之一 |
| 40 | price-track 整条缺失 | ❌ | 无逐小时盯市时间序列(pnl.py:27-43 只单次快照)、无 macro_pricetrack.json、无前端视图 | §15 移植映射表整行落空 |
| 41 | venues/kalshi 直接 import 母版 | ⚠️ | account.py:24 `from prediction_market.venues.kalshi.auth import ...` | 违反 §15"复制模式不 import 代码"与 §20.8"零 import";FAMILY_TEMPLATE 把它白名单化但 PLAN 未同步改——需明确决策:改计划还是重构 |
| 42 | M7 扩展视图 7 项缺席 | ⏸/❌ | 计划"扩到~20":claims_history/fomc_history/consistency/pricetrack/params/venues/llm 全部没有 | 其中 consistency 独立视图正是 §21.4-4 一致性告警没地方去的原因 |
| 43 | §16.2 共享件未抽取 + MacroFocusContext 缺失 | ⚠️ | 通用件没进 `src/components/shared/`(macro 自带 primitives.tsx 且注释明言不共享);MacroFocusContext/SeriesName 跨视图 popover 未实现 | 与计划"双方共用不复制两份"相反;后续 NBA/足球分支会再复制一份 |
| 44 | maker-first 入场 + 2宽桶结构缺失 | ❌ | edge.py 仅相邻价差结构;无 maker 挂单优先逻辑 | §11 规格两个小项 |
| 45 | 铁律三小项无代码守卫 | ❌ | §20.2"先paper两个print"无计数强制;§20.12 反循环(合成数据禁入训练)仅注释;§20.13 组件劣化自动退出无实现 | 三条铁律靠自觉 |
| 46 | `ops/macro_health.sh` 缺失 + 周报无十分位校准表 | ❌ | 只有 research/health.py;周报缺 §12 要求的校准表 | §15 移植项落空 |
| 47 | PDF 无下载路由 | ⚠️ | 除 #30(未搬运到 public/data)外,server 也无 PDF 下载路由 | Reports 死端的第三个环节 |
| 48 | BLS/BEA 日历只排到 2026-12 | ⚠️ | calendars.py:38-56,计划要求排到 2027 | 2027-01 起 coverage 矩阵会静默断档 |

### §22-勘误(盲测发现此前审计写得不准的 3 处,以本节为准)

1. **#6 test_pit 勘误**:单调性测试其实存在(test_m0.py:80-104),此前说"只有 canary"不准确。
   实际缺的是:发布晨测试、标签(首发vintage)对账测试两类,以及 canary 只覆盖 claims 一个系列。
2. **#13 勘误**:frontend_export 实写 **9** 个文件(此前记 8,漏数了 macro_fed.json);计划要求
   13 个,缺 inflation/labor/energy/overview/**pricetrack** 五个;macro_health.json 是计划外
   多出来的(且无人消费,见 §21.4-3)。
3. **Sidebar 三按钮直达设计**与计划 §16.1 的"双按钮互切"不符——这是本周用户主动要求的重设计,
   **有意偏离,不是缺口**;应回写 PLAN.md §16.1 备注。

**里程碑总判定(M0-M8)**:M0✅(除#6 test_pit)、M1✅、M2✅、M3⚠️(fed+fomc_week 有,
statement_risk/LLM 腿未接线→#23)、M4⚠️(replay/param_grid 有,DM检验/上线闸门/edge_capture 无
→#19/#20)、M5✅、M6⚠️(launchd/限额/日报有,熔断+per_release_day 限额无→#4/#28)、
M7✅(大体,专属数据文件 8/13→#13)、M8✅(FAMILY_TEMPLATE.md 存在)。

**⏸ 计划里明确写"以后再做",不算缺口**:`exec/` 真钱交易(需先过§7-bis)、
`combo_vs_legs()`(无COMBO系列,设计好的空转)、DFM情景VaR升级(闸门未过,设计好的降级路径)、
外国央行/DOTPLOT/零售/房产/TSA/机票等系列(计划本就写明是M0-M8之后的里程碑)。

**结论(2026-07-31 更新)**:数据层/数据库/Chronos影子/DFM闸门/launchd定时/一致性检查/
前端接线/取整卷积/跨系列桥接/新鲜度调度,这些都是真实跑生产数据的硬底子。承重结构级缺口
现在共 **五** 块,按重要性:
1. **§7-bis 闸门评估器本体缺失**(#19/#20)——"何时能上真钱"这个系统的终极问题没有可执行的
   判定链路,回测也答不了赚不赚钱;
2. **PIT 安全测试矩阵**(#6)基本空转;
3. **风控熔断最后一环**(#3/#4/#26/#27)——红灯不阻止交易、流动性帽和 edge_capture 降级空转;
4. **每日健康巡检 5 种异常检测**(#2)没做;
5. **三源 ensemble + isotonic 校准层**(#14/#15)完全没有。
另有两处**文档字符串撒谎**需要当 bug 修:`scheduler.py:10`(声称 5 分钟快照)与
`ops/pnl.py:4-5`(声称 z 归因),代码与注释不符比缺功能更危险。

---

## §23. 准确率提升路线 v2(扩展 PLAN.md §19)

### 23.1 §19 九项的真实落地状态(本次逐条代码核查,不采信文档描述)

| # | PLAN §19 原项 | 状态 | 证据 |
|---|---|---|---|
| 1 | 未取整print+取整算子卷积 | ✅DONE | `model/cpi.py::_predict_mom` 用未取整FRED指数;`model/common.py::grid_pmf`(93-122)做卷积;`ops/predict_all.py:63`接入 |
| 2 | 三源log-pool ensemble | ❌NOT DONE | 见§22-14 |
| 3 | isotonic校准层(Kelly前) | ❌NOT DONE | 见§22-15 |
| 4 | 选择性下注(熵门+分歧门+edge_capture) | ⚠️PARTIAL(1/3) | 见§22-16 |
| 5 | 新鲜度纪律 | ⚠️PARTIAL(盲测降级) | 调度偏移与数据接入有(scheduler.py T-1h/T-10m;AAA经GASREGW;RB期货),但**定稿前无 staleness 硬门**——decide_all 不检查数据过期,§8.2-5 的 stale→PASS 降级未实现(见§22-36) |
| 6 | 误差归因反馈 | ❌NOT DONE | 见§22-17 |
| 7 | 跨系列桥接 | ✅DONE | `model/pce.py`桥接回归;`model/payrolls.py`claims4周均线;`model/cpi.py::_gas_effect`RB期货 |
| 8 | LLM结构断点层 | ⚠️PARTIAL(脚手架) | 见§22-18 |
| 9 | 时点套利 | ⚠️PARTIAL(盲测降级) | YoY确定性换算(`model/cpi.py::predict_yoy`)与PCE重定价已实现;但**3分钟重估窗只在 scheduler.py:13 定义了任务名,没有处理器**,发布后快轮询未实现(与§22-22 事件窗加密同根) |

**净结果**:纯模型侧技术(1/5/7/9)扎实落地;涉及外部数据融合、校准、反馈闭环、LLM注入的
四项(2/3/6/8 + 4 部分完成)是接下来提升准确率的主战场——这也是 23.2 新方法要补的口子。

### 23.2 补充新方法(学术文献支撑,5类,均为增量可接入,非重写)

**1. 自适应/加权动态集成权重**(直接补 §19-2 的缺口)
核心思想:固定 0.4/0.6 权重假设两路预测在任何时候同样可靠,但在数据意外(如CPI爆冷)时会
失真。Bates & Granger (1969) 的经典最小MSPE组合、Elliott & Timmermann (2005) 的
regime-switching动态权重、以及"剔除最差再组合"的trimmed-mean都提供了比固定权重更稳健的方案。
**可落地**:新增一张SQLite表按系列滚动记录各源的历史平方误差,walk-forward每步按
`w_i ∝ 1/MSPE_i`(60天滚动窗)重算log-pool权重,设上下限防止某一路权重归零;
误差超阈值时临时切换为trimmed-mean(直接剔除该路)而非继续log-pool它。一张新表+一个函数,
非重写。

**2. Bridge equation / MIDAS nowcast**(直接补 §22-11/§22-5 的GDP空缺)
核心思想:GDPNow类方法不等目标数据发布,而是把高频代理数据(周度claims、月度零售)通过简单
回归"桥接方程"实时映射到低频目标(GDP/CPI/NFP),MIDAS回归是Kalman滤波混合频率模型的轻量版,
参数少、不需要状态滤波,适合小系统。**可落地**:在每个系列发布前,用已经在拉的FRED高频代理
(claims/ADP/ISM就业分项/Redbook零售等,DFM已经在用)做一次多项式(Almon)加权滞后的OLS
桥接回归,产出第二路独立nowcast,作为23.2-1里三源log-pool的第三源——只需
`statsmodels`做加权OLS,不需要新数据源。

**3. 预测市场微观结构信号**
核心思想:devig后的中点丢弃了订单簿信息。价差宽度反映市场不确定性/流动性薄弱(应降低该路
权重),订单簿失衡反映短期方向压力,Kalshi/Polymarket已有实证的favorite-longshot bias
(低价合约系统性被高估)会直接污染市场隐含概率这一路输入。**可落地**:(a)把价差宽度并入
现有熵门/分歧门,价差过宽时额外标记该系列市场输入不可信;(b)用自家历史结算数据拟合一条
分段线性修正曲线(`IsotonicRegression`,复用现有isotonic依赖),在市场概率进log-pool前先
做favorite-longshot偏差修正,与23.2-1的动态权重是两个独立小模块,不冲突。

**4. Conformal prediction 区间校准**(补23.2-1/isotonic的互补层)
核心思想:isotonic只校准边际概率,不提供有限样本覆盖保证,数据稀疏(宏观发布约月频)时容易
失真。Conformal prediction用一批"非一致性分数"给任意预测器套上分布无关的覆盖保证,时间序列
变体(Adaptive Conformal Inference, EnbPI)处理序列相关性问题。**可落地**:walk-forward回测
本就在SQLite里按时间顺序存了(预测,实际)对,新增一个小模块从这份日志滚动算非一致性分数,
用ACI的简单更新规则 `α_t+1 = α_t + γ(α - err_t)` 推导自适应区间宽度,作为熵门之外的第二道
仓位节流阀——不是替代isotonic,是校准"对校准结果的信心"。

**5. LLM结构断点层升级**(直接补 §22-18 的脚手架未接线问题)
核心思想:不止是关键词打标,把新闻文本经LLM结构化输出成"事件类型/受影响系列/预期方向/
严重度1-5",再把严重度映射成该系列未来N天的方差膨胀系数或均值偏移。**可落地**:复用已有的
`analysis/llm.py::news_risk_tags` 和 `event_risk_heartbeat.log` 管道(不建新基建),加一张
`event_flags` 表存严重度打分,把严重度接进现有熵门阈值(严重事件期间临时提高熵门/分歧门
敏感度)——这是把已经写好但零消费的代码真正接上电,而非新写一套。

### 23.3 建议实施顺序(2026-07-31 更新,纳入第三轮扫查 + 盲测发现)

0. **立即项(零风险,当天可完成)**:把 `prediction_market_macro/` 纳入 git 跟踪(#32,当前
   整个目录未受版本保护);修 Performance 已结算 PnL 字段名 bug(§21.4-bis-5,一行改动);
   修 Coverage 状态色映射(§21.4-bis-6);修 3 处模型卡虚标(#34)与 2 处撒谎 docstring。
1. **`research/eval.py` + backtest 决策/PnL 重放**(#19/#20,新升为第一优先):没有闸门
   评估器,后面一切准确率改进都无法被正式判定"有没有用"。交付:`eval.py`(calibration_table/
   drift_check/edge_capture/§7-bis 判定)+ backtest 补 decide/PnL 重放与 DM 检验/bootstrap CI。
   这一步同时解锁 #27(edge_capture<0.4 降级)的前置指标。
2. **补死代码/半成品接线**(工作量小、风险低):`exits.py` 健康降级平仓真代码、
   `circuit_breaker` 真正接入 decide 路径、`test_pit.py` 补全4件套并扩到全部P0系列、
   `research/health.py` 补全5种异常检测、`size≤20%depth` 流动性帽(#26)、修两处撒谎的
   docstring(scheduler.py:10 / pnl.py:4-5)。
3. **三源ensemble + isotonic校准层同批做**(§19-2/§19-3/23.2-1/23.2-2 是同一个架构改动,
   一起设计成本更低):新建 `strategy/calibration.py`(Kelly前isotonic)+ ensemble权重表
   (动态log-pool)+ MIDAS bridge nowcast(第三源)。改完由第1步的 eval.py 走 walk-forward
   验证 Brier 是否真的改善。
4. **熵门 + edge_capture 消费**(§19-4/§22-16),与第3步共用的 gate 框架顺带扩展。
5. **LLM结构断点接线 + Fed声明文本摄取**(23.2-5/#23),复用 `news_risk_tags`/
   `fomc_statement_diff` 现有代码接上电,顺带补 releases.actual_ts 写入与 postponed 状态(#24)。
6. **事件窗加密轮询**(#22):T-2h 5分钟快照 + 发布后快轮询,否则 §19-9 的发布窗 edge
   实际抓不到;涉及 tick/plist 改动,独立小项。
7. **微观结构信号 + conformal prediction**(23.2-3/23.2-4),放在1-6验证出实际 Brier 改善
   后再做,避免同时改太多变量导致归因困难。
8. **`model/gdp.py` + nowcast_vintages 摄取 + 缺失 lane 补建**(#5/#11/#21):GDP 系列本体、
   daily_snapshot/quarterly lane,与第3步并行但独立排期(新数据源接入,遵循§5-bis PIT全套)。
9. **PnL 归因 + PDF 前端导出**(#29/#30):结算 z 归因喂给误差反馈闭环(§22-17),PDF 同步进
   `public/data/` 让 Reports 方块有内容可渲染(配合 §21.3 的报告分组)。
