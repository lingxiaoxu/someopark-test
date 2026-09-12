# Soccer PIT / 数据完整性修复生产执行记录

> **最新发布（2026-09-11 08:53 UTC）：生产当前为 v7。** 请先读 [Soccer 队名与 Kalshi 报价修复记录](SOCCER_UI_QUOTES_20260911.md)。截图六场 Kalshi 三路报价及真实 UI 已通过验收；五语言身份目录已同步，中文四个球队词条已修正。本轮未改变布局、World Cup 或 Macro，开发与测试均在 `/tmp`。359 场、618 条策略腿继续冻结；旧历史 PIT 证据缺失及新版本真实成交/结算尚待验收的边界不变。
>
> 下方 v5、v3 等“当前”表述是此前阶段的原始记录；以最新链接的 v7 发布凭证为准。

> **最新续修记录（2026-09-11 07:28 UTC）：生产当前为 v5。** 请先读 [Soccer 剩余事项修复与验收记录](SOCCER_COMPLETION_20260911.md)。本次最终 318 项回归、22 项发布测试、14 次本地/线上读取及实际历史参考入库核验通过。固定 174 场参考里程碑已完整；旧历史 PIT 审核因缺少当时证据未能认证或替换，真实 v5 比赛成交/结算仍待实际事件验收。原 World Cup、Macro 和共享前端源码未改。
>
> 下方原有更新及正文按其当时状态保留；其中 v3/61 项等数字不是当前 v5 发布结果。历史冻结规则继续适用，所有测试仍在生产目录之外。

> 当前事实截止到 2026-09-11 04:54:34 UTC。生产安装、v3激活、12模型准备、服务恢复、完整刷新、水位确认、最终14次读取及生产结案均已验证；剩余限制与文档归档状态见末节。

## 1. 当前状态

**最终61个Soccer白名单文件已安装并逐一匹配已验收SHA256，v3前向方法已在生产激活。** 激活时间为 **2026-09-11 04:09:22.068388 UTC（纽约00:09:22 EDT）**，12个联赛模型按真实可用时点准备完成，04:11:23.735871 UTC的生产journal为`verified`。历史book没有替换；安装与激活期间，维护基线 **359场、618条策略腿**的完整记录、原顺序、累计值及六份金融文件保持不变。正常刷新后的验收按同一ledger及完整金融记录核对，不要求整份含动态信息的文件永远字节不变。

最终源码对应[提交4b1858275b5006c26190b2d315131fdac35b5b91](https://github.com/lingxiaoxu/someopark-test-backup-20260910/commit/4b1858275b5006c26190b2d315131fdac35b5b91)，绑定的隔离合同回归为 **318 passed，316.60秒，0 failures/errors/skips**。生产范围核验为 **61/61源码、692/692受保护文件、3/3原Soccer plist**全部匹配。本次没有修改UI、Macro或原World Cup源码，没有替换其服务配置。

v3当前视图于 **04:19:51.821379 UTC** 正常发布，当时生产upcoming为 **959,858字节、168场**；三项原Soccer服务于 **04:21:15.360816 UTC** 恢复。完整wrapper自 **04:20:48.420835 UTC** 开始，已成功exit 0：full_refresh于 **04:51:03.081997 UTC** 完成，required全通过；optional price_ticks和milestone_backfill仍partial，所以full_refresh如实为degraded。外层publish于 **04:51:03.868649 UTC** 完成且为ok。

本次真实published_input、trigger和settle水位均为 **5433**。04:51:45 UTC在完整immutable生产快照上调用已安装decide，返回`SKIP: outside match window (no API call)`；没有重新ack、写库或请求网络。随后 **04:51:53–04:51:54 UTC的14次GET全部HTTP200且完整通过，7类文件两端SHA逐一相同**。359场/618腿的原金融前缀全部精确一致，PDF metadata对齐。04:52:38三服务均loaded，04:52:33的live周期为ok。生产结案凭证于 **04:54:34.717715 UTC** 记录`production_verified`。

本次上线的是未来数据、决策、执行和不可变账本的修复。**没有重算或替换既有历史收益，也没有把缺失证据的历史标为严格PIT。** 本次范围内的生产验收完成，不等于所有场馆已有报价、所有历史资料已恢复，或v3已完成真实比赛交易；这些限制保持明确记录。

## 2. 最终版本与证据入口

- 生产根：`/Users/xuling/code/someopark-test`。
- 隔离维护目录 **PW**：`/private/tmp/soccer-production-20260911T020505Z`。
- 持久备份目录 **PB**：`/Users/xuling/.codex/backups/soccer-production/20260911T020505Z`。
- 原修复包 **R**：`/Users/xuling/.codex/backups/someopark-test/20260910T203538Z/repair-review`。
- 最终源码清单：`PW/evidence/reviewed-source-public-v3.json`，61项，SHA `1b31f4fc3c61b5b6a9176959f769d65f6cad847952994394a056029ce03de92f`。
- 最终测试/源码绑定：`PW/evidence/production-public-v3-final-source-test-binding.json`，测试XML SHA `5e3328f51d89780979ac84afdc00f49dee76f171337e8910dc2e23acbc29db22`。
- 生产源码/保护范围：`PW/evidence/final-production-v3-source-scope.json`，04:10:48 UTC，status=pass。该文件本身只验源码范围；epoch和模型以独立journal为准。
- 生产v3激活：`PB/public-release-v3/journal.json`，phase=verified，12模型。
- v3当前视图：`PB/current-views-v3.json`，04:19:51.821379 UTC，四builder均ok、金融head保持。
- v3服务恢复：`PB/service-restoration-v3.json`，04:21:15.360816 UTC，三项原plist均loaded；同时记录本轮full_refresh起点。
- 最终完整刷新/发布：`PB/full-refresh-v3-completed.json`、`PB/publish-v3-completed.json`；真实确认后检查`PB/trigger-v3-after-full.json`。
- 刷新后来源范围：`PW/evidence/final-production-v3-after-full-source-scope-20260911T045122Z.json`，61/61源码、692/692保护、3原plist全部通过，证据SHA `20264f6ce05266cb97226af365fa7db2d1e346bbe0deed1871e31fe7fc6fb7e5`。
- 最终一致性DB快照：`PB/production-final-v3-snapshot.json`，04:51:25.240611 UTC，631,599,104字节，integrity_check=ok；数据库SHA `cd0b6a984f57ec00f8e2cbfeec63a912a458ef1bd64e1323e7874cbe6b7b0c9d`。
- 最终只读后验：`PW/evidence/postinstall-readonly-20260911T045153Z/{result.json,review.md,review.json}`，04:51:53.376340–04:51:54.586019 UTC，failures=[]。
- 最终服务：`PB/service-post-full-v3.json`，04:52:38.860497 UTC，三服务loaded；live最新04:52:33 ok。
- 生产结案：`PB/production-release-verified.json`，04:54:34.717715 UTC，phase=production_verified，SHA `e241e94c5bf1a07f103cd6d6af87d2029da48a378871c6255cdd349ddf5416c0`。

当前前向身份为：

- `model_version = soccer-observed-pit-v1`。
- `method_version = soccer-timing-integrity-20260911-v3`。
- `epoch_id = 5d2fb2ff1b62d3d7d31b17f4c3afb2e71d8f60202b2dc5fe73a32d57e9a50795`。
- `compatible_epoch_ids = []`；没有把旧腿重标为新epoch。

最终v3是相对v2的四个精确delta：新增`util/public_projection.py`，修改`ops/run_status.py`、`ops/refresh_all.py`、`ops/match_trigger.py`。match_trigger是新纳入清单的既有文件，所以59项安装清单扩为61项，实际只新增一个产品文件。适配器SHA `b72df732845c19b13ad069fc3c756c839ca7d59a3d5f2a8404bdc8c51f3b0669`，policy SHA `fa9a63f7df97991dee1d70234c8f8f77b0ddb77499d272ff83df0ae89a2efa13`；全部旧/新源码SHA在该policy和最终清单内。

## 3. 六项生产实证问题及已安装修复

### 3.1 Demo已终结市场仍被视为open

fixture **1635729 / INPLAY / home** 的`KXUCLGAME-26SEP10SLARCL-SLA`真实回应为`status=finalized`、`result=no`、`settlement_ts=2026-09-11T00:14:22.200527Z`，完整分页持仓中没有该ticker。旧代码仅接受`settled`，导致旧持仓不结算并阻塞安全迁移。

新增`util/demo_settlement.py::terminal_binary_settlement`，统一`exec/demo_forward.py`、`exec/kalshi_mirror.py`及`util/frozen_strategy_store.py`的终态判断：核ticker、合法终态、二元结果、非provisional、真实且不晚于观察时点的settlement时间，以及存在时的结算金额。保留原始`finalized`，没有伪改成`settled`；旧内部settled只有同样证据完整时才兼容。依据：[Kalshi Market Lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle)。

针对这唯一旧订单先GET dry-run，再用已审核证据定向更新本地结算投影，没有买单、卖单或强平请求。2张YES入场现金$0.740000、真实fee $0.032700，毛损益-$0.740000、净损益 **-$0.772700**。只改变目标镜像的`exit_reason / exited_at / pnl_c / raw_json / status / won`，不改paper entry/completion或成交历史；原镜像pnl_c仍是每合约毛收益，净额按真实fills另算。

证据：`PW/evidence/demo-settlement-dry.json`、`demo-finalized-apply.json`、`demo-finalized-apply.result.json`及`PB/demo-before-reconciliation.db`。共享Demo账户余额不代表这笔交易或全量Soccer策略收益。

### 3.2 日常报告未消费completion，研究计算又阻塞金融发布

`performance_report.build(freeze=False)`已是只读预览，但原日常接线没有显式消费已封口paper completion；settle本身不会将其追加进规范book。旧`settle_reports`还在每次结果触发后执行昂贵backtest/OOS，延迟报告。

`ops/settle_reports.py`和`ops/refresh_all.py`现按顺序执行settle、**一次**`consume_completed_paper`、同对象只读预览。绩效JSON、轨迹JSON、PDF使用同一账本。快速settle_reports先发布六份金融产物及overview/watermark，backtest/OOS保留在全量refresh_all。后置`daily_collection.run(limit=12)`用独立状态，partial或失败不撤销已经发布的金融结果；limit是工作量上限，不是假称严格墙钟超时。

首次生产快报告的`settled_bet / strategy_book_append / performance_report.json / milestone_marks.json / performance_report.pdf / frontend_overview.json`六步均成功，输入水位5433。证据：`PW/evidence/financial-schedule-ready.json`、`financial-schedule.patch`、`PB/first-new-settle-reports.log`。

### 3.3 FK Shakhtar Donetsk精确别名遗漏

真实Gamma event **931726** 的fixture **1635708**（PSV–Shakhtar）away标签为`FK Shakhtar Donetsk`，精确目录缺此别名，导致`market_subject_mismatch`。`config/club_identity.json`只在既有Shakhtar记录增加这一行alias，不改canonical ID、显示名或采用模糊匹配。

证据：`PW/evidence/fk-shakhtar-alias/`中的实际payload及17项身份回归，覆盖正确合同唯一解析、错误球队/日期拒绝和既有五语言名称不变。

### 3.4 查询日期上界被截成午夜，漏掉scope内赛事

`ops/backfill_milestones.py::collect`原253–254行将带时区scope.window_start/end截成`[:10]`。真实8次GET A/B保持其余参数相同：上界`2026-09-11`仅返回6赛事；改回原`2026-09-11T02:53:21.651265+00:00`返回8赛事，两个目标各唯一恢复：**1631511** Cienciano–Montevideo City Torque、**1635386** Independiente del Valle–Flamengo，endDate均为`2026-09-11T00:30:00Z`。

修复只传完整原aware范围，不改变target、sample_ts≤target、请求预算或历史reference-only属性。原02:53请求未保存；这次是后来按同参数的真实A/B复现，不伪称原历史回应。目录恢复不保证每个历史价格目标都完整，更不证明当时可成交。

证据：`PW/evidence/global-date-bound-20260911T032040Z/{result.json,requests.json,raw-*.json,review.md}`及`global-date-bound-fix/`。已安装collector SHA `6849b61170820280208a9982c16549f61310d2d62a63fd0e775ab5e926d55746`。

### 3.5 公开upcoming重复携带完整raw收据，膨胀至229MB

v2发布后实际upcoming为 **229,733,169字节、168场**。两端HTTP200，但30MB验收读取上限使这两份文件未读完；其余12份读取成功。没有将它说成服务非200、已证明JSON坏损或全14份验收通过。首场quote_receipts达764,724字节、poly_us 732,222字节、lock_arb 255,588字节。

已安装`util/public_projection.py`及`run_status.atomic_json`的最小边界修复，仅投影configured upcoming/inplay/advance三个公开文件。完整内存不变，PRE先将完整报价保存为私有观察、IP先使用完整内存作paper/demo决策；公开价格、概率、状态、普通明细及selected receipt ID保留。真实收据先内容寻址gzip保存至私有`raw_snapshots/public_quote_captures`、权限0600；原件hash不符则不替换公开文件。归档不生成DB availability marker，也不自证PIT或可成交。

实际保存产物的隔离回放：**229,733,169→959,106字节**，168场非receipt字段相等；667个gzip对象共6,534,893字节，逐一还原hash通过，原内存与六金融文件字节不变。证据：`PW/evidence/upcoming-size-diagnosis.json`、`postinstall-readonly-20260911T034035Z/result.json`、`public-projection/`及`public-projection-independent-review.{md,json}`。959,106是隔离旧产物回放大小，不能预填为随后生产新行情文件的实测大小。

生产正常环境重跑后的实测为 **959,858字节、168场**，PMUS状态99场ok、69场unavailable；缺报价仍如实展示，未为了让状态全绿删去赛事。第一次外部publisher未加载正常`.env`，造成PMUS环境警告；原操作日志与结果完整保留在`PB/current-views-v3-before-env.log/json`。按既有顺序加载root `.env`、随后Soccer `.env`再运行，环境警告消除，04:19:51.821379 UTC发布verified。该首次操作问题没有被覆盖或包装成一次全通过；正常重跑不改变代码或冻结金融记录。

04:51完整全刷后的最终两端GET均读到 **962,896字节、168场、0场缺模型**，公开完整receipt/raw字段计数为0，两端SHA相同。PMUS为99ok/69unavailable、Kalshi为118ok/50unavailable；因此保留quote_unavailable和overview数据提示，不能将成功交付公开产物写成所有场馆均可交易。959,106、959,858、962,896分别是隔离旧产物回放、第一次正常生产视图和最终全刷后的实际大小，不混用三个时点。

### 3.6 degraded发布仍确认旧水位，重复启动全量刷新

03:24:30 UTC批次required全部发布成功，optional history为partial，因此full_refresh正确为degraded。旧`RunStatus.finish`只在ok时更新`last_success_input`；wrapper接受degraded却又确认这个旧值。只读诊断显示终场计数和settle水位为 **5433**，trigger及last_success_input仍 **5431**，因此03:54:49 UTC再次启动全量刷新。

`refresh_all.main`现只在stage.promote成功后写本次`published_input`；`match_trigger.acknowledge_refresh`校验本次attempt、终局ok/degraded、required全部ok、发布时间顺序与非负整数，然后确认该标记，不回退last_success。计数取本轮开始的保守值，避免把长运行期间新来但未进入本批的赛果误确认。last_success仍表达全健康运行，partial不被改绿。独立ack和底层水位写入均受writer维护门禁保护。

证据：`PW/evidence/trigger-repeat-diagnosis.json`、`trigger-published-tests.log/XML`、`trigger-published-independent-review.md`。不能给旧已结束runtime倒填published_input来充当新上线的真实wrapper验收。

修复后的生产正向已完成：本轮84个步骤中required全部ok，只有两项optional partial；04:51:03 UTC成功promote后产生当前attempt的published_input=5433，wrapper随后正常确认trigger=5433，settle水位也为5433。`last_success_input`仍保留旧全健康值5431，这是原语义保持，不是确认再次失败。04:51:45的installed decide在immutable完整快照返回SKIP，init_db/network/ack/writes均false，证明当前相同计数不再被当作未确认结果。这是实际正常wrapper回执加只读判定，没有回填旧状态，也不承诺今后有新赛果时永远不触发。

为进入v3维护窗口，**04:00:20.500421 UTC**在重复任务的`calibration.json`阶段暂停其已核实进程树，保存一致性DB `PB/redundant-refresh-before-v3.db`（SHA `db7d8f2fbd4ecd434f8f996bfc68c4206f3e742c331527d3b3c0daa4151b1e41`），04:00:20.898757 UTC三Soccer服务卸载。该重复批次尚未promote，保留之前已发布的完整数据和金融账本，不涉及下单或强平。证据`PB/redundant-refresh-interruption-v3.json`保留阶段、PID树、原因与备份。它不同于02:40对旧版本报告纯研究阶段的中断，两个事件分别留档。

## 4. 历史账本和持仓保护

维护初期仍有三条旧paper腿：1631511 PRE draw $0.95和IP draw $0.80、1635386 PRE away $1.00。保留旧live直至实际FT，由旧方法自然完成，没有强平、改旧entry或重标epoch。最后completion在旧报告研究阶段产生；先保存一致性DB和状态，再于02:40:18 UTC停止已确认的纯历史研究进程，随后通过旧版窄报告入口消费最后completion并发布，形成359/618安装基线。

证据：`PB/service-quiescence.json`、`legacy-report-interruption.json`、`legacy-report-before-interruption.db`、`legacy-final-financial-publish.log`、`PW/evidence/preinstall-performance-359.json`。旧审计352/353/357与当前359之间包含正常前向结算追加，不能单凭总数变化推断历史被重写。

本次各生产维护阶段保持的身份：

- `book_version_id = 00ffda382ea41f3e4fec8b19b45226b51db39e864afddb91a01753cb595c514e`。
- `ledger_id / records SHA256 = bf1bbf9c0952bb679aa0bf3e0a8ed7e5a373ba85686a709aafc38be200be6dc6`。
- `as_of = 2026-09-11T02:31:09.992402+00:00`，359场、618腿。
- 金融表组合hash：`4b39eb32aca7fbb365efd21f3e0e70c590e559254e7364269d09ed93327992ba`。

六文件为两个目录各一份performance_report.json、milestone_marks.json、performance_report.pdf。**v3安装/激活维护快照**记录两份JSON/PDF分别保持相同SHA：performance `5acd847852fcce26b48dbb32033d942b5f7101ef70c5a5304271558eb7a992ed`、marks `12e154a3136ad4ce781834fcbca1fe6e1b16506466dde198927abb3226244702`、PDF `2857110438143b178c3d8fad803ae272ce04d5a56e3e79a695ee9990d4373a28`。这些不是声称04:51正常全刷后的文件仍全字节相同。早期线上完整核验逐行对齐bet_log、marks records、strategy_record、原前缀和28页PDF metadata；本轮正常发布后的验收沿用同ledger/记录一致口径，并另核当时两端副本，而不是只比较最后利润。

最终正常全刷后的后验已通过：performance和marks携带完整相同ledger；bet_log、marks records及每行strategy_record均精确一致，359条原前缀、618腿及三累计保持，28页PDF metadata与ledger匹配。正常生成后的文件SHA与维护快照不同，验收的是金融记录不变及当时两端文件相同；没有把“六文件在维护期间不变”扩大为“每次正常刷新全部字节永远不变”。价格参考专属赛事16场，仍未计作策略下注。

账面纸面毛收益 **+$116.821**（PRE +$35.042、IP +$81.779）仅表示冻结基线保留，不是新认证的可实现利润，也不是Demo净收益。price-only赛事不补成策略交易。未来正常追加应核对旧前缀不变，不要求总记录永远359。

## 5. 前向激活、来源和恢复门禁

生产bootstrap保存16个模型输入表的完整来源基线，以本次真实可用时间登记。ClubElo检查OK、0 fail/0 warn；55国家覆盖、56请求全200、解析911俱乐部。12模型均先构建，再于实际后续cutoff证明可用；没有倒填午夜或过去的model_available_at。

初始覆盖为EPL20、LaLiga20、SerieA20、Bundesliga18、Ligue1 18、UCL81、UEL76、UECL165、Libertadores47、Sudamericana57、Brasileirão20、Argentina30。欧战这里包含资格赛的当前强度roster，不代表主赛程有81/76/165队。v1、v2和最新v3各自有12模型可用性证据；当前v3以`PB/public-release-v3/journal.json`为准。

安装和激活分两个进程；三legacy锁加持久maintenance门禁，要求无旧open、pending intent和未消费completion，核当前book、金融表、六文件、完整源码/aliases集合及实际参数。目标manifest先写journal，才commit新epoch；commit后崩溃按真实active状态继续，不重复激活、不伪造成功。全部12模型准备通过后才解除门禁。v3只允许四精确delta，只有helper是新文件；match_trigger由完整source policy绑定，不虚构runtime依赖。rules、parameters、identity及compatible=[]门禁保持。

没有用假隔离标记运行生产研究切书，也没有自动回滚已commit的epoch。**SQLite与六个文件不是联合原子事务**；采用排他维护、持久阶段、逐文件原子替换和复核恢复。需要恢复时必须先核实际book/epoch/未决/新completion/六文件，再按记录协调处理，不能只回滚源码或用旧DB覆盖新金融事实。证据：生产各阶段journal、`PW/evidence/public-v3-fourdelta-independent-review.md`及`public-v3-adapter-readiness.json`。

## 6. 实际隔离验收

- **最终61来源合同套件：318 passed，316.60秒**，无fail/error/skip；来源及提交绑定见第2节。
- **最终四delta适配器：18 passed，106.32秒**，`public-v3-with-ack-final-tests.*`；真实v2副本→v3、12模型、359与六文件保持、四替换点恢复、commit后实际`os._exit`及幂等恢复。
- **发布确认接线：18 passed，0.09秒**，`trigger-published-tests.*`；实际main/RunStatus/ExportStage，注入小型builder，验证degraded确认、新赛果保留、失败不确认、非法标记和维护拒绝。
- **公开投影：14 passed，7.47秒**，`public-projection/tests.*`；实际229MB回放、字段/内存/私有原件/六文件保持及失败拒绝。
- **终态与金融调度：31 passed，1.05秒**，28终态/门禁加3真实调度路径；其中确定性PDF renderer替身不冒充实际PDF视觉验收。
- **别名/日期/provider回归：124 passed，8.86秒**，17 alias、8日期、99 provider/ops，`global-date-bound-fix/tests.*`；历史价格正向使用明确合成schema，真实保存目录只证明发现与身份。

这些组有交集，不简单相加成唯一用例总数。测试均在隔离目录/DB与禁止生产访问、联网的约束下执行。318是已审合同套件，不代表原仓库全部旧断言仍成立：原412记录为378通过、34旧合同断言失败，基线391/21及差异说明保留在原修复执行记录，不用新套件掩盖。

分轮失败也保留：v2适配器12项通过后一个测试参数名写错，仅修外部测试后1项通过；最初日期测试误用未FT快照被正确拒绝；最初投影测试过度要求整份含receipt decision相等，仅修外部断言后14项通过。中间v3两delta的16项/60来源/078b50a提交均为历史准备证据，不替代最终四delta。此前318的308.98秒、319.04秒两轮及bootstrap/coordinator结果也在原证据中，不与最终重复累计。

隔离目录整理记录在`PW/evidence/completed-test-scratch-cleanup.json`：只清理9个已经结束的中间测试目录，保留日志、原始备份、固定输入及最终测试，释放约25.65GB。没有清理生产或用删除测试失败证据来改变验收结果。

## 7. 已发生的生产阶段

1. **02:17–02:40 UTC，排空旧方法。** refresh/trigger先停，live等待旧比赛FT后02:33:25停止；保存状态与DB后02:40:18停止旧报告纯研究段，正常消费最后completion，359/618作为维护基线。
2. **02:52–02:55 UTC，v1安装与验证。** 59文件安装；epoch `9588814b50a236b823528f12162e7dcd59632a0a517d1c8cca300ac505a28fe1` 于02:52:37.480815激活，12模型02:52:55验证；02:54:08恢复3服务。02:55首轮14GET全200、7文件两端完整字节相等、金融/PDF一致。Demo enabled/environment=true，open/pending/error为0；普通真钱开关false，Demo每单$2上限与普通$1 hard cap分开记录。
3. **03:24 UTC，首次全量发布完成。** required全部通过，optional price_ticks/milestone_backfill partial，状态degraded；DEF当前参数保留，研究建议未自动采用。证据`PB/first-new-full_refresh-completed.json`及完整日志。
4. **03:32–03:40 UTC，v2目录修复。** 03:32:35暂停3服务；两文件安装，epoch `7b7f197a19c350a89d332b194eef4ae2e992453a7124068a11e4fb1c5f8b72d3` 03:33:35.445012激活、03:35:29.675532验证12模型。03:38:44当前视图完成，03:39:47恢复3服务。03:40读验12份完整通过，2份upcoming超30MB停止，暴露第3.5项。
5. **03:54–04:00 UTC，重复触发与安全中断。** 旧trigger水位5431落后已发布5433，再次全刷；04:00:20在calibration阶段暂停已核实进程树、保存一致DB并卸载三服务，重复批次未promote，详见第3.6项。不是为了验收人为启动重复任务。
6. **04:09–04:11 UTC，最终v3安装与激活。** 四精确delta、总61来源安装；04:10:48核61源码/692保护/3原plist匹配。epoch `5d2fb2ff1b62d3d7d31b17f4c3afb2e71d8f60202b2dc5fe73a32d57e9a50795` 于04:09:22.068388激活，04:11:23.735871完成12模型、journal verified，金融基线不变。
7. **04:19 UTC，v3当前视图正常发布。** 首次publisher漏载env的原日志/结果保留；按root env→Soccer env正常环境重跑，04:19:51.821379四builder均ok、financial head保持，upcoming实测959,858字节/168场，PMUS99ok/69unavailable。
8. **04:20–04:21 UTC，完整wrapper启动与原服务恢复。** 完整wrapper于04:20:48.420835开始；04:21:15.360816三项原plist恢复loaded。
9. **04:51 UTC，完整刷新和发布确认完成。** full_refresh于04:51:03.081997完成，required全部ok、两项optional partial如实degraded；外层publish于04:51:03.868649为ok、进程exit 0。published_input/trigger/settle均5433；04:51:25形成631,599,104字节完整DB快照且integrity ok，04:51:45只读installed decide返回SKIP。04:51:22源码/保护/plist范围复核再次全部通过。
10. **04:51–04:54 UTC，最终后验和结案。** 04:51:53–54全部14GET完整通过，7类文件两端相同、账本359/618前缀及PDF对齐；61来源、3个epoch、当前v3及12观察模型通过。04:52:38三服务loaded、trigger和live最近退出均0、live已运行32次且最新周期ok；daily尚未到下次日程，waiting正常。04:54:34生成production_verified凭证，并让coordinator引用实际各阶段证据，原source-only journal保留。

最终风险来源时间为04:50:31.806100 UTC：Demo enabled/environment=true、共享账户cash **$1265.52**，open/pending为0，普通真钱开关false。报告中的last_cycle仍是 **03:29:24 UTC**，该历史cycle error_count=0，不能据此声称v3已经实际执行过订单。最终快照显示当前v3 paper entry=0、demo intent=0；现场无live比赛，真实新epoch成交及完整结算尚未发生。

关键历史凭证包括`PB/service-restoration.json`、`service-restoration-v2.json`、`current-views-v2.json`及两个postinstall-readonly目录。旧阶段事实不改写为最终阶段完成。原完整编年草稿另存`PW/evidence/PRODUCTION_REPAIR_20260910_BEFORE_READABLE.md`。

## 8. P00–P13实际覆盖与能力边界

主体修复来源为[原隔离实施记录](/Users/xuling/.codex/backups/someopark-test/20260910T203538Z/repair-review/work/prediction_market_soccer/REPAIR_EXECUTION_20260910.md)及[开发计划](/Users/xuling/.codex/backups/someopark-test/20260910T203538Z/repair-review/work/prediction_market_soccer/DEV_PLAN_PIT_DATA_INTEGRITY_20260910.md)。原记录的当时“未应用生产”状态保留，本文件补充后来真实执行；函数、测试及32项问题关闭边界仍以原记录和各closeout为准，不把计划所有待办一律勾成完成。

- **P00 备份/隔离：** `run_isolated.py`、`research_inputs.SourceSnapshot/CandidateWriter`绑定独立源与写入根；`test_t00_isolation`、`test_ops_candidate`验证生产读取/越界写/联网拒绝。GitHub及一致DB先备份，生产另有PB源码/DB/plists/六文件和61来源/692保护后验。
- **P01 破坏性重冻退役：** `ops/_refreeze_ip.py`、`settle_bets.backfill_inplay`、`frozen_strategy_store`拒绝破坏性历史apply；`test_ops_candidate`、`test_perf_paper/report`验证。生产没有DROP、清空inplay_json或删旧价格，359/618全前缀保持。
- **P02 比赛时间/阶段/事件：** `match_timeline`、`backfill_milestones`、`smart_exit`区分比赛分钟、真实观察和近似墙钟；`test_ops_candidate`、`test_root_source_history`覆盖补时、事件修订与Missed Penalty不计进球。历史缺真实下半场起点仍不可认证。
- **P03 因果历史采样：** `price_history`、`backfill_price_ticks*`、`rederive_milestone_prices`使用显式窗口、最后一个≤target样本及180秒容差，保存sample时间/密度；`test_ops_candidate`、`test_data_provider_receipts`验证。v2目录aware范围另有真实A/B，历史单价仍不能成为BBO。
- **P04 报价/选价一致：** `quote_evidence`、`upcoming_export`、`inplay_arb*`、`paper_store`、`demo_forward`共用selected receipt，PRE选边/场馆/仓位同价，IP保留优先顺序，退出必须同合同bid。quote/provider/consumer及paper/demo正向和拒绝回归通过；v3投影14项证明只缩公开载荷，内部证据不删。
- **P05 合同/球队身份：** `market_identity`、`club_identity`及各venue discovery/reader绑定fixture/date/赛事/environment/market_kind/period/line/side和唯一token；重复/冲突拒绝。quote/provider/demo回归及FK Shakhtar真实payload回放验证，不引入模糊匹配。
- **P06 名称/采集完整性：** `bootstrap_aliases`、`collection_state`、`daily_collection`、`live_refresh/run_status`持久化固定fixture×target、退避和共享预算；alias/provider/collection/candidate回归覆盖。生产optional partial如实保留；v3发布确认18项把已发布水位与采集全健康分开，不能承诺全部历史可恢复。
- **P07 可得时间/原件：** `source_history`、`ingest/store/soccer_ingest`、`timing_provenance/paper_store`在原件/投影提交后独立登记availability；source/model/paper/quote回归验证时序、原件和修订。bootstrap使用本次真实时间；旧无证据记录及私有公开收据归档均不自证PIT。
- **P08 模型/参数/校准：** `observed_strength`、`club_prior/fc_ingest`、`forward_methods`和`settle_bets._pit_cal`形成只读as_of与不可变日模型；`test_root_source_inputs_matrix/calibration_cutoff`及observed_model/epochs/paper验证实际A→B输入、loaded参数/hash和结果可得时间。v3生产12模型真实availability通过，研究建议不自动改DEF。
- **P09 独立候选/范围：** `research_inputs`、`rederive_milestone_prices`、`strategy_candidate`保存不可变run/scope/hash，缺输入返回typed unavailable；`test_ops_candidate`、`test_perf_report`验证无旧价fallback。生产不把候选直接写旧book，不以缺输入=0收益补分母。
- **P10 双对照/财务重算：** `strategy_candidate.build_candidate`、`decision_backtest`、`smart_exit*`提供固定原腿敏感性和完整重新决策两条研究路径，`test_ops_candidate`验证真实hybrid规则及缺输入处理。全历史严格PIT收益仍不可估；未生产全历史重跑/替换，也不把收益下降当验收指标。
- **P11 认证/切换/恢复：** `version_workflow`、`maintenance_gate`、`frozen_strategy_store`、`forward_replay_certification`分离登记/激活，核source/receipt/模型/未决/六文件；maintenance/recovery/certification回归及最终18适配器场景验证。生产采用独立前向epoch协调器；窄的完整已观察前向精确回放可隔离认证→登记→切书→同源marks，不能凭flags认证任意新模型历史。
- **P12 单一账本/Demo子集：** `performance_report`、`milestone_export`、`frozen_strategy_store`、`demo_forward`共用冻结ledger，preview无consume；paper/report/demo回归验证完整字段、顺序及三累计。现场3项调度回归再验证显式一次consume与快速金融发布，生产359/618保持；Demo真实fill/fee是执行子集，不能代替全paper。
- **P13 独立前向epoch：** `forward_methods`、`paper_store/paper_trading`、`demo_forward`及`version_workflow`绑定entry的book/epoch/runtime/参数，epochs/paper/demo/maintenance回归覆盖旧open及未知/部分intent拒绝。旧仓位自然排空、不强平、不假装旧Python可用，v1/v2/v3均有实际激活凭证。

**跨模块影响已单独核对。** 时间和身份(P02/P03/P05)先进入可得来源(P07)，再约束模型/校准(P08)和同一报价选择(P04)；实际entry/exit携带epoch(P13)，最终只向不可变book追加(P12)，不会因换版本重新计算旧前缀。候选研究(P09/P10)与正式认证/激活(P11)隔离，缺证据不会被包装为零收益或以布尔flag绕过。

日常采集(P06)的partial不被第3.6项改成ok：新标记只说明本批required产物已发布，和采集完成时间、last_success是不同事实。公开投影同样只影响传输层，未成为paper/demo输入，六金融文件不走该投影。运行依赖变化通过独立v3激活及12模型准备处理；原UI/Macro/WorldCup源码、三项原plist和历史金融事实继续分别以哈希核验，数据产物变化不误算成额外产品修改。

原历史调查353×2=706轨道范围完整，pending=0，但entered=0/no_edge=0/unavailable=706，表示证据不足，不是706条零收益。候选仍strict_pit_certified=false、activation_eligible=false；本次没有替换旧book或重标旧腿。窄认证只能验证完整已观察前向记录的精确回放，不能补回旧353场缺失来源，也不能认证任意新模型历史重判。

这里同时有软件能力范围限制，不能全部归结为资料缺失：通用研究构建器尚无“任意新模型历史重新决策”的独立严格认证协议。已验收的窄入口只允许保持源模型身份的完整已观察前向精确回放，不重新选边、不补no-edge或缺失腿；即使给通用候选改写认证flags，也不得激活为正式历史book。

证据不足不等同于全部历史收益错误；未逐笔证明的盈亏变化不推断。上线时没有live比赛，已证明正常无比赛cycle、实际模型可用时间和隔离正向生命周期，尚未观察新epoch真实比赛成交和完整结算。未来缺价格/模型保持等待或明确missed-data，不伪造no-edge、亏损或成交。

## 9. 生产验收结案与归档

本次生产运行与数据交付验收完成，依据如下。报价缺失、历史采集partial、旧历史证据不足、通用任意新模型历史重判认证未实现、v3尚无真实比赛成交的限制继续保留，不将原计划全部事项无条件勾成完成。

- [x] **新完整wrapper与ack：** 本轮exit 0，04:51:03完整发布完成，required全通过、两项optional仍partial/degraded；真实published_input及trigger/settle均5433，04:51:45当前完整快照只读decide为SKIP，无网络/写入/重复ack。没有倒填旧状态。
- [x] **恢复后的运行状态：** `PB/service-post-full-v3.json`三服务loaded，trigger/live last_exit=0，live最新04:52:33 ok；daily未到日程正常waiting。Demo最新余额来源与旧cycle时间分开记录，普通真钱开关仍关闭，没有把无live时0条交易说成实际前向成交验收。
- [x] **最终本地/远端GET：** 04:51:53.376340–04:51:54.586019 UTC，7文件×2端全部HTTP200、完整读取，JSON严格合法，两端SHA相同，359/618原金融前缀和28页PDF metadata对齐，failures=[]。将来正常追加核旧前缀，不要求总数永远359。
- [x] **生产结案凭证：** `PB/production-release-verified.json`于04:54:34.717715 UTC为production_verified，SHA `e241e94c5bf1a07f103cd6d6af87d2029da48a378871c6255cdd349ddf5416c0`。`PB/coordinator/journal-source-install.json`保留原始完整journal；当前journal已为production_verified并引用结案凭证和原始SHA，不用source-only阶段冒称激活成功。
- [x] **文档归档：** 两份Soccer MD已归档到本模块开发计划目录，并纳入私有GitHub修复分支；实际文件SHA、时间与文档提交由独立`PB/documentation-release-v3.json`记录。源码验收仍对应4b185827提交，不改已绑定生产结案凭证的SHA。

本文件归档于生产Soccer模块，并在PB/evidence保留副本；原计划正文和生产前备份保留。后续正常金融状态继续推进时，任何恢复均须保护新增记录和未决订单，不能以旧备份覆盖或静默强平。
