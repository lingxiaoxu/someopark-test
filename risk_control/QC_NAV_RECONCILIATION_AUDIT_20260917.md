# QC / NAV 股份与历史口径专项审计

审计日：2026-09-17 美东；API 读取跨 UTC 日期至 2026-09-18。**只读调查与文档修订**：没有改 QC、NAV、策略、前后端、人工映射或 ontology，没有下单、部署、重启、重冻或重做生产对账。

## 1. 核心结论

当前没有发现 QC 与 NAV 股份不一致：QC **44 个证券**，NAV **45 条展示行，合并为 44 个证券**，已推/已应用目标 **v45、44 个证券**，每个证券股数差均为 **0**。45 行与 44 个证券的差别是 MTFS 两个 pair 各持有 VLO 660 股，实际净仓 1,320 股。

这次不仅比较本地文件：直接读取 QC portfolio、完整订单回报和应用日志；本部署 251 个去重成交事件重建的当前股份、9/14–17 四份 QC 存档股份，全部匹配。六策略本地来源经现有 build_target 纯函数重算也匹配已推 v45。

但原规划的输入权威有错误，已经修正：**QC 已执行持仓是实际股份权威，NAV 是应与 QC 核对的投影。** 官方资本/收益继续使用用户指定的两份 performance JSON。实际仓位、目标仓位、NAV 投影、官方业绩不能混为同一账簿。

股份一致不表示所有净值归因通过。当前已有 M4 报告中，9/15–17 的 equity_check 仍 pending；9/14 的 ok 是日增量归因判据通过，不是绝对净值无差额。以下逐项说明。

## 2. 审计对象、时点与执行环境

- QC 项目：SomeoPark_Mirror，projectId `35270428`。
- QC 部署：`L-0aa990a7337b76d51fdb55377083401f`；前后均 Running，未发生部署切换。
- QC 返回 brokerage：`PaperBrokerage`。本文的“实际股份”指该 QC 部署已经成交的账户股份，不据此宣称外部真钱券商持仓/额度已验证。
- QC 第一次 portfolio 返回：2026-09-17 **20:33:10 EDT**（2026-09-18T00:33:10.035326Z）。
- QC 第二次 portfolio 返回：2026-09-17 **20:36:24 EDT**（2026-09-18T00:36:24.934524Z）。两次数量逐票一致。
- NAV 冻结发布时点：2026-09-17 **20:32:10 EDT**；结构 `e38d37828f22b743`。冻结输入用于这次比较，不拼接后来滚动更新的面板数据。
- v45 exported_at：2026-09-17 **08:41:06 EDT**；content_hash `cbcebd710e286a2b`。
- QC 日志：2026-09-17 **09:32:00 EDT**，`applied v45 CONVERGED (44 target tickers)`。
- 两份官方 performance JSON 的最近共同完整日期为 **2026-09-16**，Master **$6,827,322.95**。当前股份日期与资本锚日期分列，不冒称 9/17 官方收盘已发布。

本轮主要核验 quantity，不将 QC 返回估值、市值、行情时间与 NAV 金额强行等同。QC/NAV 两端价格延迟与现金口径仍需单独对账。

## 3. 实际做过的检查

1. 复用现有 QcClient 鉴权，调用 projects/read、live/read、live/portfolio/read、live/orders/read、live/logs/read；仅 read 端点，没有调用通用交易/部署入口。
2. 保存原始返回、完整证券 SID、原始 quantity、请求/返回时间；检查 quantity 存在、数值有限、整股、映射后无同名覆盖。当前 44 票均通过。QC 名称按已有人工映射规范化，ISIN 从已有 security_master/NAV 身份取，不伪称 QC API 返回 ISIN。
3. 冻结 NAV、匹配 structure、exporter_state、target、cohort 状态、身份注册和两份官方 JSON。使用现有 `shared/realtimeNav.ts::buildRealtimeNavPanel` 纯函数得出实际面板展示股数，没有重新实现一套“看起来相同”的公式。
4. NAV 按展示行取整，再按证券聚合，与 QC/target 逐票零容忍核对；独立复核六个当前策略来源的 `inventory_source.build_target` 纯函数结果，确认不是“exporter 停了但自己对自己一直匹配”。
5. QC 订单初次返回 loading；保留这两次尝试，没有把 loading 当空单。最终 start=0/end=500 返回 length=295、295 条订单，完整长度相符。
6. 其中 251 条属于当前部署，订单 ID 连续 1–251；另 44 条属于其他部署，分开处理。所有 295 条均为 filled，未发现无事件/部署不明订单。本部署 251 个非零 fill events 用事件 ID 去重后累计，重建当前 44 票，逐票零差。
7. 同一成交序列按四个历史 QC 存档的 taken_at 截断，再与存档 shares 比较，9/14、15、16、17 全部零差。此样本的累计净成交与实仓一致，并不构成未来任何部署都能从零仓开始的规则；未来须保留起点持仓、部署承接、公司行动和 cash-in-lieu 证据。
8. 检查源代码与历史对账状态，区分 M4/M5、目标未应用、日增量归因与累计净值桥；不执行报告里建议的 settle 命令。

完整订单读取证明的是本次返回范围的完成状态，不等于部署了持续订单监控；两次仓位稳定也不一般性保证区间中无开平 round-trip。未来采集必须保存成交覆盖区间和非原子读取质量。

## 4. 逐票股份结果

“策略来源”是 NAV/目标 attribution 的虚拟分配标签，不是 QC 分策略子账户回报。正数多头、负数空头。以下 44 行所有差值为 0；MRPT 当前无股票仓，不代表没有官方资本或现金。

| 证券 | 策略来源 | QC 已执行股数 | NAV 汇总股数 | v45 目标股数 |
|---|---|---:|---:|---:|
| ADI | AISS | 416 | 416 | 416 |
| AEE | AEUS | 1493 | 1493 | 1493 |
| AEP | AEUS | 542 | 542 | 542 |
| AMAT | AISS | 105 | 105 | 105 |
| AMD | AISS | 1448 | 1448 | 1448 |
| AON | MTFS | -263 | -263 | -263 |
| ARCC | BDC | 1253 | 1253 | 1253 |
| ARM | AISS | 177 | 177 | 177 |
| ATO | AEUS | 535 | 535 | 535 |
| BIL | BDC | 5189 | 5189 | 5189 |
| BXSL | BDC | 948 | 948 | 948 |
| DECK | MTFS | -730 | -730 | -730 |
| DUK | AEUS | 922 | 922 | 922 |
| EMR | AEUS | 452 | 452 | 452 |
| ETN | AEUS | 273 | 273 | 273 |
| GBDC | BDC | 29446 | 29446 | 29446 |
| GEV | AEUS | 59 | 59 | 59 |
| HPE | MTFS | 4431 | 4431 | 4431 |
| INTC | AISS | 1402 | 1402 | 1402 |
| KLAC | AISS | 4406 | 4406 | 4406 |
| LNT | AEUS | 1654 | 1654 | 1654 |
| LRCX | AISS | 485 | 485 | 485 |
| LVS | MTFS | -1297 | -1297 | -1297 |
| MCHP | AISS | 689 | 689 | 689 |
| NEE | AEUS | 1875 | 1875 | 1875 |
| NUE | MTFS | 616 | 616 | 616 |
| OBDC | BDC | 2093 | 2093 | 2093 |
| OGE | AEUS | 1954 | 1954 | 1954 |
| PCG | MTFS | -3611 | -3611 | -3611 |
| POOL | MTFS | -476 | -476 | -476 |
| POWL | AEUS | 232 | 232 | 232 |
| PSX | MTFS | 546 | 546 | 546 |
| SO | AEUS | 1261 | 1261 | 1261 |
| SWKS | MTFS | 1244 | 1244 | 1244 |
| TSLX | BDC | 1191 | 1191 | 1191 |
| TXN | AISS | 3113 | 3113 | 3113 |
| VLO | MTFS | 1320 | 1320 | 1320 |
| WYNN | MTFS | -632 | -632 | -632 |
| XLB | SSRS | 2911 | 2911 | 2911 |
| XLE | SSRS | 2833 | 2833 | 2833 |
| XLI | SSRS | 923 | 923 | 923 |
| XLK | SSRS | 847 | 847 | 847 |
| XLRE | SSRS | 3996 | 3996 | 3996 |
| XLV | SSRS | 1194 | 1194 | 1194 |

## 5. 历史发现：相同账本日期不等于相同执行仓位

已有 QC close_snapshot 与本次成交重建如下。晚间存档的 taken_at 不伪装成 16:00 精确收盘瞬间；在本次完整成交序列范围，相关收盘至存档间没有造成差异的额外成交。

| 交易日 | QC 存档 taken_at（EDT） | 已应用版本 | QC 非零证券数 | 存档/目标股份检查 | 本次 fills 重建与存档 |
|---|---|---:|---:|---|---|
| 9/14 | 23:50:07 | 38 | 49 | 49/49 相等 | 0 股差 |
| 9/15 | 23:50:06 | 42 | 45 | 45/45 相等 | 0 股差 |
| 9/16 | 23:50:07 | 44 | 37 | 37/37 相等 | 0 股差 |
| 9/17 | 16:20:06 | 45 | 44 | 44/44 相等 | 0 股差 |

9/14–15 原始存档早先有 QC 特定显示名差异；现有报告保留了 9/17 人工映射修正及旧检查备份。本文读取修正后的证据，不把修正时间倒写为当时已知，也没有再次修改这些报告。现存历史 shares 已丢完整 SID 的局限仍保留。

关键反例是 `account_history/account_mtfs_20260916.json`：as_of 和下列头寸 entry_date 都是 9/16，但 QC 9/16 并未持有这 7 票；它们到 9/17 09:30–09:31 EDT 才在 v45 成交：

| 证券 | 9/16 QC 股数 | 9/16 本地账本股份 / 9/17 QC 股数 |
|---|---:|---:|
| VLO | 0 | 1320 |
| AON | 0 | -263 |
| SWKS | 0 | 1244 |
| POOL | 0 | -476 |
| LVS | 0 | -1297 |
| NUE | 0 | 616 |
| WYNN | 0 | -632 |

这是官方账本的决策/记录时点与 QC 执行时点差异，不能据此认定当前漏成交。风险系统必须在 9/16 用真实仍在场的 v44，另列待执行的新目标。不能把之后看到的 v45 反填成 QC 当日实仓。

因此，原研究中的“10 日共同历史股份”“60 个策略日损益桥”仍是官方账本/NAV 尺度的研究证据；不能因为其残差很小就宣称 QC 历史实际 PnL 已完全对账。本次没有改写原研究文件或官方历史收益。

## 6. 净值检查的真实状态

| 日期 | 现有 equity_check | 实际含义 |
|---|---|---|
| 9/14 | ok | 当日增量归因剩余约 $0.01，通过该判据；不是 QC equity 等于官方 equity |
| 9/15 | pending | 独立估值路径差 $5,099.84，约 6.74bp，超过该检查 5bp；有 +$921 非成交现金变动，其入账时点不明，固定分钟价格/现金桥未验证 |
| 9/16 | pending | 当日两估值路径差 $2,703.03，约 4.17bp，在该检查 5bp 内；但缺 9/15 合格基准，不能把跨日差当一日差归因 |
| 9/17 | pending | 该日官方 EOD 尚未出现在指定 JSON；不能拿 9/16 官方净值与 9/17 QC 净值对账 |

上述 bp 是现有 QC 对账报告的 gross 口径检查单位，不是本模块风险/收益的官方 Master 分母。阈值是既有对账实现规则，不是新批准的组合风险限额。

9/14 具体说明为什么必须分清日增量与绝对水平：

- `D=P−Q=$1,268,365.41`，`K_effective=$1,258,159.64`，因此 `D−K_effective=$10,205.77`。
- 这一水平差可从前期 9/11 的 $747.17，加本日股息时点项 $9,466.51、舍入差变化 −$7.92、未归因约 $0.01，得到 $10,205.77。
- 不能把 $10,205.77 一概当成“少钱”，也不能把日增量 ok 宣称为“累计桥已归零”。完整累计核验需要起始锚点和逐步时点/舍入/其他桥证据。

继续检查 9/1–9/14 已有报告，发现一个需要新模块明确保留的未结项：这些日期日增量检查均为 ok，但累计 `unattributed_usd` 约 **−$1,057.00**，主要来自 9/1 的 −$80.23、9/2 的 −$976.78。累计 `dividend_timing` 为 +$10,913.81；小数股份残差从 8/31 −$129.87 变为 9/14 +$219.11，变化 +$348.98。由水平金额计算 `10,205.77 − 10,913.81 − 348.98 = −1,057.02`，与逐日求和相差 $0.02，来自已落盘字段的逐项取整。

这不是新发现的交易亏损，也不能直接判断为漏钱；它是已有容差规则接受但尚未逐项解释清楚的累计差额。`dividend_timing` 也是现有现金恒等式推导的分类，不等于已逐发行人/除息日/付息日核验的应收清单。故不能把整笔 $10,205.77 全称为确定会自然回冲的暂时股息差。

新模块合同为：`P − Q = K_effective + 已核验的时点/舍入/其他桥 + residual_level`；另存 `residual_daily`。未知桥不强填，失败不重冻 K，不把 QC equity 当官方资本分母。

另建未结项目与累计漂移检查，记录形成日、独立核验状态、已解释/未解释余额和后续消除证据。累计金额/bp/持续时间门限须由用户批准，不能借每天 3/5bp 的规则无限累积，也不能直接套日门限误判累计 breach。K_effective 的每个永久台阶需同时有 lag/slippage 等预期字段；缺一个不能默认为零。本次样本未发现该缺字段，但现有实现的宽松缺值处理不能成为新合同。

界面现有 `readNavReconcile()` 读取的是 Controller M5 本地对账；它不调用 QC。故 NAV 面板绿色、`navQuality=pass`、helper 字段叫 `qc_shares`，都不能替代上面这份 QC API 核验。

## 7. 未发现当前错误，但必须修正文档/预防的结构问题

### 7.1 NAV 与 exporter 舍入顺序不同

NAV 每展示叶行乘 frozen k 后 bankersRound；Controller 输出前还把原股份保留四位小数。exporter 先把各策略同一 ticker 的小数目标净额汇总，再 Python round 成整数。两条各 0.5 股的示例，NAV 可为 0，目标为 1。

当前 44 票没有这种差异；独立检查现存 41 份结构、按当前冻结参数重述也未找到该类差异。这不是历史 QC 永远相等的证明。新模块分别保留两种算法、差异桥和版本；QC 整数目标收敛仍是 0 股容忍，不放宽容差隐藏 1 股执行差。

### 7.2 现有 QC 辅助函数会丢信息

`ops/rolloff.py::qc_snapshot` 对原 quantity 做 int，并用完整 SID 的首 token 作字典键。当前样本均整股且未碰撞，但未来小数/同名前缀可能被截断/覆盖。新模块复用现有客户端的鉴权与 read 调用，先保留 raw 响应，再严格校验；本次不改生产 helper。

QC 人工映射仍从既有 `QC_SYMBOL_ALIAS` 只读复用，尤其 AOC→AON、AHA→SWKS、ARNC→HWM；不能全局改市场 ticker 身份。SID/来源 namespace/部署与原名一起保存，未知映射不自动猜。

### 7.3 策略归因和源文件并非天然唯一

SSRS 的 Controller NAV 使用 account_ssrs.positions，exporter 使用 inventory_sector_rotation.holdings；本次一致，未来必须核对。AISS/AEUS 分行业路径也是派生分配。QC 总净仓本身不提供六策略独立账户持仓，小数 target attribution 不应冒充已执行整数股。

新模型直接用 QC 实际向量算组合风险；策略归因不足的余量列 unallocated，并纳入风险合计，不按市值比例强行消失。

### 7.4 状态合并和订单筛选不能照单照搬

`merge_section` 保留早先成功记录、把后来尝试放在 later_pass；消费端应展示各自时点，而不是仅看一项 status。历史 fill helper `_of_deploy` 会排除没有 events 的订单；新模块在计算待成交风险时必须保留这类 unknown。初次 orders 返回 loading 也不应算“0 待成交”。

## 8. 更新后的设计及实现边界

已同步六份规划文档：实际风险的 QC quantity 输入、两份 JSON 资本分母、五段目标/执行/NAV 核验链、历史 book_kind 与 evidence_mode、实际/目标两套风险、归因残差、日增量与累计净值分项状态、SID/raw quantity/订单完整性验收。

当前只形成设计和审计，没有上线风险模块。未来 RISK-0/1 优先做只读采集与事实对账；模型、AI和候选降险方案都在其后。QC 数据缺失时不能给“实际风险已验证”的报告，但仍可单独提供明确标注的投影研究。

所有前后端、六策略交易逻辑、QC 运行参数、持仓、人工映射、原 ontology 计划保持本次未修改；本轮没有接触 crypto、模型训练或 Hosting。

## 9. 证据索引与复核方式

私有临时证据目录：`/tmp/risk_qc_nav_audit_20260917/`。包含原始响应（不含鉴权头/密钥）、source 冻结文件、local_manifest.json、现有 TS helper 的离线调用、verification.json 和逐笔复核脚本。原始账户数据未加入 risk_control 目录；以下只记录摘要和必要持仓核对结果。

| 证据 | SHA256 |
|---|---|
| `qc_portfolio_before.json` | `c40c51122cf653967e0d4f5ec55cbead515d16a21f42339195cfb6a947bcb3c1` |
| `qc_portfolio_after.json` | `b40105e308a57712ca231c62bf5a7c5c6cd576f39ae292966c98b7706353efd5` |
| `orders_500_attempt0.json` | `c32cef28747ea6b02ce1177f9663f30f8602085d23452ae5d20cfa8ce07ac94c` |
| `verification.json` | `87a143b47d71a86040ad5d1360e07b3684e3b4d22d1cac476deff030dc00a2f7` |
| `cumulative_bridge_check.json` | `615492bd6ddeec9e3ba87bb402ed3f3f1c811509ae9d94dcce1bc9550c83dcff` |

已有生产源码定位（只读参照，不是新实现）：

- `someo-park-investment-management/shared/realtimeNav.ts`：holdingsPresentation / buildRealtimeNavPanel，NAV 展示与本地 qc_shares 的来源。
- `someo-park-investment-management/server/utils/controllerNavData.ts`：readNavReconcile，M5 报告读取。
- `trading_quantconnect/inventory_source.py`：build_target，同票净额后取整。
- `trading_quantconnect/ops/rolloff.py`：QC_SYMBOL_ALIAS、qc_snapshot；k_effective 在函数内从 reconcile 模块导入。
- `trading_quantconnect/reconcile/qc_reconcile.py`：merge_section、k_effective、holdings_plane、target_plane、equity_plane、close_snapshot。
- `trading_quantconnect/lean/main.py`：目标版本应用、开盘执行与 CONVERGED 日志。

/tmp 不是长期档案；将来开发时须按 SHA 校验后归档必要原件到本模块私有输入区。若原件消失，本文仍是审计摘要，但不得声称每个数都能从摘要独立复算。已有报告在本次之后若被运行任务刷新，应对照本次冻结 bytes，不能覆盖本次证据结论。

文档验证记录在同临时目录的 `document_validation.json`：七份 Markdown 本地链接与围栏均通过，risk_control 内无实现代码/运行配置。ontology、SPAC 原计划、NAV 共享计算、官方权益读取器、RiskManager、QC 人工映射代码及根 ticker aliases 七个保护文件的 SHA256 均与初版设计开始前一致；这不等于宣称运行中的其他数据文件从未自行更新。
