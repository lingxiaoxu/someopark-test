# 泄漏修正账本:生产切换手册(2026-09-11 所有者授权)

## 授权与范围

所有者原话(2026-09-11,聊天指令):"我允许解除冻结 按照最正确的方式修复 重跑然后 再冻结成新的冻结账本 只要你按照正确方式做出来"。
手续费明确不进账本(demo 成交上观测)。**只修未来信息泄漏**。

修的是什么:冻结账本的 352 条旧记录里,262 场的里程碑价格来自赛后蜡烛图回填,
该回填按**墙钟**取价、按**比赛钟**算比分(中场 15 分钟 → T60/T75 的价格早于模型已知的进球),
用 10 分钟 K 线且取最近邻(可取到未来)。

不改什么:选边、注型、仓位、盘中规则、出场规则、模型、实时行(90 场)的任何腿、前向 epoch(v7)的任何记录。

## 已完成(2026-09-11 17:23–18:30 UTC,全部在隔离副本 `/private/tmp/unfreeze` 上)

| 步骤 | 结果 |
|---|---|
| 生产备份 | `~/soccer_db_backup/pre_unfreeze_20260911T1723/`(库 754 MB integrity ok、六金融文件、priors、版本表) |
| 价格候选(修复方自己的采集器 `ops/rederive_milestone_prices`,隔离根、只读快照、固定 scope) | 277 场 / 4,968 条报价修正 / 18 条不可用(仅 1607650 `identity_conflict`);已封口 `research_completion=completed`;快照 sha `f187618d…` 未变 |
| 账本重建 `ops/leak_corrected_book` | 262 重建场修正(261 + 1 保留标 unresolved),90 实时场原样;保真度诊断:出场规则 89/90、盘中 73/90 复现 |
| 打包 `util/owner_authorized_correction` | 五文档(含 authorization.json)+ certification;`validate_backtest_bundle` 通过 |
| 注册 + 切换 `version_workflow.switch` | 新版本 `86646691…` 激活,journal `verified`,六文件两目录同 sha |
| 回滚彩排 `switch(restoring=True)` | 恢复到 `00ffda38…`,激活表 seed→replace→restore,report 回到 +11,682.1 |
| 日常路径 | `performance_report.build(freeze=False)` 与 `milestone_export.build` 直接返回新账本;marks 带近似时钟标签 |
| 测试 | 新增 8 项;全量与基线一致(33 个既有失败为修复方记录的旧断言) |

最终代码在干净副本上跑生产脚本本身(18:29–18:39 UTC,precheck→rebuild→bundle→register→switch→verify→rollback→verify 全通过,含 epoch 重绑)的数字(生产会因今晚追加的前向记录略有不同):

```
                 旧账本        新账本
盘前择时      +3,504.2¢    +2,701.9¢
盘中          +8,177.9¢    +4,442.5¢
合计         +11,682.1¢    +7,144.4¢
入场价        207 修正 / 54 保留(实时 PRE 行或非 Poly 来源)
证据层        posthoc 208/170 腿 → leak_corrected_approx_pit_paper 261 场;live 90;forward 7;unresolved 1
```

## 生产窗口

今晚 8 场赛事约 02:30 UTC 结束;9/12 首场 12:00 UTC;日刷新 11:30 UTC。**窗口 03:00–11:00 UTC(23:00–07:00 EDT)**。
前提(切换函数会强制检查):无进行中比赛、无未平 paper 仓位、无未追加的 completion、无未决 demo intent、`kalshi_mirror` 无 pending/持仓。

## 对抗评审后的修正(2026-09-11 18:30 UTC,三位审查者 go_after_fixes,全部已落实)

- **`util/` 下不能新增或修改任何文件**:`runtime_snapshot()` 会 rglob `util/`(以及 model/ strategy/ config/ ingest/ venues/ jobs/ exec/),动了就让前向 epoch v7 校验失败(18:09 已在生产发生,18:21 还原后恢复)。新模块放在 `ops/`。
- 切换后必须**把 epoch v7 重新绑定到新账本**(`activate_forward_method`,同一 manifest),否则每轮 paper 都报 `forward_method_not_activated`;回滚同理。`switch_book` / `rollback` 已内置。
- 前端目录是 `public/data/soccer`(`public/data` 是世界杯模块)。
- 步骤顺序:先消费 completion 再取快照;切换前要求"未封口 paper 入场 = 0"。
- 入场价只在 **PRE 行是重建行**时修正(实时捕获的 PRE 是真实可成交卖价,保留)。
- 方法版本标签每次尝试唯一(`…-{run_id}-{attempt}`),失败重试不撞唯一约束。
- 若切换在写 journal 之前失败,`data/.soccer-maintenance.json` 会残留:确认无写进程后用**同一 operation_id** 重跑即可(门禁可重入);若放弃,删除该标记文件。
  `switch` 步骤现在会自动从该标记文件读回 operation_id 并复用,不再每次新铸。

## 生产步骤(每步都有验收;任一步失败 → 停,不继续)

0. **静默服务**:`launchctl unload` 三个 plist(soccerlive / soccertrigger / soccerrefresh),确认无 `live_refresh`/`refresh_all`/`settle_reports` 进程。
1. **消费 completion 并检查**:`performance_report.build(conn, freeze=True)`(调 `consume_completed_paper`),确认"未追加的 completion = 0"且"未封口 paper 入场 = 0"(`leak_correction_switch precheck` 会打印)。
2. **快照**(在 1 之后):`leak_correction_switch rebuild` 自动完成 1 → 快照 → 重建。
3. **重建记录**(用快照副本,不碰生产库):`python -m prediction_market_soccer.ops.leak_corrected_book --db <snapshot_copy> --candidate-db <durable>/candidate/prices.db --run-id leakfix-prices-20260911T173407Z --out <durable>/rebuild.json`。
   验收:`unresolved == [1607650]`;`legacy + forward == 生产 strategy_book_record 行数`;`live_fidelity.exit_same ≥ 89`。
4. **持久根**:候选与包放 `prediction_market_soccer/data/book_candidates/leakfix-20260911/`(在仓库根内,gitignore 覆盖 data/),含 `candidate/prices.db`(只读 0444)、`soccer.db`(采集用的 17:23 快照,只读 0444)、`manifest.json`、`bundle/`。
   仓库根放 `.soccer-isolated` = `soccer-isolated-v1`(切换函数要求;加入 .gitignore)。
5. **打包 + 注册**(生产库):`assemble_report(prev_book, records)` → `make_bundle(root=仓库根, …, authorization=所有者原话)` → `validate_backtest_bundle` → `register(conn, root, bundle_dir)`。
   验收:`strategy_book_version` 多一行 origin=`explicit_backtest`,激活表**未变**。
6. **切换**:`leak_correction_switch switch` → `switch_book(...)`,目录 = [`data/output`, `../someo-park-investment-management/public/data/soccer`];切换前核对包内 previous_book(版本 id、ledger id、前向行签名)与当前激活账本一致,未封口 paper = 0;切换后**自动重绑 epoch v7 到新账本**。
   验收(`leak_correction_switch verify`):journal `verified`;`active_version().version_id == 新`;`active_epoch().book_version_id == 新`;`paper_trading.run_cycle` 返回 `ok` 且 errors 空;六文件两目录 sha 相同;`evidence_summary.leak_correction.owner_authorized == true`、`strict_pit_certified == false`;1623407 T60 = 0-1 / 客队 44.5¢。
7. **恢复服务**:`launchctl load` 三个 plist;看下一轮 `live_refresh` 为 `no match window — skip` 且无 Traceback;确认 11:30 UTC 日刷新用新账本(refresh 日志 `performance_report.json` 的 combined ≈ 新值)。
8. **前端**:`npm run build:soccer && firebase deploy --only hosting`(在前端目录)。
   **不能只跑 `sync:soccer`**:`firebase.json` 的 hosting.public 是 `dist/`,而 `sync:soccer` 只写 `public/data/soccer`;
   只 sync 再 deploy 会把陈旧的 `dist/` 重新发上线(实测 9/11 20:30 时 dist 停在 08:48,线上仍是旧账本 11,682.1)。
   `build:soccer` = `sync:soccer && vite build`,vite 会把 `public/` 复制进 `dist/`。
   验收:`curl -s https://someopark.web.app/data/soccer/performance_report.json | python3 -c "import json,sys;print(json.load(sys.stdin)['combined_pnl_cents_total'])"` 应为新账本合计。

## 回滚

`leak_correction_switch rollback`:`switch(..., restoring=True)` + `marks_for_book` 渲染 + **epoch 重绑到恢复的版本**(已彩排)。它只追加 `restore` 激活记录,不删任何行,不覆盖新追加的前向事实。
若切换中途崩溃:journal 处于中间阶段 → `version_workflow.recover(conn, root, recovery_dir, operation_id)`;绝不用旧库覆盖生产。

## 这本新账本不是什么

- **不是严格 PIT**:中场 +15 分钟是近似;8 月输入无可得时间证据。每条记录 `pit_status = approximate_reconstruction_leak_corrected`,清单 `strict_pit_certified = false`。
- **不含手续费**(所有者决定)。
- **盘中/出场腿用的 lambda 是今日先验**(ClubElo 网站源),与冻结时的先验不完全相同;每条记录存了 lambda,实时行保真度 73/90 说明差异量级。
- 1607650(Santa Fe–River Plate,身份冲突)保留旧腿并标 `unresolved`。

## 上线前对抗复核(2026-09-12 00:1x UTC,针对今晚生产漂移)

彩排跑在 18:05 UTC 的克隆上;此后生产经历了磁盘满(live 循环停 ~50 分钟)、10 条腿结算入账(记录 359→364)、库从 ~750MB 涨到 4.7GB。五维度复核 20 项发现、18 项被独立反驳,确认并已修:

- **`_accumulate` 把 `realized_cum_pnl_cents` 写在 `if r.get("bet")` 里**,而书内有 1 条 `bet=False` 的盘中腿(ord 352 / fixture 1635708)。该行会保留修正前的 3,370.2¢,而前后行是 2,615.0¢ —— 独立复现为 +755.2¢ 的单点尖峰。`forward_signature` 和 `_validate_cumulatives` 都看不到这个字段,没有任何校验或验收打印会发现。已把该行移出 bet 守卫(`ops/leak_corrected_book.py`,非 pin 文件)。
- **epoch 运行时指纹在账本提交之后才校验**(`version_workflow.py:333`,在 `activate_backtest_version` 提交并推六文件之后)。已把该检查前移进 `precheck`,漂移则在系统未动时中止。
- **第 8 步发的是旧 dist**(见上)。
- precheck 另加:磁盘余量(按实时库大小 ×3 估算)、残留维护标记提示、`proc_lock`/`match_trigger` 进程探测。
- `switch` 复用残留 operation_id,使手册记录的重入路径在 CLI 上真正可达。

复核确认为**干净**的关键项:库 `integrity_check` / `quick_check` 均 ok(带 WAL);`read_book()` 通过;`unresolved == [1607650]` 与彩排逐位一致(262 蜡烛图场 / 90 实时场);先消费后快照的顺序正确;维护门禁用 O_EXCL 标记 + 排他 flock,写者无法并发写入;render-once-copy-bytes 确是实际执行路径;磁盘 225GiB 对 ~15.6GB 需求有 14 倍余量。
