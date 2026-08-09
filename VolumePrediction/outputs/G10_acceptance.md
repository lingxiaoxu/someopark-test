# G10 复刻验收总表（P1 出口条件）

生成: 2026-07-27 · 面板: paper_full_v5(SIZE fallback 版, 6.07M 行 × 165 列)
· 协议: paper split 2019-21 train / 2022-23 test · 深模型 5 种子

| 编号 | 验收物 | 判据 | 实测 | 判定 |
|---|---|---|---|---|
| G2 | 基准 R² 表 + 图1 | 同量级(±5pp)或差异有解释 | ma5 **93.95** vs 论文 93.68 · lag1 **93.16**/92.53 · ma22 **92.78**/92.60 · ma252 **86.51**/86.12——四项全部 **±0.6pp** | ✅ PASS |
| G3 | 175→我方因子映射表 | fund-2 折让外逐列对应;fund2≥60 逐特征留档 | `fund2_feature_map.md` 62 列逐项留档;fund1 SIZE 三段拼接+ADV 代理 fallback(ADR 覆盖边界留档);FMP 幸存者掩码双轨报告 | ✅ PASS(折让留档) |
| G4 | 参数量断言 + 训练曲线 | count_params==公式;OOS 曲线平稳 | PaperNN `assert actual==formula`(代码内断言,恒真才返回);PaperRNN 单 bias 等价实现参数量=论文公式;fig3 训练曲线 MSE 0.290→0.254/0.284→0.250 单调收敛;**§7.11-5 MPS 容差实测(2026-08-09 补齐)**: 同配置(PaperNN tech,paper epochs,seed=0)重跑两次 OOS R² 12.0005%=12.0005%,**diff 0.0pp < 0.3pp 容差 PASS**(此配置下 MPS 完全确定),`replication/mps_tolerance_test.json` | ✅ PASS |
| G5 | 表 1 三面板 | rnn>nn>ols;η OOS R²~20% 量级;v 增量≤~1.2pp | tech: 14.35>12.34>10.09 ✓;fund1: **19.96**>13.61>10.10 ✓;全谱 NN **14.21%**(论文 ~11.3);Panel B 增量 0.2-0.5pp ✓。**偏离**: RNN 在宽特征集(>100 列)退化(earn 9.63<NN),LSTM-32 容量瓶颈,详见差异报告 §2。**C.1 MoE 对照(2026-08-09 补齐)**: 市值五分位专家 vs pooled 同协议 PaperNN(tech+fund1,论文 epochs): pooled 13.17% > MoE 12.24%(Δ=−0.93pp;quick 协议 −0.76pp 同向)——复现论文"MoE 无明显收益" ✓,`replication/moe_experiment.csv` | ✅ PASS(RNN-宽集偏离已留档) |
| G6 | s 曲线图 + 损失单测 | 闭式解与数值优化一致(1e-6) | `test_econ_execution.py` 27 项(closed-form/losscon/s_opt 单测);`test_trading_experiment.py` μ→∞ ⇒ z→1 收敛断言 ✓;**图 2 复刻(2026-08-09 补齐)**: `replication/fig2_s_curve.png`(mu_grid 7 档 + calibrated μ 叠线) | ✅ PASS |
| G7 | 表 2 + 命题 1 实证 | oracle>econ>stat 的 MEL 排序 | **经迁移路线成立**: oracle(100)>transfer_econ(1.8~22.8)>stat(-6.1~20.8) 全 7 档 μ ✓;**从零直训 nn_econ 失败**(OOS 全负,-39.8~-694.7)——差异报告 `G7_divergence_report.md` | ⚠️ PASS(带差异报告) |
| G8 | 微调前后对照 | MEL 升 / MSE 略变 签名;交易率-AUM 单调 | `d_mse_after_ft` 全正且随 μ 递增(0.003→1.145)=统计拟合恶化换经济收益签名 ✓;transfer_econ 全档 μ 优于 nn_stat ✓;表 3 AUM 越大绝对收益越低 ✓ | ✅ PASS |
| G9 | 图 5/7 + 表 3 同构表 | U 形;三改进源方向一致;因子增量普遍为正 | 图 5 U/驼形清晰,z=1 理想化基线夏普 **8.18**≈论文"~7"签名 ✓;表 3 AUM 单调 ✓;动物园**纯执行成本通道 18/18 全正** ✓;净增量 27.8% 为正=gross 通道盖过成本通道(μ=1e-9),两通道统计留档 `zoo_acceptance`;**三改进源分解已闭环(2026-08-09 补齐)**: ①信息集 all 14.21>tech 12.34>ma5 0 ✓ ②非线性 NN>OLS(tech 12.34>9.75;all 14.21>13.04)✓ ③econ 增量 transfer_econ 16.78>nn_stat 5.20 全 7 档 μ ✓,`replication/g9_decomposition.csv`;**C.1 paper-split 市值五分位**同步补齐(nano 11.6→mega 17.6 单调,`c1_size_quintile_paper.csv`) | ✅ PASS |

## 总判定

8/8 通过(其中 G7 一项带正式差异报告、G5/G9 各一处偏离留档——按 G10 规则"翻转须调查解释而非静默接受"处理完毕)。

## 附:数据修复链(影响以上数字的四个真 bug,全部已修)

1. z-score 训练窗 std 退化 → |z|>10³ 离群压平 OLS(截断 ±5 后 tech OLS 0.29→10.09)
2. fund 组按票时序 z → 时间趋势特征测试期钉死在截断轨道(NN -283%);改逐日横截面 z 后 +8.79
3. share_float 字符串股本+同日重复记录 → 市值静默置空(fund1_size 非零率 17.6%→59.3%→81%)
4. ADR/外国发行人双源无股本 → SIZE 第四级 ADV 代理 z 回填(附录 B 授权口径)
