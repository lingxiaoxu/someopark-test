# RNN 月度更新与本次执行记录

用户于 2026-09-09 批准：补齐 RNN 候选训练、独立验证、验收后切换以及月度维护。
本次目标候选为 `rnn_prod_20260904`，训练协议保持现役的 14 特征、3 seeds、
50 epochs、10 日序列。复用 9/5 构建、数据截至 9/4 的完整面板，不能把训练
截止时间改成 9/8。现役为 `rnn_v6f32n_20260731`。

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
