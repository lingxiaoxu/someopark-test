# P2 十二靶点修复对照报告（"当时做得不好"逐项复盘）

生成: 2026-07-27 · 判据: plan §九 P2 表格 · 每靶点给出实现位置与证据

| 靶点 | 修复内容 | 实现/证据 | 状态 |
|---|---|---|---|
| ① 硬件 | MPS/torch 加速全深模型 | deep.py pick_device(MPS 优先);NN 50ep 全谱 ~800s/种子实测;TFT 超参空间在 config | ✅ |
| ② 目标难 | 与⑫合并 target_bakeoff | evaluation/target_bakeoff.py 实跑(v5 面板)四目标对比 | ✅ |
| ③ 集群过拟合 | 簇最小样本/正则/早停 | deep.py LSTMAutoencoder+ClusteredLSTM(min_cluster 样本闸、dropout、早停);test_models_deep 过 | ✅ |
| ④ 特征共线 | 正交化 + VIF + PCA 对照 | factor_proxy.orthogonalize_to_industry + vif_table/vif_prune(statsmodels 1e-6 对拍单测);PCR=PCA 对照组(legacy 表) | ✅ |
| ⑤ 成本函数 | λ/μ 实测校准,六 profile | calibrate_mu.py 实跑 → outputs/registry/mu_calibration.json:aiss_rebalance μ=2.82e-3(真实 alpha 衰减曲线,25 事件);pairs μ=1e-6(1032 行回归无稳健衰减,冷启动显式标注);λ 47 笔成交样本<50 显式降级论文先验;urgent=∞ | ✅(标注制) |
| ⑥ TFT | 完整实现+attention 归因 | deep.py TFT(变量选择网络+多头注意力+分位损失);attention 输出接 metrics 归因通道 | ✅ |
| ⑦ 文本 | 【用户令推迟】 | text_features.py 占位+推迟声明(§1.1) | ⏸ 按令推迟 |
| ⑧ 时序回灌 | ARIMA/SARIMA 正式基线 | baselines.py ARIMAPerTicker/SARIMAPerTicker 入 legacy 成绩表;evaluation/acf.py 显著 lag 报告 | ✅ |
| ⑨ 前视 | lookahead_audit | evaluation/lookahead_audit.py 实跑 exit=0(v5);shift_volume_columns_and_drop_last 单测 | ✅ |
| ⑩ 分层 | walkforward 行业/市值分层 | evaluation/walkforward.py(12 单测×2 轮);真面板冒烟: 行业 spread≈13pp(SIC0 23.3% vs UNK 10.1%),市值 D4-D6 8-10% vs D8-D10 ~21% | ✅ |
| ⑪ 归因 | SHAP+梯度+attention 三通道 | metrics.py SHAP(树系)、ml.py NN gradient_attribution、TFT attention | ✅ |
| ⑫ 目标选型 | bakeoff 正式结论进 config | target_bakeoff 实跑;config models.target 默认 eta(论文口径),bakeoff 数据留档 | ✅ |

## 综合成绩对照(vs 旧作)

- 旧作最优: NN2 0.815(log_volume 目标,旧 500 票宇宙)——量纲不可直接比
- 新作(η 目标,R3K 代理 4,934 票,paper 协议): 全谱 NN 14.21% / RNN(fund1)19.96%
  ——η 是"减掉 ma5 后的残差",基线即 0;对应 v 总方差口径 95.5%(旧 log_volume
  口径的同类量级)。定性排序验收(PLS 最强线性/NN 全局领先)以 legacy_scores.csv
  终表为准(light 层已出,deep 层今晚生产窗后补齐)。
