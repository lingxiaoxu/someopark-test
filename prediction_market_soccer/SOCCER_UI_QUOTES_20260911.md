# Soccer 队名与 Kalshi 报价修复记录

本记录对应 2026-09-11 用户最新截图：中文页面的 Defensa y Justicia 未翻译，Sevilla–Valencia 显示 Kalshi 报价不可用。所有开发、构建及回归均在 `/private/tmp/soccer-ui-20260911T0747Z` 及两个专用隔离目录完成。实际生产结果以文末发布凭证为准。

## 查明的问题

1. **身份已解析，但中文词条内容错了。** `src/components/soccer/soccerLabels.tsx::clubName` 成功取得 `soccer.club.defensa_y_justicia`，而 `src/i18n/locales/zh.json` 的值本身是 `Defensa y Justicia`，所以没有触发备用中文名。不是 API-Football key 或球队 canonical ID 丢失。
2. **现网前端队名目录落后于后端。** 前端构建时导入 `prediction_market_soccer/config/club_identity.json`；前几轮只更新后端后，旧线上 JS 仍少 30 个已审核别名。前后端解析器逻辑一致不代表运行中的目录版本相同。
3. **Kalshi 合同编号保留旧日期。** 本地与本次真实 API-Football 请求都确认 fixture **1570381**、Sevilla/API536 对 Valencia/API532，开球 **2026-09-11 19:00 UTC**。Kalshi 真实存在 `KXLALIGAGAME-26SEP13SEVVCF` 三路 active 合同；旧 `_entry()` 仅按 ticker 日期与 fixture UTC/纽约日期比较，返回 `event_not_found_in_complete_catalog`，并未请求该合同 BBO。
4. Kalshi 官方唯一 milestone 的 `start_date` 已为 **9月11日19:00 UTC**，`main_game_event_ticker`、primary/related 关联明确指向旧 Sep13 合同，结构化主客/平局 UUID 与三市场 `custom_strike` 相同。真实订单簿有三路报价。官方 type 恰为 `soccer_tournament_multi_leg`，不能只凭这个标签推断是 aggregate 盘口；实际合同规则明确常规90分钟、含补时、不含加时和点球。
5. **Demo 合同发现有同样的日期遗漏。** Demo 自己的完整目录也包含同一旧日期的三路合同；Demo 自己的官方 milestone 同样明确当前开球时间。`exec/demo_forward.py::DemoTickers.for_position` 原先在球队匹配之前按日期过滤，因而即使公共报价恢复，也仍会跳过该场 Demo 合同。

6. **真实调用入口漏传状态。** 首轮v6发布的源码、金融记录与HTTP检查通过，但浏览器仍提示目标Kalshi报价不可用。`upcoming_export.py` 的SQL已有 `status_short`，随后先调用 `fixture_identity()` 只保留身份字段，导致新的严格赛程核验看到 `phase=None`。`jobs/live_poller.py::_live_quote_sources` 有同样的投影；`inplay_export` 复用该入口。Demo直接传原fixture，本身没有这处丢字段问题。不能把缺状态当作NS，也不能改全局身份schema把动态状态加入合同ID。

## 精确改动

- `someo-park-investment-management/src/i18n/locales/zh.json`：仅四个 `soccer.club` 叶子。Defensa→国防与司法、Always Ready→拉巴斯准备、Montevideo City Torque→蒙得维的亚城扭矩、Torreense→托伦斯。原球队身份及英文、日文、法文、西班牙文显示保持；专有名和合法缩写不机械翻译。
- `prediction_market_soccer/config/club_identity.json`：只为上述四队追加10个核验过的中文别名，保留409个ID、旧别名与其他405条记录。
- `prediction_market_soccer/venues/kalshi/discovery.py`：只有原日期匹配失败、双方球队已经精确识别时，才以官方当前 milestone 验证改期关联；不扩大日期容差、不按相似名字拼接合同。
- `prediction_market_soccer/venues/kalshi/market_data.py`：明确 event 的公开 milestone GET 与原始收据。常规匹配不多请求；fallback 每次最多两个精确候选，缓存保留真实请求时间。分页不完整、歧义、时间/身份/规则冲突保持拒绝。
- `prediction_market_soccer/exec/demo_forward.py`：只有原有候选为空时，使用本 broker 已取得的完整 Demo 目录和 Demo 官方 milestone 进行同样的严格核对。仅接受已有两个 Demo host，不取生产元数据；正常五场的三个方向均保持原 ticker 与 binding，且不增加 metadata 请求。订单提交、未知订单恢复、风控阈值与真实资金开关没有改动。

独立审查额外复现了 metadata GET 默认跟随 302 可能把生产回应误记为 Demo 原件的问题。最终版本显式 `allow_redirects=False`，拒绝所有 3xx、非空 redirect history、实际 response URL 与请求端点不符。保存的原始 body 与 `requests.Response` 离线重放证明修前会误接受跨 host 回应；真实 `requests.Session` 与离线 Adapter 则验证修后只发生原 Demo 一次 GET，既不请求 public，也不生成 binding。原失败证据与修后通过证据均保留。

改期验证必须核准当前fixture与season、目录完整、唯一官方关联、显式带时区且精确一致的开球时间、三路唯一UUID/市场、active状态、90分钟规则以及请求原件哈希与新鲜度。原ticker、原日期、原规则和取消/改期条款完整保留；不把expected_expiration当作原始kickoff，也不声称已证明任意改期符合48小时条款。

后续两处入口修正仅在quote context附带SQL读取到的真实 `status_short`：`ops/upcoming_export.py` 与 `jobs/live_poller.py`。`fixture_identity`、binding schema、合同ID及状态拒绝规则保持不变。必须通过实际PRE producer与实际LIVE source factory回归，不能再以手造保留状态的dict替代完整调用路径。此补充已通过隔离验证，并完成v7独立备份、生产切换与真实UI验收；v6原始验证和失败UI证据保留。

## 前端发布范围

现网原 JS `/assets/index-D605HZRN.js` 已通过真实GET与本地dist、Hosting文件哈希双重核对。为了不把其他未提交改动带入全站，发布候选基于这一已运行资产，只替换两个静态 `JSON.parse` 参数：Soccer中文资源（仅四叶子不同）和Soccer身份目录（同步30个前期别名＋本次10个别名）。其他全部JavaScript字节原样保留；源码修改仍落在既有中文locale及后端catalog，未来正常构建会读取这些正式源文件。

原Hosting共134个文件；仅替换index引用并新增有内容哈希的新JS，其余原133文件、CSS和hosting配置逐项保持。旧JS也保留供现有客户端加载。没有修改世界杯、Macro、布局、组件或其它语言资源。

## 验证与边界

- 409队×5语言共2,045个显示名都能返回同一canonical ID；5,517个Python/TypeScript Unicode变体对照、5,497个既有变体检查通过。7项Python、12组实际组件SSR及TypeScript检查通过。这证明映射一致性，不声称已逐个完成全部2,045个名字的独立语言编辑认证。
- 隔离Vite完整构建成功，实际浏览器预览检查中/英/日/法/西五种语言；中文显示“国防与司法”。共享构建产物只用于验收，未用来覆盖现网站点。
- Kalshi真实原件离线回放中，目标三路ask为50/29/23¢，另外五张卡片原quote/receipt逐字段不变。该价格属于采样时刻，实际线上价格应以新的实时请求为准。
- 目标公共/Demo匹配、身份/UUID、常规时间规则、目录歧义、状态与时钟、端点及重定向拒绝：最终 owner 69 项、独立 reviewer 70 项（含同一69项及独立重定向复现）均通过，不能把这两组相加为139项独立覆盖。这里只证明合同身份及报价接线，不以离线测试声称实际成交。
- 最终完整回归 **318 passed，363.61秒**，测试后260份正式源哈希未变。第一次搬移测试目录时漏带只读原始fixtures，291项通过而27项因缺文件失败/报错；补齐原归档副本后完整重跑全绿，没有改正式源码或原测试。首轮日志也保留。
- 发布迁移共有34个独立场景：主轮33项通过；另一项实际被更早的“两目录不一致”守卫正确拒绝，但外部断言误期待后层错误文字。只修测试断言后该项单独通过（17.05秒）。不声称单轮34项全绿。包含实际12模型、commit后硬退出恢复、四个安装中断点、重复激活及历史金融行/六文件保持验证。
- 历史359场、618条策略腿保持原冻结book；本次名称/报价发现修复不重算历史，不把Demo成交子集当作全模型收益，不新增用户反对的UI来源说明。

## 实际生产发布与后验

前端已发布并真实在线读取验证：Hosting version `projects/692205032293/sites/someopark/versions/99f5c898aef2bdb4`；新JS `/assets/index-soccer-7588b19e7ad0a3b8.js` 的 SHA256 为 `7588b19e7ad0a3b8e1d8500c70a9b29b315ec863210fd956aa20f5b47196a735`。实际浏览器已显示“国防与司法”。原133个非index文件、原CSS和Hosting配置保留，原JS仍可访问。正式中文源码SHA为 `9b454de1606c1bd9c98ea9d82f90145d3d2b906e09a7782b12f350056fb7bc21`。

v6阶段的四个既有后端文件已在 GitHub 私有分支备份，该阶段源码提交为 `fe4ac700d334ace946bc3e683462d229bc91bf18`；原备份 main `1108f520a0c2d7bdd33b83519ecf3f1ad23e9b34` 未变。v6发布政策只允许这四源及身份manifest变化；v7另有下述两个调用入口修正，本轮后端合计六个既有文件。新模型epoch不继承旧epoch的兼容性，不改变模型参数或交易规则。安装后必须用全新进程激活、准备12个实际模型。任何未结纸面仓位、未知/待决Demo、待消费结算或金融文件变化都会阻止发布。

v6于 **08:24:20.076323 UTC** 激活，**08:26:40.417161 UTC** 完成12模型验收，**08:29:34.706488 UTC** 发布当前视图，**08:30:14.707217 UTC** 恢复三项原调度。14次本地/远端GET全部通过、源文件260/260一致、金融记录359/618一致；但目标实际UI仍为 `schedule_metadata_unverified`，因此不能将v6单独标为报价已修复。完整证据保存于持久备份 `postflight-v6/`。

v7最终两源已备份到私有GitHub提交 `08da110b9677867eae8305af6c3bd271e52697ce`。既有相关回归 **304 passed，34.82秒**；另17项实际producer集成和23项独立回归（含这17项及新增6个状态拒绝案例）通过。v6完整318已通过，v7未重复14项实现未变的昂贵模型认证，不把这几组相加为独立覆盖数。两源码外的258份正式文件保持原SHA；全局fixture identity与binding schema未改。

v7聚焦发布演练 **18 passed，331.38秒**：真实v6一致副本升级到7条epoch/activation、12个真实模型准备、两个安装中断点、epoch提交后的硬退出恢复、重复激活、身份不变及未决金融状态拒绝均通过。历史359条完整账本记录和六份金融文件不变。最终适配器SHA `052f5281b43751c5fd7895030836a04630d2aea0acb2a6062af590f42e6c035d`，政策SHA `9c0bf849f1b1bcf87c2b43f484a95122f50cb946134d78c40e34bf931c9624ba`；260源码清单SHA `0b10585cb5c0816dd7f532f785fa95bc06c5b55d75efe87842580b66a56a80de`。

v7在生产于 **2026-09-11T08:46:32.731180+00:00** 激活，**2026-09-11T08:48:54.002720+00:00** 完成12模型验收；最终epoch为 `f5439b6380c285d245ebfbe2590eea642c115cc19e4b5a51f25c572b7089942a`、method为 `soccer-timing-integrity-20260911-v7`。安装和激活期间，旧金融表、359条完整记录、618条策略腿、六份金融文件和身份manifest均保持。生产单独的一致备份及journal位于 `release-v7/`，没有复用测试副本作为生产库。

最终当前视图于 **2026-09-11 08:51:58.795488 UTC** 发布，三个原Soccer服务于 **08:52:34.591156 UTC** 全部恢复。08:53:18起的14次本地/远端GET全部HTTP200，七类文件两端字节相同；260份正式源全部匹配、当前epoch与12模型验证通过、359场/618腿完整金融记录及PDF metadata一致。生产后验一致快照SHA为 `d4de1ba13fe5ffdb480139a10b99cab0f364a0a20090ab032b23baf4b92feb1c`。

**截图六场的Kalshi三路报价全部为ok。** 目标1570381的新实际报价为主胜50¢、平29¢、客胜23¢（当前文件生成时间08:51:54.776059 UTC，价格会随后变化）。真实浏览器重新加载后已显示“Kalshi 现价”及三路数字，原黄色不可用提示消失，中文队名仍为“国防与司法”。页面中的“下注”是策略建议，不能当作已成交凭证。

Demo模拟镜像为开启状态、待确认订单/足球未平仓为0，余额采集状态ok；最终验收时现金为US$1268.87，采集时间 `2026-09-11T08:51:56.448229+00:00`。页面实际显示“模拟镜像已开启”，真钱开关仍关闭。现金属于共享账户，不能把余额变化归因于Soccer收益；本轮没有为验收额外发订单。

其他比赛或历史采集仍有真实不可用项，operations整体可能显示degraded；14次后验已保留这类warnings，没有将其伪装成全部健康。旧359场不因本修复取得完整历史PIT认证。最终验收快照中，v7的 paper_entries=0、demo_intents=0，尚无该epoch未来真实入场、成交或结算可证明；已有定时跟进会继续收集实际事件，本轮修复不替代这些证据。

## 私有备份与证据

本轮持久备份根为 `/Users/xuling/.codex/backups/soccer-ui/20260911T0747Z`，包含before一致数据库、370份源文件、实际现网资产/Hosting版本清单与后续发布凭证。原始资料不进GitHub；仅可追踪的Soccer源码及本开发记录按既有私有备份分支提交，gitignore保持不变。

译名来源、逐项测试和两份窄补丁见隔离交付 `soccer-club-localization-20260911-h4x5ylca/evidence/readiness.json`；Kalshi真实目录、官方milestone、订单簿、API-Football回应和合同规则见 `soccer-sevilla-kalshi-g92o9ybs/evidence`。持久归档以本轮备份manifest记录的来源路径和SHA为准。
