# Soccer PIT、取价、球队身份与冻结账本修复开发计划

> **最新发布（2026-09-11 08:53 UTC）：生产当前为 v7。** 请先读 [Soccer 队名与 Kalshi 报价修复记录](SOCCER_UI_QUOTES_20260911.md)。截图六场 Kalshi 三路报价及真实 UI 已通过验收；五语言身份目录已同步，中文四个球队词条已修正。本轮未改变布局、World Cup 或 Macro，开发与测试均在 `/tmp`。359 场、618 条策略腿继续冻结；旧历史 PIT 证据缺失及新版本真实成交/结算尚待验收的边界不变。
>
> 下方 v5、v3 等“当前”表述是此前阶段的原始记录；以最新链接的 v7 发布凭证为准。

> **最新续修记录（2026-09-11 07:28 UTC）：生产当前为 v5。** 请先读 [Soccer 剩余事项修复与验收记录](SOCCER_COMPLETION_20260911.md)。本次最终 318 项回归、22 项发布测试、14 次本地/线上读取及实际历史参考入库核验通过。固定 174 场参考里程碑已完整；旧历史 PIT 审核因缺少当时证据未能认证或替换，真实 v5 比赛成交/结算仍待实际事件验收。原 World Cup、Macro 和共享前端源码未改。
>
> 下方原有更新及正文按其当时状态保留；其中 v3/61 项等数字不是当前 v5 发布结果。历史冻结规则继续适用，所有测试仍在生产目录之外。

> **执行更新（2026-09-11）：Soccer前向修复已应用生产并完成本轮验收。** 最终61项源码、318项隔离回归、完整刷新与发布水位、14次本地/线上读取均已核验；World Cup、Macro和共享前端源码未改动。详见 [生产执行记录](PRODUCTION_REPAIR_20260910.md)。
>
> 历史book保持冻结，未将旧收益重标为严格PIT、未替换历史；历史补采和部分场馆报价仍有不可用项。通用任意新模型历史重判的严格认证能力仍不具备，不能绕过门禁切换候选。具体限制与证据见执行记录第8节。
>
> 以下完整保留2026-09-10的原始审计与计划。“PLAN ONLY”“本轮仅文档”“待执行”及原复选框描述当时状态；随后用户明确授权了Soccer生产应用。开发和测试仍在隔离目录完成，原计划不回写成无条件全部完成的清单；实际执行状态以链接记录为准。

---

> 状态：**PLAN ONLY / 二审修订，待后续执行**。2026-09-10 二次审核补齐调用链、输入输出契约、依赖、隔离与端到端验收；本轮只改本文档，未修产品、未迁移数据、未重算或替换历史收益。
>
> 创建日期：2026-09-10。原审计数据库快照：2026-09-10 19:07:42 UTC（美东 15:07:42）；两个陷阱再次只读复查：2026-09-10 19:51:35 UTC（美东 15:51:35）。下文行号对应本次复查源码，执行时须重新定位函数，不盲用旧行号。
>
> 放置依据：本模块 README 指定 [TRANSFORM_PLAN.md](TRANSFORM_PLAN.md) 为 Soccer 开发蓝图；本计划与它、PLAN_AUDIT.md 同目录。世界杯的 `.claude/plan/prediction-market-plan/` 是原始模板文档，不在此处改写。

阅读入口：[修复顺序](#execution-order) · [隔离底座](#p00) · [实现合同](#interfaces) · [17组验收](#acceptance) · [问题关闭标准](#closure)。

## 0. 任务边界与不可违反的约束

1. **只处理 `prediction_market_soccer/`。原始 `prediction_market/` 的前端、后端、数据、任务、模板文档全部保持静态。** Macro 及其他模块不在本计划范围。
2. **本轮只有文档工作。** 下列修改、测试、研究重算、版本切换均为后续待办，不能因本文件存在而视为已经执行或批准替换历史。
3. 历史必须冻结。修复报价采集代码不等于授权重新写过去下注；只有用户明确变更模型/交易方法并要求重新回测、替换，才可走第 5 节的版本流程。不能仅换版本名字来绕过这一约束。
4. 未来 PRE 必须在开球前记录，IP 必须在实际赛中观察后作决策；不得在看到终场后补造“前向”纸面交易。Demo 在有合法盘口及符合现有执行条件时尝试镜像，但成交子集不能替代全模型纸面记录。
5. 准确度盈亏、价格轨迹、PnL report 的策略记录保持同一冻结账本、同一排序与累计口径。Demo 的成交/费用另有执行记录，不能把未成交当亏损，也不能把 Demo PnL 混入全模型 PnL。
6. **不添加产品 UI 文案、证据区块、历史来源标签或 Demo 覆盖标签。** 本计划的分类、未知时间、身份校验、研究版本证据仅存后端记录与开发报告。
7. **所有代码实现、修复补丁、测试、假数据、候选数据库、模型缓存、研究输出都在生产目录外完成。** `/Users/xuling/code/someopark-test` 虽名含test，仍按生产根保护；当前只允许更新用户指定的计划MD。实现成果止于隔离补丁与验收证据，不能自动复制代码、数据库或产物回生产。后续生产迁移属于另行明确指令的阶段。
8. 不执行旧的删除 candlestick、清空 `inplay_json`、DROP 重冻流程。未验证的价格不得补零或伪造 bid/ask；缺少历史可得时间不得反填成开球/决策时间。
9. 现有未提交改动很多，后续每个补丁必须基于当时工作树比对，不能 checkout/revert 整文件抹掉其他工作。

## 1. 证据、定义与完成标准

完整原始审计与 26 项证据位于：

- [26 项核验报告](/Users/xuling/.codex/reviews/soccer-assessment-20260910/soccer-assessment-review.md)。
- [结构化核验清单](/Users/xuling/.codex/reviews/soccer-assessment-20260910/soccer-assessment-review.json)。
- [时钟与价格源码审计](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/clock-prices.md)。
- [账本与重建安全审计](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/ledger-safety.md)。
- [数据质量审计](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/data-quality.md)。
- [本轮两个陷阱只读复查、源码哈希](/Users/xuling/.codex/reviews/soccer-assessment-20260910/evidence/plan-traps-recheck-20260910.json)。
- 二审逐调用链报告：[时钟/候选/重试](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/second-review-clock.md)、[报价/身份/来源版本](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/second-review-data.md)、[账本/发布/旧持仓](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/second-review-ledger.md)。
- [二审文档覆盖、链接与237份Python源码不变校验](/Users/xuling/.codex/reviews/soccer-assessment-20260910/evidence/plan-review-second-validation.json)；这是文档校验，不是产品测试结果。

以上是本地证据路径，不是已公开网站；数据库、密钥和原始账户数据不拷入计划目录。本计划保留关键事实，不依赖以后还能获取完全相同的网络响应。

用词约定：

- **已证实缺陷**：有源码路径、固定数据或隔离反例；不自动表示每一条生产交易都触发过。
- **已存在修正**：当前主调用已有保护，需要回归而不是重复“修复”。
- **待补证**：缺少原样本、真实盘口或首次可用时间；保持未知，不能假设为安全或不安全。
- **完成一项修复**：代码改动、隔离验收、影响范围和剩余限制均入档。历史重算、生产替换有各自独立状态，不能把“测试过了”写成“历史修好了”。

## 2. 用户特别要求再次检查的两个陷阱

### 2.1 陷阱一：修代码后普通回填不会改已有价格——仍在，但 SQL 已变

**当前源码事实：**

- [ops/backfill_milestones.py](ops/backfill_milestones.py#L157) `backfill()` 默认 `since_days=14`；fixture 边界在 173–178、205–210 行，Gamma 事件日期由这个范围推导（179–186 行）。
- [285–306 行](ops/backfill_milestones.py#L285) 当前执行的是 `ON CONFLICT(fixture_api_id, milestone) DO NOTHING`，并非原文所说 `COALESCE`。已有行连空字段也不补。
- `force=True` 只绕开 FT 已存在的跳过条件（211–217 行），不会改变冲突策略，不能当作覆盖修复开关。
- 头部/附近注释仍有 “INSERT OR REPLACE”“fill only the gaps”“UPDATE-only”等旧描述，必须按实际 SQL 判断。
- `backfill_advance_pre()` 当前把新观察追加至 `milestone_observation`，不是原地改 PRE。

**最新只读数据：** candlestick 为 1,763 行/359 场；live 为 812 行/160 场；raw `settled_bet` 为 366 行。这些与原文 1,441/222、726、338/145 不是同一时间快照，禁止混用。

旧 T60 目标仍为墙钟 60 分钟的 202 行、旧 T75 目标仍为墙钟 75 分钟的 207 行再次核实未变。目标 `ts` 不等于当时真正选中历史价格点的时间，旧 nearest 可能选前后邻居。

**结论与后续处置：**

- “普通回填不会修存量”仍成立，而且保护更严格。保持正常回填不覆盖历史的原则。
- 原文“必须先删 candlestick 再跑”不是正确修法。删除后将依赖当前日期窗口、事件可发现性、名字映射、token 与行情可恢复性；45 天窗口也不保证完整。
- 原 candlestick 行可能仍含需要保存的其他场馆/模型上下文，不能用来源字段推断整行可丢弃。
- 对应待办：P01 防误执行，P02/P03 时间与行情，P06 不完整回填，P09 候选重建；历史替换只走 P11。

### 2.2 陷阱二：DROP 重冻仍危险；“只清空赛中腿”今天也不可用

**当前源码事实：**

- [ops/_refreeze_ip.py](ops/_refreeze_ip.py#L11) 顶层 11–15 行初始化数据库、`DROP TABLE IF EXISTS settled_bet`、立即 commit、再初始化和结算；没有 `if __name__ == '__main__'` 防护。**导入也有副作用。** 35–47 行还会直接写报告和 PDF。
- [ops/settle_bets.py](ops/settle_bets.py#L303) `freeze_settled_bets()` 现在只调用 `paper_store.settle()`，结算已有前向 entry。它不再按旧的近 14 天逻辑重建历史 `settled_bet`。
- 因此今天运行 DROP 脚本，**不会靠当前结算函数恢复原 366 条 raw 历史**；不能只声称损失旧的 145 条。独立 canonical 冻结 book 不由 raw 表驱动，但这不使删除 raw 变安全。
- [333–341 行](ops/settle_bets.py#L333) `backfill_inplay(dry_run=False)` 明确报错；默认只统计缺失、不写入。CLI `--backfill-inplay --apply` 在 353–354 行拒绝。
- 直接把 raw `inplay_json` 清空只会破坏原始记录；[performance_report.build()](ops/performance_report.py#L893) 读取独立 frozen book，旧 UPDATE 不能让公开账本得到修正。

**旧 145 条/43% 的限定：** 9 月 7 日备份中，8 月 24 日日期界线前确有 145/338 条；若按当时 23:22 精确滚动 14 天，分母里的相应计数是 152。这个旧统计不能当今天固定损失数。

**结论与后续处置：**

- 将 `_refreeze_ip.py` 改为无副作用的退役入口，导入不读写库，CLI 在连接库之前明确终止，不把旧 DROP 藏到 main 后继续允许执行。
- 保留 `backfill_inplay` 禁止历史写入及现有冻结触发器；不要为方便重算重新开放 `--apply`。
- 需要研究对照时，另建候选副本与版本化结果；不是清空当前 `settled_bet`。候选结果未来只有满足 P11 的模型/方法变更和替换要求后才能切换。

### 2.3 原文“四步改动”的当前状态

1. **T60/T75 +15、共用半场常量：已在主代码存在。** `backfill_milestones._MILESTONES` 64–76 行引用 `smart_exit._HALFTIME_WALL_MIN`；但固定 15 分钟只是近似，旧行也没有因此更新。HT/补时/事件时间要按 P02 继续解决。
2. **给取数点显式时间窗：主 milestone 回填、advance PRE 和 rederive 已有；独立 price_tick/advance tick 路径仍有 `interval='max'`。** 按 P03 统一，不能宣称所有采样路径已修。
3. **causal=True、180 秒：全部三个业务 `price_at` 调用已有。** 函数默认仍为 nearest/900 秒；P03 将阻止未来调用漏参数，并保留真实 sample_ts。只取过去价格仍不能证明新进球出现后旧报价可成交。
4. **按原“安全路径”重 derive：今天不能执行。** 要先修身份、完整性、价格来源与候选写入，再走 P09/P11。修正案例 19:15/44.5¢ 是特定保存行情与近似时钟版本的回归样本，不能硬编码成真实哨声或普适成交价。

### 2.4 原文收益估计不是修复目标

保留原文估计以免以后混淆：IP 8008→1800–3100¢；PRE 择时实现 3498→1490–2440¢；PRE hold 773¢不变；合计 11506→3500–5500¢。这些没有获得同一最终重算清单认证。

实际 9 月 7 日持久备份为 PRE 实现 3343.3¢、IP 7847.9¢、hold 618.1¢、合计 11191.2¢。本轮 19:07 快照 canonical 为 353 场/608 腿，合计 11861.1¢；这又是另一口径与时间。原报告 3343→2858 的后值尚未独立核实。

**PRE 的 40 条抽样中有 8 条价格点晚于 PRE 目标 10–17 秒，且另有真实 token 错配，不能预设 PRE 完全不变。** 80 条关联旧价交易腿的 6834¢ 是原收益，不是确定多赚的金额。不得按“收益下降到预期区间”挑修法、删样本或验收。

## 3. 分项代码修复工作包（全部待执行）

<a id="p00"></a>

### P00 — 在任何修复前建立可验证的生产外隔离

**覆盖：全部工作包。优先级 P0；所有实施的硬前置。**

当前路径来源：[config/config.py](config/config.py#L18) 的PROJECT_ROOT/REPO_ROOT/Paths、[store.DB_PATH/RAW_DIR](ingest/store.py#L27)、[store.connect](ingest/store.py#L352)、[proc_lock.acquire](ops/proc_lock.py#L13)、[ExportStage.__enter__](ops/export_stage.py#L23)、[config._apply_selected_params](config/config.py#L427)。

- [ ] 实施时在 `/Users/xuling/.codex/workspaces/soccer-pit-fix/<run_id>/` 建独立工作根，源码在`work/prediction_market_soccer`，测试在`tests_external`，固定证据在`evidence`，不可变源快照在`input`，候选在`candidate`，构建输出在`artifacts`。此路径是未来约定，本轮未创建工作副本。
- [ ] 基线必须包含当时已提交、未提交、未跟踪的相关源码及必要固定输入；不能只checkout HEAD而丢掉当前已修代码。以文件清单+SHA校验复制，不复制.env/私钥、不复用生产symlink/hardlink、不复制可触发部署的本机任务配置到运行位置。
- [ ] 首次导入产品前启用进程级写入边界，允许写入仅工作根；生产仓库、World Cup、真实前端数据、LaunchAgents、账户配置不可写，网络/真实broker默认关闭。不能只依赖“记得传临时conn”。边界同时覆盖子进程和Python字节码/缓存。测试进程使用环境变量白名单，清除继承的真实凭证和交易/部署开关，不打印值；不复制.env并不代表环境无凭证。
- [ ] 拟新增隔离启动器与`util/runtime_paths.py`纯路径校验（名称为待实现），导入前检查所有路径realpath及文件关系；导入后核对模块`__file__`、CONFIG全部path、store.DB_PATH/RAW_DIR、kalshi_mirror._LOG、PIT缓存、发布锁都属于工作根。路径在导入时缓存，不能事后只改CONFIG便宣称已重定向。
- [ ] `CONFIG`在导入时读取`param_selected.json`，候选必须显式装载manifest所列参数；关闭动态override时同时注入基线参数，不能退回默认值而声称“同模型”。所有参数/priors/cache命中进入实际输入清单。
- [ ] T00必须先通过：故意传生产DB/输出路径、间接symlink、错误PYTHONPATH、子进程写生产、默认连接、默认ExportStage路径时，均在写入前拒绝；合法隔离连接正常。可先在工作根实现隔离启动器；通过后才开始其余产品修复或测试。写入拒绝样例用系统边界拦截，不以真的写入生产来探测。
- [ ] 完成时交付隔离源码diff/patch、输入与结果manifest和测试证据；没有自动rsync、部署、重启或生产数据库apply步骤。生产正常任务可能自己追加数据，验证用写入审计与源快照哈希，不能靠暂停生产来制造不变结果。

<a id="p01"></a>

### P01 — 退役危险入口、保住不可变历史

**覆盖：C11、C12、C16、C17。优先级 P0。**

代码：`ops/_refreeze_ip.py` 顶层；`ops/settle_bets.py::freeze_settled_bets/backfill_inplay/main`；`util/frozen_strategy_store.py::ensure/read_book`；`util/paper_store.py::ensure`。

- [ ] `_refreeze_ip.py` 导入不执行 store 初始化、SQL、导出或交易；CLI 只说明已退役并退出。不要采用“加 main guard 后仍保留破坏命令”的半修复。
- [ ] 更新旧注释与 CLI 描述，明确历史回填与前向结算是两套路径；保留禁用历史补造交易的判断。
- [ ] 搜索所有 Soccer 调用者、脚本及任务文件，确认没有正常管线依赖旧工具；对危险 SQL 做静态审查，但不要以批量导入研究脚本的方式检查。
- [ ] 隔离验收：导入/执行退役入口时，模拟 store/输出接口调用次数为零；当前 raw/book/paper 行哈希不变；历史 `--apply` 继续被拒绝。
- [ ] 冻结行为验收：往副本更新原价、原 fixture 比分、raw payload 不改变 canonical；UPDATE/DELETE/REPLACE frozen record 被拒；不要为测试解除生产触发器。

<a id="p02"></a>

### P02 — 统一实际时间、比赛阶段与比分事件

**覆盖：C01、C02、C03、C08、H01、H02。优先级 P1。**

代码：[backfill_milestones._MILESTONES/_score_at](ops/backfill_milestones.py#L64)、[smart_exit._match_minute/_milestone_ticks/smart_exit_cashout](strategy/smart_exit.py#L17)、[timing_provenance](util/timing_provenance.py#L19)、[soccer_ingest._event_rows](ingest/soccer_ingest.py#L83)/[_store_detailed](ingest/soccer_ingest.py#L275)/[sync_live](ingest/soccer_ingest.py#L353)、`ingest/store.py` 的 `fixture_event` 与观察表 schema。

- [ ] 拟新增 Soccer 独立时间/比分工具 `util/match_timeline.py`，承接历史研究调用；这是待实现文件，不是声称现有能力。表示 `period`、比赛 elapsed、extra、目标 wall_ts、真实观察时间及 `clock_basis`，不要只存整数比赛分钟。
- [ ] 将已存在 +15 映射保留为显式 `approximate` 的研究模式；如果拿不到可信阶段转换观察，不把 +15 视为真实 second-half kickoff。真实时间无法证明的历史保持未知，不为补全行数制造时间。
- [ ] `_score_at` 排除 `Missed Penalty`；读取和处理 `extra`，让 HT 的状态与取价时刻一致。统一本模块研究评分的普通进球/乌龙球/罚失逻辑，不改世界杯工具。
- [ ] 历史仅有 minute/extra、没有首次观察或秒级事件时，应保留事件时间区间；在报价与事件可能交叠时禁止把“已知新比分+更早旧价”作为已验证执行。按 P09 记录不确定，不凭利润决定排除。
- [ ] `_milestone_ticks` 读取真实 ts/elapsed/source/provenance，不用 `T60` 标签覆盖捕获到的第 68 分钟。tick 扫描保留原秒级 ts，不能先 round 再做因果比较。
- [ ] FT 的 0/1 结算点与交易报价分开，使用 `fixture_result_observation` 的结果可用时间；不能以开球+95 分钟生成“已知终场”的执行时间。常规时间与 AET/PEN/advance 的结算范围继续分开。
- [ ] 不遗漏独立消费者：`smart_exit_advance._ticks`21–42行另有+15/phase近似，`settle_bets._event_timelines`103–127、`smart_exit`97–107及`smart_exit_advance`67–77行均读最终事件。统一接第9.3节的事件集版本/时间输入；`timing_provenance.result_availability`32–38行只取最新且比分相符的观察，需要明确as_of与结算scope，不能当通用历史赛果接口。
- [ ] 验收：正常比赛 T60/T75 的旧近似转换为 wall75/90；额外构造首半场补时、中场延长、晚开球、45+8、90+补时、AET/PEN；罚失不加球；较晚观测不得放到较早标签；有 VAR 修正时当时不可见的最终事件不能进入旧决策。
- [ ] 1623407 的已保存价格序列用作固定回归：19:15→0.445、19:30→0.55；旧输入仍能复现 19:00/0.135 以证明对照来源，候选不得改写原行。真实 phase 时间另行验证，不把这两个目标当其证明。

<a id="p03"></a>

### P03 — 所有历史价格路径因果取样、保留采样证据

**覆盖：C04、C05、C06、C07、C19、H05。优先级 P1。**

代码：[util/price_history.py::price_at](util/price_history.py#L12)；`ops/backfill_milestones.py::backfill/backfill_advance_pre`；[rederive.run](ops/rederive_milestone_prices.py#L58)；[backfill_price_ticks.backfill/_map_sides](ops/backfill_price_ticks.py#L25)；[backfill_price_ticks_advance.py](ops/backfill_price_ticks_advance.py#L74)；`venues/polymarket_global/reader.py::prices_history`；`strategy/smart_exit.py`。

- [ ] 保存已有三个调用的显式 causal/180 秒保护。消除默认参数的未来误用：业务安全接口默认 causal；如研究确需 nearest，必须显式标为非因果研究且不得进前向交易路径。具体兼容方式在枚举所有调用后确定。
- [ ] tick 路径同样传 fixture 明确 start/end，保留返回点 ts，并记录实际 median/max gap、数量和请求窗；不能仅凭 `fidelity=1` 声称一分钟数据。
- [ ] advance tick不能仅改时间窗：`backfill_price_ticks_advance.py` 38–42行按`NOT LIKE '%group%'`筛赛制、57–62行使用旧r16/qf映射且不传comp；当前`reader.reach_round_index`356–367行只支持有comp的advance→league_play，其余返回空。改为registry能力/两回合结算范围+显式fixture scope；缺真实对应市场记unsupported，不虚构所有轮次都可取到价格。
- [ ] `backfill_price_ticks.py` 109–112行调用`store.upsert`，后者487–495行为DO UPDATE；修重试后不能让同fixture/side/ts的新价覆盖原tick。保留旧观察，新响应差异另存研究版本；增加“同主键新价不同而旧tick字节不变”的回归。
- [ ] 统一返回/存储 `target_ts`、`sample_ts`、`quote_age`、价格类型、场馆、token、请求参数及原始响应哈希。超过容差/不晚于目标的点不存在时返回不可用，不向未来找点、不无限扩大容差。
- [ ] PRE 的 cutoff 是真实 decision_at（历史研究则为声明的 PRE target），不是 kickoff；同时满足 quote 与全部输入不晚于 cutoff。
- [ ] 验收：目标 200 的序列 160/.2、220/.9 只能取 .2；恰好 target、恰好 180 秒、181 秒、乱序、空序列、非法值、重复 ts 都有明确结果；同一分钟新进球与旧 bar 不得被因果标志“洗成”已验证执行。
- [ ] 固定879/600秒与200/60秒样本验证客户端请求参数和输出质量；不要每次依赖网络恰好返回相同点数。复查 PRE 40 样本中的 8 个目标后 bar 必须被排除。
- [ ] 清点既有 `tests/test_price_history.py` 的两个测试函数；新增边界测试必须在隔离副本，不以“本轮 12 个检查通过”冒充旧 412 全套结果。

<a id="p04"></a>

### P04 — 分离参考价与可执行 bid/ask，保护前向纸面决策

**覆盖：C09、H04、H05、H09、H10。优先级 P0（影响当前前向候选）。**

代码：`venues/polymarket_us/discovery.py::_price/match_quotes`；`ops/inplay_export.py`/`inplay_export_advance.py` 的价格传递；`ops/paper_trading.py::_snapshot_from_live/pre_decision/_inplay_decision/_fresh`；`util/paper_store.py::observe_pre/record_entry/record_exit`；`util/timing_provenance.py::record_live_milestone`；`strategy/smart_exit.py::_milestone_ticks`。

- [ ] 缺 bestAsk/bestBid 时不以 `currentPx` 补出假盘口；currentPx 可作为独立参考字段，缺失的买卖方向保持 unavailable。真实 ask/bid 有值时不得被参考价覆盖。
- [ ] 保留每一侧原始 BBO、报价类别、交易所时间（若服务商实际提供）、本地请求开始/结束及 observed_at；不把本地抓取时间冒称交易所 quote time。
- [ ] 将报价来源信息贯穿导出→观察→PRE/IP entry/exit。入场只使用合格 ask，退出只使用合格 bid；历史单价/参考价不能因写进 `_ask/_bid` 字段而取得执行资格。
- [ ] **选单与记录必须同一报价。** `decision_model.decide`返回venue/price_cents，但`paper_trading.pre_decision`121–136行选完后又按poly优先重取价。将side、venue、binding_id、quote_id、price、edge、stake作为不可拆的Decision返回并原样记录；argmax路径使用独立明确选择规则。不得用Kalshi算edge/仓位再用Poly价格入账。
- [ ] **退出场馆必须匹配持有合同。** `paper_trading.run_cycle`360–370行当前按Kalshi优先取bid且没记录所选venue。新增exit quote/binding引用；单场馆paper持仓只用同场馆同合同bid退出。若未来要研究跨场馆代理退出，必须另列执行假设/方法，不能把另一场馆bid当现持仓可卖价格。
- [ ] 保持`_inplay_decision`248–256行现有Poly优先的IP选择规则；统一收据不等于改成跨场馆最便宜策略。新方法改变的范围须在P13列清，不能借修复扩展选边/市场/仓位规则。
- [ ] PRE链也必须接收收据：`ops/upcoming_export.py::_price_comp_rows/_stash_pre`→`paper_store.observe_pre`；在record_entry/record_exit集中校验所选side/venue/quote身份和值，避免旁路。IP当前run_cycle有“评估后不再尝试”的标记逻辑；没有真实ask时记录缺数据、允许有效窗口内下一轮真报价重试，不能把参考价/缺价永久标成已评估无edge。
- [ ] 原交易所无时间或无量时保存 unknown，并依据明确的 paper 执行规则处理；不能捏造时间或成交量。模型候选、不交易原因、Demo 尝试/成交分别入后端记录。
- [ ] 研究回放若仅有历史单价，明确为独立执行假设版本；移除旧研究“缺 bid 就用 ask 卖出”的隐式跨方向/跨场馆回退。
- [ ] 不遗漏扫描器：`jobs/live_poller._live_quote_sources`、`strategy/inplay_arb.find_opportunities/_ask/_bid`、`strategy/inplay_arb_advance.find_opportunities_advance/_ask/_bid`也必须接同一契约。当前_bid对缺bid键回退ask、plain float视双边价；真实执行模式都应拒绝该隐式补价，显示参考值只能走明确参考字段。
- [ ] Demo的证据独立接入`exec/kalshi_mirror.DemoBroker.book`、`exec/demo_forward.DemoTickers.for_position/run/_send`；保留原目标腿唯一匹配、30秒锁内检查、手续费后再核时、durable intent、去重/对账和限额。Kalshi二元盘口由真实对侧bid推导ask是合法互补，不与currentPx假BBO一起删除。具体paper/execution receipt关系见9.2；不强制Demo三腿齐全才下目标腿。
- [ ] 对三路和>1.20、全0.5作后端诊断，不以形态本身判造假，也不随意按比例修成可成交价格。
- [ ] 验收：只有currentPx、只缺bid、只缺ask、空簿、真实宽价差、三侧异步、新进球后旧报价。只给currentPx=.50时不得再生成当作可成交的50¢纸面入场；真实 BBO 样本仍可通过，Demo独立订单簿流程不被参考价替换。

<a id="p05"></a>

### P05 — token/市场/球队一一对应，不能信任旧 token 必然正确

**覆盖：C15、H03、H07、H08、H10。优先级 P0。**

代码：`ops/backfill_milestones.py::backfill` 的事件匹配和 `_sides`；`ops/rederive_milestone_prices.py` 的逐场 token 提取；`venues/polymarket_global/reader.py::list_match_events`；`venues/polymarket_us/discovery.py` 3-way/advance 解析；`util/club_identity.py`。

- [ ] 拟新增 Soccer 身份校验工具 `util/market_identity.py`（待实现）：核对 fixture 两队、competition、赛日/开球窗口、市场类型、regulation/advance、outcome、token 所属市场。三路 3-way token 必须互异；不要套到定义不同的 YES/NO 结构而误拦正常市场。
- [ ] Global reader当前267–276行将groupItemTitle映射到token数组首项，须保留原market结构并按outcomes/token对应关系确认YES，不能假设第一个一定是YES。验收 `[No,Yes]` 顺序、重复label多market及缺outcome语义时明确拒绝/待核查。
- [ ] 多个事件匹配时拒绝 `next()`/字典覆盖式任选；记录候选及冲突原因。缺身份信息时只保留候选，不把同名当同场。
- [ ] 将fixture/comp/日期/scope传到真正定位点：Kalshi`discovery.match_index/advance_index`148–172/190–210行仍是pair-only覆盖、match_quotes仅接两队；US`_find_event/_probe_slugs`251–288行存在首个候选路径。扩大签名、缓存key及upcoming/live_poller调用参数，不只在新helper检查。跨场馆锁单的`equiv_verified=True`须由两份binding等价核验得出，不能固定传true。
- [ ] **常规赛果与晋级盘口隔离。** live_poller同时提供`*_advance`，inplay_export262–267行把完整source交给regulation scanner，后者407/564行按共同home/away键混读。为每个source/quote加market_kind/settlement_scope并在scanner验证；advance export的_adv_sources会去掉venue后缀，故不能仅靠字符串后缀过滤。90分钟三路、晋级、totals及corners不得互相匹配fair或构成假套利。
- [ ] `paper_store`当前只支持pre/inplay的regulation三路账本；本次接通advance报价证据不授权新增advance纸面或Demo交易策略。两个scanner都需正常/错误scope回归，既不混算，也不把正常advance显示链全部封掉。
- [ ] 对全部存量 candlestick token 做只读身份清单；疑似/未知先进入候选隔离，不能直接回写原 token 或冻结交易。
- [ ] 1623437：已证实共享 token 是 OFI YES，away CSKA 映射错误；需要取得正确 CSKA 对应市场/历史 token 的可追溯证据。找不到则保持缺失，不调换 home/away 凑出价格。
- [ ] 验收：1623437 七行同token被拒；主客同名、同两队不同日期/赛事、主客倒置、平局token、不同结算范围均被正确区分。旧 PRE +81.6¢ 单独标为受影响，不能把同场方向正确的 IP 一起算错。

<a id="p06"></a>

### P06 — 名称变体与采集完整性；FT 存在不等于数据完整

**覆盖：C11、C12、C13。优先级 P1。**

代码：`config/club_identity.json`、[util/club_identity.py::normalize_club_name/fold_club_name/ClubIdentityIndex](util/club_identity.py#L19)；`ops/backfill_milestones.py::_ClubResolver/backfill`；`venues/polymarket_global/reader.py::list_match_events`；[bootstrap_aliases.bootstrap_poly](ops/bootstrap_aliases.py#L255)；`ops/live_refresh.py::_maybe_backfill_milestones`；`ops/refresh_all.py`。

- [ ] 经身份核对后登记 `FC Twente '65` 和弯引号变体的精确别名，保留碰撞拒绝。Qarabağ Ağdam FK/Brighton & Hove Albion FC 当前已支持，只加回归，不能反复改映射。
- [ ] 五种既有语言的展示由同一 club_id 驱动；不能把外文重音/弯引号清理后绕过实体冲突检查。本计划只修后端身份及其记录，不改前端排版或增加 UI。
- [ ] Global 分页异常应返回/记录 `complete=false`、已得页数、失败页、错误和重试状态；不能返回部分 list 后被当作全部市场。统计完整候选、成功映射、无盘口、身份歧义、请求失败，不用8月/9月绝对场数代替漏采率。
- [ ] 同时核对Global season_event_index和US discovery的`_series_ids/_load/_find_event`：页数上限、冷启动部分失败、series缓存失败均要保留complete/last_complete_at。当前US虽有旧缓存保护，冷启动/截断不能因此被认证为完整；已确认市场可以保留，但缺项不得当not_listed。返回类型变更需兼容所有消费者，不能直接list改dict。
- [ ] Kalshi公共`market_data.list_events`100–123行也要补cursor分页；当前只有一次GET，缓存key只有series/status，尚未证明现时实际漏页。缓存加入环境/查询范围/身份版本；429、循环cursor、上限截断均保留不完整状态。Demo已有独立分页与身份保护，不能退化到此公共接口。
- [ ] 当前 `backfill` 只看 FT 行存在即跳过：改为单独的回填完整性/尝试记录，区分7个里程碑、三侧数据、身份和时间证据。重试仍追加观察/候选，不覆写已有快照和冻结交易。
- [ ] 外层也要改：`live_refresh._maybe_backfill_milestones`334–353行仍以全历史缺FT触发；统一调用`due_tasks(scope, now)`，避免已有FT缺侧不再触发、范围外缺FT一直触发。完整性与backoff随task版本记录，不能只改内层backfill。
- [ ] 将partial结果接到运行状态：`RunStatus.step`117–125行当前只要没抛错就标ok，返回`complete=false`不会自动降级。为历史采集步骤增加显式result validator/状态适配，接入refresh_all的price_ticks/milestone步骤、settle_reports第27行及live_refresh调用；非必需步骤partial不能伪装全部成功，也不应阻止已完成paper结算。
- [ ] 日常14天可保留为资源边界；一次性研究按冻结的 fixture 清单，不依赖运行当天 `now-14 days`。不要为补齐旧历史把日常窗口无限扩大。
- [ ] 范围必须传给子流程：`backfill_advance_pre()` 当前没有 since_days/limit/fixture_ids参数，397–403行会扫描全历史。它只要有一份candlestick_advance观察就跳过，即使443–452行只成功一侧；ticks也只凭任意一条tick跳过整场。把普通/advance/ticks范围及逐侧完整性统一，空范围不得发无边界网络请求。
- [ ] `bootstrap_poly()` 307–323行仍会把difflib/词子集猜测直接写进aliases（327–329行）。将猜测只写到候选清单；经身份证据确认的精确alias才进入正式表。当前未发现自动调用此Poly bootstrap，不能声称它正在每天写表或就是1623437错配的根因。
- [ ] alias变更需生成identity manifest并使`club_identity.catalog_records`的lru cache及discovery索引按版本重载；旧binding/decision仍引用原版本。保留现有Unicode归一化与多ID碰撞拒绝，不泛化删除FC/年份/二队后缀；FC许可别名不能升为所有场馆通用别名。
- [ ] `backfill_price_ticks.py` 69–89行按两队集合建字典，缺competition/date且重复会覆盖；移交P05的唯一事件匹配，验收同两队的两场比赛不串token。另 `club_prior.py:136` 已有 `canonical_team_name = canonical_club_name` 兼容别名，**不存在缺失符号的ImportError，不要虚构这个修复**。
- [ ] 验收：缺一个PRE/一侧但已有FT时能再次尝试；已有live/Kalshi/probability字段哈希不变；空返回与第2页报错可区分；别名歧义不能变成成功匹配。

<a id="p07"></a>

### P07 — 比分、报价、输入的 point-in-time 证据链

**覆盖：C07、C08、C20、H01、H05、H06。优先级 P1。**

代码：`util/timing_provenance.py::record_live_milestone/record_history_milestone/record_decision/current_state_matches`；`util/paper_store.py::observe/record_entry/record_exit/settle`；`ops/paper_trading.py::_metadata/_fresh/run_cycle`；`ingest/store.py` 观察表；`ingest/soccer_ingest.py` 事件和原始响应同步。

- [ ] 逐次保存状态与报价收据，把“发生时间”“首次本地可见时间”“决策时间”分开；历史市场 ts、后补记录创建时间都不能冒充当时 observed_at。
- [ ] **当前观察表不能直接多版本追加。** `ingest/store.py`242–245行milestone_observation主键只有fixture/milestone/source；`record_history_milestone`55–60行又从旧milestone_snapshot读payload。新增独立v2观察表/写接口，接受明确payload+provenance+version，不能用旧helper把新收据绑到旧价，也不能删除旧唯一约束/旧行来腾位置。schema/读取规则见第9节。
- [ ] 当前 source='live' 不应自动把所有 `_bid/_ask` 标成真实 orderbook，须使用 P04 的原始类型；当前 history sidecar 要保留 bar_ts、target_ts，rederive 不能丢弃。
- [ ] 写端与读端一起接：`timing_provenance.live_snapshots`63–75行当前只读取snapshot_json、丢provenance_json；新读取器必须返回同一receipt引用并核对hash。`store.write_raw_snapshot`535–545行同秒同查询的文件名可复用，需按内容寻址不可变保存，不能以可被覆盖文件作为PIT证据根。
- [ ] raw→source/state→decision的持久协议按9.4实现；现有observe/record_entry/record_exit各自commit，不能直接外包一个事务便声称原子。最终写入边界重新读fixture kickoff/阶段、持久receipt、entry完成状态；入场/退出/evaluation与input manifest一致提交。
- [ ] 对 future clock、过期数据、状态进球/红牌变化、暂停盘口设置确定的拒绝/重新观察结果。必须以当时观察状态作决策，不从最终事件表还原“实时已知”。
- [ ] 旧记录缺 receipt 时保持 unknown；不能通过补写 `observed_at=kickoff` 或 `strict_oos=true` 获得认证。`feature_observation_times_complete=False` 在证据没补齐前继续保留。
- [ ] 验收：decision 前只有A版事件，之后VAR回滚为B版，回放前决策仍只见A；收据 hash 可追溯；任何入场所消费 quote/input 的可得时间都不晚于决策；结果可用前不结算。

<a id="p08"></a>

### P08 — 模型与校准输入版本，修正“只有两条泄漏”的过度保证

**覆盖：C07、C18、C20、H06。优先级 P1。**

代码：[pit_strength.fc_input_fingerprint](model/pit_strength.py#L44)/[_source_fingerprint](model/pit_strength.py#L79)/[pit_prior](model/pit_strength.py#L117)/[WalkForwardStrength](model/pit_strength.py#L168)；[squad_strength.build_strength_live](model/squad_strength.py#L173) 的 FC blend（226行起）；`ingest/fc_ingest.py`；`util/timing_provenance.py::record_decision`；`ops/paper_trading.py::_calibration_records/_strength_inputs`。

- [ ] 为实际使用的 FC 评分、球队身份、先验、阵容、Elo、form、校准数据建立可用时间与版本清单；不能只存“当前表指纹”就宣称历史已可得。
- [ ] 拟新增`util/source_history.py`及append-only source_observation/feature_version层，保存entity/key、有效时间、provider_published_at（未知为null）、received_at/available_at、payload/ref、hash、supersedes；当前投影表继续用于live。`ingest/store.py::write_raw_snapshot/upsert`接入新观察层，旧值被覆盖前后的版本均可追溯。
- [ ] 按真实依赖扩展到`ingest/club_prior.py::_current_table`、`model/form_strength.py::form_index`、`model/xg_form.py::xg_form_index`、`model/squad_strength.py::squad_index`、`model/altdata_adjust.py`。这些历史读取不能只按当前行kickoff过滤；也不能只过滤当前fetched_at而丢掉已覆盖、当时实际可用的旧版本。
- [ ] 覆盖绕过upsert的来源：`fc_ingest.ingest_fc_players`260–267行先DELETE再INSERT、ClubElo `_fetch_clubelo/build_all`的CSV/sidecar/prior文件、PRE `pre_decision`112行读取的match_odds。分别保留旧版本与原件收据、变换版本，不能仅在store.upsert加字段。条件启用的`model/motivation.py`也进入manifest；关闭时记录disabled，开启才要求其未来赛程/次回合依赖的可得版本，不误报每笔均启用。
- [ ] 历史 as_of builder 只读 cutoff 前可用的版本；无历史版本的输入走明确的缺失/排除方案，并作为新模型定义记录，不能偷偷用今天FC表回灌旧赛季。
- [ ] 缓存键包括模型/方法/身份/数据版本及 cutoff；当前 fingerprint 仍用于一致性，但与 available_at 语义分开。不得让隔离回测写到生产 `clubs_*_pit*.json` 或 live priors。
- [ ] 校准只使用当时已观察结算、与声明模型/方法兼容的纸面记录；不得把待赛或证据未知记录当作已知胜负，也不得要求Demo已成交才纳入。`_calibration_records`77–87行当前丢model/method字段，需保留并应用显式compatibility manifest，保留已有result_available_at过滤。
- [ ] `Strength.get`61–65行按日+comp缓存及PIT bucket需明确原策略冻结周期；保留按日模型时将bucket cutoff和实际依赖固定，晚到输入不得改变同一已冻结输出。改为每决策秒级更新属于方法变化，不能在修时间证据时悄悄改变策略。
- [ ] 验收：加入cutoff后FC更新/未来赛果/VAR后结果，cutoff前预测输入与概率不变；相同manifest重跑结果一致；缺失历史输入有逐场清单而非静默换默认值。

<a id="p09"></a>

### P09 — 重建工具只生成可审查候选，不破坏现有行

**覆盖：C02、C11、C12、C14、C15。优先级 P1；拆为P09a底座与P09b候选。** P09a在P00之后、计算工作前建立只读SourceSnapshot/独立CandidateTarget/固定scope/schema；P09b再依赖P02–P08的校验结果。不能等全部计算做完才设计安全写入接口。

代码：`ops/rederive_milestone_prices.py` 的 token 获取、取价、rows_blanked、UPDATE、报告；`ops/backfill_milestones.py`；`util/timing_provenance.py::record_history_milestone`。

当前额外危险点：`run()` 58行默认 `dry_run=False`，没有conn会连接配置数据库；即使dry_run，136–141行也会ensure配置路径并写默认报告。因此当前 `--dry-run` 也不是无副作用审计入口，本轮没有运行它。

- [ ] 在后续补丁中拆分“读取/计算候选”和“写目标”，默认只产生隔离候选结果。生产原快照、Kalshi报价、模型概率、比分和冻结record不在写集合。
- [ ] **候选必须有独立消费路径。** 拟新增`util/research_inputs.py::CandidateMarketData`提供显式run_id下的quote/state/features；P10仅通过它读取。旧smart_exit/_inplay_entry直接查price_tick/milestone，须用纯函数/注入适配断开默认旧表读取；不能只写候选而后续仍读原价。历史price marks预览也须显示该候选run的价，生产marks保持原状。
- [ ] 显式迁移`smart_exit_cashout`74–91、`smart_exit_cashout_advance`54–60、`settle_bets._inplay_entry`234–255、旧`performance_report`598–609、`decision_backtest`202–215及`milestone_export._build`142–147的研究消费路径。候选缺项不回退旧tick/snapshot/frozen exit；返回`exited/held_no_trigger/unavailable/invalid`，只有确实评估且未触发退出可算hold，异常或不足10点不能自动算hold。
- [ ] 拟新增显式候选 `--source-db/--candidate-db/--output-dir/--fixture-manifest` 接口及路径保护；现工具没有这些安全能力，不能把此处参数当现成命令执行。候选路径必须不同于生产和源快照，打开数据库前检查真实路径。
- [ ] 每场全部必要侧的身份、时间、数据完整性验证通过才产生可用候选；单侧空序列、无180秒内bar或全部空不能记为ok。保留原行，同时在候选中记 excluded/unavailable，**不能把保留的旧坏价当作已修新价计入候选收益**。
- [ ] `ts`、sample_ts、价格、devig、source/provenance一并属于候选版本；不能更新统一ts却沿用不同时间的旧Kalshi上下文，也不能保留旧sidecar造成收据与数值不一致。
- [ ] 逐场状态、完整目标 fixture/行清单、成功/缺失/排除项、old/new hashes及各字段 diff 入manifest。业务可用覆盖与原始行保留是两件事，不能以“223/223原行还在”证明223场全可用。
- [ ] 报告绑定 source DB 快照SHA、candidate DB SHA、源码SHA、请求窗、参数、clock版本、identity版本、产物hash、run_id。仅 `dry_run=false` 和 sample_moves 不足以证明改了哪一份库。
- [ ] 改变计数覆盖所有字段：当前只比较价格，单独ts变化可能被记成unchanged；完整变化清单不能只保留40个样例/60个失败。写候选前再核对原行hash，当前WHERE只有fixture/milestone不能保护来源并发变化；尚无证据称该竞态已发生。
- [ ] 验收：请求全失败、单侧失败、无合格bar、重复token、部分事务失败、重复运行，源库哈希始终不变；候选不丢fixture、不把失败记成功；恢复运行不会混用不同run的输入。

<a id="p10"></a>

### P10 — 量化影响必须固定样本、逐腿对照，不能直接扣原收益

**覆盖：C03、C06、C07、C09、C14、C18、C19。优先级 P2；依赖 P09。**

代码：[settle_bets._event_timelines/_inplay_entry](ops/settle_bets.py#L103)、`strategy/smart_exit.py::smart_exit_cashout`、[performance_report._research_bet_log](ops/performance_report.py#L427)/`_record_totals/build_pdf`、`util/strategy_ledger.py::build_strategy_ledger/validate_strategy_ledger`。

当前没有完整的修正候选回测构建器。拟新增 `research/strategy_candidate.py::build_candidate(conn_ro, fixture_scope, input_manifest, model_version, method_version, output_dir)`，显式注入P09候选输入，复用经核验的纯模型/策略数学。现有 `_research_bet_log` 混读prior、archive、frozen pick/exit，**不能直接调用就宣称重新完整决策**；需要逐个剥离旧值依赖。该拟新增模块不得写当前book/paper/raw历史表。

- [ ] 先冻结原样本、规则、参数、可用数据和价格输入，产出逐腿旧值复算。金额统一¢/USD、每合约/按实际stake、实现收益/hold/相对hold改善，不能互相替代。
- [ ] 分开两类研究：固定原交易只换合格价格的敏感性对照；固定模型/方法规则在修正数据上重新决策的完整对照。二者都会说明覆盖/排除，不把一个数字说成另一个的实际影响。
- [ ] 完整对照记录旧新选边、入场时点、价格、仓位、退出/持有、费用假设和差额。P05身份错误、P02时间错位、P04执行假设的影响要能单独追溯，也要声明交互项，不能重复相加。
- [ ] 使用9.5的固定fixture×track范围和RequiredInputManifest：无入场决定、入场后无路径、真实no_edge、缺数据不能合并成0收益。分母不只选旧有盈利腿；完整重决策覆盖声明范围的所有合格机会，固定腿敏感性则只比较原腿。
- [ ] 财务契约固定：当前paper `pricing.contracts_for/sized_pnl`109–126行用分数合约及0.1¢收益舍入、paper渲染为未扣费用；Demo为整数数量并按实收费用。不得把更换数量/舍入/费率模型造成的差异当取价修复影响。未知费用不能补0叫净收益；账户余额不是累计profit。现有paper无资金账户及组合可用资金校验，若研究资金约束/ROI，另列最大同时占用资金、假设与方法版本，不因存在risk配置就声称已约束。
- [ ] 重新核验38/32/0、原退出35/25、上半场84/28的样本定义；取得不了原清单的保持未认证，不能为了追数改过滤条件。
- [ ] 保留四次异常退出实际587.7¢与相对hold改善909.7¢的分解；不把+910¢写成实现收益。
- [ ] 验收：同一输入复算一致；逐腿加总等于总额；缺失样本不按零收益冒充；未来新增比赛不改变旧研究快照；收益上涨或下降都只报告实际结果，不作为通过/失败条件。

<a id="p11"></a>

### P11 — 在真正变更模型/方法后，按现有版本机制替换而非重冻 raw

**覆盖：C16、C17、C18、C20。优先级 P2；依赖 P09/P10 及用户明确历史替换要求。**

现有代码：[frozen_strategy_store.register_backtest_version](util/frozen_strategy_store.py#L226)、[activate_backtest_version](util/frozen_strategy_store.py#L251)、[restore_version](util/frozen_strategy_store.py#L272)、[_require_no_open_positions](util/frozen_strategy_store.py#L290)。这些函数已存在，不需要重新设计一个绕过保护的脚本。

- [ ] 新研究报告必须包含完整strategy_ledger及匹配的completed manifest。现有register要求 `run_id/status/model_version/method_version/ledger_id/completed_at`；P09的输入与数据库哈希是还需补强的字段。
- [ ] 现register验证给定records/hash/累计自洽，但如果漏了fixture后重新计算整份ledger，它并不知道声明范围缺项；必须新增fixture_scope hash与每场决策/排除清单的覆盖核验，不能声称现有register已能拒绝所有缺场候选。
- [ ] 仅在实际模型/交易方法变更且用户明确要求替换后，才允许登记并激活；单独数据调查继续保留研究输出，不给当前版本改名后强行激活。
- [ ] 准备好完整候选、差异报告、旧版本恢复证据，再处理生产切换。保留 `expected_active_version` 比较、完整record哈希校验、未平 paper/Demo 持仓及pending订单阻断，不能强行清空持仓。
- [ ] 当前 `paper_entry` 有 `(fixture_api_id,track)` 全局唯一约束，新回测结果应进入候选book，不在同一实时paper表伪造历史entry；版本切换后的在途/重复fixture行为必须在副本验证。
- [ ] 分开验收门禁：register拒绝未授权、同一模型/方法版本、未完成/不匹配manifest，拟补强后拒绝声明范围缺项；register本身不检查开放持仓、没有expected_active_version，也不自动激活。activate/restore才检查expected_active_version及开放paper/Demo/pending；原版本逐字可读，restore只切换指针不重算旧行。
- [ ] **上述现有pending检查仅覆盖kalshi_mirror汇总，不覆盖全部durable intent。** `_require_no_open_positions`290–305行不查demo_forward_intent/event；`demo_forward._send`395–403行先持久化pending，HTTP后423行才同步mirror。需按intent最新event及未对账数量核查pending/unknown，再允许activate/restore；验收崩溃在403行之后、mirror仍零且paper已终止的组合，不能把汇总零视作无未决订单。
- [ ] 拟新增 `ops/publish_strategy_version.py` 协调已验收版本的切换、三件发布及恢复；它不是现有功能。现有SQLite savepoint与ExportStage逐文件替换/异常回滚，**不构成DB和六份产物的联合原子事务**。必须明确提交次序、phase journal、实际状态检查和崩溃恢复。
- [ ] `paper_store.settle()` 290行会主动commit，不能混入需要保留外层事务的切换/发布段；协调器使用已确定版本的纯report对象，不隐式调用settle或带consume副作用的build。
- [ ] 协调器枚举所有paper/Demo/refresh写入者和锁顺序；单独拿refresh_all锁不能假设阻止所有交易。保留开放持仓门禁，不能为切换自动平仓/撤单。每个提交/文件提升阶段均故障注入，验证旧或新版本可明确恢复。
- [ ] 锁/切换/恢复合同见9.7。`restore_version`只恢复DB激活指针，不恢复运行代码、参数、6份产物、已部署文件或外部订单；恢复包必须绑定这些版本，恢复结果分层验收，不能声称一次restore全部回滚。当前只在隔离环境演练。

<a id="p12"></a>

### P12 — 三处策略记录一致发布，Demo 保持执行子集

**覆盖：C10、C20。优先级 P1（保护性回归，不新增 UI）。**

代码：`util/frozen_strategy_store.py::read_book/consume_completed_paper`；`util/strategy_ledger.py`；`ops/performance_report.py::build/publish_strategy_views/build_pdf/_demo_coverage`；`ops/milestone_export.py::build/_projection`；`ops/settle_reports.py::main`；`ops/export_stage.py::ExportStage.promote`；`exec/demo_forward.py`、`exec/kalshi_mirror.py`。

- [ ] 准确度盈亏、价格轨迹、PDF都从同一个report/ledger对象派生，校验ledger_id、book_version、fixture/track/side、entry/exit、stake、实现收益、累计与顺序。
- [ ] 保留当前frozen book纯读取和append已完成paper流程；修改价表/模型缓存不能令旧记录每天变化。待结算entry不可写成输。
- [ ] Demo attempted/filled/partial/no-market/rejected/pending/settled及费用按真实执行收据记录；模型覆盖与Demo覆盖分别计算，绝不拿Demo资金余额代表模型累计收益。
- [ ] 向未来运行时，同一比赛/track不因重复轮询、API重试或导出重试重复paper入场或Demo下单；Demo继续用独立实际Kalshi盘口与现有风险门槛。
- [ ] 在隔离输出目录验证生成失败、PDF失败、并发新live marks时保留完整旧组，不混入不同ledger；复用现有ExportStage/发布锁，不绕过它直接write_text生产产物。
- [ ] PDF验收读取其实际明细与合计，不仅检查文件存在。若测试新PDF，生成/渲染都在隔离目录，必要时再应用PDF技能；本轮不生成产品PDF。
- [ ] 比较的是嵌套strategy_ledger与交易投影；价格轨迹顶层as_of/marks可随行情变化，不能要求整份JSON字节相同。PDF当前元数据含ledger_id/as_of，若要核对相同记录但不同book的方法版本，拟追加book_version_id到元数据，不能在UI新增标签。
- [ ] 注意 `performance_report.build(freeze=False)` 893–908行仍调用consume_completed_paper，并非纯只读。候选发布应新增纯report_from_book组装入口或等价封装，再传supplied report到publish_strategy_views，避免隐含追加；不要用settle_reports整任务预览候选，也不能嵌套ExportStage。

<a id="p13"></a>

### P13 — 前向修复也要版本化，不能让旧持仓失去退出路径

**覆盖：P02/P04/P07/P08/P11/P12的相互影响及H12。优先级 P0设计前置；本计划只在隔离环境实现/演练。**

代码：[paper_trading._implementation_hash](ops/paper_trading.py#L38)、[_live_model](ops/paper_trading.py#L191)、[execution_context](ops/paper_trading.py#L203)、`util/paper_store.record_entry/positions`、`exec/demo_forward.run/_send`、`util/frozen_strategy_store.active_version/_require_no_open_positions`。

- [ ] 当前implementation_hash仅覆盖inplay.py和dixon_coles.py；改这两份文件后旧entry在_live_model直接报错，paper退出与Demo执行上下文都可能受影响。改其余行为依赖又未必触发hash。这里是升级兼容缺口，不是声称目前已发生仓位卡死。
- [ ] 冻结DecisionMethodManifest：真实执行代码依赖、参数、市场/报价/时间规则、输入版本、激活时间与旧版本引用；保存到新entry。只改收据结构的兼容补丁也要说明是否改变side/price/stake/exit结果；改变交易行为须有新方法身份，不能仍冒用旧方法。
- [ ] **前向方法与历史book必须解耦。** 当前`paper_store.record_entry`153–154行把model_version/method_version强制设成active book的值，仅传新payload版本会被覆盖。拟新增不可变`forward_method_epoch`/manifest及独立激活记录，新entry同时绑定book_version_id与实际forward_epoch_id；最终写入比较expected epoch及book，exit继承entry的epoch。book版本仍代表冻结基线及其追加容器，不能为未来方法变化给旧352条重标或走P11偷换历史。
- [ ] 解耦必须接到`paper_store._render/settle`、`frozen_strategy_store._append_completed_row`、`paper_trading._live_model/execution_context/_calibration_records`和Demo context；新completion按每条PRE/IP腿保留epoch及实际方法manifest，不以_render的首腿base覆盖另一腿。新旧字段兼容语义写清：新执行/校准不从book根反推实际方法，旧记录仅读兼容。三件导出保留同一新记录与hash，不增加UI标签或回写旧行。
- [ ] 在隔离环境验证旧payload读取适配；新字段缺失保持legacy_unknown，不回写旧payload或伪造旧hash。新的合格实时报价可用于同合同退出，但不能把旧交易改标为严格PIT。
- [ ] 前向切换有两种待选择的实施方式：确认没有开放paper/Demo/未知pending后切换；或先实现按entry绑定的旧实现/参数分派并证明旧仓位能继续退出。当前没有完整旧实现分派能力，不得直接跳过hash检查或自动平仓。
- [ ] 正常新决策、旧开放仓位、pending恢复、已结算历史的生命周期都必须有集成用例。更正采集/时间/身份后，新entry生效边界明确，旧entry的side/stake/模型输入/退出规则保持不可变。
- [ ] 推荐顺序中的前向修复上线门禁不能等到P11历史替换才检查。当前“修复不进生产目录”约束下，仅交付补丁和隔离切换演练，真实部署待另行明确指令。

<a id="execution-order"></a>

## 4. 顺序、隔离测试与逐项完成登记

推荐实施顺序（不是现在执行）：

1. **S0：P00隔离验收→P01退役危险入口→P12旧记录不变的基线回归。** 同时完成P13旧持仓/方法兼容设计。未过T00不得实施其他包。
2. **S1：先约定并实现底层契约。** P09a只读源/候选/scope接口先落地；P05 MarketBinding、P07观察v2、P08 SourceVersion与P06采集task状态按第9节落地；P04先提供兼容的类型适配，不立刻让所有旧payload因缺字段失效。各包可并行做纯函数，合并schema后才接消费者。
3. **S2：接通实时完整链。** P05身份/scope→P04所有PRE/IP/scanner/Demo消费边界→P07时间一致性→P13旧仓位兼容；P06分页/重试及状态传播随之接通。通过正向可交易和坏数据拒绝测试，不能用“全skip零报错”过关。
4. **S3：接通历史完整链。** P02时钟/事件→P03采样→P06普通/advance/tick范围与重试→P09候选生产和显式读取；P08历史as_of输入同步完成。身份、阶段、价格与输入有一项未核实，该样本不能冒称严格PIT。
5. **S4：P10固定输入双对照→P12全量三件一致及每日只追加回归。** 运行T00–T16集成验收、输出逐问题证据与未解决数据清单；此时完成的是生产外修复候选，不是生产已修。
6. **S5（当前不执行）：** 用户另行明确生产应用后，先P13前向兼容/未决订单门禁；若还明确更换模型/方法并替换历史，再P11版本登记/协调发布/恢复演练。P11不是一般开发或前向改动的隐式授权。

依赖必须按上述阶段拆开：P04与P07共同依赖先定好的QuoteEvidence，不是互相等完整实现；P02先产状态与时间契约，P03选价，P09组合，P10消费；P12先做基线测试、最后再做集成发布测试。共享文件由一位集成人顺序合并，不能多代理同时改store.py、paper_trading.py或reader.py产生半套schema。

隔离环境必须核对：

- [ ] 独立源码工作副本/checkout，源码根、DB、data/raw、priors/cache、logs、output、frontend_data、锁目录全部解析到隔离根；拒绝指向生产的symlink。`ExportStage` 默认在CONFIG.data内创建目录，因此也必须重定向。
- [ ] 不复制.env或私钥进测试，不启动launchd、refresh_and_deploy、交易runner；网络和交易接口mock，历史固定公开样本从审计证据读取。
- [ ] 测试只导入已阅读、无顶层破坏副作用的隔离模块；不能用生产 store.init_db 来建立测试fixture。
- [ ] 建议新增隔离测试组：退役工具副作用、时钟/事件、取价因果、报价类型传递、market identity、分页/重试、候选保留、输入as_of、book版本/三产物一致。名称及文件只在后续工作副本确定，不在生产目录创建测试垃圾。
- [ ] 开始/结束记录源码哈希、源快照哈希、候选哈希、fixture清单、实际执行测试名称和结果。原“412全绿”单列未认证，不沿用为新的验收结果。
- [ ] 每个工作包记录：修复commit/补丁、执行日期、隔离路径、测试证据、受影响字段、剩余未知、是否改变前向方法、是否做过历史研究、是否生产激活。三个状态不能合写“已完成”。

## 5. 后续历史研究/替换的具体安全路径

1. 只读备份当前DB为一致性快照，固定候选fixture主键集合；同时保存当时canonical原始record JSON、所有累计、激活版本、raw行和产物manifest。不能只复制SQLite主文件而漏掉正在写的WAL。
2. 在生产外建立独立候选，先执行P05身份验证和P02/P03/P04/P07/P08的数据资格检查。原始行完整保留；不能证明的数据在候选中说明缺失/不确定。
3. P09只产生候选报价/状态与完整old/new差异；整场失败与部分失败都可追溯。不要删除原candlestick，不清空raw IP，不调用_refreeze。
4. P10在固定规则下重算，保存所有保留/新增/不再入场/换边/仓位变化，不预定PnL范围。历史中点回测与真实BBO纸面方法是不同执行假设，不能混淆后称“严格实盘”。
5. 对目标案例、全部受影响行、覆盖范围、原始行哈希和完整ledger逐一核对；“223成功”必须能追到具体candidate DB和manifest，不能只输出一句日志。
6. 如果用户没有明确更换模型/方法并替换，工作止于候选研究产物，当前frozen book保持原样；当前未来paper仍只增加真实观察后的决策。
7. 若已满足替换条件，且P11拟新增发布协调器已经实现并通过崩溃/故障测试，才由协调器调用现有register/activate机制；activate沿用未平仓阻断与expected_active_version。不能把当前原语直接串起来就声称具备完整恢复保证。这里不提供能被误复制执行的生产删除/改账命令。
8. 协调器以同一个已验证book生成三件产物，保留旧版本、恢复指针及发布journal。验收后记录替换发生的时间和新方法开始时间，不将事后研究伪装为过去真实下单。

## 6. 只读验收查询示例（执行时连接明确快照，禁止默认生产连接）

以下仅为SELECT示例，不会修数据；日期和fixture计数以各自快照为准。客户端应使用SQLite `mode=ro`、`PRAGMA query_only=ON`。

```sql
-- 原始覆盖：来源场次有交集，不能把场次数直接相加。
SELECT price_source, COUNT(*) AS rows, COUNT(DISTINCT fixture_api_id) AS fixtures
FROM milestone_snapshot GROUP BY price_source;

-- 目标ts相对开球墙钟；不是选中bar的sample_ts。
SELECT m.fixture_api_id, m.milestone, m.ts, m.elapsed,
       ROUND((julianday(m.ts)-julianday(f.kickoff_ts))*1440, 3) AS wall_minutes,
       m.poly_away_ask
FROM milestone_snapshot m JOIN fixture f ON f.api_id=m.fixture_api_id
WHERE m.fixture_api_id=1623407 ORDER BY m.ts;

-- 三路3-way token冲突候选；实际身份需另核market/outcome，不凭此修值。
SELECT fixture_api_id, milestone, poly_token_home, poly_token_draw, poly_token_away
FROM milestone_snapshot
WHERE (poly_token_home IS NOT NULL AND poly_token_home=poly_token_away)
   OR (poly_token_home IS NOT NULL AND poly_token_home=poly_token_draw)
   OR (poly_token_away IS NOT NULL AND poly_token_away=poly_token_draw);

-- 异常价格诊断，不等于“编造”或已量化损益。
SELECT fixture_api_id, milestone, price_source, poly_home_ask, poly_draw_ask, poly_away_ask
FROM milestone_snapshot
WHERE poly_home_ask+poly_draw_ask+poly_away_ask>1.20;

-- 罚失事件只用于定位，不将其数量写成错误下注笔数。
SELECT fixture_api_id, minute, extra, team_api_id, detail
FROM fixture_event WHERE type='Goal' AND detail='Missed Penalty';
```

真正的PIT验收还需要候选中每条quote/input的available_at、sample_ts、decision_at与market identity；当前原表字段不足时不能仅靠这些SELECT宣称全系统无泄漏。

## 7. 32项问题逐项登记：原审计26项与后续6项补充

原26项（C01–C20、H01–H06）继承19:07快照审计；H07–H12为之后源码复核补充，已在第2节另标两个陷阱的19:51新复查。所有数字保留其样本和口径，不因写计划变成已经修复。每项处理映射到第3节工作包。

### C01 — L1：下半场价格与比分错位

**核验：成立，旧记录仍存在。** 当前T60有202条取价目标仍为墙钟60分钟，T75有207条取价目标仍为墙钟75分钟；合计409行、207场。主代码已改为75/90，但旧记录并未自动修正。与9月7日23:22备份对比，原1,442行/223场的ts、elapsed、三路ask五字段全部未变。

**后续处理：** [P02](#p02)、[P09](#p09)；状态：待执行/待回归。

证据：[snapshot-findings.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/ops/snapshot-findings.json) · [clock-evidence.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/data-quality/clock-evidence.json)

### C02 — L1：1623407的价格与+538¢

**核验：独立复现，已进入冻结收益。** 特文特–卡拉巴赫52分钟客队进球；当前T60仍19:00/13.5¢、比分0–1，T75仍19:15/39.5¢。公开行情在修正目标19:15为44.5¢、19:30为55¢。冻结IP仍按13.5¢买客队，仓位$0.84，记+538.2¢。

**限制：** 44.5¢/55¢是重拉的历史价格点，未声称当时有对应数量的可成交盘口；本次未重算该腿的正确利润。

**后续处理：** [P02](#p02)、[P03](#p03)、[P09](#p09)、[P10](#p10)；状态：待执行/待回归。

证据：[public-current-window-probe.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/evidence/public-current-window-probe.json) · [frozen-price-links.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/performance/frozen-price-links.json)

### C03 — L1：38笔、32有利/0不利及退出方向统计

**核验：入场部分可复现，退出旧数未完全认证。** 46条IP入场能按比赛、分钟、方向、旧价精确关联。其中35条盲区有进球，另3条只有红牌，合计38；进球方向32有利、0不利、3净不变。另关联16条PRE退出、18条IP退出。去重80腿/68场的原纸面收益为6834¢。

**限制：** 6834¢是暴露记录的旧收益，不是已证实虚增额，不能直接扣减。原35退出/25不利未按同一旧样本完整复现；一腿有price_tick，来源不唯一。

**后续处理：** [P02](#p02)、[P10](#p10)；状态：待执行/待回归。

证据：[l1-goal-red-directions.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/data-quality/l1-goal-red-directions.json) · [frozen-price-links.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/performance/frozen-price-links.json)

### C04 — L2：879根10分钟 vs 200根1分钟

**核验：同一token公开请求已复现。** interval=max/fidelity=1返回879点，中位600秒；按当前开球前30分钟、后170分钟显式窗口返回200点，中位60秒。三个milestone取价调用现已带窗；独立backfill_price_ticks仍interval=max，现有34444点/7场的中位间隔仍600秒。

**限制：** 这是该token与本地存量的实测，不能声称服务商所有行情固定返回同样数量。当前前向paper不靠该历史tick回填下单。

**后续处理：** [P03](#p03)、[P06](#p06)；状态：待执行/待回归。

证据：[public-price-probe.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/evidence/public-price-probe.json) · [public-current-window-probe.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/evidence/public-current-window-probe.json) · [snapshot-findings.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/ops/snapshot-findings.json)

### C05 — L2：最近邻会取未来，causal/180秒是否已修

**核验：主调用已修，旧数据与同步边界仍在。** 函数默认仍是nearest/900秒，但全部三个业务price_at调用显式causal=True/180秒。构造目标200时旧规则取220/.9，新规则取160/.2可复现。不能将函数默认值误报为主调用仍未修。

**限制：** causal只保证价格点不晚于目标；仍可能用14分旧价格配15分新进球状态。它没有证明该旧价格在决策时仍可成交。

**后续处理：** [P03](#p03)、[P07](#p07)；状态：待执行/待回归。

证据：[clock-prices.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/clock-prices.json) · [tests-result.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/ops/tests-result.json)

### C06 — L2：84次上半场入场、28次早于进球

**核验：机制成立，原精确样本未完整核实。** 旧最近邻与粗价格点存在时间错位；本轮确认上半场和PRE也可以取目标之后的点。原84/28缺少完整逐笔选中bar及当时样本定义，不能单靠当前milestone.ts重建其全部分母。

**限制：** 没有为追平旧数字重新选择全部历史下注，也没有重写冻结记录。

**后续处理：** [P03](#p03)、[P10](#p10)；状态：待执行/待回归。

证据：[clock-prices.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/clock-prices.json) · [pre-sample-result.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/evidence/pre-sample-result.json)

### C07 — PRE：零开球后价格，所以全部干净

**核验：结论过度，截止点检查不充分。** 从9月7日备份204个已下注candlestick PRE中按时间均匀抽40条，公开重拉40次均成功。0条越过开球，但8条所选价格点晚于PRE自身目标10–17秒，8个价格都与旧存价一致。PRE还存在已确认的token错配和未版本化模型输入边界。

**限制：** 这40条不声称与原AI抽样完全相同。10–17秒不能夸大为赛中泄漏；也未量化其收益影响。+773¢并未因此获得无泄漏认证。

**后续处理：** [P03](#p03)、[P05](#p05)、[P07](#p07)、[P08](#p08)、[P10](#p10)；状态：待执行/待回归。

证据：[pre-sample-result.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/evidence/pre-sample-result.json) · [frozen-price-links.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/performance/frozen-price-links.json)

### C08 — D4：elapsed不早于标签，所以一定保守

**核验：零提前行成立，方向推论不成立。** 当前802条live行中elapsed<名义分钟为0；654条赛中记录有385条晚于标签，最大8分钟。旧smart_exit回退仍按标签分钟而不是实际elapsed配比分。没有逐腿交易所时间，无法从elapsed比较推出报价/比分先后或利润偏差方向。

**限制：** 新前向paper用实际elapsed及观察时间核对当前比分/红牌，不能将旧回放问题直接套成新实时记录同样泄漏。

**后续处理：** [P02](#p02)、[P07](#p07)；状态：待执行/待回归。

证据：[timing-count-extras.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/data-quality/timing-count-extras.json) · [clock-prices.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/clock-prices.json)

### C09 — D5：13行异常、8行全0.5、4次退出+910¢

**核验：数量有依据，性质和金额口径需纠正。** 当前三路Polymarket ask和>1.20为27行，其中历史13、live14；8行0.5/0.5/0.5仍在。四次PRE退出实现收益合计587.7¢；909.7¢才是相对持有到期的改善。宽价差或全0.5本身不证明编造。

**限制：** 历史单一price被同时写成bid/ask是明确的执行假设；没有当時订单簿，不能把它当真实可成交价。异常行数也不等于错误下注数。

**后续处理：** [P04](#p04)、[P10](#p10)；状态：待执行/待回归。

证据：[d5-full-rows.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/data-quality/d5-full-rows.json) · [frozen-price-links.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/performance/frozen-price-links.json)

### C10 — Demo镜像的6笔能否证明/否定泄漏

**核验：跨场馆少量成交不能认证整个纸面账本。** 旧9月7日备份可复现6条分钟≥60、有卖方ask并实际成交的IP入场；今天同一定义为7条。它们纸面引用Poly、实际成交Kalshi，不能仅凭价差证明或否定泄漏。Demo成交是执行子集，不能代表全部多模型收益。

**限制：** 不把已有Demo盈利/亏损作为历史纸面记录PIT的替代证据。

**后续处理：** [P12](#p12)；状态：待执行/待回归。

证据：[ledger-safety.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/ledger-safety.json) · [mirror-claim.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/performance/mirror-claim.json)

### C11 — COALESCE只填空，重跑不会覆盖旧价

**核验：当前更严格：冲突直接DO NOTHING。** 当前普通历史回填不覆盖任何已有行，甚至已有行的空字段也不补。advance历史补价另写观察表。因此仅修代码再跑普通回填不会修正存量旧价；部分注释和模块头仍是旧说法。

**后续处理：** [P01](#p01)、[P06](#p06)、[P09](#p09)；状态：待执行/待回归。

证据：[ledger-safety.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/ledger-safety.json) · [clock-prices.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/clock-prices.json)

### C12 — 删除全部再重建、14天窗口与45天彩排

**核验：丢失风险成立，不能照搬。** backfill当前仍默认14天，since_days同时限制fixture与事件查询；已支持显式扩大窗口。扩大到45天也不保证旧事件、标签、token和价格都可恢复。旧145条是8月24日期界线的特定统计，不是所有运行时点固定值。

**限制：** 336/65、1043/166和634候选是旧彩排数字，本次没有删除再跑来强行复现。保留这些主张为旧证据不足，不能当作今天可执行迁移方案。

**后续处理：** [P01](#p01)、[P06](#p06)、[P09](#p09)；状态：待执行/待回归。

证据：[ledger-safety.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/ledger-safety.json) · [data-quality.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/data-quality.json)

### C13 — Polymarket三种全名失败、23场 vs 200场

**核验：一队变体仍漏；另外两队已支持。** FC Twente '65及弯引号版本仍未识别；Qarabağ Ağdam FK和Brighton & Hove Albion FC当前已正确识别。当前candlestick覆盖按开球月份为8月263场、9月96场，旧23/200不能视作今天覆盖率，也不能据此把月度数量差全部归因于改名。

**限制：** 未取得原166个失败标签和同一旧事件集合。当前分页异常可返回部分结果且缺完整性标记，仍有静默漏采风险。

**后续处理：** [P05](#p05)、[P06](#p06)；状态：待执行/待回归。

证据：[identity-evidence.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/data-quality/identity-evidence.json) · [coverage-definitions.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/data-quality/coverage-definitions.json) · [data-quality.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/data-quality.json)

### C14 — 223成功、1005改变、214不变、0清空

**核验：报告存在，当前数据库不等于报告结果。** 9月7日23:35的报告确有这些数字和dry_run=false。但原1442行的五个关键字段现在全部仍等于23:22备份；报告40个sample_moves也全部等于old、没有一项等于new。报告未绑定数据库路径或哈希。

**限制：** 不能凭报告推断当时从未落库还是后来被恢复。1005+214=1219，另223个FT本就不重新取价，并非少了223行。338/242属于旧进度，备份实际为338/245IP。

**后续处理：** [P09](#p09)、[P10](#p10)；状态：待执行/待回归。

证据：[rederive-report-provenance.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/evidence/rederive-report-provenance.json) · [snapshot-findings.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/ops/snapshot-findings.json) · [report-vs-database.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/data-quality/report-vs-database.json)

### C15 — 按存量token重拉就零错配、零清空

**核验：两个保证均有反例。** 逐场事务和整场请求失败保留旧行确实存在；但单侧缺数据或找不到180秒内因果点时仍会写NULL，甚至三侧全NULL也计ok。UPDATE还改ts/devig，丢弃所选bar时间；旧token自身也已查出错配。

**限制：** 没有在生产运行rederive。报告曾经0清空不等于程序保证未来不清空。

**后续处理：** [P05](#p05)、[P09](#p09)；状态：待执行/待回归。

证据：[tests-result.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/ops/tests-result.json) · [clock-prices.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/clock-prices.json) · [duplicate-token-market-details.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/evidence/duplicate-token-market-details.json)

### C16 — _refreeze_ip.py会DROP TABLE

**核验：危险入口仍在，当前更不适用。** 该模块顶层直接DROP settled_bet并commit，无main guard，导入也会执行。当前freeze_settled_bets已经只结算已有paper，不能重建原366条raw历史。此脚本不属于正常周期调用。

**限制：** 仅阅读源码，绝未导入或运行此脚本；正常冻结book另有不可变保护。

**后续处理：** [P01](#p01)、[P11](#p11)；状态：待执行/待回归。

证据：[ledger-safety.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/ledger-safety.json)

### C17 — UPDATE inplay_json=NULL再backfill是安全替代

**核验：今天已失效，且会破坏原始字段。** backfill_inplay apply现在明确拒绝。公开三件产物读独立完整冻结book，清空raw inplay_json既不会更新该book，也违反保留旧记录的目标。内存副本验证raw价格/比分/表变化均不改canonical，真正book改写被触发器拒绝。

**后续处理：** [P01](#p01)、[P11](#p11)；状态：待执行/待回归。

证据：[immutable-probe.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/performance/immutable-probe.json) · [ledger-safety.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/ledger-safety.json)

### C18 — 预计收益会大降、3343→2858才是真实影响

**核验：预估不能当结果，未取得完整最终对照。** 旧备份实际PRE3343.3¢、IP7847.9¢、hold618.1¢、合计11191.2¢；不同于引文8008/3498/773/11506。2858和其对应完整重算清单未被本轮独立认证。当前353场/608腿是另一个固定口径，合计11861.1¢。

**限制：** 没有重新选边、调参或全量重算，因此不给一个新的猜测区间。收益下降与否不能作为选用修法的验收条件。

**后续处理：** [P08](#p08)、[P10](#p10)、[P11](#p11)；状态：待执行/待回归。

证据：[snapshot-metrics.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/performance/snapshot-metrics.json) · [ledger-safety.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/ledger-safety.json)

### C19 — 三个回归测试、412全绿

**核验：相关断言可复验，412全套未认证。** 现test_price_history.py有2个测试函数，覆盖所说三类不变量。本轮在隔离目录执行这2个原函数和10个边界检查，共12通过；另用报价mock验证currentPx候选链。部分通过的边界检查恰恰证明现代码存在缺口。

**限制：** 没有在生产目录运行测试，也没有把本轮12项称成412项或完整PIT验收。

**后续处理：** [P03](#p03)、[P10](#p10)；状态：待执行/待回归。

证据：[tests-result.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/ops/tests-result.json) · [current-px-forward-analysis.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/data-quality/current-px-forward-analysis.json)

### C20 — 当前冻结与新前向记录是否正常分开

**核验：分离已成立，历史缺陷仍被保留。** 当前book353场=352条旧基线+1条真实前向结算；608腿=607旧腿+1新IP。新IP18:19:59记录、18:36:12观察终场后结算-78¢；另4条PRE在18:40记录、19:00开球，尚未结算。三件产物同记录，不等于旧记录已经无偏。

**限制：** 只认证这份快照与所读记录；当前仍有currentPx/报价时间和模型版本可得性边界，不能扩大为整系统严格PIT。

**后续处理：** [P07](#p07)、[P08](#p08)、[P11](#p11)、[P12](#p12)；状态：待执行/待回归。

证据：[ledger-safety.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/ledger-safety.json) · [snapshot-metrics.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/performance/snapshot-metrics.json)

### H01 — 固定15分钟、HT47与事件extra

**核验：残余时间错位，已有边界反例。** 固定15分钟不包含实际开球延迟、首半场补时和实际休息长度；HT47的比分按minute<=45却忽略extra，会把45+8进球提前计入。当前至少3个HT存在45+3/45+4正常进球候选。FT也用名义wall95，不是赛果首次可用时间。

**限制：** raw periods中5336个半场间隔全部恰好60分钟，未经额外验证也不能当真实哨声替代。没有把名义FT误称成真实提前结算。

**后续处理：** [P02](#p02)、[P07](#p07)；状态：待执行/待回归。

证据：[clock-prices.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/clock-prices.json) · [tests-result.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/ops/tests-result.json)

### H02 — 历史比分把Missed Penalty算进球

**核验：代码缺陷已复现。** _score_at只筛type=Goal，未排除detail=Missed Penalty，会给罚失增加一球；当前事件表有28条这类事件。smart_exit另一路已有排除，同模块处理不一致。

**限制：** 28条是事件数量，不能直接写成28笔错误下注或推算利润。

**后续处理：** [P02](#p02)；状态：待执行/待回归。

证据：[clock-prices.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/clock-prices.json) · [tests-result.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/ops/tests-result.json)

### H03 — OFI与CSKA的主客token相同

**核验：真实身份错配，已关联一条冻结PRE。** fixture1623437全部7个里程碑的home/away使用同token；公开市场确认是OFI获胜YES。冻结PRE却记买CSKA客队32.5¢、卖63¢，仓位$0.87、账面+81.6¢。这条PRE消费了错误away列。

**限制：** 同场IP买OFI方向本身正确，不把整场收益都算为错配；重新抓旧token无法修身份。

**后续处理：** [P05](#p05)、[P09](#p09)；状态：待执行/待回归。

证据：[duplicate-token-market-details.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/evidence/duplicate-token-market-details.json) · [frozen-price-links.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/performance/frozen-price-links.json)

### H04 — Polymarket US缺BBO时用currentPx补买卖价

**核验：当前前向候选路径也会接受。** 当前_price在bestAsk/bestBid缺失时以currentPx补齐，丢失报价类别。隔离mock仅currentPx=.50会生成ask=bid=.50，并通过真实_inplay_decision函数体形成50¢纸面买入候选。

**限制：** 没有宣称今天已记录的5笔真实输入一定触发了该fallback；原始BBO缺失字段未留存，无法逐笔证明。Demo仍另取Kalshi执行盘口。

**后续处理：** [P04](#p04)、[P07](#p07)；状态：待执行/待回归。

证据：[current-px-forward-analysis.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/agents/data-quality/current-px-forward-analysis.json) · [data-quality.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/data-quality.json)

### H05 — 历史单价、盘口与时间收据混用

**核验：证据不足，不能当真实可成交收益。** 历史接口提供t/p单价，回填把它复制到bid和ask；旧smart_exit还会在缺bid时用ask退出。新historical sidecar虽保留sample_ts并标非可执行，旧1442行缺这类收据；rederive也丢弃bar_ts。live只保留本地采集/导出时间，未保留逐腿交易所时间。

**限制：** 价格历史接口和真实订单簿是不同数据契约，见报告官方文档链接。此项是执行可验证性与同步边界，不把所有旧行一律认定伪造。

**后续处理：** [P03](#p03)、[P04](#p04)、[P07](#p07)、[P09](#p09)；状态：待执行/待回归。

证据：[clock-prices.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/clock-prices.json) · [data-quality.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/data-quality.json)

### H06 — “只有两条泄漏”能否视作完整PIT结论

**核验：不能，模型输入历史仍未获认证。** 当前历史352条本来标为未获严格OOS证明；FC输入按当前表读取，没有历史available_at/完整版本证明。最终事件表也不能还原当时VAR修正及首次到达。冻结机制保留了原收益及其缺陷，新前向记录则保存当时观察与输入。

**限制：** 无法据这些证据数出全系统恰好几条独立泄漏；本轮确认具体缺陷和证据边界，不把潜在风险都当已发生的实际亏损。

**后续处理：** [P07](#p07)、[P08](#p08)、[P10](#p10)；状态：待执行/待回归。

证据：[ledger-safety.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/ledger-safety.json) · [clock-prices.json](/Users/xuling/.codex/reviews/soccer-assessment-20260910/reports/clock-prices.json)

### H07 — tick事件索引缺赛事/日期，兼容函数并未缺失

新增源码复核：`ops/backfill_price_ticks.py` 69–89行以两队集合建字典，相同两队的不同赛事/日期可互相覆盖。该函数被`ops/refresh_all.py` 54–55行调用，但未证明现有7场tick实际发生错配。`club_prior.py:136`已有canonical_team_name兼容赋值，不能误报ImportError。

**后续处理：** [P03](#p03)、[P05](#p05)、[P06](#p06)。用含赛事/日期/双方身份的唯一binding；调换events输入顺序不应改变匹配；保持旧tick不变。

### H08 — bootstrap_poly可将模糊候选写成正式别名

新增源码复核：`ops/bootstrap_aliases.py::bootstrap_poly` 307–323行的近似/词子集映射直接进入aliases，327–329行写文件。未发现该Poly函数的自动调度；现模块CLI只调用Kalshi bootstrap。不能推断它就是1623437历史错配的来源。

**后续处理：** [P05](#p05)、[P06](#p06)。模糊结果只产审核候选，应用经核验的精确manifest；propose不修改已有alias文件，碰撞拒绝。

### H09 — PRE选单价格、入账价格及退出场馆可能脱节

**二审源码确认：** `decision_model.decide`238–246行返回所选venue/price，`paper_trading.pre_decision`121–136行却重新按Poly优先取价；run_cycle360–370行退出又优先选Kalshi bid且丢venue。代码存在可触发的不一致，尚未统计生产实际受影响笔数或收益。

**处理与关联：** [P04](#p04)贯穿SelectedQuote，P05核合同，P07持久校验，P13声明方法边界。PRE用价/edge/stake同源；保留IP原选择规则；退出核对持仓合同。[T04](#t04)、[T12](#t12)、[T14](#t14)必须覆盖正向与拒绝路径。

### H10 — regulation扫描混入advance及缺bid回退ask

**二审源码确认：** live_poller提供advance，inplay_export262–267行将全部source传给regulation scanner；inplay_arb407/564行按home/away共用键消费。其_bid58–61行在缺bid键时回退ask，plain float也被视双边价。advance导出还会删venue后缀，不能只靠后缀修复。此结论是信号消费缺陷，未据此断言paper或Demo成交过晋级腿。

**处理与关联：** [P04](#p04)/[P05](#p05)统一receipt与market_kind/scope/line/period，双场馆等价性来自binding。保留合法二元互补ask与现有advance信号链，不新增advance纸面交易。[T04](#t04)、[T05](#t05)、[T13](#t13)验收。

### H11 — 激活/恢复门禁可能漏掉已持久化但未同步的Demo intent

**二审源码确认：** frozen_strategy_store290–305行只检查paper与mirror汇总；demo_forward395–403行先commit pending intent，408行才HTTP，423行同步mirror。崩溃窗口内mirror可仍为零；尚无证据称生产已实际发生错误切换。

**处理与关联：** [P11](#p11)/[P13](#p13)直接核查intent最新event与未决数量，在统一writer门禁内复核；[P12](#p12)保持独立执行对账。[T13](#t13)、[T14](#t14)、[T16](#t16)验证未知结果、pending sell、部分成交及并发，不清空intent绕过保护。

### H12 — 前向源码/参数升级缺完整旧持仓兼容合同

**二审源码确认：** paper_trading38–41行只hash两份模型文件，191–200行对旧entry hash不符直接拒绝；execution_context用于paper退出和Demo。其他行为依赖改变又未必触发hash，校准记录还丢版本字段；paper_store153–154行又强制entry继承active book的方法版本。这里是兼容性/审计缺口，未断言当前仓位已卡死。

**处理与关联：** [P13](#p13)前置真实方法manifest与旧实现分派或无开放持仓切换；[P08](#p08)参数/输入/校准兼容；[P11](#p11)恢复门禁；[P12](#p12)旧记录不变。[T08](#t08)、[T14](#t14)、[T16](#t16)验收。不得修改旧hash、跳过检查或自动平仓。

## 8. 本次文档交付与后续记录

- [x] 找到Soccer原开发计划位置；本文件放在TRANSFORM_PLAN.md同目录。
- [x] 原20项主张与6项遗漏逐项记录；首审补2项维护风险、二审补4项可达路径问题，共32项（C01–C20、H01–H12）。每项区分已证实代码问题、已证实数据影响与尚未量化部分。
- [x] 再次只读检查两个陷阱及当前目标数据，明确不执行旧删除/清空方案。
- [x] 二审扩为P00–P13共14个工作包，补齐第9节实现合同、第10节T00–T16共17组验收、第11节逐项关闭与限制。下列测试是待办，不能把清单数当通过数。
- [ ] 后续实现/测试/研究重算：尚未开始。
- [ ] 后续生产应用/历史替换：尚未开始，不能依据本计划自动执行。

后续每次修复只更新相应工作包状态并附真实证据，不删除原判断和旧快照数字；如果新证据推翻旧判断，追加日期、原因和源码/数据版本。

<a id="interfaces"></a>

## 9. 二审补充：实现前必须固定的接口、存储与接线合同

本节所有新类型/文件/表/协调器都是**待实现设计**，不是现成命令。实施可调整命名，但必须保留字段语义、约束、调用覆盖和验收；把变更记录在对应工作包。所有schema迁移先在独立候选库演练，不能对生产执行DDL。

### 9.1 源、候选与不可变观察的所有权（P00/P03/P06/P07/P09a）

- `SourceSnapshot`：显式绝对路径、snapshot_id、SHA、schema版本、fixed_as_of、fixture_scope；独立SQLite一致性快照以`mode=ro`及query_only读取。不能让读取器调用ensure/init_db/commit。禁止与候选同realpath或同inode；共享审计快照始终只读。
- `CandidateTarget`：显式独立路径、run_id、scope_id、schema_version、创建时间；候选writer没有生产默认路径或`conn=None→store.init_db()`回退。OutputRoot同样必须显式且属于隔离根。候选事务提交后才生成completed manifest；未完成/不同run输入不能被正式消费者读取。
- 新`market_binding`与`quote_receipt`：binding_id/receipt_id为不可变主键，保存规范payload/hash与schema版本；所有外键指向实际存在对象。精确重放同capture+payload幂等，不同内容是新观察；同ID不同hash报冲突。不能用INSERT OR REPLACE“修正”旧收据。
- 新`milestone_observation_v2`：observation_id、schema_version、run_id/capture_id、fixture_id、milestone/target_id、binding_id、method_version、state_revision_id、receipt_ids、payload、payload_hash、recorded_at、supersedes_id。writer直接接收这份payload，不调用会重新读旧snapshot的record_history_milestone。旧表、旧PK及旧行保留；不得用随意source后缀绕开版本设计。
- 新`price_sample_observation`记录每side/target的采样收据，旧price_tick保持原值。时间戳相同但价格不同保留两次响应，通过声明run选择版本，不能以“最近写入”重解释原决策。
- 不可变`collection_attempt`与可变`collection_task_state`分表。task自然键至少包含scope、fixture、market_kind、target/side、collector_version；state保存last_attempt/next_retry/attempt_count/lease/last_complete及指向已提交观察的引用。修复缺侧不能覆写immutable observation，也不能只加不可变表而没有可查询的到期状态。
- 为旧payload提供只读适配器，缺收据标legacy_unknown；不得反填旧entry/exit/observation。新表字段不得强制所有旧记录迁移后才能读，现有canonical哈希与顺序保持不变。
- `forward_method_epoch`以epoch_id为不可变主键，持manifest_id/hash、实际model/method身份和完整依赖；激活/停用另写append-only epoch事件。新entry的book_version_id仍绑定账本容器，forward_epoch_id绑定真实前向方法，commit前同时核对expected book/epoch及激活时间。每腿exit/completion和执行/校准读取继承此引用；旧entry不补改。该前向版本机制不激活历史回测book，P11保持独立。

### 9.2 市场与报价合同（P04/P05，供所有消费者共用）

拟新增`util/quote_evidence.py`，`QuoteEvidence`统一指下列`QuoteReceiptV1`，避免每个export发明不同字段：

1. **身份**：provider、environment/endpoint_id、fixture_api_id、competition/season、两队API ID及canonical ID、identity_version、binding_id；provider event/market ID、token/ticker、原outcome、本地side。不同provider ID不可冒充API-Football ID。保留market_kind、settlement_scope/rules_version、period、line；advance两回合所属tie/leg也显式绑定。
2. **数值**：ask、bid、reference_price分开，统一0–1且finite；ask_size/bid_size可null。reference_kind区分current/last/history/model；ask_state/bid_state分别available/empty/unavailable/invalid/suspended及reason。0/1结算值可作结果，不自动作可交易报价。二元互补ask保存原对侧bid引用及转换规则。
3. **时间**：request_started_at、received_at、本地durable available_at、provider_quote_at可null及time_basis。规范时间为UTC、保留毫秒或更细原精度；价格采样epoch秒只在边界显式转换。未知provider时间/深度不能捏造，也不等同于本地真实BBO不存在或已成交。
4. **证据**：receipt_id、capture_id/run_id、schema_version、raw_ref/hash、规范payload hash。密钥/鉴权header不进入收据。每腿分别有时间，不用整个导出的as_of代替其时间。
5. **选择**：`SelectedQuote(receipt_id,binding_id,side,action,venue,price)`；`Decision`绑定它、模型概率、edge、stake、选择规则版本。最终写入核对引用、数值、方向与当前持仓合同。对来源为旧裸float/参考价的兼容字段，不能赋予真实执行资格。

必须逐点接线：US `_price/match_quotes/totals_quotes`和Kalshi discovery→`upcoming_export._price_comp_rows/_stash_pre`→`paper_store.observe_pre`；live_poller→两种inplay_export→`paper_trading._snapshot_from_live`与`timing_provenance.live_snapshots`→`pre_decision/_inplay_decision/run_cycle`→`paper_store.record_entry/record_exit`。`decision_model`、两种inplay scanner及hedge/lock调用同一资格函数。旧展示数值可由收据投影，不能独立形成另一套交易资格。

Demo独立链：`DemoBroker.book`→`DemoTickers.for_position`→`demo_forward.run/_send`，保存paper_receipt_id和execution_receipt_id。纸面Poly价与Kalshi Demo实际价可以不同，但fixture/side/意图一致；Demo环境进入binding/cache，不能拿public盘口冒充demo盘口。目标腿足够时不强制另外两腿存在。保留现有30秒核时、状态重验、整数数量、IOC/费用/限额及对账逻辑，合格BBO也不等于保证成交量。新的执行假设/阈值先入方法manifest，不在修复时暗改策略。

### 9.3 时间、历史样本与模型来源（P02/P03/P07/P08）

- `SourceObservation/FeatureVersion`保存source/entity_key、原件receipt、provider有效时间、provider发布时刻可null、首次received/available、revision/supersedes、payload/hash、transform版本及依赖ID。只有available_at不晚于cutoff的已完成版本可用；晚收到的旧日期资料不能据有效日期提前使用。旧无可用时间资料保持unknown。
- 事件以**完整返回集合revision**保存，包含原始顺序/事件精度、删除/重排/VAR修订；不能仅靠当前fixture_event的seq upsert。当前投影可更新，但历史TimelineInput明确选择某份已可见集合。部分事件响应标partial，不冒称完整事件集。
- `TimelineState`输出period、elapsed/extra、target_at或wall_time_interval、clock_basis、score/reds、source_revision_ids、known_at、certainty/reason。研究固定+15是approximate；真实比赛阶段观察和历史分钟区间不能混合为精确秒。未来paper以当时已持久观察的状态为准；缺事件秒级时间时不得宣称已证明事件后成交。
- `PriceSeriesReceipt`保存identity、request start/end/fidelity、received_at、raw hash、实际覆盖start/end/count、median/max gap与quality。`PriceSample`保存target_at、provider_sample_at、price_kind、price_unit、age_seconds、series_receipt_id和不可用原因；历史返回bar不因今天received_at有值而成为旧时已知BBO。原list/tuple调用需要明确适配，不能直接改返回类型使旁路崩溃。
- 采样规则：选`sample_at<=target_at`最后一点且age<=180秒；重复时间同价去重、同时间不同价报冲突，乱序先规范排序；空/非finite/越界拒绝，不插值、不找未来点。恰好边界可接受，181秒拒绝。跨侧devig保留每侧sample时间；时间不齐只能称异步参考篮子，不能当同刻可交易盘口。跨进球/红牌状态变更的旧报价不能因age合格自动取得执行资格。
- `DecisionMethodManifest`包含所有实际代码依赖/参数、market/quote/timeline规则、先验/阵容/FC/Elo/form/xG/altdata/odds/条件motivation输入、bucket cutoff、校准版本兼容集、缓存键与激活时间。派生特征引用完整依赖ID，不能仅用全表MAX时间或两个模型文件hash代替。固定数值回放与原算法版本回放分别标明。

### 9.4 原件、观察与决策的提交协议（P07/P08/P12）

1. raw先写隔离的内容寻址临时文件，校验hash后fsync/原子rename为不可变对象，再允许数据库引用。相同内容复用、不同内容新对象；失败最多留下未引用raw，不允许DB指向缺失或被后写覆盖的文件。同秒相同query不同响应必须保留两份。
2. 一个采集批次的receipts/source版本/事件集/当前投影在同一DB事务写入，不先推进完整性watermark。采用明确的保守可见协议：数据事务提交后追加finalized availability marker，只有marker已提交版本可供决策选择；marker缺失由幂等恢复补记实际恢复时间，不能回填成早先事务开始时间。观察允许延迟可用，不允许早于实际持久完成被历史回放选中。
3. entry或exit在最终写入事务中重新读取fixture的真实kickoff/阶段、当前entry完成状态、所选持久receipt与manifest，校验时间/方向/合同/hash。对应decision、selection、input manifest及终结evaluation一起成功或一起回滚。需拆开现有helper内部commit以形成明确的事务所有权；不得在外层savepoint里调用会主动commit的helper后假称仍有保护。
4. 可重试观察与终结evaluation分开：缺输入不占用永久no_edge标记。相同自然键同payload幂等；不同payload冲突拒绝。entry/exit/settlement并发由最终持久约束及重读保护，不能信任调用者旧内存position。
5. Demo仍先持久intent再HTTP，网络不放进DB写锁；unknown响应先对账，不重发。paper记录必须先成立，才产生对应Demo intent；Demo失败不回写paper决策价或历史收益。

### 9.5 采集scope、重试与候选资格（P06/P09b/P10）

`CollectionScope`必须显式包含scope_id、固定ordered fixture_ids及hash、market_kinds、UTC window、fixed_now、scope_basis、fixture/request预算。日常由现有14天和有效赛事生成；研究用源快照固定清单。普通/advance/ticks及外层due_tasks消费同一scope；空scope零请求，超预算移至后续，不堵实时主循环。

`CollectionRunResult`至少区分网络枚举完整性、身份解析完整性、目标市场/侧覆盖、采样时间质量；逐项有attempted/complete/retryable/unsupported/conflict及next_retry。partial市场列表中的已确认市场可继续使用，缺项不能判not_listed。缓存命中保留原接收时间，warm旧完整索引不能刷新成新报价；只有持久完成才更新last_complete。RunStatus适配器在live/full/settle wrappers读取这些结果，partial不吞掉，也不阻断无关fixture的合法结算。

纸面状态机（后端，不增加UI）：

- `waiting_data/retryable`：缺价、请求失败、身份待核查、状态变更、模型未就绪；记录缺项和下一次尝试，保留原PRE/IP窗口和预算。
- `evaluated_no_edge/hold`：本方法所需模型/报价集合已合格且实际评估；不以“任意一侧非空”取代该条件，也不强制所有策略都等全三侧。
- `entry_committed/exit_committed`：原子保存对应最终记录，下一轮不得重选。
- `window_expired/fixture_final/no_market_confirmed`：区分缺数据错过窗口、终场和经完整发现确认无市场；不作为亏损、不冒充no_edge。过窗后不能补“前向”交易。

`RequiredInputManifest`由`analysis_purpose`固定，执行前冻结：固定原腿价格敏感性只要求该腿及声明的退出输入；完整PRE/IP择时必须覆盖规则所消费的所有候选侧、决策时刻、退出路径、事件/模型版本；advance是单独支持能力与两侧结算范围。FT是结果，不能作为第七份交易报价。改用途/输入资格/容差需新分析版本，不能看PnL后改分母。

每fixture×track/目标有可审查结论：`reconstructed_usable`、`retrospective_reference_only`、`time_ambiguous`、`identity_unresolved`、`source_unavailable`、`unsupported_market`；暂未完成为`pending/retryable`。`scope_total=usable+terminal_excluded+pending`并逐ID验证，不能只对总数。无入场、无edge、不可估计不硬生成金融腿。原行保留数、合格研究数、实际入场数分别统计。

### 9.6 候选必须被真正消费（P09/P10/P12）

拟新增`CandidateMarketData`只接受显式run_id/scope和已完成输入manifest；提供`state_at/quotes_at/features_at/exit_path/result_at`，不得隐式读默认store。所有研究entry/exit使用注入对象或纯计算函数，不再回退原tick/milestone/frozen exit。缺输入返回结构化结果，`unavailable/invalid`与`held_no_trigger`分开。

研究分两条独立输出：固定腿敏感性保存原side/stake/时点，仅比较声明的价变化；完整重决策重跑该版本所有合格机会、side/stake/exit并保留eligibility/no-entry记录。`build_candidate`按可计算的单向顺序生成：先固定input/scope manifest及hash，ledger引用它，再由completed manifest引用ledger_hash、input/scope_hash与全scope终态。禁止两个文件互嵌对方完整SHA形成循环依赖；register同时校验这条证据链和逐ID覆盖，不能只看给定ledger自洽。

候选图价和PDF预览使用显式candidate report/marks读取器，原milestone_export默认继续保持原金融记录。未激活候选不能调用只接受active book的publish_strategy_views冒充正式发布；拟新增纯report_from_book/隔离preview入口，不调用会consume的build(freeze=False)。金融字段仍按三件统一投影，缺价格轨迹不能删已存在交易。

### 9.7 writer、切换与恢复（P11/P13；只在隔离环境演练）

必须覆盖当前入口，而非只列三个定时器：live_refresh的settle/paper/Demo；refresh_all；settle_reports；performance_report；standalone milestone_export；standalone settle_bets/kalshi_mirror --once；可直接调用的paper_trading.run_cycle/demo_forward.run；refresh_and_deploy发布包装。具体行号见二审账本报告。当前锁嵌套包括live_refresh→refresh_all与refresh_deploy→refresh_all→publication_lock，proc_lock非可重入；不要反向取锁或嵌套取得同名锁。

拟新增Soccer版本维护门禁，所有这些writer在最终持久写入/发布前遵守；协调器先关闭新writer准入、等待已进入流程到可对账终态，再于排他段重查active version、paper/Demo开放数量与每个durable intent最新event。未知外部请求不能靠等待时间推定失败。协调器不调用有交易/settle/consume副作用的报告构建函数，不为切换撤单或平仓。

固定执行阶段：validate candidate+expected_active→准备code/params及初步恢复包→writer门禁及持仓复查→在排他段固定最终恢复包→隔离stage与全量验证→版本激活/提升产物→复核实际DB及各文件hash→完成journal后释放。门禁前备份仅为准备；排他段重新核对book_version/ledger_id/as_of、canonical行前缀/hash及六文件hash。即使book版本未变，也可能刚追加完成记录；发现变化须中止旧切换尝试、重取最终恢复包，并核查候选是否遗漏新增金融记录，不能只比较expected_active_version。最终恢复包持久完成且候选范围重新通过后才允许activate/promote。每阶段记录durable journal，恢复时以真实状态核对，不能仅信journal称已完成。

现SQLite与六文件没有联合原子提交，ExportStage只保证单文件替换及进程内异常恢复。必须在方案中明确短维护窗口/恢复期间的可见性限制；若要求所有外部读者在任意崩溃下永不见混组，则需额外实现版本目录+单一发布指针及消费支持，现有代码不能满足，不能按测试无异常就声称原子。这是生产迁移前的待决能力，不能本轮擅改前端或全站发布。

恢复包绑定旧代码依赖、param_selected、manifest、book指针及六文件；数据库restore不代表外部已部署内容或订单已恢复。恢复也需旧模型兼容/无未决执行门禁，出现新entry/成交后不能绕过。实际部署、停止任务、账户操作与历史替换均不属于当前文档工作或生产外实施的自动下一步。

<a id="acceptance"></a>

## 10. 待执行验收矩阵：每组同时验证成功路径与拒绝边界

下列17组应放在`<work_root>/tests_external`，文件名可对应组名。全部使用隔离导入、独立DB、冻结时钟、固定响应和fake broker；没有生产测试命令。每组输出机器可读结果、输入hash、代码hash和失败明细；数值精度按声明规则比较。原412测试只有实际在该环境运行并审查失败后才能列入新证据，不继承旧结论。

<a id="t00"></a>

### T00 — 隔离与路径拒绝（P00/P09a）

- [ ] subprocess验证错误cwd/PYTHONPATH、默认DB/Output、symlink/hardlink、继承凭证、pycache/字体缓存/日志/锁/子进程写入都被边界拒绝或重定向，拒绝发生在写生产前；合法工作根路径可正常建候选。记录所有导入模块真实路径与写入审计。World Cup/生产源码无本次写入，源快照SHA不变。

<a id="t01"></a>

### T01 — 危险入口退役与冻结不变（P01/P12）

- [ ] 修改后的_refreeze入口导入/CLI均无DB/输出/交易调用；backfill_inplay apply仍拒绝。副本输入价/比分/raw变化不影响旧canonical bytes/ordinal/逐行hash/累计；frozen UPDATE/DELETE/REPLACE拒绝。合法未来completion恰好追加一次，不把基线总数硬写成352/353。

<a id="t02"></a>

### T02 — 时钟/完整事件集/结果可得时间（P02/P07）

- [ ] 1623407固定证据复现旧19:00/.135与近似候选19:15/.445、T75 19:30/.55；额外覆盖延迟开球、补时、延长中场、45+8、90+、AET/PEN与Missed Penalty。真实ingestion→A事件集→B删除/重排→as_of回放仅见当时版本；advance与regulation结果不混，结果首次可用前不结算。未知秒级时间保留区间，不能硬过“精确PIT”。

<a id="t03"></a>

### T03 — 所有采样调用贯通（P03）

- [ ] 主milestone/advance PRE/rederive及两种tick请求都经过固定reader响应→180秒因果采样→候选writer→读取器。160/.2和220/.9在target200取.2；覆盖180/181秒、相同/冲突ts、乱序、空/NaN/越界、PRE目标后bar、879粗点/200细点收据与单位往返。unsupported advance零请求且不伪complete；受支持样本两侧正常。

<a id="t04"></a>

### T04 — 报价方向、选择一致与市场范围（P04/P05）

- [ ] PRE同侧Kalshi .40/Poly .50时entry与Decision所选价、venue、edge/stake引用一致，覆盖value/argmax；IP保留原优先规则。reference-only不形成可执行BBO，缺bid键/值None都不回退ask，合法二元互补保留。regulation .60与advance .10不混；goals2.5与corners10.5不拼lock。真实同scope BBO仍产生预期信号，退出必须匹配原合同及bid。

<a id="t05"></a>

### T05 — 身份、token及别名（P05/P06）

- [ ] 1623437重复OFI token不能当CSKA；找到正确证据才恢复away，未找到记未知不调换凑数。覆盖[No,Yes]反序、重复市场、同两队不同赛事/日期、主客互换、中立场、单目标Demo腿及相冲突第三队；打乱输入顺序不改变唯一结果。Twente直/弯引号恢复、Qarabağ/Brighton现映射不退化；NFC/NFD和fold碰撞/二队保留拒绝。alias propose不写正式表，版本变更后新查询重载，旧binding不变。

<a id="t06"></a>

### T06 — 分页、能力、scope与外层重试（P06）

- [ ] Global/US/Kalshi分别覆盖第二页失败→恢复、429、循环cursor、截断、warm/cold缓存、无支持/确认无市场/请求失败。已有FT但缺PRE或一侧时live外层确实触发；范围外旧缺FT不循环请求，空scope零网络，advance/ticks不扫全历史。真实wrapper输出partial，last_complete只在持久完整时推进；fixture A失败不妨碍B正常处理。

<a id="t07"></a>

### T07 — 观察版本、raw与原子提交（P07/P08）

- [ ] 同秒同query不同raw并存；旧milestone/tick不变、v2两次缺侧→补齐可读、同内容重放幂等、同ID异hash拒绝。故障注入raw写后/SQL中途/数据commit后marker前/decision事务各边界，均无悬空receipt引用、无未持久input对应decision。live_snapshots及observe_pre往返不丢provenance；并发exit/settle以最终持久状态为准。

<a id="t08"></a>

### T08 — 输入as_of、参数与缓存（P08/P13）

- [ ] FC直接重建、ClubElo文件更新、match_odds修订、晚到赛果/xG/VAR等B版本到达后，cutoff前仍读A；同manifest离线概率一致。日bucket第一次输出不被后来输入改写；选中参数明确注入而非默认替换。兼容校准包含版本并按result_available_at过滤，Demo未成交不影响合格paper校准；关闭motivation不虚构其依赖，开启必须可追溯。

<a id="t09"></a>

### T09 — 缺数据恢复与有效窗口（P04/P06/P07）

- [ ] 首轮currentPx-only/次轮真ask在窗口内产生且仅产生一次entry；缺bid→合格bid、缺模型→恢复也能继续；合格no_edge只封一次。锁等待跨kickoff/IP截止/TTL后拒绝旧请求，持续缺失到过窗记missed-data而不是亏损/no_edge；未来不得补造已经错过的实时交易。

<a id="t10"></a>

### T10 — 候选保留、读路径及分母（P09/P10）

- [ ] 全请求失败/单侧缺失/无180秒bar/身份冲突/半途崩溃均保留源库与完整scope。只变ts也计入diff；失败不记ok，恢复不混run。把旧表置高价、候选置另一价并删除一个候选输入，完整entry/exit/marks必须只读候选或报unavailable，绝不回退旧表。scope按ID恰好分解，改变必要输入集合需新manifest。

<a id="t11"></a>

### T11 — 逐腿财务与双对照（P10）

- [ ] 固定原腿敏感性与完整重新择时各有scope/manifest；PRE hold、择时实现、IP实现、相对hold改善、费用分开。分数paper与整数Demo分别复算，逐腿=逐场=全量累计，0.1¢及¢/USD转换正确；未知费用/路径不补0。验收四次退出587.7¢与909.7¢分解，不要求总收益下降到引文区间。

<a id="t12"></a>

### T12 — PRE/IP真实schema端到端生命周期（P04/P07/P12/P13）

- [ ] 至少各一个保存的真实响应schema完整经过reader→绑定/receipt→durable observation→模型输入→Decision→paper entry→同合同bid exit或结果结算→冻结账本。必须有正向入场及退出，重复轮询/导出不重复交易；未结算不记亏损。新日只追加新记录，旧payload/累计逐字不变；不能全mock成空结果来通过。

<a id="t13"></a>

### T13 — Demo独立盘口与幂等（P04/P11/P12）

- [ ] paper Poly entry、Demo仅目标腿存在时按原side尝试，paper/执行收据分开。手续费等待25秒+锁等待20秒不得POST；pending/unknown不重发，部分成交按原规则只处理剩余量；缺market/空簿恢复后可继续，paper已exit时用fresh执行bid且不重选信号。二元互补ask和当前限额保留；全用fake broker，真实网络调用数0。

<a id="t14"></a>

### T14 — 版本门禁与旧持仓（P11/P13）

- [ ] register不等于activate：缺scope/未完成/错hash/虚假同模型改名拒绝；合法候选可登记但不隐式激活。activate/restore遇expected_active不符、paper开放、Demo余量或durable pending/缺event皆拒绝，覆盖mirror为0但intent已commit。修改两模型文件及仅改参数/辅助依赖都触发正确兼容检查；旧entry要么继续原版本退出，要么明确阻止切换，不能改旧hash过关。前向epoch更新可保持book及旧历史不变，新entry不得被record_entry覆盖成旧方法；若实现允许两epoch并行，同fixture的PRE/IP completion也须分别保留原epoch，执行/校准/导出不混。

<a id="t15"></a>

### T15 — 三件金融记录、PDF与失败发布（P12）

- [ ] output/frontend_data六文件的report.bet_log、嵌套ledger.records、milestone每场strategy_record与PDF全量行顺序、entry/exit/stake/PnL/三累计/版本一致；允许行情marks和顶层as_of变化。PDF逐页提取全量、核末行与跨页续表并渲染检查，缺轨迹仍保留金融记录。生成/PDF/中间提升失败保留或明确恢复旧完整组，候选预览不修改active或触发consume。

<a id="t16"></a>

### T16 — 多进程、逐阶段崩溃与恢复（P11/P13）

- [ ] 隔离live/report/standalone demo/publisher/切换协调器并发，遵守锁次序无死锁、不混方法entry、不重复HTTP intent。初步备份后、门禁前追加一条同book版本completion，切换必须发现ledger/前缀变化、重取最终恢复包并拒绝漏新记录候选。注入register/activate/DB commit/每个文件替换/journal失败等崩溃点，按实际DB/文件hash恢复；门禁覆盖已经进入HTTP的unknown结果。分别核DB指针、代码/参数、六文件恢复，外部部署和成交不能冒称已回滚。若无法满足选定可见性保证，生产迁移仍阻断。

<a id="closure"></a>

## 11. 覆盖核对与关闭标准

### 11.1 32项问题的测试追踪（不是测试结果）

- C01/C02 → T02/T03/T10；C03 → T02/T10/T11；C04/C05 → T03；C06/C07 → T02/T03/T08/T11。
- C08 → T02/T07/T09；C09 → T04/T11；C10 → T13/T15；C11/C12 → T01/T06/T07/T10。
- C13 → T05/T06；C14/C15 → T05/T10/T11；C16/C17 → T01/T10/T14；C18 → T08/T11/T14；C19 → T00/T03及实际新测试清单；C20 → T01/T08/T12/T15。
- H01/H02 → T02/T07；H03 → T05/T10/T11；H04 → T04/T07/T09/T12；H05 → T03/T04/T07/T10；H06 → T02/T07/T08/T10。
- H07/H08 → T05/T06；H09 → T04/T12/T14；H10 → T04/T05/T13；H11 → T13/T14/T16；H12 → T08/T14/T16。
- P00及所有工作包另受T00生产外隔离约束。T16属于后续迁移能力的隔离验证，不意味着本计划批准生产切换。

### 11.2 每项完成必须提交的证据

每个C/H条目保留`status、affected_functions、patch_id、tests/evidence、input_scope/hash、before/after、related_issue_ids、behavior/method_change、remaining_unknown、production_status`。一个测试覆盖多问题时逐项说明断言，不靠包级“全绿”代替问题关闭。变更过的共同函数要运行其所有消费者回归，不能只测直接调用者。

分别登记：**生产外代码修复、前向采集/决策验收、历史可恢复性、候选研究、生产应用/历史替换**。用户未授权的生产应用保持未执行；这不会被偷偷作为“完成修复”的附带动作。真实实施前重新冻结当前工作树及输入并核对源码变化，行号漂移按函数定位，不照抄旧补丁。

已确认且可修的软件缺陷应有实际补丁及T00–T16对应证据；具有足够存档的数据必须真实产生可审查候选，不允许统统标unknown省掉修复。历史缺BBO/秒级事件/首次可得时间/正确token时，应列查过的存档、缺字段、无法补证原因和影响ID；可以完成调查与拒绝路径，但不能称这些历史已经恢复或严格PIT已证明。

完整性分开验：pending=0才说明本轮固定scope调查闭合；terminal_excluded不是恢复成功。若usable=0，P10只能报告不可估计，不能用空ledger激活覆盖旧历史。前向验收必须有真实schema正向entry/exit/结算及失败后恢复，零报错且零决策不能算通过。正确代码无法保证供应商一定提供所有盘口或成交，缺盘口不等于程序错误，但必须有可追溯原因和恢复机会。

### 11.3 本计划可作出的承诺边界

本计划覆盖第7节列明的assessment及二审遗漏、冻结历史、三件统一、Demo独立执行和World Cup不变约束。不能据此声称以前所有运营/UI请求均已修好：例如欧洲阵容覆盖、API-Football采集健康、赛事标题、venue余额/开关显示以及过去24小时前端改动审查，仍需各自当前源码/数据验收；若不在本计划的32项及测试证据中，完成时不得顺带宣告解决。也不在本计划中顺手修改其前端。

交付时报告“哪些软件缺陷已在隔离环境修复、哪些历史仍不可认证、哪些未部署”，以实际证据关闭问题。**不能在执行前保证历史缺失信息一定可恢复，也不能用收益符合某个预计区间证明修复正确。**
