# TypeSafe Jev 与预测市场：接入研究与自审修订

初稿：2026-09-17。复核修订：2026-09-22。

本文件是仓库内的可读版本；[交互报告与成本计算器](/Users/xuling/.cursor/projects/Users-xuling-code-someopark-test/canvases/JEV-prediction-markets-research.canvas.tsx)包含模块切换及更多代码引用。

本次对照公开官方资料、当前工作区代码和已有审计文档。代码核查时 HEAD 为 `7de448b1cf5c53b06f0b8bb3118d7352eed97d9c`，工作区可能包含未提交改动，因此该 commit 不能单独重建本次全部观察。没有调用 Jev API、运行交易策略、修改交易代码或写交易数据库。

## 结论

有接入空间。最合理的第一步是 Macro 新闻分类的影子对照，再扩展到俱乐部足球和 Crypto 的公告、事件与证据冲突判断。Jev 可以通过已发布的语义特征参与每次候选下注；无需每次下注都发送一次远程请求。

这仍是集成与实验建议。尚未证明它比当前模型更准确、更快、更省总成本，更没有证明它提高了扣成本后的交易收益。

## 本次自审发现与修正

### 原报告错误或表述过强

1. **足球 900 秒频率写错了含义。** 原文称“新结算检查为 900 秒”。实际是完整刷新、构建和发布触发器的检查间隔；纸面结算也在每次 live cycle 和 paper cycle 中检查。live 配置仍为 60 秒。
2. **“现有模型昂贵、慢”缺少测量支撑。** Macro 使用自托管 Nemotron。能确认进程内串行锁、180 秒请求超时、最多两次尝试，不能从超时上限推导正常时延，也不能从模型大小推导 Jev 一定更省钱。锁只提供同进程互斥。
3. **分类建议混合了不互斥的维度。** “官方公布”和“更正”可以同时成立。已改为来源认证、报道类型、修订状态及主题相关性分别处理。
4. **W10 的“公允值”措辞过强。** FV60 计算未经校准的正态 CDF 概率代理；它继承 W8 管理和成交路径，是条件纸面对照，并非独立执行器。60 秒约束报价创建时间，不代表到时自动撤销已有订单或平仓。
5. **示例把已撤回的历史说法也可能算成冲突。** 已改为判断修订之后仍未解决的冲突，并要求代码核验修订关系。
6. **成本计算器存在边界输入问题。** 原默认数字正确，但超出输入范围时会暗中截断计算值。已改为显式校验；零候选可计算为零费用，并将对象数与真实并发数区分。

### 原报告重要遗漏

- **Macro 分钟 tick 没有分钟新闻采集。** 当前新闻在每日 refresh 路径拉取；分类只读取近 36 小时最新 40 条标题和来源，不含正文与发布时间。同 ID 使用 `INSERT OR IGNORE`，不能捕捉同文章修订。已有 `first_seen_ts` 可复用。分钟级方案必须先补采集频率、正文及修订版本。
- **减少交易本身也能降低回撤。** 评测增加匹配风险暴露或成交覆盖率的减仓/抽样基线，区分语义判断增益与单纯交易更少。
- **分组之外还要守住训练边界。** 按时间划分训练、验证和最终测试；清除标签跨边界样本，必要时留间隔。prompt、阈值、特征与校准不能根据最终测试结果反复挑选。
- **风险 veto 与概率特征是不同实验。** 概率特征研究可在基础质量与运行验收后并行开展，不应以 veto 是否成功作为其必要前提；上线仍需要各自前向验证。

### 9 月 17 日之后的资料变化或补充

- **MCA 于 9 月 19 日更新。** 当前公开 §2.3 已不再包含旧版的公开 benchmark 禁令。原报告在初稿时引用旧版，此次已更新；模型模仿/蒸馏限制仍在。公开网页不等于本账户适用的单独协议。[当前 MCA](https://typesafe.ai/legal/mca)
- **官方限制说明现明确跨问题概率不保证逻辑一致。** Noul 与 Choice 的概率不保证相同，分别问命题和否定也不保证和为 1。固定问法与类型，逐任务校准，逻辑约束交给代码。[模型限制](https://docs.typesafe.ai/model-jaggedness/jev-1.13)
- **模型页现明确英语表现最佳，且不提供客户级 fine-tuning / LoRA。** 足球多语言来源需要分语言验证，不能假定英文能力等价迁移。[模型说明](https://docs.typesafe.ai/models)
- **9 月 20 日仓库审计补充了 W10 上游可得性证据。** 首报价发现延迟、事件排除和继承执行路径会限制“每笔成交前可用”。这属于当时冻结样本，不是 9 月 22 日新实测。[已有审计](/Users/xuling/code/someopark-test/crypto-dev/26_pfme_drawdown_and_runtime_audit.md:91)

## Jev 的能力与边界

Jev 是 TypeSafe 的 System One 模型，接收文本或 JSON 状态及有限问题，返回 Choice、Score 或 Noul。它适合窄语义判断，不生成开放式解释。官方介绍 RLCD 和并行问题处理；本次未独立验证其内部训练机制。[介绍](https://docs.typesafe.ai/introduction) · [训练目标说明](https://docs.typesafe.ai/introduction/machine-learning-primer)

- Choice：有限选项、概率分布及 confidence。
- Score：有序语义等级的加权分数与分布；不是精确数值回归器。
- Noul：yes 的概率，不带独立 confidence 字段。

同一状态可批量问多个问题；额外问题仍增加输入 token。分类格式受约束并不意味着事实正确。confidence 是输出分布的统计量，不能直接替代球队获胜概率、合约结算概率或 Kelly 的 p。[Confidence](https://docs.typesafe.ai/confidence) · [API](https://docs.typesafe.ai/api)

官方承认数字、日期、多跳、对抗文本等弱点。多问题独立计算也不意味着错误独立或逻辑自洽。净 edge、时间比较、仓位、手续费、结算、盘口与硬风控均应由确定性代码处理。[已知限制](https://docs.typesafe.ai/model-jaggedness/jev-1.13)

公开 workflow eval 主要是安全事件、agent 轨迹、发票与客服，并使用强模型共识标签；它不是这三个预测市场的收益证据。[官方评测方法](https://evals.typesafe.ai/)

## Macro：优先，但先补新闻输入

当前分钟 tick、普通持仓 15 分钟维护、事件窗口分钟维护，为高频消费语义状态提供了入口。但新闻并没有跟着每分钟更新。[tick](/Users/xuling/code/someopark-test/prediction_market_macro/jobs/tick.py:384)

实际问题包括：

- `refresh` 拉新闻、predict、decide，之后才生成新闻 flags；新标签晚于当轮决策。[刷新顺序](/Users/xuling/code/someopark-test/prediction_market_macro/ops/refresh.py:187)
- 新闻分类只用标题/来源。同 ID 文章不更新，难以识别撤回和正文修订。[分类输入](/Users/xuling/code/someopark-test/prediction_market_macro/analysis/llm.py:103) · [采集存储](/Users/xuling/code/someopark-test/prediction_market_macro/ingest/market_data.py:169)
- flags 默认五天 TTL，相同 tag/family 未过期就跳过；direction 不参与去重。消费侧主要用 severity 收紧门槛与减仓，未直接将方向变成预测概率。[flags](/Users/xuling/code/someopark-test/prediction_market_macro/analysis/llm.py:176) · [消费](/Users/xuling/code/someopark-test/prediction_market_macro/ops/decide_all.py:514)
- `decide_all` 持执行锁，远程请求必须在锁外异步完成。若观察“每笔下注”，应覆盖 open、argmax、arb、snipe 路径；套利和已发布数字的数学仍由代码负责。[决策入口](/Users/xuling/code/someopark-test/prediction_market_macro/ops/decide_all.py:296)

优先任务是新闻相关性、明确修订/撤回、报道与推测区分、针对指定指标/统计期的方向、FOMC 语言变化。先与 regex 和现有 Nemotron 并行，不替换定量模型，也不从新闻 confidence 直接推仓位。

## 俱乐部足球：需要新的自然语言信息

live 调度为 60 秒；900 秒是完整刷新/发布触发器。纸面结算随 live cycle 检查。入场和退出仍受 PRE、T15、T30、HT、T60、T75 等 milestone 窗口约束，并限制同场每 track 的入场次数。[结算检查](/Users/xuling/code/someopark-test/prediction_market_soccer/ops/live_refresh.py:655) · [决策窗口](/Users/xuling/code/someopark-test/prediction_market_soccer/ops/paper_trading.py:366)

本次只读复核中 injury 表仍为空；事件 comments 主要是固定标签。未发现持续新闻或完整文字解说主链。把这些固定标签每分钟发送给 Jev 没有明确价值。

可新增俱乐部公告、发布会、可信记者和文字解说，再分类球员缺席报道、否认、轮换原因、VAR 待定等。官方比分和结构化事件优先；球队/球员绑定、来源认证及时间关系由代码核验。

当前 PRE 可在 value 决策未选中时回退 argmax；不能笼统说所有路径“无 edge 就不下注”。纸面与显示模型的 xG/补时输入也不完全相同，实验要固定真实 incumbent。[PRE 回退](/Users/xuling/code/someopark-test/prediction_market_soccer/ops/paper_trading.py:134)

Poisson、Dixon–Coles、比分、时间、费用和 Kelly 保留。若改成每分钟都能开仓或退出，应视为独立策略实验。

## Crypto：已有数值基线，Jev 必须带来增量信息

- W7：15 分钟合约、约 60 秒扫描。
- W8：约两秒独立循环，处理成交、库存、配对、退出与重挂。
- W9：服务轮询两秒，但每模型每分钟最多尝试一次预测。RNN 主要预测成交量/绝对收益风险，不等于涨跌方向模型。[去重](/Users/xuling/code/someopark-test/crypto_trading/crypto_strategies/w9_rnn_paper/model.py:250)
- W10：本次复核仍为 FV60，每秒观察本地源；首次报价时计算概率代理，约束首报价后 60 秒内创建的入场报价。它继承 W8 的执行管理路径。[FV60 定义](/Users/xuling/code/someopark-test/crypto_trading/crypto_strategies/w10_entry_paper/fv60_policy.py:1)

最有价值的候选是新增监管、交易所、协议事故、攻击和宏观冲击文本，异步发布资产/到期组的语义特征。每笔候选读取缓存，W8 的止损与订单管理不等远程 RPC。

如果 Jev 改变 W8 报价或库存，必须独立模拟整个执行路径。不能从母策略历史账本删掉亏损成交后称为新策略收益。W10 的条件对照可借鉴其事前发布机制，不能被当作独立成交能力的证明。

## 调用频率与成本

2026-09-22 复核：公开版本仍为 `jev-1.13.0`，输入 $0.042/百万 token，输出免费；公布限额为 1,200 requests/min、250,000 tokens/s，官方说明可能动态调整。总上下文为 64k，另需满足 state 加最长问题不超过 32k。[模型与限额](https://docs.typesafe.ai/models)

厂商公布 70–500ms 端到端区间，但没有本项目部署位置/负载下 p95、p99 实测。Python SDK 默认单项 HTTP 操作超时 10 秒，重试可能继续扩大等待；生产须另设整体 deadline。[发布说明](https://typesafe.ai/blog/introducing-system-one-models-and-jev) · [SDK 常量](https://docs.typesafe.ai/sdk/python/api/constants)

采用每请求总输入 2,000 token、30 天、24 小时运行：

- 100 对象，每 15 分钟一次：288,000 请求，$24.192/月。
- 100 对象，每分钟一次：4,320,000 请求，$362.88/月。
- 100 对象，每两秒一次：129,600,000 请求，$10,886.40/月；平均 3,000 RPM，超过公开 RPM 限额。
- 100 对象，每秒一次：259,200,000 请求，$21,772.80/月；平均 6,000 RPM。
- 每天 10,000 候选，每候选一次：300,000 请求，$25.20/月。这是另一种调用规模，不能与以上对象轮询规模直接比较。

公式：月请求数 × 每请求总输入 token × 0.042 ÷ 1,000,000。总输入包括 state、instructions 和 criteria；未计数据、重试、其他模型、税费和运维。缓存只减少重复调用，不应抹掉实际请求成本。

每 15 分钟可维护低速事件；每分钟检查新证据；每次下注检查合格缓存。没有新语义内容时不必重问。低于平均配额仍可能因突发流量限流。

## 接入与验证方案

推荐路径：采集新证据 → 代码核验来源/实体/时间并去重 → Jev 原子任务 → 版本化特征快照 → 每次交易候选读取 → 原有统计策略、硬风控与执行。

快照记录 model、prompt/schema 版本、证据 hash、published_at、first_seen_at、request/response/persisted_at、valid_until。现有 first_seen 字段可复用，特征不能早于真实可用时间生效。更正与撤回使旧特征失效；source timestamp 不能代替 receipt timestamp。

明确区分 clear、conflict/uncertain 和 unavailable。API 故障、过期或缺数据不等于无风险。影子阶段不改变基线；未来依赖语义证据的新增策略应预先定义缺失行为。强制退出与硬风控保持独立。

验证顺序：

1. 语义准确性：规则、现有模型与 Jev 分层对照；关注重大漏判、误触发、否定/更正、unknown 和语言差异。
2. 运行性能：实际区域和输入规模下的 p50/p95/p99、超时、429/529、事件到特征可用时间、usage 与缓存。
3. 前向影子：原策略、规则 overlay、Jev overlay；另加匹配暴露/覆盖率的基线。记录未下注候选、避免亏损和放弃盈利。
4. 概率特征：独立研究 Jev 特征进入统计模型的增量，检验 Brier/log loss、校准及扣成本收益。不能用单次分类 confidence 代替校准。

按时间与独立事件分组，隔离训练、验证和最终测试；Macro 按 release，足球按 fixture，Crypto 按时间块与相关到期组。今天的模型可能记得历史结果，因此历史回放只作初筛，最终依赖向前验证。

第一轮建议仅实现 `macro_news_risk_shadow`：先补采集与版本证据，再并行模型对照，不写生产 flags 或交易账本。分类运营价值可以单独验收；交易 alpha 需要另证。

## 仍未完成的实证

没有 Jev API 实测、账户额度核实或交易收益对照。9 月 17 日控制台跳到登录页；9 月 22 日未重新登录。官方价格不等于账户合同价格，厂商延迟不等于我们的 SLA。

正式合作可讨论固定吞吐、模型保留期、区域延迟和领域评测支持。公开条款不训练客户数据权重的承诺并不等同零保留；enterprise ZDR 可另行了解。[官方数据说明](https://docs.typesafe.ai/legal)

自审后的建议仍是：先研究 Macro 语义分类，但把“补数据管线”和“实测对照”放在“宣称提速、省钱、提升下注质量”之前。
