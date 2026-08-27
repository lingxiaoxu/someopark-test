# prediction_market_soccer — 欧洲/南美俱乐部足球预测市场模块改造计划

> **来源模块**:`prediction_market/`(世界杯 2026,已完赛,104/104 结算,系统处于赛后静止态)
> **目标模块**:`prediction_market_soccer/`(本目录)——欧洲五大联赛 + 欧战 + 南美,以**俱乐部**为主题
> **方法**:先复制、后修改(不重写);每个功能与世界杯模块一一镜像;完全独立运行,零影响世界杯代码
> **状态**:PLAN ONLY——本文件是本目录当前唯一内容,未复制任何代码,未改动任何现有文件
> **写作日期**:2026-08-25(所有 Kalshi / API-Football 调研数据均为当日实测)

---

## 0. 五条铁律(用户指令,凌驾一切实现细节)

1. **最小修改,先复制再改**:所有代码从 `prediction_market/` 复制,只改必须改的;禁止"顺手重写"。**复制方式 = 整目录全量复制(用户强指令 2026-08-25):`prediction_market/` 里面的东西全部丢进来(代码+数据+raw+logs+缓存,一个不落),绝不一个一个挑文件——挑着复制会丢;全部过来之后再改。复制时必须保住本文件(`TRANSFORM_PLAN.md`),绝不先清空目标、绝不 `rsync --delete`。**
2. **平行镜像**:soccer 的每个功能与 worldcup 逐一对照(同名文件、同名导出、同结构 JSON、同名测试),差异只来自赛制本身。
3. **零影响世界杯**:不改 `prediction_market/` 任何文件;不写它的 `wc.db`;不覆盖它在 `public/data/` 的任何 JSON;不动它的三个 launchd 任务。世界杯模块下线了 soccer 也能跑,反之亦然。(注:世界杯已完赛,其三个 launchd 任务处于**赛后空转**——live 每 30s "no match window — skip"、trigger 恒 SKIP、daily 快速跑完,日志实证;soccer **不依赖也不打扰**它们,运维重心 = §6.0 保证 soccer 自己的进程可运行。若将来要停用世界杯三件套,另行征得批准单独操作,非本计划范围。)
4. **后台程序全套复制适配**:refresh / live / trigger 三条管线 + plist 全部有 soccer 版(新 label、新日志、新水位文件)。
5. **前端尽量一模一样**:只是内容换成 soccer;唯一结构性适配 = **"联赛 → 比赛"两层层级**(世界杯是单一赛事直含比赛,soccer 是多联赛各含比赛),前端与后端 JSON 都要为此加一层 `league` 维度。

其他继承约束(项目记忆):`~/Library/LaunchAgents/` 安装必须用户明示批准;firebase deploy 用 `firebase deploy --only hosting`(不加 npx);仓库是 **public**,密钥与数据一律 gitignore;conda 环境 `someopark_run`。

---

## 1. 范围确定(2026-08-25 实测 Kalshi 覆盖)

### 1.1 Kalshi 足球覆盖总量

Kalshi `GET /trade-api/v2/series?category=Sports` 共 3,511 个系列,其中足球相关 **~1,365 个**,按"联赛族 × 市场类型后缀"完全标准化:

**市场类型后缀分类学**(每个联赛族基本都配齐,是发现层的键):

| 后缀 | 含义 | 世界杯对应物 |
|---|---|---|
| `GAME` | 单场 3-way(主/客/**TIE**) | `KXWCGAME` |
| `TOTAL` / `1HTOTAL` / `2HTOTAL` | 大小球 | `KXWCTOTAL` |
| `BTTS` / `1HBTTS` | 双方进球 | `KXWCBTTS` |
| `SPREAD` / `1HSPREAD` | 让球 | `KXWCSPREAD` |
| `SCORE` | 正确比分 | `KXWCSCORE` |
| `TEAMTOTAL` | 单队进球 | `KXWCTEAMTOTAL` |
| `CORNERS` / `TCORNERS` | 角球(总/单队) | `KXWCCORNERS` |
| `1H` / `2H` | 半场胜负 | `KXWC1H` |
| `FTTS` / `FIRSTGOAL` / `GOAL` / `ANYGOAL` / `SOA` / `AST` | 首进球队/射手props | `KXWCFTTS` 等 |
| `MOV` / `MOF` | 获胜方式 | `KXWCMOV` |
| `ADVANCE` | 晋级 2-way(世界杯=单场;俱乐部杯赛=单场或两回合 tie 级) | `KXWCADVANCE` |
| (族名裸系列) | 赛季冠军 | `KXMENWORLDCUP` |
| `TOP4` / `TOP` / `TOP2` / `TOP6` / `TOPX` | 赛季区间(欧冠区等) | `KXWCROUND`(reach-round 的类比) |
| `RELEGATION` / `LAST` | 降级/垫底 | (无——世界杯没有) |
| `LEADER` / `POINTMARGIN` / `TEAMPOINTS` / `H2H` / `SEASONSTAT` / `POY` | 赛季衍生 | (无) |

### 1.2 上线范围(12 项赛事,单一标准,不分级;全部当日验证有开放盘口)

> **用户指令(2026-08-25):不区分 Tier——12 项赛事按同一个标准全部保证可用**:摄入、建模、赛季/锦标赛模拟、场所发现、导出、前端、in-play、结算台账,一项不缺。下面分 A/B 两组只是按赛制与时区排版,**无任何功能差异**。赛事之间真正的差异只来自赛制本身,由 §3.0 的"赛制形态 × 市场能力矩阵"统一表达。

**A 组 — 五大联赛 + 欧冠**:

| 联赛 | Kalshi GAME 系列 | 赛季系列(当日开放事件) | API-Football league_id | 实测样例 |
|---|---|---|---|---|
| 英超 EPL | `KXEPLGAME` ✅ | `KXPREMIERLEAGUE-27`(20 队冠军)、`KXEPLRELEGATION-27` ✅;`KXEPLTOP4`(暂 0 开放) | **39** | `KXEPLGAME-26AUG28CRYMCI-{CRY,MCI,TIE}` |
| 西甲 | `KXLALIGAGAME` ✅ | `KXLALIGA-27` ✅ | **140** | `KXLALIGAGAME-26AUG26RMARSO` |
| 意甲 | `KXSERIEAGAME` ✅ | `KXSERIEA-27` ✅ | **135** | `KXSERIEAGAME-26AUG28ACMVEN` |
| 德甲 | `KXBUNDESLIGAGAME` ✅ | `KXBUNDESLIGA-27` ✅(18 队) | **78** | `KXBUNDESLIGAGAME-26AUG28BMUVFB` |
| 法甲 | `KXLIGUE1GAME` ✅ | `KXLIGUE1-27` ✅ | **61** | `KXLIGUE1GAME-26AUG28LILPSG` |
| 欧冠 UCL | `KXUCLGAME` ✅(现为资格赛,标题带 "Regulation Time Moneyline") | `KXUCL-27` ✅(29 个市场);`KXUCLTOP8`/`KXUCLRO16`/`KXUCLRO8`/`KXUCLRO4`/`KXUCLFINALIST`/`KXUCLADVANCE`/`KXUEFACLTOPGOAL` | **2** | `KXUCLGAME-26AUG25ASKCEL-{ASK,CEL,TIE}` |

**B 组 — 欧联/欧协联/南美(同一标准,无任何降级)**:

| 赛事 | Kalshi | API-Football league_id | 备注 |
|---|---|---|---|
| 欧联 UEL | `KXUELGAME` ✅(资格赛开放中)、`KXUEL`(冠军系列存在,暂 0 开放) | **3** | 联赛阶段抽签后补 |
| 欧协联 UECL | `KXUECLGAME` ✅、`KXUECL` | **848** | 同上 |
| 解放者杯 | `KXCONMEBOLLIBGAME`(当日 0 开放——8 月轮次间歇,9 月 QF 恢复)、`KXCONMEBOLLIB`、`ADVANCE` | **13** | 两回合 tie |
| 南美杯 | `KXCONMEBOLSUDGAME`、`KXCONMEBOLSUD` | **11** | 两回合 tie |
| 巴甲 | `KXBRASILEIROGAME` ✅、`KXBRASILEIRO`、`RELEGATION`、`TOP`/`TOPX` | **71** | 年历赛季(1–12 月),现已过半程 |
| 阿甲 | `KXARGPREMDIVGAME`、`KXARGPREMDIV*` 全套 | **128** | Clausura 进行中 |

**扩展位 — registry 里留条目、`enabled=false`,首发不启用**(Kalshi 均有 GAME 系列):葡超 `KXLIGAPORTUGALGAME`(94)、荷甲 `KXEREDIVISIEGAME`(88)、土超 `KXSUPERLIGGAME`(203)、苏超 `KXSCOTTISHPREMGAME`(179)、比甲 `KXBELGIANPLGAME`(144)、英冠 `KXEFLCHAMPIONSHIPGAME`(40)、五大联赛国内杯(FA Cup 45 / Copa del Rey 143 / Coppa Italia 137 / DFB Pokal 81 / Coupe de France 66 + EFL Cup 48)、南美其余(乌拉圭/智利/秘鲁/厄瓜多尔/哥伦比亚/玻利维亚/委内瑞拉)。**启用即全标准**:代码是能力驱动的(§3.0),翻开 `enabled` 后新赛事自动获得与 12 项首发完全相同的全栈,不存在"半启用"状态。**不做**:MLS/Liga MX(北美,超出"欧洲+南美"范围)、女足、低级别。

### 1.3 Polymarket 覆盖(当日验证)

- **Polymarket Global(只读参考 + 历史价格回填)**:✅ 逐场市场存在,slug 语法 `epl-cry-mac-2026-08-28`(联赛前缀-主队3码-客队3码-日期),另有 `-halftime-result`/`-second-half-result`/`-first-to-score`/`-exact-score`/`-total-corners` 衍生事件;赛季市场 `epl-2027-champion-*`、`laliga-2027-champion-*`、`uefa-champions-league-2027-champion-*`。gamma API `tag_slug=epl|la-liga|champions-league` 可直接过滤。世界杯模块的 milestone 回填 / price_tick 管线可平移(slug regex 从 `fifwc-…` 换成按联赛前缀)。
- **Polymarket US(可执行场所)**:公开 gateway 不带鉴权探测受限(仅证实体育类存在);**待办 D1-3**:用现有 `polymarket_us` SDK 凭证跑 `search.query("Premier League"/"EPL"/"vs")` 确认逐场覆盖与 slug 语法(世界杯是 `fwc-{h3}-{a3}-{et_date}` + `atc-…` 3-way 前缀,俱乐部大概率是 `epl-…` + 同样的 `atc-`/`aadc-`/`tsc-` 市场前缀)。PMUS 没挂的联赛照常出信号,闸门标 `no_tradable_contract`(世界杯已有此机制,`upcoming.json` 里 `poly_us: null`)。

### 1.4 API-Football 数据源(当日实测,key 已在 `prediction_market/.env`,Pro 计划 7,500 req/天)

`GET /leagues?season=2026` 实测(2026 = 2026-27 欧洲赛季;南美为年历 2026):

| league_id | 赛事 | 2026 赛季窗口 | coverage(events/lineups/fixture-stats(xG)/player-stats/standings/topscorers/predictions/odds/injuries) |
|---|---|---|---|
| 39 | Premier League | 08-21 → 05-30 | ✅ 全满 |
| 140 | La Liga | 08-15 → 05-30 | ✅ 全满 |
| 135 | Serie A | 08-22 → 05-30 | ✅ 全满 |
| 78 | Bundesliga | 08-28 → 05-22 | ⚠️ 当前只有 standings/predictions/odds——**未开赛的预热期假象**(2024/2025 赛季全满,已对照验证);开赛日 D1-1 复查 |
| 61 | Ligue 1 | 08-21 → 05-29 | ✅ 全满 |
| 2 | UEFA Champions League | 现仅资格赛(07-07 → 08-26) | ✅(standings 待联赛阶段抽签后出现;36 队单表) |
| 3 / 848 | UEL / UECL | 资格赛中 | ✅ 基本满 |
| 13 / 11 | Libertadores / Sudamericana | 02-04 → 09-15 / 09-16 | ✅ 全满(淘汰赛阶段) |
| 71 | Brasileirão Série A | 01-28 → 12-02 | ✅ 全满(含 injuries) |
| 128 | Liga Profesional Argentina | 01-22 → 11-08 | ✅ 全满 |
| 40/45/48/66/81/88/94/137/143/179/203/144 | 英冠/杯赛/其余欧洲联赛 | — | 已验或待赛季载入(FA Cup、Copa del Rey、Coupe de France 的 2026 条目要 10-11 月才出现,属正常) |

相对世界杯的**数据增益**:俱乐部联赛 injuries 覆盖真实可用(世界杯是 False)、standings 是 API 直供的真实积分榜(世界杯要自己算小组积分)、每队赛季 34–38 轮(强度更新的样本量完全不同)。

---

## 2. 世界杯模块解剖(已逐文件读毕)与处置表

模块统计:git 跟踪 167 文件(ops 52 / tests 27 / model 24 / strategy 17 / venues 15 / ingest 6 / jobs 3 / exec 3 / backtest 3 / util 3 / config 2 / docs 2 / analysis 3 + 根文件);Python 生产代码约 20,120 行。数据 1.9 GB(`wc.db` 595 MB + `raw/` 1.3 GB + logs)全部 gitignored。**复制策略 = 整目录全量过来(含数据与缓存,APFS clone 磁盘增量≈0)**;副本数据的 git 保护 = 复制过来的模块 `.gitignore` + Phase 0 步骤 4 的双层补丁(**注意**:`kalshi_docs/`、`research/`、`docs/REMOTE_DATA_ACCESS.md` 三项在世界杯侧只靠根 .gitignore 的 `prediction_market/` 前缀规则,对副本无效,必须补,已实测);soccer 运行**新建 `soccer.db`**,`wc.db` 副本闲置为只读参考(战术常数重估时还有用),清理需用户批准。

### 2.1 贯穿性风险点(六个,复制后必须最先改,否则静默出错)

| # | 风险 | 位置(源模块行号) | soccer 处置 |
|---|---|---|---|
| C1 | **`is_knockout` = "round 名不含 'group' 即淘汰赛"**。联赛轮次叫 `"Regular Season - 3"`、UCL 联赛阶段叫 `"League Stage - 3"`——全部会被误判为淘汰赛:错误套用 `knockout_lambda_scale=0.85`、强制中立场、跳过平局校准、给联赛比赛算"晋级概率" | `model/match_pricing.py:103-106`、`strategy/inplay_arb.py:101`、`strategy/inplay_arb_advance.py:119,337`、`model/motivation.py:29`、`tournament.py:141,238,297` 的 SQL `round LIKE '%roup%'` | 改为**从 league registry 查询**:单点函数 `stage_of(comp, round_name)` 返回赛制形态,再由 `caps(stage, comp)` 给出该场比赛的市场能力(有无 advance / 两回合 / ET / 点球,完整矩阵见 §3.0);全部调用点(含前端 JSON 的 `caps` 字段)只认这一个真值,不再有任何字符串猜测 |
| C2 | **主场优势语义反转**。世界杯:中立场,`home_adv` 只给东道主(`strength.py:68-109` `host_ids` 分支 + `host_rating_boost`);俱乐部:**每场都有真实主场** | `model/strength.py:37-40,68-109,180`、`squad_strength.py:146-152`、`inplay_advance.py:173` / `inplay_arb_advance.py:290` / `smart_exit_advance.py:60` 三处 `host_neutral=True` 硬编码 | `pair_lambdas(home_id, away_id)` 默认套 per-league `home_adv`(bootstrap 时按联赛用近两季结果拟合,EPL/西甲 ≈ 0.20–0.25 log-λ);`host_ids` 机制整体移除;`host_neutral` 仅保留给"决赛中立场"与杯赛特例 |
| C3 | **48 队先验文件是全模块的实体闸门**。`data/priors/ext_sim_v0.json`(12 组×4 队+FIFA rank+exp_points)被 `prior_ingest.load_prior()` 加载后,venue 发现层(`discovery.py:44`)、in-play 置信分层(`inplay_confidence.py:30-37` 读 FIFA rank)、fc_ingest(按**国籍**过滤球员)全都拿它当"合法实体全集" | `ingest/prior_ingest.py` 全文件 | 重写为 `club_prior.py`:按联赛生成 `data/priors/clubs_<league>_<season>.json` + 跨联赛合并 `clubs_all.json`(见 §3.2);类与核心方法保持、`group`→`league` 字段映射,下游少改 |
| C4 | **锦标赛模拟 → 联赛赛季模拟**。`tournament.py`(529 行)从 `_GROUP_FIXTURES` 4 队单循环到 Annex-C 第三名分槽、32 强签表全部是 2026 赛制;`knockout_bracket.py`(157 行)整文件是 FIFA 73–104 场次编号 | `model/tournament.py`、`model/knockout_bracket.py` | 新写 `model/league_season.py`(替代品,见 §3.3)+ `model/ucl_phase.py`(瑞士制,§3.4);`knockout_bracket.py` 随整体复制过来但[闲置]不接线 |
| C5 | **两回合 tie 无模型**。`dixon_coles.knockout_advance_prob`(单场 90'+ET+点球)与 `inplay_advance.py`(单场语义,`ET_FRACTION`、无首回合比分输入)都只支持一回合定胜负;UCL 淘汰赛/欧联/解放者杯全是两回合合计(无客场进球规则) | `model/dixon_coles.py:77-100`、`model/inplay_advance.py` | 新增 `two_leg_advance_prob(lam_h1,lam_a1,lam_h2,lam_a2, agg_h=0, agg_a=0)`(第二回合条件于首回合比分;合计平后按赛事规则 ET→点球或直接点球,见 §3.0);in-play advance 加 `agg_from_leg1` 输入。**单一标准下这是 phase 2 必做件**(不是缓期项):UEL/UECL 资格赛两回合 tie 正在进行、解放者杯 QF 9 月开打,C5 落地即有实战验证场 |
| C6 | **FC26 按国籍轴 → 按俱乐部轴**。`fc_ingest.py:119-124` 用 `nationality` 映射到 48 国家队;俱乐部模块要按 `teamName/club` 列映射 | `ingest/fc_ingest.py` | 翻转匹配轴,canonical_team_id 指向俱乐部;注意同名俱乐部消歧(按联赛限定) |

### 2.2 逐目录处置表

标记:**[原样]** 复制后零修改(除 import 改名);**[参数]** 复制后小改(常数/数据源/接线);**[重写]** 复制骨架、核心逻辑重写;**[新写]** 世界杯没有的新文件;**[闲置]** 随整体复制一并过来,但**不接线、不进 soccer 生产路径**(留在原位,清理需用户批准——整体复制原则下没有"不复制"这回事)。

**`config/`**
| 文件 | 处置 | 要点 |
|---|---|---|
| `config.py` | [参数] | `Paths` 锚定新目录(`__file__` 相对,自动成立);`DB = soccer.db`;`frontend_data` 指 `public/data/soccer/`;`SoccerConfig.league_id=1` 单值 → 删除,改由 registry 供给;`ModelConfig` 增 per-league 覆盖(`home_adv` 按联赛、`knockout_lambda_scale` 仅杯赛);`_apply_selected_params` 保留(param_selected.json 换命名空间) |
| `leagues.py` | [新写] | **全模块唯一的大新增配置**——League Registry,见 §3.1 |

**`ingest/`**
| 文件 | 处置 | 要点 |
|---|---|---|
| `store.py` | [参数] | schema 天然带 league_id,基本原样;`DB_PATH → soccer.db`;`nt_recent` 表语义改注释为"俱乐部近期比赛(含杯赛)";新增 `club_registry` 表与 `tie`(两回合)视图;`_VENUE_SEED` 原样 |
| `api_football.py` | [原样] | 客户端本就 league 参数化;预算守卫原样(7,500/天共享同一个 key——见 §6.4 预算表) |
| `soccer_ingest.py` | [参数] | 模块级 `LEAGUE/SEASON` 常量 → 改为循环 registry 活跃联赛;`sync_live` 改用 **`/fixtures?live=all` 一次调用全球在播再本地按 league_id 过滤**(省额度的关键);`sync_squads` 按俱乐部——**行级陷阱(本次核验发现)**:它的队列表来自 `WHERE national=1`(L749),俱乐部行 national=0 会选出零队,改从 club_registry 取;`_team_stub_rows` 硬编码 `"national": 1`(L71)→ 置 0;`sync_nt_recent` → `sync_club_recent`(每队 last=8,含所有赛事,球队全在评级域内——世界杯"友谊赛对手无评级"的缺陷自动消失) |
| `prior_ingest.py` | [重写] | → `club_prior.py`,接口签名保持(§3.2);`TEAM_ALIASES` → 分联赛俱乐部别名表(§3.6) |
| `fc_ingest.py` | [参数] | C6 轴翻转;**已实测**:它读的 `ea_fc26_players.csv`(16,228 行)原生带 `team` + `leagueName` + `playStyles` 列,匹配轴从 `nationality` 换 `team` 即可;pen_taker/attack_rank 逻辑原样;联赛覆盖与缺口见 §3.8 |

**`model/`**(agent 审计结论,已核对)
| 文件 | 处置 | 要点 |
|---|---|---|
| `dixon_coles.py` | [原样]+ | 纯内核零依赖;**追加** `two_leg_advance_prob`(C5) |
| `calibrate.py` / `probability_calibration.py` | [原样] | 平局加权跳过淘汰赛的逻辑对任何赛制都正确 |
| `strength.py` | [重写核心] | C2 主场反转;`_rank_to_rating`(FIFA rank)→ 俱乐部先验分(Elo/上季积分);拟合目标 3 场小组 exp_points → **赛季期望积分**(整表拟合,不再需要 rank_anchor_weight 补丁);`update_with_results` / `update_strength_from_store` [原样](本就通用,且俱乐部 38 轮让它成为主角) |
| `match_pricing.py` | [参数] | C1 修复点(canonical `stage_of`);`price_group_stage` → `price_upcoming_fixtures(league)`(直接从 fixture 表取未来 N 轮,不再从 prior.draw() 生成对阵) |
| `tournament.py` | [重写] | → `league_season.py`(§3.3):保留其向量化骨架、`future_opp` 机制、`eliminated_teams` 的"数学淘汰"思想(变为夺冠/欧冠区/降级的数学锁定) |
| `knockout_bracket.py` | [闲置] | 不接线(FIFA 73-104 场次编号无俱乐部类比);UCL 用 `ucl_phase.py` 的抽签采样器替代(§3.4);其 `_perfect_matching` 约束抽签机制可直接 import 借用(文件就在手边,又一个整体复制的好处) |
| `golden_boot.py` | [参数,phase 2] | → `top_scorer.py`:去掉"晋级深度耦合"(联赛人人 38 场),`future_opp`(对手防守强度加权)升为主模型;`_wc_to_date_by_team` → 联赛进球;KXUEFACLTOPGOAL/联赛射手王市场对接 |
| `penalties.py` | [参数] | 数学通用;`SHOOTOUT_REPUTATION` 国家队字典 → 置空(靠 rating gap),联赛路径 dead code,杯赛用 |
| `inplay.py` | [原样] | 已确认逐场通用(λ 由外部传入);常数是 WC 标定,**先沿用 + 上线后按联赛重标定**(骨架有 `state_scaling` 等开关) |
| `inplay_advance.py` | [参数,phase 2/5] | C5:加 `agg` 首回合输入;`host_neutral=True` → 该回合真实主队;ET/点球规则按 registry `et_in_ties`(§3.0:欧战 ET→点球,南美两杯直接点球);单场杯赛沿用现逻辑 |
| `inplay_corners.py` | [参数] | `CORNER_TOTAL_PRIOR = 9.5` → per-league(EPL ≈ 10.5);Kalshi 俱乐部角球系列齐全(`KXEPLCORNERS` 等),这块世界杯没上成(没挂牌),soccer 可以真上 |
| `motivation.py` | [重写,默认关] | 小组赛心理学无联赛类比;骨架保留、信号换为:欧战赛前轮换(UCL 赛前 3 天联赛)、保级生死战、争冠末段、垫底摆烂;**权重 0 上线,数据攒够再开**(遵循世界杯"动机只走实时路径不进闸门"的纪律) |
| `ensemble.py` | [参数] | 扰动轴换:`home_adv (0.15,0.30)`、去掉 `penalty_favorite_edge/knockout_lambda_scale`(联赛 no-op);输出 σ 字段名 `p_title_sigma/p_top4_sigma/p_releg_sigma` |
| `squad_strength.py` | [参数] | **`_LEAGUE_STRENGTH` 表(39:1.00,140:0.97,…)本来就是俱乐部联赛表,直接成为跨联赛归一的基石**;`squad` 表间接层删除(player_stat.team_api_id 就是俱乐部);`build_strength_live` 组合根保留(去 host_rating_boost);裸 `except: pass` 逐个换成带日志 |
| `club_aggregation.py` | [参数] | 名字骗人——它是"国家队吃俱乐部数据"的聚合;去掉 `COALESCE(squad→国家队)` 间接层后即为俱乐部版;与 squad_strength 合并为一处(消重) |
| `form_strength.py` | [参数] | 数据源 `nt_recent` → `club_recent`;`FRIENDLY_DISCOUNT` → 杯赛/欧战权重;`DECAY_XI 0.008`(87 天半衰)→ ~0.03(俱乐部 3-4 天一场) |
| `xg_form.py` | [原样] | 已通用已走查验证;`DECAY_XI 0.012` → ~0.03 同理 |
| `fc_strength.py` | [参数] | FC26 canonical_team_id 指俱乐部后即用;`_TOP_N 16 → 18`(轮换) |
| `altdata_adjust.py` | [参数] | `nt_recent` → `club_recent`;`_RECENT_N 6`(国际赛 2 年)→ 8-10(俱乐部 5 周);xGA 分支原样 |
| `venue_climate.py` | [参数,权重 0] | 16 座北美球场表 → 空表 + 钩子保留(冬季天气/海拔——南美有真海拔:La Paz 3,640 m,玻利维亚入 registry 时启用) |
| `oos_eval.py` | [参数] | `price_match(knockout=False)` 硬编码 → `stage_of`;先验加载换 club_prior |
| `run_model.py` | [参数] | 编排链保留;`worldcup_model.json` → `soccer_model.json`(**按联赛分组的** champion/table/top_scorer payload);淘汰赛 confirmed-reach 覆盖层 → 联赛"数学锁定"覆盖层(提前夺冠/降级即 100%/0%) |

**`strategy/`**
| 文件 | 处置 | 要点 |
|---|---|---|
| `devig.py` `edge.py` `sizing.py` `risk.py` `cross_venue.py` `decision_model.py` `smart_exit.py` `inplay_hedge.py` `inplay_hedge_advance.py` | [原样] | 纯交易数学,零赛制耦合(agent 逐行确认);`smart_exit.py:69` 的 `is_knockout` 调用点随 C1 一起换;`decision_model` 的 `conviction_side` 入参在 motivation 关权重时自然静默 |
| `inplay_tactics.py` | [参数] | 17 条战术里 16 条逐场通用;**`knockout_late_draw` 反号**(联赛晚段平局是活市场不是终点,docstring 自己写明白了方向);8 条挖掘常数(n=26 WC 样本)沿用启动 + 台账记录"待俱乐部样本重估" |
| `inplay_tactics_advance.py` / `inplay_arb_advance.py` / `smart_exit_advance.py` | [参数,phase 5] | 随 C5;`_is_knockout` 闸门(哪些比赛有 advance 市场)→ `caps.advance`(§3.0)——纯联赛场次这一路整条不激活,零成本 |
| `inplay_arb.py` | [参数] | 两处 `_is_knockout` 随 C1;其余原样;`poly_us_corners` 源从未注册的既有小坑顺手补上(世界杯遗留) |
| `inplay_confidence.py` | [参数] | FIFA rank 分层(`ext_sim_v0.json`)→ 俱乐部 Elo 分层(club_prior 提供同名 rank 字段,阈值重标:top-10 → Elo 前 10%) |
| `xv_monitor.py` | [参数] | `KXMENWORLDCUP-26` → registry 每联赛 champion 系列;"48-way exclusive" devig → 20-way(EPL)/18-way(德甲/法甲)/36-way(UCL);`world-cup` 搜索词 → 联赛词表 |

**`venues/`**(发现层 = 改造面最集中且最小的地方,agent 已逐行定位)
| 文件 | 处置 | 要点 |
|---|---|---|
| `base.py` `guard.py` `ratelimit.py` `kalshi/auth.py` `kalshi/market_data.py` `kalshi/orders.py` | [原样] | 全部 venue 通用;$1 硬上限、双开关硬默认 false 原样保留 |
| `kalshi/discovery.py` | [参数] | 6 个系列常量(L25-30)→ **registry 驱动的 per-league 系列映射**;`"reg time:"` 前缀剥离逻辑保留(UCL 资格赛实测同款标题);`-TIE` 后缀判平原样;实体闸门 `load_prior()` → club registry;**新增**:从 event_ticker 解析 `26AUG28CRYMCI` 日期+双方 3 码,建立 `kalshi_code ↔ club_id` 映射持久化进 club_registry 表 |
| `champion_prices.py` | [参数] | `KXWCROUND-26*` 事件映射 → per-league:{champion: `KXPREMIERLEAGUE-27`, top4: `KXEPLTOP4-27`, relegation: `KXEPLRELEGATION-27`, ucl_reach: `KXUCLRO16/RO8/RO4/FINALIST`};`_real_price`/结算感知逻辑逐字保留(是精华) |
| `polymarket_us/discovery.py` | [参数] | slug 语法 `fwc-` → per-league 前缀(D1-3 验证后定);6 个搜索词 → 联赛词表;`atc-`/`aadc-`/`tsc-` 市场前缀待验证后沿用 |
| `polymarket_global/reader.py` | [参数] | `fifwc-([a-z]{2,4})-([a-z]{2,4})-(\d{4}-…)` → `(epl|laliga|seriea|bundesliga|ligue1|ucl)-…`(gamma 实测 slug `epl-cry-mac-2026-08-28`);`tag_id 100350`(soccer)沿用;reach-round slugs → 赛季市场 slugs;`prices_history`/`parse_clob_book` 原样 |

**`exec/`**:`order_translation.py` [原样];`executor.py` [参数](champion 信号族换 registry 系列;match 信号路径本就中性)。

**`ops/`**(52 文件全部随整体复制;~36 个接线改造,16 个 `_*.py` 研究脚本 + `online_microfootball.sh` [闲置])
| 文件 | 处置 | 要点 |
|---|---|---|
| `refresh_all.py` | [参数] | 步骤序保留;摄入循环 registry;导出全部换 soccer 命名空间 |
| `refresh_and_deploy.sh` | [参数] | 路径/日志/`npm run sync:soccer`;**新增 flock 单实例锁**(吸取 macro 模块 refresh 双跑事故教训,世界杯版没有锁是已知弱点,新模块修上) |
| `live_refresh.py/.sh` | [参数] | `_in_match_window` 原样(纯 DB);`_write_both` 指 `public/data/soccer/`;里程碑捕获原样;champion 水位 → per-league 水位;**新增 flock** |
| `match_trigger.py` | [参数] | 窗口判据原样;水位文件 `.trigger_watermark` 在新 output 目录天然隔离 |
| `upcoming_export.py` | [参数] | **加 `league` + `stage` + `caps` 字段,按联赛分组输出**(前端层级与显隐的后端半边,§3.0);advance 块只在 `caps.advance` 场次出现(替代"knockout-only");`_tentative_pairing`("Winner Group A")→ UCL 版("Winner of tie X");motivation 块随权重 0 静默 |
| `inplay_export.py` / `inplay_export_advance.py` | [参数] | 同上加 league/stage/caps 维度;advance 版只扫 `caps.advance` 场次(§3.0) |
| `milestone_export.py` / `backfill_milestones.py` / `backfill_price_ticks*.py` | [参数] | `_ALIASES` 国家队名 → 俱乐部别名表;`_NEXT_ROUND` 映射 → per-competition;Poly slug regex 换;七里程碑结构原样 |
| `knockout_export.py` | [重写,phase 2] | → `ucl_bracket_export.py`(瑞士制表 + 淘汰赛路径);五大联赛无此物 |
| `squad_export.py` / `form_export.py` | [参数] | 按联赛分组;"48 teams" 措辞换 |
| `schedule_export.py` | [参数] | 加 league 维度;`group_only` → per-league 轮次 |
| `reach_round_export.py` | [重写] | → `season_odds_export.py`:每联赛 {冠军 / 前四 / 降级} × {model_pct, kalshi_c, poly_c, edge};UCL 加 {top8, r16, …} 阶梯(即世界杯 reach-round 的直接镜像) |
| `team_styles_export.py` | [参数] | 48 队手工先验字典(L52-101)→ **FC26 playStyles 自动生成 642 俱乐部先验(§3.8-d,已实测标签可用)** + 豪门人工覆盖;0.55/0.45 混合、10 风格码、周更节流、`_with_legacy` 全部原样 |
| `performance_report.py` / `settle_bets.py` | [参数] | 冻结台账、五赛道口径、`match_pick` 单一真值全保留;PDF 标题换;`_advancer` 的 2-way 判据仅杯赛用 |
| `risk_report.py` / `calibrate_fit.py` / `monitor.py` / `backtest_export.py` / `walkforward_eval.py` / `decision_backtest.py` | [参数] | 机制原样;`reg_score` 90' 口径通用;校准闸门 per-league(见 §3.5) |
| `param_sweep.py` | [参数,后开] | 网格轴换(home_adv 进网格,rank_anchor 出);**样本量态度反转**:世界杯 104 场逐日重扫,联赛头几周样本少 → 前 6 周不跑 sweep,沿用 bootstrap 默认 |
| `frontend_export.py` / `system_overview.py` | [重写文案] | 静态目录全 WC 中文文案,重写为 soccer 版 |
| `pdf_style.py` | [原样] | |
| `schedule.py` | [参数] | ET/PT 双时区 → ET/**CET** 双时区(欧洲比赛) |
| 3 × `.plist` | [参数] | 新 label:`com.someopark.soccerlive`(**60-90s 自适应**,世界杯定频 30s——12 项赛事额度调速,见 §6.1)/ `com.someopark.soccertrigger`(900s)/ `com.someopark.soccerrefresh`(**07:30**,与世界杯 06:30 错峰、也与凌晨 MRPT/MTFS pipeline 错峰);日志指 soccer 目录 |
| `online_microfootball.sh` + 16 个 `_*.py` | [闲置] | DFM/研究一次性,不接线不进管线;`_validate_signals.py` 等研究脚本留在手边,R5 战术常数重估时正好复用其方法学 |
| `jobs/hourly_job.py` `live_poller.py` | [参数] | `_LIVE` 状态元组原样;`_live_quote_sources` 的 per-league 发现实例化;live_poller 循环退避原样 |

**`backtest/` `util/`**:全 [原样](`replay.py` 的 `price_match(knockout=False)` 随 C1 换 `stage_of`)。

**`tests/`(27 文件 261 项)**:[参数] 复制后分三类——(a) 纯数学测试(devig/hedge/pricing/sizing/¢换算/exec 上限)原样跑通;(b) 赛制测试(`test_fifa_tiebreak`、`test_knockout_settlement`、`test_prior` 恒等式)替换为联赛版(EPL/西甲/意甲/德甲 tie-break 各一、UCL top8/进 KO 判定、两回合合计结算、club_prior 校验);(c) 数据层测试改 fixture round 名(`"Regular Season - 3"`)。**门槛:soccer 测试数 ≥ 世界杯的 261 减去纯 WC 赛制项,新增联赛项后总数不低于 240。**

**根文件**:`README.md` [重写](沿用世界杯 README 结构);`PLAN_AUDIT.md` [闲置];`.env` [原样](随整体复制过来,同一批 key 直接可用;`KALSHI_ENV=demo`、双交易开关 false 原样);`.gitignore` [参数](**保留** `wc.db` 三行——副本里真有这个文件,必须继续被 ignore;**新增** `soccer.db` 三件套 + `data/kalshi_docs/` + `research/` + `docs/REMOTE_DATA_ACCESS.md` + 两个归档目录——后三项世界杯侧只靠**根** .gitignore 的 `prediction_market/` 前缀规则保护,副本不受覆盖,git check-ignore 实测,见 Phase 0 步骤 4);`requirements.txt` [原样];`docs/` 两篇 in-play 研究 [原样](战术常数的出处文档);`research/` `analysis/` [闲置]。

### 2.3 国家队级数据资产 → 俱乐部级对应物(逐项核验,2026-08-25 对着代码与磁盘数据实测)

> 用户指令:凡是世界杯里存在的"国家队级别数据",俱乐部级**都要有**。逐项过,每项给可行性判定与数据源;FC26 游戏数据的提取建模详见 §3.8。

| # | 国家队级数据资产 | 世界杯来源(代码/表) | 俱乐部级对应物 | 核验判定 |
|---|---|---|---|---|
| 1 | 球队风格 styles(10 风格码,手工 48 队先验 + 实况指标混合) | `team_styles_export.py` PRIOR(L52-101)+ `fixture_stats` 箱线指标 | **FC26 `playStyles` 标签按队聚合自动生成 ~642 俱乐部先验**(§3.8-d)+ 同一套 `fixture_stats` 实况混合(API-Football 对俱乐部提供完全相同的 possession/passes/shots/xG 字段) | ✅ 可行且**升级**(世界杯先验纯手工,俱乐部版可自动生成+人工覆盖豪门) |
| 2 | 球队强度先验(FIFA rank + exp_points 12 组模拟) | `ext_sim_v0.json` + `prior_ingest.py` | 上季积分榜(API standings season=2025,实测可取)+ ClubElo + Kalshi 冠军盘反解(§3.2 三锚) | ✅ 可行 |
| 3 | 近期战绩 form(nt_recent,友谊赛折扣) | `form_strength.py` + `sync_nt_recent` | `club_recent`(俱乐部全部近赛含杯赛/欧战,竞赛权重替代友谊赛折扣);API `fixtures?team=X&last=8` 同款端点 | ✅ 可行且样本多 10 倍 |
| 4 | 大名单 squad(国家队 25-26 人) | `sync_squads`(`players/squads?team=`) | 同一端点按俱乐部 team_id 调用(API-Football squads 本就是俱乐部端点) | ✅ 同端点零改造 |
| 5 | 阵容强度(球员俱乐部赛季数据回灌国家队) | `squad_strength.py` + `club_aggregation.py`(squad 表间接层) | 间接层删除:`player_stat.team_api_id` 本来就是俱乐部,直接聚合;`_LEAGUE_STRENGTH` 表(L50-73)原生就是俱乐部联赛表,升为跨联赛归一主角 | ✅ 反而更简单 |
| 6 | FC26 天赋锚(按国籍过滤 9,853 人) | `fc_ingest.py`(nationality 轴) | 按 `team` 轴重摄入(§3.8-a);fc_strength top-N、golden-boot goal_rate、pen_taker 全部机制原样 | ✅ 实测列在,见 §3.8(巴甲有缺口有兜底) |
| 7 | FIFA 排名(强弱分层,inplay_confidence 用) | `ext_sim_v0.json` fifa_rank | ClubElo 分位(club_prior 提供同名 rank 字段,阈值改百分位) | ✅ 可行 |
| 8 | 点球声誉(10 国家队字典) | `penalties.py` SHOOTOUT_REPUTATION(L26-30) | 置空走 rating gap(数学通用);后续可从历史 shootout 数据攒俱乐部字典(非阻塞) | ✅ 降级可接受(联赛路径本是 dead code) |
| 9 | 射手种子(seed_players.json WC 热门) | `golden_boot.load_seed_players` | per-league 种子 = FC26 俱乐部射手 goal_rate top 池 + API `players/topscorers`(上季);两源都实测可用 | ✅ 可行 |
| 10 | h2h 历史 | `sync_h2h`(国家队对阵稀疏) | 同端点,俱乐部 h2h 数据远厚(联赛年年打) | ✅ 同端点,质量更好 |
| 11 | 伤停 injuries | WC coverage=False(几乎空) | 12 项赛事 coverage 实测=True(§1.4) | ✅ 世界杯没有的,俱乐部反而有 |
| 12 | 球场/气候(16 北美球场手工表) | `venue_climate.py` _VENUES | API-Football `teams` 端点自带每俱乐部主场 venue(名称/城市/容量);海拔手工表只为南美高原(La Paz 等)保留;权重 0 上线不变 | ✅ 可行(权重 0,无阻塞) |
| 13 | 小组出线心理(motivation) | `motivation.py`(3 场小组赛专属) | 联赛动机(保级生死战/争冠末段/欧战前轮换),骨架同形,权重 0 上线攒数据 | ✅ 设计已在 §2.2,无数据阻塞 |
| 14 | 国家名/国旗(countries.ts + BR_FLAG emoji) | 前端硬编码 | clubs.ts(~250 队 zh)+ API-Football `team.logo` URL(DB `team.logo` 列已存,实测每队都有) | ✅ 可行 |

**核验结论:14/14 全部有俱乐部级对应物,无一缺失;其中 #1/#3/#10/#11 俱乐部数据比国家队更厚。唯一真实缺口是 FC26 无巴甲联赛授权(§3.8-e 有兜底),不构成阻塞。**

---

## 3. 核心新设计(仅这些是"新写",其余全是复制改参)

### 3.0 赛制形态 × 市场能力矩阵(总纲——"哪些比赛只有 3-way、哪些有淘汰赛"的唯一真值)

世界杯只有一条"小组赛/淘汰赛"二分线;soccer 的 12 项赛事必须显式建模为**四种比赛形态(stage)**,每场比赛归入且只归入一种,由 `stage_of(comp, round_name)` 判定、`caps(stage, comp)` 给出能力。**后端逻辑与前端渲染都只看 caps,不看赛事名、不做字符串猜测**:

| 能力 \ stage | `league`(联赛轮/欧战联赛阶段) | `cup_two_leg_l1`(两回合首回合) | `cup_two_leg_l2`(两回合次回合) | `cup_single`(单场淘汰) |
|---|---|---|---|---|
| 3-way 90′ 比赛市场(胜/平/负) | ✅ 唯一市场 | ✅ | ✅ | ✅ |
| 平局=终局结果 | ✅(拿 1 分,市场结算 Tie) | ✅(比赛市场结算 Tie;**tie 未完**) | ✅(比赛市场结算 Tie;晋级另算) | ✅(90′ 市场结算 Tie;晋级打加时/点球) |
| advance/晋级 2-way 市场 | ❌ **无此物** | ✅(tie 级,含次回合展望) | ✅(tie 级,当日决出) | ✅(=本场决出) |
| ET/点球建模 | ❌ | ❌(首回合永不加时) | 按 `et_in_ties`:欧战 ET→点球;解放者/南美杯直接点球 | 按赛事:欧战决赛 ET→点球;阿甲季后赛直接点球(D1-7 核验) |
| 两回合合计状态(`agg`) | — | 产生(带入次回合) | 消费(C5 模型输入) | — |
| 中立场 | ❌(真实主客) | ❌ | ❌ | 仅决赛(按 fixture venue 判定) |
| `knockout_late_draw` 战术方向 | 联赛版(反号:晚段平局是活市场) | 比赛市场=联赛版;advance 市场=KO 版 | KO 版 | KO 版 |
| 联赛动机信号(保级/争冠/轮换) | ✅ | ❌ | ❌ | ❌ |

**12 项首发赛事 → 形态构成**(哪个赛事含哪些 stage,这决定每个赛事的前后端"要不要淘汰赛那一路"):

| 赛事 | league | cup_two_leg | cup_single | 说明 |
|---|---|---|---|---|
| 英超/西甲/意甲/德甲/法甲 | ✅ 全部 38/34 轮 | ❌ | ❌ | **纯 3-way 赛事**:advance 一路的前端模块与后端逻辑整条不出现 |
| 巴甲 | ✅ 全部 38 轮 | ❌ | ❌ | 同上(Kalshi 虽存在 `KXBRASILEIROADVANCE` 系列,联赛轮无此物,registry 不接) |
| 阿甲(`league_playoffs`) | ✅ 分区常规轮 | ❌ | ✅ 季后赛(R16→决赛,平局直接点球) | 常规轮=纯 3-way;进入季后赛轮 advance 一路自动亮起(细节 D1-7 核验) |
| UCL/UEL/UECL | ✅ 联赛阶段 8 轮(瑞士制,**单场 3-way,无 advance**) | ✅ 资格赛(现在进行中)+ KO(play-off/R16/QF/SF) | ✅ 决赛(中立场,ET→点球) | 三种形态都有,按轮次切换 |
| 解放者杯/南美杯 | ❌(小组赛已结束,2026 剩余全是 KO) | ✅ QF/SF(合计平**直接点球**,无 ET) | ✅ 决赛(中立场,ET→点球) | 当前为纯淘汰赛态 |

**接线原则(后端)**:advance 全家桶(`inplay_export_advance`、`*_advance` 四件套、`price_tick_adv` 回填、Kalshi `*ADVANCE` 系列查询)只对 `caps.advance == true` 的 fixture 运行——这就是 C1 的正确修法,替代一切 `"group" not in round` 子串判断。两回合 tie 由 `tie_key`(两场 fixture 配对 + `agg` 合计)在 store 层落库;`cup_two_leg_l2` 的定价与 in-play 必须带 `agg` 输入(C5)。

**接线原则(前端)**:每条比赛记录(upcoming/inplay/schedule JSON)由后端算好并携带 `stage` + `caps: {advance, two_leg, leg, agg, neutral, et_rule}`,前端**永不自行推断**:
- `AdvanceMode` 切换(Regulation/Advances)只在当前视图存在 ≥1 场 `caps.advance` 比赛时渲染——五大联赛+巴甲用户**永远看不到**这个开关;
- `MatchCard` 的 advance 区块、两回合 `agg` 徽章(如"首回合 2-1")按 caps 出现;
- 网格卡片按所选赛事的形态显隐:纯联赛 → 显示积分榜/冠军/前四/降级卡,**隐藏**晋级阶梯与签表卡;UCL/UEL/UECL → 显示瑞士表 + `top8/r16/…` 阶梯 + KO 签表卡,**隐藏**降级卡;解放者/南美杯 → 晋级阶梯 + 签表,无积分榜/降级;
- season_odds 档位由 kind 决定:league → {冠军/前四/降级};swiss_ucl → {top8/r16/qf/sf/final/冠军};cup → {冠军 + 各 tie advance}。

### 3.1 League Registry(`config/leagues.py`)——全模块唯一新增的中枢

```python
@dataclass(frozen=True)
class Competition:
    key: str                  # "epl" — 全模块统一联赛 id(JSON/DB/前端都用它)
    name: str; zh: str
    api_football_id: int      # 39
    season: int               # 2026(欧洲 2026-27;南美年历 2026)
    kind: str                 # "league" | "league_playoffs"(阿甲) | "swiss_ucl" | "cup_two_leg"
    enabled: bool             # 12 项首发全 true;扩展位 false。启用即全标准,无分级
    et_in_ties: bool          # 两回合合计平后是否先打加时:欧战 True;解放者/南美杯 False(直接点球)
    n_teams: int              # 20/18/36
    tiebreak: str             # "pts_gd_gf"(EPL/法甲) | "pts_h2h_gd"(西甲/意甲) | "pts_gd_gf_h2h"(德甲)
    home_adv: float           # bootstrap 拟合后固化,per-league
    kalshi: dict              # {"game": "KXEPLGAME", "champion": "KXPREMIERLEAGUE",
                              #  "top4": "KXEPLTOP4", "relegation": "KXEPLRELEGATION",
                              #  "total": "KXEPLTOTAL", "btts": "KXEPLBTTS", "spread": "KXEPLSPREAD",
                              #  "score": "KXEPLSCORE", "corners": "KXEPLCORNERS", ...}
    poly_slug_prefix: str     # "epl"(Global);PMUS 前缀 D1-3 验证后补
    season_year_suffix: str   # "-27"(Kalshi 赛季事件后缀,巴甲/阿甲为 "-26")
    stage_rules: dict         # round_name 正则 → stage("Regular Season.*"→league;
                              #   "League Stage.*"→league(瑞士制单场);".*1st Leg"→cup_two_leg_l1;
                              #   ".*2nd Leg"→cup_two_leg_l2;"Final"→cup_single(neutral);
                              #   阿甲 "1st Phase.*"→league、季后赛轮→cup_single)

REGISTRY: dict[str, Competition] = {...}   # §1.2 的 12 项首发(enabled=true)+ 扩展位(enabled=false)
def stage_of(comp_key, round_name) -> Stage    # C1 的唯一真值
def caps(stage, comp) -> StageCaps             # §3.0 能力矩阵的查询入口
def active() -> list[Competition]              # 全部 enabled 项,单一标准不分级
```

设计原则:**加一个联赛 = 加一条 registry 记录 + 一份俱乐部别名 JSON**,其余零代码。

### 3.2 俱乐部先验(`ingest/club_prior.py`,替代 prior_ingest)

镜像 `PriorSnapshot` 接口——**精确说**:类名与核心方法(`load_prior()`/`team_id()`/`canonical_team_name()`/`by_id`/`ranks()`)保持,但字段映射 `group`→`league`、`draw()`→`league_table()`(这两个的调用点全在本就要重写的文件里,不构成额外工作);另出一份跨联赛合并的 `clubs_all.json`(Elo 分位版 rank),`config.prior_ext_sim_v0` 指针指它——`inplay_confidence._rank_by_name` 是**直读该 config 路径**的(L30-37 实测),换指针即接通。内容换为:

- **实体表**:每联赛俱乐部 {club_id(canonical snake_case)、name、zh、api_football_team_id、kalshi_code(从 event ticker 实测解析)、poly_code、logo}。
- **强度锚(替代 FIFA rank + exp_points)**,三源融合,权重进 config:
  1. **上季终表积分/名次**(API-Football `standings?season=2025`,零额外成本;升班马用次级联赛折算 −8~−12 分);
  2. **ClubElo**(clubelo.com 免费 CSV API,`api.clubelo.com/{club}`——独立、跨联赛可比、日更;bootstrap 拉一次固化,per-league z-score);
  3. **市场隐含**(Kalshi 冠军系列 devig 后反解 rating——世界杯 `strength.py` 的 exp_points 反拟合器直接复用,拟合目标从"3 场小组期望积分"换成"38 轮赛季期望积分")。市场锚只做先验定位,**披露在 MODEL_NOTES**(对着市场交易时用市场当先验的自指问题,与世界杯 `rank_anchor_weight` 的处理纪律一致)。
- **恒等式校验**(镜像世界杯 ±2pp 纪律):每联赛 Σp_champion = 1、Σp_relegation = 3(德甲 2.5,第 16 名附加赛半权)、每队 p_top4 ≥ p_champion;UCL Σp_top8 = 8。
- **iron rule 原样继承**:先验是 stale starting line,结果覆盖它、时间衰减它;俱乐部版结果多 10 倍,`update_strength_from_store` 从配角变主角。

### 3.3 联赛赛季模拟(`model/league_season.py`,替代 tournament.py)

保留 tournament.py 的向量化骨架 + `future_opp` 机制,结构换为:

- 输入:当前真实积分榜(API standings,含 played/pts/GD/GF)+ **剩余赛程表**(真实 fixture 列表,每场带主客)+ per-match Dixon-Coles 概率(带 per-league home_adv)。
- N 次模拟剩余赛季(默认 N=200k,发布 run 500k;38 轮 × 20 队远重于 6 场小组赛,向量化按"每轮批量采样比分矩阵"实现)→ 终表 → per-league tie-break(registry `tiebreak`;**v1 妥协:西甲/意甲 H2H 用 GD 近似,写进 MODEL_NOTES 披露**——H2H 需要模拟内记录对战矩阵,phase 3 补真);→ 输出每队:`p_champion / p_top4 / p_top5 / p_relegation / p_bottom / e_points / rank 分布 / e_final_rank`。
- **数学锁定覆盖层**(镜像世界杯 elimination/confirmed-reach 双覆盖层):夺冠数学锁定→100% 并把他队归零重归一;数学降级同理;积分扣分(admin)预留字段。
- 映射市场:champion→`KXPREMIERLEAGUE-27`;top4→`KXEPLTOP4`;relegation→`KXEPLRELEGATION`;last→`KXEPLLAST`;point margin/team points 后续。

### 3.4 UCL 瑞士制(`model/ucl_phase.py`,phase 2)

- 联赛阶段:36 队单表、每队 8 场固定对手(**抽签 8/27-28 出,fixtures 由 API-Football 直供,不生成**)→ 表模拟(league_season 的 8 轮特例)→ `p_top8 / p_9_24(播降 KO play-off) / p_25_36(出局)`。
- 淘汰赛:排名种子化签位(1-8 对 9-24 胜者的官方路径)+ 约束抽签采样器(借 knockout_bracket 的 `_perfect_matching` 机制)→ 两回合 tie(C5 的 `two_leg_advance_prob`)→ `p_r16/qf/sf/final/champion`,恰好映射 `KXUCLRO16/RO8/RO4/FINALIST/KXUCL`(**世界杯 reach-round 阶梯的完整镜像,前端 ReachRound 组件近乎原样复用**)。
- 资格赛期间(现在):资格赛两回合 tie 用 C5 模型定价 `KXUCLGAME` + `KXUCLADVANCE`——**C5 是 phase 2 必做件,落地即接**(UEL/UECL/UCL 资格赛是两回合模型的第一个实战验证场,不必等 KO 阶段)。

### 3.5 per-league 校准与闸门

世界杯是单赛事单闸门;soccer 每联赛独立:`calibration.json` → `{league: {method, param, brier, trade_grade, n}}`。**冷启动规则**:联赛 n<30 场结算前 trade_grade 恒 false(只出研究信号不出交易信号);全局校准(池化五大联赛)作为 n<30 的过渡参考。平局率基线按联赛(法甲/意甲 ~26-28%,英超 ~23%),`draw_extra_theta` per-league 化。

### 3.6 俱乐部别名与实体对齐(镜像世界杯 TEAM_ALIASES 纪律)

- 规模:世界杯 48 队 4 种拼法 → soccer 首发 12 项赛事 **~250 俱乐部**(五大 96 + 欧战三档去重后 ~80 长尾 + 南美 ~75;欧联/欧协联贡献最多长尾小俱乐部)× 4 场所(API-Football / Kalshi 名 + 3 码 / Poly Global 3 码 / PMUS)。实测差异样例:Kalshi "Nottingham" vs API-Football "Nottingham Forest";"Bodoe/Glimt" vs "Bodø/Glimt";Kalshi 3 码 `LFC`(利物浦)/`ASK`(LASK)。
- 方法:bootstrap 时半自动——按联赛拉 Kalshi 开放 events,`difflib` 模糊匹配 API-Football 队名生成候选表 → **人工过目一次 → 固化进 `data/priors/aliases_<league>.json`** → 实时路径只走精确别名(世界杯铁律:错配会下错单;模糊匹配只允许历史回填用)。ticker 3 码从 event_ticker 解析后存 club_registry 表,变成第二把精确钥匙(比名字匹配更稳)。

### 3.7 前端"联赛→比赛"层级(铁律 5 的唯一结构适配)

**挂载方式完全照抄 macro 模式先例**(前端盘点确认 macro 是现成的平行镜像模板,比再抄 prediction 更干净):

- 新 appMode `'soccer'`:枚举扩展触 4 个共享文件(`App.tsx` L121-124 / `Sidebar.tsx`(新按钮)/ `ChatArea.tsx` L247 / `server/routes/chat.ts` L93)+ `RightPanel.tsx` 加 `soccer_` 前缀分支 + **`index.css` 加 `[data-mode="soccer"]` 主题块**(复制 L640-792 的 prediction 反色块——data-mode 绑定的是字符串,新模式必须有自己的块,前端盘点已指出)——**对共享代码文件的全部修改就这 6 处,均为加法,不动 stock/prediction/macro 现有行为**;另有共享资源文件的加法:`package.json` 两个 script、五份 i18n locale 加 `soccer.*` 命名空间。
- 新目录 `src/components/soccer/`:`SoccerArtifact.tsx`(复制 PredictionArtifact 的 REGISTRY 结构)、`SoccerArtifactGrid.tsx`、`SoccerUpcoming.tsx`、`ClubName.tsx`(CountryName 镜像)、`soccerApi.ts`(独立 fetcher 模块,不扩 lib/api.ts——macro 先例)。artifact 前缀 `soccer_*`。
- **层级实现**:所有 JSON 顶层加 `leagues: [{league: "epl", name, zh, matches|teams: [...]}, ...]`;每个 artifact 组件顶部一条**联赛选择 chips**(состояние进 artifact params,默认记住上次选择;`upcoming` 卡片按联赛分组渲染,LIVE 优先置顶跨联赛混排)。世界杯"champion 表"→"每联赛冠军赔率表 + 联赛切换";"BracketView"→ 仅 UCL 显示(瑞士表 + KO 树);"reach_round"→ season_odds(冠军/前四/降级三档,UCL 为 top8/r16/... 阶梯)。
- **能力驱动显隐(§3.0 的前端半边)**:所有组件只读后端算好的 `stage`/`caps` 字段决定渲染——AdvanceMode 开关、MatchCard advance 区块、两回合 agg 徽章、签表卡 vs 降级卡的显隐全按 §3.0 矩阵;前端零赛制判断逻辑,**不写任何 `if (league === 'ucl')` 式特判**,这样扩展位新赛事启用时前端自动正确。
- 队名/旗帜:`countries.ts` → `clubs.ts`(~250 队 zh 名;欧战长尾小俱乐部可先英文名后补 zh);旗帜 emoji → API-Football `team.logo` URL(DB 已存),`BR_FLAG`/`BR_META`(球场表)/`R32_TREE` 三个硬编码不复制。
- i18n:新 `soccer.*` 命名空间(五语言);`prediction.*` 键结构照搬改文案,реason/hedge/cap 等动态串大部分逐字复用(赛制无关)。
- 服务端:`artifactDetector.ts` 加 `soccer_*` 触发词表;`agentPrompt/prompt/predictionMarketTool` 的 soccer 版工具(读 `public/data/soccer/`)。
- **JSON 命名空间 = `public/data/soccer/` 子目录**(文件名与世界杯同名不冲突:`soccer/upcoming.json`、`soccer/inplay_live.json`、`soccer/soccer_model.json`…);`scripts/sync_soccer_data.mjs` 新文件(白名单式,照抄 sync_prediction_data.mjs,**并负责 `mkdir -p public/data/soccer`**);soccer config 的 `Paths.ensure()` 把 `frontend_data` 一并 mkdir(世界杯版 ensure 不建前端目录、run_model 靠 exists() 跳过、live_refresh `_write_both` 目录缺失的行为未定义——soccer 版一行补丁根治);`package.json` 加 `sync:soccer` / `build:soccer` 两个脚本(加法)。
- **每个 artifact 的逐一改造对照见附录 C**(23 个 registry 类型 + 4 个非网格表面 + 后端孤儿输出,一节不落,"无修改"也明写)。

### 3.8 FC26 俱乐部数据提取建模(用户指令:必须参考 FIFA 游戏;2026-08-25 磁盘实测)

数据已在盘上(`data/raw/fc26/`,随整体复制过来):`ea_fc26_players.csv` **16,228 行**(fc_ingest 实际读取的合并文件)= outfield 14,412 + GK 1,816;共 **642 家俱乐部 / 45 个联赛**。关键列**实测存在**:`team`(俱乐部)、`leagueName`(联赛)、`overallRating`、`finishing/positioning/shotPower/penalties/longShots/volleys`、`pac/sho/pas/dri/def/phy` 全属性、**`playStyles`/`playStylesPlus`**(EA 球风标签,如 Technical / Long Ball Pass / Press Proven / Finesse Shot / Aerial Fortress / Relentless)。

**联赛覆盖实测 vs 我们的 12 项**:

| 我们的赛事 | FC26 对应 leagueName | 覆盖 |
|---|---|---|
| 英超/西甲/意甲/德甲/法甲 | `Premier League` 20c / `LALIGA EA SPORTS` 20c / `Serie A Enilive` 20c / `Bundesliga` 18c / `Ligue 1 McDonald's` 18c | ✅ 全满 |
| 阿甲 | **`LPF` 30 俱乐部 781 人** | ✅ 全满 |
| 解放者杯 / 南美杯 | **`Libertadores` 19c / `Sudamericana` 19c**(EA 把两杯作为独立"联赛"收录,巴西豪门在此) | ✅ 参赛队基本覆盖 |
| 巴甲 | **无联赛授权**(仅洲际参赛队经上行两条覆盖) | ⚠️ 部分,见 (e) 兜底 |
| UCL/UEL/UECL 参赛队 | 经各自国内联赛覆盖:Liga Portugal 18c、Eredivisie 18c、Trendyol Süper Lig 18c、1A Pro League 16c、Scottish Prem 12c、Ekstraklasa、Allsvenskan、Eliteserien、SUPERLIGA(丹)、Brack Super League(瑞士)、Ö. Bundesliga(奥)等 | ✅ 主体覆盖;东欧长尾小俱乐部部分缺失,走 (e) |

**五条提取管线**(全部落在既有文件的 C6 轴翻转上,不新造轮子):

- **(a) `fc_ingest.py` 俱乐部化**:匹配轴 `nationality` → `team`(+`leagueName` 限定消同名歧义);`canonical_team_id` 指向俱乐部 club_id(经别名表);去重键 `(club, lastName)` 保最高 overall、每队 pen_taker(penalties 最高的攻击型球员)、`team_attack_rank`(队内 goal_rate 排名)逻辑**原样**。俱乐部名 ↔ API-Football 名的对齐并入 §3.6 别名表流程(FC26 是第五个拼法源)。
- **(b) `fc_strength.py`**:每俱乐部 top-N `overallRating` 均值 z-score 入评级,`_TOP_N` 16→18(轮换深度);跨联赛 z-score 配合 `_LEAGUE_STRENGTH` 归一。GK 行天然纳入(合并文件含 gk 属性列)。
- **(c) `top_scorer` 种子与射手率**:`fc_goal_rate(finishing, positioning, shot_power, overall, position_type)` 公式**原样**(它本来就是位置+属性函数,与国家队无关);种子池 = 每俱乐部 goal_rate top 名单 ∪ API-Football 上季 `players/topscorers`;贝叶斯更新用本赛季联赛进球(§2.2 golden_boot 行)。
- **(d) 俱乐部风格先验自动生成(styles artifact 的核心升级)**:替代世界杯 48 队手工 PRIOR——把每俱乐部球员的 `playStyles` 标签计数 + 数值属性聚合,映射到既有 10 风格码:possession ← Technical/First Touch/Incisive Pass/Press Proven + 高 pas;direct ← Long Ball Pass/Whipped Pass/Rapid/Quick Step + 高 pac;high_press ← Relentless/Intercept/Anticipate/Jockey + aggression;low_block ← Block/Slide Tackle/Bruiser;clinical ← Finesse Shot/Low Driven Shot/Chip Shot + finishing;high_volume ← Power Shot + longShots/volleys;set_piece ← Dead Ball/Precision Header/Aerial Fortress + freeKickAccuracy;dominant_attack ← 攻击线 overall/sho 聚合;balanced/contained ← 低离散度/残差。产出与手工 PRIOR **同形**的 `{club_id: [(style, weight)], …}`,豪门可人工覆盖;0.55/0.45 先验-实况混合、`_SECOND_STYLE_FRAC`、周更节流、`_with_legacy` 适配器**全部原样**(§附录 C-9)。映射表是启动先验,俱乐部 38 轮实况数据会快速修正它(世界杯只有 3 场,先验独大;俱乐部版实况权重实质更高)。
- **(e) 缺失兜底纪律**:z-score 只在**有 FC26 数据的俱乐部**上计算,缺失俱乐部填中性 0(不伪造);巴甲整体与东欧长尾:`fc_blend_weight` 对该 competition 置 0,天赋 proxy 改用 API-Football `player_stat.rating`(赛季评分,12 项赛事 coverage 全满已实测);styles 先验缺失 → 落到 live-only + `balanced` 缺省(世界杯对无先验队的既有行为,L201)。

---

## 4. 隔离与安全边界(铁律 3 的可验证清单)

| 资源 | 世界杯 | soccer | 冲突检查 |
|---|---|---|---|
| 代码目录 | `prediction_market/` | `prediction_market_soccer/`(平级,互不 import,与 `prediction_market_macro/` 同款隔离) | `grep -r "from prediction_market\."` 在新模块必须 0 命中(只允许 `prediction_market_soccer.`) |
| DB | `data/wc.db` | `data/soccer.db` | 路径写死在各自 config |
| API-Football key | 同一把(7,500/天共享) | 同一把 | 预算守卫各自记账在各自 DB → **soccer 的 `daily_budget` 设 6,500 留 1,000 余量**(世界杯赛后日用 ~2,基本无争用;两模块各自守卫独立,尖峰调速见 §6.1) |
| 前端 JSON | `public/data/*.json` | `public/data/soccer/*.json` | 文件级零交集 |
| launchd | `com.someopark.prediction{live,matchtrigger,refresh}`(仍在跑,**绝不 unload/改**) | `com.someopark.soccer{live,trigger,refresh}` | label 全新;**安装动作需用户明示批准**(cp / launchctl bootstrap 拆步,一次一个) |
| 日志 | `prediction_market/data/logs/` | `prediction_market_soccer/data/logs/` | |
| 水位/锁 | `.trigger_watermark` `.champion_watermark`(无锁) | 同名文件在新 output 目录 + **新增 flock 锁**(macro 事故教训) | |
| Firebase | 同一个 hosting(整站 dist 一起部署,静态共存,无互踩语义) | 同上 | deploy 是全站原子发布,soccer 的 deploy 会带上世界杯现有静态文件的原样拷贝——安全 |
| git | 公开仓库 | `.gitignore` 随整体复制继承 + Phase 0 步骤 4 双层补丁(模块层补 soccer.db/kalshi_docs/research/REMOTE_DATA_ACCESS/归档目录;根层加 `prediction_market_soccer/` 伞形镜像块)——**根 .gitignore 的既有规则全按 `prediction_market/` 前缀写死,对副本一概无效(实测)** | Phase 0 步骤 4 的 `git status` 过目;commit 前跑 git-push 技能的密钥扫描 |
| 交易开关 | 硬默认 false + $1 上限 | **逐字继承**(`KALSHI_TRADING_ENABLED`/`PMUS_TRADING_ENABLED` false、`max_test_order_usd=1.0`、demo 优先、prod key 只在明示指令下动用) | |

---

## 5. 分阶段实施(每阶段带验收门,G-style)

> 依赖顺序遵循 agent 审计的 DAG:`strength(C2) → build_strength_live → match_pricing(C1) → league_season(C4) → 发现层 → 导出/前端 → ops → in-play`。

**Phase 0 — 整体复制(半天)**
1. **整目录全量复制(用户强指令)**:`cp -Rc /Users/xuling/code/someopark-test/prediction_market/. /Users/xuling/code/someopark-test/prediction_market_soccer/`(APFS clone,3.1 GB 磁盘增量≈0)——把源模块**全部内容**(代码+数据+raw+logs+`__pycache__` 等一个不落)**合并**进本目录。目标目录已存在且已含 `TRANSFORM_PLAN.md`(源里无同名文件,合并复制不会碰它);**绝不**先清空目标、**绝不** `rsync --delete`。
2. 复制对账 + DB 完整性:两侧 `find -not -path "*/.git/*" | wc -l` 差值 = 1(即本 plan 文件);`TRANSFORM_PLAN.md` 仍在;源目录 `git status` 零改动;`diff -rq` 抽查三个子目录;**`wc.db` 副本跑 `PRAGMA integrity_check`**——世界杯 `predictionlive` 每 30s 打开该库(WAL),在线 cp 可能撕裂;若不干净,用 `sqlite3 源库 ".backup 副本路径"` 重拷这一个文件(只读参考用途,但要干净)。
3. **陈旧产物归档(拆三个静默炸弹,均已对代码实证)**:`data/output/` 整目录改名为 `data/output_wc_archive/`(**归档不删除**,整体复制原则)并重建空 `data/output/`;理由:① 副本里的 `param_selected.json` 会被 `config._apply_selected_params()`(config.py L376-404)**自动加载**,soccer 会静默采用 WC 调参;② `.trigger_watermark` 记着 WC 的 104 场结算数,`match_trigger` 的 `settled > prev` 判据会**压死 soccer 触发器**直到第 105 场;③ `calibration.json`/`oos_report.json` 会把 WC 校准冒充 soccer 的闸门状态(executor/risk_report/backtest 读它们)。`data/logs/` 同样改名 `logs_wc_archive/` 并重建空目录(6,700+ 旧文件,防 IO 堆积与日志混淆)。**`data/priors/` 保留原位**(G0 的 261 项测试还需要 ext_sim_v0.json;club_prior 上线后它转为闲置)。
4. **gitignore 补丁(公开仓库红线,缺口已实测)**:模块 `.gitignore` 加 `soccer.db` 三行 + `data/output_wc_archive/` + `data/logs_wc_archive/` + `data/kalshi_docs/` + `research/` + `docs/REMOTE_DATA_ACCESS.md`——后三项在世界杯侧**只被根 `.gitignore` 按 `prediction_market/` 路径前缀忽略**(git check-ignore 实测),副本不受任何规则保护;根 `.gitignore` 再加一个 `prediction_market_soccer/` 伞形镜像块(照抄其 L109-126 的 belt-and-suspenders 模式)。然后 `git status --porcelain prediction_market_soccer/` 过目零意外文件。**本阶段不做任何 `git add`**。
5. 机械改名(实测计数,sed 分六类跑,逐类 diff 抽查;**范围排除 `TRANSFORM_PLAN.md`**——它引用源模块路径是故意的):Python 内 `prediction_market` → `prediction_market_soccer`(151 文件 912 处;全量复制后 `research/`/`analysis/`/`_*.py` 也在扫描范围,一并改名无害);sh/plist 路径与 `python -m` 调用(6 文件 25 处);`wc.db` → `soccer.db`(26 处,只改代码引用——`data/wc.db` 副本文件本身留在原地闲置);launchd label 3 处;`sync:wc` → `sync:soccer`(refresh_and_deploy.sh 2 处——**该串不含 "prediction_market",类 1 抓不到,必须单列**);完整清单见附录 A。
6. **门 G0(升级版)**:`conda run -n someopark_run python -c "import prediction_market_soccer.config"` 通过且 `DB_PATH` 指向 `soccer.db`;**完整 261 项测试全绿**——改名后代码+priors 数据与世界杯同构,理应逐项通过(priors 保留原位就是为这一步;**已 grep 实证 tests/ 零引用 `data/output`/`wc.db`/`raw`,归档不影响**),任何红 = 改名弄坏了东西,比"只跑数学类"强得多;跑法带 `PM_DISABLE_PARAM_OVERRIDE=1`(config 自带的固定默认开关)排除"归档掉 param_selected 后配置漂移"这个变量;**pytest 只在 soccer 目录跑,绝不在世界杯目录跑**(哪怕只产生 .pytest_cache 也算改动源模块);残留检查三种形态全部 0 命中:`from prediction_market\.` / `^import prediction_market\b` / sh 里 `-m prediction_market\.`;`TRANSFORM_PLAN.md` 原样;世界杯模块 `git status` 无任何改动;`wc.db` 副本 mtime 不再变化——**基线取步骤 2 完整性检查/重拷之后**(sqlite 首开会做 WAL 恢复动一次 mtime,属预期;此后再变即异常)。

**Phase 1 — Registry + 数据层(1-2 天)**
7. 写 `config/leagues.py`(12 项首发全字段 + `stage_rules`/`caps` 能力矩阵,§3.0;扩展位 enabled=false;别名表 bootstrap 脚本);`store.py` 加 club_registry 表;`soccer_ingest` 多联赛循环 + `live=all` 改造;`club_prior.py`(三锚融合);`fc_ingest` C6 翻转;`sync_club_recent`。
8. 首次摄入:12 项赛事 teams/fixtures/standings(~36 req)+ 上季 standings(12 req)+ ClubElo bootstrap + Kalshi 各系列 events 拉取 → 别名表半自动生成 + 人工过目固化。
9. **门 G1**:12 项赛事 fixtures/teams/standings 落库行数对账(EPL 380 场/20 队等);club_prior 恒等式全过;**每场 fixture 的 `stage_of` 判定全覆盖**(零 unknown stage——阿甲/欧战全部轮名命中 registry 规则,这是 §3.0 落地的第一道验收);别名表覆盖率 = 100%(每个 Kalshi 开放市场的队都能映射);API 用量 < 400 req。

**Phase 2 — 模型层(2-3 天)**
10. `strength.py` C2 重写(per-league home_adv 拟合脚本跑近两季数据固化进 registry)+ 赛季期望积分反拟合;`match_pricing` C1 + `price_upcoming_fixtures`;`league_season.py`(§3.3);`run_model.py` 出 per-league `soccer_model.json`;`ensemble` 换轴;blend 族(form/xg/fc/altdata/squad)数据源切换;**`dixon_coles.two_leg_advance_prob`(C5)+ `tie_key`/`agg` 落库(§3.0)——用进行中的 UEL/UECL 资格赛做实战验证**。
11. **门 G2**:恒等式 16 组式回归(记忆里的方法学复用):Σp=1、锁定覆盖层、monotone(p_top4≥p_champion)、home/away λ 对称性抽查;对照检验——模型 3-way 与 API-Football odds devig 中位绝对差 < 8pp(联赛市场高效,模型不应离谱);EPL 冠军榜 top4 与 Kalshi 顺位一致性肉眼过目。
12. UCL:抽签落地后(8/27-28)接 `ucl_phase.py` league-stage 部分(KO 签表采样器顺延——**C5 两回合模型本 phase 已随步骤 10 落地并在资格赛验证**,KO 采样只在 2027 年 KO 抽签前需要)。

**Phase 3 — 发现层 + 导出 + 前端 v1(2-3 天)**
13. `kalshi/discovery.py` registry 化 + ticker 3 码持久化;`champion_prices` 赛季系列;`polymarket_global/reader` slug 正则;PMUS SDK 验证(D1-3)后接;`upcoming_export`/`schedule_export`/`season_odds_export`/`xv_monitor` 带 league 维度;`sync_soccer_data.mjs`。
14. 前端:appMode 'soccer' 共享文件六处加法(§3.7)+ `src/components/soccer/` 全套 + i18n 五语言 + `clubs.ts`;本地 `npm run dev` 验证后走一次完整 `sync:soccer && build && firebase deploy`。
15. **门 G3**:`soccer/upcoming.json` 里每场三源价(model/kalshi/poly_global)非空率 > 90%(PMUS 视 D1-3);前端 12 项赛事切换、比赛卡、冠军榜、赛季 odds 全渲染,**caps 显隐逐赛事过一遍**(纯联赛视图无 advance 开关/无签表卡,UCL 视图无降级卡,两回合场次带 agg 徽章——§3.0 矩阵当验收单用)+ **附录 C 逐 artifact 过一遍**(23 个类型 + 4 表面各自的"前端/后端改动"逐条勾验,"无修改"项验证确实未被改动);世界杯页面回归无恙(截图对比);i18n 五语言无缺 key(lint 脚本)。

**Phase 4 — ops 全套 + 台账(1-2 天)**
16. `refresh_all` 串通(摄入→校准→导出→PDF→frontend_export);三个 sh + plist(带 flock);`match_trigger`/`live_refresh` 窗口验证;`performance_report`/`settle_bets` 冻结台账 per-league;`risk_report`;monitor。**进程复制的逐项程序按 §6.0-B 七步执行,写入面对照 §6.0-C。**
17. plist 安装:**征得用户批准后**按 §6.0-B 步骤 3-4 的顺序(refresh→trigger→live)逐个 cp + bootstrap(或交用户跑);观察 48h 日志(§6.0-B 步骤 5-6)。
18. **门 G4**:模拟一个比赛日全链路(周五德甲揭幕 8/28 或周末英超):trigger 检出新结算→全管线→deploy 成功;`soccer/performance_report.json` 出现首批冻结 pick;两模块 launchd 互不干扰(`launchctl list` + 日志时间戳核对);API 日用量 < 2,000。

**Phase 5 — in-play(12 项全赛事,单一标准)+ 里程碑 + smart_exit(2-3 天,首个满负荷周末前完成)**
19. `inplay_export` + 17 战术(`knockout_late_draw` 按 §3.0 双向:比赛市场用联赛版反号、advance 市场用 KO 版);**advance in-play 栈同批上线**(caps 驱动,只对 `caps.advance` 场次激活——UEL/UECL 资格赛与解放者杯 QF 即是首批实战场,纯联赛场次零开销);里程碑捕获与 Poly 回填;smart_exit;`inplay_confidence` Elo 分层;live 60-90s 自适应节奏 + 每 poll 额度调速器(§6.1 尖峰算式,超限自动降档——**新增的小改造,世界杯无此需求**)。
20. **门 G5**:一个真实比赛窗口(建议先拿单场德甲/法甲晚场试,再上周六 15:00 UK 尖峰)`soccer/inplay_live.json` 逐分钟更新、里程碑 T15…T75 落库、复盘 jsonl 生成;尖峰时段 API 用量实测 < 预算模型 ±20%。

**Phase 6 — 校准闸门放行 + 观察期(持续)**
21. 各联赛攒 30 场结算 → `calibrate_fit` per-league → trade_grade 逐联赛放行;`decision_backtest`/`walkforward_eval` 复跑;param_sweep 6 周后启用。
22. UCL 联赛阶段开赛(9 月中)接满 `ucl_phase` + `KXUCLTOP8` 等阶梯(两回合 C5 与 advance 栈此时已在资格赛/解放者杯上跑过实战,KO 阶段只是轮次切换,零新代码)。
23. **门 G6**:paper 模式跑满两周;五赛道口径报表(argmax/decision/hold/realized/inplay)对世界杯格式逐列镜像;此后才讨论任何实盘议题(需另行明示批准,非本计划范围)。

Phase 2 之后各阶段可与前端并行;总量级 **~10-14 个工作日**的改造(不含观察期)。

---

## 6. 关键运行参数与预算

### 6.0 定时与实时进程:全清单与逐进程复制手册(2026-08-25 实测盘点)

> 用户问题"世界杯那些每分钟/每几秒/每天/每小时运行的 refresh 和实时进程怎么复制、有没有写入"的集中回答。**实测结论:世界杯全部周期性任务只有 3 个 launchd agent;crontab 里零条目(已查);hourly_job 从未安装(docstring 里的 cron 示例只是示例);另有两个与前端共用的常驻服务不属于本模块。**
>
> **世界杯三件套现状 = 赛后空转(2026-08-25 日志实证)**:live 每 30s 打一行 `no match window — skip`;trigger 每 15 分钟 `SKIP: outside match window`;daily 06:30 仍每天完整跑一遍(快速)并 deploy。**用户指令:它们已不承担实际功能,本节的目标不是"与它们共存",而是"保证 soccer 版所需的每一个进程都能跑起来"**——所以本节按"可运行性"组织:B0 前置条件 → B 复制与安装 → C 写入面 → D 在跑判据。世界杯三件套原样不动(铁律 3)。

**A. 全景表(七类,一个不漏)**:

| # | 进程/节律 | 节奏 | 宿主 | 干什么 | soccer 处置 |
|---|---|---|---|---|---|
| P1 | `com.someopark.predictionlive` | **30s**(plist 注释写 per-minute 与实际值不符——世界杯的既有笔误,soccer 版注释改一致) | launchd | `live_refresh.sh` → `ops.live_refresh` 单次:窗口外 1 次 DB 查询秒退;窗口内 live 摄入→inplay/upcoming/milestone/xv/oos 全套导出双写 | **复制**,间隔改 **60s**(§6.1;自适应调速在进程内部,plist 定频) |
| P2 | `com.someopark.predictionmatchtrigger` | 900s | launchd | `refresh_and_deploy.sh --trigger`:水位闸门,有新结算才跑全管线 | **复制**,900s 不变 |
| P3 | `com.someopark.predictionrefresh` | 每日 06:30 | launchd | `refresh_and_deploy.sh` 全管线(摄入→导出→build→deploy→param_sweep) | **复制**,改 **07:30 ET**(收口前一日全部完赛:欧洲赛事 ET 下午踢完、南美至多午夜;亦在 MRPT/MTFS 09:10 之前) |
| P4 | crontab | — | cron | **实测零条目**,世界杯不用 cron | 无事可做(soccer 也不用 cron) |
| P5 | `jobs/hourly_job.py` / `jobs/live_poller.py` | 设计为每小时/逐分钟 | **从未安装**(crontab 空、launchd 无) | 早期编排工具,被 P1-P3 取代;**注意 `live_poller._live_quote_sources` 被 `inplay_export` 当库函数 import——接线的是函数不是进程** | [闲置] 同样不安装;库函数随 import 正常工作 |
| P6 | Express `server/index.ts`(:3001)+ `cloudflared tunnel` | 常驻 | 前端仓库(**模块外**,实测在跑) | 静态服务 `public/data` 过隧道 → 站点分钟级更新不用重 deploy | **共用零改动**:`express.static` 递归覆盖 `public/data/soccer/` 子目录,零新进程;若隧道/server 掉线,退化为 Firebase 部署快照(功能不丢只是不再分钟级) |
| P7 | 隐性内部节律(不是独立进程,是管线内时间闸门) | 见右 | 各 py 内部 | team_styles 周更(7d mtime)、risk_report ≥600s、champion 水位(逐结算)、周日 `--with-form`、param_sweep 仅日更+sleep 60、摄入 TTL 层(fixtures 6h / results 1h / standings 1h / static 7d / h2h 14d / lineups 600s / live 30s)、前端轮询(Upcoming 60s/20s、inplay 30s) | 全部随代码复制自动继承;唯一改动:champion 水位 per-league、周日 form → `sync_club_recent` 周节奏 |

**B0. soccer 进程可运行的前置条件清单(装 plist 前逐项打勾——任何一项缺失都是"装了也不跑"的静默故障)**:

1. **conda 可达**:launchd 的最小 PATH 下 `conda run -n someopark_run` 必须能跑——两个 sh 里的 `export PATH="/opt/homebrew/bin:/Users/xuling/miniforge3/bin:…"` 随整体复制继承(世界杯同款此刻在用,实证有效),sed 后勿动这一行。
2. **密钥双 source**:sh 里 `source $REPO/.env` + `source $REPO/prediction_market_soccer/.env`(sed 类 1 自动改对;`.env` 随整体复制已在);`FIREBASE_TOKEN` 必须在其一(refresh 无人值守 deploy 的硬前提)。
3. **目录存在**:`data/{output,logs,priors,raw}`(Phase 0 步骤 3 归档后已重建)+ `public/data/soccer/`(`Paths.ensure()` 补丁,§3.7)。
4. **DB 自建**:任一进程首跑会 `store.init_db()` 自建 `soccer.db`,无需手工。
5. **flock 锁文件**:两个 sh 的锁放 `data/` 下(不放 /tmp),路径可写。
6. **前端工具链**:`node_modules` 齐(必要时 `npm ci`)、`firebase` CLI 在 PATH、`sync:soccer`/`build` 脚本在 package.json(Phase 3 已加)。
7. **plist 语法**:`plutil -lint` 三个全过;label/路径/间隔逐字段对照 §6.0-A。
8. **手动预演(关键解耦步)**:上 launchd 之前,三条入口各手跑一次——`bash ops/live_refresh.sh`(窗口外应秒退)、`bash ops/refresh_and_deploy.sh --trigger`(应 SKIP 秒退)、`bash ops/refresh_and_deploy.sh`(完整跑到 deploy 成功)。**手动全绿再装 launchd**,把"进程能不能跑"和"launchd 配置对不对"两类故障分开排查。

**B. 逐进程复制与安装程序(P1-P3 通用七步,一次一个)**:

1. **源**:plist 随 Phase 0 整体复制已在 `prediction_market_soccer/ops/`;sed 类 1/3 已把路径与 label 改好。逐文件核对目标态:label `com.someopark.soccer{live,trigger,refresh}`、ProgramArguments 指 `prediction_market_soccer/ops/*.sh`、日志指 `prediction_market_soccer/data/logs/`、`StartInterval` 60/900、`StartCalendarInterval` 07:30、RunAtLoad(live=true,其余 false 照抄)。
2. **脚本自检**:两个 .sh 的 `REPO` 路径、`source prediction_market_soccer/.env`、`python -m prediction_market_soccer.…`、`sync:soccer`、flock 锁(`/tmp` 之外,放模块 data/ 下 `.lock` 文件)全部就位;手动跑一次 `bash ops/live_refresh.sh` 验证窗口外秒退。
3. **安装(需用户明示批准,记忆铁律)**:一次一个,拆步:`cp ops/com.someopark.soccerrefresh.plist ~/Library/LaunchAgents/` → `plutil -lint` → `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.someopark.soccerrefresh.plist`。
4. **安装顺序**:soccerrefresh(每日,最低频)→ 观察一个整点 → soccertrigger → soccerlive(最高频最后装)。
5. **首验**:`launchctl list | grep soccer` 三条在册;tail 各自日志见首轮输出;`launchctl list | grep prediction` **世界杯三条原样未动**(数量、label、状态逐条对比)。
6. **持续验**:48h 日志无异常退出码;flock 生效(人为并发触发第二实例应立即退出);G4 门的时间戳交叉核对。
7. **回滚**:`launchctl bootout gui/$UID/com.someopark.soccerXXX` + 删 `~/Library/LaunchAgents/` 里对应 plist;模块内文件不动。

**C. 写入面矩阵("有没有写入"的直接回答——每个进程写哪里,全部落在 soccer 自己的地盘)**:

| 进程 | 写入目标 | 对世界杯目录写入 |
|---|---|---|
| soccerlive(60s) | `soccer.db`(live 摄入 + milestone_snapshot 表)、`data/output/*.json`、**`public/data/soccer/*.json`**(`_write_both` 11 个文件 + 按需 PDF)、`data/logs/inplay_review*.jsonl` | **零** |
| soccertrigger(900s) | 闸门态:读 DB + 1 次 `sync_results` 写 `soccer.db` + `.trigger_watermark`;触发后 = 下一行全套 | **零** |
| soccerrefresh(每日 07:30) | `soccer.db` 摄入、全部导出 JSON/PDF(双写 output + `public/data/soccer/`)、前端 `dist/`(npm build)、**Firebase hosting(外部写,整站原子发布)**、param_sweep JSON | **零** |
| (对照)世界杯三进程 | 继续写 `wc.db` + `public/data/` 根层 | 与 soccer 文件级零交集(§4) |

唯一共享的写入汇合点是 **Firebase deploy**(两模块都触发整站发布)——静态文件互不覆盖(根层 vs `soccer/` 子目录),发布本身原子,先后无所谓;真正的并发保护靠各自 flock(同模块内)+ firebase CLI 自身的发布序列化(跨模块,极端同时 deploy 时后者覆盖前者的**发布版本**但内容是并集快照,无损)。

**D. "在跑"判据表(装完后每个进程的可观测证据;G4/G5 验收直接引用本表)**:

| 进程 | 空闲态证据 | 工作态证据 |
|---|---|---|
| soccerlive(60s) | `data/logs/live.out.log` 每 60s 一行 "no match window — skip"(与世界杯空转同款行为,天然回归对照) | 比赛窗口内:`public/data/soccer/inplay_live.json` mtime 逐分钟滚动、`n_live > 0`、`inplay_review_<日期>.jsonl` 增行 |
| soccertrigger(900s) | `trigger.out.log` 每 15 分钟一行 SKIP | 比赛日有新结算后 ≤15 分钟:日志出现 `RUN:` → 全管线各步成功行 → deploy 成功 |
| soccerrefresh(每日 07:30 ET) | — | 每天 07:30 后新 `refresh_deploy_*.log`,末行 `done @ … someopark.web.app`;`soccer/soccer_model.json` 当日 mtime;param_sweep 段按期跳过/执行 |

首个真实检验点:**8/28(周五)德甲揭幕 + 周末英超**——soccerlive 应在 ET 上午-下午的欧洲比赛窗自动进入工作态,soccertrigger 在每场结算后 15 分钟内触发,07:30 daily 收口前一日全部结果。

### 6.1 API-Football 日预算模型(Pro 7,500/天,soccer 自限 6,500——**12 项赛事全部 in-play,单一标准**)

**每 poll 结构优化(三招,全赛事 in-play 的前提;世界杯单联赛无此需求)**:① `/fixtures?live=all` **一次调用**拿全球在播,本地按 league_id 过滤(不是每赛事一次);② `/odds/live` 不带参数**一次调用**拿全部在播赔率;③ `fixture_players`(仅 lone_threat 战术用)降频为每 3 poll 一次。每 poll 成本 ≈ 2 + C × 4/3(C = 并发在播场数;`fixtures/statistics` 每场每 poll 1 次)。

| 场景 | 算式(90s 档 = 40 poll/h) | 日用量 |
|---|---|---|
| 每日基线(refresh):fixtures+standings+topscorers × 12 赛事 + 结果批拉 + 新结算场 stats/lineups/players(3 req/场 × ~20 场)+ odds(30) | 36 + 4 + 60 + 30 | **~130** |
| 常规日(欧洲晚场 + 南美晚场,C≈3,共 ~5h) | (2+4)×40×5 | **~1,200** |
| **周六**(五大联赛日 ~22 场;15:00 UK 窗英超+德甲同踢峰值 C≈10,全日在播 ~10h 均值 C≈4) | 峰值 (2+13.3)×40×3 + 其余 (2+5.3)×40×7 | **~3,900** |
| **周四(最坏日)**:UEL 18 场/轮 + UECL 18 场/轮,双开球窗(18:45/21:00 CET),窗内并发 C 可达 ~18 | (2+24)×40×4 + 基线与零星 | **~4,400 ⚠️** |

⚠️ 结论:结构优化后 12 项全 in-play 装得下,但周四/周六到半预算。**内置三档额度调速器**(live_refresh 每 poll 查当日已用,对世界杯 `_check_budget` 的 ~30 行扩展):< 3,500 → 60s 全功能(低并发日自动提速);3,500–5,000 → 90s 标准档;> 5,000 → 120s 且停拉 `fixture_players`(17 战术里仅 lone_threat 降级,其余全保)。**额度极端时的取舍按流动性不按赛事**(单一标准原则):优先保"有 Kalshi 挂牌 + 有持仓"的场次,而不是"先保英超砍南美"。

### 6.2 Kalshi / Polymarket 读预算
Kalshi 公开行情无鉴权无严格额度(沿用 ratelimit.py 令牌桶);Poly Global 公开;PMUS 60 req/min 限速沿用。in-play 每 poll 的 venue 读:6 场 × 3 腿 × 2 场所 ≈ 36 次 orderbook——世界杯同量级,无新问题。

### 6.3 磁盘
raw 快照按世界杯比例外推:12 联赛全季 ~6-8 GB/年。`data/raw/` 保持 gitignore,按月归档压缩(新增 cron 内一行 tar,phase 4 顺手)。

### 6.4 与现有系统的资源共处
- **本机时区 = 美东 ET(`date` 实测;v5 初稿曾按"本地=中国时间"推算,已改正)**。07:30 ET 的 daily refresh 时点成立的真正理由:前一日**全部**比赛已完赛(欧洲赛事在 ET 上午-下午踢完,南美最晚 ~午夜 ET 收尾),且落在世界杯 06:30(空转)之后、MRPT/MTFS pipeline 09:10-09:40 ET 之前;VP 17:33、macro 四件套均无冲突。
- in-play 时段换算到本地(ET):欧洲晚场 20:00-23:00 CET = **14:00-17:00 ET**(工作日与美股盘中及 VP 16:20-17:00 窗口重叠——都是轻量进程,无碍);周六英超 15:00 UK = 10:00 ET(周末无美股);周四欧联双窗 = 12:45/15:00 ET;南美晚场 = 19:00-22:00 ET。60-120s 节奏的 Python 单进程负载可忽略;WF 变慢调查结论(文件系统 IO 是根因)提示:soccer 的 inplay_review jsonl 按日切文件(世界杯已是)+ 月度归档,避免 6,700+ 小文件堆积重演。

---

## 7. 风险与开放问题(写明,不藏)

| # | 风险/未知 | 处置 |
|---|---|---|
| R1 | **Bundesliga 2026 coverage 假象**(§1.4)| D1-1:8/28 揭幕日实测 events/lineups/xG;若真缺→**不降级**(单一标准):依赖 xG/阵容的战术在德甲自动静默(既有数据闸门),3-way 定价与赛季模拟照常,同时追 API-Football 工单 |
| R2 | **PMUS 俱乐部覆盖与 slug 语法未证**(公开探测受限)| D1-3:SDK search 实测;若 PMUS 不挂某联赛→该联赛单场所(Kalshi)运行,edge 口径自动退化为 vs_kalshi(机制已存在) |
| R3 | **UCL 抽签未出**(8/27-28)| league-stage 模拟推迟到 fixtures 落地;资格赛期间 KXUCLGAME 用普通两队定价即可 |
| R4 | 西甲/意甲 **H2H tie-break v1 用 GD 近似** | MODEL_NOTES 披露;phase 3 后补真 H2H(模拟内记录对阵矩阵);对 champion/top4 概率影响仅在积分并列场景,量级小 |
| R5 | **17 条 in-play 战术常数是 WC n=26 标定**(finishing_uplift、late_goal 窗、possession_trap 等)| 沿用启动但 `docs/` 台账登记"待重估"清单;攒 100+ 俱乐部场次后走 `_validate_signals` 同款流程重估(研究脚本已随整体复制在手边,[闲置]待用) |
| R6 | **别名表规模 ×5**(~250 队 vs 48 队,欧联/欧协联长尾小俱乐部最多)且赛季中有冬窗改名/搬场 | 精确别名铁律 + ticker 3 码双钥匙;monitor 加"未映射市场"告警(发现层 silently drop 的世界杯行为改为记数上报) |
| R7 | **周四(欧联双赛 36 场/轮)与周六(五大联赛日)额度尖峰**(§6.1)| 三档额度调速器;极端时按流动性取舍(保有挂牌+有持仓场次),**不按赛事分级**(单一标准原则) |
| R8 | 巴甲/阿甲**赛季中途接入**(已踢半程)| standings 直接给出当前态,league_season 天然支持;性能台账从接入日起算,不回溯 |
| R9 | 升降级/冬窗转会导致实体漂移 | club_registry 带 `valid_from/valid_to`(借 ticker_aliases 机制的日期窗思想,不共用代码);FC26 评分静态无碍 |
| R10 | 无锁并发(世界杯已知弱点)与 30s/900s 任务重叠 | 新模块 flock 全覆盖(§4);macro ebdf6a9 事故的直接教训 |
| R11 | **阿甲 2026 赛制细节未核**(分区数/季后赛轮次/平局是否直接点球——阿根廷赛制年年变)| D1-7:API-Football rounds 实拉 + Kalshi `KXARGPREMDIVADVANCE` 开放事件探测;核验前阿甲季后赛轮的 `stage_rules` 先按 `cup_single`(直接点球)保守缺省 |

---

## 8. Day-1 验证清单(开工第一天,任何代码之前)

- [x] D1-1 **已完成(2026-08-26)**:GitHub 回滚锚点 tag **`wc-baseline-20260826`** → commit `c6f51aa` 已推 origin(推前三查全绿:WC 目录零未提交、本地与 origin 同步 0 领先、跟踪文件无 .env/.key/.pem 且内容级密钥扫描零命中)。**revert 配方**:`git checkout wc-baseline-20260826 -- prediction_market/`。注意这是**代码级**备份——`wc.db`/`raw`/`logs`/`.env` 数据与密钥本就不进公开仓库(也不该进);Phase 0 整体复制会在本地天然形成一份数据快照(APFS clone)。
- [ ] D1-2 Kalshi:12 项赛事 GAME 系列(`KXEPLGAME/KXLALIGAGAME/KXSERIEAGAME/KXBUNDESLIGAGAME/KXLIGUE1GAME/KXUCLGAME/KXUELGAME/KXUECLGAME/KXBRASILEIROGAME/KXARGPREMDIVGAME/KXCONMEBOLLIBGAME/KXCONMEBOLSUDGAME`)各拉一页 events 存 scratchpad(别名表原料;解放者 9 月恢复后补拉)。
- [ ] D1-3 PMUS SDK `search.query`("Premier League"/"La Liga"/"vs")→ 确认 slug 语法与市场前缀,回填 §1.3/§3.1。
- [ ] D1-4 API-Football `/fixtures?league=39&season=2026` 全季拉一次核对 380 场 + round 命名("Regular Season - N")→ 固化 registry `stage_rules`。
- [ ] D1-5 ClubElo API 试拉五大联赛(`api.clubelo.com/2026-08-25`)确认可用性与命名对齐成本。
- [ ] D1-6 8/28 德甲揭幕:R1 复查。
- [ ] D1-7 阿甲赛制核验(R11):API-Football `fixtures?league=128&season=2026` 全轮名清单 + 季后赛 ET/点球规则 → 固化 registry `stage_rules`,消掉 §3.0 表中的"D1-7 待核"标注。
- [ ] D1-8 欧战 advance 市场形态:`KXUCLADVANCE/KXUELADVANCE/KXUECLADVANCE` 开放事件探测(资格赛两回合 tie 的 advance 按 tie 挂还是按场挂、次回合是否新开)→ 定 C5 的市场对接口径。

---

## 附录 A:机械改名 checklist(Phase 0 sed 分类,均已实测计数)

0. **前提 = Phase 0 步骤 1-4 已完成**(复制、对账、归档、gitignore 补丁);sed 扫描范围 = 新目录全部 .py/.sh/.plist,**排除 `TRANSFORM_PLAN.md`**(它对源模块的引用是故意的)。
1. `\bprediction_market\b` → `prediction_market_soccer`:.py 151 文件 912 处(import / `python -m` / docstring 路径);sh+plist 6 文件 25 处。**注意排除**:README 引用的论文名、`.claude/plan/prediction market plan/` 文档路径引用(改指本文件)。
2. `wc.db` → `soccer.db`:26 处(store.py 为源,其余是注释/工具脚本)。
3. launchd label:`com.someopark.prediction{live,matchtrigger,refresh}` → `com.someopark.soccer{live,trigger,refresh}`(3 plist × label+日志路径)。
4. 前端产物名:`worldcup_model.json` → `soccer_model.json`;所有 `frontend_data` 路径 → `public/data/soccer/`(config 单点)+ `sync_soccer_data.mjs` 白名单。
5. **`sync:wc` → `sync:soccer`**(refresh_and_deploy.sh 2 处;该串不含 "prediction_market",类 1 的 sed 抓不到——本次逐行核验新增的类)。
6. 文案层(非阻塞,phase 4 前清完):"World Cup"/"世界杯"/"48 队" 字符串 112 处(py),集中在 system_overview / frontend_export / PDF 标题 / docstring。

## 附录 B:世界杯模块参考文档索引(设计出处,改造时对照)

- 设计蓝图 24 篇:`.claude/plan/prediction-market-plan/00-24_*.md`(01 Kalshi 集成、02 数据管线、03 建模、04 策略执行、05 基建、10 先验模板、14/16 前端、18 里程碑、20-23 in-play 复盘、24 advance fork)。
- 系统总览与赛后复盘:`prediction_market/README.md`(结构照抄给 soccer README)。
- in-play 战术证据:`prediction_market/docs/INPLAY_FINDINGS.md`、`INPLAY_SCENARIOS.md`(复制,R5 台账挂靠处)。

---

## 附录 C:Artifact 逐一改造对照(一节不落;"无修改"也明写)

> 覆盖前端 REGISTRY 全部 **23 个 wc_* 类型**(网格 20 + 网格外 3,实测自 `PredictionArtifact.tsx:1987-2010` 与 `PredictionArtifactGrid.tsx` PREDICTION_ITEMS)+ 4 个非网格表面 + 服务端 chat 面 + 全部后端孤儿输出。soccer 键名 = `wc_` 前缀换 `soccer_`(个别语义改名单独注明)。"前端无修改" = 组件逻辑零改动,只换 fetcher 路径(`/data/soccer/…`)与 i18n 命名空间(这两项是全体共有的机械动作,下文不再重复)。

### C-0 概览组(overview)

**C-1 `wc_overview` → `soccer_overview`**(OverviewModelNotes)
- 后端链:`frontend_overview.json` ← `frontend_export.build()` ← `system_overview.py` 静态目录 + 各输出聚合。
- 后端改动:`system_overview.py` **全文案重写**(48 队/淘汰赛措辞 → 12 项赛事/赛季模拟/两回合);`frontend_export.py` 结构原样(schema_version "1.0" 保留)。
- 前端改动:**无修改**。

**C-2 `wc_venues` → `soccer_venues`**(VenuesApi)
- 后端链:`risk_report.json` ← `risk_report.py`(场所余额实调 + API 预算 + 闸门)。
- 后端改动:KX 系列清单换 registry 输出;预算行显示 6,500 自限;其余机制(demo URL、prod-key 不查询铁律、blocked_summary)**无修改**。
- 前端改动:**无修改**。

**C-3 `wc_budget` → `soccer_budget`**(Budget;网格外,deep-link/chat 兼容位)
- 后端链:同 C-2(读 `risk_report.json.api_budget`)。
- 改动:**前后端均无修改**(随 C-2 联动);保留网格外兼容位的做法照抄(注释同款)。

**C-3b `wc_risk` → `soccer_risk`**(RiskCard;网格外,chat/deep-link 可达——首版附录漏列,本次核验补上)
- 后端链:`risk_report.json` ← `risk_report.py`(与 C-2 同文件:RiskCard 读限额/敞口/kill-switch 段,VenuesApi 读场所/预算段)。
- 后端改动:随 C-2(同一次生成);敞口/限额段数值机制**无修改**($1 硬上限、kelly 0.25、单市场 5%/主题 10%、日亏 8% 熔断逐字继承)。
- 前端改动:**无修改**。

**C-4 `wc_methodology` → `soccer_methodology`**(Methodology;网格外)
- 后端链:**无数据文件**——纯 i18n 静态视图(`prediction.cap` 目录,实测 `PredictionArtifact.tsx:298`)。
- 后端改动:**不存在后端,无修改**。
- 前端改动:组件零改动;`soccer.cap` 目录文案重写(数据源/赛前/决策/实时/模拟/其他 六段换 soccer 版)。

### C-5 球队情报组(teamIntel)

**C-6 `wc_champion` → `soccer_champion`**(ChampionOdds)
- 后端链:`worldcup_model.json` ← `run_model.refresh_champion()` ← `tournament.simulate` + `champion_prices`。
- 后端改动:C4(`league_season.py` 赛季模拟)+ payload 改 per-league 分组(`soccer_model.json`);冠军 ¢ 源换 registry 赛季系列(`KXPREMIERLEAGUE-27` 等);elimination/confirmed-reach 双覆盖层 → 数学锁定覆盖层。
- 前端改动:列语义换(`FIFA`→Elo 序、`Grp`→删、`SF/QF/R16`→`前四/降级`或 UCL 阶梯,按 kind);48 队假设(L136)删除;联赛 chips(§3.7)。

**C-7 `wc_reach_round` → `soccer_season_odds`**(ReachRound;语义改名)
- 后端链:`reach_round.json` ← `reach_round_export.py` ← `champion_prices.reach_round_cents`(`KXWCROUND-26*`/`KXWCGROUPQUAL` + Poly 5 slugs)。
- 后端改动:[重写] → `season_odds_export.py`(§2.2 已列):league kind 出 {冠军/前四/降级} 三档,swiss_ucl 出 {top8/r16/qf/sf/final/冠军} 阶梯(`KXUCLTOP8/KXUCLRO16/RO8/RO4/FINALIST`),cup 出 {冠军+各 tie advance};`_group_form`(12 组积分)→ 直接读 `standing` 表(API 直供,世界杯还得自己算,这里更简单);`_KALSHI_SANITY_CENTS` 交叉验证闸门原样。
- 前端改动:轮次 tabs → 档位 tabs(caps/kind 驱动);行内 group_points/gd 列 → 联赛积分列;组件骨架(表格+edge 高亮)无修改。

**C-8 `wc_golden_boot` → `soccer_top_scorer`**(GoldenBoot;语义改名)
- 后端链:`worldcup_model.json.golden_boot` ← `golden_boot.simulate_golden_boot`(嵌在锦标赛路径)← seed_players.json + fc_player + topscorers。
- 后端改动:§2.2 golden_boot 行(去晋级耦合、`future_opp` 对手加权升主模型、种子=§3.8-c 双源);市场对接 `KXWCGOALLEADER` → `KXUEFACLTOPGOAL`(UCL)+ 各联赛射手系列(D1-8 探测形态);per-league 输出。
- 前端改动:列结构(e_goals/p/goals-so-far)同构**无修改**;队名→俱乐部、联赛 chips。

**C-9 `wc_squad` → `soccer_squad`**(SquadStrength)
- 后端链:`squad.json` ← `squad_export.py` ← `squad_strength.squad_index`。
- 后端改动:§2.3-#5(squad 表间接层删除、`_LEAGUE_STRENGTH` 升主角);`fifa_rank` 字段填 Elo 序(字段名保留,前端零适配)。
- 前端改动:**无修改**(列头 i18n 文案 FIFA→Elo)。

**C-10 `wc_styles` → `soccer_styles`**(TeamStyles)
- 后端链:`team_styles.json` ← `team_styles_export.py`(手工 48 队 PRIOR × 0.55 + fixture_stats 实况 × 0.45,周更节流)。
- 后端改动:PRIOR → **FC26 playStyles 自动生成先验(§3.8-d)** + 豪门人工覆盖;实况混合/10 风格码/`_SECOND_STYLE_FRAC`/`_with_legacy` 适配器/周更节流**全部无修改**;输出按联赛分组。
- 前端改动:矩阵按联赛分页(chips);其余无修改。

**C-11 `wc_form` → `soccer_form`**(FormCard)
- 后端链:`form.json` ← `form_export.py` ← `form_strength`(nt_recent)。
- 后端改动:§2.3-#3(club_recent、竞赛权重、DECAY 缩短)。
- 前端改动:**无修改**(`F` 友谊赛徽标 → 杯赛/欧战徽标,i18n 级)。

### C-12 实时组(live)

**C-13 `wc_match_pricing` → `soccer_match_pricing`**(MatchPricing)与 **C-14 `wc_predictions` → `soccer_predictions`**(Predictions)
- 后端链:同一个 `upcoming.json` ← `upcoming_export.build`(两个视图共用)。
- 后端改动:§2.2 upcoming_export 行(league/stage/caps 字段、per-league home_adv 定价、advance 块 caps 化)。
- 前端改动:按联赛分组渲染 + MatchCard caps 化(见 C-26);两视图自身逻辑无修改。

**C-15 `wc_divergence` → `soccer_divergence`**(Divergence)
- 后端链:`xv_matches.json` ← `xv_monitor.compare_matches`。
- 后端改动:§2.2 xv_monitor 行(发现层 registry、devig N-way、`is_knockout`→caps)。
- 前端改动:**无修改**。

**C-16 `wc_inplay` → `soccer_inplay`**(InPlay)
- 后端链:`inplay_live.json` + `inplay_live_advance.json` ← `live_refresh` → `inplay_export`(+`_advance`)← 17 战术 + hedge。
- 后端改动:§2.2 两行(caps 维度、advance 只扫 `caps.advance`)+ §6.1 调速器。
- 前端改动:advance lens 仅当数据含 `caps.advance` 场次(AdvanceMode 联动,C-27);LIVE 卡跨联赛混排置顶;其余无修改。

**C-17 `wc_pricetrack` → `soccer_pricetrack`**(PriceTrack)
- 后端链:`milestone_marks.json` ← `milestone_export` ← `milestone_snapshot`(live 捕获)+ `backfill_milestones`(Poly Global)。
- 后端改动:§2.2 milestone 行(`_ALIASES`→俱乐部别名、slug regex per-league、`_NEXT_ROUND`→per-competition);**七里程碑结构/三视图对账口径无修改**。
- 前端改动:**无修改**。

**C-18 `wc_schedule` → `soccer_schedule`**(Schedule + BracketView tab)
- 后端链:`schedule.json` ← `schedule_export`;bracket tab 世界杯**不读后端**(`knockout_bracket.json` 被前端故意无视,用硬编码 `R32_TREE`+`BR_META`+`BR_FLAG`,实测 L463-502)。
- 后端改动:schedule_export 加 league 维度;[重写] `ucl_bracket_export.py`(§2.2)产出**数据驱动**的瑞士表+KO 树。
- 前端改动:列表视图无修改;**BracketView 重写为读 `ucl_bracket.json`**(三个硬编码字典删除,场馆/国旗→俱乐部 logo);bracket tab 仅 swiss_ucl/cup kind 显示(caps)——世界杯"前端无视后端 bracket"的死结在 soccer 里反转闭环。

### C-19 质量组(quality)

**C-20 `wc_performance` → `soccer_performance`**(PerformanceCard + 内嵌 BetLog)
- 后端链:`performance_report.json`(+PDF)← `performance_report.py` + `settle_bets`(冻结台账)。
- 后端改动:五赛道口径/`match_pick` 单一真值/冻结纪律**无修改**;新增 per-league 分段统计;PDF 标题文案。
- 前端改动:BetLog **无修改**;头部联赛过滤 chips(phase 6 可选)。

**C-21 `wc_calibration` → `soccer_calibration`**(Calibration)
- 后端链:`oos_report.json` ← `oos_eval.evaluate`(live_refresh 循环写)。
- 后端改动:§3.5 per-league 校准结构(`{league: {...}}`)+ `price_match` stage 化。
- 前端改动:加 per-league 切换;指标卡本体无修改。

**C-22 `wc_backtest` → `soccer_backtest`**(Backtest)
- 后端链:`backtest.json` ← `backtest_export`。
- 后端改动:`reg_score` 90′ 口径、uniform 2/3 基准**无修改**;加 per-league 维度。
- 前端改动:**无修改**。

**C-23 `wc_params` → `soccer_params`**(ParamSweep)
- 后端链:`param_sweep.json` + `param_selected.json` ← `param_sweep.py`(config 回读)。
- 后端改动:网格换轴(§2.2);**前 6 周不跑**(样本纪律)。
- 前端改动:逻辑无修改;⚠️ 未核验组件对文件缺失的空态(世界杯从未缺过此文件)——Phase 3 加一行空态兜底,这是本附录唯一的前端未核验点,如实标注。

### C-24 报告组(reports)

**C-25 `wc_pdfs` → `soccer_pdfs`**(Pdfs)
- 后端链:`performance_report.pdf` + `risk_report.pdf` ← 各自 `build_pdf` ← `pdf_style.py`。
- 后端改动:标题/文案 soccer 版;`pdf_style.py` **无修改**。
- 前端改动:**无修改**(文件名不变,路径入 `/data/soccer/`)。

**C-26 `wc_microfootball` → 不移植**(MicroFootballSim + TrajectoryPlayer)
- 后端链:`microfootball_index.json`/`dfm_index.json`/`/sim` 资产 ← `sync_microfootball.mjs`(box B ssh)+ `microfootballAnalyze` 路由。
- 决定:**明确不移植**——DFM/微足球是世界杯研究线(box B 智能体模拟器只有国家队语料),俱乐部无对应数据源;soccer 网格无此卡、SoccerArtifact REGISTRY 不含此类型;世界杯侧原样保留不动。若未来 box B 产出俱乐部模拟,按 microfootball 先例补一节。

### C-27 非网格表面

**C-28 PredictionUpcoming → SoccerUpcoming**(欢迎屏"即将开赛"卡)
- 数据:`upcoming.json` + `inplay_live.json`(60s/20s 轮询)。
- 改动:标题 `World Cup 2026` 字面量(实测 L168,非 i18n)→ `soccer.upcomingTitle`;比赛按联赛分组、LIVE 跨联赛置顶;轮询/排序逻辑无修改。

**C-29 MatchCard → SoccerMatchCard**
- 改动:advance 区块与"2-way 晋级镜头"从 `knockout` 布尔 → `caps.advance`;新增两回合 `agg` 徽章("首回合 2-1",cup_two_leg_l2 时);三源价/去 vig 注/决策行布局**无修改**。

**C-30 AdvanceMode(context + 分段开关)**
- 改动:渲染条件从"淘汰赛阶段"→"当前数据含 ≥1 场 `caps.advance`"(§3.0);context 机制**无修改**;纯联赛视图永不挂载。

**C-31 CountryName / countryIndex / PredictionFocusContext → ClubName / clubIndex / SoccerFocusContext**
- 数据:clubIndex 并发拉 soccer 侧对应 JSON 集(世界杯 13 源中 microfootball 剔除,加 season_odds)。
- 改动:`RORDER` 轮次阶梯(实测 countryIndex.ts:34)→ per-kind 状态语义(league:争冠/欧战区/中游/保级/降级;ucl:阶梯);`FIFA #rank` → Elo 序;popover/MutationObserver 滚动定位机制**无修改**;旗帜 → `team.logo`。

**C-32 服务端 chat 面**(artifactDetector / predictionMarketTool / prompt / agentPrompt / chat.ts grounding)
- 改动:全部为**加法**——`soccer_*` 触发词表(中英)、soccer 版数据工具(view→`/data/soccer/` 文件映射,五个工具同构复制)、prompt 里加 soccer 段、chat.ts 加 `soccer_*` grounding 分支;`wc_*` 现有分支一律不动(共享代码文件的六处加法清单见 §3.7)。

### C-33 后端孤儿/无前端消费输出(逐项决定,不跳过)

| 输出 | 世界杯现状 | soccer 决定 |
|---|---|---|
| `latest.json` / `model_run_*.json`(保 10 份) | run_model 留档 | **无修改**,照常生成 |
| `param_selected.json` | config 回读(`_apply_selected_params` 自动加载) | **副本在 Phase 0 步骤 3 随 output 归档移走**(否则 soccer 静默采用 WC 调参——实证炸弹);soccer 自己的要到 param_sweep 启用(6 周后)才出现,期间 config 走手设默认,行为正确 |
| `calibration.json` | 生成+同步,前端不读(risk_report/backtest 内部读) | **无修改**:照常生成与同步,维持"内部消费"角色 |
| `match_signals.json` | executor 生成+同步,前端不读 | **无修改**;台账注记"将来想上信号卡再接" |
| `xv_champion.json` + 死 fetcher `getWCChampionXV` | 后端生成,前端从未消费 | 后端保留生成(研究价值);**前端死 fetcher 不镜像**(死代码不复制进 soccerApi.ts) |
| `inplay_signals.json` + 死 fetcher `getWCInplay` | live_poller 写,前端不读 | 同上:后端保留、前端 fetcher 不镜像 |
| `knockout_bracket.json` + 死 fetcher `getWCKnockout` | 后端生成,前端故意无视(硬编码) | **反转闭环**:`ucl_bracket.json` 成为 BracketView 真数据源(C-18),死结终结 |
| `health.json` / `signals.json` / `inplay_opportunities.json` / `walkforward_eval.json` | 内部/研究输出 | **无修改**,照常生成不进前端 |
| `.trigger_watermark` / `.champion_watermark` / `inplay_review*.jsonl` | 水位与复盘日志 | 逻辑**无修改**,但**副本水位文件随 Phase 0 步骤 3 归档清零**(WC 的 104 场计数会压死 soccer 触发器——实证炸弹);soccer 从自身第一场结算起重建;champion 水位改 per-league 计数(§2.2 live_refresh 行) |
