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

---

## §24. Polymarket 策略文对照与深化路线(2026-07-31,来源: coinsbench "Top 10 Polymarket Trading Strategies")

逐条对照 10 个策略对 macro 模式的适用性。结论:4 条已内建、2 条不适用、**4 条是真正的深化机会**(按价值排序 A-D)。

### 已内建(印证现有设计,无需动作)
- **#4 规则/结算边缘**(headline≠resolution):我们的 settle_source 逐条 rulebook 核实 + strict/gte 语义 + 未取整卷积正是此策略的系统化版本——文章把它当"边缘",我们把它当地基。
- **#9 Mention-market No bias**:本质是 favorite-longshot 偏差,我们的 FL 修正图(§23.2-3b)已统计化处理。
- **#1/#2 的侦测半边**:YES+NO<1 与 bundle<1 的侦测 = consistency 的 MONOTONE-ARB/PARTITION-ARB 扫描(每快照必跑)——但只报警不下单,见深化 A。

### 不适用
- **#10 鲸鱼跟单**:Kalshi 无公开钱包/排行榜。
- **#7 跨平台套利**:方向对但需接入 Polymarket 宏观市场(新 venue 全套 + 双腿同时成交风控);列为远期 roadmap,可复用 prediction_market/ 的 PM 基建。

### 深化机会(按预期价值排序)

**A. 把已侦测的免费套利真正下单(#1/#2 的执行半边)** — 价值最高、风险最低
现状:consistency 每天真实抓到 MONOTONE/PARTITION-ARB(告警可见)但零执行。
深化:新增 `strategy/arb.py` 执行路径——束买(YES_i + NO_j 或全 bundle),
零模型风险(数学锁定),独立小额度(如 $2/束),独立 kind='arb' 台账。
额外收益:这是系统里唯一"确定赚"的钱,且为 edge_capture/滑点校准提供真实成交样本。
风控:深度门 + 手续费前置(净差>fee 才动)+ 铁律5 流动性帽照常适用。

**B. 发布后狙击(post-print sniper,#3 催化剂动量的宏观特化)** — 第二优先
宏观独有优势:发布后 print 是公开确定值(BLS/BEA 官网 8:30 挂出),但 Kalshi
报价收敛需要分钟级时间;此窗口内结算方向已确定的 legs 若仍 mispriced ≈ 无风险。
基建已备:事件窗加密轮询(±10min 1 分钟快照)+ T+3m reassess 任务 + releases.actual_ts。
深化:reassess 处理器内加"print-known 分支"——若 fred 首印 kt ≤ now(或 fed_text
已抓到声明),对照结算规则算出已确定 legs,吃仍在旧价的一侧;kind='snipe' 独立记账。
风险:结算规则误读(铁律2:每系列先 paper 两个 print 验证)+ 数据源比 Kalshi 慢的场景要探测(claims FRED vintage 有延迟时禁用,改抓 DOL 官网?v0.1 仅在 kt 确认后动)。

**C. 期限结构一致性(#5/#6 的宏观版)** — 中期
同系列跨期(Fed 会议曲线、CPI 月度序列、claims 周序列)模型隐含曲线 vs 市场曲线:
新增 consistency 第五件套 `term_structure()`——模型说 8 月与 9 月 CPI 相关(基数效应
确定性),市场曲线若与模型曲线形状背离超阈值 → 告警 + (P2) 对冲对交易(买错价期
卖对价期,方差最小化 hedge ratio 复用 pce 桥回归框架)。

**D. 年化收益率视角的 favorite 策略(#8)** — 与现有门互补
现状:min_net_edge=0.04 绝对值门会拒掉 95¢ 买入 2-3¢ edge 的高确定短久期机会,
但那类机会按年化看可能极优(5.2%/72h)。
深化:decide() 增加第二条准入路径——`edge_per_day = net_edge / max(days_to_settle,0.5)`
超阈值(如 0.008/天)且 fair>0.90 时放行,尺寸独立小帽;与 FL 修正互洽(FL 统计上
说明 favorites 被低估,正是此策略的学术根基)。

### 实施顺序建议
A(1 天,零模型风险)→ B(2-3 天,需两 print 验证期)→ D(半天,decide 小改)→
C(consistency 新件套先侦测后交易)。全部先 paper,过各自小闸门再谈规模。

---

## §25. 提高胜率路线 v3 —— 逐笔置信度 + 逐系列建模 + 流优先级(2026-08-05,用户指令)

**为什么有这一节。** #129 的结论是"用预测模型在这个盘口上做不出盈利"。那个结论**没有被推翻**,
但它是在一个**从未被单独优化过的配置**上得出的:14 个系列合在一起、混合流的优先级照抄自足球、
参数选择器 75 天里采纳 0 次、回测里没有任何提前平仓、也没有任何逐笔"要不要下这一注"的判别。
本节把这四件事逐一变成可测项。**它不是推翻 #129,是补齐 #129 没覆盖的自由度。**

### §25.0 立项时已测到的事实(全部来自 `d75:model:end2026-08-04`,勿重测)

| 事实 | 数据 | 出处 |
|---|---|---|
| argmax 买的是**模型**的 favourite,市场只有否决权 | `st_a = max(cands, key=fair)` 然后 `if fair > cost: 弃` | walkforward.py:516-522 |
| hybrid 优先级 = **edge 优先**,从未验证 | `hybrid = trades + [argmax if k not in opened]` | walkforward.py:589 |
| 两条流同时触发 21 次,**选中的结构 0/21 相同** | edge 腿 19.0% 胜/−42.91%;argmax 腿 76.2% 胜/−11.02% | §25.0 实测 |
| argmax 单独跑优于 hybrid | argmax −9.62% (76.5% 胜) vs hybrid −23.50% (47.1% 胜) | streams |
| 但**三等分不稳定** | 1/2 段 argmax 优先赢,**第 3 段反转**(edge +27.8% vs argmax +13.9%),靠一笔 GDP +$5.03 | §25.0 实测 |
| 参数选择器 75 天**采纳 0 次** | `param_rescores: 54, params_adopted: 0`;`dsr.MIN_OBS=12`,单系列最多 11 事件 | gate_stats |
| 回测**没有平仓模型** | `_settle_struct` 只算 0/1 结算;`ops/exits.py` 的三条规则一条都没模拟 | walkforward.py:439 |
| 亏损高度集中在 5 个系列 | claims(6笔/17%胜/−87.7%)+ CPIYOY/U3/PAYROLLS/CPICORE(各2笔/**0% 胜**/≈−105%)= 8 笔吃掉 86% 的总亏 | by_series |
| 能源三兄弟接近打平 | WTIW 10笔/70%胜/−19.2%,NATGASW 11笔/55%胜/−18.7%,AAAGASW 6笔/50%胜/−6.1% | by_series |
| paper 决策**没有逐系列开关** | `series_gate` 只被 `exec/kalshi_exec.py:48` 读,决策路径不看 | grep |
| skill 门已经在做粗粒度的事 | 75 天里 `skill_blocked: 54`, `skill_defensive: 64` | gate_stats |

### §25.1 纪律(先于任何实现,违反即作废)

1. **本窗口(2026-05-21..08-04)只用于"发现",不用于"判定"。** 任何在它上面调出来的东西,
   必须写进 `docs/PREREGISTER.md`(新建)并在 **8/5 起的前向实盘**上判定。#126(`:nofav`)
   是站在这里的反面教材:样本内大幅改善 + 有机制解释,持出第三段不复现。
2. **三等分持出是最低门槛,不是充分条件。** §25.0 里 argmax-优先在 2/3 段赢、1 段反转,
   这种强度**不够上线**,只够做前向候选。
3. **每加一个自由度,记一次多重性。** DSR 的 `n_trials` 必须真实反映本节试过的所有变体,
   不是只报最后那个。
4. **展示面不动。** 8/5 起的实盘段是唯一的判定证据;回测段永远标"回测"。
5. **禁止把 §25 的任何改动追溯到已 freeze 的展示段。** 要重 freeze 必须换 config_hash 并说明。

### §25.2 P0 — 先修使评估本身失真的 bug(不改策略,必须先做)

**a. `pnl_score._staked` 的毛名义 bug(#125 的孪生)。**
`research/pnl_score.py:179` `sum(l.price for l in st.legs) * count` —— 与 walkforward 2026-08-05
刚修掉的完全同一个错误(生产 `decide()` 记的是 `count * st.fill_cost(count)`,bucket 是净借记
`sum(px)−1`)。这个函数是**参数选择器的目标函数**,分母吹大 ⇒ 参数排序失真。改成
`st.fill_cost(count) * count`,加与 `test_walkforward_staked.py` 同构的测试。

**b. `pnl_score` 跑的不是生产策略(#125 本体)。**
`wf_gates()` 注释仍写着"db-state 闸门 fit 在本窗口所以排除",但 `research/pit_gates.py` 已经
解决了这个问题(逐日从严格更早的 close 重建)。而且 `event_pnl` 传 `market_implied=None`,
在 #130 修复之后这意味着 sanity 门参照物变成 `st.cost` —— 和 walkforward/decide_all 都不一致。
**修法:让 `pnl_score` 复用 `pit_gates.GateHistory` 和 walkforward 的 `_implied` + #130 规则。**
判据:同一事件、同一天,`pnl_score.event_pnl` 与 `walkforward` 必须给出**逐位相同**的
(desc, count, staked, realized)。新增 `tests/test_pnl_score_matches_walkforward.py` 钉死。

**c. `_stream_summary` 之外补 per-series 汇总进 metrics_json。**
现在 `by_series` 只覆盖 edge 流。hybrid/argmax 的 per-series 要每次 run 都落盘,
否则 §25.4 每次都要手工重算。

### §25.3 P1 — 逐笔置信度模型(用户核心诉求)

**定位:这不是第 15 个预测模型,是一个「这一注该不该下」的二元判别器。**
它预测的不是宏观数据,而是**这一笔下注会不会赚钱**。目标变量是已知的:每笔历史模拟下注的
`realized > 0`(或直接回归 `realized/staked`)。

**a. 特征(全部 PIT,全部已在 DB 里,不新增数据源)**
按笔可得、且在下注当刻就能算出来的量:
- `fair`, `cost`, `net_edge`, `fair − cost`(分歧度 —— #129 §4 已证这是**最强的单变量**:
  一致时 −1.58%,两侧背离恶化到 −16.5%/−6.7%,所以它必须进模型,但符号是**反的**:
  分歧越大越不该下)
- `spread`(median_spread)、`ask` 绝对水平、`st.kind`(single/bucket)、腿数
- `lead_days`(距结算天数)、`entropy_norm`(模型 pmf 熵)
- **该系列当日的 skill_ratio / conformal_factor / 是否 defensive**(`pit_gates.GateState`
  已经算好,直接取,零额外成本)
- 该系列**滚动 N 笔**的实现胜率与 ROI(严格用更早结算的)
- `stream`(edge / argmax)—— 这一维本身就能学出 §25.0 那个优先级结论

**b. 逐系列 vs 汇总 —— 两个都建,让数据说话**
用户要求"14 个 bet 各自训练"。数据现实是**单系列最多 11 个可交易事件**(#120),
逐系列训练必然过拟合。所以:
- **层次化(hierarchical)**:一个汇总模型 + 每系列一个偏移项,偏移量按该系列样本量收缩
  (n 小 ⇒ 收缩到汇总)。这是"逐系列"在 n=11 下唯一诚实的实现方式。
- 同时保留**纯逐系列**版本作对照,报告它的过拟合幅度(样本内 vs LOEO)。
- **不要**用普通 K-fold。用 **leave-one-event-out + 按 close 时间前向**(LOEO-forward):
  训练只用严格更早结算的事件。这与 `pit_gates.asof` 同一套契约。

**c. 模型类别**
先做**逻辑回归 + 单调约束**(分歧度单调降、spread 单调降),不做树模型:n≈50-70 笔,
树会记住 GDP 那一笔。系数必须可读、可写进 plan、可被质疑。

**d. 阈值 = 这才是产出**
输出 `p_profit ∈ [0,1]`,低于阈值不下注。阈值**不在本窗口上调** —— 按
"期望净 ROI ≥ 0" 的解析条件定(用 §25.0 的 −2.6% 净吃单成本作基线),再前向验证。

**e. 拒绝的形态也要记账**
`decide()` 已有 `pass` 台账。置信度否决必须写成新的 `reasons` 项
(`confidence_gate:0.31`)而不是静默跳过,否则前向期无法回答"它挡掉的那些本来赚不赚"。

**f. 失败判据(先写死,防止事后找补)**
如果层次化置信门在 LOEO-forward 下**不能**把 hybrid ROI 从 −23.5% 提到 ≥ −5%,
或它挡掉的样本实现 ROI 与放行样本**不显著不同**(事件聚类 bootstrap CI 跨零),
则本条路记为证伪,写进 TRADEABILITY_129 的续章,不再重试。

#### §25.3 已实现(2026-08-05)—— 设计取舍记录

代码:`research/confidence.py` + `tests/test_confidence.py`(17 测试)+ `walkforward` 的
特征发射。**登记在 PREREGISTER PR-5,判据写在看结果之前。**

**(1) 训练集从哪来 —— 不新建管道,让回测顺手吐出来。**
`walkforward._trade_row` 现在每笔额外写一行 `feature_rows`,含下注当刻可知的 8 个特征 +
标签。这样训练集与被评估的策略**逐笔同源**,不存在"训练用一套重放、评估用另一套"的裂缝。

特征行**不进 trade row**。理由与 §25.5 的 `held_paths` 一样:`freeze_track` 把 trade row
原样拷进对客户公开的 track payload,任何加进去的字段都会被发布。两者改为写进独立的
`walkforward_features` experiments 行(同 config_hash),既不撑大 headline 行,又不用为了
拿训练集重跑 50 分钟。

**(2) 为什么是"闸门"不是"选边规则"—— 以及那张分歧表其实不能用。**
立项时的理由是:分歧表三格全负、"一致"最不亏 ⇒ 分歧是噪声,所以要闸门不要选边规则。
**闸门这个定位站得住,但它的证据换了一个。**

在 §25.2a 的 `staked` 三处修复之后重算同一张表,**顺序颠倒**:

| 分歧 | 立项时(bug 前) | 重算(bug 后) | n |
|---|---|---|---|
| 模型 ≪ 市场 ≤−10% | −16.54% | **−1.99%** | 6 |
| 一致 | −1.58% | **−53.08%** | 19 |
| 模型 ≫ 市场 ≥+10% | −6.66% | **−5.31%** | 26 |

而且更根本的问题是**这条轴本身不干净**:cost = 0.90 时 `fair − cost ≥ +0.10` 需要
fair ≥ 1.00,算术上不可能。所以"一致"格**结构性地**收走了所有贵的热门 + 所有便宜的长尾 ——
正是单笔亏损 = −100% 本金的两个价格区。逐笔看进去也确实是这两类:
cost 0.10–0.11 的长尾归零、cost 0.78–0.91 的热门输掉。

**这张表测的是价格水平,不是分歧。** 所以 `abs_div` 的负号先验被撤回(PREREGISTER PR-5
修订 A,在 LOEO 评估跑之前做的),改为放开;`cost` 保留正号约束留在模型里,于是
`abs_div` 的系数是**控制了价格之后**的读数 —— 这才是这张表想问却问不出来的东西。

闸门定位不变的理由也变简单了:真正解释亏损的是**系列**(5 个系列 14 笔承担绝大部分),
不是分歧方向。选边规则对此无能为力,闸门可以。

**(3) 8 个特征里 6 个符号事前定死,2 个故意放开。**
符号约束不是装饰,是在 n≈70 时唯一能防住"学出一条自信的反向规则"的手段。用 L-BFGS-B 的
箱约束实现 —— 标准化只做正数缩放,所以标准化系数上的符号约束等价于原始系数上的。
单测 `test_every_signed_coefficient_obeys_its_prior` 用**故意造反**的数据喂进去,断言
每个系数都被夹到 0(实测:8 个全部为 0 —— 模型拒绝反向学习,而不是自信地学错)。

放开的两个:`is_argmax`(把 §25.6/#137 的问题**搬进交叉验证里问**,比在 21 个打架事件上
肉眼看强)和 `abs_div`(先验依据被撤回,见上)。两个都有单测钉住"能双向移动"——
一个残留的约束会让模型永远说不出修正后数据真正显示的方向。

**(4) "14 个 bet 各训一个"—— 层次化是唯一诚实的实现。**
§25.7 已测:最大系列 11 个可交易事件。14 个独立拟合就是 14 次记忆。所以:
汇总斜率 + 逐系列**截距偏移**,偏移带 L2 收缩,`SERIES_TAU=0.75` 由"在
`skill.MIN_PAIRED`=6 笔时收缩到一半"解析推出(logistic 单点 Fisher 信息上界 0.25,
`n·0.25/(n·0.25+2τ)=1/2` 在 n=6 ⇒ τ=0.75)。**没有一个常数是在本窗口上调出来的。**
同时 `fit_per_series()` 把 14 个独立模型也拟合出来**作对照**,`evaluate()` 报告它们的
样本内 vs LOEO 落差 —— 那个落差就是"逐系列训练会过拟合"的证据本身,不是本文档的断言。

**(5) 验证是 LOEO-forward,不是 K-fold。**
给事件 E 打分的模型,只用**严格更早结算**的交易训练;同一结算日的事件共用一个模型
(结算是成批落地的,拆开就等于让上午的结果告诉下午)。这与 `pit_gates.asof` 同一套契约。
单测直接 monkeypatch `fit` 记录每折的训练切片,逐折断言无一条 settle ≥ 被打分日。

**(6) 阈值不是调出来的,是算出来的。**
`E[roi] = p·w + (1−p)·l ≥ 0 ⇒ p ≥ −l/(w−l)`,其中 w、l 是**该折训练段**赢/输交易的平均
ROI。每折自算自己的阈值。单测 `test_p_star_does_not_look_at_the_scored_fold` 断言阈值
逐折变化(常数就说明它是全样本算的)。

**(7) 两条反作弊约束,各有单测钉住。**
- **只能减不能加**:没有任何分支能开出现有门拒绝的仓,也不改仓位大小。下行有界。
- **未训练折必须放行**(abstain ≠ block)。这条最要命:如果把"还没训好"读成"挡掉",
  前 20 笔全被扣掉,ROI 会凭空变好,而提升 100% 来自不下注。

**(8) 被挡的 edge 腿会把该事件的 argmax 腿顶上来。**
`_hybrid()` 按 `decide_all` 的真实规则重建 —— argmax 腿独立于 edge 腿,edge 被挡不等于
该事件不下注。不这样重建,就会把"换了一腿"记成"避免了一笔亏损"。

### §25.4 P2 — 逐系列下注开关(比置信模型简单,先做)

置信度模型是逐笔的;这个是逐系列的,粒度粗但样本量友好得多,而且 §25.0 显示亏损
高度集中,所以**期望收益大而实现风险小**。

- 新建 `strategy/series_enable.py`:按该系列**严格更早**结算的滚动样本,给出
  `enabled / defensive / disabled`。判据用现成的 `skill_ratio`(model Brier / market Brier)
  **加上**实现 PnL,而不是只看 Brier —— #119 已经证明 Brier 与美元脱钩。
- 接进 `decide_all` 与 `walkforward` 的**同一个位置**(skill BLOCK 之后、predict 之前),
  两边共用一个函数,禁止各写一份。
- ⚠️ 与 **#124** 冲突要一并解决:skill 门现在把 KXAAAGASW 挡了约 20 周,而证据说门是对的
  (0/11, ratio 8.35)。新开关不能变成"绕过 skill 门"的后门。
- 前向判据:被 disable 的系列在实盘期若开始跑赢,必须能自动 re-enable(带滞回,防抖)。

#### §25.4 已实现(2026-08-05)—— 设计取舍记录

`strategy/series_enable.py`(纯函数,无存储)+ 三处接线。判据**没有**按原计划用
`skill_ratio`,只用**实现美元**:skill 门已经在管 Brier 那一路,再叠一层同源判据
只是把同一个信号数两遍;#119 的结论正是 Brier 与美元脱钩,所以这个门要问的是
"这些注赚不赚钱",不是"预测准不准"。

**信号源 = `eval.decision_replay` 的逐笔交易,不是实盘 ledger。** 两个理由:
1. 可 PIT 切片 —— `pit_gates.GateHistory` 本来就为 capture 记忆加载这份列表,
   回测于是能用**严格更早**的成交重建当日状态,零泄露且与实盘同一条规则;
2. **关掉之后证据仍在累积。** 用实盘成交喂的门是吸收态:关掉 → 不下注 → 没有新证据
   → 永远关着。replay 不需要我们真的下过注,所以被关掉的系列照样产出能把它打开的证据。
   这条是本模块最重要的性质,任何重写都必须保住(`test_the_gate_is_not_absorbing`)。

**只能减,不能加。** `blocked()` 只返回"理由"或 None,模块里**故意没有** `enable()`。
接线位置在 `decide_all` 的 skill BLOCK **之后**、predict 之前,后果相同(整个事件跳过,
argmax 腿一并抑制)。放在 skill 之前不会改变哪些注会发生,但会让"两个门都拒绝"的事件
在 ledger 里记下较弱的那个理由 —— 而理由字符串是"这笔为什么没下"的唯一记录。这样也
从结构上堵死了它变成"绕过 skill 门的后门"(原 §25.4 的 #124 担忧)。

**四个常数全是借来的,一个都没拟合**:`OFF_ROI=0`(盈亏平衡,唯一非任意点)、
`ON_ROI=0.026`(本 book 实测净 taker 成本,滞回带)、`MIN_N=6`(`skill.MIN_PAIRED`)、
`WINDOW=12`(`dsr.MIN_OBS`)。已在 `docs/PREREGISTER.md` PR-4 预登记,**含失败判据**:
若窗口内触发 0 次或近乎全触发,正确反应是如实报告而不是去调这四个数。

**已知代价:换手慢。** WINDOW=12 在周频系列上约 3 个月才换完一轮,即被关的系列在变好
之后仍会关一阵 —— 这就是 #124 在另一个门上的同一个毛病。取舍是有意的:更短的窗口更吵,
而一个吵的逐系列开关比一个粘的更糟(它恰好会关掉刚刚运气不好的系列)。

**顺带修掉第三处 `staked` 总名义额 bug**:`eval.decision_replay:231` 与
`walkforward._trade_row`、`pnl_score._staked` 同源同错。`gate_verdict` 只测 `roi > 0`
所以判决不变,但报出来的 ROI 一直偏善良,而现在 `series_enable` 要 fold 这个数。

### §25.5 P3 — 把持仓路径放进回测(回答"是不是有一阵子赚钱没出掉")

现状 `walkforward._settle_struct` 只有 0/1 结算。要回答这个问题必须重建**持仓期间的逐日
报价**,而 candles 表里已经有(`_candle_quote` 已经能按任意 asof 取)。

- 新增 `walkforward` 的**持仓轨迹**:开仓日到结算日,逐日记录 mark-to-market
  (`yes_bid`/`yes_ask` 中点,按持仓方向)。落进 trade row 的 `path` 字段。
- 有了 path 才能测:(i) `ops/exits.py` 的 `EXIT_EDGE=-0.06` 反转平仓在回测里值不值;
  (ii) 有没有"最高浮盈 > X 却最终亏损"的系统性形态;(iii) 止盈规则是否有效。
- ⚠️ **诚实边界**:日线中点不是可成交价,`exits.py` 用的是 `bid − SLIP`。
  path 只能用于**诊断**,不能直接拿它的 PnL 当可实现收益。这条必须写进代码注释。
- 先只出诊断表,**不加止盈/止损规则**。加规则要走 §25.1 的预注册。

#### §25.5 已实现(2026-08-05)

`walkforward._mtm_path()` + `_held_analysis()`,**不改变任何一笔交易**(只往输出 dict
加字段)。两点与原计划不同,都是往严格的方向改的:

1. **主口径用 bid 不用中点。** 原计划写"中点",但 `ops/exits.py` 出场用的是
   `bid − SLIP`,而且它**明确拒绝**在无双边报价时用中点(KXCPIYOY-26SEP-T3.4 报
   0.18/0.98,中点 0.58 没人接)。所以 `mtm` 按 bid−SLIP−两侧 taker fee 计,
   `mtm_mid` 另存一列 —— **两者之差本身就是"这笔浮盈能不能拿到"的答案**。
2. **`n_observed` 与 `n_trades` 分开报。** 开仓日距结算不到一天的交易根本没有逐日观测,
   把它算成"从没绿过"就是用开仓时机的假象去回答用户的问题。所有比例都以 `n_observed`
   为分母。

`oracle_gain` = 每笔在自己的最高日 mark 出场能多赚多少,用 `max(peak, realized)` 所以
**不会为负**(只跌不涨的那笔贡献 0,而不是一个负"收益",否则它会抵消真实的回吐、
上界就不再是上界)。这是**事后上界,任何规则都够不到**:一天一个观测、还是蜡烛收盘价。
它只回答一件事 —— 这里到底有没有东西可追;如果 `oracle_gain` 很小,那用户问的
"是不是赚钱没出掉"答案就是**没有**,任何出场规则都救不了。

### §25.6 P4 — 混合流优先级(在 P1-P3 之后,因为置信模型可能直接替代它)

§25.0 已测:两条流打架 21 次,现行规则每次选差的那条。但三等分不稳定。
- **不单独实现**。让 §25.3 的置信度模型把 `stream` 当特征学 —— 如果 argmax 真的更好,
  模型会学出来,而且是带收缩的、有 LOEO 验证的,比手工翻转优先级严谨。
- 若置信路证伪(§25.3f),再回来把优先级翻转做成 K=1 预注册前向测试。

### §25.7 参数选择器:承认 n 不够,不要假装

`params_adopted: 0` 不是 bug,是 `MIN_OBS=12` 遇上 63 个可交易事件。**不要动 MIN_OBS。**
可做的只有两件:
- (a) 修 §25.2 的两个 bug,让选择器至少在打分**正确**的策略上;
- (b) 靠 `com.someopark.macroweekly` 每周攒事件,等 n 自然到 12。⚠️ **与 #120 赛跑**:
  Kalshi 蜡烛 ~75 天过期,老事件掉出窗口的速度可能≥新事件进入的速度。
  **先量化这个净增速**,如果是负的,整条参数选择路要标记为"数据上不可达"并停止投入。

#### §25.7 测量结果(2026-08-05,#138 已量化 —— 结论:数据上不可达)

(b) 被证伪。净增速**不是正的,是零**。实测:

```
可报价事件 63 个,分系列:
  KXWTIW 11 / KXNATGASW 11 / KXAAAGASW 11   (weekly, oldest 72-74d)
  KXJOBLESSCLAIMS 10                        (weekly, oldest 68d)
  KXPCECORE 3
  KXCPI / KXCPIYOY / KXCPICORE / KXCPICOREYOY / KXPAYROLLS / KXU3
      / KXFED / KXFEDDECISION  各 2
  KXGDP 1
真实蜡烛 bar 8141 根,最早 2026-05-16(81 天前),最新 2026-08-02
settlements 表最早 2021-07-12 —— 结算记录有 5 年,蜡烛只有 81 天,瓶颈是蜡烛不是结算
```

在 ~75 天蜡烛留存下,每个系列的**稳态**事件数 = 留存 / 周期:

| cadence | 稳态上限 | vs MIN_OBS=12 |
|---|---|---|
| weekly | ~10.7 | 达不到 |
| monthly | ~2.5 | 达不到 |
| fomc | ~1.7 | 达不到 |
| quarterly | ~0.8 | 达不到 |

**没有一个 cadence 能越过 12。** 而且三个 weekly 系列(10-11 个事件)已经**坐在天花板上**了
—— 每周进一个、掉一个,净增 0。所以 `params_adopted: 0` 不是"样本还年轻",是**不动点**。

推论,写清楚免得以后又绕回来:
1. **不要降 MIN_OBS。** 12 对 DSR 已经很薄,deflation 的全部意义就是小 n 撑不起 73 组网格搜索。
   为了让门开而降门槛 = 把过拟合合法化。
2. **#120 的归档 cron 是唯一的解。** 用户之前否掉过它;现在它的地位变了 —— 它不是"数据卫生"
   的 nice-to-have,而是整条参数选择路能不能存在的**唯一前提**。有归档后 weekly 系列约
   **2 周**越过 12。要不要做由用户定,但代价必须如实说:不做 = `param_select` 永远返回默认值。
3. **`param_select.py` 的 docstring 已改。** 原文写着"each weekly series gains about one event
   a week ... 是 bounded downside 而非 no-op",这个论证建立在被本次测量推翻的前提上,已重写
   (2026-08-05)。留着一个已知错误的理由比没有理由更糟。
4. §25.7(a) 已随 §25.2 完成 —— 选择器现在**打分是对的**,只是没有样本可打。这两件事要分开记:
   机制正确 ≠ 机制可用。

### 实施顺序

`§25.2(P0,修 bug,~半天)` → `§25.4(逐系列开关,~1 天)` → `§25.5(持仓 path 诊断,~1 天)`
→ `§25.3(置信模型,~2-3 天)` → `§25.6(视 §25.3 结果)` → `§25.7(a) 随 §25.2 一起`

每一步做完**立即在 75 天窗口上重跑并三等分持出**,但**不重 freeze 展示段**,
直到 §25.3 的失败判据被明确回答为止。

---

## §25.8 结果(2026-08-05,单次运行同时回答 §25.4 / §25.5 / §25.3)

一次 `d75:model:end2026-08-04` 运行(耗时 3h20m,退出码 0)同时产出三条线的数据。
**三条全是负面结果。** 判据全部按登记原文执行,一个常数都没有事后调整。

基线(§25.2a 修复后):hybrid n=51 / ROI **−23.50%** / edge −29.17% / argmax −9.62%。

### §25.8a 逐系列开关(§25.4 / PR-4)—— 机制正确但无证据

触发 13 次,却只删掉 **2 笔** hybrid 交易(51→49),两笔都是 KXNATGASW:
07-02 亏 $0.48、07-10 赚 $0.52,净 **+$0.04**。hybrid ROI **−23.50% → −24.14%(−0.64pp)**。

判据 (a) 要 ≥ +5pp,实测是**变差**。判据 (b) 表面上三等分 2 段改善,但**那是假象**:
被删的两笔都结算在第三段,一、二段一笔都没删,它们的数字变化 100% 来自 n 从 51 变 49
之后**三等分边界平移的重新分桶**;唯一真实变化的第三段 +42.22% → +29.37%(−12.85pp),
是变差的。所以 (b) 不成立,不许拿它当"部分成立"。

触发 13 次既非 0 也非全部 ⇒ 那四个常数的尺度是对的,PR-4 的失败判据不适用,
**因此也没有任何理由去动它们**。代码保留(只减不加,笔数下行有界),但不宣称改善。

**真正的教训:§25.0 那个"8 笔交易承担 86% 亏损"的集中度是事后看出来的,不是 PIT 可交易的。**
滚动 12 笔实现 ROI 在这个窗口里根本认不出那几个系列;等它认出一个(KXNATGASW)时,认错了。

### §25.8b 持仓路径(§25.5 / #135)—— 用户问题的正面回答

问题:「是不是有一阵子赚钱没出掉 还是拿到结果出现的那一刻 0 或者 1 更赚」

**答:确实有,但比头条数字小得多,而且大头不是"赚了没跑",是"该止损没止"。**

46 笔 hybrid 有日频观测。头条 `oracle_gain = $18.29`(ROI −23.68% → +25.43%),
但那个数**把止损和止盈混在一起了**,拆开看:

| | 笔数 | 说明 |
|---|---|---|
| 曾在 **bid** 上转正(peak>0) | 18 / 46 | 只有这些才谈得上"赚到过" |
| 曾转正**且**最后结算更差 | **8 / 46** | ← 真正的"赚钱没出掉",占 17% |
| 从未转正 | 28 / 46 | 无论什么离场规则都拿不到利润 |

$18.29 里只有 **$8.09** 来自那 8 笔的止盈,**$10.20 来自止损**(从未转正、但中途没那么亏)。

而且那 8 笔的峰值**很小**:$+0.39 / $+0.15 / $+0.11 / $+0.24 …,而它们最终各亏约 $1.00。
形态是「小幅浮盈 → 结算归零」,不是「大幅浮盈没跑掉」。

结论,措辞要精确:**在这个 book 上,可能的改进主要来自不把已经在亏的仓拿到结算,
而不是来自兑现浮盈。** 但这一切都是 `oracle`:事后挑每笔自己的峰值、每天只有一个观测、
成交在我们未必看得到的蜡烛收盘价上。它是**上界,不是可实现收益**,
按 §25.1 不许直接变成离场规则,要变必须先登记。

### §25.8c 逐笔置信度门(§25.3 / PR-5)—— 证伪,且方向是反的

70 个特征行(edge 36 / argmax 34)。LOEO-forward:放行 23、挡掉 47、未训练放行 20。

- (a) hybrid ROI **−24.14% → −49.34%(−25.2pp)**,判据要 ≥ −5%。
- (b) 挡掉的 −8.68%、放行的 −42.08%,**顺序颠倒**;聚类 bootstrap 95% CI
  [−85.43pp, +10.79pp] 跨零。

**机制已定位,不是噪声:`corr(p, cost) = +0.887`。** 模型的"置信度"有 89% 就是价格本身。
放行组中位价 0.840 / 胜率 47.8%;挡掉组中位价 0.720 / 胜率 59.6%。门退化成了**价格筛子**,
专挑贵的热门放行 —— 而贵的热门正是这个 book 上亏钱的那批。

**根因是阈值形式错了,不是特征选错了。** 单腿成本 c:赢 ROI=(1−c)/c、输 −1,
所以盈亏平衡概率 **就是 c 本身**(26 笔单腿赢单实测中位 |ROI_win −(1−c)/c| = 0.0185,
差额即手续费/滑点)。而 `p_star` 是**每折一个全局标量**(实测 0.672–0.771)。
拿一个全局阈值去卡**逐笔各异**的盈亏平衡点,数学上必然「便宜的 +EV 全挡、贵的 −EV 全放」。

这也顺带解释了 §25.3(2) 里那张分歧表为什么不能用:**它和这里是同一个混淆** ——
`cost=0.90` 时 `fair−cost ≥ +0.10` 需要 fair ≥ 1.00,算术上不可能,
所以"一致"格结构性地收走所有贵热门 + 所有便宜长尾。两处都是**价格水平冒充信号**。

按 PR-5 失败判据:**本条路证伪,判据不改、特征不换、不在本窗口重跑。**
逐笔阈值是**新设定**,已另开 **PR-6**(K=2)且**只能前向判决**。

### §25.8d 顺带得到的两个读数

1. **「把 14 个 bet 都单独来做」—— 数据上做不到,这是结论不是借口。**
   `fit_per_series` 实测 **`n_fitted = 0`**:`MIN_TRAIN=20`,而窗口内单系列最多 11 笔
   (KXNATGASW),6 个系列只有 **2 笔**。14 个独立模型一个都拟合不出来。
   分层收缩(逐系列截距 + 全局斜率)是这份数据上唯一诚实的"逐系列",已实现;
   它的 LOEO ROI −49.34%,而强行按系列拆(非分层)是 −54.06%,**更差**,方向一致。
2. **§25.6 / #137 的问题被交叉验证回答了一半:`is_argmax` 系数 = +0.4006(放开符号,取正)。**
   即"这是 argmax 腿"确实预测**更高胜率**,与 argmax 流 76.5% vs edge 36.1% 一致,
   ROI 也一致(−9.62% vs −30.20%)。但**胜率高 ≠ ROI 好**是本节反复踩到的坑,
   而且承载这个系数的模型整体已被证伪,所以它**不构成上线依据**。
   PR-3 的路径不变:仍需 K=1 前向 15 个打架事件、argmax-first 优于 edge-first ≥ 10pp。

---

## §25.9 实盘与回测的两处分歧(2026-08-05,#141 / #142)

§25.8 的所有数字都建立在一个默认前提上:**回测跑的是实盘那套策略**。#109(门)和 #128
(展示了 gates-OFF 的运行)已经各栽过一次。这一节记两处新发现的同类分歧,一处在实盘侧
(实盘做错了),一处在回测侧(回测少做了)。

### §25.9a #141 —— 实盘按 `min` 平仓,应当按 `sum`

**先说我错在哪。** 我最初报给用户的根因是"开仓用 `survival(strict=spec.strict_gt)`、
平仓用 `leg_fair(strike_type)`,两条路对 fair 不一致"。**这个假设是错的,已被我自己证伪:**
把平仓路径逐腿的 `leg_fair` 结果重构回结构 fair,与开仓存下的 `fair` 到 4 位小数完全一致;
`strike_type` 与 `spec.strict_gt` 在 **14 个 series / 6,392 个合约上 0 处不一致**。
这条已被 `test_registry_strict_gt_matches_every_strike_type` 钉死。

**真正的 bug 是聚合方式。** `ops/exits.py` 用 `min(逐腿 holding edge)` 判反转,应当用 `sum`:

```
e_lo + e_hi = [S(lo) − mid_lo] + [(1−S(hi)) − (1−mid_hi)]
            = [S(lo) − S(hi)] − [mid_lo − mid_hi]
            = fair(bucket) − cost(bucket) at mid       ← decide() 开仓判的正是这个量
```

一个 bucket 是**一个**合成二元合约,整包开、整包平,它的持仓 edge 就是两腿之和。`min`
没有任何这样的解释,而 bucket 的 lo 腿是深度实值、接近 $1 买进的,**单独看**的模型 edge
按构造几乎恒负 —— 所以 `min` 在价差成立的那一秒就读出"反转"。

| 实测(修复前的账本) | |
|---|---|
| 同周期 open→exit 往返(`secs=0.0`) | **39 笔** |
| 其中 bucket | **36 笔**,且**全部 36 笔**在被平掉时结构 edge 为正 |
| 最差单例 | #3197 KXCPIYOY 2026-07:min=−0.3692,真实结构 edge **+0.1399**,付 −$0.27 平掉 |
| 其余 3 笔 | argmax 单腿,sum==min,属 #126/#137 的双流冲突,不是本 bug |
| 今天代码下仍会误平 | **16 笔**(另 20 笔已被 #130 的 `two_sided` 守卫挡住) |

单腿仓位 sum==min,所以本修复对单腿是**可证明的 no-op** —— 这是它可以直接上线、
不必重跑单腿那半本账的原因。回归测试见 `tests/test_exit_struct_edge.py`(6 条)。

### §25.9b #142 —— 回测一次都不离场,实盘一半仓位离场

`ops/refresh.py` 每个 cycle 都调 `exits.run`,而 `walkforward` 从开仓直接跳到 0/1 结算。
**所以这个 harness 出过的每一个 headline,描述的都是一个没人在跑的"持有到结算"策略。**

已把规则 1(edge 反转)和规则 3(regime review)搬进 `walkforward`:

- `_hold_edge(pmf, st, quotes)` —— 与 `exits.py` **同一个**度量,逐腿 `fair(side) − mid(side)`
  再**求和**(§25.9a),同样对宽价差 `two_sided` 拒绝报数(报数就等于向 0.18 的 bid 砸单)。
- `_pmf_for(d)` —— **逐日重新预测**,参数取自同一个 PIT 日选择器。钉死在 entry 日的模型
  不是泄露,但是**过时**,等于让离场规则回答一个生产早已替换掉的预测。
- `_first_exit` —— 前向扫描取**第一个**触发,规则 3 先于规则 1,与实盘同序。
- 离场后:`realized` 改记离场价、资金自 `exit_day` 起从 `_open_rows` 释放、§25.5 的 oracle
  路径截断到离场日(否则 oracle 会声称一个我们已经不持有的峰值)。

**规则 2(熔断强平)故意没搬**,这是测量后的决定不是疏漏:全账本 **52/52** 次离场都是规则 1,
规则 2 一次都没触发过;去模拟一个 PIT 状态重建不了的熔断,只会引入建模风险而换不到东西。

防漂移的那条测试是 `test_hold_edge_matches_what_exits_run_would_compute`:建一个真库仓位,
把**同一个**模型和**同一份**盘口同时喂给 `ops.exits.run` 和 `walkforward._hold_edge`,断言两者
相等 —— 包括 §25.9a 的求和聚合,也就是它们上次分歧的那个点。

**粒度偏差的方向是安全的:** 回测每天看一次,实盘每 cycle 看一次,所以回测只可能**晚于**
实盘离场,不可能早于。

**#142 没有关掉的一处分歧,如实记下:离场之后的再入场。** 实盘一笔 exit 会把仓位从
`ledger.open_positions` 里移走,于是下个 cycle `decide()` 看到 `already_open=False`,
edge 回来时可以**再买一次**同一个 (series, period)。回测的 `opened` 以事件为键且从不清除,
所以每个事件最多一笔。**暂不改是有意的**:清键要把 `opened`/`opened_argmax` 从"每事件一个 dict"
改成列表,并重写两条流、hybrid 合并与 `held_paths` 的构造 —— 在离场本身很少的前提下,
这个改动比它要修的偏差更大。偏差方向是**回测少算了实盘会做的交易**,上界就是离场次数本身
(`gate_stats['exit_*']`)。等这个数不再小的时候再回来改。

### §25.9b-1 #142 的实测结果(2026-08-05 重跑,`d75:model:end2026-08-04`)

同一窗口、同一份数据,唯一的差别是回测现在跑实盘那三条离场规则:

| 流 | n | staked | 零离场 realized / ROI | **含离场 realized / ROI** |
|---|---|---|---|---|
| edge | 36 | 29.40 | −8.88 / −30.20% | −8.72 / **−29.66%** |
| argmax | 34 | 28.27 | −2.72 / −9.62% | −2.60 / **−9.20%** |
| hybrid | 49 | 39.98 | −9.65 / −24.14% | −9.30 / **−23.26%** |

**离场触发 23 次**(`exit_edge_reversal` 23、`exit_regime_review` 0;
按流拆是 edge 6 + argmax 17,和 `held_analysis.n_exited_early` 对得上)。
入场笔数与 staked 一行未变 —— 离场不改变开仓,staked 按入场计。

两个该记住的读数:

1. **回测和实盘现在跑同一个策略了**,这是 #142 的全部目的,已达成。
2. **离场几乎不值钱**:49 笔里触发 23 次,hybrid ROI 只从 −24.14% 抬到 −23.26%,**+0.88pp**。
   一条会碰到一半仓位的规则只换来不到 1pp,说明现有 `EXIT_EDGE = −0.06` 的离场基本是
   EV 中性的 —— 这正好是 PR-7 步骤 0 要去检验的那个前提(价格近似鞅时,平仓只是白付一次价差)。
   **它不构成"离场没用"的结论**,只说明 −0.06 这个门槛上的离场没用;PR-7 测的是别的东西。

**#142 未闭合的分歧,现在有了上界**:离场后不重新入场的偏差,上界就是 23 次
(见 §25.9b 末段)。这个数不算小,但它的偏差方向是"回测少算了实盘会做的交易",
而在一个所有流都亏钱的 book 上,少做交易只会让回测显得**偏好**。等它变成正贡献时再回来改。

### §25.9c 由此产生的一条纪律

§25.8 的三个结论(PR-4 无证据、PR-5 证伪、§25.5 的 oracle)都是在**零离场**的回测上得到的。
#142 之后基线变了,但**判据不许因此重开** —— PR-4 和 PR-5 的失败判据写的就是"不换特征重试"。
新基线的作用是给 #140 / PR-7 一个能说明问题的起点,不是给已关闭的登记条一次翻案机会。

## §25.10 #139 / PR-6 —— 把置信门的阈值从"每折一个标量"改成"逐笔"

### 为什么这不是"PR-5 换个参数再试一次"

PR-5 的失败有一个**已定位的**原因,不是"结果不满意":放行条件 `p >= p_star` 拿**一个**标量
去卡盈亏平衡点相差 70 个百分点的一堆下注。结构以 `cost` 买入、付 $1,它自己的盈亏平衡就是
`p = cost`(赢 `(1−c)/c`、输 −1,`p(1−c)/c − (1−p) = 0` 解出 `p = c`)。所以全局阈值卡的
根本不是"技能",而是**价格**:贵的一律放行、便宜的一律挡掉。PR-5 自己量到了这个退化,
`corr(p, cost) = +0.887`。

PR-6 把阈值换成 `bet_threshold(row) = cost + FEE_WEDGE`。**没有新常数**:`cost` 来自行本身,
`FEE_WEDGE = 0.026` 是本 book 实测的一次往返净 taker 成本,和 `series_enable.ON_ROI` 是
**同一个数**(测试 `test_the_threshold_is_the_contracts_own_breakeven_plus_one_round_trip`
直接断言 `cf.FEE_WEDGE == se.ON_ROI`,防止哪天有人偷偷把它变成一个可调旋钮)。

### 一个看着像 bug、其实是对的行为

`cost > 1 − 0.026` 时阈值 **> 1.0**,该笔无条件被挡。这不是要 clip 回 1.0 的边界情况:
0.98 的合约赢了只付 0.02,**任何胜率**都盖不住 0.026 的手续费。`evaluate` 用
`n_threshold_above_one` 把这种笔数报出来,而不是塞进注释里。

### `gate="global"` 保留,是为了让证伪可复现

`evaluate(rows, gate=...)` 有两档:`"global"` 逐字复现 PR-5 被证伪时的规则
(`p >= p_star`,回归测试 `test_global_gate_reproduces_pr5_verbatim` 手算一遍对拍),
`"per_bet"` 是 PR-6 且是默认。删掉旧档会让"PR-5 失败了"变成一句关于**已经不存在的代码**
的断言。`p_star` 降级为纯诊断输出,仍然报,只是不再是门。

### 本窗口的数字一律不作数

PR-6 登记在 K=2、**只前向判决**。阈值是在看到本窗口失败**之后**才改的,所以这份 75 天数据上
再好的 ROI 也只是"关于我怎么挑阈值"的证据,不是关于规则的证据。`_verdict(gate="per_bet")`
因此**恒定**返回 `SMOKE ONLY`,既不打 PASS 也不打 FALSIFIED
(`test_the_per_bet_gate_is_the_default_and_is_never_graded_on_this_window` 钉住)。
判决只能来自登记后前向 30 笔 hybrid:门开 vs 门关实现 ROI 差 ≥ 5pp,事件聚类 bootstrap
95% CI 不跨零。#128 就是"同一批交易换个阈值重放一遍然后当成结果报出来"栽的跟头。

### 该看的那个数

`corr_allow_cost`,两档一起报,从**同一批**打过分的行上算。`corr(p, cost)` 不可能变
(模型没动,只动了阈值),所以它不是判据;能变的是**决策**和价格的相关。
诊断本身能不能"响"也被测了:`test_the_diagnostic_registers_a_price_sieve_when_there_is_one`
构造出 PR-5 的形状,global 档给 +1.0、per_bet 档给 −1.0。常数列报 `None` 而不是 `0.0` ——
把"没法算"印成"量到了、筛子没了"是这里最容易犯的假阳性。

## §25.11 #140 / PR-7 步骤 0 —— 鞅检验的结果与它的脆弱性

完整数字、已排除的机械解释、阈值扫描表都在 `docs/PREREGISTER.md` 的 PR-7 步骤 0「结论」里,
这里只记该记进计划的三件事。

### 1. #140 原本问的那个问题,答案是"没有证据"

#140 立项时问的是**价格止损**。回撤态单元 `E[y − m] = −0.042`,CI [−0.232, +0.152] 跨零 ——
**跌下去还会继续跌,没有证据**。所以 S1 不登记。这条要写死:二元合约的亏损本来就被本金封顶,
止损**不能**"控制风险",它唯一的收益来源就是"跌了还会跌",而这个来源在本 book 上没测到。
以后任何人再提"加个止损吧",先回来读这一行。

### 2. 被拒绝的是另一个方向,而且它很脆

盈利态单元反向拒绝:涨上去 ≥10pp 的仓位**会吐回来**(`E[y − m] = −0.376`,
CI [−0.637, −0.028];17 个事件里市场标价均值 0.776、实际只结算 40%)。
但 CI 上界离零只有 2.8pp,阈值收到 +0.25 时符号翻转,且步骤 0 一共看了 3 个单元(族错误率约 14%)。

**判据满足了,所以按登记进入步骤 1;脆弱性不去改判据,而是去改执行方式。**
看到结果之后补一个多重比较校正把不顺眼的结果压掉,和事后放宽判据救一个结果,是同一种病。

### 3. 步骤 1 用影子模式,不动实盘

登记的规则是 **S2:`hold_edge ≤ 0` 即平仓**(把 rule 1 的 `EXIT_EDGE = −0.06` 对全部仓位收成 0,
**净减一个常数**)。它和步骤 0 的发现自洽:盈利态 30 行里 21 行 `hold_edge` 已 ≤ 0,
18 个亏损行里 15 行会被抓住。

但它**在通过前向判据之前不上实盘**。两个臂都可观测,所以不需要拿钱去买样本:
`exits.run` 每天本来就在算 `net_edge`,把 S2 的触发点和当日可成交价**记下来但不执行**;
"持有到底"那一臂是实际发生的,"按 S2 平掉"那一臂用记录的价格还原。
前向 30 笔 hybrid,零实盘风险。

这也顺带定下一条通用做法:**当一个规则的两个臂都能从记录里还原时,默认走影子模式,
不要为了"拿到前向样本"去改实盘行为。**

---

## §25.12 #143 —— S2 影子记录器(PR-7 步骤 1 的实施)

§25.11 定了"走影子模式",这一节是它落地后的样子和两个当时没想清楚的点。

### 25.12a 结构

```
refresh cycle
  ├─ decide_all            开新仓
  ├─ s2_shadow   ← 新增    读 open_positions,逐仓写一行 shadow_exits,不执行
  └─ exits.run             实盘三条离场规则
```

`s2_shadow` 排在 `exits.run` **之前**,不是随手放的:S2 的门槛(`hold_edge ≤ 0`)比 rule 1
(`< -0.06`)松,所以一定先于或同时于实盘触发。放在后面,同一 cycle 被实盘平掉的仓已经不在
`open_positions` 里,S2 在它最后一天的状态就永远看不到了。

三个共享点,每一个都是为了让"两条臂只差一个阈值"这句话是真的:

| 共享 | 函数 | 不共享会怎样 |
|---|---|---|
| 持仓边 | `exits.hold_state()` | 变成第三份 `hold_edge` 实现 —— #141 就是两份实现悄悄分岔 |
| 平仓盈亏 | `exits.exit_realized()` | 两臂用不同公式定价,差值里混进定价差而不只是时点差 |
| 守卫 | `exits.frozen()` + 深度 ≥ 20 | 影子臂会"成交"在实盘根本发不出单的时刻 |

`hold_state()` 是从 `run()` 里原样提出来的,包括 #141 那段 sum-over-legs 的推导注释。
现有 39 个 exits 相关测试在重构前后逐一通过,行为未变。

### 25.12b 记录什么,以及为什么不只记触发日

每 cycle **每个开仓都写一行**,含 `hold_edge`、逐腿可成交价、`triggered` 0/1 和原因
(`s2_trigger` / `no_depth` / `edge_intact`)。不可测的书(单边/超宽价差)**不写行** ——
写 `triggered=0` 会在事后读成"S2 看过了,决定不动",那和"S2 根本看不了"是两回事。

只记触发日更省,但那样"S2 一次没响"和"记录器挂了三周"在数据上长得一样。前向检验最容易
坏在这种地方:样本量到了 30,却没人知道其中几天记录器是活的。

### 25.12c 一处需要更正的说法

我对用户说过"前向数据不记就没了,K 线里没有 bid"。查了之后:**这句话不准确。**
`quotes` 是 `(ts, ticker)` 主键的全历史表,代码库里没有任何 DELETE/prune 逻辑,
所以事后重建在数据上是做得到的。

影子记录器真正买到的是**时点**,不是数据。事后重建时每一个选择 —— 用哪个 cycle 的报价、
哪个 pred vintage、滑点算多少、深度门槛卡在哪 —— 都是研究者自由度;在结算已知之后再去定
这些,预注册就退化成拟合。逐日写死在结算之前,才使 PR-7 步骤 1 是前向的。

这条更正也写进了 `PREREGISTER.md` PR-7 步骤 1 的实施表下面。

### 25.12d 计分器的两条纪律

`research/shadow_s2.py`:

* **不到 30 笔不给判决。** `run()` 返回 `PENDING`,并且**连 bootstrap CI 都不算** ——
  算了就会有人引用。#128 就是一个数字从"没在量它宣称在量的东西"的 run 上被引出去。
* **S2 没出手的仓照样计入**,两臂同值。只在它出手的日子上打分,等于挑了最容易靠运气好看的
  子集;而"这条规则大部分时候是惰性的"本身就是关于它的真实信息。
* 无 `realized_usd` 的平仓行(`cancel` 退役行、#141 之前的 exit 行)**丢弃并计数**,
  不补 0 —— 补 0 会读成一笔打平的交易,而不是缺失数据。

首次读数(2026-08-06):5 个在仓全部记录,0 次触发(`hold_edge` 全为正,+0.050 ~ +0.238),
前向 **4 / 30**。

## §25.13 #144 —— 参数选择器仍在给"持到结算"打分

### 病灶

#142 把 `ops/exits.py` 的离场规则搬进了 `research/walkforward.py`,**但没有搬进
`research/pnl_score.py`**。后者是 `research/param_select.py` 每天用来给 73 组候选参数排序的
目标函数。于是从 #142 落地那天起:

* 回测(walkforward)按实盘规则离场;
* 实盘(ops/exits)按实盘规则离场;
* **只有参数选择器仍然把每一笔都持到结算**。

这正是 #125 / #133 那条病的第三次发作,只是换了一扇门:选择器排序所依据的策略,
既不是回测跑的那个,也不是实盘跑的那个。它不是"近似",实盘一半仓位由规则 1 平掉,
在那些交易上结算结果**根本不是**这笔的盈亏。

发现方式不是审查,是 #143 的全套测试跑完时 `test_pnl_score.py::
test_default_params_reproduce_the_stored_walkforward_trade_for_trade` 挂了一条:
`KXAAAGASW 2026-06-01: 1.0 vs walkforward 0.7`。先排除了 #143 自己
(`pnl_score` 不 import `ops.exits` 里的任何东西),再从存档 run `d75:model:end2026-08-04`
里查到那笔带着 `exit_rule='edge_reversal'` / `exit_day='2026-05-27'` / `realized=0.7`。
**那条钉死两个 replay 的测试,是唯一报警的东西。**

### 修法 —— 调用,不是再抄一遍

`event_pnl` 新增 `model_exits=True`,在两条流(edge / argmax)各自的入场日之后,
`from prediction_market_macro.research.walkforward import _first_exit, _mtm_path`,
按 `walkforward._trade_row` 同样的顺序:先 `_settle` 拿结算值,若有 exit 就用 `ex["mtm"]`
**整个替换**掉(`mtm` 已经净掉入场费、出场费和 `exits.SLIP`,就是 `_write_exit` 实盘记的那笔账)。

**为什么是 import 而不是复制。** `hold_edge` 这个度量现在有三处需要它:
`ops/exits.py`(实盘)、`walkforward._hold_edge`(回测)、以及这里。#141 的代价就是
两处各写各的、一处用 `min` 一处用 `sum`、36 笔 bucket 在正边际上被平掉才被发现。
所以第三处一行都不许自己写。

**一处刻意的不同:`_pmf_for` 用候选参数,不是 PIT 选择器。**
`walkforward._pmf_for` 在每个持仓日重新问一次 PIT 日频选择器,因为它在**模拟那个选择器**;
`pnl_score` 是在给**一组固定候选**打分,再问选择器会导致持仓段用的参数和入场段不是同一组,
候选之间就不再是配对比较 —— 而配对正是 `dsr` 去膨胀的全部依据。

### 实测(2026-08-06,存档 run `d75:model:end2026-08-04` 的 38 笔 edge)

窗口内 6 笔在结算前被规则 1 平掉,修复后**全部逐位对上 walkforward**:

| series | period | 离场日 | 修复后 | 若持到结算 | walkforward |
|---|---|---|---|---|---|
| KXAAAGASW | 2026-06-01 | 05-27 | 0.70 | 1.00 | 0.7 |
| KXAAAGASW | 2026-06-08 | 06-03 | 0.63 | 1.08 | 0.63 |
| KXAAAGASW | 2026-06-29 | 06-25 | 0.11 | −0.60 | 0.11 |
| KXNATGASW | 2026-06-05 | 06-02 | 0.23 | 0.40 | 0.23 |
| KXNATGASW | 2026-06-26 | 06-21 | −0.08 | 0.39 | −0.08 |
| KXPCECORE | 2026-04 | 05-27 | 0.05 | −0.79 | 0.05 |

**修正的方向是双向的:6 笔里 3 笔是把赢的提前吐回去,3 笔是把亏的砍在半路。**
所以这不是"选择器一直高估/低估",是它一直在**另一条曲线**上排序 —— 这种偏差没法靠眼力看出来,
只能靠那条 trade-for-trade 的钉子。

### 纪律

`walkforward` 和 `pnl_score` 是同一个策略的两个 replay,任何**改变持仓结局**的规则
(离场、止损、加仓、对冲)落进其中一个,就必须同一个 PR 落进另一个,并由
`test_default_params_reproduce_the_stored_walkforward_trade_for_trade` 现场验收。
新增的 `tests/test_pnl_score.py::test_the_exit_rules_are_walkforwards_own_and_not_a_third_copy`
把"调用而非复制"也钉住了:把 `walkforward._first_exit` 掐成返回 None,`pnl_score` 的答案
必须跟着变回持到结算 —— 变不回去,就说明有人又抄了第三份。

### 今天的实盘影响:零,但不能因此降级

`param_selection` 全部 14 个系列 `adopted=0`(截至 2026-08-05)。原因是 §25.7 / #138 那条:
K 线 75 天过期、无归档,每个 cadence 的稳态事件数都低于 `dsr.MIN_OBS = 12`,DSR 闸门
**永远开不了**,选择器每天返回登记默认值。所以 #144 修的这个目标函数,今天一个参数都没改。

但它不是"无害的"。它是**在#120 归档 cron 落地、闸门第一次真的打开的那一刻起才会造成后果**的
那类 bug —— 到那时它已经在库里躺了几个月,而第一次被采纳的参数会是在错误曲线上选出来的。
`adopted=0` 是不修它的理由这件事,恰恰是 #128 的思维方式。

### 端到端复核:同库、同 asof,只切 `model_exits`

上面那张表是单笔口径。选择器口径的对照跑在 `/tmp/macro_144.db` 的隔离副本上,同一个 `asof`,
唯一的变量是 `pnl_score.event_pnl(model_exits=...)`。**不能拿生产库 08-05 那行当对照** ——
副本是 08-06 01:48 拷的,中间隔了一天 K 线过期,`n_obs` 自己就从 11 掉到 6(KXAAAGASW)、
22 掉到 20(energy 池),那是 §25.7 的样本腐烂,不是 #144。

| 系列 | n_obs (ON/OFF) | pnl_default OFF | pnl_default ON | Δ | dsr_p OFF → ON | adopted |
|---|---|---|---|---|---|---|
| KXAAAGASW | 6 / 6 | −0.29 | −0.33 | −0.04 | None | False |
| energy 池 (KXNATGASW=KXWTIW) | 20 / 20 | −3.98 | −4.64 | −0.66 | 0.6299 → 0.6088 | False |
| KXPCECORE | 3 / 3 | 1.71 | 2.55 | +0.84 | None | False |

三件事:

1. **`n_obs` 两边完全一样。** 这是必须验的:`score_matrix` 的规则是"某一组候选算不出来,
   整个事件就整行丢掉",所以只要 `_exit_or_settle` 在某些事件上抛异常,样本就会静默缩水,
   而缩水本身会伪装成"目标函数变了"。没有缩水,说明离场路径在全网格上都跑通了。
2. **Δ 对得上单笔表。** KXAAAGASW 三笔 `(0.70+0.63+0.11) − (1.00+1.08−0.60) = −0.04`,
   KXPCECORE 一笔 `0.05 −(−0.79) = +0.84`,两个都精确到分。energy 池表上两笔算出 −0.64、
   实测 −0.66,差 0.02 落在四个 2 位小数的舍入带内(池里还并了 KXWTIW 的事件,没有再细拆)。
   逐笔口径和选择器口径闭环。
3. **今天的选择不变。** 两边 `chosen=0`、`adopted=False`,`params={}`。`dsr_p` 动了一点点
   且是往好的方向(0.6299 → 0.6088),但离 `ADOPT_P` 还差得远 —— 跟 §25.7 的结论一致。

顺带澄清一个看起来像 bug 的东西:**`KXWTIW` 和 `KXNATGASW` 的报告逐字相同是设计如此**,
`param_wf.POOLS["energy_fut"]` 把两者合成一个池、同一套 grid、同一份合并样本打分。

## §25.14 #145 —— `gate_history` 的 memo 从来没生效过(修 #144 时撞见)

`research/pnl_score.py:gate_history` 原来这么写:

```python
cache = getattr(conn, "_pnl_gate_hist", None)
if cache is None:
    cache = {}
    try:
        conn._pnl_gate_hist = cache
    except AttributeError:
        return _build_gate_history(conn, series)   # ← 每次都走这条
```

`sqlite3.Connection` **不接受任意属性**,所以 `except` 分支每一次调用都触发,缓存永远是空的。
`GateHistory.__init__` 里是一整轮 `backtest.replay_series` + `eval.decision_replay`,
本该每个系列建一次,实际是每 `event_pnl` 建一次 —— 73 组 × 11 事件 = **803 次**。

**它一直没被发现,是因为它只是性能 bug:`GateHistory` 是确定性的,重建的答案一模一样。**
撞见它是因为 #144 新加的那条"持仓段必须用候选参数"的测试:spy 抓到一串 `params=None` 的
模型调用,先怀疑是自己接错了线,查下去才发现是 memo 死了、每次都在重放。
`try/except AttributeError` 把一个"这条路根本走不通"的事实降级成了静默兜底 —— 这是本条的教训,
不是"少写了个缓存"。

修法:模块级 `_GATE_HIST[(id(conn), series)] = (conn, hist)`。value 里存 conn 本身是关键:
id 只有在对象被释放后才可能被复用,持有引用就杜绝了这一点。
`tests/test_pnl_score.py::test_the_gate_history_is_actually_memoised` 用 identity 钉死
(不钉墙钟时间 —— 那会在热页缓存上随机翻绿)。

## §25.15 #137 —— argmax 优先的答案:模型说"不",而且原因是价格

### 问题回顾

§25.6 量的是:两条流在 21 个事件上都开了火,**21/21 挑了不同的结构**。
edge 腿 19.0% 胜率 / −42.91% ROI,argmax 腿 76.2% 胜率 / −11.02%。
按流分:argmax 单独 −9.62%,hybrid −23.50%,把 hybrid 改成 argmax 优先是 −9.77%。

#137 当时**没有**去改 `walkforward.py:589`,理由写在任务里:三分法不稳(第三段反号,靠一笔
GDP +$5.03 撑着),而 #126 就是"看完哪边亏再定规则"翻车的标准反例。指定的做法是
**把 `stream` 作为特征喂进 §25.3,让答案带收缩、带交叉验证地出来**。

### 缺的那一块

`is_argmax` 从 #136 起就在 `FEATURES` 里,而且是**唯一一个不加符号约束的自由系数** ——
这是有意的,不约束才能让模型说得出"argmax 更差",而不只是"argmax 没更好"。
但报告里只打了一个 `coefficients_full_sample` 的**全样本标量**。
那不是"交叉验证过的答案",那是同一次目测外面套了个岭惩罚:
前半段 +0.4、后半段 −0.4 的系数,平均出来是个看着很笃定的 0。

所以这次补的是 `research/confidence.py:coef_path()` —— 在 `loeo_forward` 的**同一批折**上
逐折重拟,报每个特征的 `first/last/mean/min/max`、`n_zero`、`n_sign_flips`。
两者共用抽出来的 `_folds()` 生成器,不是各写一个循环:同一次验证的两种读法一旦各走各的,
报告描述的就是一个从没给任何东西打过分的拟合。这条由
`test_the_coefficient_path_walks_the_same_folds_as_the_scorer` 钉住。

### 结果(70 笔,18 折)

| 特征 | 先验 | first | last | mean | min | max | n_zero | flips |
|---|---|---|---|---|---|---|---|---|
| `cost` | + | 0.7576 | 0.8537 | 1.0141 | 0.61 | 1.1985 | 0 | 0 |
| `spread` | − | −0.3117 | −0.3360 | −0.2842 | −0.4292 | −0.0645 | 0 | 0 |
| `abs_div` | 自由 | 0.1861 | 0.2657 | 0.2224 | 0.0325 | 0.3470 | 0 | 0 |
| `lead_days` | − | −0.0173 | −0.4081 | −0.2093 | −0.4726 | 0.0 | 1 | 0 |
| `skill_ratio` | − | 0.0 | −0.2299 | −0.0981 | −0.4165 | 0.0 | 12 | 0 |
| `entropy_norm` | − | 0.0 | −0.1627 | −0.0821 | −0.2714 | 0.0 | 9 | 0 |
| `ser_roi` | + | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | **18** | 0 |
| **`is_argmax`** | **自由** | **−0.0149** | **−0.1622** | **−0.2384** | **−0.3452** | **−0.0149** | **0** | **0** |

**`is_argmax` 在 18 折里没有一折是正的。** 它从来没想要那个符号,而且随着数据累积越走越负。

### 为什么这和 §25.6 的 76.2% 胜率不矛盾

因为 `cost` 也在模型里,系数 +1.01 —— 近乎机械的一条:0.85 的合约本来就大约 85% 兑现。
`is_argmax` 读出来的是**控住价格之后**的剩余。argmax 腿买的是贵的热门(#126 量到均价 83.6¢),
它的高胜率是**价格,不是本事**。同价位下,胜率和 ROI 同向排序,所以"同价位下 argmax 更容易输"
就等于"同价位下 argmax ROI 更低"。§25.6 那张按流分的表没控价格,量的是两件混在一起的事。

**结论:argmax 优先不采纳。** #137 的原话是"只有 §25.3 被证伪,才回来把这个翻转做成 K=1 前瞻
预注册"。§25.3 没有被证伪 —— 它回答了这个问题,答案是否定的。`walkforward.py:589` 不动。

### 这个 0 flips 值多少 —— 别高估

折是**扩张窗**不是滑动窗:第 k 折训练在它之前的全部数据上。所以一次后段反转,只要前段数据
压得住,表现出来是系数**衰减向 0**,不是翻符号。`n_sign_flips == 0` 的正确读法是
**"在任何训练规模下它都没想要另一个符号"**,不是"18 折独立地一致同意"。
两种行为各有一个测试钉着(`..._reports_a_flip_when_the_data_flips` /
`..._decays_instead_of_flipping`)。这是我写第一版测试时自己搞错、被测试打回来才想清楚的。

顺带一个自己咬自己的 bug:`n_sign_flips` 第一版是逐对比较,于是 +1.63 → 0.0 → −1.24 这条
**明明翻了号的路径报 0 次翻转** —— 因为中间有一折 `round(b, 4)` 落到了正 0。
受约束的系数贴在边界上会连着好多折是 0,同样中招。改成跳过 0、和上一个非零值比。
少报一次翻转正是这个数字唯一不能犯的错,它存在的全部意义就是别让不稳的系数读起来很稳。

### 顺手记一条不属于 #137 的发现

**`ser_roi` 18 折全部被夹到 0。** 按 `FEATURES` 里它自己的注释:
"If this coefficient clips to zero then §25.4's premise — that losing series keep losing —
is not supported"。也就是说 §25.4 / #134 的逐系列开关,其前提在这个置信模型里**拿不到支持**。
#134 已经上线,这条不构成"立刻回滚"的理由(逐系列开关和"亏损系列继续亏"这个断言不是同一件事,
而且 `ser_roi` 是被符号约束夹住的,夹到 0 只说明数据往反方向拉),但它必须被记下来而不是烂在
一个 JSON 字段里。**已开 #146 单独追。**

---

## §25.16 #118 / PR-1 —— claims 近因权重的前向评分器

登记在 2026-07-31,是四条预注册里最早的一条,但一直只有一行登记文本、没有能给它打分的代码。
`research/shadow_claims.py` 补上这一块。它**不产生任何交易行为**,只把一条已经写死的判据
每天算一遍,并且在样本量到齐之前拒绝给结论。

### 判据里有两处必须先读准

**一、对手是市场,不是默认参数。** 发现期的数字(49 个事件上 Brier 0.1497 vs 0.1600)是
candidate-vs-default,而登记下来的判据写的是 `paired Brier(model) < Brier(market)`。这是**严格更难**
的一条:一个跑赢默认参数、却仍然跑输盘口的参数集,什么都没挣到 —— `strategy/skill.py` 早就在
拿同一个比值挡模型路的下注。所以 `run()` 的 `primary` 是 candidate-vs-market;candidate-vs-default
作为 `secondary` 一并输出(它是发现所在的量,藏起来反而不诚实),但它的字段名里直接写着
"grades nothing"。日后一次把 primary 悄悄换成 default 的编辑,等于在同一个名字下换一个容易得多的
判据,`test_the_constants_still_match_what_was_registered` 会去读 `PREREGISTER.md` 里
`Brier(market)` 这几个字并断言它还在。

**二、"改动"栏写了两个旋钮,实际只动一个。** `seasonal_years=10` 本来就是
`model/claims.DEFAULT_PARAMS` 的值 —— `git log -L 30,40:model/claims.py` 确认自文件写成起从未变过。
真正变的只有 `level_weights`。这关系到 K 的诚实性:动一个旋钮登记一次是 K=1,动两个而只登记一次不是。
`test_the_only_knob_the_candidate_actually_moves_is_the_weights` 把这一点钉住。

### 为什么是 replay 而不是像 S2 那样做实时记录器

`shadow_s2`(§25.12)必须实时写,因为它的离场取决于书的状态,事后重建就要**在结算已知之后**
去挑用哪个 cycle 的报价 —— 那是研究者自由度。Brier 没有这个自由度:`asof` 是事件收盘时间和
发布时间的确定性函数,两个都在库里。所以两条臂都用 replay 跑,把本来会变成暗坑的两件事显式钉住:

* **两条臂都走 `backtest.replay_series`。** asof 规则只住在那里(收盘晚于发布时把 asof 退到
  发布前一秒;`data_horizon` 越过发布的事件直接丢弃)。`param_wf.score_matrix` 用的是平的
  close−1h,拿它给候选打分、再跟 replay 口径的市场比,就是**两个不同 asof 之间的"配对"检验** ——
  也就不是配对检验。这条和 #141 / #144 是同一个病:同一个量有第二份实现。
* **事件集取交集,不对称丢弃要报出来。** 候选在某周预测失败,就不能让它在更容易的子集上被打分
  而市场保留全集。`n_dropped_asymmetric` 和 `dropped` 就是为这个存在的。
* **两条臂的市场列必须逐位相同。** 市场的 Brier 不依赖我们的参数,只依赖 asof,而 asof 与参数无关。
  不同就说明有本不该依赖参数的东西依赖了参数,`market_column_mismatch` 会把它吼出来,
  而不是让它化进一个平均数里。

为此 `backtest.replay_series` 加了一个 `params` 参数,并且**只在它不为 None 时才向下传** ——
生产调用发出的是与从前逐字节相同的调用,一个不接受该 kwarg 的模型函数不会因此在周扫里开始抛异常。
`test_replay_series_forwards_params_only_when_they_are_given` 两个方向都断言。

### 代码变更是活的,这件事写在输出里

两条臂跑的都是今天的代码,所以一次对 `model/claims.py` 的修改会把两条臂**一起**挪动,配对关系
活得下来 —— 但登记的效应量活不下来。`code_fingerprint()`(`model/claims.py` 的 sha1 前 12 位)
每次运行都写进输出。它既不假装没事发生,也不擅自拒跑:该怎么办取决于改了什么,那是人的判断。

### 现在的读数:0 / 8,而且这个 0 是真的

| | |
|---|---|
| `n_forward` | **0 / 8** |
| `code_fingerprint` | `6b52d629c385` |
| `verdict` | `PENDING — 0/8 forward weeks. No verdict, and the numbers above are a progress readout, not a result.` |

不是记录器坏了,查过:最后一个**已结算**的初请周是 26JUL30(close 2026-07-30T12:25Z),
在 2026-07-31 登记**之前**,按判据不计数。登记后第一个到期周是 26AUG06,合约在库、
close 2026-08-06T12:25Z,写下这段时(21:03Z)结算还没落库 —— 26JUL30 那周实测的入库延迟约 21 小时,
所以这是正常延迟不是卡住。初请是周频,**最早可能出判决的日子是 2026-09-24**(08-06 再加 7 周)。

在那之前 `_verdict` 恒返回 PENDING,连 Wilcoxon 都不算(`_wilcoxon` 另外要求至少 5 个非零差值)。
提前把一个进度读数当结果报出去是什么下场,#128 已经演示过一次了。

### 测试(10 条,全绿;全套 469 passed)

预注册测试的失败模式不是崩溃,是:判据事后漂移去迁就结果、计数器数了个空、"配对"的两条臂其实
跑在不同样本上、样本量没到就先印结论。每样钉一条。另有两条**跑在真实库上**,专治"看起来在测其实什么都没测":
`test_the_market_column_is_identical_across_the_two_arms`(要求至少 5 个可 replay 的事件,
否则报错而不是空过)和 `test_the_candidate_actually_changes_the_model`(参数必须真的挪动了
model 列 —— 如果 `params` 在链条某处被悄悄丢掉,本模块产出的每个数字都会是"默认参数和它自己比",
看着像有结论,其实什么结论都没有)。

写测试时自己踩的一个坑记一笔:`test_the_only_knob...` 第一版把条件表达式塞进了推导式的 `if` 从句里,
`SyntaxError` 直接挡在收集阶段。写成一个 `norm()` 小函数就好了 —— 一行塞两个三元判断,
省下来的行数不值它花掉的可读性。

---

## §25.17 #146 —— `ser_roi` 夹到 0 不是证据,是一段被自己的门截断的列

§25.15 末尾顺手记了一条:`ser_roi` 在 18 折里全部被符号约束夹到 0,而 `FEATURES` 里它自己的注释说
"夹到 0 就意味着 §25.4 的前提(亏损系列继续亏)拿不到支持"。**那句注释是错的,现已撤回。**
按它去回滚 #134,会是拿一个根本没测过这件事的数字去推翻一个规则。

### 先量一下这一列到底有多少

| | |
|---|---|
| 特征行 | 70 |
| `ser_roi` 非空 | **4** |
| 不同取值 | **3**(0.0402 / 0.1865 / 0.3862) |
| 负值 | **0** |
| 全部来自 | KXNATGASW 一个系列 |
| `ser_n = 0` 的行 | **66**(不是"小于 MIN_N",是 0) |

`Scaler` 对缺失填训练中位数、且**不加缺失指示列**(这是 n=70 下省自由度的有意选择)。
所以那个系数是拿 4 个有信息的点、对着 66 个被填成常数的点拟出来的。

### 为什么只有 4 个 —— 机制,不是巧合

两层原因,第二层才是要命的那层。

**一、10 / 14 个系列在窗口里根本攒不到 6 笔。** `series_enable` 的 `evaluate` 只在滚动窗
`len(w) >= MIN_N = 6` 时才写出 `roi`/`n`。月频系列(CPI 系、U3、PAYROLLS、PCECORE、GDP、FED)
在 75 天里的 `decision_replay` 只有 1–3 笔,永远到不了 6。对它们 `ser_roi` 是结构性 None。

**二、剩下 4 个够到 6 的系列里,只有 1 个在够到之后还下过注。** 而这不是运气 ——
**特征行只在"真的下了注"时才写,而 §25.4 恰恰在这个数 ≤ 0 时把系列关掉。**
关掉 ⇒ 事件在 `decide()` 之前就被中止 ⇒ 不产生特征行 ⇒ 那个负的 `ser_roi` 永远进不了这张表。
这一列**正好被截断在它想检验的那个符号边界上**。

实测(`GateHistory.asof` 直接复算,与库里存的值逐个对上):

| 系列 | 第 6 笔 replay 收盘 | 该日之后的门状态 | 该日之后的下注行 |
|---|---|---|---|
| KXJOBLESSCLAIMS | 2026-07-02 | 07-03 起 **enabled=False**,roi −0.660 → −0.548 → −0.057 → −0.172,窗口结束都没回来 | **0** |
| KXWTIW | 2026-07-31 | 08-01 起 **enabled=False**,roi −0.275 | **0** |
| KXAAAGASW | 2026-06-29 | 一直 enabled(roi +0.340 → +1.373 → +1.059) | 0(最后一注 06-23,另有原因) |
| KXNATGASW | 2026-06-26 | 06-27 **False**(roi −0.837)→ 07-25 **True**(roi +0.040) | **4** |

**KXNATGASW 一个系列就把两半都演全了**:06-27 那天 roi = −0.8370,门关,零行;
07-25 回到 +0.0402,门开,4 行。同一个系列,负的那半被吞掉,正的那半留下来。
所以这一列不是"没测出效应",是**按构造只可能看到正值**。

顺带解释了 §25.4 登记里那个当时只能描述、没能解释的结果:PR-4 触发 13 次却只删掉 2 笔、
且两笔都是 KXNATGASW —— 因为 KXNATGASW 是唯一一个在窗口内既被关掉又被重新打开的系列,
其余系列要么门永远没意见,要么一旦有意见就再也没开过。

### 放开约束跑一遍(诊断,已跑,不采纳)

`FEATURES` 未动,只把模块级 `SIGNS` 里 `ser_roi` 那一位临时改成 0,走同一套 `_folds`:

| | 全样本系数 | 折路径 |
|---|---|---|
| 约束 +1(生产) | `0.0` | 18 折全 0 |
| **放开(诊断)** | **−0.3378** | last −0.3201, mean −0.0536, n_zero 15/18, 0 次翻号 |

看着像"它想要负号"。**不采纳,而且这个数不该被引用**:它是 4 行拟出来的,那 4 行里
`corr(ser_roi, entropy_norm) = 0.9322` —— 在这个子样本上它几乎就是 `entropy_norm` 的副本,
拿到的是后者的方差。`ser_roi ≤ 0` 的行数是 **0**,所以"亏损系列"这一侧一个观测都没有。
`corr(ser_roi, win)` 在 4 个点上是 −0.54,胜率 3/4 —— 这不是证据,这是四个点。

**没有跑门控 ROI,也不会跑。** 放开一个特征再回到同一个 75 天窗口上读 ROI,就是在 51 个点上做
变量选择,正是 §25.1 第 1 条禁止的事。诊断脚本(`/tmp`,不入库)刻意不打印那个数。

### 结论与动作

1. **#134 / §25.4 不回滚。** 撤回的是一条错误的读法,不是一条支持它的证据 ——
   §25.4 的前提在这份数据上**未被检验**,既没被支持也没被推翻。
2. **改了两处注释,没改任何行为。** `FEATURES` 里 `ser_roi` 那段错误推论已替换为上面的机制说明;
   `test_a_clipped_prior_is_visible_as_a_zero_and_not_as_a_small_number` 的 docstring 同样重写 ——
   它的机械断言(`n_zero` 数的是精确 0)一直是对的,错的只是它给自己安的意义。
   合成行里 `ser_roi` 每行都有、正负都有,而真实列两样都没有,这个差别现在写在测试里。
3. **真要让这一列可用,得改的是记录而不是模型:** 被门中止的事件也应当写一行特征
   (带 `staked=0` 或一个 `blocked_by` 字段),这样负的 `ser_roi` 才进得来。这是 `walkforward`
   的改动、要重跑窗口,**且它只会让这一列可读,不会让 §25.4 变成已验证** —— 判决仍然只能来自前向。
   另开任务追,本条不顺手做。

## §25.18 #126 / PR-2 —— argmax defer-to-market 的双臂影子记录器

### 规则是什么,以及为什么它长得像个 bug

`ops/decide_all.defers_to_market` 一行:

```python
return st.fair > st.cost        # True ⇒ _place_argmax 跳过
```

也就是说,**argmax 腿只在 `fair <= cost` 时下单**。翻译过来:只在我们的 fair 不高于市价
时买入 —— 净边际 `net_edge = fair - cost <= 0` 是**构造性**的,不是偶发的。

这看着像符号写反了,但它有个说得通的逻辑:这是一条**逆向**规则。原始理由是"在最热门的那条
腿上,我们觉得便宜的时候我们通常是错的,所以让市场说了算"。#126 观察到的症状
("argmax 腿用 83.6c 买一个 72.7% 结算的东西")正是这条规则的**预期表现**,不是它的故障。

问题不在于它是否反直觉,而在于**它的证据过期了**:"dual-window 27W-2L" 是在 #109 重建 PIT
门、#127 修 bucket devig 之前测的。那是关于一个已经不存在的策略的证据。所以 PR-2 的事前
假设是空的 —— 这是重新验证,不是改进。

### 双臂怎么构造的

关键是 `_place_argmax` 里的**先选后滤**:

```python
st = argmax_candidate(structs)        # 选:cost∈[0.10,0.90] 且 fair>0.5 中 fair 最大的
...
deferred = defers_to_market(st)       # 滤:只决定买不买,不决定买哪个
_shadow_argmax(conn, ..., deferred, now)
if deferred:
    return False
```

因为顺序是这个顺序,**关掉过滤器不会改变买哪个结构**。两条臂因此是嵌套的:

| 臂 | 组成 | 含义 |
|---|---|---|
| `filter_on` | `placed` | 过滤器放行的那些 |
| `filter_off` | `placed + deferred` | 规则拿掉,全都买 |

如果顺序反过来(先滤后选),OFF 臂买的会是**另一个结构**,两条臂就不再是配对比较,而是两个
不同策略的对比。`test_turning_the_filter_off_does_not_change_which_structure_is_bought`
把这个顺序钉住了,并且顺手证明了反过来会买到 `structs[2]` 而不是 `st`。

写这个测试时踩了一脚值得记下来的坑:第一版用 `_struct(0.95, 0.99)` 当最高 fair 的候选,
测试挂了。原因不是代码错,是 **cost=0.99 落在 `argmax_candidate` 的 [0.10, 0.90] 带外**,
那个结构压根没进候选池 —— 真正在起作用的是价格带而不是过滤器,测试即使"过"了也没测到
它自称在测的东西。改成 cost=0.90 才让 `fair` 成为唯一的区分量。

### 两个必须挡住的不对称

1. **风控不对称。** 如果只对 placed 臂跑 `risk.check`,那么一笔"被 defer 掉、但风控本来也
   会拒"的交易会被算进 OFF 臂的功劳里 —— OFF 臂凭空多出一些永远发不出去的交易。所以
   `_place_argmax` 把 `risk.check` **提到过滤器之前**,两臂同一道准入标准。这个重排安全的
   前提是 `check` 是纯读(不写、不 commit),`test_risk_check_is_only_ever_read_from` 对
   `decisions / fills / shadow_argmax / alerts` 四张表做快照比对来钉住它。这和
   `shadow_claims` 用交集事件集合挡的是同一类危险。
2. **重复计数不对称。** `refresh` 一天可能跑很多轮,同一个事件会被反复看到。
   `shadow_argmax` 主键取 `(series, period)`,同一事件只留一行 —— 样本量只能靠"交易"涨,
   不能靠"重跑"涨。这一条在真实数据上立刻就有用:全部 4 笔 placed 腿落在同一个事件
   (KXWTIW 2026-08-07)上,不去重的话 20 笔的门槛会被一个事件刷掉五分之一。

### ⚠️ 收尾时发现的事:入场规则和退出规则互相矛盾(#148)

这是本次最重要的发现,而且它**推翻了我自己刚写下的一句话**。

`shadow_pr2` 的 docstring 原本把 ON 臂标成 "what the book actually did"。这是错的:

* `_place_argmax` 只在 `net_edge <= 0` 时开仓(构造性,见上);
* `ops/exits` 在 `hold_edge < EXIT_EDGE = -0.06` 时平仓;
* ⇒ argmax 仓位只能活在 **[-0.06, 0] 这条 6 分钱的窄带**里,带外的当轮开、当轮平。

账本上全部 4 笔的实测:

| 时间 | net_edge | 落点 | 结果 |
|---|---|---|---|
| 2026-08-05T09:12:03 | -0.1543 | 带外 | `edge_reversal`,realized **-0.0700** |
| 2026-08-05T09:16:06 | -0.1443 | 带外 | `edge_reversal`,realized **-0.0700** |
| 2026-08-05T12:31:32 | -0.1443 | 带外 | `edge_reversal`,realized **-0.0700** |
| 2026-08-06T09:13:41 | -0.0101 | **带内** | 持有 |

**3/4 当轮往返,-$0.21 / $2.29 = -9.2%**,每轮付掉价差加两次 taker 费,然后下一轮再把同一
个 ticker 开回来 —— 是个 churn 循环,不是一次性的。查证过程里还摔了一跤值得记:第一版用
SQL `datetime(ts, '+120 seconds')` 去配对开平仓,对带时区偏移的 ISO 串算不出来,返回
"0/4 往返",和我几分钟前亲眼看到的同秒开平仓的原始 trace 直接冲突。是那个冲突让我回去重
算,而不是采信查询结果。**结论没变,但它差一点就以反过来的形式被写进文档。**

对 PR-2 的后果,已经写进 `exit_policy_note` 和 PREREGISTER 读法第 2 条:

> 两条臂都是"持有到结算"的反事实。PR-2 的 ROI **不是** argmax 流的真实盈亏,判决只针对
> 过滤器本身。

固定退出策略是**故意**的 —— 要孤立"入场"规则,就必须让退出在两臂间保持一致,否则测到的是
入场和退出的混合效应。固定在"持有到结算"是一个选择,现在它被事先写下来了。这正是 #144 那个
坑(评分器在给一个没人跑的策略打分)从**沉默的假设**变成**声明的假设**。

`test_the_entry_and_exit_rules_still_contradict_each_other` 钉的是**关系**而不是数值:
`EXIT_EDGE` 被重新调参时它不该响,只有 #148 真的把矛盾解决掉时才该响。

**#148 不得在 PR-2 到期前修。** 修 `_place_argmax` 等于半途改掉本登记正在测的旋钮。

### 现在的读数

```
n_forward 0 / 20   (0 placed, 0 deferred)
verdict   PENDING
```

0 是真的 0,不是死日志。记录器 2026-08-06 才装,而 argmax 流本身极稀疏:全历史 4 笔 placed
腿、1 个事件。deferred 臂的到达率**此前从未被记录过**,所以未知 —— 这也正是装这个记录器的
一半理由。按 placed 侧的密度推,20 笔是以**月**计的等待。下次复核如果 `shadow_argmax` 还是
0 行,那才该怀疑记录器。

测试 17 条,全绿;宏观全量 486 条全绿。

---

## §25.19 #147 —— `shadow_blocked`:把被门中止的事件也记一行,而且一分钱不动

§25.17 第 3 条留下的动作:特征表只在"真的下了注"时写一行,而 `ser_roi` 恰恰在 ≤ 0 时把系列
关掉 —— 于是这一列**按构造只可能看到正值**,拟在上面的任何系数都不能检验 §25.4 的前提。
`skill_ratio` 被 `skill_blocked` 以同样方式截断(70 行里只有 9 行)。

`research/walkforward.run(shadow_blocked=True)`(默认 **False**)让这两类事件跑完整条路径,
写一行 `placed=False` / `blocked_by=<gate>` 的**反事实**行:它不进 `trades`、不进 `opened`、
不进 `open_rows`/`opened_today`/`day_trades`,也**不进 `pending_scores`**。最后那条是全部
安全性所系 —— `pending_scores` 汇进 `pool_runner`,决定 `fair_mode='pooled'` 的对数池权重,
进而决定 pmf、结构、PnL。少了 `blocked_by is None` 这个守卫,这个开关只在 `fair_mode='model'`
下是 PnL 中性的,而在**用于展示的 pooled run 上悄悄不是**(`walkforward.py:685`)。

### 配对 A/B(唯一能证明"不动钱"的证据)

同一个 db 副本(`/tmp/wf_ab.db`,不碰生产库),`days=61`、`end=2026-07-31`,只翻这一个开关,
11 个 PnL 键逐字节比对(`json.dumps(..., sort_keys=True)`):
`n_trades / win_rate / staked / realized / roi / by_series / lead_buckets / curve / trades /
streams / held_analysis`。

```
differing PnL keys: NONE  <-- bit-identical
A  roi=-0.25275  realized=-6.21  n_trades=31
B  roi=-0.25275  realized=-6.21  n_trades=31

feature rows: A=58  B=67  (+9)
  B placed=58  blocked=9        placed subset identical to A: True
  blocked_by: {'skill_blocked': 8, 'series_disabled': 1}   (stream: edge 6 / argmax 3)
```

`placed` 子集与 A **逐字节相同**,不只是条数相同 —— 否则"多 9 行、PnL 不变"也可以由
"58 行内容变了但净效应抵消"产生。两次运行写出两个不同的 experiment key
(`d61:model:end2026-07-31` 与 `…:shadowblocked`),没有互相覆盖;这正是
`test_a_shadow_run_cannot_overwrite_the_production_experiment_row` 要挡的事,现在有了实测。

**A 臂复现了 §25.17。** 库里 §25.17 用的那一行(`d75:model:end2026-08-04`)是 70 行、
`ser_roi` 4/70 值域 [+0.0402, +0.3862]、`skill_ratio` 9/70 值域 [1.0639, 1.1401];A 臂是
4/58 和 9/58,**取值集合完全相同**。58 vs 70 是 61 天窗 vs 75 天窗,不是回归。

### 打开之后看见了什么

| | A(生产默认) | B(`shadow_blocked`) |
|---|---|---|
| `ser_roi` 有值 | 4 / 58 | **13 / 67** |
| 其中负值 | **0** | **4** |
| 值域 | [+0.0402, +0.3862] | [**−0.8655**, +1.0960] |
| 来自几个系列 | 1(KXNATGASW) | 3(+ KXAAAGASW, KXJOBLESSCLAIMS) |
| `skill_ratio` 有值 | 9 / 58 | **18 / 67** |
| 值域 | [1.0639, **1.1401**] | [1.0639, **12.4971**] |
| 来自几个系列 | 2 | 4 |

两条截断线都**正好落在门自己的阈值上**,不是"大致偏了一点":

- `series_enable` 在 `ser_roi ≤ 0` 时关系列 → A 臂最小值 **+0.0402 > 0**,B 新增的 4 个值全 < 0。
- `strategy/skill.BLOCK_RATIO = 1.50` → A 臂最大值 **1.1401 < 1.50**,B 新增的 6 个值
  (1.90 / 1.98 / 2.10 / 12.09 / 12.21 / 12.50)**全 > 1.50**。

**任务里点名要查的 `skill_ratio` 确实同病,而且按量程算比 `ser_roi` 更重**:A 臂看到的是
一条真实跨度 1.06→12.50 的列上宽 0.076 的一小片,约 **0.7%** 的量程。`ser_roi` 那边 A 臂
覆盖了约 18% 的量程、且负半边一个观测都没有。

被挡的 9 行明细:

| 系列 | 期 | 日 | 流 | 原因 | `ser_roi` | `ser_n` | `skill_ratio` |
|---|---|---|---|---|---|---|---|
| KXAAAGASW | 2026-07-06 | 06-30 | edge / argmax | skill_blocked | +0.3399 | 6 | 12.4971 |
| KXAAAGASW | 2026-07-13 | 07-07 | edge / argmax | skill_blocked | +0.1179 | 7 | 12.2056 |
| KXAAAGASW | 2026-07-27 | 07-21 | argmax | skill_blocked | +1.0960 | 9 | 12.0915 |
| KXJOBLESSCLAIMS | 2026-07-16 | 07-10 | edge | skill_blocked | −0.5477 | 7 | 2.1040 |
| KXJOBLESSCLAIMS | 2026-07-23 | 07-17 | edge | skill_blocked | −0.0573 | 8 | 1.9845 |
| KXJOBLESSCLAIMS | 2026-07-30 | 07-24 | edge | skill_blocked | −0.1718 | 9 | 1.8997 |
| KXNATGASW | 2026-07-10 | 07-04 | edge | series_disabled | −0.8655 | 7 | 1.0653 |

**顺带看见一件此前在这张表里根本不可见的事:两个门在 KXAAAGASW 上指向相反。**
skill 说 12.5(远超 1.50,封),`series_enable` 说 +0.34 / +0.12 / **+1.096**(赚钱,该开)。
这不是新 bug —— #124 已经把 KXAAAGASW 的 skill 封锁**复核为正确**(当时 ratio 8.35);
值得记的是:在 A 臂的特征表里,一个 `ser_roi = +1.096` 的系列被封这件事**一行都不会留下**。
这正是 #147 想让人能看见的那类事实。

### 这次运行**不**授权什么

1. **不拿它下判决。** 重跑的是同一个 61 天窗,PREREGISTER §25.1 第 1 条:判决只能来自前向窗。
   这一节的全部内容是"这一列现在可读了",不是"§25.4 被验证了"。§25.4 的前提在这份数据上
   **仍然未被检验**。
2. **没有拿新行重拟 §25.3 的置信模型,也不会拿它的系数说事。** 在同一个窗上放开一个特征再读
   系数,就是在 51 个点上做变量选择 —— §25.17 已经为 `ser_roi` 拒绝过一次(那个 −0.3378
   明确写了"不该被引用"),这里不重犯。
3. **没有跑门控 ROI。** 同上,理由同 §25.17。
4. **默认仍然 False。** 一个会自己打开的诊断开关不是诊断,是没交代的策略变更
   (`test_the_flag_is_off_by_default`)。

### 成本

A 臂约 17 分钟,B 臂约 34 分钟(同机当时还跑着另一个满核任务,这个约 2× 不应被当作开关的
固有代价引用)。这也是为什么这条端到端证据放在文档里而不是测试套件里 ——
`tests/test_walkforward_shadow_blocked.py` 的 9 条是源码级断言,守的全是"删掉也不报错"
的遗漏型失效,端到端那一条由本节承担。

测试 9 条全绿;宏观全量 506 条全绿。

---

## §25.20 #149 —— 一次平仓到底关掉了哪个仓位:四个"我已经持有了吗"里三个算错

### 症状:同一个 ticker、同一个价格、相隔 15 秒的两笔真实开仓

`decisions` 3638(18:31:13)和 3697(18:31:28),都是 `kind='open'`、都是 `favorite_path`、
都是 `net_edge=+0.0546 fair=0.9346 cost=0.8700`、都在 `KXWTIW-26AUG0714-B78.50` 上、
各自带一组 fills。这不是一笔被记了两次,是两个仓位。发生在 2026-08-06 的活账本上。

`strategy/decision.py:65` 里 `already_open` 是挡住加仓的**唯一**一道:

```python
if already_open:
    return Decision("pass", None, 0.0, 0, ("already_open_no_averaging_down",), ...)
```

所以问题只可能是那个 flag 读错了。

### 根因:分子按流分,分母不分

四个函数回答同一个问题,写法各自为政:

| 函数 | 分子 | 分母 |
|---|---|---|
| `ops/ledger.has_open` | `kind='open'` | `exit`\|`cancel`\|`settle_note` |
| `strategy/arb._has_open_arb` | `kind='arb'` | 同上(全部) |
| `strategy/snipe._has_open_snipe` | `kind='snipe'` | 同上(全部) |
| `ops/decide_all._has_any_open` | `open`\|`argmax`\|`arb`\|`snipe` | 同上(全部) |

只有最后一个是平衡的。前三个每次都在减一个自己从没加过的数。

根子在写入端:`ops/exits._write_exit` 记 `series`/`period`/`structure_json`,**但不记它关掉
的是哪一笔**。所以一行平仓在库里只能被读成"这个 period 上发生过一次平仓",没法读成"这个
仓位没了"。KXWTIW 2026-08-07 当时是 2 笔 open + 4 笔 argmax + 3 笔 argmax 的 exit,
`has_open` 算出 `2 > 3 = False` —— 三笔 argmax 的平仓把 edge 流的计数吃掉了。

### 量化:66 个 period 全量重放,不是推断

把每一行平仓按 structure_json 归到它真正关掉的那个仓位(FIFO 兜底),再和四个检查各自
的返回值逐 period 对:

```
has_open         分歧 1 个 period      <- KXWTIW 2026-08-07,就是那两笔
_has_open_arb    分歧 0
_has_open_snipe  分歧 0
_has_any_open    分歧 0                <- 平衡的那个,当基准用
```

**arb/snipe 的 0 是数据事实,不是结构事实。** 它们的 SQL 和 `has_open` 一样错,只是至今还
没有哪个 arb/snipe 仓位跨过一次别的流的平仓。把 0 读成"这两个是对的"是这次最容易犯的错。

### 修法:平仓写清楚自己关了谁,四个检查读同一个实现

`decisions` 加 `closes_decision_id INTEGER`(纯新增列,append-only 不破)。三个写平仓的地方
全部落这个字段:

* `ops/exits._write_exit` → `pos["id"]`(`pos` 本来就是一行 decision,一直有 id)
* `ops/pnl` settle → `pos["id"]`
* `ops/retire_stale_book` → `r["id"]`。这个写入端**本来就知道**答案,只是把它塞在
  `inputs_json` 的 `retires_decision_id` 里,任何计数查询都够不着。同一个值,挪到列上。

读端收敛成一个 `ledger.open_decisions(conn, series, period)`,四个检查全走它。
`_has_any_open` 本来是对的也一起改 —— 让唯一正确的实现继续当唯一的私有副本,正是这四个
当初分头长歪的方式(#141 是现成的账单)。

`decisions` 是 `CREATE TABLE IF NOT EXISTS`,DDL 只到得了新库,活账本得靠 `init_db` 里那条
幂等 `ALTER`。少了它,这个列就只在测试里存在 —— 两头都不落好。

### 历史行的桥:NULL 走 FIFO

活账本**全部** 119 行平仓都早于这个列,`closes_decision_id` 是 NULL(#149 只是把写入端接上了,
之后还没写过一行平仓;初稿这里写的"53 行里 40 行"是把它和下面 (b) 的 `realized_usd`
计数串了,#150 复核时按库改正)。要是让它们什么都不关,
全部历史仓位会重新变成"未平",`exits`/`settle` 会去重新处理几周前就结束的账 —— 比原来的
bug 坏得多。所以 NULL 按 FIFO 退最老的一笔,也就是旧 `open_positions` 查询给它们的读法。

这不是猜的:整本活账本两种读法各跑一遍,8 个未平仓位、66 个 period **全部一致**。桥是可证
明不回归的。

### 验收(活库副本,不碰生产)

```
closes_decision_id 迁移后存在      True
历史行被追认的数量(必须 0)        0
66 个 period 里行为变化            1     <- ('KXWTIW','2026-08-07') has_open False->True
open_positions 的 id 集合          [3089,3094,3187,3192,3259,3284,3638,3697]  新旧完全相同
fills 仍挂在仓位上                 True (8 笔)
```

唯一一处行为变化,方向正确:那个 period 现在读成"持有中",第三笔重复开仓不会再发生。

测试 11 条(`tests/test_open_attribution.py`),宏观全量 506 条全绿。

### 顺手澄清两件差点被写错的事

**(a) KXPCECORE/KXCPICORE 的"巨额 churn"不是新 bug,别开 task。** KXPCECORE 2026-11 有 14
笔 open、KXCPICORE 2026-09/10 各 11 笔,看着像一个比 #148 大得多的同周期反复开平环。它是
两个**已修**缺陷的历史脚印:#141(exits 用 `min()` 聚合腿边际,2026-07-31 修)和 6b625b4
(stop marking off a midpoint no one would trade at,2026-08-04)。账本自己把界划得很干净:
2026-07-31T12:41 之前的平仓行 `inputs_json` 是 `{"hold_edges": [...]}` 且没有 realized 字段
(旧代码),之后是 `{"exit_note","realized_usd"}`(现行代码)。**40 笔反复开平全在分界之前。**

这里差点写错一句。初稿写的是"2026-08-04 之后全账本只剩 3 笔同周期开平,全是 argmax"。
把全账本按"平仓真正关掉了哪个开仓"配对、算持仓时长之后,08-04 之后是 **4** 笔 0 秒往返,
其中一笔是 edge 流的:KXCPIYOY 2026-07,3197 → 3229,`+0.0999` 进、`-0.3692` 出。

但它也不是新缺陷,而且理由要看**部署**时间而不是 commit 时间。这笔的账是:

```
进 T3.2 yes @0.91 + T3.3 no @0.38  -> cost 0.29
出 T3.2     @0.89 + T3.3     @0.36  -> 0.25        标只动了 4 分
realized = (0.89-0.91)*3 + (0.36-0.38)*3 - 手续费 0.15 = -0.27   ✓ 与 note 一致
```

标只动 4 分,`hold_edge` 却报 -0.3692 —— 对着 `fair=0.4199` 反推要求 fair 掉到负数,
不可能。这就是 6b625b4 要修的那个中点/取最坏腿的假边际。而它的 note 还是 `worst=`;全账本
最后一笔平仓(3704,08-06T18:31)才是 `hold_edge=`。**所以 6b625b4 是在 08-05T12:31 与
08-06T18:31 之间才上的线,08-05 那笔跑的仍是旧代码。** commit 日期不是部署日期,note 的
格式才是账本里能查证的那个界。

按部署界重述:6b625b4 上线之后,全账本的同周期开平只剩 3 笔 argmax —— 也就是 #148,没有
别的。对照组:KXNATGASW 2026-08-07,08-01 开、持有五天、+$1.19 平掉。

**(b) 53 行 exit 里只有 11 行带 realized 数字。** 另外 42 行的 `inputs_json` 是
`{"hold_edges": ...}`(40 行,#141 之前)或 `{"exit_note": ...}`(2 行),那个字段比它们年轻。
对历史求和会把这 42 行静静读成 0。我自己就用 `.get(..., 0.0)` 踩了一次,得到"KXPCECORE 13 次往返 realized
+0.00",这个数根本不存在 —— 和 §25.18 里那个 tz 偏移的 SQL 是同一类:**默认值和空结果都会
伪装成一个干净的答案。**

---

## §25.21 #150 —— #149 只修了写入端:三个读账的地方仍然把平仓配给"周期"

### 这一节和 §25.20 的关系

#149 把"我已经持有了吗"那四个**写入侧**的检查收敛到了一个实现。它没碰**读侧**。读侧有三个
地方在回答同一个问题(这个仓位平了没有?),三个都还是老问法,而且三个都直接出现在钱上:

| 位置 | 它问的 | 错法 |
|---|---|---|
| `ops/frontend_export` | 这个 (series, period) 上有没有 id 更大的 **`settle_note`** | exit 不算平仓 → 已离场的仓位永远挂在展示的持仓表上;`LIMIT 1` 把一条结算记给该周期上**每一个**仓位 |
| `ops/pnl.report` | `GROUP BY series, period` 去重 | 平的是**仓位**不是周期 → 一个周期上多个仓位分别结算时只留一个 |
| `ops/risk` | `NOT EXISTS(这个 period 上有更晚的平仓)` | 这不是计数:一条平仓把该周期上所有仓位都藏了 |

三个方向各不相同,但共同点是:**#149 让账本第一次有能力回答"关掉的是哪一笔",这三个还在按
老办法问。**

### 症状里最要命的一条:展示的成绩单今天就是错的

`frontend_export` 是客户看到的那张表。它只认 `settle_note`,而活账本上 53 行平仓里 **53 行都是
`exit`**(只有 7 个仓位是被 `settle_note` 关掉的)。所以:

```
                      展示(修前)          真值(修后)
未平仓位              12 笔 / $9.55        8 笔 / $6.39
已平仓位              0 笔  / $0.00        4 笔 / -$0.48
```

12 笔里有 4 笔是幻影:3100、3161、3222(KXWTIW 2026-08-07)和 3197(KXCPIYOY 2026-07)。
它们早就离场了,展示端既没把它们从持仓里拿掉,也没把它们的已实现盈亏计进成绩单 —— **一笔
在两边同时消失。**

`LIMIT 1` 那条今天还没发作,但离发作只剩一天:KXWTIW 2026-08-07 上现在同时挂着 3 个仓位
(3284 argmax、3638 open、3697 open),**明天结算**。一旦第一条 `settle_note` 落地,老代码会把
它的 realized 记给这三个仓位各一次。

### 修法:平仓配对只有一个实现,和 `open_positions` 同一次重放

`ledger._replay(conn, series, period)` 一次扫描返回两半:`(还开着的行, {open_id: 关掉它的那行})`。
`open_decisions` 取第一半(行为逐位不变),新的 `closures()` 取第二半。三个读账的地方全改走它。

一次重放而不是两个函数,因为这本来就是同一个问题问两遍 —— 而每一个"再问一遍"的私有副本
都问歪了,这是 #141 和 #149 已经付过两次的账。

配套两个东西:

* `closures(conn, kinds=...)`:`pnl.report` 只要 `('settle_note',)`,断路器要 `('settle_note','exit')`。
  **`cancel` 不是平仓** —— 那 43 行是 #121 按规则退掉的被否认的 pre-cutover 账,展示端专门排除
  在外。我第一版把 `CLOSE_KINDS` 整个喂给了断路器,等于让一个记账动作去推一个风控。改回来了。
* `realized_usd(close)`:**没记就是 `None`,不是 0**。见 §25.20(b) —— 42 行 exit 根本没有这个字段。

### 平仓行(119)多于被平掉的仓位(103)

这是这次最容易读错的一处,单独记一下:

```
exit          53 行 -> 配上 53 个仓位,0 孤儿
cancel        43 行 -> 配上 43 个仓位,0 孤儿
settle_note   23 行 -> 只配上 7 个仓位,16 行孤儿
```

那 16 行是 #149 之前的 `settle_pass` 对**已经结掉的仓位**反复再结一次(KXFED / KXFEDDECISION
2026-07 各被结了 6 次)。**按行求和 = -6.07,按仓位求和 = -4.37**,后者才是发生过的事。
老的 `GROUP BY series, period` 给出的也是 -4.37 —— 它凑巧对,因为活账本上每个多结算周期底下
恰好只有一个仓位。**凑巧对不等于对**:明天 KXWTIW 结算,同一个周期上是三个真仓位,老写法会
丢掉三分之二。

### 验收(全部在活库副本上,不碰生产)

```
不变式        8 未平 + 103 已平 = 111 行开仓                    OK
open_positions id 集合   [3089,3094,3187,3192,3259,3284,3638,3697]  与 #149 完全相同
pnl.report 已实现合计     -4.3700  == 按仓位配对的真值            OK
risk._open_exposure       8 笔 / $6.39                            与修前一致(潜在修复)
test_open_attribution.py  11 条全绿(#149 的行为一条没动)
```

`frontend_export` 真跑一遍(输出到 `/tmp/fe_out`):未平 8 笔、已平 4 笔且 `closed_by` 全是
`exit`。断路器写出 `rolling20 disarmed: 18/20 scored closures (42 closes carry no realized_usd)`,
同日重跑只有一行。

新增 `tests/test_close_pairing.py` 15 条,宏观全量 **521 条全绿**。

### 断路器其实一次都没武装过,而且它自己不说

`check_rolling20` 要 20 笔已计分的平仓。42 行 exit 没有 realized 数字,过去被**静默丢弃**,
剩 18 笔 < 20,于是函数 `return None` —— 和"看过了,没有回撤"是同一个返回值。

现在缺数据会**出声**:写一行 `alerts`(`source='risk'`, `level='warn'`,每天最多一行)。
故意**不**写成 `circuit_breaker` 行:那是铁律 10 留给"实测到的回撤"的,而且它需要人工 ack 才
解除;这个警告在凑够 20 笔的那一刻自己就没了。**一个看不见东西时把自己关掉的风控,比没有这个
风控更糟 —— 没有的那个至少是可见的。**

### 必须写清楚的一件事:这个修复让展示的数字变好看了

```
合计   笔数 51 -> 55   投入 $40.89 -> $44.05   已实现 -$9.61 -> -$10.09   ROI -23.502% -> -22.906%
```

已实现亏损**变大**了(多认了 4 笔真实亏损),但 ROI 反而**改善** 0.6 个百分点,因为新认的这
4 笔平均亏 15.2%,比历史平均的 23.5% 好。同时展示的在险敞口从 $9.55 降到 $6.39。

方向对我有利,所以说明白它不是挑出来的:**所有**此前被漏掉的、且在 cutover 之后的平仓仓位
一笔不落全部加了进来,没有任何筛选。补充一句校正 —— 我第一次写 task 时说漏掉的是 +$0.71
含 KXNATGASW 3704 的 +1.19,查下来 3704 关的是 2400,开仓日 2026-08-01,属 **cutover 之前**,
按 #123 本来就该排除。真正漏掉的是 **-$0.48**,四笔,全是亏的。

**大局没变:** 回测 61 天 -25.275%,#129 "这本账上没有能盈利的预测模型"的结论一个字没动。
这一节修的是**计量**,不是盈利能力。

---

## §25.22 #151 —— 回测和实盘跑的不是同一条规则:八处(F1–F8),其中两处让回测偏乐观

### 这一节和 §25.20 / §25.21 是同一类病,换了一个器官

那两节讲的是"账本里同一个问题有四个/三个实现,互相不一致"。这一节讲的是同一个问题的
**跨进程版本**:一条规则在 `research/walkforward.py`(产出展示数字的那条回测)里是一个样子,
在 `ops/decide_all.py`(每天真下单的那条实盘)里是另一个样子。

这类分歧比普通 bug 危险,因为**它不会报错,也不会让任何测试变红**。它唯一的症状就是:
回测报出来的那个数,没有任何一天真的被执行过。

### 总表

| # | 位置 | 回测怎么做 | 实盘怎么做 | 方向 |
|---|---|---|---|---|
| **F1** | §25.4 `series_enable` 逐系列开关 | PIT 重算,d75 窗里 `series_disabled` **触发 13 次** | 读 `experiments` 存档行 —— 库里 **0 行**,从未触发过 | **回测偏乐观** |
| **F2** | §27.4 AAA 信息劣势门 | **没有对应实现**,6 笔全部照下 | `mode != aaa_daily_anchor` 一律拒 | **回测偏乐观** |
| **F3** | `model/ensemble.learn_weights` | — | `pooled` / `chronos` 混进权重,还当上了 trim 的基准 | 潜伏(实测未发作) |
| **F4** | `series_enable.blocked` | — | `metrics_json` 为 NULL 时 TypeError 逃出,**整个当日循环中止** | 潜伏(存档行为空时才发作) |
| **F5** | walkforward 的 `decide()` | 不传 `release_ts`,`freeze_window` 门**永远无法触发** | 传 | 本窗口实测不动 |
| **F6** | walkforward 的 argmax 腿 | edge / argmax 两条流**各扣一次**同一个风控钱包 | 实盘每个事件只持一条腿 | 本窗口实测不动 |
| **F7** | `risk.check` 的当日敞口上限 | — | 五道上限里四道数全部 `OPEN_KINDS`,**第五道只数 `kind='open'`** | 潜伏,且**失效方向朝开** |
| **F8** | `strategy/snipe.run_for` | — | 直接写 `decisions`,**五道上限一道都不过** | 可达 |

F5/F6 我照样修了,理由和 §25.21 一样:"这次没发作"不是"这次是对的"。
F7/F8 是查 F1–F6 时顺着"同一个问题有几个实现"这条线索继续挖出来的,不在原来的六条里。

---

### F1 —— 一个只有回测能用的闸门

`strategy/series_enable.py` 有两条路径算同一个折叠:

* 回测路径 `research/pit_gates.GateHistory` —— 每个模拟日现算,只用严格更早收盘的事件。
* 实盘路径 `series_enable.blocked` —— 读 `eval.run_series` 写进 `experiments` 的存档行。
  之所以不现算,是因为那是每系列几分钟,而 `decide_all` 每天要跑全 registry。

问题是存档行**从来没被写过**。查活库:

```
experiments 里 name='series_enable' 的行数: 0
最后一次 eval.run_all: 2026-08-04
series_enable.py 落地:  2026-08-05
```

模块比最后一次周更晚了一天,而刷新是**周级**的(`ops/refresh.py` 的 `weekly_eval_gates`)。
于是 2026-08-06 这天,14 个系列全部走"无行 ⇒ 放行"这条默认路 —— 而同一天发布的 d75 回测里,
`gate_stats` 记着 `series_disabled: 13`。

**fail-open 本身是对的**(一个从没评估过的否决门必须放行),错的是**它一声不吭**。所以修法
不是把默认改成拒绝,而是让"没评估过"这件事说出来。新增 `series_enable.unevaluated()` 和
`decide_all._warn_unevaluated_series_gate()`,形状照抄 `risk._breaker_blind`:**只告警,不跳闸**,
每天最多一条。实测(在库副本上跑两遍):

```
pass 1 -> alerts: 1
pass 2 -> alerts: 1          # 幂等
series_enable gate unevaluated for 14/14 series (KXFEDDECISION,...,KXGDP)
  — §25.4 cannot fire until weekly_eval_gates runs
```

#### F1 的后果要单独说:它一旦生效,今天就会停掉两个系列

把折叠在当前 `decision_replay` 上跑一遍,存档行**如果**现在写进去,结果是:

| series | enabled | n | roi |
|---|---|---|---|
| KXJOBLESSCLAIMS | **False** | 10 | −0.2895 |
| KXWTIW | **False** | 6 | −0.27462 |
| KXNATGASW | True | 11 | +0.1452 |
| KXAAAGASW | True | 11 | +0.91285 |
| 其余 10 个 | True | 0 | None(事件数结构性到不了 MIN_N=6) |

**我没有去写这一行,这是要用户定的。** 理由是 §25.16 / §25.18 的登记纪律:PR-2(0/20)和
PR-7/S2(4/30)正在前向采样中,而"哪些系列在下注"就是它们采样的那个总体。在登记中途改总体,
和中途改判据是同一类事 —— 这正是 #148 被挂起不修的那条理由,不能对自己网开一面。

---

### F2 —— 回测赚得最少的那个系列,恰恰是实盘不许碰的那个

`_aaa_information_gate`(§27.4)的原始注释写着:

> 刻意只做实盘侧 —— 回测不需要对应实现,因为每一行 AAA_DAILY 的 `knowledge_time` 都 ≥
> 2026-07-31T19:46Z,所以 2026-07-31 之前的任何重放都看到空序列、**按构造走 proxy 分支**。

**前提是真的,从前提推出的结论是反的。** proxy 分支**就是被拒的那个分支**。所以
"2026-07-31 前的重放必走 proxy" 不是"所以不用管",而是"所以那段窗口里的每一笔 AAA 都处在
这个门专门要拒绝的状态",回测照单全收了。d75 窗里 6 笔 KXAAAGASW 全部落在 05-25..06-29。

修法:在 walkforward 里 `pred = fn(...)` 之后立刻做同一个判断(必须在这里,因为它要读 `mode`,
比实盘那道门的位置晚),记 `blocked_by = "aaa_proxy_only"`,并在 `_GateBook.stats` 里加计数。

实测代价(存档 run `d75:model:end2026-08-04`,edge 流):

```
AAA 6 笔        staked 5.07    realized -0.35    roi  -6.90%
edge 流 全量    staked 29.40   realized -8.72    roi -29.66%
edge 流 去 AAA                                   roi -34.40%
```

要说清楚这里发生了什么:AAA **不是赚钱的**(−6.9%),它是**亏得最少的**。把它从一本平均亏
29.7% 的账里拿掉,剩下的自然更难看。也就是说,回测里表现最好的那个系列,是靠在一个
"对手方已经知道答案而我们不知道"的状态下下注拿到的,而实盘规则明确禁止这个状态。
**这条修复只会让展示的数字更差,方向对我不利,所以它更需要被写下来。**

---

### F3 / F4 —— 两个潜伏项

**F3.** `learn_weights` 从 `source_scores` 读 Brier,但 `source_scores` 是一块**通用记分板**,
不是这个池子的成员名单。它同时存着 `pooled`(由 `eval.run_series` 写,而 `pooled` 是
model+market 在**同一批事件**上的确定性函数)和 `chronos`(§7-bis 影子成员)。两个都进了权重、
进了 floor/ceiling 裁剪,更糟的是**进了 trim 的 `best` 基准** —— 一个影子成员在决定真实成员被
判得多严。KXAAAGASW 上实际算出来是 `{'pooled': 0.49, 'market': 0.51}`:一半权重给了池子自己的
导出量,而且被原样写进了每一行 `inputs_json["weights"]`。

修法是加 `MEMBERS = tuple(PRIOR)` 并在读取时过滤。**但我要把话说准:这是潜伏修复。**
`log_pool` 只保留它真正拿到 pmf 的源并重新归一,幸存者之间的比例不变;当前没有任何权重
碰到 0.10/0.70 的夹子;四个有学习权重的系列逐个查过,**只按成员 trim 之后留下的真实成员完全
相同**。所以 pmf 没被改过。它是"哪天夹子第一次绑上"那天才会发作的东西。
(我最初收到的报告说这导致 KXAAAGASW 的 `model` 被 trim 掉了;查原始 MSPE 后不成立 ——
`model` 0.1317 对 `market` 0.0158,无论 `pooled` 在不在都会被 trim 掉。写在这里免得日后被当成
一个更强的结论引用。)

**F4.** `blocked()` 里 `json.loads(r["metrics_json"])` 在该列为 NULL 时抛的是 **TypeError**,而
`except ValueError` 不接 TypeError。它会从 `decide_all` 的逐系列循环里逃出去,**中止当天整个
决策循环**。兄弟模块(`calibration._load`、`conformal.sizing_factor`)早就是 `or "{}"` + 双异常
的写法,这里照抄。这条和 F1 是一对:F1 让存档行不存在,F4 决定了存档行存在但内容为空时会
发生什么。

---

### F5 / F6 —— 两处回测/实盘不同构,本窗口实测不动

**F5.** `strategy/decision.py:66-70` 的 `freeze_window` 门是按 `release_ts` 判的,而 walkforward
的 edge 流调 `decide()` 时**根本不传这个参数**,所以这道门在回测里永远是关着的。修法是加一个
`release_cache`,从 `releases` 表按 `(cal, period)` 查 `scheduled_ts` 传进去。

**F6.** argmax 腿在记账时,除了写 `opened_argmax`,还无条件往 `open_rows` / `opened_today` 里
加了一笔 —— 而 edge 流在同一个事件上可能已经加过一次了。风控钱包(`_sim_risk_veto` 读的那个)
于是被**同一个事件扣了两次**。实盘每个事件只持一条腿,所以对上限可见的是**混合账本**。
修法:argmax 只在 `(series, period) not in opened` 时才扣钱包,和 `_open_rows` 建 `hybrid_book`
用的是同一条规则。

F6 的方向是**偏保守**(钱包被多扣 ⇒ 回测比实盘更早触发风控否决),但方向对我有利不等于它是对的,
一样修。

#### 顺带修掉一个"喊狼来了"的测试

F6 加了一层缩进和一段注释之后,`test_pnl_bearing_state_is_only_touched_under_the_guard` 红了:

```
AssertionError: open_rows.append( at 43772 is not under the #147 guard
```

守卫**一直在**,红的原因是那个测试往回扫的是**固定 500 个字符**,注释和多出来的一层缩进把
`if blocked_by is None:` 顶出了窗口。这是个假阳性,而假阳性比没有测试更危险:一个在正确代码上
喊狼来了的断言,下一个撞上它的人会直接把它改弱 —— 而它守的恰恰是 #147 那条"删掉不报错、
没有任何测试会动"的静默腐蚀性质。

所以修的不是注释长度(那只是把同一个引信重新装上),而是把断言改成它本来的意思:
新增 `_enclosing_blocks()`,按缩进求出**词法上包含该行的所有块头**,断言其中有
`if blocked_by is None:`。这对 Python 是精确的,比字符距离**更强**。反向对照跑过:

```
guarded   -> True  ['if k not in opened:', 'if blocked_by is None:', 'for ev in x:', 'def run():']
UNguarded -> False ['if k not in opened:', 'for ev in x:', 'def run():']
```

---

### F7 —— 同一个函数里,五道上限用了两套 kind 集合

`risk.check` 一共五道闸。前四道(per_event / per_family / per_cluster / gross)读
`_open_exposure` → `ledger.open_positions`,数的是**四种 `OPEN_KINDS`**(open / argmax /
arb / snipe)。第五道(per_release_day $30)自己写了一条 SQL:

```sql
SELECT SUM(size_usd) FROM decisions WHERE kind='open' AND ts_utc>=?
```

于是一条 argmax / arb / snipe 腿**不消耗当天的额度**。这就是 #149 和 #150 在平仓侧修掉的
那个"一个问题两套 kind 集合",活到了**同一个函数内部**。

它的失效方向是**朝开的**(少读了今天的敞口 ⇒ 放行本该被拦的单),对一道风控闸来说这是坏的
那一边。今天还是潜伏:argmax 全生命周期 4 行 / $3.19,混合口径最大的一天是 2026-08-05 的
$6.10,对 $30 的闸。反向对照:

```
OLD query (kind=open only): $0.00  -> +$1 = $1.00  vs cap $30 -> PASSES (bug)
NEW query (all OPEN_KINDS): $29.50 -> +$1 = $30.50 vs cap $30 -> VETO
```

**顺手排掉一个悬案。** 之前记着两天超了 $30 闸没有解释:07-28 的 $48 和 07-29 的 $32。
按 kind 拆开之后,两天**都是纯 `kind='open'`**(混合口径合计 == 只数 open 的合计),所以
它们跟 F7 无关 —— 破的是那道本来就在数的闸。这 80 行是账本头两天、id 从 1 开始。
**它们当时为什么没被拦下来,仍然是个没答案的问题**,写在这里免得被 F7 顺手"结案"掉。

新增 `tests/test_daily_cap_kinds.py`(7 条)。夹具的做法是当天开、当天平:这样前四道闸看到
的是空账本,能把第五道单独隔离出来,同时也钉住了这道闸真正的语义 —— 它管的是当天新增敞口的
**流量**,不是收盘时还站着的**存量**。四种 kind 全部参数化,包括从没跑过的 arb / snipe:
**恰恰是没跑过的那两个,出了 bug 才最不会被发现。**

---

### F8 —— snipe 开仓不过任何一道风控闸

`decide_all` 在 edge 腿(~476 行)和 argmax 腿(`_place_argmax`,~145 行)之前都调了
`risk.check`。`strategy/snipe.run_for` 直接往 `decisions` 里写,唯一管着它的只有它自己的
`MAX_SNIPE_USD = 2.0`。

这条是**可达的**,不是理论上的:`_has_open_snipe` 把这条路限死在每个 (series, period) $2,
而 edge 流在**同一个 period** 上本来就可以持到 `per_event_usd = 5.0` —— 两边一加就过线,
而且没有任何东西会说话。snipe 是**方向性**的(它买的是已公布数值所蕴含的那条腿),所以它的
最大亏损**就是**它的本金,五道闸原样适用。

修法:在写库前调 `risk.check(conn, series, period_key, stake)`,不过就 `continue`。同一个
连接上未 commit 的插入是可见的,所以同一轮循环里的第二条腿会拿第一条已经占掉的额度来判。
顺带把记账用的金额和送去过闸的金额**收敛成同一个变量 `stake`** —— 这两个数一旦分叉,就是
#132 那个 bug 的形状(按一个数管闸,按另一个数记账)。

**没有对 `arb.execute` 做同样的事,这是有意的。** 一个 arb 的保底收益 ≥ 它的成本,所以它的
最大亏损**不是**本金;拿方向性亏损上限去卡一个锁定利润的结构,是另一个问题,两边都有说得通
的道理。而 `arb` 在这本账上**一次都没触发过**,我没有任何证据可以据以判断。所以它被写下来,
不是顺手改掉。(相关的还有 `strategy/arb.py:52` 把**毛名义**记成 `size_usd` —— 同样留着。)

新增 `tests/test_snipe_risk_cap.py`(5 条)。第一条是
`test_the_fixture_actually_reaches_the_open`:夹具要靠 monkeypatch 撑起四个依赖,少了这条
"无阻碍时确实会开仓"的对照,夹具里一个笔误就能让另外四条**全部空过**。

---

### 验证

```
prediction_market_macro/tests:  533 passed   (新增 F7 7 条 + F8 5 条)
```

F1 的告警路径在**库副本**上验过(幂等,14/14),没有往生产库写过一条 alert。
d75 的合并重跑同样跑在 `/tmp` 的库副本上 —— 存档行 `d75:model:end2026-08-04` 是 §25.17 / §25.19
引用的基线,用同一个 `cfg_hash` 重跑会把它原地覆盖掉,那等于毁掉这次重跑要做的那个对比。

#### d75 合并重跑(2026-08-06,库副本,`days=75 fair_mode=model end=2026-08-04`)

```
             修前(存档)                  修后
edge     roi -0.29660  n 36        roi -0.34402  n 30
argmax   roi -0.09197  n 34        roi -0.07515  n 29
hybrid   roi -0.23262  n 49        roi -0.25637  n 43
```

新增门控计数:`aaa_proxy_only: 34`(拦掉的事件数;其中 6 个是原本已经成交的那 6 笔)。

**这次重跑同时是一次独立校验。** 我在跑之前用存档 run 的逐笔明细算过一遍:把 6 笔 AAA
从 edge 流里减掉应得 `(-8.72+0.35)/(29.40-5.07) = -0.34402`。重跑实测 **-0.34402**,小数点后
五位一致,n 也正好 36→30。也就是说这道门**恰好**拿掉了那 6 笔、没有顺带动到别的东西 ——
这正是我要的证据,否则"数字变差了"和"我把回测改坏了"是分不开的。

`series_disabled` 仍是 13,和修前一致 —— F1 修的是**实盘侧**的沉默,回测侧本来就在算这个折叠,
所以它不该变,而它确实没变。

### 展示口径:这一节让数字变差,而重新 freeze 不是我能定的

F2 单独一项就把 edge 流从 −29.66% 推到 −34.40%。按 #123 / #131 的协议,展示段的重新
freeze 是一个需要用户拍板的动作,我只把测量做出来。#129 的结论("这本账上没有能盈利的预测
模型")不但没动,而且被**加强**了:此前账面上亏得最少的那个系列,现在确认是在一个实盘不许
交易的信息状态下取得的。

---

## §25.23 #151 续 —— F9,以及两处"回测根本没在采样实盘会做的那批交易"(A2/A3)

F1–F8 收尾之后我又跑了两个只读审计 agent。它们报了一批东西,我逐条回源码核过,**只保留自己
验证成立的**。结果分三类:一个新 bug(F9,已修),两个**结构性**差异(A2/A3,不修、披露),
以及若干未验证的线索(记在末尾,不当结论用)。

### F9 —— `pnl.report` 同一个返回值里,两份"当前持仓"互相打架

`ops/pnl.py:report()` 一次返回 `open_by_series` 和 `open_by_kind`。#150 把后者改成了读
`ledger.open_positions`,前者原封不动留着一条裸 SQL:

```sql
SELECT series, COUNT(*) n, SUM(size_usd) staked FROM decisions WHERE kind='open' GROUP BY series
```

两个缺陷叠在这一条语句里:

1. **完全没有平仓核算。** 没有任何东西 join `closes_decision_id`,所以这本账**开过的每一个
   仓位都还算作持有中**。这是主项。
2. `kind='open'` 单独取,argmax/arb/snipe 的持仓根本不出现 —— 和 #149 / #150 / F7 同一个
   "一个问题、两套 kind 集合"的裂缝。

实测(生产 db):

| | 仓位数 | 敞口 |
|---|---:|---:|
| 修前 `open_by_series` | **107** | **$105.36** |
| 账本真值 = 修后 `open_by_series` = `open_by_kind` | **8** | **$6.39** |

也就是虚报了 13 倍。**减轻情节:`pnl.report` 是运维/CLI + 测试面**,`refresh.py` 和
`jobs/tick.py` 都不调它,所以没有任何对外发布的数字是从这里算出来的 —— 否则这就是一起事故
而不是一个 bug。

修法上我没有把那条 SQL 补成带 join 的版本,而是让两份口径**走同一次遍历、同一个源**
(`ledger.open_positions` 一趟同时累加 by-kind 和 by-series)。理由是这个 bug 的病史:#149 和
#150 各自编辑过 `report()` 里的 closures 段和 open_positions 段,**两次都从第 218 行旁边走
过去了**。只要两份口径还是两段代码,第三次就还会漏。合成一趟之后它们不可能再不一致,因为已经
没有第二个实现可以不一致了。

测试 `tests/test_pnl_report_open_by_series.py`(7 个):断言的是**不变量**(两份切法总额相等)
而不是今天的数字,parametrize 覆盖全部四种 `OPEN_KINDS`(`arb`/`snipe` 实盘从未触发过 ——
这正是它们身上的 bug 不会被人发现的原因),外加"全平仓的账必须读作空"这个退化用例。

### A2 —— 回测把深度门关掉了,实盘 202 次 pass 是被这道门单独挡下的

| | 实盘 | 回测 |
|---|---|---|
| `min_leg_depth_usd` | `strategy/decision.py:20` = **50.0**,在 `:89` 强制 | `research/walkforward.py:480` 显式覆盖为 **0.0** |
| 腿深度 | 真实 order book | `:583` 一律盖成 `bid_depth/ask_depth = 1e9` |

实盘 3474 条 `pass` 里,**282 条**含 `depth_fail`,其中 **202 条 `depth_fail` 是唯一理由** ——
即这 202 次交易实盘只因为深度不够就没做,回测里它们全部照做不误。

这一条的方向是**明确偏乐观**的:它让回测能成交一批实盘成交不了的腿。但它是**结构性**的 ——
Kalshi 的日 K 线里没有深度字段,不存在"把这道门补回去"的正确实现,只能在假深度和无深度之间
选,而 `1e9` 至少是诚实的假(源码里已有注释写明)。所以处理是**披露**,不是补代码。

### A3 —— 回测的每一笔,都是实盘的新鲜度门会拒掉的那一笔

实盘 `ops/decide_all.py:292-311` 有一道硬门:预测 >26h 或**最新报价 >6h** ⇒ 强制 PASS,
理由 `stale_inputs`(实盘 164 条)。回测**没有对应物**。

我自己量了一遍(没有用 agent 报的数字):

* 全库 8141 根 K 线,`end_ts` 的 UTC 小时分布是 **`{4: 8141}`** —— 每一根都收在 04:00Z。
* 回测钟是 `offset_hour=16`,即 16:00Z;`_candle_quote` 取 `end_ts <= asof` 的最后一根。
* 75 天 × 全体活跃 ticker = 7057 个 ticker-day 上的报价年龄:
  **min 12.0h / p50 12.0h / p90 12.0h / max 132.0h**,其中 **93.9% 恰好等于 12.0h**。
* **能通过实盘 6h 门的比例:0.0%。**

所以这不是"有些交易偏乐观",而是**回测的整个交易总体,实盘一笔都不会下**。

方向性我**不下"偏乐观"的结论**,因为我去量了而不是靠讲道理。实盘 quotes 表覆盖
2026-07-28~08-06,与回测窗口重叠,可以把"04:00Z K 线收盘价"和"同 ticker 12 小时后真实报价"
配对(172 对,配对时间中位差 2.2h):

* `|12h 内 yes_ask 变动|`:mean **0.0214**、median 0.0000、p90 0.0600、max 0.3200;
  30.2% 的腿动超过 1 分,11.0% 动超过 5 分。
* 有符号均值 **−0.0015** —— 没有可测的**水平**偏差。
* 均值回复检验(用前一日 K 线变动预测后 12h 变动):β = **−0.040**,corr = **−0.034**,
  n=172 —— 与 0 无法区分,**测不出"因为陈旧报价看着便宜才被选中"的赢家诅咒**。

诚实的说法因此是:12 小时陈旧度往入场价里注入了**约 2 分的噪声**(而入场阈值本身
`min_net_edge` 才 0.04),没有可测的方向性偏差;真正的问题是**总体不匹配**。这比 agent 原本
给的"OPTIMISTIC"标签更弱、也更站得住 —— 我把它写成前者。

同样是结构性的:日 K 线在 16:00Z 的钟下**不可能**产生比 12h 更新鲜的报价,把实盘的 6h 门原样
移植进回测,结果是回测下不出任何一笔(0 笔),那是一个空的评估器,不是一个更保守的评估器。
所以同样是**披露**。

### 这两条对 #129 结论的影响

不改变,方向上还是加强。A2 让回测多做了实盘做不成的交易;A3 说明回测测的压根不是实盘那个
决策时点。两者都属于"**已发布的回测数字所对应的策略,实盘跑不出来**"这一类,而已发布的数字
本身还是负的。

### 记录但未验证 / 未决(不当结论用)

* `frontend_export.py:64` 与 `:171` 的 kind 集合不一致(疑似影响 KXNATGASW 2026-08-07)——
  已验源码存在分歧,未量化影响。
* `report.py` / pricetrack 的 marks 表"持仓"口径 vs `refresh` 的执行顺序。
* 入场熔断器(circuit breaker)在回测里没有对应物。
* `streams.argmax` 34 笔里有 21 笔实盘挂不出去(agent 说法,未复核)。
* 平仓侧缺深度测试。
* 已证明**无影响**的两条:`close_time` 用 MIN vs MAX;bankroll 100 vs 492.65。
* 仍未解释:2026-07-28($48)与 07-29($32)两天突破 $30 日上限 —— 已确认**不是** F7
  (两天都是纯 `kind='open'`,混合口径总额 == 单一口径总额),是另一个问题,仍开着。

### 验证

```
540 passed in 55.58s        # F1–F8 收尾时 533,F9 的 7 个测试是净增
```

---

## §25.24 #152 —— 07-28/29 的超限:不是 F7,是"那时候还没有这条规则"(附:真正超限的是另一道门)

F7 那一节留了个尾巴:实盘头两天 2026-07-28($48)、07-29($32)都越过了 `per_release_day_usd`
= 30.0。当时我已经排除了 F7(两天都是纯 `kind='open'`,混合口径总额 == 单一口径总额),但没有
解释是什么放它们过去的。

**答案:那两天这条规则还不存在。** `per_release_day_usd` 是在 `83917ac`
(**2026-07-31 08:38 EDT**)才加进 `risk.LIMITS` 的;在那之前 LIMITS 只有
per_event / per_family / per_cluster / gross 四项。07-28、07-29、07-30 的仓位早于这条规则,
所以谈不上"突破"。**不是 bug。**

### 但顺手查出一件真的:KXPCECORE 2026-11 在 $5 的单事件上限上堆到了 $13

`per_event_usd = 5.0` 在那三天**是存在的**,而这一格堆了 **13 笔**同 series、同 period、同结构、
每笔 $1.00 的 `open`,时间从 2026-07-28T01:44Z 一路到 2026-07-30T09:13Z,一个 tick 一笔:

```
39 / 255 / 312 / 486 / 546 / 606        07-28   $6
666 / 729 / 790 / 851 / 912 / 973 / 1034 07-29   $7   ← 累计 $13,上限 $5
1095                                     07-30
```

同期还有 KXCPICORE 2026-09($7)、2026-10($7)、KXPCECORE 2026-09($7)。

**当时跑的是什么代码,查不到了。** `ops/risk.py` 的 git 历史起点是
**2026-07-31 07:46 EDT**(`83d5e5d` step 0: track prediction_market_macro in git),
也就是说这三天的代码**从来没进过版本控制**。我不打算靠猜去补一个故事。

有后果的问题只有一个:**今天的代码还会不会这样?** 这个可以直接答 ——
`tests/test_per_event_cap_replay.py` 把这 13 笔按实盘时间戳逐笔重放进现在的 `risk.check`,
**第 6 笔就被拒了**(`risk_per_event 6.00>5.0`)。测试里另外钉了两点:拒它的必须是单事件上限
而**不是**某个按天的上限(这堆跨了三个自然日,一个只在日内成立的"单事件上限"不叫单事件上限),
以及全部平仓后额度必须放开(否则前两个断言可能只是因为 `check` 总是拒绝而空过)。

这批行同时属于 #121 的**弃用 pre-cutover 账**,已经被排除在所有展示口径之外,所以没有任何
对外数字依赖它们。这一节的价值只在那个 pin:一个 $1 平坦下注、每 tick 触发一次的流,不能再
从单事件上限旁边走过去。

### 验证

```
543 passed        # §25.23 的 540 + 本节 3 个重放测试
```

---

## §25.25 #153 —— 同一个页面上两块面板对"这个周期现在是什么状态"给出两个答案

§25.23 末尾"记录但未验证"里挂着一条:`frontend_export.py:64` 与 `:171` 的 kind 集合不一致。
这次核完了,是真的,而且拆出来是两个不同的缺陷。

### 现状:两份手打的 kind 列表

| 面板 | 输出 | kind 集合 |
|---|---|---|
| board(`macro_board.json`) | 该周期"最后发生了什么" | `('open','pass','exit')` |
| stances(`macro_bets.json`) | "今天在下什么注" | `('open','argmax','arb','snipe','pass')` |

全账 66 个 (series, period) 里,**1 个真的不一致**:
`KXNATGASW 2026-08-07` —— board 显示 `exit`(id 3704),stances 显示 `pass`(id 3698)。

### F10a:board 漏掉了三种开仓 kind

两块面板**允许**差一个 token:board 报"最后发生了什么",平仓属于它;stances 报"当前可执行
立场",平仓不能进去 —— 前端 `MacroArtifact.tsx` 里是
`const isBet = d && d.kind !== 'pass'`,**任何非 pass 的 kind 都会被画成绿色的活跃下注**,
把 exit 塞进 stances 等于把已平仓位显示成持仓。

不允许的是 board 那份列表漏了 `OPEN_KINDS` 里的三个:argmax / arb / snipe 开的仓**当不了**
board 的"最新决策",于是 board 会退回去显示一条更早的 `pass` —— 在我们其实已经开了仓的周期上
对外说"我们放弃了"。

今天是**潜伏**的:argmax 只有 4 行,没有一行是所属周期里最新的,所以把集合补全**改变 0 / 66
行**。正因为现在改不花任何代价,才现在改。两份集合现在都从 `ledger.OPEN_KINDS` 推导而不是重打
一遍,以后加第五种开仓 kind 会自动进两块面板;它们之间的差异被压成一个显式 token:`exit`。

### F10b:光把 exit 排除掉不够,底下露出来的那条立场是假的

把 `exit` 从 stances 里排除,面板就会显示**平仓下面那一条**立场 —— 而仓位没了之后,那条立场
基本都是错的。实盘现在这一行是:

```
KXNATGASW 2026-08-07   pass: already_open_no_averaging_down
```

而这个仓位 18:31Z 已经被平掉了(id 3704)。**对外页面上写着"我们已经持仓所以不加仓",而我们
并没有持仓。**

规则改成:**该周期最新的一行(任何 kind)如果是平仓,就没有当前立场**,面板照实报
`decision: null`(前端本来就会渲染成 `—`)。没有任何信息因此消失 —— board 面板照旧带着那笔
平仓和它的已实现盈亏。而且这个状态是**自愈**的:下一次 `decide_all` tick 会在该周期写一条新的
pass 或 open,行就重新填上真话。

全账处于这个状态的周期有 4 个(KXWTIW 07-31、KXNATGASW 07-31、KXAAAGASW 08-03、
KXNATGASW 08-07),其中只有最后一个落在 stances 面板的 `-0.5..7.5` 天窗口内,所以
**今天改变 9 行里的 1 行**。改前/改后实测(导出到 /tmp,没碰生产 `public/data/`):

```
改前  KXNATGASW 2026-08-07  0.8d  pass: already_open_no_averaging_down
改后  KXNATGASW 2026-08-07  0.8d  — (no standing stance)
```

### 顺带纠正一条我自己差点报错的"bug"

查这条的时候我发现实盘 **119 条平仓行的 `closes_decision_id` 全是 NULL**,包括今天 18:31Z 刚
写的 id 3704 —— 看上去像 #149 的写入端没生效。核下来**不是**:

* 三个写入方(`exits._write_exit` / `pnl.settle_note` / `retire_stale_book`)源码都写了这一列,
  我在 sandbox 里跑 `_write_exit` 确认它确实落库;
* id 3704 写于 **18:31 UTC**,而 `ops/exits.py` 的 mtime 是 **17:50 EDT = 21:50 UTC** ——
  修复比那一行**晚 3 小时 19 分**。之前我把 `stat` 的输出当成 UTC 读了,差点据此报一个不存在的
  线上回归。

但有一个事实值得写进 `ledger.open_decisions` 的 docstring(已加):原文"早于该列的行是 NULL"
读起来像"只有一部分",实际是 **119/119 全是**,也就是说 `_replay` 里 `cid is not None` 那条
分支**在生产数据上从未被走到过**,当前 100% 的配对由 FIFO 兜底分支完成。这没问题(它就是被替换
的那个读法,而且两种读法在全账上一致),但意味着 **#149 的关联在实盘上尚未被验证过** ——
下一笔真实平仓是该检查的那一笔,如果它仍然是 NULL,那才是修复没进线上。

### 验证

```
559 passed        # §25.24 的 543 + F10a/F10b 的 16 个
```

---

## §25.26 #148 —— argmax 开的仓,开出来的那一刻就已经跌破平仓线

这条一直按预注册纪律锁着(等 PR-2 到 n=20)。用户 2026-08-06 明确解锁:"这一轮 PR-2 作废,
重新注册",理由是样本本来就薄 —— argmax 实盘一共只有 4 笔、$3.19。

### 两条规则各自都对,合起来是个死循环

| | 判据 | 出处 |
|---|---|---|
| 进场(argmax) | `fair <= cost` 即可开,**对有多负没有下限** | `streams/argmax` |
| 出场(rule 1) | `hold_edge = Σ_legs[fair(side) − mid(side)] < −0.06` 就平 | `ops/exits.py` |

argmax 的立场是"我没有 edge,这里听市场的",所以它**故意**在负 edge 上开仓。而 rule 1 是
edge 反转平仓,它不知道这仓位本来就是负 edge 开的。因为每条腿的**可成交价 ≥ 它的中价**,恒有
`mid_cost <= cost`,所以

```
hold_edge = st.fair − mid_cost  >=  st.fair − cost = net_edge(未扣费)
```

一笔 `net_edge = −0.15` 的 argmax 开仓,在**订单写下去之前**就已经在平仓阈值的另一侧了;而
`ops/exits` 在同一个 tick 里跑在 `decide_all` 后面。

### 实盘:4 笔里 3 笔在同一个 tick 内往返

```
id 3100  09:12:03.036  fair 0.6357  ask 0.77  mid_cost 0.7600 → hold_edge −0.1243
id 3107  09:12:03.255  exit worst=−0.1243  realized −0.07        ← 219 ms
id 3161  09:16:06.600  fair 0.6357  ask 0.76  mid_cost 0.7500 → −0.1143
id 3168  09:16:06.774  exit worst=−0.1143  realized −0.07        ← 174 ms
id 3222  12:31:32.869  fair 0.6357  ask 0.76  mid_cost 0.7500 → −0.1143
id 3230  12:31:33.333  exit worst=−0.1143  realized −0.07        ← 464 ms
id 3284  09:13:41.888  fair 0.8999  ask 0.90  mid_cost 0.8800 → +0.0199   仍持仓
```

$2.29 本金,亏 $0.21,**−9.2%,全部是往返的过路费** —— 没有任何一分钱是行情走出来的,持仓时间
加起来 857 毫秒。第 4 笔之所以活着,正是因为它的 `hold_edge` 在阈值上方,不是因为运气。

**这三行 `hold_edge` 是先用新写的 `struct_mid_cost` 回放出来、和账上已记录的 `worst=` 对到
1e-4 之后,才动的代码。**先证明谓词能重现历史,再让它去否决未来。

### 修在进场侧,不修在出场侧

另一个自洽的修法是:对"故意负 edge"的 argmax 流**不套用** edge 反转平仓。**没有采用**,理由
是方向 —— 那是**放松一道出场**,在一个已经证明会亏钱的流上放松风控,收益是假的(仓位活下来
只是因为没人再看它)。进场侧否决是**收紧**,最坏结果是少开一笔本来就要立刻平掉的仓。

阈值**不新增常量**,直接从拥有它的模块读 `exits.EXIT_EDGE`。测试里用 ±1e-6 探过:谓词绑的就是
那个数,而且**恰好等于阈值时开仓**,镜像 `if hold_edge >= EXIT_EDGE: continue`。

### 三条"不许多做"的边界

* **测不出来 ≠ 该拦。** `struct_mid_cost` 在缺报价 / 非 `two_sided` / 定不了价的腿上返回
  `None`,与 `hold_state` 返回 None 的条件逐条相同;调用方必须把 None 读成"rule 1 打不着",
  绝不能读成"rule 1 会打"。
* **它拦不到 edge 流。** `mid_cost <= cost`,而 edge 流要求 `net_edge > 0` 即 `fair > cost`,
  于是 `fair − mid_cost >= fair − cost > 0 > EXIT_EDGE`,**恒不成立**。这个矛盾被写成断言
  (`test_the_guard_cannot_bind_on_the_edge_stream`),所以这道门在结构上只能是 argmax-only。
* **放在 `risk.check` 旁边、影子写入之前。** 和 `risk.check` 同理:它是一条**准入**标准,只对
  实际下单的那条臂套用,会让 PR-2 把根本持不住的交易记到 defer 臂头上,两条臂就不可比了。

### 回测为什么一次也没看见

`_mtm_path` 从 `entry_day + 1` 起算 —— **同 tick 往返在网格外,是构造性的**,不是漏跑。这三笔
全发生在 219 毫秒内。所以这一条是 §25.22 那一类(回测/实盘跑的不是同一条规则)里**回测偏乐观**
的又一例:回测把这三笔当成持有到期的仓位在评分。修复同时落到 `research/walkforward.py`
(`argmax_churn_blocked` 计数),并绑在 `model_exits` 上 —— 它是从出场策略推导出来的,出场策略
不开,这道门也不该开。

### 验证

```
575 passed        # 559 + 16
```

---

## §25.27 #155 —— §25.4 那道门是活的,只是还没有人喂过它

`series_enable.blocked` 从落地起就是一道**真否决**,接在 `ops/decide_all` 的事件循环里。但
`experiments` 表里 `name='series_enable'` 的行数是 **0** —— 产出这些行的 `weekly_eval_gates`
还没跑过。于是这道门**看上去无害**,实际是一颗定时装置:`weekly_eval_gates` 第一次跑完的那一
刻,它会**无声地**从交易池里摘掉两个系列。

按今天的账重算(`GateHistory` + `se.evaluate` 全登记表跑一遍),会被摘掉的是:

| series | trailing roi | n |
|---|---|---|
| KXWTIW | −0.27462 | 6 |
| KXJOBLESSCLAIMS | −0.28950 | 10 |

**两个都在 PR-2 和 PR-7/S2 正在采样的总体里。**门一响,两个预注册测试的总体在中途被改写,而且
没有任何一行记录说明它被改写过。14 个系列里 2 个会被关。

### 影子模式:算、记、不动手

用户拍板走影子。`SHADOW = True` 只在**一个地方**被读到:

```python
def veto(state) -> str | None:
    """被执行的那个答案:live 下是 reason(state),SHADOW 下恒为 None。"""
    return None if SHADOW else reason(state)
```

`ops/decide_all`(实盘)和 `research/pit_gates.GateState.disabled`(回测)**都**走 `veto`。
这是刻意的:#109 / #128 / #151 全是"两条道各自实现同一个判断然后分叉",而**这道门已经分叉过
一次** —— 回测的 `GateState.disabled` 一直在套用 §25.4,实盘那侧却因为没有 artefact 而空转。
所以 SHADOW 必须同时管住两条道,否则影子模式本身就制造一次 §25.22。

观测口是 `would_disable` / `would_block`,与 `veto` 同源不同用:**报,永远不据此分支交易**。
`test_both_lanes_read_the_one_switch` 把这条钉死。

### 记什么,以及为什么连"没意见"也要记

新表 `shadow_series_enable`,主键 `(day, series)` —— **每系列每天一条判决**,不是 tick 日志,
否则事后读数会把 tick 多的日子加权更重。

```sql
evaluated   INTEGER NOT NULL   -- 0 = 没有存储判决(门是瞎的),这是第三种状态
would_block INTEGER NOT NULL   -- 1 = 它会关掉这个系列
roi, n, flips                  -- 它据以决定的那段 trailing 窗口
reason      TEXT               -- 与账本同形的那条 veto 字符串
```

**enabled 的系列和 unevaluated 的系列也照记。**否则"门今天什么都不想拦"和"记录器死了"在数据
上不可区分 —— 而那正是让 §25.4 空转了一整天没人发现的那种失效。`{}`(周报从没产出过判决)
是第三种状态,不记就会被读成"评过了,没事"。

回测侧对称地加了 `series_disabled_shadow` 计数,放在 `blocked_by is None` 之后,沿用 veto 本来
会有的**同一套优先级** —— 所以 `series_disabled` 和 `series_disabled_shadow` 任何时候至多一个
非零。

### 这是一个开关,不是一个设计

阈值一个都没碰(§25.1:`WINDOW=12` 借 `dsr.MIN_OBS`,`MIN_N=6` 借 `skill.MIN_PAIRED`,
`OFF_ROI=0.0`,`ON_ROI=0.026`,全是借来的,从没被拟合过)。**PR-2 走到 n=20 之后,把
`SHADOW` 翻成 `False` 即可上线,不需要改第二处** —— `test_flipping_the_switch_makes_it_a_veto_
with_no_other_change` 就是为了保证"第二处"不存在;真有第二处,那里就是两条道下次分叉的地方。

### 验证

```
582 passed        # 575 + 7
```
表已经在生产库里(`init_db` 每个 live 入口每周期都调,DDL 自动到位),当前 0 行 —— 下一个
`decide_all` tick 开始写。

---

## §25.28 #120 —— K 线是会过期的,而抓取它的那条路只在有人手动跑回测时才走

### 先把期限测出来,再谈修

之前一直写作"~75 天",是估的。这次按合约年龄逐个探 `/candlesticks`(2026-08-06):

```
age 62d KXNATGASW       -> 7 bars      age 76d KXNATGASW       -> HTTP 404
age 63d KXJOBLESSCLAIMS -> 7 bars      age 77d KXJOBLESSCLAIMS -> HTTP 404
age 69d KXNATGASW       -> 7 bars      age 80d KXAAAGASW       -> HTTP 404
age 70d KXPCECORE       -> 8 bars      age 83d KXNATGASW       -> HTTP 404
age 73d KXAAAGASW       -> 0 bars      age 84d KXJOBLESSCLAIMS -> HTTP 404
```

**四个不同系列在同一处翻转**,所以这是平台级保留策略,不是某个系列的毛病:**最后一个还应答的
年龄是 73–75 天**。(73d 那行返回 0 bars 是"这个档位没人交易过",不是过期 —— 过期的信号是
404。)常量取 `RETENTION_DAYS = 74`,刻意取实测区间的**保守端**:高估保留期 = 跳过一个其实
还救得回来的合约,而这个方向的错误**不可逆**。

本地 `candles` 表是永久的,所以窗口内抓到的永远留着;窗口外的,任何数据源都补不回来。

### 真正的缺陷不是"没有抓取",是"抓取不在任何时间表上"

抓取逻辑一直有 —— `research.backtest.backfill_candles`。但**没有任何调度器调它**,它只在有人
手动跑回测时作为副作用被触发。这条副作用路径**已经在漏**:写这个模块时,窗口内有 **14 个已结算
合约零 K 线行,其中 2 个离过期不到一周**。一份"只有人想起来跑研究脚本时才增长"的历史不叫档案。

### 它解开的是 #138

PnL 回测的样本量被 K 线覆盖率卡死,而 `dsr.MIN_OBS = 12` 是参数选择器返回非默认值的下限。周频
系列在 API 窗口里最多只能装下 `75/7 = 10.7` 个事件 —— **光靠 API,它永远到不了 12**,选择器
在构造上不可达。对着一个持续累积的本地库,这个计数是单调的,当前 10–11 个覆盖周期的几个周频
系列两周内越线。实测覆盖:

```
KXWTIW 11/153   KXNATGASW 11/18   KXAAAGASW 11/130   KXJOBLESSCLAIMS 10/49
最老的一根 bar:2026-05-16 —— 正好是 82 天前,就是那堵墙
```

### 三个设计点

**1. 队列按剩余寿命升序,这是承重的。** `run` 在 `MAX_FETCH` 处截断。如果队列是最新优先,那个
上限每次生效**都会永久毁掉数据** —— 它推迟掉的正是最等不起的那些。最老优先的队列被截断时,推迟
的是余命最长的,明天再抓。

**2. 告警必须留出余量。** `WARN_AGE_DAYS = 55`,不是 74。在数据已经死掉的那天才响的告警是尸检
报告,不是警告。55 天留 **19 天**余量,而 `refresh` 每天 05:00 跑,足够有人注意到并处理。

**3. 必须有第三种状态,否则告警会永远响。** 这一条是上线时才发现的:14 个合约抓完 **0 bars**,
队列**还是 14**。它们不是 404(404 会写哨兵行),是 200 + 空列表 —— 从没成交过的深度虚值档位
(KXAAAGASW 4.515 / 4.505 / 4.165 …,年龄 17d 到 73d 全是空)。没有终态,它们会被**每天重抓
直到过期**,更要命的是**把 overdue 告警永久钉在响的位置**。一个一直在响的告警不是告警。

修法是复用 `kalshi_md.candles` 在 404 上已经在写的那个哨兵行(`end_ts = 0` + 价格全 NULL),
而不是发明一个新状态:它们语义相同("这个 ticker 没有 K 线数据"),生产里已经有 ~6.7k 行,
所有读取方**本来就已经暴露在它面前**,`_market_leg_prob` 遇到 NULL bid/ask 返回 None,正是
"没有市场"的正确答案。加 `EMPTY_CONFIRM_DAYS = 3` 的年龄闸:唯一能毁数据的走法是在最后一根
bar 还没落库时就把合约判死,3 天给日常道 3 次尝试机会,而期限在 74 天外,等得起。

### 不需要新 cron

挂进 `ops/refresh.py`,位置在 `settle:*` **之后** —— 是那一步把刚收盘的合约放进 `settlements`
的,反过来排就永远晚一天归档。`com.someopark.macrorefresh.plist` 每天 05:00 已经在跑 refresh,
所以归档自动获得调度,**没有新增第四个 launchd 任务**。

### 上线实测

```
第一次   pending 14  overdue 6  fetched 14  bars 0  empty 14
第二次   pending  0  overdue 0  fetched  0  bars 0  empty  0     告警静默
```

积压的 14 个全部是**永远不会有 K 线**的未成交档位,所以**这次实际上一根 bar 都没丢**。但那 2 个
"离过期不到一周"是真的,下一批就不一定这么走运了 —— 修的是那个。

顺带核过一条可能的隐患:窗口内被 404 哨兵**误判死**的合约有 **0 个**,6696 个哨兵全是真正过期
或从未成交的老合约,没有掩盖任何还救得回来的东西。

### 验证

```
600 passed        # 582 + 18
```

---

## §30 demo 实盘跟单 —— `trading_kalshi` 镜像执行($492 demo 账户,用户 2026-08-18 指令)

### §30.0 一句话架构

**paper 台账就是 inventory,demo 账户是它的 ×100 放大镜像。**内部系统每写一笔 paper
成交(open/exit/arb/snipe),跟单器 `ops/trading_kalshi.py` 读到它,在 Kalshi demo
账户按 **100 倍张数**下同方向同价格档的单:paper 下 $0.42,demo 下 $42。跟单器
**永远不做自己的交易决策**——它没有模型、没有闸门判断、没有择时,只有"台账有,我就有;
台账平,我就平"。放大收益,同时把执行管线(鉴权/下单生命周期/部分成交/对账)在真钱
之前跑熟。

用户指令原文要点(2026-08-18,全文进 git):
* "每次我们内部系统paper记录下单 就让/trading_kalshi 读取那个然后在kalshi同步下单"
* "paper下0.42 我们在kalshi demo account通过api 放大100倍下42美金"
* "'模型 Brier 打赢市场'这个改成paused 优先按照我们macro的系统里面实盘下单了…
  关闭了一个bet 你也要在kalshi里用api做一样的交易"
* "我建议用market order而不是limit order。这样一定能成交"
* "不要简化不要fallback 遇到问题要解决"
* 不动:KALSHI_ENV=demo、熔断与风控帽原样、三道闸门结构原样、prod key 只在明示时启用。

### §30.1 晋级判据(PR-9,已注册,先于任何实现)

实盘 paper 台账自 2026-08-11 重置起:**≥10 笔结算 且 事件聚类 bootstrap 95% CI 的
ROI > 0**。达成 → 实施 §30.3;不达成 → 一行代码不写。判据全文与计数在
`docs/PREREGISTER.md` PR-9,判据不改,K=1。

### §30.2 三个已知缺口,各自的处理(用户点名要求解决,不许敷衍)

**缺口 1 —— `exec/kalshi_exec.py` 无人 import。**
处理:新模块 `ops/trading_kalshi.py` 是唯一调用方。挂载点 = `jobs/tick.py` 的
arm/decide/reassess lane 与 `ops/refresh.py` ③ 段,**紧跟在 decide_all / exits /
arb / snipe 写完 paper fills 之后**调 `trading_kalshi.sync(conn, s)`。sync 的工作
方式是**扫账补差**而不是事件回调:读"尚未镜像的 paper fill"(按 fills.id 升序),
逐笔镜像——所以错过一个 tick 不丢单,进程崩溃不丢单,重复调用不重复下单(见 §30.4
幂等)。

**缺口 2 —— 只有下单没有回读,demo 账本会漂移。**
处理分三层,全部进 D1 交付物:
1. **成交回读**:每笔订单发出后轮询到终态(filled / partial / unfilled),真实成交
   价、张数、手续费写 `demo_fills` 表;未终态的订单每个 tick 续轮询,超时(15 分钟)
   撤单并记 `status='unfilled'` + 告警。
2. **每日持仓对账**:`GET /portfolio/positions` 与 Σ`demo_fills` 的期望持仓逐 ticker
   比对;**任何不一致 → level=error 告警 + 跟单器自动暂停**(写 `mirror_halt` 行,
   人工 ack 才恢复)。漂移不许带病运行。
3. **每日余额对账**:demo 余额 vs (492 + Σrealized − Σfees) 的期望值,超容差同样
   halt + 告警。

**缺口 3 —— 闸门②的 Brier 判据和赚钱不是一回事,$492 永远花不出去。**
处理:**Brier 判据对跟单路径 PAUSED**(用户指令原文)。三道闸门**结构**保留,内容
重定义:
| 门 | 原判据 | 跟单路径新判据 |
|---|---|---|
| ① 全局开关 | settings.trading_enabled ∧ env KALSHI_TRADING_ENABLED=1 | **原样** |
| ② 逐系列 | series_gate real=true(eval.py 按 Brier 赢市场写) | **"paper 台账下了这笔"本身就是判据**(跟单器由台账驱动,天然满足)+ 一个逐系列运维 kill-switch(`mirror_series_off` 行,默认全开,只用于紧急摘除单系列,不是策略判断) |
| ③ 熔断 | 7 天内无 circuit_breaker 告警 | **原样**,另加 §30.2-2 的 mirror_halt 未 ack 也算红 |
eval.py 照旧每周计算并写 series_gate 行(**计算不停,只是跟单路径不读它**)——那套
判据留给未来可能的"模型直接实盘"路径,和本节的镜像路径是两回事,PREREGISTER PR-8/
README 的口径同步注明 paused。

### §30.3 执行机制(D1 实施规格)

**订单类型:taker,保证成交,但必须带价格保护。**用户要 market order 的本质诉求是
"一定成交、和 paper 的 ask 成交假设可比"。**裸 market order 在 demo 薄订单簿上
×100 张会扫穿价格档**(demo 交易所的簿是独立的、远薄于生产簿)——所以规格是
**market-order 语义 + 成本上限**:优先用 API 的 `type=market` + `buy_max_cost`
(实施时在 demo 环境实测该参数;若 demo API 不支持,则等价实现为"ask+3¢ 上限的
marketable limit,立即成交部分保留、余量撤单")。两种实现的行为承诺相同:
**要么在受控价格内立即成交,要么明确失败并告警**——绝不悄悄挂着,绝不敞开滑点。
卖出(exit 镜像)对称:bid−3¢ 下限。

**张数与规模**:`MIRROR_MULT = 100`,`count_demo = 100 × count_paper`。paper 的风控
帽(单事件 $5/家族 $20/日 $30/总 $100)原样不动——跟单器不自设仓位,demo 敞口
天然 = 100 × paper 敞口轮廓。**买力硬约束要显式处理**:$492 撑不起 100× 的理论
上限($100×100),下单前查 demo 买力,不足时按买力能容纳的最大张数下(例:目标
4,200 张只买得起 1,100 张就下 1,100 张),`count_target` vs `count_filled` 差额记录
在案 + 每日报告"目标 vs 实际"敞口比;**这不是 fallback,是外部硬约束的显式测量**
——$492 是用户给定的资金面,缩张数是唯一诚实的执行方式,且每一次缩都可见。

**镜像顺序与生命周期**:
* 按 paper fills.id 严格升序处理——开仓镜像先于平仓镜像,构造上不可能倒序;
* exit 镜像的张数 = min(100×paper平仓张数, demo 实际持有张数)——如果开仓当时因
  买力/簿深只成交了部分,平仓自动对齐实际持仓,永不裸卖空;
* 结算不镜像(交易所自己结算),但结算后触发一次持仓对账确认归零。

**幂等与崩溃恢复(§30.4)**:
* `demo_orders` 表以 **paper fill_id 为主键**(一笔 paper fill 至多一张 demo 订单);
* `client_order_id = 'spm-m{fill_id}'` **确定性生成**——进程在"订单已发、本地行未写"
  之间崩溃,重启后 sync 先按 client_order_id 反查交易所在途订单认领,绝不重复下单;
* 所有对交易所的写操作(下单/撤单)前先落"intent 行",终态后更新——审计链完整。

**新表**(`ingest/store.py` DDL,`CREATE TABLE IF NOT EXISTS` 惯例):
```sql
CREATE TABLE IF NOT EXISTS demo_orders(
  fill_id INTEGER PRIMARY KEY,      -- 镜像的 paper fills.id,1:1
  decision_id INTEGER NOT NULL,
  client_order_id TEXT NOT NULL UNIQUE,   -- 'spm-m{fill_id}',确定性
  ticker TEXT NOT NULL, side TEXT NOT NULL, action TEXT NOT NULL,
  count_target INTEGER NOT NULL,    -- 100 × paper
  count_filled INTEGER NOT NULL DEFAULT 0,
  avg_price REAL, fee_usd REAL,
  status TEXT NOT NULL,             -- intent|sent|filled|partial|unfilled|skipped_halt|skipped_power
  order_id TEXT, ts_sent TEXT, ts_terminal TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS demo_fills(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fill_id INTEGER NOT NULL,         -- 回链 demo_orders.fill_id
  ticker TEXT NOT NULL, side TEXT NOT NULL, action TEXT NOT NULL,
  price REAL NOT NULL, count INTEGER NOT NULL, fee_usd REAL NOT NULL,
  exchange_fill_id TEXT UNIQUE, ts TEXT NOT NULL);
```
**设计决定与偏离说明**:demo 成交**不写进 `fills` 表的 `mode='demo'` 行**而是独立
`demo_fills` 表。偏离原因(向用户说明):`fills` 现有几十处读取方全部默认 paper
口径,任何一处漏加 mode 过滤就会把 ×100 的 demo 数字混进展示台账——这正是 #149
"三个读账处算错"那一类事故的放大版。独立表 + `decision_id/fill_id` 回链保留全部
可联查性,把出错面从"每个未来读者"缩到零。paper 台账的任何数字不因 demo 上线而
改变一个 bit。

**执行质量测量(phase 1 的真正交付物)**:paper 影子照跑(本来就是主账本),逐笔
导出 paper 假设成交价 vs demo 实际成交价的滑点、成交率、买力缩减率 →
`macro_demo_exec.json` 前端瓦片。**demo 的 PnL 不当 alpha 裁判**——demo 簿与生产簿
无关,paper 台账(按真实生产盘口定价)仍是唯一的策略裁判;demo 证明的是管线。
maker(post_only 省费)留到 phase 2,以 phase 1 的滑点/成交率数据为对照组。

### §30.4 分期与测试

**D0(现在)**:本节 + PR-9 注册。不写实现代码。
**D1(PR-9 达成日启动)**:`ops/trading_kalshi.py` + 两张表 + kalshi_exec 补
market-with-cap 支持 + 对账三层 + tick/refresh 挂载 + `macro_demo_exec.json` 导出。
**D2(D1 上线两周后)**:执行质量报告(成交率/滑点/缩减率),据此决定 maker 实验
与是否调 MULT。**prod key 的任何讨论只在用户明示后开始。**

D1 测试清单(全部先于上线,不许缩水):
1. 幂等:同一批 paper fills 上 sync 跑三遍,demo_orders 行数与交易所订单数不变;
2. 崩溃恢复:模拟"已发单未落行",重启后按 client_order_id 认领,不重复下单;
3. 顺序:构造 open+exit 同批,断言镜像顺序;exit 张数被实际持仓截断;
4. 买力护栏:mock 买力不足,断言缩张、status='partial'、差额入账、告警发出;
5. 对账 halt:注入持仓不一致,断言 mirror_halt 写入、后续 sync 全部 skipped_halt、
   ack 后恢复;
6. 乘数与费:100× 张数的费用按 Kalshi 公式逐档断言;
7. 三闸门:①任一开关关闭全拒;② kill-switch 摘除单系列;③ circuit_breaker 或
   mirror_halt 未 ack 时全拒;
8. paper 台账零污染:D1 全套跑完后,`fills`/`decisions`/展示 JSON 与 D1 前逐 bit 一致。
