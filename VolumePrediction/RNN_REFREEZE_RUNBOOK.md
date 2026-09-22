# RNN 月度更新与本次执行记录

用户于 2026-09-09 批准：补齐 RNN 候选训练、独立验证、验收后切换以及月度维护。
本次目标候选为 `rnn_prod_20260904`，训练协议保持现役的 14 特征、3 seeds、
50 epochs、10 日序列。复用 9/5 构建、数据截至 9/4 的完整面板，不能把训练
截止时间改成 9/8。该次批准时现役为 `rnn_v6f32n_20260731`。

当前状态：2026-09-17 按用户新的明确指令，已人工切换到 `rnn_prod_20260904`，
正式发布验证完成。此次人工决定覆盖此前“性能验收通过后切换”的条件，详见末节。
同日 18:08 又按用户要求更新 LGBM production 为 `lgbm_prod_20260904`，
两类学习模型均为 0904 版本，全局 `refreeze_due=false`。
同日 18:41 已进一步将 LGBM 备用层替换为修复日期特征后重训的
`lgbm_prod_20260904_calfix_20260917`；RNN 主层不变。重建、重训、历史诊断及
上线记录见 `LGBM_DIAGNOSTIC_20260917.md` 顶部。历史诊断通过不代表五日前向通过。

## 2026-09-20 日更故障检查与修复

9/14–9/19 六次日更的 daily/RNN/TCA/execution 均退出 0；9/14、9/18
耗时约三小时来自历史财报批量刷新。9/19 虽完成，但 Polygon DNS 失败被共享
fetch 转成空列表，VP 把自有 splits 缓存从 9,787 条覆盖成 0 条；Mongo 也超时。
9/20 两个连接已恢复，并另查明未来财报集合的日期为 ISO 字符串，旧 BSON-only
查询即使成功也返回 0 条。不能把上述批次的退出 0 理解为输入数据完全健康。

修复：拆股刷新拒绝空/坏响应，保留有效旧缓存及原日期，无有效缓存时明确失败；
学习特征计算在逐股票容错前先校验缓存；未来财报查询兼容两种日期类型并去重。
错误日志不再输出上游敏感连接文本。63 项相关合成/回归测试通过。

9/20 15:19 ET 已恢复拆股缓存并重跑 asof 9/18 日更（目标 9/21），五套 adapter
及影子评估成功，无此次连接/服务错误。正式 12,978 条预测仍为 RNN 3,837 +
MA5 9,141，数值与修复前逐位一致；RNN seq_tail/meta 哈希不变。LGBM 备用
3,837 条中 2,408 条预测更新；权重、版本、RNN 优先路由均未切换。
审计备份及验证：`outputs/ops_audit_20260920/{audit,verification}.json`。

9/18 首个真实前向日的 asof 9/17 证据保留未改：237 个共同持仓的 MAPE 为
RNN 53.1892%、clean LGBM 33.2776%、MA5 51.7677%；RNN 地板连败仅 1 日。
9/21 起 LGBM 输入已采用本次数据修复，后续验收应记录这个服务代码变化，
不得把重跑/重发当成新增前向交易日，亦不得用未来日历倒灌改写旧验收结果。

## 已接入的运行流程

- `com.someopark.vp.rnnrefreeze`：每天美东 10:03 检查。周六让位现有重任务；
  本月已有候选则跳过。最新面板距 raw 超过 10 个交易日则等待面板更新。
- 真正起训只允许 09:30–14:30，并检查 pairs 活进程、PID/PGID、未完成 START；
  有并发训练锁，17:15 截止以避让 17:33 日更。守卫 exit=3 可次日或白天重试。
- 候选在隐藏 staging 中构建、验证后原子发布到
  `outputs/registry/artifacts/<version>/`，再登记 candidate；不会改现役。
- 每天现有 17:33 日更发布正式预测后，自动为 candidate 生成独立预测并评估。
  `outputs/shadow_rnn/candidates/<version>/` 存放候选预测、CSV、review.json。
- 正式版本的序列由 refresh 单独更新，影子只读；未上线候选自行更新自己的序列。
- 各层缓存必须匹配模型版本、asof、训练截止且数值完整。同日切换使用候选自己的缓存。

## 验收标准

至少 5 个已收盘的真实前向 NYSE 交易日。候选预测实际生成时间必须早于目标日
开盘，目标必须为 asof 的下一交易日，实际日期必须晚于训练截止。事后回放只作
诊断，不计入 5 日。9/9 上午 10:03 后才训练，9/8 与 9/9 的回放均不会计入前向。
若 9/9 正常完成，首个可计入日为 9/10，最早 9/16 收盘后积满 5 日。

新 RNN 必须直接对照训练时的现役旧 RNN，同时对照 MA5。所有指标基于共同有效
持仓样本；至少 30 只、覆盖率至少 80%，覆盖骤降、缺前向评估或无效行阻止 ready。
按持仓样本数加权的 MAPE 和 log-MSE 必须均不劣现役、均优于 MA5，并在至少 60%
的观察日两项指标均不劣现役。这些是运维验收阈值，不声称统计显著性。

`review.json` 只报告 observing/ready/failed，日更和训练任务均不会自动上线。
用户已批准本次完整流程；后续本线程复核 ready 的证据及当前现役版本未变化后，
可执行下述显式切换，无需重新请求同一授权。如果未通过，保留现役并报告原因，
不降低验收门槛。

## 操作命令（仓库根目录，someopark_run 环境）

```bash
conda run -n someopark_run python -m VolumePrediction.rnn_maintenance --train-monthly --dry-run
conda run -n someopark_run --no-capture-output python -u -m VolumePrediction.rnn_maintenance --train-monthly
conda run -n someopark_run python -m VolumePrediction.rnn_maintenance --observe
conda run -n someopark_run python -m VolumePrediction.rnn_maintenance --promote rnn_prod_20260904 --dry-run
conda run -n someopark_run python -m VolumePrediction.rnn_maintenance --promote rnn_prod_20260904 --by user_approved_20260909
```

正式 shell 入口 `conductor/vp_rnn_refreeze_monthly.sh` 会自行加载环境。
训练日志：`VolumePrediction/logs/rnn_refreeze_YYYYMMDD.log` 与训练模块自身日志。
训练状态：`outputs/refreeze_rnn/status.json`、`status_rnn_prod_20260904.json`。
候选状态：`outputs/shadow_rnn/candidates/rnn_prod_20260904/review.json`。

训练运行时不要另启训练或重型测试。守卫返回 deferred 时先看实际 pairs 是否已结束，
只在受保护的日间窗口重试，不能绕过守卫或清除别的任务的锁。

## 切换与回滚

`--promote` 重新计算验收，核对训练时现役与当前现役一致，核对候选状态和缓存已追到
最新 raw 对应的 next trading day，再调用 `svc.ops.set_blend(True, rnn_version=...)`。
保持原 LGBM production 指针和 full_coverage。切换记录进入 registry.blend_changes
及新工件 promotion.json。旧工件保留。

切换后在日更不在运行时，以 `daily_update --date <最新已完成raw日期> --no-fetch`
走正式发布路径，核对 latest 中确为新版本、训练截止、覆盖、health，必要时同日再跑
一次验证缓存幂等。不要用盘中日期触发拉取未完成行情。

若切换后的正式验证失败，保留错误证据，使用 `svc.ops.set_blend(True,
rnn_version='rnn_v6f32n_20260731', by='refreeze_rollback')` 回滚，并在只使用完整 raw 的
前提下让 refresh 按日补齐旧模型序列，验证输出恢复。不能手改 seq_tail_date。

## 后续跟进

Codex 本线程应每日美东 12:15 检查训练/候选报告。候选未建成时检查 launchd 和日志，
若已进入安全窗口且没有训练进程可重试现有命令；训练中只读进度。候选已建成时检查
昨日 17:33 日更是否追加了前向评估；必要时只重跑候选观察。累计够样本后复核指标，
通过则执行上述切换并验证；不通过则保留现役并解释。此次候选完成验收处理后暂停
这条 Codex 跟进，项目的月度候选任务和日更观察继续运行。

## 本次验收结案：2026-09-17

`rnn_prod_20260904` 训练成功，但首轮五个真实前向交易日（9/10、9/11、9/14、
9/15、9/16）验收失败，未执行切换。独立复核确认每天共同持仓覆盖率 100%，
共 1,171 个股票日样本，五份预测均在目标日开盘前生成。

加权 MAPE：候选 19.6790%、现役 19.5995%、MA5 21.4119%；加权 log-MSE：
候选 0.0756419、现役 0.0755674、MA5 0.0810563。候选虽优于 MA5，但两项
累计指标均略逊现役，双指标不劣现役仅 1/5 天，未达到 3/5 天要求。

本次跟进 `rnn-refreeze` 已暂停；正式 RNN 保持 `rnn_v6f32n_20260731`。
项目的月度候选任务与日更影子观察继续运行。注册表保留 candidate 状态以维持
本月训练幂等及影子数据积累；这不表示通过验收，也不会自动切换。
后续日更可能更新实时 `review.json`，本次五日结论和证据哈希已单独归档至
`outputs/refreeze_rnn/acceptance_rnn_prod_20260904_20260917.json`。

## 用户明确指令上线：2026-09-17

用户在收到五日失败结论后明确要求“必须切换成新的0904版本”。本次据此调用
`svc.ops.set_blend(True, rnn_version='rnn_prod_20260904',
by='user_explicit_switch_20260917')`，标记新模型为 serving，并保留原始 failed
验收记录。此决定为本次用户指定的人工上线，不改变通用自动验收门槛；没有清空
旧观察数据或把失败改为通过，也无需以新五日观察作为此次上线前提。

以完整 raw 日期 9/17 重发布，目标日 9/18。latest、history 和正式 RNN 预测已
核对：新 RNN 3,836 行、旧 RNN 0 行、LGBM 1 行、MA5 9,135 行，共 12,972 行。
新 RNN 数值与其独立候选缓存逐项一致，五个 adapter 建议文件全部刷新；同日再次
refresh 后预测相同，两版 seq_tail 文件哈希未变，均保持目标日 9/18。

LGBM production 指针及 full_coverage 保持原值。ZKIN 不在新 RNN 覆盖中，仍由
旧 LGBM 兜底，因此全局 refreeze_due 继续为 true；新 RNN 层训练截止为 9/4，
其自身不超期。首次操作因把全局 due 当作新 RNN 超期而回退，随后修正一次性
检查为逐层核验并完成上线，完整告警没有被清除。

本次操作记录：
`outputs/refreeze_rnn/switch_rnn_prod_20260904_20260917_175153/switch.json`；
模型上线记录：`outputs/registry/artifacts/rnn_prod_20260904/promotion.json`。
记录包含用户授权、人工覆盖说明、原始评估、切换前备份和正式验证结果。
原 refreeze 跟进保持暂停；项目日更将继续滚动现役 0904 的状态并做日常评估，
月度维护任务继续保留。

## LGBM 兜底更新完成：2026-09-17 18:08 EDT

用户明确要求立即更新旧 LGBM 兜底模型。复用 9/5 已训练完成、训练数据截至
9/4 的 `lgbm_prod_20260904` 工件；特征及模型输入维度与服务兼容，先独立生成
目标日 9/18 的完整预测并检查版本、数值、覆盖，再调用
`svc.ops.promote('lgbm_prod_20260904', by='user_explicit_lgbm_update_20260917')`。
旧 LGBM 状态记为 superseded，工件仍保留用于回退。

以完整 raw 9/17 重新发布后，latest/history 一致，共 12,972 行：RNN0904
3,836 行、MA5 9,136 行，旧学习模型行数为零。新 LGBM 在当前覆盖集合内全部
被 RNN 接管，作为 production 备用层和反事实对照保持启用；其正式反事实预测
与独立预检逐项一致。ZKIN 在两个 0904 学习模型中均为 active=False，按既有
覆盖规则转 MA5，未放宽活跃标记。五个 adapter 已刷新，RNN 预测值、blend
配置、模型注册记录和 seq_tail 哈希保持不变。

健康检查确认 `production=lgbm_prod_20260904`、`refreeze_due=false`、
`stale=false`、`forecast_static=false`。未手动清除告警标志。
操作记录：`outputs/refreeze_lgbm/switch_lgbm_prod_20260904_20260917_180716/switch.json`；
模型上线记录：`outputs/registry/artifacts/lgbm_prod_20260904/promotion.json`。
