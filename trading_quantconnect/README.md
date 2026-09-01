# trading_quantconnect — QC 模拟盘镜像 + 日对账(持仓/目标/净值三平面)

QuantConnect paper 账户按**官方口径**镜像五策略(MRPT/MTFS/AISS/SSRS/BDC)实盘持仓。
本目录只读五份持仓文件与 QC API,只写 `reconcile/` 与 `logs/`——不下单决策、不碰
inventory、不碰上游 state。

## 镜像链路

```
五持仓文件(AISS/SSRS=account,pairs=inventory 双腿,BDC=inventory)
  → exporter(launchd 常驻,60s 轮询;股数 × golive 冻结 scalar → 整数 target + 小数残差账)
  → state/target_portfolio.json vN → ObjectStore 推送
  → QC 算法收敛到 target(幂等按版本;盘外推送次日 09:30 开盘执行)
```

- 面板按**决策日收盘**入账、QC **次日开盘**才成交——这段缺口是 ΔD 的永久台阶,
  由日对账的 `rebalance_mirror_lag`(换仓镜像滞后)项逐腿挂账(见下),不是误差。
- 重部署会重置 paper 账户:重部署后必须 `export_once(push=True, force=True)` 强推。

## QC↔面板日对账(reconcile/qc_reconcile.py,launchd 五时点)

三平面,写同一份 `reconcile/qc_reconcile_<session>.json`(`merge_section` 保证后趟
不抹前趟终态):

| 平面 | 内容 | 时点约束 |
|---|---|---|
| ① holdings | QC 实际持股 vs 已推 target 逐票 0 差(容忍 0 股) | 16:20——21:30 pipeline 一改 inventory 现场就没了 |
| ② target | 已推 target vs 由持仓文件重建的 target(内容哈希) | 同上 |
| ③ equity | D = P(官方 EOD Σ) − Q,ΔD 逐项归因后裁决 | Q 只在 [16:00 D, 09:30 D+1) 可观测;P 常到 D+1 上午才落地 |

- **Q = cash + Σ 逐票股数 × 官方收盘价(Polygon 日 K)**——QC payload 逐票价停在收盘前
  ~15 分钟(实测差 48.5bp),只降级作交叉校验。
- ③ 的两趟制:16:20 存 `close_snapshot`(Q 存档),次日 11:00/13:30 `--settle` 用存档
  补算——那条路径一行 QC API 都不调。派活按 `unsettled_sessions()` 欠账清单,不按
  "今天是哪天";ΔD 有紧邻交易日闸门,漏天不出裁决。
- **ΔD 归因项**:过渡期未镜像盈亏 / `rebalance_mirror_lag`(qty × (下单瞬间价 − 决策日
  官方收盘);滑点只算 [下单→成交] 尾段,不双计) / 滑点 / 小数残差 delta / 股息时点。
  换仓日阈值 3bp、无成交日 5bp;有算不出的项只给 partial,不给 ok。

## K 常数(ops/rolloff.py)

legacy 出清后两边持仓逐票相同,净值差定格为现金项 K——但**每个换仓日的镜像滞后+滑点
是永久台阶**,K 不是死常数。恒等式:

```
Q + k_effective ≡ P,   k_effective = k_equity(冻结值) + Σ 每换仓日(镜像滞后+滑点)
```

台阶直接从历史对账报告 attribution 累加(报告即台账,无第二真相源);缺台阶的天点名
并拦 ok。`--measure` 只读试算;`--freeze` 为**报告锚定制**(2026-08-31,方案 A):
K = 锚点场次对账报告里**判过的** D_usd,零 QC API、不要求账户静止——闸门只剩
"只冻一次 + L/S 队列空 + 锚点场次三段全 ok(equity 可 baseline)",`--session`
指定锚点(默认取最新一份三段全 ok 的报告)。

## 常用命令

```bash
# 手工对账(live 窗口内取现场;窗口外自动 settle)
ops/qc_reconcile.sh
# 补某天
conda run -n someopark_run python -m reconcile.qc_reconcile --settle --session YYYY-MM-DD
# K 试算 / 冻结
conda run -n someopark_run python ops/rolloff.py --measure   # 只读
conda run -n someopark_run python ops/rolloff.py --freeze    # 一次性,闸门把关
# 测试(121 项)
conda run -n someopark_run python -m pytest tests/ -q
```

细节与设计动机:`QUANTCONNECT_MIRROR_PLAN.md`;go-live 当日操作:`M0_MONDAY_RUNBOOK.md`。
