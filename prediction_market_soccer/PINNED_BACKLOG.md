# Pinned-code backlog（需要 forward-method epoch 才能上线的改动）

**为什么需要 epoch**：`util/ model/ strategy/ config/ ingest/ venues/ jobs/ exec/` 八个目录
（外加 `util/forward_methods.runtime_snapshot()` 里点名的 26 个 ops/config 文件、以及
`identity_manifest_id()` 哈希的 `config/club_identity.json` + `data/priors/aliases_*.json`）
被激活 epoch 的运行时指纹钉住。改动其中任何一个字节，paper 前向路径**立刻停止交易**
（`entry_method: Running code or parameters differ`），直到登记并激活一个带新指纹的 epoch。
2026-09-11 18:05 UTC 曾因此中断生产 16 分钟。

**上线方式**：`ops/forward_epoch_update.py precheck|activate|verify`，要求无进行中比赛、
无未平 paper 仓位、三个 launchd 服务已卸载。窗口通常是 UTC 02:30–11:30（南美夜场结束
到次日首场之间）。规则不变的改动只换指纹，兼容集自动取传递闭包，旧前向入场继续参与
PIT 校准。

> 本清单是**单一真值**。新发现的 pin 改动写进这里，不要只留在对话或记忆里。
> 上线一项就从表里删除并在底部「已上线」追加一行。

---

## 待办

（空）所有已知 pin 待办均已上线。新发现写在这里。

## 待查

（空）

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
