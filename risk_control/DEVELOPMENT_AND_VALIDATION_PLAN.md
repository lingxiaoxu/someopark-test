# 独立开发、验证与运行计划

日期：2026-09-17。**本次仅编写/修订文档并进行只读核验。下列目录、函数、命令与任务都是未来实施清单，不能当作已存在能力。** QC/NAV 核验结果及限制见 [专项审计](QC_NAV_RECONCILIATION_AUDIT_20260917.md)。

## 1. 实施边界

所有新代码、配置、依赖环境、采集输入、缓存、结果和日志只在 `risk_control/` 下。生产源只读；不改前端、后端、策略、controller、QC、VolumePrediction、ontology 计划或其他计划。不开启 cron/launchd，不启动服务，不部署 Hosting。

现有仓库有大量其他工作和运行中数据变动；不得 reset/checkout 回滚那些文件。审计本次写入清单及保护文件摘要，不能把运行进程自己更新的数据归咎于本模块，也不能因此宣称全仓字节绝对不变。

## 2. 未来目录结构

~~~text
risk_control/
  README.md
  PORTFOLIO_RISK_CONTROL_PLAN.md
  DATA_CONTRACT_AND_ONTOLOGY_PLAN.md
  AI_HARNESS_AND_GOVERNANCE_PLAN.md
  DEVELOPMENT_AND_VALIDATION_PLAN.md
  RESEARCH_SOURCES_AND_DECISIONS.md
  QC_NAV_RECONCILIATION_AUDIT_20260917.md

  # 以下尚未创建
  pyproject.toml                 # 独立依赖，不安装进生产环境
  .gitignore                    # 仅本目录：忽略私有数据/模型/日志/.venv
  .venv/
  configs/
    sources.yaml                # 源路径、契约、读取方法、价格basis、已审核副作用
    models.yaml                 # 模型窗口/方法，draft/validated 状态
    policies.example.yaml       # 显式未批准，不带“默认生效”交易限额
    scenarios.yaml              # 版本化研究情景与批准状态
    providers.example.yaml      # AI默认关闭，无密钥
  schemas/                      # 本域数据合同；复用本体语义，不建证券主数据
  src/risk_control/
    cli.py
    adapters/
      performance_files.py
      controller_snapshot.py
      qc_account_snapshot.py    # 仅允许只读端点；保留原始 SID、q、账户和部署身份
      target_projection.py     # 本地源→重建/已推/已应用目标；不是实际成交
      historical_shares.py
      identity_projection.py
      market_files.py
      factor_sources.py
      etf_lookthrough.py
      broker_evidence.py        # 无源时明确 unavailable
      ontology_read.py          # 后期接口，不作为初期依赖
    contracts/                  # types、时间、单位、股份/价格basis
    snapshots/                  # manifest、稳定读取、内容存储、原子发布
    checks/                     # 身份/完整性/PIT/对账/隔离/模型资格
    engine/
      official_history.py
      position_valuation.py
      pnl_bridge.py
      position_reconciliation.py # QC 实际仓、target、NAV 投影及归因残差
      factor_models.py
      covariance.py
      risk_contributions.py
      tail_risk.py
      scenarios.py
      liquidity_funding.py
      candidate_analysis.py
    governance/                 # policy评估、finding状态、例外/批准引用
    ai/                         # 独立白名单harness；默认不运行
      providers/
    reports/                    # JSON+Markdown，模板与AI说明分开
  tests/
    fixtures/                   # 脱敏/小型明确样例；私有基准另隔离
    unit/
    contracts/
    integration/
    replay/
  data/
    raw/<digest>/               # 原始证据bytes，不覆盖
    bundles/<bundle_id>/        # manifest引用原始内容
    derived/<run_id>/           # 私有计算表
    projection/risk.duckdb      # 可重建派生索引
  runs/<run_id>/
    manifest.json
    checks.json
    metrics.json
    findings.json
    report.zh.md
    provenance.json
    ai_analysis.json            # 可选，不能覆盖metrics
  proposals/                    # 研究/政策建议，独立于生效政策
  state/                        # 本模块latest指针、finding索引，可重建
  logs/
~~~

不需要 Kafka、常驻图数据库、向量数据库或 Web 服务才能完成第一版。Python 批处理加普通表/Parquet 足够；DuckDB 是可选查询索引，不是新的权威账本。依赖版本锁在本目录，禁止升级现有生产 Python/Node 环境。

## 3. 拟议入口与函数合同

下面是开发目标，不是可立即执行的命令：

~~~text
python -m risk_control capture --as-of ... --knowledge-cutoff ... --mode current
python -m risk_control analyze --bundle <id> --models <version>
python -m risk_control report --run <id> --ai off
python -m risk_control scenario --run <id> --scenario <approved-or-research-id>
python -m risk_control compare --run-a <id> --run-b <id>
python -m risk_control validate --bundle <id>
~~~

| 接口 | 输入 → 输出 | 必须保证 |
|---|---|---|
| capture_bundle | 来源契约、as_of、knowledge_cutoff → immutable bundle | 有界稳定读取、版本/时间/来源齐备；保留 QC 请求时间范围及非原子采样资格；无生产写入 |
| load_official_history | 两 JSON bytes → 日期×六策略权益/PnL | 字段显式映射、共同日期、Master 恒等式 |
| load_qc_actual_positions | 原始 QC portfolio payload + 账户/部署身份 → 实际证券净仓 | 保存完整 SID 和 raw q，检查缺失、有限数和整数约束；不能 int 截断；空仓需完整成功响应证明 |
| project_panel_shares | NAV+同hash结构+冻结参数 → 逐策略逐ISIN展示股份 | 与纯TS helper的golden一致；父子不双计；book_kind=nav_projection，不冒称 QC 实仓 |
| reconcile_position_chain | 源、重建/已推/已应用 target、QC actual、NAV → 逐票数量桥 | 先识别版本/时间与身份，再比较整数；pending_apply 与错仓分开；不放宽0股容限 |
| load_historical_positions | 目标区间+book_kind+证据模式 → 股份/资格/coverage | QC快照/fills重建与NAV/account历史分开；目标决策日不等于成交日 |
| validate_bundle | 全输入及用途 → 检查结果+可运行模块 | 不因一个可选源缺失抹掉全部可用结果 |
| value_positions | qc_actual股份+同步raw报价 → 实际signed value | 主风险用QC观测股数；target/NAV投影另算，标明book_kind；资本仍用两份performance JSON |
| allocate_strategy_exposure | QC净仓+目标/成交归因证据 → 策略分摊及残差 | 目标attribution不是实际子账户；rounding/execution/unidentified残差不强制分摊隐藏 |
| explain_official_pnl | 官方PnL+历史股份/交易/费用 → bridge | 未解释余额保留；不修改官方PnL |
| fit_factor_model | 固定收益/因子/版本 → 系数/误差/资格 | 无泄漏、RF/百分比单位正确、方法可复算 |
| estimate_covariance | 合格收益 → PSD矩阵+健康指标 | 缺失不填0、不无声丢仓、窗口固定 |
| compute_risk | 当前美元向量+模型 → sigma/RC/尾部/情景 | 单位一致、贡献可加、分母可追溯 |
| evaluate_policy | metrics+获准版本 → 状态/原因 | draft不产生“已通过”；unknown不算安全 |
| analyze_candidate | 基准+确定性候选+约束 → 风险/成本比较 | 无生产订单、取整后重检、不可行显式返回 |
| publish_run | 完整run目录 → 独立latest指针 | 检查通过到该用途所需等级；原子更新 |

每个步骤保存 duration、输入/输出 digest、code/config/schema 版本和 outcome。重新运行同输入得到同数值产物；涉及 bootstrap 必须固定 seed、采样规则和软件版本。

股份、账簿和由其派生的风险记录必须带 `book_kind`，区分 `qc_actual`、`exporter_target`、`nav_projection`、`official_performance`；通用价格/因子原始证据不属于其中一本账，该字段可空，不能强贴 official_performance。历史资格另以 `evidence_mode=observed | reconstructed_with_known_rules | restated_normalization` 表示，与账簿种类正交；“历史影子账”不能单列成一种实际账户。只有核验过的实际 QC 账户数量可进入当前实际持仓主风险；NAV/target 可作为独立预期风险视图。实际源缺失时输出 `actual_positions_unverified`，另记录缺失原因及quality=unavailable，保留明确标识的投影分析，不自动替代或混合两本书。QC实际持股是本项目执行账户的数量事实，不凭此宣称它是入金券商账户、已核验购买力或六个独立策略子账户。

未来采集器应维护 `source → rebuilt target → pushed target → applied target → QC observed actual` 证据链，并将 NAV 投影分别接到 target 和 actual 做比较；这不是声称 NAV 从 QC 读取。每次采集绑定 project/deploy 身份和请求起止时间；对期间新成交、版本变动、重部署及分页不完整进行检测。有界重采仍不一致时标 `incoherent`，不假装跨多个只读 API 的结果是原子快照。

## 4. 分阶段开发与退出条件

### RISK-0：合同和冻结样本

工作：确认源字段与副作用；从现有分析中挑选最小完整样本，复制为私有证据包；记录缺口，不复制整个生产数据库或历史仓库。

交付：独立 source contract、input manifest、只读适配器骨架、能力清单。没有任何模型/政策自动生效。

验收：同日六策略合计和跨文件重复字段正确；实际股份能追溯到 QC 原始观测，NAV 与目标投影可分别复算并解释逐票差异；输出范围被技术约束；已有 source bytes 不被写入。旧 /tmp 资料须按 digest核对后归档，否则不得作为长期依赖。

### RISK-1：可相信的事实与对账

工作：官方曲线、QC实际股份、完整目标链和NAV投影、价格basis、历史证据等级、损益桥、ETF不重复穿透；确定性报告先可用。复用既有人工身份映射的语义及来源版本，不改映射文件，也不把旧函数丢失SID或截断数量的行为复制进新契约。

交付：资本/损益、当前敞口、覆盖和异常清单。此阶段能独立回答“数据是否足以算”和“钱从哪里变化”。

验收：已知历史样本复算在数据精度容限内，故意注入缺失/错映射/半更新必须被发现。M4 holdings、target、equity 分开列状态；M5本地自洽通过不能代替QC对账。M4保留的旧终态和later_pass必须保留各自版本/时间；无法恢复准确采样时间时说明限制。不能等高级风险模型做好才处理基础口径。

净值再分 `daily_delta`、`level_bridge`、`cumulative_drift` 和 `open_items` 独立状态。每日ΔD归因通过不能冒称累计绝对净值差已归零；完整公式是 `P−Q = K_effective + 已验证的时点/舍入/其他桥 + residual_level`。level核验须保留锚点未结项、每步归因证据和回冲，不能强制 `D−K_effective=0`，也不能重冻K吸收差额。缺失mirror lag或slippage字段不得按0累加；现金恒等式反推的dividend_timing是推导分类，未必已有逐发行人应收/到账的独立核验，证据等级必须另列。

固定样本中9/14 equity `ok` 表示当日ΔD未归因余额约$0.01符合已有判据；`D−K_effective=$10,205.77`可接前日level余额$747.17、当天dividend_timing $9,466.51、fractional变化−$7.92及约$0.01残差，不能直接宣称漏钱或绝对对账归零。但9/1—9/14的daily状态全部ok仍累计`unattributed_usd=−$1,057.00`；独立level桥扣累计dividend_timing $10,913.81及fractional变化$348.98，剩−$1,057.02（报告字段取整差$0.02）。未来应单列累计漂移/未结项状态，阈值仍由待批准政策决定，不能用每日通过覆盖累计余额。

固定审计样本：2026-09-17 的QC实际44票、NAV45条展示行汇总44票、v45目标44票在本次核验均为0股差。2026-09-16的QC v44只有37票，随后NAV/account_history含有次日才执行的7票目标。两者应分别通过“当前一致”和“历史预期差异”案例，不能把后一例强行修成同日实际股份一致。详细时刻、原始来源与7票明细在专项审计中记录。

本次另对原始295条订单按部署过滤：251条属于当前deploy，均为filled；没有无events的未归属订单。对251条fill events按来源身份去重重建，当前44票及9/14、9/15、9/16、9/17存档收盘股份均为0差。该结果是这次只读账簿核验，不是新风险引擎通过验收，也不保证未来订单永远有events；数据契约和故障测试仍须保留缺事件/在途单情况。

### RISK-2：因子、共同风险和压力

工作：FF5/FF5+Mom、行业模型、收缩/EWMA、贡献、历史与静态股份两种ES、情景库、压力流动性粗估。

交付：一个独立风险包，列明每模块 coverage/eligible status。QC 因子候选接入有自己的资格门，失败不伪造 8/9 月数据。

验收：单位、PSD、贡献和PnL恒等式；同样本模型比较；bootstrap可复现；对样本稀少和模型分歧显示限制。不能通过仅选最低风险模型获得更好的“通过率”。

### RISK-3：持续影子分析

只有另行决定启用调度后才开始。单机读取已完成源，不占用生产任务锁，不调用刷新/训练。

交付：按新输入摘要生成的版本化运行、finding生命周期、数据延迟统计、每日/每周变化报告。已配置政策也仅模拟评估，不影响订单。

验收目标：至少 20 次连续合格或正确降级运行；覆盖正常、收盘、休市、源故障与恢复场景；0 越界写入。收集 20–30 交易日新预测与实际风险对照，可评估运行质量，但不能据此声称尾损预测已经统计充分。

### RISK-4：咨询式方案和 AI

前置：RISK-1/2 稳定；用户提供风险偏好、模型选择及数据外发规则。确定性候选方案可先于 AI 实现。

交付：有成本/上行代价/生存约束的方案比较、可验证AI说明与人工决策证据。

验收：模型不可用不影响数值结果；输出引用匹配；不能自动批准政策或关闭 breach。独立试验与生产模型完全分开。

### 本计划之后的事项

Ontology 查询、聊天/前端展示和交易系统消费属于独立接线项目；不在本次目录开发中顺带完成。自动交易前风控还需券商订单/资金、实时行情与并发预算预留，不能用“影子运行通过”代替执行系统验收。

## 5. 验收案例（必须针对真实错误类型）

| 类别 | 样例与期待结果 |
|---|---|
| 官方资本 | 把 master.combined_equity 当总资本必须失败；漏 BDC/AEUS 必须失败 |
| 文件同步 | 两 JSON 日期不同/一边只写半行：报告 incomplete，不用内部账户补齐 |
| 实际股份解析 | 保存完整QC SID/raw q；非整数、NaN、缺q不得int截断/补零；同显示名不同SID不得覆盖 |
| NAV投影换算 | MRPT/MTFS展示股数不乘scalar；四乘性族按bankersRound；正负x.5边界一致；qc_shares推算字段不得当API实仓 |
| 舍入顺序 | 两展示叶行各0.5股：NAV逐行0+0，exporter先净额再round得1；分别复算且报告差异，真实股份依QC观测 |
| 目标链 | source重建=已推不代表已成交；已推v45/已应用v44标pending_apply；当前QC与匹配版本target逐票0股容限 |
| 非原子读取 | portfolio与orders之间成交或重部署：重采/降级；project、deploy或分页范围冲突不得拼接 |
| 空仓/加载 | loading、错误、缺holdings和完整响应的真空仓必须区分；未知不得变0敞口 |
| 订单完整性 | 没有events的open order仍保留；缺事件/订单页不证明无在途订单；撤单请求未确认继续计潜在风险 |
| 历史执行 | 9/16 QC v44的37票不能替换成9/16影子账的v45目标44票；7票9/17成交前不得进入actual历史 |
| 部署连续性 | deploy_id变更必须新执行账户段；没有有据转移桥不能沿用旧现金/持仓/成交历史 |
| 对账状态 | M5绿色不证明QC一致；M4股份通过/净值pending分开；保留终态不遮蔽later_pass的新版本等待 |
| 日增量/绝对桥 | 9/14当日未归因约$0.01通过，但D−K仍$10,205.77；从锚点及所有桥重建level，不强逼D−K为0 |
| 累计漂移 | 多天daily ok但累计unattributed约−$1,057仍须独立呈现；字段取整误差单列，不重冻K或自动调现金吸收 |
| 未结项资格 | dividend_timing反推额不是逐发行人已验证应收；open_items保留证据/账龄/回冲；缺lag/slip字段不得补0 |
| 父子结构 | 一票在pair/strategy/portfolio多层出现：按叶路径正确汇总；真实重复pair敞口不丢 |
| 身份 | QC ARNC/HWM与市场ARNC不能混；成功取价但ISIN错误要阻断 |
| 价格 | adjusted Close×raw股数被拒绝；拆股跨日价值连续；分红不双计 |
| 官方份额桥 | UI fractional与integer不同：单列桥，不改持股逼平 |
| 策略归因 | 同票多策略/相反方向/部分成交保留目标与actual差异；分摊加rounding/execution/unidentified桥回到账户净仓 |
| 时间 | available_at晚于cutoff或未知：严格PIT失败；研究解释可用但带明确模式 |
| 冻结 | AEUS 9/1使用9/2 scalar：restated；不能称当时已批准规模 |
| 日历 | 休市日非零账本变动合并到后续交易收盘；累计权益变化不丢失 |
| 历史PnL | 当天开平仓：不能用当天收盘股数乘全天收益；缺交易就留residual |
| 假昨收 | day_state重建basis不得当EOD价格；分钟流读全schema分段 |
| 因子 | percent→decimal只转换一次；RF区别；QC Value=HML而非总因子；缺值sentinel不能为0 |
| 因子版本 | 官方与重建行独立标记；后补vintage不回写成当时已知 |
| 风险数学 | variance贡献和=1，vol贡献和=sigma；同票跨策略抵消仍按各x_s直接分摊；零风险分母返回undefined |
| 因子重构 | 同样本OLS及相容协方差fixture重构样本值；收缩/异窗预测只检查自身BFB′+Ω，不逼回原样本 |
| 资本/期限 | 官方资本≤0时只保留美元量和资本异常，百分比指标不可用；日度/年化或期限不匹配不得比较 |
| 尾部 | 精确2.5%概率质量、边界权重、损失符号、ties和小样本告警 |
| 协方差 | 缺证券不静默删除；对称/PSD/维度/顺序检查；病态逆矩阵拒绝或用声明方法 |
| 穿透 | ETF持仓+ETF整体收益不双计；未知权重不强制归一到100% |
| 融资 | 经纪商条件缺失返回unknown，不能用102%账本假设当真实保证金 |
| 潜在订单 | 部分成交/撤单未确认保持风险；相反方向挂单不当确定对冲 |
| 候选方案 | 连续解取整后重新超限即拒绝；不可行不放宽约束 |
| 事件状态 | 心跳恢复但原价仍旧：不得resolved；acknowledged不是resolved |
| AI | 伪造数值/引用、新闻指令、超时：拒绝AI段，数值报告照常 |
| 隔离 | symlink逃逸、..、临时文件、cache、pyc、日志：不能写出本目录 |

金额检查按源精度设容限，例如每个已四舍五入至分的项允许其半分舍入误差，合计按项数推导上界，并加浮点数值误差；不统一用任意 $1 容差掩盖小差异。股份检查按该字段的整数/小数合同。生产风险阈值与实现的舍入容限是两件事。

现金梯子测试还须包括期中资金负值但期末为正、卖出后结算延迟、解押延迟、贷款到期、已到账应收和费用重复计入。策略层gross与券商账户净额不等同，缺account/netting证据时必须保留融资未知。

已有研究 60 个策略日剩余误差最大约 $0.00763，是本地官方/影子账股份解释样本，可作为该口径的固定复算参考；不是QC实际历史已完整核验的证明，也不是所有未来方法必须盲目通过的容差。SSRS 已知不一致样本应该产生预期 finding。

## 6. 隔离的技术实现要求

路径白名单须 realpath 后校验，不仅字符串前缀；输出 symlink 不能指向源目录。最好在只读挂载生产输入的隔离进程中运行，只有 risk_control 输出目录可写。若宿主无法提供只读挂载，须把需要的文件先复制进输入包，计算进程只暴露副本，并用文件访问审计验证。

Python 禁止在生产源码生成 __pycache__；不导入会写缓存、注册表或账本的生产入口。复制/复用纯数学语义时记录来源 SHA；NAV投影用TS parity fixtures，target投影用exporter纯函数fixtures，actual数量用原始QC payload检验，不能用面板一致性替代实际仓验证。不要求改共享 TS 文件。

QC采集只允许项目识别及账户/持仓/订单/日志读取等经审计端点；禁止export、ObjectStore写入、部署、重启、清仓、下单、freeze或settle写回。不要运行生产对账命令来“顺便修复”旧报告。已有M4报告按bytes复制读取，新增采样及审计只写本模块证据目录。

本地采集阶段默认不加载整份生产环境变量。外部数据或AI适配器只获得所需最小凭据，日志屏蔽头部、token、签名URL。无网络的核心计算可在同bundle离线重放。

并发初始限制为一个重型分析任务；进程锁只在 risk_control 内。资源预算先测量CPU/内存/磁盘，不预设远端空闲；不与策略训练共用 GPU 服务。压测生产源应避免全目录重复扫描。

私有输入与报告未来必须由本目录 .gitignore 排除；本次只有规划文档，没有复制账户数据包。开源另起干净仓库，不把风险快照或API原件直接发布。

## 7. 失败、修订与发布

- run 状态：running / completed / degraded / failed；module status 独立。进程 exit 0 不等于所有模块通过。
- 完整产物先写本目录 staging；校验后 rename 发布。跨网络拉取不承诺全局事务，通过输入包隔离部分失败。
- latest 是指向一个不可变 run 的指针，不覆盖旧 report。新失败不抹掉上次结果，上次结果需显示其原时点/过期状态。
- 输入修订产生新bundle；记录 supersedes 和差异，不改旧证据、旧批准或旧预测。
- 原文件丢失但证据包仍在可复算；只有摘要在则仅可解释历史摘要，reproducible=false。
- 重试次数/时限有界，attempt不覆盖；运行中断不会触发策略重启、账户重放或数据重建。

## 8. 性能和质量衡量

系统成功不以“生成了多少张图”衡量。跟踪：资本/股份覆盖、可解释PnL比例及残差、事实新鲜度、身份冲突、预测风险误差、尾部漏报、告警重复率、调查耗时、方案实施可行性、运行成本。

必须给每项指标分母和观察期。小样本的 VaR 越界数不具备独立校准结论；持续记录风险预测生成时间，等实际结果成熟后再评估。即使不讨论策略回测，分析系统本身仍需数据、计算和故障行为验证。

## 9. 本次文档交付检查

本次包含 Markdown 相互链接、实际来源路径、设计一致性、保护文件摘要及写入范围检查，以及专项记录的QC/NAV只读采样、数量对比和纯函数核验；没有运行尚未开发的风险引擎测试，也没有将过去研究测试冒称成新模块验收。后续阶段完成与否按各阶段实际证据记录。

本次已完成：

- 文档清单现为七份 Markdown；新增QC/NAV专项审计。目录中没有实现代码或运行配置，交付前对七份链接及围栏统一检查。
- 独立只读复核覆盖数据合同、ontology/AI 边界、风险方法及QC实际持仓链；此次纠正NAV主数量权威、先逐行取整就等于QC、历史影子账等于同日执行仓等错误。
- 初版已核对ontology原计划、SPAC原计划、NAV共享计算/官方读取器、RiskManager、QC人工映射代码和根别名表的保护摘要；本次修订仍只改risk_control文档，最新检查证据见专项审计。
- 本次新增/修订的持久文件范围为risk_control七份文档，临时采样和核验材料在/tmp；未实现或启动风险引擎、服务、模型、训练或交易任务。
