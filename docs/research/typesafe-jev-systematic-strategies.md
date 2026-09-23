# TypeSafe Jev 与六个量化策略：接入研究与验证计划

初稿与代码复核：2026-09-22。状态：**研究与计划，未实施接入**。

范围限定为 **MRPT、MTFS、SSRS、AISS、AEUS、BDC**。本文沿用 [预测市场研究](typesafe-jev-prediction-markets.md)的组织方式，但不复用其策略结论；Macro、足球和 Crypto 预测市场不在本文范围。BDC 指上市 BDC 股票策略及其贷款穿透数据，不是一个预测市场策略。

本文是用户本次明确要求的 Jev 专项研究，不是第二份 ontology 规范。[ONTOLOGY_CONTROL_PLANE_PLAN.md](../../.claude/plan/systemic-strategies-plan/ONTOLOGY_CONTROL_PLANE_PLAN.md)继续负责身份、数据权威、时间、权限和开发隔离；本文只引用这些约束并提出 provider 实验。其 9/21 选定的本地模型候选不因 Jev 研究而替换，也不把 Jev 当成已可下载、可 LoRA 的本地底座。

本次阅读官方资料、当前源码、配置、近期运行日志、信号/日报、历史计划与原始 SEC 缓存。核验时 HEAD 为 `be250acc281ec4ed60dda1bc3431f8ff487c9ea2`；工作区包含未提交改动，commit 不能单独重建所有观察。代码存在、调度配置、某次成功记录和当前服务健康是不同证据。本次没有运行策略、连接 QC 或模型服务、调用 Jev、上传项目数据、改交易代码或写业务数据库。

## 结论

有明确接入空间，但六个策略的首要价值是**运营分诊和证据质量**。没有证据说明它们需要每秒用模型重新判断价格、仓位或风控。

建议首批并行准备两个独立影子实验：

1. **`systematic_ops_triage_shadow`**：六策略共用任务规范、分别适配日志和记录；代码先识别运行阶段、状态与已有原因，再让 Jev 对剩余语义问题提供诊断分类、runbook 候选和证据不足判断。
2. **`bdc_disclosure_semantics_shadow`**：以已发现的 SEC 指标口径/表格错配为回归案例，比较改进后的确定性解析与 Jev 辅助审计。现有 manifest 不能直接作为正确标签。

第二批是六策略报告陈述审核与 AISS 新闻稿片段分类。MRPT/MTFS 的股票事件语义、SSRS 行业新闻、AEUS 电力产业事件可以保留为后续研究，但目前不能假定已经具备完整的原文采集、版本和首次可得时间。

这些是可执行的实验建议，不是 Jev 性能实测。尚未证明它比现有规则或模型更准确、更快、更省总成本；也未证明任何交易收益增量。对已经有精确 reason code、数值阈值或可靠规则的问题，继续用代码。

## 本次代码与记录复核发现

### 不能沿用的旧认识

- **不能只读默认 config 推断当前策略。** 9/21 留存记录中 SSRS 为 `low_beta_bab/v1`，AISS 为 `pure_capex/v1`，AEUS 为 `broad_5/v1`。参数选择记录、实际运行日志和日报必须一起读；当前配置宇宙也不等于当前持仓。
- **不能把日志写入日当成市场事件发生日。** AISS/AEUS weekly 日志中的历史 VIX、drawdown、stop 警告来自回测循环；MRPT/MTFS 的 9/22 文件对应 9/21 signal date，且其中部分 regime 信息标为 9/22。应分开市场日、生成时点、实际可得时间和执行时点。
- **不能把旧事故统一写成当前未修。** MRPT 的 Kalman finite guard、MTFS 的 split-cliff healing、AISS 的 updater 接线和 ASML guidance 换源、AEUS 的 EIA 截断修复都已有代码与记录。另一方面，SSRS P/E 的 constituents 路径修复不能代表 Polygon 路径也拥有相同检查。
- **不能把现有 QA 的绿色视为所有字段正确。** BDC 两个披露指标已见口径/行列错配，而现有 QA 仍为 ok；Controller 本地对账通过也不代表 QC 执行股份已收敛。
- **不能把历史计划当作根因标签。** MTFS NaN 的原始日志证实了 NaN，但旧计划所写 DNS 因果缺乏同 run 关联依据。详细证据见 MTFS 一节。

### 对 ontology 接入有影响的新增边界

Ontology 的 9/21 版本要求 D0–D7 全程独立研发、不接生产，正式集成另走 I0。Jev 不因此获得修改普通聊天、共享 artifact detector、策略、锁、参数、账本或目标仓位的权限。

另一个直接影响是 TypeSafe 9/19 更新的公开 MCA §2.3：限制使用服务或输出来蒸馏、训练模仿其输出的模型等。因此 **Jev 输出不能默认进入 ontology 的 D7 蒸馏、本地模型 SFT/DPO 或拟合替代分类器的数据集**。审计保存与训练复用要分开。已有代码规则、独立人工核验事实与其他获准训练数据保留独立来源；涉及 Jev 输出的训练/规则拟合或开源复用，先核验实际适用协议及明确授权，不用“人工看过”自动消除来源限制。[当前 MCA](https://typesafe.ai/legal/mca)

## Jev 的能力与边界

Jev 接收文本/JSON 状态与有限问题，返回 Choice、Score 或 Noul。官方将其训练方法称为 RLCD；本文没有独立验证模型架构、训练过程或参数规模。它适合语义分类、候选选择、相关性与证据支持判断，不生成开放式分析或代码。[模型介绍](https://docs.typesafe.ai/introduction) · [训练目标说明](https://docs.typesafe.ai/introduction/machine-learning-primer)

- **Choice** 返回有限选项及概率分布；任务应有 `other/unknown/insufficient_context` 等退路。一个事件可能有多种原因，不应硬塞进互斥的单标签根因。
- **Score** 是给定等级的加权评分，不是精确的收益、金额、风险或违约率估计。
- **Noul** 返回 yes 概率，不带独立 confidence。Choice/Score 的 confidence 是分布统计量，不是此次判断正确的概率，更不是交易胜率。

同状态的独立问题可以批量并行，省去重复 state；问题与选项仍增加输入成本。独立计算不代表错误独立、概率逻辑自洽，或一个答案会自动成为另一个问题的上下文。依赖关系由代码或后续调用明确表达，阈值逐任务、问法、版本校准。[API](https://docs.typesafe.ai/api) · [Confidence](https://docs.typesafe.ai/confidence) · [已知限制](https://docs.typesafe.ai/model-jaggedness/jev-1.13)

官方明确披露数字、日期、多跳、长上下文干扰和对抗文本方面的弱点。合法输出格式不保证事实正确。所有价格有限性、金额/百分比换算、报告期比较、PIT、身份、份额恒等式、锁和权限检查继续由代码承担；Jev 也不能成为唯一的 prompt-injection 防线。[已知限制](https://docs.typesafe.ai/model-jaggedness/jev-1.13)

官方现明确英语是主要训练语言、表现最好，CJK 表现不等价；不提供客户级 fine-tuning/LoRA。我们的中文日志、英文报表、混合缩写必须分层评测。当前公开交付方式按托管 API 评估，不假定有模型权重或自托管部署。[模型说明](https://docs.typesafe.ai/models)

## MRPT：价差策略的运行分诊与复盘证据

### 当前链路与真实记录

MRPT 的批量参数入口是 [PortfolioMRPTStrategyRuns.py:511](../../PortfolioMRPTStrategyRuns.py)，daily pipeline 实际运行 rolling WF，再执行 `DailySignal --strategy both`、diagnostic 与 PnL report。[pipeline_runner.sh:112](../../conductor/pipeline_runner.sh)

价差 z-score、动态 entry/exit、波动/时间/价格止损、ADF/Half-life/Hurst/KPSS 已在 [PortfolioMRPTRun.py:362](../../PortfolioMRPTRun.py)中计算；财报 blackout 是日期规则，Kalman 对齐和 finite guard 已存在于同文件 274–315 行。Jev 不重做这些判断，也不取代 OOS exclusion/Tier 阈值。[DailySignal.py:311](../../DailySignal.py)

[9/22 日报](../../trading_signals/daily_report_20260922_083937.txt)记录 `signal_date=2026-09-21`、生成时间 9/22 08:39:37；[MRPT 信号](../../trading_signals/mrpt_signals_20260922_083855.json)有三个原开仓候选被 circuit breaker veto。原因已由代码明确，不需要再付一次模型调用确认。09-22 的 [event_risk heartbeat](../../trading_signals/event_risk_heartbeat.log)也记录 asof 09-21；这是已发生的一次检查，不代表高频文本推理已存在。

### 最值得试验的窄任务

1. **剩余异常的 runbook 候选。** 输入 run/pair 身份、运行阶段、已算出的 coverage/finite 检查结果、短错误片段和已审定的处置说明候选，分类数据源故障、统计计算失败、已处理的数据缺口或证据不足。明确的 NaN/缺价先由代码拒绝；模型不能决定补零、跳过标的或修改 Kalman 参数。
2. **复盘因果是否有证据。** [5/14 亏损复盘](../../.claude/trade%20reviews/TRADE_LOSS_ANALYSIS_20260514.md)将部分大幅波动解释为可能财报/收购/重大事件，属于待证推断，不能直接当事件真值。Jev 可判“来源支持 / 推断超出证据 / 矛盾 / 信息不足”，为人工复核排序，不生成权威根因。
3. **股票公告的事件类别，留待数据先行。** 当前 [MRPTFetchEarnings.py:87](../../MRPTFetchEarnings.py)主要保存 filing、acceptance、release timing 等元数据，未见主链消费新闻正文。未来只有拿到有授权、实体绑定和首次可见时间的公告，才研究 pair 两腿的事件关联；不能拿现有日期缓存假装完整新闻历史。

### 重放与验收

本次盘点到 MRPT signals 128 份、121 个不同 signal date、1,067 个 `signals` 元素；这是留存规模，不是独立事故、真实成交或人工标签数。MRPT/MTFS 共用日报也不能再算一份独立样本。按 run/day/incident 去重，正常、已处理 warning、真实失败和信息不足都要覆盖。

旧 [Kalman 修复计划](../../.claude/plan/systemic-strategies-plan/KALMAN_HEDGE_NAN_ALIGNMENT_PLAN.md)可提供历史样例，但已修复的日志洪水不能计入 Jev 新收益。先比较现有 reason code/模板检索与 Jev 对未知诊断、因果陈述的增量；首轮不改 veto、signals、inventory、ledger 或参数。

## MTFS：动量配对的动作语义与错误归因

### 当前链路与真实记录

MTFS 与 MRPT 共用 daily 调度链，但交易逻辑不同。[PortfolioMTFSRun.py:352](../../PortfolioMTFSRun.py)计算两腿 momentum spread、hedge、动态阈值和 trend flags；394–497 行处理 momentum reversal、pair PnL、波动/时间止损、consistency decay、财报与冷却。VIX term-slope、容量和 circuit breaker 在 [DailySignal.py:1470](../../DailySignal.py)中继续做确定性检查。

[9/22 MTFS 信号](../../trading_signals/mtfs_signals_20260922_083937.json)对应 9/21，六个原开仓候选被 circuit breaker veto；同日合并日报还有 circuit-breaker CLOSE。**禁止新开仓、既有持仓退出、优化目标和实际执行，是不同事件。** 已有 [AuditPairs.py:25](../../AuditPairs.py)检查 entry/action/equity/PnL/cash/stop 类型，已知动作和前缀先用代码解释。

### 最值得试验的窄任务

1. **公司行动/价格口径相关消息的证据分诊。** [KLAC/REGN 历史计划](../../.claude/plan/strategies-plan/KLAC_REGN_FALSE_STOP_REMEDIATION.md)记录过假止损；当前 `PortfolioMTFSRun.py` 1053–1057、1243–1247 行已有 split-cliff healing，09-22 原始 pipeline 日志仍有 `[CA][heal] KLAC`。日志出现告警词不代表修复失败。Jev 可推荐需检查的证据和 runbook，不能认定某次止损无效或直接恢复仓位。
2. **报告与动作来源一致性。** 对一段待发布解释和对应 CLOSE/HOLD/new-open-veto 事实，判断解释是否混淆动作类型、模拟触发日与实际执行日。精确 pair、action、日期和数值先由代码核验，模型仅审核自然语言含义。
3. **动量事件解释仅作未来研究。** 没有当前 PIT 公告原文链的证据，不能让 Jev 从价格数组“判断反转”，也不能代替 momentum weights、stop 或持仓容量。

### 原始记录对旧计划的反证

[MTFS NaN 计划](../../.claude/plan/systemic-strategies-plan/MTFS_NAN_ALIGNMENT_PLAN.md)将 2026-04-06 CSX/AIG NaN 归因于紧前 DNS 失败。本次直读其引用的 [压缩原始日志](../../pipeline_state/logs/archive/pipeline_20260818_195900.log.gz)：解压行 434105–434110 确有 NaN；该文件第一个 `Could not resolve host` 位于解压行 3814137，不能支撑“紧前 DNS 导致该窗口”的叙述。NaN 事实成立，具体因果仍需同 run 证据，不应把远处日志拼成因果标签。

当前 `PortfolioMTFSRun.py` 149–173 行取末值的局部路径仍未见 finite guard；这是独立的确定性代码检查候选，不能靠 Jev 兜底。本研究不修该代码，也不据此外推当前所有结果已受污染。

本次 MTFS signals 为 127 份、120 个不同 signal date、967 个 `signals` 元素；两策略共有 JSON/TXT 日报各 127 份。跨 07-03 monitor PnL 去二次 scaling 等代码时代，须分层，旧表现不回改。可启动报告审核与历史分诊样本建设，尚不能证明完整股票文本 PIT 回测可行。

## SSRS：行业轮动的运行上下文与报告核查

### 当前链路与真实记录

SSRS 配置 11 个行业 ETF，regime、动量、相对估值和组合优化均有数值实现。[config.yaml:32](../../qlib-main/sector_rotation/config.yaml)是基础配置，不等于当前有效参数；[9/21 参数选择](../../qlib-main/sector_rotation/selected_param_set.json)为 `low_beta_bab/v1`，[固定日期日报](../../qlib-main/sector_rotation/trading_signals/sr_daily_report_20260921_20260921_173814.json)也记录同一选择，避免仅依赖会变化的 selected 文件。

[9/21 daily 日志](../../qlib-main/sector_rotation/logs/sr_daily_20260921.log)42–91 行记录 macro、Polygon P/E cache、composite、optimizer、risk controls、`no_rebalance`、库存与 ledger/报告更新。`SectorRotationDailySignal.py` 1190–1194 行明确无调仓时保留当前权重和股数。因此 optimized target 不等于今天实际换仓。[代码](../../qlib-main/sector_rotation/SectorRotationDailySignal.py)

[9/20 weekly review](../../qlib-main/sector_rotation/backtest_results/weekly_review_20260920_032729.json)当时参数是 `crisis_defense`，而 9/21 已不同，不能将旧周报覆盖新参数；其 `declining/improving/stable` 由 [weekly_review.py:106](../../qlib-main/sector_rotation/weekly_review.py)按 Sharpe 差值计算，无需 Jev 再判一次。9/17、9/18、9/21 有相应留存运行记录，不等同于核验全部调度成功。

### 最值得试验的窄任务

1. **未知日志的主题与 runbook 分类**：数据获取、缓存 provenance、运行依赖、仅报告层问题、信息不足。代码先提供 `run_kind/phase/as_of/exit_code/check_results`，不用 Jev 判断删锁或重跑。
2. **“已换仓”等报告陈述的支持检查**：代码先读 `rebalance/trades/inventory`，Jev 审核叙述是否越过这些事实；不能把优化目标当 fill。
3. **研究记录相关性筛选**：候选说明是关于当前数据源、参数研究还是历史账务口径。当前没有可确证的 SSRS 实时新闻/情绪因子主链；行业收益预测不是首批任务。

### 必须保留的代码边界

9/13–9/14 的 P/E 修复在 [signals/value.py:366](../../qlib-main/sector_rotation/signals/value.py)的 constituents 路径有 freshness 检查；同文件 605–610 行的 Polygon 路径仍是有缓存即返回，9/21 daily 实录正使用 Polygon。不能声称两条路径已同样修复；也未比较缓存实值和原源，不能直接认定当前 P/E 已错误。该问题应先用确定性 provenance/freshness 检查核验。

[pipeline_lock.sh](../../qlib-main/sector_rotation/pipeline_lock.sh)已有互斥与死进程/超时处理，[daily_backtest.sh:77](../../qlib-main/sector_rotation/daily_backtest.sh)已有完成标记幂等门。Jev 分类只能留在独立 sidecar，不能成为删锁、重跑或参数恢复的执行依据。

## AISS：半导体披露片段与数据源异常

### 当前链路与真实记录

AISS 配置八个半导体子板块，V1 月度、V2 半月调仓，但当前 [9/21 选择](../../qlib-main/semiconductor_strategy/selected_param_set.json)为 `pure_capex/v1`，[固定日期日报](../../qlib-main/semiconductor_strategy/trading_signals/aiss_daily_report_20260921_20260921_185716.json)也记录同一选择。[AISSStrategyRuns.py:66](../../qlib-main/semiconductor_strategy/AISSStrategyRuns.py)的映射及 185 行定义表明 pure_capex 的 CapEx 权重为 1、供应链权重为 0。不能从默认 config 推断当前有效权重，也不能宣称 ASML 语义改进已直接改善当前 alpha。

[semiconductor_pipeline.sh:143](../../qlib-main/semiconductor_strategy/semiconductor_pipeline.sh)有七步数据更新，并接入 daily；失败记 WARN 后仍可使用已有数据继续。[9/21 updater](../../qlib-main/semiconductor_strategy/logs/aiss_update_data_20260921_185512.log)记录 7/7 OK；其中 32/32 是 fetch universe，不能说持有 32 只股票。[同日 daily](../../qlib-main/semiconductor_strategy/logs/aiss_daily_20260921_185512.log)记录九只持仓股票、三个子板块、零交易、`no_rebalance` 与 ledger 成功。

### 最值得试验的窄任务

1. **ASML SEC 新闻稿片段的指标语义。** [industry_signals.py:393](../../qlib-main/semiconductor_strategy/data/industry_signals.py)有实际 6-K press-release exhibits 入口。代码给定候选段落/数值位置后，Jev 可分 `next_quarter_net_sales_guidance / realized_net_sales / net_bookings / annual_backlog / qualitative_only / unknown`。币种、单位、区间、报告期组装和数值 sanity 仍由代码完成，不让模型生成数字。
2. **数据源变化的分诊。** 区分没有附新闻稿、解析未命中、抓取失败、停止披露、正常尚无新季度与上下文不足。可用来选择下一份应核验的证据，但不能将代码判定的 stale 改为 ok。
3. **研究片段是否支持既有供应链图边。** 仅供检索与人工审核；不更新 graph weight/lag，不取代数值校准。对当前 pure_capex，必须把相关数据层研究与正在使用的因子分开。

### 已修事项与拒绝样本

8/27 updater 未接 daily、旧 verify 仅查存在的问题已有接线与 freshness 代码；ASML 从停披露 bookings 转向 guidance 也已经修复。9/21 updater 119–128 行记录 guidance 22 条、无新记录、bookings 为 RETIRED；“无新季度”不等于抓取失败，也不能让 Jev 补出未披露的 bookings。

[test_asml_guidance.py:79](../../qlib-main/semiconductor_strategy/tests/test_asml_guidance.py)已有定性描述返回 None、反向/离谱区间拒绝，以及历史切换/单位测试。新增语义实验必须保留拒绝行为。[aiss_pit.py:112](../../qlib-main/semiconductor_strategy/data/aiss_pit.py)负责可用日筛选、冻结追加和数据频率对应的 freshness；这些都不是语言判断。

新闻稿完整原文快照与本系统首次实际可用时间仍须在 J0 清点。当前 `aiss_pit.py` 的相关筛选/序列构造按日处理；保存了 `acceptance_datetime` 不等于消费者已按精确时刻限制可用性，不能据此宣称已具备盘中 PIT 重放。

[9/20 weekly 日志](../../qlib-main/semiconductor_strategy/logs/aiss_weekly_20260920_032933.log)1236–1266 行中的 VIX/DD/STOP 警告属于历史回测。样本必须按完整 run/accession 分组、保留历史市场日，不能把它们标成 9/20 实盘事故。

## AEUS：电力产业链多源更新与证据解释

### 当前链路与真实记录

当前 [config.yaml:48](../../qlib-main/electric_utilities_strategy/config.yaml)列十个电力产业链子板块、41 只带权篮子股票，另有 reserve 和 benchmark；股票篮子的权威定义为 [data/universe.py](../../qlib-main/electric_utilities_strategy/data/universe.py)。这不是当前持仓数，也不能沿用早期三子板块登记数量描述整个当前宇宙。

[aeus_pipeline.sh:143](../../qlib-main/electric_utilities_strategy/aeus_pipeline.sh)有九个更新步骤，涵盖价格、CapEx、SEC、EIA、ERCOT、PJM。更新失败后 daily 仍可能使用已有数据继续，需保留源质量而非只看最后退出码。[9/21 updater](../../qlib-main/electric_utilities_strategy/logs/aeus_update_data_20260921_202302.log)214、242 行记录 capacity 90 月、最少 33 个来源码、所报告最大缺口为 0；260 行记录 `ercot_rt_price` 23/126 天正常累积、尚未进入 price_pulse；282 行为 9/9 OK。

[9/21 daily](../../qlib-main/electric_utilities_strategy/logs/aeus_daily_20260921_202302.log)18–19 行选中 `broad_5/v1`，42–43 行 E=1.061 而 gross 100%→100%，56–64 行为无调仓、零成交及日报/账本落盘。[对应日报](../../qlib-main/electric_utilities_strategy/trading_signals/aeus_daily_report_20260921_20260921_202615.txt)明确展示当前持仓。不能用 9/2 历史参数替代本次运行状态。

[调度 payload](../../qlib-main/electric_utilities_strategy/AEUS_CRON_PAYLOADS.md)是配置说明，不是现行安装验证；9/21 daily、9/20 weekly 日志证明各自那次运行，不能直接宣称严格按文档时刻运行。

### 最值得试验的窄任务

1. **异构来源的剩余异常分诊**：provider unavailable、配置/认证、schema 变化、依赖警告、正常 warm-up、上下文不足。已知“23/126 accruing”等固定语义优先规则处理；不为已识别信息重复调用 Jev。
2. **报告解释是否得到证据支持**：例如“今天实际加仓”“放大器提高了满仓 gross”“PJM 未接通”。Jev 审核文字与给定证据，rebalance、股数、source coverage 和公式仍由代码核验。
3. **历史回测告警的上下文筛选**：[9/20 weekly 日志](../../qlib-main/electric_utilities_strategy/logs/aeus_weekly_20260920_030011.log)5962–5967 行的历史警告不能按写入时间归到当前风险。先由运行元数据分开回测和运维，再做语义分类。

### 已实现约束与样本限制

[AEUS_PLAN.md:759](../../qlib-main/electric_utilities_strategy/AEUS_PLAN.md)记录放大器和防守现金约束，当前配置开启放大器但 `allow_leverage=false`；[portfolio/risk.py:227](../../qlib-main/electric_utilities_strategy/portfolio/risk.py)计算 E 和敞口，不能让 Jev 调整。当前 `event_derisk.enabled=false`，pipeline 也跳过半导体事件源，不能假设已有 AEUS 新闻自动减仓接口。

EIA 的分页/top-12 截断修复已有记录和 [altdata_signals.py:746](../../qlib-main/electric_utilities_strategy/data/altdata_signals.py)检查；当前实现检查最大正缺口及最少来源数，不应夸大为任意双向差额、全来源正确性的证明。[aeus_pit.py](../../qlib-main/electric_utilities_strategy/data/aeus_pit.py)继续负责可用时间与冻结追加。

本次精确匹配到 16 个 daily 日志（08-31 至 09-21）、22 个 update 日志、7 个 weekly 日志、21 个 TXT 日报。它们可支持分诊样本建设，但不等于独立事件数或修复前完整快照。一次 9/9 OK 更不能代替历史数据质量评估。

## BDC：披露语义、口径绑定和身份候选

### 当前链路与真实记录

BDC 股票 sleeve 包含 GBDC、TSLX、OBDC、BXSL、ARCC，并有 SEC 底层贷款穿透。[bdc_daily_pipeline.sh:71](../../conductor/bdc_daily_pipeline.sh)依次执行 rates、holdings ingest、look-through；[9/21 日志](../../conductor/logs/bdc_daily_20260921_160545.log)记录 16:05:45–16:07:12。日常重新估值不表示每天获得新的季度披露。

[latest_manifest.json](../../price_data/bdc_holdings/latest_manifest.json)的五家报告期均为 2026-06-30，filing 在 7/29–8/6、fetched 为 9/21；逐家 rows 合计 4,803。[deal 明细 CSV](../../portfolio_of_private_credit_deals/bdc_results/bdc_deal_start.csv)的只读统计为 4,803 个 deal/tranche 行、4,803 个唯一 deal_uid；2,164 个 company 字符串不等于已核验的法律实体数。

[9/21 日报](../../portfolio_of_private_credit_deals/bdc_results/daily_report_2026-09-21.json)同时包含运行日 9/21、披露 as_of 6/30、股票层 as_of 9/18；new/exited/changed/warnings 是跨快照比较结果，363 条 warning 不是当天新发生的 363 个独立信用事故。[RunBDCLookThrough.py:77](../../RunBDCLookThrough.py)已有数值 diff 与 severity，165–173 行有 accession 集合及 rates_date 幂等键。

### 已确认的抽取错误：可作为回归样本，不能作为 Jev 效果

1. **ARCC 的计量口径错配。** manifest `non_accrual_pct_fv=0.024`，自带 source_text 已区分 cost 2.4% 与 FV 1.4%。[原始 HTML](../../price_data/bdc_holdings/raw_cache/inst_000162828026050307.htm)完整段落确认：6/30/2026 为 cost 2.4%、FV 1.4%；12/31/2025 为 cost 1.8%、FV 1.2%。当前 FV 字段取到了 cost 数值。
2. **BXSL 的表格邻接错配。** manifest 保存 0.004；[原始 HTML](../../price_data/bdc_holdings/raw_cache/inst_000173603526000016.htm)表格的当前/上期列中，fixed-rate share 是 0.7%/0.4%，non-accrual cost 是 3.6%/0.6%，non-accrual FV 是 1.8%/0.5%。现有值取自上一行、上一期的 fixed-rate share。
3. [RefreshBDCHoldings.py:327](../../RefreshBDCHoldings.py)的 first-match regex 没有完整绑定指标、口径和表格列；955–976 行 QA 主要检查行数、FV 覆盖、gross/net，因而上述 manifest 仍为 ok。这不等于所有穿透字段都错，也尚未测量其对最终展示或决策的影响。

应先改进确定性段落/表格解析并建立人工核验真值，再比较 Jev 的增量检出率。**当前 manifest 不可作为这些字段的正确标签；本次没有修复或覆盖任何生产值。** 160 字符 source_text 可能截断关键限定，应读取带段落或行列标题的原文切片。

### 最值得试验的窄任务

1. **现存数值候选的语义角色。** 代码给出数值 token、来源 offset、完整行列标题和目标字段，Jev 标 `non_accrual_fair_value / non_accrual_cost / accruing_share / unrelated_metric / insufficient_context`。精确期间匹配、单位/小数转换、100−x 和数值写入由代码执行。候选漏失或标题缺失时弃权，不能让 Jev 编一个正确数字。
2. **非标准行业名的候选映射。** [bdc_sector.py:26](../../portfolio_of_private_credit_deals/bdc_sector.py)是 keyword first-rule-wins，行业与风险倍率相连。Jev 只能提出 canonical sector 候选；不能把概率直接写入行业风险倍率。缺 industry_source 的 477 行也不能统一认定为漏采或让模型补出事实。任何将 Jev 输出拟合/蒸馏成替代规则的后续用途还要遵守前述条款边界。
3. **issuer/tranche 对齐证据。** [RefreshBDCHoldings.py:670](../../RefreshBDCHoldings.py)有字符串归一，814–823 行按 CIK、issuer、tranche tag 构造 deal_uid。Jev 可给候选标显式别名证据、可能同 issuer 不同 tranche、无关或信息不足，不能改主键、合并贷款或推断信用因果。同名、fka、描述变更都不构成自动合并依据。

`bdc_deal_loader.py` 的 risks 是既有字段拼出的标签，不是独立信用叙事；未披露 EBITDA/杠杆等数据不能由模型补造。[loader:83](../../portfolio_of_private_credit_deals/bdc_deal_loader.py)

### 版本与评测前置

脚本注释/旧计划说只 ingest 新 filing，但当前 `RefreshBDCHoldings.py` 1049–1061 行每次遍历五家；994–995 行直接写相同 reportDate/adsh 的 parquet 路径，没有阻止覆盖。9/21 日志也记录重复处理同批 Q2 披露并写五家快照。文件名带 accession 不等于严格 immutable vintage，试验必须另冻结原文、parser 版本和 digest。

本次清点到十份 raw HTML、五家各两期共十个快照、67 份日报（6/16–9/21）；原文内容重点核验 ARCC/BXSL 相关段落与表格，并非十份全文逐一审计。67 日报不是 67 次独立披露，五家两期也不足以证明跨模板泛化。按 accession/issuer/template 分组；ARCC/BXSL 已公开给实验设计者，应放回归集，不能同时作为盲测成功证明。

## 六策略共用的对账与报告审核

### 先保留检查域，再解释文字

[Controller reconcile](../../controller/reconcile_eod.py)比较本地结构、shares 和同步价格的自洽；[QC reconcile](../../trading_quantconnect/reconcile/qc_reconcile.py)分别检查实际持股、已推目标、净值归因。它们不是同一个判定。

本次读取的具体例子：

- [Controller 9/21 报告](../../controller/output/reconcile_2026-09-21.json)：六策略 position_check 与 portfolio_check 为 ok，生成于 9/21 16:45 ET。
- [QC 9/21 报告](../../trading_quantconnect/reconcile/qc_reconcile_2026-09-21.json)：holdings_check 为 breach（两票未收敛到 v47），target_check 与 equity_check 为 ok；报告含 9/22 11:00 ET 的 settled_at。这是该留存报告状态，不是本次查询 QC 后得到的实时结论，也不是六策略全都失败。
- QC 9/18 报告保留较早 holdings ok 及后一次 `later_pass.pending_apply`。不能只取最后一个标签覆盖此前观测；版本先后和 pending 判定继续由代码执行。

这些已经能由结构化规则解读。Jev 的增量只可能在“这段对外说明是否把本地自洽误说成实际股份一致”“是否遗漏一个独立 breach”“是否把 later-pass 的等待应用说成已执行”等语义审核中。**模型不能把 breach 判成无害并关闭告警。** 9/21 源码及报告已更正 QC 分红按除息日入账的说明，而文件开头仍有旧付息日注释；引用旧叙述要标注其失效，不用模型猜哪个会计事实正确。[当前实现:799、1125](../../trading_quantconnect/reconcile/qc_reconcile.py)

本次找到 Controller 对账 21 份（8/11–9/21）、QC 对账 17 份（8/27–9/21），并非 90 天完整 incident 集。QC 同一 session 文件可能继续 settle，重放必须保存当次原始 bytes 与 observation time。

### 数据权威保持原合同

实际已执行股份、exporter 目标、NAV 展示投影、官方业绩与资本分母继续分开。QC 是跨策略净额账户，不能仅从净仓反推出六策略独立实仓。MRPT/MTFS 的加性资本桥、其余策略的冻结换算、bankers rounding、ledger_start 与 splice_at，均由现行合同和代码处理，Jev 不选择数值口径。[数据合同](../../risk_control/DATA_CONTRACT_AND_ONTOLOGY_PLAN.md)

报告审核的输入是**已由代码选出的事实与证据**，不是整个账户、原始持仓或全部日志。选择当前/历史/研究口径的语义可以成为模型任务，但允许读取的源、对象与时间边界由代码限定。不扩大现有前端路由权限。

## 调用频率与成本

2026-09-22 复核：当前 `jev-1.13.0`，输入 $0.042/百万 token、输出免费；公开限额 1,200 requests/min、250,000 tokens/s，可动态调整。上下文为每请求总计 64k，另须满足 state 加最长问题不超过 32k；设计应使用远小于上限的相关片段。固定模型版本，记录实际返回版本，不用会移动的 latest/preview 别名支撑已校准阈值。[模型与限额](https://docs.typesafe.ai/models)

以下仅为**每请求总输入 2,000 token、每月 22 个交易日**的算术场景，不是已测需求：

- 六策略各每天一请求：132 次，**$0.011088/月**；一请求显然不能覆盖所有复杂事件。
- 六策略各每天 20 个新的待判片段：2,640 次，**$0.22176/月**。
- 一次性审计 10,000 个不同证据片段：**$0.84/批**，不是月度固定成本。
- 六策略各交易时段每分钟一次，按每天 390 分钟：51,480 次，**$4.32432/月**。
- 同条件每秒一次：3,088,800 次，**$259.4592/月**；便宜也不能证明重复判断有价值。

公式：实际请求数 × 总输入 token × 0.042 ÷ 1,000,000。总输入包含 state、instructions、criteria；增加问题仍增加 token。未计数据授权、预处理、重试、其他模型、人工和运维。英文/中文真实 token 用 usage 测量，不按字符猜。已有规则几乎没有边际 API 成本，不能仅凭 Jev 单价低宣称替换后省钱。

调用应由**新内容、新版本或新的未知异常**触发：同一事件影响两条策略时，可在保留各自适用范围的前提下共享证据分类；不同来源、版本或时点不能仅因文字相似就合并。BDC 日常 rates 重估不要求重新分类同一季报；AISS 正常没有新季度、AEUS 已识别 warm-up 也不要求每日重问。

厂商公布 70–500ms 端到端表现，通常从美国西海岸测试，并承认特定 workflow 的倍数属于较高收益端；参考标签是强模型共识，不是本项目业务真值。[发布与评测说明](https://typesafe.ai/blog/introducing-system-one-models-and-jev)

Python SDK 默认单项 HTTP 操作超时 10 秒，不是完整请求含重试的总 deadline。未来实验要记录本机 p50/p95/p99、429/529、重试次数和整个任务时长；任何远程调用都放在策略锁和执行链之外。[SDK 常量](https://docs.typesafe.ai/sdk/python/api/constants)

## 与 ontology 的接入设计

拟议路径：**只读冻结证据 → 代码校验身份/版本/时间/运行阶段 → 规则处理明确情况 → Jev 判断剩余窄问题 → 校验输出并保存 annotation → 人工/强模型复核**。以下均是待实现设计，不是当前存在的接口。

1. **独立 provider。** 使用 `POST /v1/systemone`，不把它当作 OpenAI/Anthropic 对话模型直接换名。它服务 Query Monitor 的候选分类、R2 分诊和 Cascade 的补充判断；不承担复杂计划生成、最终权限或确定性 Checker。
2. **独立读写面。** 未来可在独立研究根下建立 `jev_systematic/` 的 inputs、annotations、evals、reports、cache 与专用环境，本次不创建实现目录。输入先冻结为副本；计算进程不获得生产写权限，不运行 DailySignal/refresh/exporter 来采样，不共享生产锁、cron、凭据或模型实例。`DailySignal.py` 会写库存，`Registry.spid_of(register_if_new=False)` 对已有 retired 对象仍可能触发变更，不能仅凭函数名认为只读。
3. **严格区分事实和推断。** 原始 PipelineRun/ReconcileReport、Evidence、账本状态保持不变；Jev annotation 关联 run 与 evidence，不写成 GovDecision 的批准，也不自动升级为根因真值。缺少源身份或运行阶段时保留 unknown。
4. **完整 provenance。** 拟记录 strategy、task、run_kind、phase、源记录键、book_kind、event/as_of、source available_at、captured_at、question/schema/parser 版本、输入与证据 digest、实际 model ID、原始概率/confidence、输出状态、代码处置、耗时、usage、人工标签来源。digest 只能验证已有 bytes，不能替代可重放的本地证据包。
5. **模型失败独立表示。** `abstain/insufficient_context`、源 stale/unavailable、API failed/timeout 和“证据支持该陈述”是不同状态。失败不得默认为 benign、clear 或 no-risk；影子阶段原链照常运行，Jev 无权消除原告警。
6. **缓存按证据版本。** 至少绑定模型、问题/候选版本、输入 digest、语义适用时间、来源与权限范围；新披露、更正、parser 变化使结果失效。相同请求不保证逐位相同结果，缓存是应用选择，不代表 API 免费重试或服务端折扣。
7. **训练与发布隔离。** Jev 输出标明受限来源，默认不进入训练/蒸馏导出。开源 provider 接口、脱敏样例和本地模型训练配方是否可发布分别审查，不把 Jev 商业服务打包成可再分发权重。D0–D7 不接生产；I0 才评审具体消费者、失败行为、回退和权限，C 编码路线继续禁触。

源没有首次可得时间时，不用 mtime 或文件名补造。当前 MRPT/MTFS JSON 中 signal_date 与 regime.as_of 不同，BDC 运行日、披露期与股票层日不同，都应保留。今天模型可能接触过历史结果，固定版本也不能证明无前视：历史实验首先验证“是否忠于给定证据”，交易用途最终需要向前验证。

## 接入与验证方案

### J0：证据清点与任务定稿

在独立研发环境准备六策略适配清单、数据字典、事件分组、代码版本、可重放 manifest 和人工标签规范。区分 daily/live decision、historical backtest、weekly review、data update、reconcile；无法由记录确定的阶段填 unknown。对同 run 重复日志与同 filing 切片去重，保留生成时点和源上下文。

先将本次发现的确定性缺口登记为单独工作项：BDC 口径/表格解析与同版本覆盖、SSRS 两条 P/E 缓存路径检查覆盖、MTFS 局部 finite guard。各自后续代码修订需单独测试与评审；不能让 Jev 的高置信度成为不修代码的理由。原解析器只作 incumbent，人工核验后的证据才是 gold label。

**交付与门槛：** 六策略都有明确候选任务、输入字段、拒绝条件和可追溯样本；无法重放的历史案例单列，不伪称 90 天齐备。先证明隔离和 provenance，再进行模型实验；本阶段无需调用 Jev。

### J1：两个最小离线影子实验

准备 `systematic_ops_triage_shadow` 与 `bdc_disclosure_semantics_shadow` 的独立测试客户端。前者共享分类规范、六策略分开报告；后者给定相同候选和原文，比较当前正则、改进的确定性解析、相同解析加 Jev 审核。不要通过给 Jev 更多人工信息制造不公平对照。

起步可按可获得数据准备 120–240 条人工核验的跨策略分诊/报告案例，正常、已处理警告、硬失败、未知都要有；这是目标，不是当前已存在的题库。BDC 按独立 filing/模板留出，而非用每日重复报表扩充样本数。已读过并据此设计 prompt 的 ARCC/BXSL 错例只作回归样例，另找盲测或等待新披露。

**观察指标：** 严重事件误分和漏提示、错误根因归因、正常事件误报率（分母为正常事件）、弃权覆盖、候选提取召回、字段口径错误漏检、报告不支持陈述的错误放行率，以及人工审核耗时。不能只报总体 accuracy，也不能把原系统的 WARN/OK 直接当根因标签。

### J2：扩展披露片段与报告支持判断

在 J1 的数据和运行评测合格后，增加 AISS ASML 片段、MRPT/MTFS 复盘、SSRS/AEUS 无调仓与数据层说明、BDC 多时点报告。先运行已知规则，模型只处理剩余语义。每个任务单独验证中文/英文、否定、定性描述、修订/撤回、错误实体/时期、缺标题、注入文本与超长噪声。

同一事故跨多次参数回测的重复信息必须在同一分组；同一 filing 不能跨训练/阈值集和最终测试。按时间划分，prompt、候选生成和阈值在最终留出前固定，代码时代变化单列。原始数字和明确状态由确定性测试验证，模型不负责补证据。

### J3：前向影子与运行验收

只消费获准的只读采集包，在独立进程持续保存模型建议和原系统结果，**不改变生产行为**。建议观察至少一个完整月度运维/调仓周期，并单列实际遇到的严重事件数；BDC 新模板/新季度的泛化需要等待对应新披露，不能由一个月重复调用代替。

参照 ontology R2 的召回≥90%、误报≤20%作为候选评测目标，同时报告样本量、分策略/分错误类型表现及不确定性，不能把少数样本全对当成可靠性保证。严重硬失败的明确状态不得被任何模型输出覆盖；需要自动影响流程的任务应另定更严格的门槛。量总成本、人工负担、真正省掉的强模型调用、真实 p95/p99 和失败降级；无增量时保留规则。

**不通过的处理：** 返回证据/候选构造或任务定义修订；重新定义后使用新的留出评测，不反复在同一盲测上调优。API 限流、过载和缺失证据保持可见，不能在统计中丢弃困难请求。

### J4：单独决定生产集成或交易特征研究

只有前述结果支持时，才按 ontology I0 提交具体接线提案，包括资源、消费者、权限、超时、回退、审计和删除/禁用方式。优先考虑只读报告和人工分诊辅助；不会因一次离线分类达标自动修改策略或前端。

如另行研究 Jev 文本特征参与选股、事件 veto 或风险 overlay，必须先新增合格原文采集和事前发布的特征快照，并独立验证每个策略的增量。比较原策略、规则 overlay、Jev overlay，以及匹配 gross/行业暴露或交易覆盖率的简单降仓基线；记录放弃盈利、避免亏损、费用、换手、滑点与执行可行性。BDC 的风险字段、AISS/AEUS 图边不能由分类 confidence 直接转成权重；MRPT/MTFS 也不能从旧成交账本删去亏损交易便称得到新收益。

运营分类有效与交易 alpha 有效是两项不同验收。本计划不预设六策略必须全部使用 Jev，允许只保留通过测试的任务。

## 仍未完成的实证与资料保留

本次完成的是代码/记录驱动的研究，没有 Jev API 实测、账户额度核实、延迟压测、人工标注集或交易收益对照。9/17 控制台曾跳转登录，本次未重新登录。官方价格不等于账户合同价格，公开测试不能替代我们中文日志、财报模板和网络环境的结果。

数据政策称不以客户输入输出训练模型，enterprise 可另谈 ZDR；不训练不等于零保留。未来仅发送获准且最小必要的脱敏片段，金融身份、账户、持仓和私有参数通常无需全量外传。保留安排、版本保留期、固定吞吐和适用协议应在实际接入时核验。[官方数据说明](https://docs.typesafe.ai/legal)

以上记录链接指向工作区原件，其中 latest、当前日志和对账文件会继续更新。为识别本次读取版本，两个关键对账样本的 SHA256 为：

- Controller 9/21：`bcd715dfc17ecce42b15e2b1c490919d8f143a0130c529708ffd4651d9baadbd`。
- QC 9/21（含 9/22 settle）：`02cfaab775812c70c1a49b03444a373bbdb4a8f961264455c5e0ab5fa28b2e89`。

本轮只新增本文，不修改预测市场研究、ontology 主计划、既有事故记录、代码或生产数据。下一步的第一项工作是按 J0 建立可核验的样本与隔离合同；Jev 是否值得接入，由各任务的对照结果决定。
