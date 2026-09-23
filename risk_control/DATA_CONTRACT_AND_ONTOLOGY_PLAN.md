# 数据合同、身份与 ontology 兼容计划

日期：2026-09-17。本文件区分已读到的代码/数据与未来契约。以下新类型和接口均是拟议，未注册、未实现。

## 1. 数据权威与来源映射

路径相对 someopark-test 根目录。适配器未来只在 risk_control 内实现，源文件保持原样。

| 目的 | 已有路径/字段 | 本模块用途 |
|---|---|---|
| MRPT/MTFS 官方资本、收益 | `someo-park-investment-management/public/data/strategy_performance.json`；`date, mrpt_equity, mtfs_equity, mrpt_pnl, mtfs_pnl` | 权威历史与资本；与 Master 同日交叉核验 |
| 六策略和 Master | `someo-park-investment-management/public/data/master_portfolio_performance.json`；`mrpt/mtfs/sr/aiss/aeus/bdc_equity`、对应 `_pnl`、`master_equity/master_pnl` | 六策略权威总量；SSRS 字段前缀是 `sr` |
| QC 当前已执行股份 | `trading_quantconnect/qc_api.py::QcClient` 对 `live/read`、`live/portfolio/read` 的只读返回 | 实际股份权威；保留项目、部署、环境、完整 SID、原始 quantity 与读取时间 |
| QC 订单、成交和应用版本 | `live/orders/read`、`live/logs/read` 原始返回；`trading_quantconnect/lean/main.py` 的 applied/converged 日志语义 | 未成交风险、实际股份重建及已应用目标版本；日志不是独立持仓权威 |
| 已推目标与归因 | `trading_quantconnect/state/target_portfolio.json` 的 `version/content_hash/targets/attribution`；`inventory_source.py::build_target` | 当前六源重算目标→已推目标→QC 已应用版本→实际股份的核验链 |
| NAV 当前结构与股份 | `controller/output/nav_latest.json` | 展示投影、节点、结构引用、发布与行情质量；不是 QC API 实仓 |
| 结构证据 | `controller/output/structure_snapshot_{structure_hash}.json` | `hash, ts, nodes, sources`；节点 `children/attrs`，来源摘要 |
| 历史 NAV 流 | `controller/output/nav_stream_YYYYMMDD.csv` 及 `.csv.v1/.v2…` | 用各分段表头解析、按 structure_hash 绑定结构与时点 |
| 冻结资本转换 | `trading_quantconnect/state/exporter_state.json` | `scalars, scalar_basis, onboard_log`，不可重算或写回 |
| 稳定身份 | `controller/registry/node_registry.json`、`security_master.json` | 非叶 SPID、证券 ISIN/占位身份、来源 ticker |
| 市场改名 | `ticker_aliases.json`、`ticker_aliases.py` | 日期有效的市场身份映射；只读规则与版本 |
| QC 专用显示名 | `trading_quantconnect/ops/rolloff.py::QC_SYMBOL_ALIAS` | 仅 QC 来源的身份桥；保留完整 SID 证据 |
| Controller 对账 | `controller/output/reconcile_YYYY-MM-DD.json` | 对账方法、日期、覆盖、缺价；不替代官方资本 |
| QC 对账与历史实仓 | `trading_quantconnect/reconcile/qc_reconcile_YYYY-MM-DD.json` 的 `close_snapshot.qc.shares/deploy_id`、`taken_at/fills/scalars`；同目录 `{qc_reconcile,ledger_history,official_close}.py` | 历史 QC 观测、现金/价格/持股差异；股份、目标、净值分项状态和 later_pass 都要读取 |
| 历史价格 | `price_data/index.json` 指向的 parquet；`price_data/{semi_strategy,elec_strategy}/prices/`、`price_data/sector_etfs/polygon/` | 直接只读文件；逐源核实价格 basis |
| 公司行动 | `price_data/dividends_cache.json`、`price_data/splits_cache.json` | 生效日、金额、拆股转换及版本 |
| ETF / BDC 穿透 | 已核验官方基金持仓文件；`price_data/bdc_holdings/latest_manifest.json`、`bdc_results/bdc_deal_start.csv` | 非实时穿透与事件集中度；不能替代市场估值 |

现有 `server/utils/officialEquity.ts::readOfficialEquity()` 的 BDC 读的是第三份 `private_credit_bdc_performance.json`。因此本模块**不能直接把该读取器当资本权威**：BDC 以用户指定 Master 中的 `bdc_equity` 为准，第三文件只作同步差异证据。

两份 JSON 的 `combined_equity` 不同：strategy 文件=MRPT+MTFS，master 文件=五策略、不含 BDC。任何总组合查询都必须显式选 `master_equity`。禁止通过模糊名称自动选列。

## 2. 统一快照而不是拼接“各自最新”

### 2.1 拟议采集算法

1. 请求必须给 `as_of`、`knowledge_cutoff`、用途（当前诊断/历史解释/严格历史可知）和来源配置版本。
2. 找到不晚于目标、两官方文件均完整的同一日期，校验六策略齐备及合计。较新但半更新的数据单独报告，不混入已完成快照。
3. 读取 QC live identity/portfolio，保存完整响应；读取 NAV bytes、匹配 structure_hash 的结构、六源目标输入、已推目标、镜像参数和身份文件。再读订单全页、应用日志及第二次 QC live/portfolio，记录请求起止时间。
4. 对可变文件采集前后校验 bytes/hash/大小、目标版本、部署和股份，检查引用关系；变动时有界重试，持续变化则 `inconsistent_snapshot`。前后相同只证明这些采样稳定，不保证中间没有 round-trip 成交；有完整成交时间覆盖时才能进一步证明持仓路径。不能锁住生产写入或要求暂停策略。
5. 复制所需原始 bytes 到本模块专属输入包，计算 SHA256；此后计算只读这个包。生产原件后来变化不改变本次结果。
6. 数据稳定读取不等于同一业务时间。另校验日期、持仓时间、QC 已应用与目标已推版本、结构与冻结参数适用范围、报价时间及 known_at。盘后新目标未应用标 `pending_apply`，保留当前 QC 实仓；不可拿相同 `positions_as_of` 推断已成交。无法证明全仓一致时显式 `coherence=partial`。
7. manifest 完整落盘后才开始计算；中断的包保留失败状态，不能成为 latest。

选取较早完整官方锚必须显示 `anchor_date`、候选最新日期和滞后，不冒称今日收盘。数据不足时不改用内部账户/QC 净值。过期结果可供历史查阅，但必须带 stale 和原日期，不作为当前合格结果。

### 2.2 新数据记录共同字段

~~~yaml
# 字段提案，非现有源格式
schema_version: risk-control/v1
source_namespace: controller | performance | qc | french | vendor
book_kind: qc_actual | exporter_target | nav_projection | official_performance  # 股份/账簿指标必填；通用价格/因子输入可空
evidence_mode: observed | reconstructed_with_known_rules | restated_normalization
account_ref: QC project/account identity，非QC来源可空
deployment_ref: QC deployId，非QC来源可空
execution_environment: 原源环境标识，非交易来源可空
source_record_key: 原系统键或文件内定位
entity_ref: 已有 SPID 或 ISIN，未知留空并有原因
event_time: 业务发生时间，可空
as_of: 被描述的业务时点
available_at: 该版本首次可用时间，可空
available_at_basis: source_reported | captured | unknown
read_at: 本次读取时间
captured_at: 本次冻结输入时间
content_digest: sha256 原始 bytes
source_locator: 文件相对路径或获准 URL，无凭据
source_version: 原版本标识，可空
unit: USD | shares | decimal_return | percent | bps
price_basis: raw | split_adjusted | total_return | not_applicable | unknown
quality: complete | partial | stale | unavailable
limitations: []
~~~

源没有 available_at 时不得用文件名日期补造。首次本模块采集可证明“此刻已知”，不能证明更早已知。mtime 可以帮助检测文件变化，但不是金融知识时间。

未来时间检查要区分预测的 target_time 与事实 available_at；预测未来目标本身不是泄漏。所有时区用带 offset 的 UTC 记录，交易日按 America/New_York 和交易所日历判断，不能用 UTC 日期代替美东交易日。

## 3. 股份合同：QC 实际股份与 NAV 投影分别核验

### 3.1 实际股份权威与五段核验链

`qc_actual` 使用 `live/portfolio/read` 的已执行 quantity，保留完整 SID 和原始值。先核查字段存在、数值有限、整股合同、身份唯一及部署，再计算；不要沿用 `rolloff.qc_snapshot()` 的 `int(q)` 与首 token 覆盖字典来承担新模块的审计合同。当前 44 票均整股，不代表将来的非整数可以静默截断。

本地六源重算目标 → 已推 `target_portfolio` → QC 日志的已应用/收敛版本 → QC API 实仓 → NAV 展示，是五个独立检查点。前三者仍是目标/版本证据，只有 API 和经锚点核验的成交记录证明实际执行股份。若源更新而 exporter 未推送，第二段失败；若目标已推但等待开盘，第三段 pending；收敛到对应版本后，逐票目标与 QC 整数股份差异必须为零。

QC 实仓始终可用于当前账户风险，即使它与新目标不一致；目标差异另作执行进度和潜在风险。QC 不可读则 `actual_positions_unverified`，允许另出有明确标题的目标/NAV 研究，禁止静默替代实际风险视图。

QC 是跨策略净額账户。策略/pair 的归属是派生层，需要版本化 `attribution` 和执行记录；不能从净仓单独证明六个策略各自的实仓。保留 allocation/rounding/unallocated 桥，所有归因和残差合计必须等于 QC 实仓。每次运行只在已确认的账户、部署和净额范围内合并。

### 3.2 NAV 展示规则与股份差异桥

已核对纯计算文件 `someo-park-investment-management/shared/realtimeNav.ts`：`bankersRound`、`holdingsPresentation`、`officialAnchor`、`buildRealtimeNavPanel`。

| 策略族 | 股份 | 官方权益转换 | 限制 |
|---|---|---|---|
| MRPT / MTFS | NAV 原始股份，不乘 QC scalar | ledger value − C | C 来自 frozen basis，股数与权益转换不同 |
| SSRS / AISS / AEUS / BDC | bankersRound(raw shares × frozen k) | ledger value × k | 每策略冻结版本独立；BDC 当前 k=1 不硬编码 |

C = `scalar_basis.ledger[st] − scalar_basis.official[st]`。现有代码没有独立 `capital_base.json`，不要发明来源。小写 k 是乘数；rolloff 的 `k_equity` 是加性净值桥 K，不可混用。

半整数向偶数舍入包括负股份；不能改用 JS Math.round。已有 TS 纯函数仅是 **NAV 展示投影**的金标准，在固定 fixtures 中验 Python 实现；不是 QC 实仓的金标准。实际采用哪个文件版本由 SHA 锁定，生产助手变化不会无声改变历史计算。字段 `holdingsPresentation.qc_shares` 也是本地根据 cohort/scalar 推导，未查询 QC；新模块称为 `nav_computed_mirror_shares`，避免误认。

UI 的市值仍可能基于未舍入 raw×k。以同一价格单独计算实际与投影：

~~~text
actual_risk_value = qc_actual_shares × qualified_raw_quote
nav_projection_value = panel_shares × qualified_raw_quote
display_rounding_bridge = ui_fractional_value − nav_projection_value
actual_vs_nav_share_difference = qc_actual_shares − panel_shares
~~~

输出逐行和汇总桥，不能反向调股数使金额一致。`display_rounding_bridge` 只有 UI 估值与 raw_quote 同价同时点才全是舍入差；否则再拆价格/时间桥。MRPT/MTFS 当前源为整数；未来出现非整数原股份时按源合同保留，不擅自截断或套乘性族舍入。

### 3.3 防止父子重复计算，并保留不同舍入顺序

结构 `nodes` 为按 ID 索引的对象，子边是 `[child_id, weight]`。沿指定 strategy 根递归到证券叶，只遍历一次经济路径，检测循环和非法重复边；多路径到同一证券可有真实多份敞口，应按路径权重求和，不能简单去重丢仓位。

父策略的 holdings 若已包含子节点展开，不再和孩子 holdings 累加。保留 pair/subsector 路径用于归因。**计算 NAV 投影时，先按实际展示每条叶行应用 k 和 bankersRound，再按策略/ISIN汇总。计算 exporter 目标时，复用其先跨策略汇总同票小数股份、最后 round 的规则。** 舍入与求和不交换：两条各 0.5 股，NAV 可能为 0+0，exporter 目标为 1。不能因当前对齐就宣称构造上永远相等；差异列为 `display_rounding_difference`，不调整 QC 实仓强行对齐 UI。Controller 发布前将原股数保留四位小数也进入来源精度证据。展示行 identity 至少含结构 hash、策略、原始子路径和 ISIN。

9/17 当前 NAV 45 行合并为 44 票；MTFS 的 VLO 为两行各 660 股，汇总 1,320 股，不能去重成 660 或把父节点再加一次。SSRS 的 NAV 源是 `account_ssrs.positions`，exporter 源是 `inventory_sector_rotation.holdings`；本次逐票一致，未来必须显式对拍，不能假定同源。

历史 account 若只有聚合 positions、缺少当时的展示子路径，不能宣称精确还原逐行整数股份。需要对应结构证据；否则标 reconstructed/restated、说明行切分未知及可能舍入差。策略分配gross、证券经济净额gross与有证据的账户融资gross分别保存。

`controller/output/risk_matrix_latest.json` 来自 `controller/engine_flatten.py`，实际是证券到各层节点的原始股份反向索引。它**不是协方差**，包含父子层重复，未统一官方 k，也没有独立的时间/hash envelope；仅作结构诊断，不作为统计模型输入或可直接求和的持仓表。

### 3.4 报价与 NAV 质量

`nav_latest.ts` 是发布时间；`feed_delay_min` 不证明每只证券恰好同一报价时间。至少检查 stale、missing、backfilled、tick_error、market_degraded、rebuild_error、positions_as_of、structure_hash；保留价格时间是否推断的标记。

若只有 h.value/h.shares 能推导估值价格，标 `price_source=nav_implied` 并核实 raw 口径；零股、缺值或金额混合不能这样推导。将来证券原始 quote 能用时逐票记录，不能把发布时点作为成交价时点。

面板 `navQuality` 的 pass 不是本模块所有质量检查通过。`controllerNavData.readNavReconcile()` 读取的是 Controller 本地 M5 对账，不是 QC M4；绿色不能代替 QC 当前股份、已应用版本或账户净值核验。已知延迟仍能做延迟诊断，不满足实时交易前校验。

## 4. 历史股份、冻结、对账

### 4.1 已有历史源

实际股份优先：`trading_quantconnect/reconcile/qc_reconcile_YYYY-MM-DD.json::close_snapshot.qc.shares`，同时绑定 `taken_at`、`deploy_id`、fills/scalars；历史存档未保留完整 SID 的限制不能隐藏。其次用同部署完整、去重的 fill events 加起点/公司行动重建，并以多个 QC 快照锚点验证。禁止跨部署假定初始零仓或把缺失日期默认空仓。

以下是官方账本/NAV 投影历史源，并非自动等于 QC 实仓：

- `account_history/account_mrpt_YYYYMMDD.json`
- `account_history/account_mtfs_YYYYMMDD.json`
- `qlib-main/semiconductor_strategy/account_history/account_aiss_YYYYMMDD.json`
- `qlib-main/sector_rotation/account_history/account_ssrs_YYYYMMDD.json`
- `qlib-main/electric_utilities_strategy/account_history/account_aeus_YYYYMMDD.json`
- `trade_ledger_bdc.jsonl` 中带日期的 OPEN/DRIP 与 `account_bdc.json`。
- Controller 带时间 stream 对 structure snapshot 的引用，提供更强的 **NAV 投影观测**证据，仍不是 QC 执行证据。

账户字段已见 `as_of, positions[ticker].shares, equity, cumulative_fees` 等；初始资本只作桥接元数据。BDC 将来出现其他交易事件必须明确支持或报 unsupported，不能忽略新事件继续声称账本完整。

### 4.2 账簿种类与三种证据资格正交

每行同时给 `book_kind` 与 `evidence_mode`。例如 observed nav_projection 是“当时确实展示过”，不能改名 qc_actual；当前参数重算的历史目标仍是 exporter_target/restated。

1. `observed`：有时间戳、来源版本与结构证据的实际观测。
2. `reconstructed_with_known_rules`：能证明确切规则和输入在知识截止前可用的重建。
3. `restated_normalization`：今天按冻结规则统一到官方尺度，适合解释，不能冒称当时已知或当时实时控制。

即使是 observed，若 available_at 不明，也不自动满足严格 PIT。`ledger_start`、`splice_at`、scalar freeze、position effective time 分列。SSRS 的 ledger_start=2026-05-01 与 Master splice=2026-05-08 是不同概念。

2026-09-17 先前固定研究快照：六策略共同股份覆盖 9/1–16；使用上一交易日股份的共同收益样本仅 9/2–16 共 10 日。AEUS 9/1 按后续 scalar 重述，全部冻结后共同收益从 9/3 起。不能因此宣称已经有数百日完整逐股 PIT 风险控制验证。

**追加更正：上述覆盖和 60 个策略日损益桥属于官方账本/NAV 尺度研究，不是历史 QC 执行验证。** 9/16 QC v44 收盘存档为 37 票；当日晚间本地账本已有下一交易日 v45 的 44 票，新增 7 票到 9/17 09:30–09:31 EDT 才成交。`positions_as_of=9/16` 不能作为这 7 票 9/16 已执行的证据。本次按全部当前部署 fill events 重建 9/14–17 四个 QC 存档时点，与对应 shares 全部零差异；范围和证据见 [专项审计](QC_NAV_RECONCILIATION_AUDIT_20260917.md)。

### 4.3 损益桥

官方口径的最小恒等式（输入股份必须来自同一官方账本/尺度，不能混入次日 QC 仓）：

~~~text
官方损益 = 前收持仓价格损益 + 当日交易增量损益
         + 股息/现金利息 − 已记录费用/借券/融资
         + 公司行动与官方尺度桥 + 取整桥 + 未解释余额
~~~

当日交易增量需交易时间/价格与持仓路径；没有足够记录时，不能拿收盘股份×全天收益填补。无交易日可用前一日股份；发生拆股先做股数/价格生效日转换。融资估计与实际费用分列，不能重复扣官方已经含的费用。

`day_state.basis` 可能在结构重建时重置为当前价或开仓价，绝不能当可靠历史收盘。股票估值使用未复权价格；协方差 total-return 序列与股息现金不能双重计入 PnL。

QC 执行 PnL 另用 QC 时点股份、实际 fills、股息/现金变动/费用和公司行动计算；与官方 PnL 的差异单列执行时差、价格差、资本/冻结桥和未解释余额。不能把官方数字改成 QC equity，也不能把 QC 实仓改成官方目标。

对账分层输出，互不替代：

- 官方性能 JSON 恒等式与股份损益解释；
- Controller 原始结构/价格同步对账；
- M4 的 source→target 检查、target/version→QC 实际股份检查；
- QC 现金/股息/价格时点的日增量归因与累计净值桥核验。完整合同是 `P − Q = K_effective + verified_timing_rounding_other_bridges + residual_level`，不能省略时点/舍入项强制 `P − Q − K_effective = 0`。rolloff 的加性 K_effective 不是策略股数乘数 k，需保留逐日桥，不擅自固定成一项常数。

原始方法、coverage、缺价和误差必须保留。`qc_reconcile.merge_section` 可保留较早成功结果、把后续结果放入 `later_pass`；必须显示成功时点与最新尝试，不能只读 status=ok 就推断现在通过。K 不能为了归零而重冻。已知 SSRS 5 月初资本桥异常保留；不因后续十日通过就推定全部历史通过。

日增量 residual 与累计 level residual 是两项检查。9/14 的现有 status=ok 对应日增量未归因约 $0.01；`D − K_effective` 仍为 $10,205.77，含前期桥、股息时点与舍入变化，不能直接当漏钱或认定绝对净值已归零。新模块从合格锚点累积每项已核验桥；锚点、某日桥或价格时点缺失就报 level incomplete。未验证的差额不能因为命名为 bridge 就被消除。

9/1–9/14 现有日检查虽均 ok，累计未归因仍约 −$1,057；须独立输出 `open_items_and_cumulative_drift`。每个未结项记录形成日期、分类依据、金额、独立核验状态及消除证据。dividend_timing 的现有分类来自现金桥，不能直接等同逐发行人已验证的应收股息。累计门限另待批准；不能重冻 K 吸收残差，也不能借 daily pass 宣告 level pass。K_effective 台阶所需的 lag/slippage 字段逐一检查，单字段缺失不按零处理。

## 5. 身份与企业行动

Portfolio 既有 ID 为 SPPF96UGK55；策略 ID 从 registry 按 canonical identity 读取，六策略代码映射固定验收，不硬编码注册节点总数。registry 是累计身份目录，不是当前仓位。

证券 ISIN 优先；已有 XF/FIGI 占位保留身份质量。ticker 用于源查询/展示，不作为跨时间的唯一主键。根 `ticker_aliases` 的 resolve(date) 与 canonical 语义不同，必须按用途明确选择。

QC 的 AOC→AON、AHA→SWKS、ARNC→HWM 是来源特定人工映射。ARNC 还可能是另一只证券，不能全局替换。完整 QC SID、源订单和 ISIN 用于消歧。不能因价格查询成功就认定身份正确。

新模块不维护第二套人工别名；读取现有映射版本。若直接调用原函数会静默原样返回，则加前置核验或从已核验声明式内容解析，缺规则产生 finding，绝不自动补写。

拆股、分红、更名、并购/退市、ETF 权重变化均有 event_time 与 available_at。价格 basis 不明的行禁止进入美元对账；ETF 成分历史无 vintage 时不能回填成历史已知穿透。

## 6. 只读陷阱与隔离

- `Registry.spid_of(register_if_new=False)` 对 retired ID 仍可能复活并写 changelog；不能作为只读 lookup。直接读 JSON，不运行 controller assemble/scheduler。
- `PriceDataStore` 初始化可建目录，load 可拉取并写 parquet、股息和索引；不得直接调用生产对象。已有 parquet 直接读取，新下载只进 risk_control 输入目录。
- 根 weekly 价格缓存的 Close 可为 split-adjusted，不能凭列名认作 raw close。`trading_quantconnect/reconcile/official_close.py` 的 adjusted=false 方法可作规范参照；将来下载器独立输出。
- `qlib-main/portfolio_ledger/replay.py` 会写/删除账户历史；不作读取入口。
- `controller/reconcile_eod.py` 的 write_report=False 仅限制报告写入，不足以证明依赖链无缓存副作用；本期不调用。
- 不 import DailySignal/RiskManager 整条业务链取得数据；旧风险报告只作已产生证据。RiskManager 的主要范围是 pairs，其 master diagnostic 也不是当前六策略完整度量。
- 不连通用 agent 的 run_python/配置写入/停止任务工具。命名为 read 的函数也必须经副作用审计。
- QC 可复用现有 QcClient 的鉴权和只读调用；新适配器限制为审核过的 read 端点，不运行 exporter、rolloff freeze、reconcile settle 或 lean 部署。订单接口 `loading` 不等于空列表，必须有界完成分页/长度核验；`_of_deploy` 会丢无事件订单，不可直接用于完整待成交风险采集。

## 7. 与 ontology 原计划逐条对齐

原文件：[ONTOLOGY_CONTROL_PLANE_PLAN.md](../.claude/plan/systemic-strategies-plan/ONTOLOGY_CONTROL_PLANE_PLAN.md)。本次读取 SHA256：`d0eb89a2c3afdac4bca30e4d906ced7d1c7b2d6a62719a71e0c0729ed8117b39`。未修改。

| 原计划约束 | 本模块落实 |
|---|---|
| §III.1 投影而非迁移、身份复用 | 只读源文件，沿用 SPID/ISIN，不新增正式 SPxx 前缀 |
| §III.2/III.8 统一对象与 API 尚未实现 | 初期 LocalSnapshotAdapter；以后显式接 OntologyReadAdapter，不假装现在能 query |
| §III.3 Evidence/Run/GovDecision 分开 | 本模块产出 RiskRun、证据工件和 proposal，预留映射；finding 不等于批准动作 |
| §III.4 来源、时间、证据 pin | 原 bytes、SHA、业务/知识/读取时间、定位、摘要及可达性分别保存 |
| §III.5 A0/A1/A2/A3 | 业务读取 A0，独立产物 A1；本模块初期没有 A2/A3 执行能力 |
| §III.7 frozen/splice/ledger_start | 单独字段与适用区间，不把一个 live_start 用于所有时间规则 |
| §IV.1/IV.5 LLM 数值禁区、确定性 checker | AI 引用已算指标和情景，不算资金/权重，不改变限额或交易 |
| §III.9 路线边界 | 不改聊天、coding toPrompt、morph、前端 artifact 或 MCP 池 |

### 7.1 派生对象而不是第二套本体

本模块内部拟议记录：

- RiskRun：输入包、代码/schema/model/policy 版本、任务结果，对接未来 PipelineRun。
- RiskMetric / RiskFinding：实体引用、方法、数值或原因、证据摘要；归档为 Evidence 工件内容。
- ScenarioResult：模板版本、假设、逐证券贡献、损失、局限；是 Evidence，不是模型自造交易 Decision。
- PolicyProposal：目标、备选、facts_as_of、证据；未来映射 GovDecision，初期 status=proposed。
- ReconciliationResult：方法、误差和覆盖；未来可映射 ReconcileReport。

内部 ID 用本域 run/content digest 或 UUID，仅标识派生记录；不与原 registry 的证券/策略身份竞争，不注册自创 SPxx。

证据原件和 manifest 持久保存在 risk_control 独立产物区。DuckDB 为可重建索引，不能是历史证据唯一存储位置。Evidence 的内容身份与原件路径分离，移动文件不改变内容 ID。`origin_reachable` 不等于 hash 匹配；两项分别检查。

未来接入 ontology 只需投影本模块 manifest/results；查询需保留来源、单位和截止时间。任何新 schema 正式扩展要在原 ontology 流程内独立评审，本次不改原计划，也不提前生成 prompt/enum。

### 7.2 与 SPAC / 旧 RiskManager 的边界

SPAC 计划是配置研究；risk_control 提供事实、约束和已计算候选风险，不争夺 MRPT/MTFS 内部 regime 或策略资金旋钮。旧 SPAC 某些 AI 观点进权重的设想，与较新的 ontology 数值边界不能自动合并；本模块采用 ontology 的更窄只读/解释范围，不修改任一计划。

RiskManager 原有输出、hook、阈值全部不动。新模块不复用它的 pairs 资本当 Master 分母，不假设它的融资/抵押模型是券商事实。

## 8. 必须显式返回的缺口

QC 实仓缺失/部署不匹配、原始 quantity 非法/身份碰撞、目标未应用、NAV 舍入差、策略归因残差、缺失 scalar、价格 basis 不明、available_at 不明、半更新官方文件、未成熟因子源、券商额度未知、缺完整未成交订单，均有机器可读 reason_code。历史无完整 SID 的样本要保留身份资格限制；禁止自动变成“完全核验”。

局部可计算的结果仍可发布，但附覆盖的美元量/证券数/日期，不能把局部数值命名为全组合完整风险。未知金额不能计作零；未知仓位规模时连覆盖率分母也可能未知，应如实返回。
