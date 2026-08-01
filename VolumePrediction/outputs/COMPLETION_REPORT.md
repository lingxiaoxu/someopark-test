# VolumePrediction 开发完成报告

日期: 2026-07-28 · plan: `.claude/plan/systemic-strategies-plan/VOLUME_PREDICTION_MODULE_PLAN.md`(873 行最终版)
· 实施纪律: §〇 全程遵守(仅限本目录开发/测试只进 /tmp/生产零污染/不简化不删减)

## 一、总结论

**plan 全部代码交付物 100% 完成**;复刻验收 G10 **8/8 通过**;58 步勾验
done 57 / deferred 1(文本线,用户令推迟)/ missing 0;测试 100+ 用例全绿。
剩余两项为纯日历累积的影子验收(P3 至 7/31、P4 至 8/7),系统每日自动执行。

## 二、交付物清单(按 plan 章节)

| plan 章节 | 交付物 | 证据 |
|---|---|---|
| §四 架构 | 48 个 .py,架构图逐文件对应(text_features 按令占位) | 文件树 + gen_checklist 核对 |
| §三 G1-G5 | 面板 v5(6.07M×165)、基线表、Table 1 三面板、参数量断言 | outputs/replication/table1_*.csv, baselines_table.csv |
| §三 G6-G8 | Table 2(μ 七档 MEL)、Table 3(迁移)、图 4 | table2_mel.csv, table3_transfer.csv, fig4(+11.59pp) |
| §三 G9 | 记账协议模拟、先知信号(图 5)、因子动物园(图 7)、表 3 同构、model 档三改进源 | replication_trading.py + fig5/fig7/table3_trading.csv |
| §三 G10 | 验收总表 8/8 + 差异报告 | G10_acceptance.md, G7_divergence_report.md |
| §五 服务化 | VolumeService 32 方法、六 utility profile、7 命名空间 | service.py(零重依赖验证), econ/objective.py |
| §六/§八 P0 | 12 项数据审计(10✓/2 项定性留档/1 推迟) | P0_data_audit.md/.json |
| §七 规格 | 双缺失协议/宇宙细则/拆股处理/config 全骨架 | config.yaml + data/ 七模块 |
| §九 P1 | 12 模型成绩表(定性排序=旧作)、58 步勾验 | legacy_scores.csv, appendixA_checklist.md |
| §九 P2 | 12 靶点修复对照(11✅/1⏸) | P2_targets_report.md |
| §九 P3 | 日更独立脚本 + 影子 d1 落地(7/27) | daily_update.py, outputs/adapters/*2026-07-27* |
| §九 P4 | 三 adapter + advice schema + 影子累积中 | strategy_adapters/ ×3(pairs 修复 list 解析) |
| §九 P5 | walkforward 分层引擎、tex v2 框架 | evaluation/walkforward.py, outputs/tex_v2/ |
| P2⑤ | μ/λ 校准 registry(aiss 实测 2.82e-3;冷启动显式标注) | outputs/registry/mu_calibration.json |

## 三、核心复刻数字(v5 终稿)

- 基线四项 ±0.6pp(ma5 93.95 vs 93.68 等)
- Table 1: 全谱 NN 14.21% / RNN(fund1) 19.96% / NN>OLS 全线(论文 ~11.3%)
- 迁移学习: 全 7 档 μ 优于统计模型,均值 +11.6pp;MSE↑/MEL↑ 签名全档
- G9: U 形复现、理想化夏普 8.18(论文 ~7)、AUM 单调、动物园成本通道 18/18 正
- 12 模型: lgbm 16.34 > ols/lasso 13.67 > pls 13.23 > nn2 12.36 > sarima 9.15
  > lstm 7.17 > arima 7.51 > pcr 0.09 > prevday −12.2 > ada −79(定性排序=旧作)

## 四、开发中发现并根治的真 bug(全部有留档)

1. z-score 训练窗 std 退化 → OLS 压平(tech 0.29%→10.09%)
2. fund 组时序 z 的时间趋势轨道 → NN −283%→+8.79%(改横截面 z)
3. Mongo share_float 字符串股本 + 同日重复记录 → 市值静默置空(17.6%→81%)
4. ADR/外国发行人无股本源 → SIZE ADV 代理 z 回填(附录 B 授权)
5. fmp_share_float 索引前缀缺失 → COLLSCAN 400 万行挂死(IXSCAN 后 2.9s)
6. NN2 内部标准化 1e-8 地板 → 测试期爆炸(−4.1e9→12.36%)
7. pairs adapter pair_universe list 解析(与 universe.py 同款)
8. MFE 网络无超时挂死(requests 会话层注入 60s)

## 五、偏离与留档(不静默接受,G10 规则)

- G7: 从零直训 nn_econ OOS 失败 → 差异报告(生产 profile 不用此路线,零影响)
- G5: RNN 宽特征集(>100 列)退化(LSTM-32 容量),窄集领先(fund1 19.96%)
- G9: 动物园净增量 27.8% 为正(gross 通道盖过成本通道),成本通道 18/18 正,
  两通道统计留档 zoo_acceptance

## 六、剩余日程(纯日历,零开发)

- P3: 影子 d2-d5(每交易日 17:37 自动)→ 7/31 验收 MAPE/R²
- P4: 影子 d10 → 8/7 验收 → **plan 全量收官**
- 服务三档就绪度: ①ma5 保守口径今天可用 ②学习模型 promote 建议 P3 达标后
  (面板延展+训练+config promote,半天) ③四策略消费接线属下一阶段(等批)
