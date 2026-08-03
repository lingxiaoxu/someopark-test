# RNN 生产化：设计 → 实测 → 已实施（E4）

日期: 2026-08-03（2026-08-01 初稿为纯设计；本版为实测改判 + 实施完成态）
状态: **代码已实施并测试通过；晋升(promote)待影子 AB 后人工决定**

---

## 0. 一次判断错误的完整记录（保留，勿删）

2026-08-02 上午我曾据全特征集 walk-forward 得出「RNN 垫底(0.139) < lgbm(0.182)，
E4 不该做」的结论。**该结论错误，已撤回。** 三处错误叠加：

1. **实验域错**：论文 Table 1 的 RNN 优势只在 **tech+fund1 窄集**上成立
   （仓库自身复现产物 `outputs/replication/table1_A_r2_eta.csv`：rnn 窄集 27.53 /
   全集 9.63）。喂满 114 列时 LSTM 被淹没——我却用全集去否定论文主张。
2. **序列上下文截断**（我写的 WF 框架的真 bug，只伤序列模型）：测试窗每票前
   `seq_len-1=9` 行的历史落在训练期内（严格过去、非前视），却未喂给 LSTM 而被
   零填充，**7.1% 的测试行被系统性惩罚**。已修（`walkforward._seq_context`）。
3. 据 1+2 的坏数据改判 E4。

教训：**当实测与既有文献冲突时，先怀疑自己的实验条件，再怀疑文献。**
用户坚持「尊重论文、可能是你做错了」，直接救回本项。

## 1. 复赛实测（12 窗 walk-forward，同面板同协议）

| 配置 | 平均 OOS R²(η) | 胜负 |
|---|---|---|
| **窄集 RNN（tech+fund1, 14 特征）** | **0.2947** | — |
| 全集 lgbm（现役 production） | 0.1773 | RNN 胜 11/12 窗，平均 +0.117 |
| 窄集 lgbm | 0.1428 | |
| 全集 RNN（错误设置） | 0.1388 | |

逐窗（w0→w11）：0.299 / 0.290 / 0.131 / 0.264 / 0.295 / 0.310 / 0.312 / 0.350 /
0.333 / 0.354 / 0.330 / 0.268。唯一劣于现役的是 w2（0.131 vs 0.149）。

## 2. 经济回测（2024-01→2026-07，644 交易日，真 OOS 预测）

G9 记账协议（`replication_trading.simulate`，二次冲击成本），交易需求由 oracle
信号外生给定 → 各档差异纯粹来自 v̂ 质量。μ 取各档最优（1e-9）：

| AUM | 档位 | 净年化 | 净夏普 | 成本拖累 |
|---|---|---|---|---|
| $1e8 | **rnn_narrow** | **62.19%** | **7.00** | 0.51% |
| | lgbm_full | 60.23% | 6.19 | 0.50% |
| | ma5 | 58.58% | 6.25 | 0.51% |
| $1e9 | **rnn_narrow** | **57.58%** | **6.48** | 5.13% |
| | lgbm_full | 55.73% | 5.73 | 5.00% |
| | ma5 | 53.98% | 5.76 | 5.11% |
| $1e10 | **rnn_narrow** | **11.44%** | **1.28** | 51.3% |
| | lgbm_full | 10.76% | 1.10 | 50.0% |
| | ma5 | 7.99% | 0.85 | 51.1% |

结论：R² 优势转化为 **$1B 上 +1.85pp 净年化 / +0.76 夏普**（vs 现役 lgbm）。
注意 $1e8/$1e9 上各模型档净夏普高于 oracle 档——oracle 知道真 v 后铺量更激进、
换手与成本方差更大；该现象属框架特性，不影响三个预测器之间的同口径比较。

## 3. 已实施的生产化（方案 C：numpy 权重前向）

### 3.1 `rnn_export.py`（新）
- `export_weights(model, path)`：训练进程导出 LSTM(W_ih/W_hh/b_ih) + 三层 dense
  → npz。断言 `b_hh` 全零（训练侧冻结的单 bias 语义）。
- `RNNWeights`：**纯 numpy** 前向（门序 i,f,g,o 与 torch 对齐），
  `predict_windows` / `predict_panel`（块内左侧零填充，与 `build_windows` 同语义）。
- 实测 torch vs numpy **max|diff| = 1.5e-8**。

### 3.2 `prod_model_rnn.py`（新）
- `freeze(panel, asof, seeds=3)`：窄集全量训练 → 每 seed 一份 weights npz +
  `per_ticker.parquet`（冻结 mu/sd、z_next、ma5v_next、active、fund1 末值）+
  **`seq_tail.npz`**（每票末 9 日特征窗 + 日戳）+ meta。
- `serve(art, target_date, update_state=False)`：窗 = seq_tail(9) ⊕ 当日行 →
  numpy 前向 → **多 seed 均值** → `pred_v = ma5v_next + η̂`；schema 与现役
  lgbm/ma5 工件一致（refresh 可直接分发）。
- **有状态 + 三道纪律**：日更滚动 seq_tail 后原子回写；同日重复调用幂等
  （日戳判定）；**日期断档直接抛错拒绝出数**，绝不用错位窗静默预测。

### 3.3 测试（7 项，全过；VP 全套 146 passed）
`tests/test_rnn_export.py`
- numpy ≡ torch 逐行对拍
- 零填充语义与手工窗逐位一致 + 元信息
- **红线（功能性）**：子进程中用 meta_path 钩子封死 `import torch`，服务路径仍
  跑通且数值一致 —— 「服务端零 torch」是被测试守住的，不是口头承诺

`tests/test_prod_model_rnn.py`
- 工件完整性 + seq_tail ≡ 面板末 9 行
- serve 窗拼接与 seed 平均：与手工复算逐位一致
- **断档必须抛错**
- 同日重复 serve 幂等（窗不二次滚动）

### 3.4 意外红利
窄集只需 **14 个特征（tech 8 + fund1 6）**，不需要 fund2/cal/earn → serve 路径
比现役 lgbm **更简单**（无日历/财报 shell 组装、无 earnings loader 依赖）。

## 4. 尚未做（需人工决策，不自动执行）

1. **真面板 freeze 一次**（`panel prod_v6f32n` + 当前 asof），产出候选工件
2. **影子 AB ≥5 交易日**（rnn 候选 vs 现役 lgbm，同 P4 分组裁判纪律）
3. **人工 promote**（registry.production 指针切换）—— 永远人工，不自动
4. 全集 RNN 带上下文修复的重跑（纯验证：量化 §0-2 那个 bug 吃掉多少 R²）
5. `refresh` 分发端识别 `kind=learned.rnn`（现按 kind 分支，加一路）

## 5. 训练不确定性纪律（保留自初稿）

torch 训练即使固定 seed 仍有跨设备非确定性。工件记录 seeds/weight_files；
OOS 评审与 promote 一律用 **多 seed 均值**，杜绝单 seed 幸运票。freeze 默认
seeds=3，serve 端对各 seed 预测取均值（已实施）。
