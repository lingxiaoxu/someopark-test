"""rnn_export — PaperRNN 权重导出 + 纯 numpy 前向(E4 方案 C 实施)。

服务端红线: 读取路径不 import torch。本模块的 `RNNWeights.predict` 只依赖
numpy,权重来自训练期(重进程)导出的 npz。

数学契约(与 models/deep.py 的 _RNNCore 逐位对应):
  h_t, c_t = LSTM(x_t; W_ih, W_hh, b_ih, b_hh≡0)      # 单层, hidden=32
  y = W3·relu(W2·relu(W1·h_T + b1) + b2) + b3          # 32→16→8→1
torch LSTM 门序为 i,f,g,o(chunk 4);b_hh 在训练侧冻结为 0,导出时不写。
零填充语义与训练一致: 不足 seq_len 的历史在窗前部补 0。

用法:
    export_weights(paper_rnn, path)          # 训练进程(有 torch)
    w = RNNWeights.load(path); w.predict(X)  # 服务进程(零 torch)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

SEQ_LEN_DEFAULT = 10


def export_weights(model, path: str | Path) -> Path:
    """PaperRNN → npz(仅训练进程调用;此处才 import torch)。"""
    path = Path(path)
    net = model.net
    lstm = net.rnn.lstm
    d = {
        "W_ih": lstm.weight_ih_l0.detach().cpu().numpy(),
        "W_hh": lstm.weight_hh_l0.detach().cpu().numpy(),
        "b_ih": lstm.bias_ih_l0.detach().cpu().numpy(),
        "W2": net.l2.weight.detach().cpu().numpy(),
        "b2": net.l2.bias.detach().cpu().numpy(),
        "W3": net.l3.weight.detach().cpu().numpy(),
        "b3": net.l3.bias.detach().cpu().numpy(),
        "W4": net.out.weight.detach().cpu().numpy(),
        "b4": net.out.bias.detach().cpu().numpy(),
        "seq_len": np.array([getattr(model, "seq_len", SEQ_LEN_DEFAULT)]),
        "n_pred": np.array([model.n_pred]),
    }
    # b_hh 训练侧冻结为 0(单 bias 语义);断言导出前提成立
    b_hh = lstm.bias_hh_l0.detach().cpu().numpy()
    assert not b_hh.any(), "b_hh 非零 — 导出契约(单 bias)不成立"
    np.savez(path, **d)
    return path


def _sigmoid(x: np.ndarray) -> np.ndarray:
    """数值稳定版(2026-08-03 实盘工件触发 exp 溢出 warning)。

    朴素式在 x 很负时 exp(-x) 溢出;虽然极限值仍对(1/inf→0),但生产日志里
    不该有 RuntimeWarning。正负分支各用不溢出的等价式,数值与朴素式在
    非溢出区逐位相同。
    """
    out = np.empty_like(x, dtype=np.float32)
    pos = x >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-x[pos]))
    e = np.exp(x[~pos])
    out[~pos] = e / (1.0 + e)
    return out


class RNNWeights:
    """纯 numpy 推理(零 torch)。"""

    def __init__(self, d: dict):
        self.W_ih = d["W_ih"].astype(np.float32)      # (4H, F)
        self.W_hh = d["W_hh"].astype(np.float32)      # (4H, H)
        self.b_ih = d["b_ih"].astype(np.float32)      # (4H,)
        self.W2, self.b2 = d["W2"].astype(np.float32), d["b2"].astype(np.float32)
        self.W3, self.b3 = d["W3"].astype(np.float32), d["b3"].astype(np.float32)
        self.W4, self.b4 = d["W4"].astype(np.float32), d["b4"].astype(np.float32)
        self.seq_len = int(np.asarray(d["seq_len"]).ravel()[0])
        self.n_pred = int(np.asarray(d["n_pred"]).ravel()[0])
        self.H = self.W_hh.shape[1]

    @classmethod
    def load(cls, path: str | Path) -> "RNNWeights":
        with np.load(str(path)) as z:
            return cls({k: z[k] for k in z.files})

    def _lstm_last(self, X: np.ndarray) -> np.ndarray:
        """X: (B, T, F) → 末步 h: (B, H)。torch 门序 i,f,g,o。"""
        B, T, F = X.shape
        H = self.H
        h = np.zeros((B, H), dtype=np.float32)
        c = np.zeros((B, H), dtype=np.float32)
        Wih_T, Whh_T = self.W_ih.T, self.W_hh.T
        for t in range(T):
            g = X[:, t, :] @ Wih_T + self.b_ih + h @ Whh_T
            i, f, gg, o = np.split(g, 4, axis=1)
            i, f, o = _sigmoid(i), _sigmoid(f), _sigmoid(o)
            gg = np.tanh(gg)
            c = f * c + i * gg
            h = o * np.tanh(c)
        return h

    def predict_windows(self, X: np.ndarray) -> np.ndarray:
        """X: (B, T, F) → (B,) 预测。"""
        X = np.ascontiguousarray(X, dtype=np.float32)
        h = self._lstm_last(X)
        h = np.maximum(h @ self.W2.T + self.b2, 0.0)
        h = np.maximum(h @ self.W3.T + self.b3, 0.0)
        return (h @ self.W4.T + self.b4).ravel()

    def predict_panel(self, feats: np.ndarray, tickers: np.ndarray,
                      batch: int = 4096) -> np.ndarray:
        """按票分块的行序面板(同票按日连续,与训练侧 _sort_panel 同序)→ 逐行预测。

        窗口构造与 models/deep.build_windows 逐位一致(块内左侧零填充)。
        """
        feats = np.ascontiguousarray(feats, dtype=np.float32)
        N, F = feats.shape
        s = self.seq_len
        starts = np.zeros(N, dtype=np.int64)
        st = 0
        for i in range(1, N):
            if tickers[i] != tickers[i - 1]:
                st = i
            starts[i] = st
        out = np.empty(N, dtype=np.float32)
        for lo in range(0, N, batch):
            hi = min(lo + batch, N)
            W = np.zeros((hi - lo, s, F), dtype=np.float32)
            for k, i in enumerate(range(lo, hi)):
                a = max(starts[i], i - s + 1)
                W[k, s - (i - a + 1):, :] = feats[a:i + 1]
            out[lo:hi] = self.predict_windows(W)
        return out
