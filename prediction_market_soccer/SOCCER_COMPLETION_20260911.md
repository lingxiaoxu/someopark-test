# Soccer 剩余事项：修复、历史证据与真实比赛验收

> **最新发布（2026-09-11 08:53 UTC）：生产当前为 v7。** 请先读 [Soccer 队名与 Kalshi 报价修复记录](SOCCER_UI_QUOTES_20260911.md)。截图六场 Kalshi 三路报价及真实 UI 已通过验收；五语言身份目录已同步，中文四个球队词条已修正。本轮未改变布局、World Cup 或 Macro，开发与测试均在 `/tmp`。359 场、618 条策略腿继续冻结；旧历史 PIT 证据缺失及新版本真实成交/结算尚待验收的边界不变。
>
> 下方 v5、v3 等“当前”表述是此前阶段的原始记录；以最新链接的 v7 发布凭证为准。

本文件记录 2026-09-11 对上一轮生产验收剩余项的继续处理。发布结果只以末尾生产凭证为准；修复测试、历史重放和操作工具均位于 `/private/tmp/soccer-completion-20260911T0502Z`。生产根仍为 `/Users/xuling/code/someopark-test`。没有把名称中含 test 的生产目录当作测试目录。

## 范围和不可变约束

- 只修改 Soccer。原 World Cup `prediction_market/`、Macro、共享前端源码和三个原 Soccer LaunchAgent 配置保持不变。没有新增用户反对的 UI 说明。
- 保留原 GitHub 主分支备份 `1108f520a0c2d7bdd33b83519ecf3f1ad23e9b34`。v4 源码提交为 `81b1d3b67aba639122308ddaa1b486185dc7fe0d`；v5 两文件提交为 `87e559c866aadaafe9c53f48d2df263adae7b836`，均在私有仓库 `lingxiaoxu/someopark-test-backup-20260910` 的 `soccer-production-20260911` 分支。数据库、原始行情、env、凭据和外部测试没有上传。
- 维护基线为 359 场、618 条策略腿。完整 ledger SHA 为 `bf1bbf9c0952bb679aa0bf3e0a8ed7e5a373ba85686a709aafc38be200be6dc6`；book version 为 `00ffda382ea41f3e4fec8b19b45226b51db39e864afddb91a01753cb595c514e`。没有删除 `milestone_snapshot`，没有 DROP `settled_bet`，没有清空历史 `inplay_json`。
- 准确度盈亏、价格轨迹和 PDF 的金融记录继续由同一冻结 book 提供。补采 reference 不会触发历史重算、每日改写旧收益或将 Demo 子集当作全策略收益。
- 持久备份和审查证据位于 `/Users/xuling/.codex/backups/soccer-completion/20260911T0502Z`。发布备份使用 SQLite 一致备份；原件和候选具有独立哈希，不能使用普通复制代替活动数据库一致备份。

## 已修复的后端问题

### 1. 历史序列被错误整段拒绝

`util/price_history.py` 原先因返回序列中一个窗口外样本拒绝整段。现在完整验证真实 raw 后保留显式请求窗口内的样本，同时保存原始与投影哈希。目标只允许最后一个 `sample_ts <= target_ts` 的样本，容差 180 秒；不会使用最近邻未来点，也不会把单价转写成可成交 bid/ask。

`ops/recover_reference_history.py` 提供独立不可变恢复候选，本身不写 collection 状态。合法窗口外样本保留在 raw 及排除证明中，再使用窗口内投影；非法或冲突样本、过大间隔、缺 token、缺盘口身份或原件不符均明确拒绝。另由独立 ACK 维护工具在原件、目标和 sealed candidate 核验成功后确认两个 collection 表的完成标记，金融表由 SQL authorizer 禁写。

### 2. 失败赛事反复占满每日队列

`util/collection_state.py::daily_scope` 现在按从未尝试、最久未服务、固定开球/API ID 排序，保留原 14 天范围、12 场/48 请求上限、退避、scope 与版本区分。之前最新的少数失败赛事可能持续占用队列，让其余赛事从未尝试。

整本一次性恢复以固定 359 场范围为准，不采用 DELETE 后 14 天重跑。既有完成 25 场先保留，其余 334 场分成 39 个明确批次，每批最多 12 场、48 次公开 GET；一次执行，不自动重试。HTTP 401/403/429/5xx 会停止后续批次。

### 3. 场馆发现状态过宽、公开输出重复

`venues/kalshi/discovery.py`、`venues/polymarket_us/discovery.py` 和 `ops/upcoming_export.py` 将报价失败原因绑定到确切 fixture、competition、market family 和查询窗口。完整诊断留在后端 `discovery_receipts`，公开输出只保留紧凑范围与原因。

真实 `atomic_json` / `public_projection` 路径验证完整收据先存为私有 gzip，再生成公开文件；不会以缩短 UI 数据为由丢失用于决策的收据，也不会将私有归档时间倒填成历史可用时间。压力验收包含 1,000 条拒绝记录且公开文件低于 2 MB。

### 4. 球队完整名称变化导致静默漏匹配

真实历史目录中的 30 个未识别标签逐一核查。`config/club_identity.json` 只向现有球队追加 28 条有原始证据的精确 alias，409 个 canonical ID、既有别名与五语言显示字段均保持不变。证据同时绑定 provider team ID、比赛双方、赛事和精确相同开球时刻。

Rīga FC 与 FK Rīgas Futbola Skola、FC Inter Turku 与 Inter、PFK CSKA Sofia 与 CSKA 1948 分别验证，不能靠相似字符串合并。`CD Universidad Católica` 和 `KF Víkingur` 仍有歧义，没有添加。

119 份已保存目录离线重放，334 场严格匹配从 281 增至 331；原 281 场的事件和三路 token 全部不变，恢复另外 50 场。继续核原件发现 1570336、1607650 的合同实际存在，只是改期后 slug 仍保留旧日期；这两场不能称为场馆未上市。1623411 的 KF Víkingur 保持身份未解决。

`ops/backfill_milestones.py` 同时修正未匹配分类：目录传输不完整为 `discovery_partial`；传输完整但身份解析不完整为 `identity_unresolved`；只有两者都完整才可记 `not_listed`。已经精确匹配的赛事不因其他赛事的未知标签被连带拒绝。

另新增 `_rescheduled_binding`，只为原日期窗之外的旧 slug 提供严格改期验证：必须同时满足源 fixture kickoff、原 event.startTime、已选三路 market.gameStartTime 为显式带时区且完全一致；唯一 YES、球队主客顺序、moneyline、原件哈希、延期后保持合同以及常规 90 分钟结算条款全部通过。endDate 不参与开球推断；旧 slug、题目和原收据不改写，候选保存 schedule_binding。39 批目录回放确认原 331 场整个 binding 不变，仅恢复 1570336、1607650。两场实际 6 次 history GET 全部成功，36 个里程碑目标与 6 条连续序列已形成独立候选。

最终源码在私有 GitHub 提交 `b5a3313a8dc5abdbab00eade296daf5423705c4a`。早期 v5 两文件测试和源码保留为中间证据；包含改期修复的最终发布包在独立 `final-v5` 目录重新验收，不能混用先前测试结果。

### 5. 空转周期遗漏 Demo 对账

`exec/demo_forward.py` 与 `ops/live_refresh.py` 在没有新比赛信号时仍对已有 pending、持仓与净 durable fills 对账，只调用场馆 GET，不发新订单；本地执行投影及结算按真实成交/终态凭证补回或更新。完全无工作时不额外请求。找回镜像必须唯一对应既有 durable intent。

不会因未查询到订单而重发，不会清空 unknown、不强平、不改下单规则。结算使用实际场馆终态、时间与 payout，保留成交、手续费与余额的独立来源。共享 Demo 余额不代表全量 Soccer 纸面收益。

### 6. 历史认证与替换缺少独立验证

新增 `util/redecision_certification.py`，并修复 `forward_replay_certification.py`、`frozen_strategy_store.py`、`ops/version_workflow.py`：认证必须独立从真实来源重决策，验证源数据 DAG、模型实际可用截止点、报价与比分状态、显式 PRE 窗口和 120 秒新鲜度。缺输入不等于没有优势或零收益；`condition_not_triggered` 不自动变成可替换的金融收益证据。

`util/source_archive.py`、`source_history.py`、`research_inputs.py` 支持哈希验证的原件档案，保留原 DB/ref 和原始获得时间；`copied_at` 不能成为历史 availability。

只有完整范围、非空、因果与财务双通过的独立 bundle 才能登记/替换。三份 candidate 文档与独立 certification 的四份数据均与 ledger 绑定，并非四份公开金融报表。`ops/milestone_export.py` 读取已封存输出，日常展示不重新运行历史研究。

当前独立重决策仅支持已实现的 strength、PRE hybrid、IP/overshoot 路径和已经持久记录的观察机会，不覆盖任意新模型或未观察的连续时间窗口。当前重算的 `computed_at` 与历史 `simulated_at` 分开；`strict_oos=false`，没有认证历史真实成交。通过当前受支持路径的程序回归，不等于已经为任何旧模型或全部历史建立 PIT 认证。

## 已完成的真实历史 PIT 审核及其结论

已对完整 359 场 × PRE/IP 两腿范围进行独立审核，共 718 个目标。97 份实际原始引用成功校验归档；没有改原 DB 引用。718 个目标全部缺少 durable quote receipts，不能恢复为证据完整的当时决策。`audit_complete=true`；`input_causal=false`、`financial=false`、`PIT=false`；独立生产替换门禁拒绝。

这是已完成审核后得出的证据不足，不能通过代码开关变成认证通过。后来取得的历史成交价不能补出原始 BBO、真实比分观察时刻、事件修订历史和当时模型参数的可用证据。现有历史收益保留冻结，不能改贴新 epoch，也不能用一个空候选替换全部旧记录。

证据：持久目录 `verified-artifacts/evidence/actual-historical-redecision-v4`、`verified-artifacts/input/source-raw-archive` 和 `verified-evidence/actual-historical-audit-summary.json`。参考行情恢复与严格历史认证是两个不同的验收结果。

## 当前场馆报价的真实核查

06:15 UTC 的 168 场 upcoming 内有 119 个场馆报价缺失项，均有明确诊断；没有遗漏对应诊断或错绑赛事/盘口类别。49 个 Kalshi 和 37 个 PMUS 项在当次完整目录未找到，32 个 PMUS 目标在常规查询窗口之外，另 1 个 Kalshi 首次目录请求失败。

后来仅用 6 次公开 GET 做定向核查：Kalshi KXUECLGAME open 完整返回空目录；PMUS 分页完整读取 286 个 series，按 UCL=12、UECL=131 及 10 月 12–16 日窗口精确查询均返回空事件。原始 HTTP/body/hash 全部保存。可证明的是该次该范围未发现盘口，不能断言永久不上盘；也不能为了消除 unavailable 制造报价或更换成其他比赛合同。

## 真实比赛验收

新版本实际下单必须等真实 PRE/IP 条件和真实可成交盘口。下一场既有赛程为 2026-09-11 18:30 UTC（纽约 14:30），源码中的 PRE 资格窗口从开球前 25 分钟，即 18:05 UTC（14:05）开始。原有比赛任务正常按此窗口运行；18:10 UTC（14:10）是另行安排的第一次后续验收时间。窗口之外不应人为制造成交。

已设置附在本任务的 Soccer 后续验收，纽约时间 14:10、15:10、16:10、17:10、18:10、20:10、22:10 检查。每次先读取实际 active epoch，再检查真实 paper decision → durable intent → fills/fee → 场馆 finalized settlement → 余额 → 三份同源金融输出，并在 `/tmp` 做独立核验。

触发条件不成立要保存真实不入场原因；没有盘口/成交则保留真实缺失，不强制下注。不启用真钱、不放宽模型或门禁、不重写未知订单。只有实际发生的成交和结算才可标完成；届时暂停已完成的后续验收。

## 实际发布和最后验收

### 生产 v5 已安装并验证

最终生产代码提交为 `b5a3313a8dc5abdbab00eade296daf5423705c4a`，在安装前已推送并核对私有 GitHub 远端。当前方法为 `soccer-timing-integrity-20260911-v5`，模型版本为 `soccer-observed-pit-v1`，epoch 为 `4a284ef0f9b79844e8752a8edbd016de7dc1a76f557824debc5f815f5cd28162`。实际激活时间为 **2026-09-11 07:13:39.535129 UTC（纽约 03:13:39 EDT）**；07:15:47.401643 UTC 的 `release-v5/journal.json` 已为 `verified`，12 个模型均按真实可得时间准备并校验，没有继承旧 epoch 的交易身份。

最终发布包独立完成 **318 项回归通过（319.00 秒）**、**22 项实际 v4→v5 发布合同测试通过（293.22 秒）**。两个 XML 的 SHA 分别为 `f350e9a6376c772fa3786ff817ef384b77c1179cf92cfc2b1961031649365e01`、`173dfa7e9381a173a71bc668c4d12baaed1c2e5c3c5b60554ea1fe21619e8452`。二者与最终 259 文件清单及发布策略、适配器绑定；没有使用中间版本的绿灯替代最终源码验收。针对别名、改期、采集确认、行情缺口还有专项回归，测试组可能交叉，不能相加宣称独立用例总数。

07:26:34 UTC 核验 **259/259 个 Soccer 静态文件、692/692 个受保护文件**全部匹配。259 是本次检查范围，不是本次修改了 259 个文件。最终代码验证后，仅在本模块新增本文、向原计划和 v3 执行记录增加最新入口；这一独立的三份文档差异记录于 `final-documentation-source-scope.json`，不改变运行代码、模型、交易规则或原测试绑定。

三项原 Soccer 服务在维护期间停用，并于 **07:28:03 UTC** 全部恢复，原 plist 完整保留。当前 upcoming、inplay、risk、overview 四个 builder 于 **07:27:54.847848 UTC** 成功发布，金融 head 保持不变。**07:28:41.254248–07:28:44.278421 UTC 的本地及线上 14 次 GET 全部 HTTP 200，七类文件两端字节一致，failures=[]**；359 场/618 腿的完整金融记录、轨迹记录、累计值和 PDF metadata 对齐。live 在 07:28:25.343594 UTC 为 ok，核验时心跳年龄约 16 秒。

当前 upcoming 为 168 场、1,448,514 字节，仍如实报告 119 个场馆报价缺失项。先前完整刷新中 optional 历史采集留下的 degraded 状态没有伪改为成功；本次当前视图发布成功并不意味着所有场馆都有报价或旧采集运行已被重写。

最新实际 Kalshi Demo 查询时间为 **07:27:52 UTC**：cash **$1,266.87**、portfolio **$3.21**；Demo 镜像 `enabled=true`、`environment_ok=true`，待处理订单及 Soccer 开仓均为 0。这是共享账户余额，不能将余额变化算成 Soccer 收益。53 条历史成交不属于新 v5；v5 一致快照中的 paper entry 和 demo intent 均为 **0**，真实新比赛成交、结算尚未发生。没有启用真钱或改变原交易限额。

### 历史参考实际入库验收

最终一致快照为 `production-v5-after-ack.db`，取得于 **07:25:06.219194 UTC**，大小 676,438,016 字节，integrity_check=ok，SHA 为 `cb24008c0dd915a9d2e66368fe70318210dfb89bc5812d3ee6ffa9f64f57ab79`。

独立只读验收逐一核对 **7,827 个成功参考采集目标**，包括原有 417 个和本次追加确认的 **7,410 个（270＋5901＋1050＋42＋147）**；这些是价格/轨迹采集目标，不是策略腿或 PIT 认证目标。110 个新增候选副本与 11 个原件的路径、哈希、密封 run/scope、观察引用和最新 append-only attempt 均一致。确认操作只写 `collection_task_state_v1` 与 `collection_attempt_v1`，不覆盖金融记录；全部实际操作完成后才解除对应维护门禁。

- 原账本固定 359 场：**358 场里程碑完整，356 场连续历史价格轨迹完整**。
- 本次固定 14 天范围 174 场：**174 场里程碑完整，172 场轨迹完整**。
- 两范围并集 374 场：**373 场里程碑完整，371 场轨迹完整**。这些是固定快照范围，不能直接当作未来每天的覆盖分母。

剩余 27 个目标在真实 DB 中均未登记，未误标 complete：1623411 的 KF Víkingur 身份未解，影响 18 个里程碑价格及 3 条轨迹；1635609、1635643 各三路历史轨迹存在 369–370 秒缺口。针对后者再作 6 次明确窄窗 GET，均 HTTP 200，每条返回 5 个已见样本，缺口内没有新点且无冲突。保留原始收据，没有插值或补价。这里只能证明两轮指定请求未给出内部点，不能宣称供应商永久没有数据。

用户 assessment 中的 **1623407** 已由本次实际保存的原始序列复核：T60 的目标为 **2026-08-20 19:15:00 UTC**，最后因果样本 19:14:14、away 价格 **0.445**；T75 目标 **19:30:00 UTC**，样本 19:29:15、价格 **0.55**。这说明修正后的参考查询可以复现相应价格；默认中场 15 分钟仍是历史墙钟近似，不能因此认证原始比分观察、真实下半场起点或可成交 BBO。旧账本没有据此重写收益。

实际入库完整报告位于持久目录 `final-closeout/evidence/production-v5-reference-coverage.{md,json}`；JSON SHA 为 `6008eba9d2d20a959966b785f810259900532b1efddd5748b5bfa5a1000cd7f6`。逐目标证据保留原始失败/缺失状态，不把“参考行情完整”写成“历史 PIT 通过”。

### 剩余事项的关闭条件与后续顺序

1. **真实比赛前向验收：等待实际事件。** 已启用的本任务 heartbeat 从纽约 9 月 11 日 14:10 开始跟进。读取当时实际 active epoch，核对 PRE 在开球前、IP 在真实观察后记录，报价/模型输入均已当时可得；有真实可执行盘口且策略触发时核对唯一 intent、真实成交、手续费、终态结算与三份同源输出。没有入场条件不算错误，也不能把未成交标成成交验收通过。
2. **1623411 身份：等待正式唯一映射证据。** 需要 provider team ID 与 API-Football canonical team 的可追溯桥接，能明确区分 Reykjavík 与 Gøta 同名候选，并与比赛双方、赛事和原时间一致。拿到证据后先在 `/tmp` 回放既有全部 binding，证明旧匹配不变，再补独立 reference 候选；不能先加全局模糊 alias。
3. **六条轨迹缺口：等待真实缺失样本或独立来源。** 只允许有来源、时间和哈希的实际样本补充新候选。需验证连续性、因果时刻与 scope，旧原件不能覆盖。没有新增数据时不反复无界请求，不扩大容差掩盖缺口。
4. **当前没有报价的场次：继续原有有界发现。** 对当前真实窗口内的数据错误继续修复；对完整目录无目标、查询窗口之外或身份未解分别保留原因。未来目录出现合同后仍须通过球队、日期、方向、规则、场馆环境与可成交报价校验，不能借其他比赛盘口成交。
5. **旧历史 PIT 认证及替换：证据不足，未完成认证/替换。** 审核流程已经完成，但 718 个目标全部缺少当时的 durable quote receipts。只有找回原始、可核验的报价/状态/模型可得证据，并由完整范围独立重决策通过因果及财务门禁，才能提出替换；不能用后来取得的行情或现在新建的文件倒填时间。现阶段保留原冻结 book。以后按新方法实际累积的前向记录可以独立验收并追加，但不能倒过来认证旧记录。

### 持久证据入口及恢复原则

以下路径均相对 `/Users/xuling/.codex/backups/soccer-completion/20260911T0502Z`，私有原件不进入 GitHub：

- `release-v5/journal.json`、`production-before-final-v5.db`：实际安装、激活、12 模型验证及前置一致备份；每个 ACK 批次另有独立 before DB 和结果/复核/维护释放记录。
- `final-v5-verified-package/`、`final-v5-verified-package-manifest.json`：最终源码、318/22 测试、发布策略、独立审查；旧中间版本证据按原哈希保留。
- `reference-full-book-evidence/`、`fifty-history-evidence/`、`final-reference-evidence/`、`final-verification-addenda/`：实际原始 HTTP、候选、收据、采集操作与独立验收，各自配有 manifest。
- `final-closeout/`、`final-closeout-manifest.json`：最终 14 GET、实际生产覆盖、文档与范围检查、后续验收配置快照及外部操作工具纠正记录。
- `services-v5-restore-20260911T072803Z.json`、`current-views-v5.json`、`installed-final-v5-source-scope.json`：实际原服务恢复、当前发布、受保护范围。
- `github-source-backup-final-v5.json` 与后续 `github-documentation-closeout.json`：源代码先备份再安装，结案文档另作精确三文件提交；原主分支不改写，gitignore 保持不变。

恢复时先只读确认当前 epoch、开放持仓、待处理 intent 与本次后续新增记录；不得在比赛运行中直接拷贝旧 DB 覆盖现有数据。原始一致备份用于审查或在独占维护下制定恢复方案，不能将本次“前向修复完成”解释成“历史 PIT、全部报价和真实新交易均已证明”。
