# Soccer 模块写入与体积审计(2026-10-02)

**性质**:只读调查,未修改任何代码或数据。供修复人员直接使用。
**背景**:2026-10-02 当天机器三次因磁盘写满挂死/重启。主因已查明是 App Store 自动更新的失败重试循环(已处理),
但调查中发现 soccer 模块体积从 9/11 的 18 GB 涨到 93 GB,且有写盘超限记录,单独列出供修复。

---

## 一、体积:93 GB(2026-09-11 时 18 GB)

| 部分 | 体积 | 性质 |
|---|---|---|
| `data/book_candidates/` | 54 GB | 整库快照副本,大部分是死重 |
| `data/soccer.db` | 24 GB | 主库(WAL 模式,page_size 4096) |
| `data/raw/` | 11 GB | 原始抓取 |
| `research/` | 1.2 GB | |
| `data/.refresh-stage-*`(10 个) | 1.3 GB | 异常退出未清理的临时目录 |

## 二、book_candidates 54 GB

| 目录 | 体积 | 内容 | 问题 |
|---|---|---|---|
| `epoch_20260915_v17_readbudget` | 19 GB | `recovery/database-before.db` | 最新一个已验证回滚点,**按设计保留**,正常 |
| `epoch_20260914_labels` | 16 GB | `recovery/database-before.db` | journal `phase=verified` 且已被 0915 取代,**本该被回收但没有** |
| `leakfix-20260911` | 18 GB | `recovery/database-before.db` 5.9G、`recovery_epoch/database-before.db` 5.9G、`snapshot_20260912T030926Z.db` 5.9G、`source_snapshot.db` 719M | 目录名非 `epoch_*`,**回收函数永远不扫描它**;无 journal |

**根因**:回收函数 `_reap_superseded_recovery_dbs()`(`ops/forward_epoch_update.py:44`)只在手动执行
epoch 更新时被调用(第 151 行)。9/15 之后没有新 epoch,所以 0914 的 16 GB 一直未回收。
它也只 glob `epoch_*`,`leakfix-*` 被排除在外。

代码注释原文:"at 12.5 GB a piece, the three epochs of 2026-09-13 alone ate 38 GB of free space in one
night (the 2026-09-11 outage began exactly this way)"。——每次 epoch 更新都整库复制,现在一份 24 GB。

## 三、soccer.db 24 GB 内部(dbstat 实测)

| 对象 | 体积 | 行数 | 说明 |
|---|---|---|---|
| `source_observation_v1` | 11.08 GB | 9,562,903 | 近期每天新增约 2,700 行(9/24–10/2),增长已慢 |
| `paper_observation` | 4.40 GB | 3,173 | `payload` TEXT 平均 **1.42 MB/行**,最大 6.92 MB;`INSERT OR IGNORE`(`util/paper_store.py:144`),只增不改,9/22 后停写 |
| `quote_receipt_v1` | 2.71 GB | 22,846 | 约 120 KB/行 |
| `avail_cover_v1` | 1.08 GB | | |
| `source_lookup_v1` | 0.97 GB | | |
| `source_availability_v1` | 0.96 GB | | |
| `sqlite_autoindex_source_availability_v1_1` | 0.74 GB | | |
| `sqlite_autoindex_source_observation_v1_1` | 0.74 GB | | |
| `paper_evaluation_v2` | 0.40 GB | | |

主库目前**不是快速增长源**,但超大行设计让每次整库复制 / 备份 / epoch 操作都非常重。

## 四、写盘行为

macOS 资源违规报告(`/Library/Logs/DiagnosticReports/python3.11_2026-10-02-104657_*.diag`):

```
Resource Coalition: "com.someopark.soccerlive"
PID 7096   Parent 7086(live_refresh.sh 包装)
Event: disk writes
Writes: 8589.94 MB of file backed memory dirtied over 340 seconds (25.26 MB/s)
时段: 2026-10-02 10:41:16 → 10:46:56
调用栈热点: libsqlite3 / pwrite
```

正常 live 周期几秒结束(每 ~60s 一次,python 进程存活约 5s),这次跑了 340 秒以上。

**live 路径中唯一的重操作**(`ops/live_refresh.py` 约 440–470 行):结算场次数(`settled`)变化时触发整轮刷新:
1. `si.sync_topscorers()`
2. `ExportStage()`:`shutil.copytree` 复制 `output` 与 `frontend_data`,并对每个文件计算摘要(`ops/export_stage.py:22-33`)
3. `refresh_model()`
4. `form_export` / `season_odds_export` / `schedule_export`
5. `stage.promote()`

**8.6 GB 的确切去向无法事后追踪**(进程已结束)。最大嫌疑是 `refresh_model()` 对 24 GB WAL 库的大量写入
及随后的 checkpoint;10/2 09:58 的崩溃重启也可能导致结算水位(watermark)与实际不符,从而触发这一轮。

**ExportStage 崩溃残留**:临时目录只在 `__exit__` 中 `rmtree`(`ops/export_stage.py:115-117`),
进程被杀或机器崩溃即残留。现存 10 个:

| 目录 | 体积 | 建于 |
|---|---|---|
| `.refresh-stage-hddjalqo` | 462M | 09-10 23:54 |
| `.refresh-stage-lghy91hn` | 23M | 09-10 22:30 |
| `.refresh-stage-7bllu4po` | 50M | 09-11 23:05 |
| `.refresh-stage-tbtfwsjy` | 70M | 09-12 22:15 |
| `.refresh-stage-5529er7o` | 84M | 09-12 23:27 |
| `.refresh-stage-1u2pwe_g` | 84M | 09-13 00:03 |
| `.refresh-stage-nf1a1nae` | 89M | 09-13 22:28 |
| `.refresh-stage-0of5ktgo` | 90M | 09-13 23:00 |
| `.refresh-stage-fg0vei4s` | 90M | 09-13 23:53 |
| `.refresh-stage-ta1qbz64` | 271M | 09-20 02:01 |

## 五、对备份的影响

soccer 现有 **428 个 sqlite 库**(含 book_candidates 内全部副本)。`conductor/backup_gaps_to_external.sh`
每次逐库在线备份(`sqlite3 .backup`)约 99 GB,耗时数小时。回收死重后备份负担同步下降。

---

## 修复建议(按收益排序)

1. **回收函数独立运行**:每日定时或在 live 刷新末尾调用 `_reap_superseded_recovery_dbs()`,
   不再只依赖手动 epoch 更新。→ 立即回收 `epoch_20260914_labels` 16 GB。
2. **`leakfix-20260911` 由负责人确认**(泄漏修正 9/12 已上线、回滚已彩排)后删除或纳入回收规则。→ 18 GB。
3. **`ExportStage.__enter__` 时清扫超过 N 小时的 `.refresh-stage-*`**,防止崩溃残留累积。→ 1.3 GB,并防复发。
4. **量化结算触发路径的写盘量**:在 `refresh_model()` 及各 export 前后读取
   `proc_pid_rusage(..., RUSAGE_INFO_V2).ri_diskio_byteswritten` 打日志,定位 8.6 GB 写到了哪里。
5. **epoch 更新前做空间预检**:整库复制前检查空闲 ≥ 2× 库大小,不足则拒绝执行(避免重演 9/11、9/13)。
6. **长期**:`paper_observation.payload`、`quote_receipt_v1` 的大 JSON 移出数据库(压缩文件存储),
   主库体积与每次整库复制的代价都会大幅下降。

## 复核方法(只读)

```bash
# 体积
du -sh prediction_market_soccer/data/* | sort -rh
# epoch journal 状态
for d in prediction_market_soccer/data/book_candidates/epoch_*/; do
  j="$d/recovery/method-journal.json"; [ -f "$j" ] && echo "$d $(python3 -c "import json;print(json.load(open('$j')).get('phase'))")"; done
# 表体积(只读,约 2 分钟)
python -c "import sqlite3;c=sqlite3.connect('file:prediction_market_soccer/data/soccer.db?mode=ro',uri=True);[print(f'{b/2**30:.2f}G {n}') for n,b in c.execute('SELECT name,SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC LIMIT 10')]"
# macOS 写盘超限报告(按作业归属)
grep -l "soccer" /Library/Logs/DiagnosticReports/python3.11_*.diag
```

---

## 补充(12:25–12:38 实测):空闲周期的 Demo reconciliation 是现行写盘主力

**方法**:读 SSD 控制器累计写字节(`ioreg -c IOBlockStorageDriver` 的 `Bytes (Write)`,含 root 进程),
逐秒采样,与 `data/logs/live.out.log` 的周期时间戳对齐。

**10 分钟监测**:全盘写入平均约 20 MB/s(≈1.2 GB/分钟、≈1.7 TB/天),每分钟峰值 300–500 MB/s;
同期可用空间净变化仅 −0.02 GB——即几乎全是**写了又释放的临时数据**(SQLite 临时文件创建即 unlink)。
所有长期存活的用户态进程合计 < 1 MB/s,说明写手是每分钟拉起、几秒退出的短命进程。

**逐秒对时(130 秒)**:10 次 > 60 MB/s 的突发中 9 次发生在 soccer live 周期运行期间,且精确贴合日志:

| 突发 | 日志 |
|---|---|
| 12:36:32–36:36,75 → **468 MB/s** | `[live_refresh 16:36:36] no match window — skip`(UTC) |
| 12:37:38–37:45,369 → 62 MB/s | `[live_refresh 16:37:45] no match window — skip` |

每个周期在 skip 之前都打印:

```
[live_refresh] idle Demo reconciliation: {'enabled': True, 'needed': True, 'settled': 0, 'pending': 0, 'errors': []}
```

**结论**:空闲(无比赛窗口)周期里的盘口探测 + Demo reconciliation 每分钟执行;后者**每次都判定 `needed: True` 并执行**,
即使 `settled=0, pending=0` 无任何待处理项。每次执行约写入 1 GB(多为 SQLite 临时文件,对 24 GB 的
soccer.db 做排序/聚合时溢写),进程退出即释放,所以磁盘净空间不变,但:
- SSD 写入 ≈1.4 TB/天(主要磨损源)
- 系统高负载或挂死时,这些"创建即删除"的临时文件来不及释放,会在几分钟内堆积成几十 GB 的隐形占用

**修复建议(补充,优先级最高)**:
- `needed` 的判定逻辑:`settled=0 且 pending=0` 时应为 False,直接跳过 reconciliation
- 若 reconciliation 必须周期性运行,改为低频(如每 15–30 分钟)并只扫描增量
- 排查 reconciliation 中的查询:对大表的 `GROUP BY`/`ORDER BY`/`DISTINCT` 是否缺索引导致全表排序溢写;
  可设 `PRAGMA temp_store=MEMORY` 或给临时空间设上限作为兜底
- **定位入口**:`ops/live_refresh.py:690–705` 的空闲路径,每个周期依次执行两个操作,**两者都在嫌疑范围内**
  (突发发生在周期内、skip 之前,逐秒采样无法区分二者):
  1. `venue_liquidity.probe(conn)`(约第 690 行,盘口探测,写 `venue_book_probe`)
  2. `prediction_market_soccer.exec.demo_forward.reconcile_only(conn)`(约第 700 行,Demo 对账)
  建议在两者前后各读一次 `proc_pid_rusage` 写字节数并打日志,一个周期即可分辨。

---

## 修复状态(2026-10-02 当日追记)

- 建议 1–5 已全部实施并提交(249dce90):回收接入每日 refresh+盲区上报、`leakfix-20260911` 18.4GB
  经负责人确认删除、ExportStage 入口清扫 24h+ 残留、结算触发路径写盘打点、备份前 2× 空间预检。
  建议 6(payload 出库)单独立项待拍板。
- **补充节(空闲写盘主力)已定位并修复(2e49d9fc)**:元凶既不是 probe 也不是 `needed` 判定
  (`needed:True` 反映真实未平 demo 敞口,语义正确),而是两条路径共用的
  `util/paper_store.py::positions()` — `ORDER BY e.decision_at,e.decision_id` 无覆盖索引,
  SQLite 把含 302MB payload 的全结果集塞进 temp B-tree,每次调用溢写 328MB(lsof 实测 etilqs
  328,483,172 字节,与 payload 总量逐位吻合;`EXPLAIN` 显示 `USE TEMP B-TREE FOR ORDER BY`)。
  `settle()` 与 demo 对账两进程每分钟各一次 ≈1.2TB/天。排序挪到 Python 端(fetchall 本就全量进内存,
  不加索引是因为 428 个历史库副本吃不到 schema 变更)后 etilqs 328MB→0、序逐位一致、432 tests passed;
  生产实测残留峰值 50MB/周期(≈0.1TB/天,降 92%),残留在 demo 对账 enabled 分支内部,另查。
  `completed_records()` 同病同修。`temp_store=MEMORY` 被否(328MB 级排序进内存会重演资源耗尽),
  降频被否(改产品行为且 demo 进程不受益)。
