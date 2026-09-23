# Pinned-code backlog（需要 forward-method epoch 才能上线的改动）

**为什么需要 epoch**：`util/ model/ strategy/ config/ ingest/ venues/ jobs/ exec/` 八个目录
（外加 `util/forward_methods.runtime_snapshot()` 里点名的 26 个 ops/config 文件、以及
`identity_manifest_id()` 哈希的 `config/club_identity.json` + `data/priors/aliases_*.json`）
被激活 epoch 的运行时指纹钉住。改动其中任何一个字节，paper 前向路径**立刻停止交易**
（`entry_method: Running code or parameters differ`），直到登记并激活一个带新指纹的 epoch。
2026-09-11 18:05 UTC 曾因此中断生产 16 分钟。

**上线方式**：前向 epoch 工作流，要求无进行中比赛、
无未平 paper 仓位、三个 launchd 服务已卸载。窗口通常是 UTC 02:30–11:30（南美夜场结束
到次日首场之间）。规则不变的改动只换指纹，兼容集自动取传递闭包，旧前向入场继续参与
PIT 校准。

2026-09-15 发布审核确认：`ops/forward_epoch_update.py verify` 会执行 paper/Demo
交易周期并删除旧恢复数据库，不能当作只读验收；该 CLI 的 activate 重试会重建 manifest/head，
也不能用于激活已提交后的盲目重试。v17 使用外部固定计划适配器复用
`version_workflow.activate_forward_method`，以同一 manifest/head/journal 恢复，验收只读。
具体步骤及 12 项隔离恢复测试见本页的 v17 发布记录。

> 本清单是**单一真值**。新发现的 pin 改动写进这里，不要只留在对话或记忆里。
> 上线一项就从表里删除并在底部「已上线」追加一行。

---

## 待办

- **A2：旧报价复用及全历表覆盖**：另一 AI 临时目录的 `ops/upcoming_export.py`
  还包含 carry-forward；未纳入 v17。复用与 PRE 暂存/决策输入相互影响，需要独立设计、
  逐字段来源和新鲜度验收。对应前端 7 文件的沿用提示也未部署，本轮不改前端。
- **全局按比赛开球排序**：v17 仅按各赛事最早开球时间调整赛事处理顺序，并非将所有赛事的
  单场比赛全局排序；不能保证所有近开球场次都先于其他赛事的远期场次获价。
- **预算诊断贯穿 discovery**：上线后真实导出发现 `venues/polymarket_us/discovery.py`
  的 `_probe_slugs` 仍将所有异常统一记为 `request_failed`；55 场的嵌套 `probe.error`
  实际都是 `BudgetExhausted`。报价入口 `_price` 已区分预算耗尽，这个探测分支还未统一。
  下一轮需补相同异常分类及 probe/catalog 回归，不应将这些行解释为 API key 失效或新网络故障。

## 待查

- **原有整套测试失败基线**：另一 AI 前后报告 34/33 个失败，涉及共享输出目录污染的可能；
  尚未独立重判，不应称其无害。v17 只据隔离目录内 124 项相关回归及 12 项发布恢复测试上线。
- **trigger 吞吐与调度**：保留原 900 秒触发及三份 plist；一轮慢于间隔的问题没有通过本次
  改动全部解决。Polymarket 的 180 秒是赛前扫描的请求准入/重试等待截止时间，不是整个导出的
  硬上限；已发出的 SDK HTTP 请求仍有自己的 30 秒超时，live 请求仍有各自等待预算。
- **归档阻塞**：本次停服前，`archive_review_logs --once-per-day` PID 32823
  在 trigger 进程组 74178 内滞留约 5 小时，已完成的 refresh 因而仍占 wrapper 锁。
  原始本地 `inplay_review_20260912.jsonl` 尚在；正常 unload 后进程组全部退出。
  归档依赖的外盘复制没有时限，此缺陷需要单独修复；v17 没改归档脚本或 wrapper。
- **发布工具后续加固**：现有 verify 只查 `errors`，Demo 某失败分支返回 `error`；
  `version_workflow._sha` 一次读完整数据库文件。v17 外部适配器避开交易验收并用等值流式
  SHA256 校验创建的备份，未修改这两处产品源码。

## 2026-09-15 实际发布（UTC）

- **06:19:18，v17 `2207bcba73b2` 已激活**：
  `soccer-timing-integrity-20260915-v17-rate-clock-identity-lock`。
  上一版为 v16 `6c530295b6be`（2026-09-14 的 Kalshi 短标签修复，已上线）。
- 仅安装 11 个运行文件：`venues/ratelimit.py`、`venues/polymarket_us/discovery.py`、
  `venues/kalshi/discovery.py`、`jobs/live_poller.py`、`ops/upcoming_export.py`、
  `ops/inplay_export.py`、`ingest/store.py`、`ops/refresh_all.py`、`ops/run_status.py`、
  `util/club_identity.py`、`data/priors/aliases_sudamericana.json`。
- **A**：相同 Soccer 数据根的独立进程共享令牌、live 保留额度与 Retry-After 冷却；
  最后一次 429、等待预算不足、重启和损坏状态都有回归。180 秒限额的实际边界如上。
- **B**：upsert 总入口不再仅因 `updated_at/fetched_at` 变化增加版本；实质变化和
  incomplete→complete 恢复继续记录。旧 source history 不删不改。小时增速须用自然刷新继续观测，
  不把离线去重测试等同于已证明每小时仅几百条。
- **C**：三个导出入口接通 409 个中文名，修正与既有前端标准冲突的
  `always_ready`、`atletico_torque`、`defensa_y_justicia`。前端本已有翻译，
  原先全空的是后端 `club_registry.zh` 导出字段，不是所有页面都未翻译。
- **D**：仅 club_recent opt-in 重试；自有事务失败回滚，并用旧 batch_id 和 rowid 下界
  定向恢复“数据已提交、availability 提交被锁阻断”的修订，不扫描全量历史或回溯可用时间。
- **E**：SEVEN→EIGHT 注释订正，删掉无足够消歧证据的南美杯别名
  `CD Universidad Católica → u_catolica`，保留其他有依据映射。
- 验收：124 项联合回归 + 12 项实际 SQLite 激活/硬退出恢复测试均在 `/tmp` 隔离目录；
  **零测试文件进入生产**。两处共六个金融文件哈希、399 场完整账本和纸面/Demo 财务行均未变；
  只追加一次 epoch activation，保持旧 epoch 兼容闭包及冻结 book `58fbc07692b9`。
  三服务已原配置恢复，首轮 live_refresh 为正常无比赛空转。World Cup、Macro、前端源码
  共 542 个受保护文件及 3 个 plist 均与发布前相同。
- 恢复和证据：
  `/Users/xuling/.codex/backups/soccer-releases/v17-20260915T061200Z/` 含原源码、候选源码、
  六金融产物、固定 activation-plan、124+12 测试、独立部署范围审核及只读激活验收；
  本模块 `data/book_candidates/epoch_20260915_v17_readbudget/recovery/` 含一次一致数据库
  备份和 `phase=verified` 的 `method-journal.json`。本轮未重跑历史回测、未做额外下单、未部署 Firebase。
- **06:25:54 正常报价刷新完成**：调用既有 `_payload_upcoming` 和 `write_both`，耗时
  306.334 秒，168 场/336 个球队中文字段全部非空；线上接口 HTTP 200，与本地发布文件逐字节一致。
  与刷新前 159 场相比，Polymarket 完整报价 7→18、部分报价 10→1；两者合计 17→19，
  样本数不同，不据此声称覆盖率显著改善。新结果另含 62 个显式预算耗尽、55 个探测预算耗尽
  （顶层仍是通用 request_failed）、3 个完整目录未找到、29 个目录窗口之外。
  最近六场中四场完整获价，两场南美杯受预算限制；全历表覆盖仍未完成。
  同轮纸面/Demo 财务哈希未变。实际小时修订增速及有比赛时的成交/结算不在本轮空窗验收中冒充已证明。

## 已上线

| 日期 | epoch | 内容 |
|---|---|---|
| 2026-09-12 04:29 | `66b4b708c658` v8 | demo 镜像 Decimal 序列化（venue→evidence 边界转 str） |
| 2026-09-12 04:35 | `ad13a1ccf038` v9 | evidence tiers 改为读取时从记录重算 |
| 2026-09-12 06:02 | `5965a28acf3e` v10 | 86 对 ClubElo 名桥、KF Víkingur 别名、南美杯 comp 内别名 |
| 2026-09-13 03:0x | `aabbce3970b8` v11 | ① Kalshi 联赛名实测别名表（12 赛事中 7 个名字不同，该校验路径对它们从未通过）<br>② review 日志剥 `binding.identity_evidence`（单行 87KB→27KB，receipt 33 字段全留）<br>③ stale-minute 仅对瞬时抖动重试（持续上游滞后改为记录不重试）<br>⑥ 当日模型预建进 `refresh_all`（7 赛事实测全建成） |

### 已证伪（无需修）
- **原第 5 项「跳过不留痕」**：2026-09-13 复核发现痕迹一直都在，写的是
  `paper_data_state_event`（577 行）而非 `paper_evaluation_v2`；2026-09-12 丢掉的 11 场
  每场都有 `missed_data/window_expired` 行。原条目是表名记错。
| 2026-09-13 03:4x | `d1b7147a3fc4` v12 | ④ PRE 腿优先决策 + `ps.positions()` 提出内层循环（实测单次 1.5 s × 每里程碑 ≈ 83 s，全花在 120 s 新鲜度预算上）<br>① demo 镜像三处静默 `continue` 现在记原因，summary 报 `skipped={原因:条数}` |

### 已查明（无需 pin 改动）
- **demo 镜像 0 单 = 两道真护栏,非缺陷**（2026-09-12 全量复核）：25 条 pre 腿中 20 条死于 Kalshi **DEMO 盘口 NO 边全空** → `yes_ask=None` → `no_executable_ask`（164/164 demo 回执无一有 ask；同一票号同一时刻 public 盘有 0.26/0.25）；另 5 条巴甲死于 demo **只挂了大小球事件、没有三向 GAME 事件**。问题在于两者都完全静默 —— 已在 v12 补上可观测性。demo 深度 0–4 档 vs 生产 19–55 档,是场馆特性。
- **原第 7 项 `persist_model_run`**：世界杯版遗留。`git log -S` 证明该调用点在俱乐部分叉时就没带过来（从未存在），函数签名是 `champion=`/`golden_boot=` 淘汰赛形状,俱乐部无对应物;`model_run`/`sim_champion`/`sim_golden_boot` 三表零行零读者。修法 = 让 `model_freshness` 改读 payload 里的 `meta.run_ts`（`ops/monitor.py`,**非 pin**,已随本轮上线）。检查从永久 ALERT 变 OK，且与 `model_export_freshness`（文件 mtime）语义分离。
| 2026-09-14 02:47 | `c7d53f8739ea` v14 | PIT 修订膨胀止血:`project_results_to_club_recent` 每次刷新给全部 nt_recent 行盖新 `fetched_at`,内容哈希去重因此失效,一张 12,473 行的表积了 640 万条修订(同一 entity 588 条,去掉该字段后只有 1 个不同 payload)。改为实质字段未变则不 upsert;验收:连跑两次投影 0 upsert / 0 新增修订 |
| 2026-09-14 03:30 | `2cff73ad5cbb` v15 | 投影缓存 `project_asof_cached`:决策恢复当日模型时不再重建世界视图,按 manifest_id 落盘(41MB/份)并在「输入集可证未变」时复用。判据=写缓存时的 availability 最大 rowid + 一次有界扫描(0.00s);高水位线在投影前采样;manifest 比对保留且仍执行。生产实测恢复 150s → **2.4s**。附带修掉并发 `immutable observation identity` 逸出 |

### 非 pin 但同期上线
- **两个索引**(2026-09-14 02:1x,无需 epoch):`avail_cover_v1(revision_id, available_at)` 消掉关联表回查、`source_rowid_v1(source)` 消掉 ORDER BY 临时排序。克隆实测 `project_asof` 201.7s→96.7s,内容逐表相同、manifest 逐位相同。
- **epoch 恢复点自动回收**(`ops/forward_epoch_update.py`):每次 activate 前的整库备份(12-16G/次)在 journal=verified 后由下一次 verify 回收,只留最新一份。两轮共释放 28.5G。
- **磁盘下降速率告警**(`ops/pre_match_sentry.py`):>5 GB/h 即 WARN,补上 10G 地板响应太迟的问题。
