# 研究依据、已有证据与设计裁决

日期：2026-09-17。仅研究与文档。本文件区分公开机构事实、仓库已核验事实、既有研究快照、我们拟议的实现，不将其混写。

## 1. 公开专业资料及本项目采用方式

### Citadel：风险职能独立于策略

[Citadel 官方业务说明](https://www.citadel.com/what-we-do/)描述 PCRG 独立于投资团队，持续监控暴露和更新压力测试。我们借鉴职责分离：策略不通过修改自己的报告来豁免 Master 风险；风险输入、规则和分析单独可审计。该页面未披露可照抄的私有模型和限额，本计划不声称复刻 Citadel。

### AQR：真正有用的是完整风险系统而非单一 risk parity

[AQR Style Premia Trust PDS](https://australia.aqr.com/-/media/Australia-Documents/Basic-Information/Basic-Information---Style-Premia-Trust/PDS.pdf)第 26–27 页讨论独立风险团队、分析平台，以及不同频率的暴露、杠杆、市场流动性、融资流动性、自由现金和清算能力监控。我们据此将损失、生存、退出预算并列，并按数据能力安排刷新。它是特定基金披露，不是 AQR 全产品的一套统一限额。

[AQR Total Portfolio Approach，2026-05-19](https://www.aqr.com/Insights/Research/Alternative-Thinking/Total-Portfolio-Approach)讨论整体组合视角及放松传统约束的风险。我们的设计推论是：穿透共同经济风险，同时保留策略职责和独立保护约束；整体优化不等于取消所有边界。

[AQR 关于大回撤中分散化的研究](https://www.aqr.com/Insights/Research/Alternative-Thinking/It-Was-the-Worst-of-Times-Diversification-During-a-Century-of-Drawdowns)区分低相关资产与真正具有危机防御特性的配置，并指出保护具有回报代价。对应本模块：比较正常相关性、尾部共损和不利/有利情景，不承诺无成本保留全部上涨。

### Optiver、HRT、Tower：行情风险和交易/运营风险是不同控制面

[Optiver 官方风险框架](https://www.optiver.com/what-we-do/control-risk/)包含暴露、压力、限额、交易前控制、对账和事件管理。对应本模块将数据健康与市场风险拆开，heartbeat 正常不等于组合风险正常。

[HRT 给 SEC 的技术与交易意见函](https://www.hudsonrivertrading.com/wp-content/uploads/2024/08/SECtechnologyAndTradingRoundtable.pdf)强调未成交订单的潜在风险和独立回报核验。文件是 2012 年信函，URL 的 2024 是存放路径；不能当作其今日私有实现。我们由此要求未来分析同侧未成交/取消待确认风险，不假设相反挂单同时成交。

[Tower Research Capital Europe 2024 披露](https://tower-research.com/wp-content/uploads/2025/09/TRCE_Limited_Pillar3_2024_Final.pdf)说明风险偏好、正式限额审查、独立监控和压力机制。我们采用版本化政策/例外与可追溯升级；不引用未公开的阈值，也不将欧洲实体披露推广成整个公司的完整体系。

### MSCI、BIS：模型误差和尾部风险不能藏在一个点估计里

[MSCI Equity Factor Models](https://www.msci.com/data-and-analytics/factor-investing/equity-factor-models)涵盖风格、行业及宏观等风险视角，支持五因子之外仍需经济暴露层的设计。

[MSCI 关于风险模型错误的研究](https://www.msci.com/documents/10199/8426a871-688b-4705-ae65-aee7c4c9a0c5)讨论估计噪声、非平稳和优化对模型误差的利用。我们采用多模型挑战、固定候选和成本/融资约束，不把样本内最小波动当最终建议。

[BIS 市场风险标准说明](https://www.bis.org/publications/basel-framework-standard/d457-inbrief.pdf)提供 ES、流动性期限和难以建模风险的纪律参考。本项目不是银行监管资本系统，不宣称“Basel 合规”，不照搬银行资本权重。

### 因子来源

[Kenneth French Data Library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html)用于 FF5/Momentum 的标准定义与官方历史版本。9/17 重新下载并核验 CSV/TXT 后，实际文件仍截止 7/31；这不是通过月份推断出的结论。

[QuantConnect Fama French 数据文档](https://www.quantconnect.com/docs/v2/writing-algorithms/datasets/quantconnect/fama-french)说明每日重建、不同底层数据及 `IsEstimate`。它提供及时数据的候选路径，不等于本项目已取得 8/9 月记录。开发须验证实际返回、单位、知识时间和估计/官方差异。因子对象的 `Value` 对应 HML，不能当作整个因子向量。

### AI 风险

[NIST Generative AI Profile](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf)讨论虚构事实/引文和过度依赖等风险。具体 metric-ref 渲染、白名单工具和生产隔离是本项目设计，不声称 NIST 已认证该方案。

## 2. 仓库阅读与兼容依据

重点已读：

- `.claude/plan/systemic-strategies-plan/ONTOLOGY_CONTROL_PLANE_PLAN.md`，尤其 III.1–III.9、IV.1–IV.7、VII 和附录 C。
- `.claude/plan/systemic-strategies-plan/AI_DYNAMIC_ALLOCATION_PLAN.md` 的配置/内部 regime 边界。
- `.claude/plan/strategies-plan/RISK_MANAGEMENT_MODULE_PLAN.md` 的既有报告与执行端区分；再以现行 RiskManager.py 核验实际范围。
- `someo-park-investment-management/shared/realtimeNav.ts` 与 `server/utils/{officialEquity,controllerNavData}.ts`。
- `controller/{registry,model,engine_flatten,scheduler,reconcile_eod}.py`，现有 registry、结构和 NAV 数据。
- `trading_quantconnect/{qc_api,inventory_source}.py`、`lean/main.py`、`ops/rolloff.py`、`state/{exporter_state,target_portfolio}.json` 与 reconcile 相关代码；9/17 追加直接读取 QC live/portfolio/orders/logs。
- 实际官方 JSON、各域账户历史、价格存储与历史分析证据。

原 ontology 计划此次读取摘要：

~~~text
SHA256 d0eb89a2c3afdac4bca30e4d906ced7d1c7b2d6a62719a71e0c0729ed8117b39
~~~

原计划是 PLAN ONLY。统一本体实现未发现；registry 身份体系已经存在。这是两种不同事实，不能以其一推断另一。所有原计划未修改。

## 3. 既有量化研究如何转成设计需求

以下数值来自 **2026-09-17 18:40 UTC 附近取得的固定研究快照**，不是本次文档撰写结束时的新实时结果。官方资本截至 9/16；当时 NAV 行情标注 15 分钟延迟。原股份输入属于 NAV 投影；本次晚间 QC 核验确认当下 v45 的股份与 NAV 一致，但没有重跑这些价格/模型数字，也不能据此将旧历史投影重新命名为 QC 实仓。

| 研究观察 | 设计要求 |
|---|---|
| 六策略官方 Master $6,827,322.95，逐项相加一致 | 资本基准自动对账，不接受内部 $1m 替代 |
| AISS 官方资本约占 41.25%，某当前股份模型方差贡献约 82.69% | 同时报资本权重、绝对风险和模型依赖，不能只按策略资金分散 |
| 穿透后半导体主题约 45.40% Master | 策略/ETF/配对共同经济暴露与事件压力 |
| MTFS gross/官方策略资本约 5.08 倍 | 区分官方策略尺度杠杆和券商融资；两条约束分别检查 |
| 面板整数股价值与其未取整估值有约 $220 有符号桥 | 股份和金额含义分别保存，不能把舍入差当漏仓 |
| 官方历史 222 行但交易日风险为 211 个收益观测 | 日历/休市账本变动转换有显式规则 |
| 六策略 lagged 官方账本/NAV 尺度股份共同收益样本仅 10 日 | 不把这份覆盖当作 QC 历史实际执行覆盖 |
| 60 个官方账本策略日损益桥最大剩余约 $0.00763 | 固定基准复算；不推定 QC 账户或所有旧日/未来日都成立 |
| 官方 FF5 到 7/31，9月价格模型可到 9/16 | 因子版本与最新价格模型分开；不伪造补月 |

这些观察驱动优先级，但不生成默认减仓指令。收益集中也可能意味着有效专长，风险负责人的工作是把共同损失、预算和代价讲清楚，而不是自动把每个策略配成等风险。

## 4. 旧研究证据的可追溯位置与限制

已有独立目录：`/tmp/portfolio_professional_risk_20260917/`。主要文件：

- `input_manifest.json`：原输入路径、SHA 与日期。
- `panel_capture.json`、`current_panel_shares.csv`、`historical_panel_shares.csv`、`historical_share_coverage.json`。
- `risk_results.json`、`risk_extensions.json`、`ff5_results.json`、`current_ff5_results.json`。
- `historical_holdings_pnl_by_strategy_day.csv`、`ssga_lookthrough.json`、`validation.json`。

因子可用性重查在 `/tmp/ff5_availability_recheck_20260917/`。追加 QC 审计在 `/tmp/risk_qc_nav_audit_20260917/`，包含两次 QC 原始响应、全量订单、日志、冻结本地输入及 `verification.json`。这些是研究参考，**未来模块不能依赖 /tmp 长期存在**；RISK-0 应逐件校验并归档必要证据到自己的私有输入区。risk_control 本次只留 Markdown 证据摘要，未复制账户数据包，也未把旧脚本当生产实现。

旧官方文件版本：

~~~text
strategy_performance.json
8156d53176b0c1b34f0ed6de465a6cd48d60e6ffc41442a3193442178a5a90c1

master_portfolio_performance.json
96aa5ecec401934e939b28adc693bd13c161fed825683fb392b662703eb3e2c5
~~~

旧验证通过只适用于对应输入。本文引用研究结论，不声称重跑了全部历史回归或新引擎已经通过测试。

## 5. 设计裁决记录

| 编号 | 采用 | 没有采用及原因 |
|---|---|---|
| RC-01 | 新根目录 risk_control，A0/A1 独立研究 | 不扩写旧 RiskManager/前端，避免影响生产与旧口径 |
| RC-02 | 两官方 JSON 资本 + QC 已执行股份；NAV/目标独立核对 | 不用内部 $1m/QC equity 替代资本分母，不把 NAV 推导值冒充 QC 实仓 |
| RC-03 | 三视图并列 | 不把历史PnL、当前股份回放、账户融资能力合为一个分数 |
| RC-04 | FF5解释+行业/残差/压力 | 不用五因子取代单票和融资风险 |
| RC-05 | 官方/估计因子分轨 | 不静默补值或把7月模型标作9月已更新 |
| RC-06 | 明确证据时间与历史等级 | 不把后验缩放历史伪装实时PIT |
| RC-07 | 复用身份、本体兼容派生记录 | 不建第二套证券真值、不假设ontology API已存在 |
| RC-08 | 政策为未批准草案 | 不把示例阈值直接上线、不承诺回撤硬上限 |
| RC-09 | 确定性候选+成本/上行取舍 | 不以无约束最小波动对冲给交易建议 |
| RC-10 | AI解释和调查、用户选模型 | 不让模型改数值规则、不先采购或部署模型 |
| RC-11 | 单机批处理起步 | 不建低延迟交易平台；升级依据实际瓶颈 |
| RC-12 | 数据质量与风险政策独立状态 | 不把缺失数据、空结果或未配置规则标绿色 |
| RC-13 | 账簿种类与证据等级分离；QC 份额、目标链、日增量/总额净值桥分开 | 不用 observed NAV 冒充 observed QC；不以局部对账通过宣布全部核验成功 |
| RC-14 | 实仓总风险 + 有证据的策略分配 + 未归因残差 | 不从 QC 总净仓无损反推虚拟策略仓，也不强行把差额摊到策略 |

### 5.1 9/17 晚间实测后的文档修订

原稿将 panel_shares 作为风险量的最终权威，按用户本次澄清已纠正：实际股份为 QC，NAV 作为投影和核对对象；两个 performance JSON 的资本权威不变。

QC 20:33–20:36 EDT 的两次返回持仓稳定，44 个证券；NAV 45 展示行合并后 44 个证券、目标 v45 亦 44 个证券，全部零股差。本部署 251 个去重 fill events 重建当前及 9/14–17 四份 QC 存档数量也全部一致。9/16 本地账本已含 7 票次日才成交的新头寸，因此原历史面板研究不能充当 QC 执行历史。QC 返回环境为 `PaperBrokerage`，这是该账户已执行持仓证据，不能据此证明外部真钱券商持仓或保证金额度。

没有发现当前少仓、多仓或 1 股差异。发现的设计/接口风险包括逐行舍入与目标净额后舍入不交换、QC 辅助解析丢完整 SID/截断 quantity、无事件订单可能被历史筛选器忽略、M5 面板绿色不代表 M4 QC 验证。均已加入新模块契约及验收，未改任何现有运行代码。净值 pending 和证据边界详见 [QC/NAV 专项审计](QC_NAV_RECONCILIATION_AUDIT_20260917.md)。

追加逐日累计检查：9/1–9/14 的日检查均 ok，但累计未归因约 −$1,057；当前规则的“每日在容差内”不能推导成“累计已经完全解释”。新设计加入未结项/累计漂移监控，同时区分 cash bridge 推导的股息分类与独立核验的应收股息，不通过改 K 抹平。

## 6. 尚需补证但不阻止开始的事项

- 真实经纪商购买力、保证金、haircut 与借券；QC 一次性全量订单读取本次已成功，但持续订单完整性/事件时间/部署衔接仍需新模块实施验收。
- QC 重建因子的实际可取得范围、价格/许可和可追溯历史版本。
- 更完整的历史股份/逐日交易时间证据；尤其账户历史文件的 available_at。
- ETF/BDC穿透历史的有效日期与发布时点，事件/供应链关系的可靠来源。
- 风险偏好、约束期限与例外流程；AI模型及外发政策。

这些缺口在报告中保留为未满足能力。基础官方损益、股份暴露和条件压力仍可先独立实施，不需要通过修改生产系统来填补它们。
