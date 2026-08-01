# RNN/NN 生产化设计（E4 设计节 — 只设计，不实施）

日期: 2026-08-01 · 依据: Extension Plan E4 + 服务红线（service 读取端不 import torch）
· 状态: **设计稿，待用户批准后实施**。实施前需同面板 OOS 复赛证明 RNN 确实赢 lgbm。

---

## 0. 问题定义

Table 1 证据: RNN(fund1 窄集) OOS R² 19.96% > lgbm 16.34%，但两者设置不同，需同面板复赛
（E3 的全模型 WF 总表正在跑，bhodn40h1，出结果后才有推进依据）。若复赛确认 RNN 优势，
生产化面临两道硬约束：

1. **torch 红线**: `service.py` 明文规定 torch 延迟到重方法内 import；refresh/serve 读取路径
   （17:37 cron → conductor）绝不能引入 torch 启动开销与依赖脆弱性。
2. **序列服务化**: PaperRNN 是 many-to-one、seq_len=10 —— serve 时每票需要"截至 T-1 的
   连续 10 个交易日特征窗"，而现有 lgbm serve 是单行特征。这是比红线更大的工程差异。

## 1. 网络体量核查（决定方案选型）

`models/deep.py` PaperRNN 实测结构：

```
_SingleBiasLSTM(n_pred → 32, b_hh 冻结为 0)   # 单层, many-to-one 取末步隐状态
→ Linear(32,16) ReLU → Linear(16,8) ReLU → Linear(8,1)
```

参数量 = `(n_pred+33)*128 + 33*16 + 17*8 + 9`。fund1 窄集 n_pred≈15 → **~6.8k 参数**。
这是一张微型网——毫秒级 numpy 前向完全可行。选型据此展开。

## 2. 推理方案三选一

### 方案 C（推荐）: numpy 权重导出 + 前向复刻

- **freeze 时**（重进程，可 import torch）: 训练完成后把权重导出为
  `registry/<model_id>/weights.npz`（W_ih/W_hh/b_ih + 三层 dense 的 W/b，共 8 个数组）。
- **serve 时**（轻进程，零 torch）: ~40 行 numpy 实现 LSTM 单层前向
  （标准公式 i,f,g,o = split(x@W_ihᵀ + h@W_hhᵀ + b_ih)；b_hh 恒 0 与训练侧语义一致）
  + 3 层 dense。依赖 = numpy（已有），**零新增依赖**。
- **验收**: 逐票逐日与 torch 前向对拍，容差 ≤1e-5（float32 累加差异），全宇宙 100% 通过
  才算导出正确。对拍脚本进 tests/，freeze 流程内置自检（导出后当场抽 100 票对拍）。

### 方案 B（备选）: ONNX 导出 + onnxruntime

- freeze 时 `torch.onnx.export`；serve 端 `onnxruntime` 推理。
- 缺点: **新增 onnxruntime 依赖**（违背"零重依赖"精神，需用户单独批准）；
  LSTM 的 onnx 导出对 batch_first/冻结 bias 的处理有版本坑。
- 仅当未来模型升级到 numpy 复刻不经济的体量（Transformer 级）才启用。

### 方案 A（否决）: serve 端延迟 import torch

- 技术上可行（重方法内 import），但 cron 环境 torch 加载 ~3-8s、MPS/CPU 设备选择
  不确定性、conda 环境升级脆弱性——对一张 6.8k 参数的网毫无必要。否决。

## 3. 序列特征服务化（真正的工程主体）

### 3.1 冻结物扩展

`prod_model.freeze` 在现有 per_ticker 冻结统计之外，增存：

```
per_ticker[tkr]["seq_tail"]: 末 9 个交易日的 z 化特征行 (9 × n_pred, float32)
per_ticker[tkr]["seq_dates"]: 对应日期戳（重放对齐用）
```

窗口语义与训练面板逐位一致：训练侧 `build_windows` 对不足 seq_len 的头部做左侧零填充
（`W[.., seq_len-len:, :] = seg`），serve 侧同款——新票/停牌复牌票头部零填充，不降级不跳过。

### 3.2 serve 日更流程（fast path）

行 T 预测 = 网络输入窗 `[T-10+1 … T]` 的 z 化特征（行 T 特征本身按面板口径只用 ≤T-1 信息，
与 lgbm serve 现行口径一致，无新增前视）：

1. 用冻结统计对当日新特征行做 z 化（与 lgbm serve 同一函数，不重写）；
2. `窗 = concat(seq_tail, 当日行)[-10:]` → numpy 前向 → 预测；
3. **seq_tail 滚动更新**并原子回写 registry（当日行入尾、最老行出）——这是与 lgbm
   serve 的关键差异: RNN serve 是**有状态的**。
4. 状态防护: seq_tail 带 `last_date` 戳，重复运行同日 refresh 幂等（同日不二次入窗）；
   若发现日期断档（cron 漏跑），从 raw 尾窗重建整窗（general path 兜底），大声 log。

### 3.3 general path（重放/断档兜底）

与 lgbm 一致走 raw 尾窗（330d + sl.adjust 复权）重算特征，再按 3.1 语义现场构窗。
A14 首行教训已内化: 重放起点前需回补 seq_len-1+特征暖机天数，缺一天则该票当日回 ma5 兜底
并计数报警，不静默。

## 4. NN（PaperNN）顺带生产化

PaperNN 是扁平单行输入（32→16→8→1，无序列），生产化 = 方案 C 的 dense-only 子集，
**无 3.2 的状态机**。若复赛中 nn 赢面大于 rnn，优先落 nn（工程量约为 rnn 的 1/3）。

## 5. 训练不确定性纪律

torch 训练即使 seed_everything 也存在跨设备(MPS/CPU)非确定性。冻结物中记录
`train_device/torch_version/seed/loss_history`；OOS 复赛与 promote 评审用 seeds≥3 的
中位数成绩，杜绝单 seed 幸运票。

## 6. 实施顺序（获批后）

1. 复赛证据关（依赖 bhodn40h1 总表）: 同面板 rnn vs lgbm vs nn，seeds=3，分层分组
2. `prod_model.freeze` 多模型分支 + weights.npz 导出 + 对拍自检
3. serve numpy 前向 + seq_tail 状态机 + 幂等/断档测试
4. 影子 AB（与 lgbm 促升同款纪律，≥5 交易日）→ 人工 promote
5. 全程测试进 tmp；registry 结构向后兼容（lgbm 工件字段不动，新增字段并存）

## 7. 明确不做

- 不改训练协议（论文精确系保持 Adam 默认/1024/50ep）
- 不引 onnxruntime/torchscript（除非用户批方案 B）
- 不在 conductor/cron 中引 torch
- 不自动 promote —— promote 永远人工
