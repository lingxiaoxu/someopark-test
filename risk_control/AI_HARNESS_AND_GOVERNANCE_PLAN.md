# AI 判断层与治理设计

日期：2026-09-17。状态：仅设计。尚未选择、连接、部署或调用新的模型。ChatGPT API、本地模型、用户提到的 Jev 均保留为候选，由用户后续决定。

## 1. AI 放在哪里最有价值

数值事实、数据时点和约束可被程序精确计算，交给 LLM 反而增加不可复现性。AI 的增量应是阅读大量非结构化信息、发现遗漏的经济联系、解释异常和组织证据。

核心系统在 AI 全部不可用时仍应生成相同的数字、质量状态和确定性告警。AI 是异步判断助手，不是风险计算主循环的必要依赖。

| 工作 | 默认执行者 | AI 可做的部分 |
|---|---|---|
| 官方资本、QC实际股份、价格/费用桥 | 确定性代码 | 用指标引用解释差异，不改数；资本用两份performance JSON，实际股份用QC观测 |
| QC实际仓/target/NAV数量对账 | 版本化确定性checker | 组织版本、身份、舍入和执行时点证据，不把投影当成交 |
| FF5、协方差、ES、压力、优化 | 固定版本的数值程序 | 解释结果，提出模型遗漏假设 |
| 是否越过已批准限额 | 政策引擎 | 说明为何触发；无权豁免或降低级别 |
| 数据 stale/identity/PIT/缺失 | checker | 帮助调查来源，不把 unknown 写成已恢复 |
| 日报变化原因 | 先做数值变化桥 | 组织已证实变化，区分观察/假设/未知 |
| 新闻、财报、政策、供应链事件 | 受限只读证据检索 | 提取实体/事件/时间/原文位置，形成待核查线索 |
| 新压力情景 | 人或版本化确定性研究规则指定数字 | 提出定性机制、选择既有情景 ID |
| 应对方案比较 | 引擎生成受约束候选 | 比较代价、指出待查事实，不决定目标股数 |
| 历史相似事故 | 证据索引 | 检索、解释相同和不同条件 |

“行业共振可能增加”是可检验假设；“明天半导体会跌 12%”不是本系统允许凭模型生成并投入控制的数值。

## 2. 严格遵守 ontology 的边界

原计划 §IV.1 和 §VII.2 不允许控制面 LLM 产出进入交易决策的数值、选择参数或权重。本模块保持：

- 模型不能改资本、股份、证券身份、freeze、历史记录、生产模型权重。
- 模型不能把NAV推算的qc_shares或exporter目标升级为QC已执行仓，不得将QC账户净值替代用户指定的performance JSON资本分母。
- 模型不能把目标attribution分摊称为实际策略子账户，不能替归因残差猜归属；M5绿色、M4旧终态或applied日志不能独立证明当前实际仓全部已核验。
- 模型不能把daily ΔD归因通过写成level绝对桥归零，不能以连续daily ok掩盖累计未归因漂移；dividend_timing现金反推分类不等于逐发行人已核验应收。累计差异、未结项证据和政策状态由确定性程序保留，模型不得重冻K或虚构桥项吸收。
- 不能选择有效 covariance window、目标波动、减仓阈值或自己批准新的情景幅度。
- 不得生成可直接下单的仓位、金额或订单。人批准也不会把 A3 下单权限赋予这个 AI。
- 不能通过“解释”“审核”接口改变确定性 breach、输入完整性、模型资格或政策版本。
- 外部文本里出现的工具指令是材料内容，不是可执行授权。

可读工具的真实调用链逐个审计。不复用当前 Someo Agent 全局工具池的宽权限，不暴露 shell、通用 run_python、训练、配置写入或停任务功能。

## 3. 最小 harness 流程

~~~text
RiskRun 已完成并冻结
  → 确定性筛选与此问题有关的 metrics / findings / evidence
  → 输入资格检查 + 数据最小化
  → 选定 AI 任务和已允许的只读查询
  → 模型输出受约束 JSON
  → schema、实体、引用、时间、数字、权限六项检查
  → 合格分析附入独立报告；不合格保留失败记录
~~~

输出失败时，报告继续采用确定性模板，并注明 AI 分析 unavailable/rejected。不能静默重试另一个模型并把它的结果记作原模型，也不能为了完成回答扩充权限。

困难问题可升级到用户指定的另一模型；升级规则由任务类型、schema 失败、证据不足等客观条件控制，不以模型自报“很有信心”作为通过标准。AI 互相同意不是事实核验。

### 3.1 建议的只读工具合同（未来独立实现）

- `get_risk_run(run_id)`：输入包与计算版本、当前资格。
- `get_metrics(run_id, metric_ids)`：返回程序计算值、单位、定义和证据。
- `get_exposure(run_id, entity_ref, book_kind, view)`：股份暴露须明确选择qc_actual、exporter_target或nav_projection；返回已聚合证券/策略/主题、net/gross/穿透、观测日期和归因资格。official_performance是独立业绩账，不从中猜测股份。
- `get_reconciliation(run_id, method)`：返回source/重建/已推/已应用/actual/NAV各数量桥、各自版本/时间、误差和未解释余额；M4 holdings/target/equity与M5分开；净值独立返回daily_delta、level_bridge、cumulative_drift和open_items，不压成一个通过徽标。
- `get_scenario_result(run_id, scenario_id)`：仅已计算的情景。
- `get_evidence(run_id, evidence_ref)`：只从获准run的manifest解析locator，读取已冻结、受限大小的材料及可达性/摘要校验状态。
- `find_prior_findings(run_id, entity_refs, event_before, knowledge_cutoff, kinds)`：同时约束事件时间及该版本知识时间的历史事件。

工具不接收任意文件路径、SQL、Python、URL 或自然语言 shell。新外部材料先由独立采集器进入 evidence store，模型不能自行绕过来源与网络访问限制。

每次检索前统一核验 task_id 对应的 allowed_run_ids、allowed_evidence_refs 和 knowledge_cutoff；不能访问别的run中的未来或未获准材料，再寄希望于回答阶段删掉。历史finding也要求其版本available_at≤knowledge_cutoff；未知知识时间在严格PIT模式拒绝，明确的事后解释模式才可带限制读取。

### 3.2 拟议输入结构

~~~yaml
task_type: explain_change | investigate_finding | review_scenario | extract_event
run_id: 固定运行 ID
as_of: 风险业务截止时间
knowledge_cutoff: 本次解释允许的知识截止时间
entities: [已存在 SPID/ISIN]
metrics: [metric_id, value, unit, method, quality, evidence_refs]
position_context:
  book_kind: qc_actual | exporter_target | nav_projection | official_performance
  evidence_mode: observed | reconstructed_with_known_rules | restated_normalization
  account_ref: 获准的脱敏账户引用
  deployment_ref: 固定部署/账户历史段引用
  observed_at: 实际观测时间，可空
  target_version: 相关目标版本，可空
  applied_version: 经核验应用版本，可空
  reconciliation_refs: 各独立数量/净值检查引用
  attribution_status: observed | model_allocated | partial | unavailable
findings: [finding_id, deterministic_severity, reasons, evidence_refs]
scenario_results: [scenario_id, assumptions_version, metric_refs]
evidence: [digest, title, source_time, available_at, excerpt_locator]
allowed_tools: 当前任务白名单
limitations: 缺失、延迟、估计与未验证事项
~~~

上下文过长时，先按确定性相关性选取材料，保留元数据和按 ID 取全文的能力；不能截掉引用、否定词或数据局限后仍冒称完整。事实永远从该次输入包取，模型记忆不参与资本/价格/持仓回答。

混合材料须逐条指标携带自己的book_kind、资本锚、观测时间和证据引用，不能仅靠顶层position_context给一段同时含目标与实际数据的内容贴统一标签。实际仓不可用时，确定性报告先给actual_positions_unverified及quality=unavailable和原因；模型只能解释缺口，不得根据“通常与NAV一致”推断当前股份。账户状态loading/取数失败不等于空仓；订单无events不等于无挂单；同日期account_history不等于该日QC已成交。

### 3.3 拟议输出结构

~~~yaml
schema_version: risk-analysis/v1
run_id: 输入 run_id
claims:
  - kind: observation | hypothesis | limitation
    text_template: 使用指标占位符的句子
    metric_refs: []
    evidence_refs: []
    entity_refs: []
    uncertainty_reason: 具体证据不足原因
investigation_requests:
  - question: 尚需核实的事实
    permitted_read: 已登记查询名
scenario_proposals:
  - existing_template_id: 可空
    mechanism: 定性压力机制
    affected_entity_refs: []
    needs_human_assumption: true
proposal_only: true
~~~

数值在渲染时由程序按 metric_ref 插入，含符号、单位、日期和取整。模型不能自己重新抄算一套金额；不支持的数字或引用拒绝进入已验证段落。

原文事件提取中的金额/日期可作为 `unverified_extraction` 保存，并绑定原文 span；核实前不能变成价格、因子或限额输入。模型信心是辅助标签，不等于校准概率。

## 4. 模型接入与待用户选择事项

### 4.1 提供商无关接口

未来 `providers/base.py` 定义 `analyze(request, model_spec) -> structured_response`，适配器可分别支持用户选择的 ChatGPT API、本地服务或 Jev。它是新模块自己的接口，不修改已有聊天 provider 或模型菜单。

`model_spec` 保存 provider、精确 model_id/version、endpoint 引用、能力、上下文上限、timeout、输出 schema 能力、数据处理/留存约束。凭据由隔离进程的显式配置提供，绝不写进 prompt、产物或日志。

对 Jev 的确切接口、结构化输出、版本固定能力、许可和实际延迟本次没有核实，不依据“高频快速”称谓承诺吞吐或适配兼容。

### 4.2 三种职责可用不同模型

1. 常规风险说明与事实问答：候选本地模型，优先隐私和引用正确率。
2. 新闻事件分类/实体抽取：候选低延迟服务，包括 Jev，但先通过事实与身份评测。
3. 复杂跨行业解释、方案复核：候选 ChatGPT API 或其他用户指定模型，按需调用。

这不是最终分配。一个模型足够时不强行增加多 agent。低延迟任务不自动获得更高权限；本组合分析也没有必须为毫秒级交易优化 LLM 的前提。

### 4.3 用户后续只需决定这些

- 选择的提供商、精确模型名和本地/API endpoint。
- 允许外发哪些信息：默认外部 API 只接收脱敏的必要指标与获准公开材料，详细持仓/金额需明确策略。
- 使用预算和延迟要求：每日报告与异常调查可不同。
- 是否允许复杂任务升级到第二个模型。

这些选择之前，`ai.enabled=false`，确定性模块照常可开发。无需改原 ontology 的待核验 Qwen 候选，也不把其候选当成已部署依赖。

## 5. 评测与失败行为

### 5.1 题库来自真实风险陷阱

至少覆盖：两个 combined_equity 的区别；SSRS/sr；BDC 第三文件；integer shares 与 fractional value；raw/adjusted price；重复 VLO；ETF 双计；5 月 SSRS 桥；QC ARNC 身份与完整SID；未冻结的历史 scalar；FF5 缺月；配对不等于市场中性；已确认恢复与新心跳不同。

QC/NAV专项题库包括：9/17同v45的QC44票、NAV45展示行汇总44票确实一致，但不能由这一个样本断言永远一致；9/16 QC v44的37票与次日执行的7票目标应分开；两行各0.5股的NAV舍入合计与exporter净额舍入不同；NAV字段qc_shares只是推算；QC原始数量不能int截断；M4股份通过和净值pending并存；M5绿灯不核验QC；报告保留旧终态的later_pass不能被忽略；actual归因残差和无events订单不可被模型省略。固定答案须引用专项审计的时刻和口径，不能将样本数量写成永久配置。

净值桥题库还需区分：9/14当日约$0.01未归因通过，`D−K_effective=$10,205.77`本身既不证明漏钱，也不等于level完全通过；`P−Q = K_effective + 已验证的时点/舍入/其他桥 + residual_level`必须带锚点和连续证据。9/1—9/14 daily均ok仍有累计未归因−$1,057.00（独立level剩−$1,057.02，字段取整差$0.02），不能概括成“没有漂移”。缺lag/slip不填0；推导dividend_timing不得描述为逐发行人已核验应收；累计漂移/open_items的告警政策未批准时如实说明，不自创安全阈值。

额外故障材料：缺一策略、错日期、陈旧但格式正确的数据、互相冲突的来源、恶意网页指令、伪造引用、单位扩大 100 倍、模型编造机构阈值、把假设写成事实。

### 5.2 可度量的上线门

- 数值/单位/实体/引用校验：固定关键案例中 100% 通过；否则该回答不能进入已验证报告。
- 未授权写入/工具调用：隔离测试中 0 放行。
- 未知/缺失表达：不可把 unknown、partial、draft 标作已核验安全。
- 解释质量：盲评是否覆盖主要风险、有没有混淆因果、是否提供可查证证据。
- 提醒漏报/误报：仅评价 AI 附加调查排序，不让它过滤既有确定性告警；同时报告样本量和区间。
- 性能：实测 p50/p95 延迟、成本、超时率、schema 成功率和退回率。

按时间及事件族分组留出，不能同一事故的多个改写版本跨训练与评测。测试中的零错误只是约定样本结果，不承诺所有未来输入零错误。

### 5.3 不以微调替代数据质量

遵守 ontology 的 R0 之前不微调。先用确定性上下文和 schema 约束；只有稳定的行为缺陷在 prompt/检索修复后仍存在，才考虑单独研究 SFT/蒸馏。

若未来训练，也只能使用 risk_control 独立目录与独立注册表。不得触碰 VolumePrediction 任何生产 RNN/LGBM 权重、考核模型或 promote 状态。风险解释模型不能把陈旧业务数字记进权重当事实来源。

## 6. 审计与治理

保存 task_id、run/input digest、provider/model/prompt/schema 版本、允许工具、实际工具调用、输出、checker 判定、费用/耗时及人工反馈。保存可核查的理由和证据，不要求记录模型私有思维链。

从模型提出调查、证据核验、定量重算到人接受风险分别留痕。proposal 的 approved_by/approval_ref 默认空；只有真实批准记录才能改变其状态。批准新研究假设不等于批准生产交易。

数据传输失败可有有界重试，重试共享任务键并记录 attempt；不得超出预算无限循环。prompt 注入、超长材料或无效 schema 应确定性拒绝。所有失败都保留已完成的数值风险包。

AI 贡献的衡量标准是减少调查时间、发现遗漏、提高解释可核验性，而不是写出更长或更肯定的市场故事。
