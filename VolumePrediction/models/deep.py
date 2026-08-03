"""
models/deep — Tier4 深度模型(Plan B §四/G4/58 步 D·F 组/I57)
============================================================
两个体系,训练协议严格分开(计划 G4 与弱点③):

【论文精确系(G4,不调参)】
- PaperNN : 3 隐层全连接 32-16-8,ReLU,线性输出
             param_count == (n_pred+1)*32 + (32+1)*16 + (16+1)*8 + (8+1)
- PaperRNN: 第一隐层换成 32 隐状态+32 细胞状态的 LSTM(many-to-one,序列长 10),
             其余 16-8 全连接保持
             param_count == (n_pred+32+1)*32*4 + (32+1)*16 + (16+1)*8 + (8+1)
  * torch LSTM 默认双 bias(b_ih+b_hh);二者在前向中恒相加,双 bias 与单 bias
    数学等价。本实现把 b_hh 固定为 0 且 requires_grad=False —— 可训练参数量
    与论文单 bias 公式**逐一相等**,前向语义与标准 LSTM 完全一致(见 param_count
    docstring 的两套核算)。
- 训练协议(论文附录 A.1): Adam 默认参 / batch=1024 / 50 epochs / 无早停 /
  无 dropout / 缺失滞后零填充 / 接受 seed;5 种子平均由调用侧(evaluation)负责。

【旧作 58 步系(D30-D33, F42-F43;可用早停/dropout——与论文系协议分开)】
- LSTMAutoencoder(latent 默认 20,弱点③修复可配 8)
- cluster_latent(): KMeans / DBSCAN 于 latent 空间
- SingleLSTM(win10) 预测器
- ClusteredLSTM: 分簇独立 LSTM;簇最小样本≥cluster_min_rows(默认 5000,弱点③),
  小簇并入 residual 簇并在 .merge_log 留痕

【TFT(I57 必做——自研完整版】
路线选择: pytorch-forecasting 在实施时点不可用(安装未就绪),且其 TimeSeriesDataSet
封装与本包面板契约(MultiIndex DataFrame)之间会引入 lightning 运行时依赖。
按实施纪律"完整实现不许简化",自研 **完整** Temporal Fusion Transformer
(Lim et al., 2021, arXiv:1912.09363),组件一个不少:
  静态协变量编码器(4 个上下文向量 c_s,c_e,c_c,c_h) / 变量选择网络 VSN(静态+历史+未来)
  / GRN+GLU 门控残差 / seq2seq LSTM(静态上下文初始化) / 静态富集 GRN
  / 可解释多头注意力(共享 V,按头平均) / 位置前馈 GRN / 各级门控跳连
  / 分位数输出(默认 0.1/0.5/0.9,QuantileLoss) / attention 权重导出(弱点⑪)。
静态输入=ticker 嵌入;未来已知输入=日历/财报旗标(cal_/earn_ 前缀,t+1 已知)。

面板契约(DEV_CONTRACTS): X 为 MultiIndex(date,ticker) 的 DataFrame,fit/predict
在内部按 ticker 分组构窗(不足历史零填充,与论文 A.1 一致);predict 输出与
X.index 对齐的 Series。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

__all__ = [
    "PaperNN", "PaperRNN",
    "LSTMAutoencoder", "cluster_latent", "SingleLSTM", "ClusteredLSTM",
    "TFT", "QuantileLoss",
    "build_windows", "pick_device", "seed_everything",
]


# ═══════════════════════════════════════════════════════════════════
# 公共工具
# ═══════════════════════════════════════════════════════════════════

def pick_device(device: Optional[str] = None) -> torch.device:
    """MPS 可用则 MPS,否则 CPU;显式传入优先。"""
    if device is not None:
        return torch.device(device)
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def seed_everything(seed: int) -> None:
    np.random.seed(seed)
    torch.manual_seed(seed)


def _sort_panel(X: pd.DataFrame, y: Optional[pd.Series] = None):
    """按 (ticker, date) 排序以便逐票构窗;返回排序对象与\"还原到原 index 顺序\"的定位器。"""
    if not isinstance(X.index, pd.MultiIndex) or X.index.nlevels != 2:
        raise ValueError("panel X must have MultiIndex (date, ticker)")
    order = np.lexsort([X.index.get_level_values(0).values,
                        X.index.get_level_values(1).values])  # ticker 主序, date 次序
    Xs = X.iloc[order]
    ys = y.iloc[order] if y is not None else None
    return Xs, ys, order


def build_windows(X: pd.DataFrame, seq_len: int = 10) -> Tuple[np.ndarray, np.ndarray]:
    """
    many-to-one 窗口构造(论文 §3.1): 对每个 (i,t) 取 {X_{i,t-seq_len+1..t}}。
    历史不足处**零填充**(论文 A.1 缺失滞后零填充的序列化推广)。

    返回 (W, order): W shape=(N, seq_len, F) 与排序后的行一一对应;
    order 为 lexsort 定位器(还原原 index 用)。
    """
    Xs, _, order = _sort_panel(X)
    vals = Xs.values.astype(np.float32)
    tickers = Xs.index.get_level_values(1).values
    N, F = vals.shape
    W = np.zeros((N, seq_len, F), dtype=np.float32)
    start = 0
    while start < N:
        end = start
        t = tickers[start]
        while end < N and tickers[end] == t:
            end += 1
        block = vals[start:end]                     # 单票按日连续
        for j in range(end - start):
            lo = max(0, j - seq_len + 1)
            seg = block[lo:j + 1]
            W[start + j, seq_len - len(seg):, :] = seg
        start = end
    return W, order


def _block_starts(tickers: np.ndarray) -> np.ndarray:
    """每行所属单票连续块的起始行号(与 build_windows 的块扫描同语义)。"""
    N = len(tickers)
    starts = np.zeros(N, dtype=np.int64)
    start = 0
    for i in range(1, N):
        if tickers[i] != tickers[i - 1]:
            start = i
        starts[i] = start
    return starts


class _LazyWindowDataset(torch.utils.data.Dataset):
    """按需构窗 Dataset — 与 build_windows 产出的窗口逐位相同,但不物化
    (N, seq_len, F) 大数组(2026-08-02: 190万行×10×114 ≈ 9GB 物化被 jetsam
    连杀,惰性化后常驻只有 (N,F))。零填充语义与 build_windows 完全一致。"""

    def __init__(self, vals: np.ndarray, tickers: np.ndarray, seq_len: int,
                 y: Optional[np.ndarray] = None):
        self.vals = np.ascontiguousarray(vals, dtype=np.float32)
        self.starts = _block_starts(tickers)
        self.seq_len = seq_len
        self.y = None if y is None else np.asarray(y, dtype=np.float32)

    def __len__(self) -> int:
        return len(self.vals)

    def __getitem__(self, i: int):
        s = self.seq_len
        lo = max(self.starts[i], i - s + 1)
        w = np.zeros((s, self.vals.shape[1]), dtype=np.float32)
        w[s - (i - lo + 1):, :] = self.vals[lo:i + 1]
        t = torch.from_numpy(w)
        if self.y is None:
            return (t,)
        return t, torch.tensor(self.y[i], dtype=torch.float32)

    def get_batch(self, idxs: np.ndarray) -> torch.Tensor:
        """批量构窗(批内 numpy 向量化;避免 1 亿次逐样本 __getitem__ 开销)。"""
        s = self.seq_len
        B = len(idxs)
        w = np.zeros((B, s, self.vals.shape[1]), dtype=np.float32)
        for k, i in enumerate(idxs):
            lo = max(self.starts[i], i - s + 1)
            w[k, s - (i - lo + 1):, :] = self.vals[lo:i + 1]
        return torch.from_numpy(w)


class _TorchRegressorMixin:
    """fit/predict 的共享训练循环(论文协议: Adam 默认/1024/50ep/无早停/无 dropout)。"""

    batch_size = 1024
    epochs = 50

    def _train_loop(self, ds: TensorDataset, seed: int, device: torch.device,
                    epochs: Optional[int] = None) -> List[float]:
        seed_everything(seed)
        self.net.to(device)
        opt = torch.optim.Adam(self.net.parameters())      # 默认学习率与其他默认参
        lossf = nn.MSELoss()
        loader = DataLoader(ds, batch_size=self.batch_size, shuffle=True,
                            generator=torch.Generator().manual_seed(seed))
        history = []
        self.net.train()
        for _ in range(epochs or self.epochs):
            tot, nb = 0.0, 0
            for xb, yb in loader:
                xb, yb = xb.to(device), yb.to(device)
                opt.zero_grad()
                pred = self.net(xb).squeeze(-1)
                loss = lossf(pred, yb)
                loss.backward()
                opt.step()
                tot += float(loss.detach().cpu())
                nb += 1
            history.append(tot / max(nb, 1))
        return history


# ═══════════════════════════════════════════════════════════════════
# 论文精确系(G4)
# ═══════════════════════════════════════════════════════════════════

class _NNCore(nn.Module):
    def __init__(self, n_pred: int):
        super().__init__()
        self.l1 = nn.Linear(n_pred, 32)
        self.l2 = nn.Linear(32, 16)
        self.l3 = nn.Linear(16, 8)
        self.out = nn.Linear(8, 1)
        self.act = nn.ReLU()

    def forward(self, x):
        h = self.act(self.l1(x))
        h = self.act(self.l2(h))
        h = self.act(self.l3(h))
        return self.out(h)


class PaperNN(_TorchRegressorMixin):
    """论文 nn(G4)。X 为扁平面板行(不需要序列)。"""

    name = "paper_nn"

    def __init__(self, n_pred: int, seed: int = 0, device: Optional[str] = None):
        self.n_pred = n_pred
        self.seed = seed
        self.device = pick_device(device)
        seed_everything(seed)
        self.net = _NNCore(n_pred)
        self.train_history: List[float] = []

    def param_count(self) -> int:
        """论文公式 (n_pred+1)*32+(32+1)*16+(16+1)*8+(8+1);
        torch Linear(in,out) 恰有 (in+1)*out 参数 → 两套核算恒等。"""
        formula = (self.n_pred + 1) * 32 + 33 * 16 + 17 * 8 + 9
        actual = sum(p.numel() for p in self.net.parameters() if p.requires_grad)
        assert actual == formula, (actual, formula)
        return formula

    def fit(self, X: pd.DataFrame, y: pd.Series, epochs: Optional[int] = None) -> "PaperNN":
        Xs, ys, _ = _sort_panel(X, y)
        ds = TensorDataset(torch.tensor(Xs.values, dtype=torch.float32),
                           torch.tensor(ys.values, dtype=torch.float32))
        self.train_history = self._train_loop(ds, self.seed, self.device, epochs)
        return self

    @torch.no_grad()
    def predict(self, X: pd.DataFrame) -> pd.Series:
        self.net.eval()
        v = torch.tensor(X.values, dtype=torch.float32).to(self.device)
        out = self.net(v).squeeze(-1).cpu().numpy()
        return pd.Series(out, index=X.index, name=self.name)


class _SingleBiasLSTM(nn.Module):
    """标准 nn.LSTM,但 b_hh 固定 0 不训练 → 可训练参数=单 bias 公式,前向语义不变。"""

    def __init__(self, input_size: int, hidden: int = 32):
        super().__init__()
        self.lstm = nn.LSTM(input_size, hidden, batch_first=True)
        with torch.no_grad():
            self.lstm.bias_hh_l0.zero_()
        self.lstm.bias_hh_l0.requires_grad_(False)

    def forward(self, x):                      # x: (B, T, F)
        out, _ = self.lstm(x)
        return out[:, -1, :]                   # many-to-one: 末步隐状态


class _RNNCore(nn.Module):
    def __init__(self, n_pred: int):
        super().__init__()
        self.rnn = _SingleBiasLSTM(n_pred, 32)
        self.l2 = nn.Linear(32, 16)
        self.l3 = nn.Linear(16, 8)
        self.out = nn.Linear(8, 1)
        self.act = nn.ReLU()

    def forward(self, x):
        h = self.rnn(x)
        h = self.act(self.l2(h))
        h = self.act(self.l3(h))
        return self.out(h)


class PaperRNN(_TorchRegressorMixin):
    """论文 rnn(G4): lstm(32) 换第一隐层,many-to-one 序列长 10。"""

    name = "paper_rnn"
    seq_len = 10

    def __init__(self, n_pred: int, seed: int = 0, device: Optional[str] = None):
        self.n_pred = n_pred
        self.seed = seed
        self.device = pick_device(device)
        seed_everything(seed)
        self.net = _RNNCore(n_pred)
        self.train_history: List[float] = []

    def param_count(self) -> int:
        """论文公式 (n_pred+32+1)*32*4 + (32+1)*16 + (16+1)*8 + (8+1)。
        torch 核算: W_ih(4H×I)+W_hh(4H×H)+b_ih(4H) [b_hh 冻结为0不计]
        = 4*32*(n_pred+32+1) —— 与公式逐一相等;双 bias 在前向中恒为
        b_ih+b_hh,冻 0 后数学等价于单 bias LSTM。"""
        formula = (self.n_pred + 32 + 1) * 32 * 4 + 33 * 16 + 17 * 8 + 9
        actual = sum(p.numel() for p in self.net.parameters() if p.requires_grad)
        assert actual == formula, (actual, formula)
        return formula

    def fit(self, X: pd.DataFrame, y: pd.Series, epochs: Optional[int] = None) -> "PaperRNN":
        # 惰性构窗(2026-08-02): 不物化 (N,10,F) 大数组(190万行 ≈ 9GB 曾被
        # jetsam 连杀)。训练轨迹与旧 build_windows+TensorDataset+DataLoader
        # 路径逐位一致: 手动批循环逐位复刻 DataLoader 语义(同 seed 的
        # generator randperm 跨 epoch 状态 + 同批切分),窗口内容同 build_windows
        # (parity 测试见 tests)。
        Xs, ys, _ = _sort_panel(X, y)
        ds = _LazyWindowDataset(Xs.values, Xs.index.get_level_values(1).values,
                                self.seq_len, ys.values)
        seed_everything(self.seed)
        self.net.to(self.device)
        opt = torch.optim.Adam(self.net.parameters())
        lossf = nn.MSELoss()
        gen = torch.Generator().manual_seed(self.seed)   # 同 DataLoader generator
        yt = torch.from_numpy(ds.y)
        N = len(ds)
        history = []
        self.net.train()
        for _ in range(epochs or self.epochs):
            # 逐位复刻 DataLoader 每 epoch 的 RNG 消耗序(4-epoch 实测 ALL MATCH):
            # ① 迭代器 base_seed(random_) ② RandomSampler 主 randperm
            # ③ sampler 尾部第二次 randperm(切片[:0]丢弃但消耗 RNG)
            torch.empty((), dtype=torch.int64).random_(generator=gen)
            perm = torch.randperm(N, generator=gen)
            torch.randperm(N, generator=gen)
            tot, nb = 0.0, 0
            for bi in perm.split(self.batch_size):       # = drop_last=False 批切分
                xb = ds.get_batch(bi.numpy()).to(self.device)
                yb = yt[bi].to(self.device)
                opt.zero_grad()
                pred = self.net(xb).squeeze(-1)
                loss = lossf(pred, yb)
                loss.backward()
                opt.step()
                tot += float(loss.detach().cpu())
                nb += 1
            history.append(tot / max(nb, 1))
        self.train_history = history
        return self

    @torch.no_grad()
    def predict(self, X: pd.DataFrame) -> pd.Series:
        self.net.eval()
        Xs, _, order = _sort_panel(X)
        ds = _LazyWindowDataset(Xs.values, Xs.index.get_level_values(1).values,
                                self.seq_len)
        out = np.empty(len(X), dtype=np.float32)
        dev = self.device
        N = len(ds)
        for i in range(0, N, 4096):
            xb = ds.get_batch(np.arange(i, min(i + 4096, N))).to(dev)
            out[i:i + 4096] = self.net(xb).squeeze(-1).cpu().numpy()
        inv = np.empty_like(order)
        inv[order] = np.arange(len(order))
        return pd.Series(out[inv], index=X.index, name=self.name)


# ═══════════════════════════════════════════════════════════════════
# 旧作 58 步系(D30-D33 / F42-F43)——允许早停/dropout(与论文系分开)
# ═══════════════════════════════════════════════════════════════════

class _AE(nn.Module):
    def __init__(self, seq_len: int, n_feat: int, latent: int, dropout: float):
        super().__init__()
        self.enc = nn.LSTM(n_feat, latent, batch_first=True)
        self.drop = nn.Dropout(dropout)
        self.dec = nn.LSTM(latent, latent, batch_first=True)
        self.head = nn.Linear(latent, n_feat)
        self.seq_len = seq_len

    def forward(self, x):
        _, (h, _) = self.enc(x)                      # h: (1,B,latent)
        z = self.drop(h[-1])
        rep = z.unsqueeze(1).repeat(1, self.seq_len, 1)
        out, _ = self.dec(rep)
        return self.head(out), z


class LSTMAutoencoder:
    """D30: 序列自编码(latent 默认 20;弱点③修复常用 8)+早停+dropout。"""

    name = "lstm_ae"

    def __init__(self, n_feat: int, seq_len: int = 10, latent: int = 20,
                 dropout: float = 0.2, patience: int = 5, max_epochs: int = 100,
                 seed: int = 0, device: Optional[str] = None):
        self.seq_len, self.latent, self.patience = seq_len, latent, patience
        self.max_epochs = max_epochs
        self.seed = seed
        self.device = pick_device(device)
        seed_everything(seed)
        self.net = _AE(seq_len, n_feat, latent, dropout)

    def fit(self, X: pd.DataFrame, val_frac: float = 0.1) -> "LSTMAutoencoder":
        W, _ = build_windows(X, self.seq_len)
        n_val = max(1, int(len(W) * val_frac))
        rng = np.random.default_rng(self.seed)
        idx = rng.permutation(len(W))
        Wtr, Wva = W[idx[n_val:]], W[idx[:n_val]]
        dev = self.device
        self.net.to(dev)
        opt = torch.optim.Adam(self.net.parameters())
        lossf = nn.MSELoss()
        best, best_state, bad = math.inf, None, 0
        loader = DataLoader(TensorDataset(torch.tensor(Wtr)), batch_size=1024,
                            shuffle=True, generator=torch.Generator().manual_seed(self.seed))
        va_t = torch.tensor(Wva).to(dev)
        for _ in range(self.max_epochs):
            self.net.train()
            for (xb,) in loader:
                xb = xb.to(dev)
                opt.zero_grad()
                rec, _ = self.net(xb)
                loss = lossf(rec, xb)
                loss.backward()
                opt.step()
            self.net.eval()
            with torch.no_grad():
                va = float(lossf(self.net(va_t)[0], va_t).cpu())
            if va < best - 1e-6:
                best, bad = va, 0
                best_state = {k: v.detach().clone() for k, v in self.net.state_dict().items()}
            else:
                bad += 1
                if bad >= self.patience:
                    break
        if best_state:
            self.net.load_state_dict(best_state)
        return self

    @torch.no_grad()
    def encode(self, X: pd.DataFrame) -> pd.DataFrame:
        self.net.eval()
        W, order = build_windows(X, self.seq_len)
        zs = []
        for i in range(0, len(W), 4096):
            _, z = self.net(torch.tensor(W[i:i + 4096]).to(self.device))
            zs.append(z.cpu().numpy())
        Z = np.concatenate(zs)
        inv = np.empty_like(order)
        inv[order] = np.arange(len(order))
        return pd.DataFrame(Z[inv], index=X.index,
                            columns=[f"z{i}" for i in range(Z.shape[1])])


def cluster_latent(Z: pd.DataFrame, method: str = "kmeans", n_clusters: int = 20,
                   dbscan_eps: float = 0.5, dbscan_min_samples: int = 20,
                   seed: int = 0) -> pd.Series:
    """D31-D32: latent 空间聚类(KMeans / DBSCAN;DBSCAN 噪声=-1)。"""
    from sklearn.cluster import KMeans, DBSCAN
    if method == "kmeans":
        lab = KMeans(n_clusters=n_clusters, random_state=seed, n_init=10).fit_predict(Z.values)
    elif method == "dbscan":
        lab = DBSCAN(eps=dbscan_eps, min_samples=dbscan_min_samples).fit_predict(Z.values)
    else:
        raise ValueError(method)
    return pd.Series(lab, index=Z.index, name=f"cluster_{method}")


class _PlainLSTM(nn.Module):
    def __init__(self, n_feat: int, hidden: int, dropout: float):
        super().__init__()
        self.lstm = nn.LSTM(n_feat, hidden, batch_first=True)
        self.drop = nn.Dropout(dropout)
        self.out = nn.Linear(hidden, 1)

    def forward(self, x):
        o, _ = self.lstm(x)
        return self.out(self.drop(o[:, -1, :]))


class SingleLSTM:
    """F42: 单体 LSTM(win10);旧作系协议(早停+dropout 允许)。"""

    name = "lstm_single"

    def __init__(self, n_feat: int, seq_len: int = 10, hidden: int = 32,
                 dropout: float = 0.2, patience: int = 5, max_epochs: int = 60,
                 seed: int = 0, device: Optional[str] = None):
        self.seq_len, self.patience, self.max_epochs = seq_len, patience, max_epochs
        self.seed = seed
        self.device = pick_device(device)
        seed_everything(seed)
        self.net = _PlainLSTM(n_feat, hidden, dropout)

    def fit(self, X: pd.DataFrame, y: pd.Series, val_frac: float = 0.1) -> "SingleLSTM":
        W, order = build_windows(X, self.seq_len)
        yv = y.iloc[order].values.astype(np.float32)
        n_val = max(1, int(len(W) * val_frac))
        rng = np.random.default_rng(self.seed)
        idx = rng.permutation(len(W))
        tr, va = idx[n_val:], idx[:n_val]
        dev = self.device
        self.net.to(dev)
        opt = torch.optim.Adam(self.net.parameters())
        lossf = nn.MSELoss()
        loader = DataLoader(TensorDataset(torch.tensor(W[tr]), torch.tensor(yv[tr])),
                            batch_size=1024, shuffle=True,
                            generator=torch.Generator().manual_seed(self.seed))
        va_x, va_y = torch.tensor(W[va]).to(dev), torch.tensor(yv[va]).to(dev)
        best, best_state, bad = math.inf, None, 0
        for _ in range(self.max_epochs):
            self.net.train()
            for xb, yb in loader:
                xb, yb = xb.to(dev), yb.to(dev)
                opt.zero_grad()
                loss = lossf(self.net(xb).squeeze(-1), yb)
                loss.backward()
                opt.step()
            self.net.eval()
            with torch.no_grad():
                vloss = float(lossf(self.net(va_x).squeeze(-1), va_y).cpu())
            if vloss < best - 1e-6:
                best, bad = vloss, 0
                best_state = {k: v.detach().clone() for k, v in self.net.state_dict().items()}
            else:
                bad += 1
                if bad >= self.patience:
                    break
        if best_state:
            self.net.load_state_dict(best_state)
        return self

    @torch.no_grad()
    def predict(self, X: pd.DataFrame) -> pd.Series:
        self.net.eval()
        W, order = build_windows(X, self.seq_len)
        out = np.empty(len(W), dtype=np.float32)
        for i in range(0, len(W), 4096):
            out[i:i + 4096] = self.net(torch.tensor(W[i:i + 4096]).to(self.device)) \
                .squeeze(-1).cpu().numpy()
        inv = np.empty_like(order)
        inv[order] = np.arange(len(order))
        return pd.Series(out[inv], index=X.index, name=self.name)


class ClusteredLSTM:
    """F43: 分簇 LSTM(弱点③修复): 簇行数<cluster_min_rows 的并入 residual 簇,
    合并留痕于 self.merge_log;每簇独立 SingleLSTM。"""

    name = "lstm_cluster"

    def __init__(self, n_feat: int, cluster_min_rows: int = 5000, seed: int = 0,
                 device: Optional[str] = None, **lstm_kw):
        self.n_feat = n_feat
        self.cluster_min_rows = cluster_min_rows
        self.seed = seed
        self.device = device
        self.lstm_kw = lstm_kw
        self.models: Dict[int, SingleLSTM] = {}
        self.merge_log: List[dict] = []
        self._label_map: Dict[int, int] = {}

    RESIDUAL = -999

    def _effective_labels(self, labels: pd.Series) -> pd.Series:
        counts = labels.value_counts()
        small = counts[counts < self.cluster_min_rows].index.tolist()
        self._label_map = {c: (self.RESIDUAL if c in small else c) for c in counts.index}
        for c in small:
            self.merge_log.append({"cluster": int(c), "rows": int(counts[c]),
                                   "merged_into": "residual",
                                   "reason": f"rows<{self.cluster_min_rows} (弱点③)"})
        return labels.map(self._label_map)

    def fit(self, X: pd.DataFrame, y: pd.Series, labels: pd.Series) -> "ClusteredLSTM":
        eff = self._effective_labels(labels)
        for c in sorted(eff.unique()):
            mask = eff == c
            m = SingleLSTM(self.n_feat, seed=self.seed, device=self.device, **self.lstm_kw)
            m.fit(X[mask], y[mask])
            self.models[int(c)] = m
        return self

    def predict(self, X: pd.DataFrame, labels: pd.Series) -> pd.Series:
        eff = labels.map(lambda c: self._label_map.get(c, self.RESIDUAL))
        out = pd.Series(np.nan, index=X.index, name=self.name)
        fallback = self.models.get(self.RESIDUAL) or next(iter(self.models.values()))
        for c in eff.unique():
            mask = eff == c
            model = self.models.get(int(c), fallback)
            out[mask] = model.predict(X[mask]).values
        return out


# ═══════════════════════════════════════════════════════════════════
# TFT — 自研完整版(Lim et al. 2021;I57)
# ═══════════════════════════════════════════════════════════════════

class GLU(nn.Module):
    def __init__(self, d: int):
        super().__init__()
        self.a = nn.Linear(d, d)
        self.g = nn.Linear(d, d)

    def forward(self, x):
        return self.a(x) * torch.sigmoid(self.g(x))


class GRN(nn.Module):
    """Gated Residual Network(论文式 2-5): 可选上下文 c,GLU 门控+残差+LayerNorm。"""

    def __init__(self, d_in: int, d_hidden: int, d_out: Optional[int] = None,
                 d_context: Optional[int] = None, dropout: float = 0.1):
        super().__init__()
        d_out = d_out or d_in
        self.skip = nn.Linear(d_in, d_out) if d_in != d_out else nn.Identity()
        self.w2 = nn.Linear(d_in, d_hidden)
        self.w3 = nn.Linear(d_context, d_hidden, bias=False) if d_context else None
        self.elu = nn.ELU()
        self.w1 = nn.Linear(d_hidden, d_out)
        self.drop = nn.Dropout(dropout)
        self.glu = GLU(d_out)
        self.norm = nn.LayerNorm(d_out)

    def forward(self, x, c=None):
        h = self.w2(x)
        if self.w3 is not None and c is not None:
            h = h + self.w3(c)
        h = self.w1(self.elu(h))
        h = self.drop(h)
        return self.norm(self.skip(x) + self.glu(h))


class VSN(nn.Module):
    """Variable Selection Network(论文式 6-8): 逐变量 GRN + softmax 选择权重。"""

    def __init__(self, n_vars: int, d_model: int, d_context: Optional[int] = None,
                 dropout: float = 0.1):
        super().__init__()
        self.n_vars = n_vars
        self.var_grns = nn.ModuleList(
            [GRN(1, d_model, d_model, dropout=dropout) for _ in range(n_vars)])
        self.weight_grn = GRN(n_vars, d_model, n_vars, d_context=d_context,
                              dropout=dropout)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x, c=None):
        # x: (..., n_vars) 标量变量;返回 (加权表示 (...,d_model), 选择权重 (...,n_vars))
        w = self.softmax(self.weight_grn(x, c))
        reps = torch.stack([g(x[..., i:i + 1]) for i, g in enumerate(self.var_grns)],
                           dim=-2)                         # (..., n_vars, d_model)
        out = (w.unsqueeze(-1) * reps).sum(dim=-2)
        return out, w


class InterpretableMHA(nn.Module):
    """可解释多头注意力(论文式 14-16): 各头独立 Q,K、**共享 V**,输出按头平均。"""

    def __init__(self, d_model: int, n_heads: int = 4, dropout: float = 0.1):
        super().__init__()
        self.h = n_heads
        self.dk = d_model // n_heads
        self.q = nn.ModuleList([nn.Linear(d_model, self.dk) for _ in range(n_heads)])
        self.k = nn.ModuleList([nn.Linear(d_model, self.dk) for _ in range(n_heads)])
        self.v = nn.Linear(d_model, self.dk)               # 共享 V
        self.out = nn.Linear(self.dk, d_model)
        self.drop = nn.Dropout(dropout)

    def forward(self, q, k, v, mask=None):
        heads, attns = [], []
        vv = self.v(v)
        for i in range(self.h):
            qq, kk = self.q[i](q), self.k[i](k)
            a = qq @ kk.transpose(-2, -1) / math.sqrt(self.dk)
            if mask is not None:
                a = a.masked_fill(mask, -1e9)
            a = self.drop(torch.softmax(a, dim=-1))
            heads.append(a @ vv)
            attns.append(a)
        h_mean = torch.stack(heads).mean(0)
        attn_mean = torch.stack(attns).mean(0)             # (B, Tq, Tk) 可解释权重
        return self.out(h_mean), attn_mean


class QuantileLoss(nn.Module):
    def __init__(self, quantiles=(0.1, 0.5, 0.9)):
        super().__init__()
        self.qs = quantiles

    def forward(self, pred, target):                       # pred: (B,Q) target: (B,)
        losses = []
        for i, q in enumerate(self.qs):
            e = target - pred[:, i]
            losses.append(torch.max(q * e, (q - 1) * e))
        return torch.stack(losses, dim=1).mean()


class _TFTNet(nn.Module):
    def __init__(self, n_past: int, n_future: int, n_static_emb: int,
                 d_model: int = 32, n_heads: int = 4, dropout: float = 0.1,
                 quantiles=(0.1, 0.5, 0.9)):
        super().__init__()
        d = d_model
        self.static_emb = nn.Embedding(n_static_emb, d)
        # 静态侧仅 1 个变量(ticker 嵌入)→ VSN 退化: softmax 权重恒为 1,
        # 表示=该变量的 GRN 变换(与论文式 6-8 在 n_vars=1 时严格一致)
        self.static_grn = GRN(d, d, dropout=dropout)
        # 4 个静态上下文向量(论文): c_s(VSN 上下文) c_c/c_h(LSTM 初始) c_e(富集)
        self.ctx_s = GRN(d, d, dropout=dropout)
        self.ctx_c = GRN(d, d, dropout=dropout)
        self.ctx_h = GRN(d, d, dropout=dropout)
        self.ctx_e = GRN(d, d, dropout=dropout)
        self.past_vsn = VSN(n_past, d, d_context=d, dropout=dropout)
        self.fut_vsn = VSN(n_future, d, d_context=d, dropout=dropout) if n_future else None
        self.enc = nn.LSTM(d, d, batch_first=True)
        self.dec = nn.LSTM(d, d, batch_first=True)
        self.gate_seq = GLU(d)
        self.norm_seq = nn.LayerNorm(d)
        self.enrich = GRN(d, d, d_context=d, dropout=dropout)
        self.attn = InterpretableMHA(d, n_heads, dropout)
        self.gate_attn = GLU(d)
        self.norm_attn = nn.LayerNorm(d)
        self.pos_ff = GRN(d, d, dropout=dropout)
        self.gate_out = GLU(d)
        self.norm_out = nn.LayerNorm(d)
        self.head = nn.Linear(d, len(quantiles))
        self.last_attn: Optional[torch.Tensor] = None
        self.last_var_w: Optional[torch.Tensor] = None

    def forward(self, past, fut, static_id):
        # past: (B,T,n_past)  fut: (B,1,n_future) 或 None  static_id: (B,)
        B, T, _ = past.shape
        s = self.static_emb(static_id)                     # (B,d)
        s_sel = self.static_grn(s)                         # 单静态变量的退化 VSN
        cs, cc, ch, ce = self.ctx_s(s_sel), self.ctx_c(s_sel), self.ctx_h(s_sel), self.ctx_e(s_sel)
        p_emb, p_w = self.past_vsn(past, cs.unsqueeze(1).expand(-1, T, -1))
        self.last_var_w = p_w.detach()
        enc_out, (h, c) = self.enc(p_emb, (cc.unsqueeze(0).contiguous(),
                                           ch.unsqueeze(0).contiguous()))
        if self.fut_vsn is not None and fut is not None:
            f_emb, _ = self.fut_vsn(fut, cs.unsqueeze(1))
            dec_out, _ = self.dec(f_emb, (h, c))
            seq = torch.cat([enc_out, dec_out], dim=1)     # (B,T+1,d)
            phi = torch.cat([p_emb, f_emb], dim=1)
        else:
            seq = enc_out
            phi = p_emb
        seq = self.norm_seq(phi + self.gate_seq(seq))      # 门控跳连(式 10-11)
        theta = self.enrich(seq, ce.unsqueeze(1).expand(-1, seq.size(1), -1))
        Tq = seq.size(1)
        mask = torch.triu(torch.ones(Tq, Tq, dtype=torch.bool, device=seq.device), 1)
        attn_out, attn_w = self.attn(theta, theta, theta, mask)
        self.last_attn = attn_w.detach()
        x = self.norm_attn(theta + self.gate_attn(attn_out))
        x = self.pos_ff(x)
        x = self.norm_out(seq + self.gate_out(x))          # 最终门控跳连(式 17)
        return self.head(x[:, -1, :])                      # 末位置 → 分位输出


class TFT:
    """完整 TFT(自研;I57)。未来已知输入=cal_/earn_ 前缀列(t+1 已知);静态=ticker 嵌入。
    predict() 返回中位数;predict_quantiles() 返回全部分位;get_attention()/
    get_variable_weights() 供弱点⑪归因。旧作系协议(早停允许)。"""

    name = "tft"
    seq_len = 10

    def __init__(self, past_cols: List[str], future_cols: List[str],
                 d_model: int = 32, n_heads: int = 4, dropout: float = 0.1,
                 quantiles=(0.1, 0.5, 0.9), max_epochs: int = 30, patience: int = 5,
                 seed: int = 0, device: Optional[str] = None):
        self.past_cols, self.future_cols = list(past_cols), list(future_cols)
        self.quantiles = quantiles
        self.max_epochs, self.patience = max_epochs, patience
        self.seed = seed
        self.device = pick_device(device)
        self._ticker_ix: Dict[str, int] = {}
        self._net_kw = dict(d_model=d_model, n_heads=n_heads, dropout=dropout,
                            quantiles=quantiles)
        self.net: Optional[_TFTNet] = None

    def _static_ids(self, X: pd.DataFrame) -> np.ndarray:
        tk = X.index.get_level_values(1)
        for t in tk.unique():
            if t not in self._ticker_ix:
                self._ticker_ix[t] = len(self._ticker_ix)
        return np.array([self._ticker_ix[t] for t in tk], dtype=np.int64)

    def _tensors(self, X: pd.DataFrame):
        W, order = build_windows(X[self.past_cols], self.seq_len)
        Xs = X.iloc[order]
        fut = (torch.tensor(Xs[self.future_cols].values, dtype=torch.float32).unsqueeze(1)
               if self.future_cols else None)
        sid = torch.tensor(self._static_ids(Xs))
        return torch.tensor(W), fut, sid, order

    def fit(self, X: pd.DataFrame, y: pd.Series, val_frac: float = 0.1) -> "TFT":
        seed_everything(self.seed)
        self.net = _TFTNet(len(self.past_cols), len(self.future_cols),
                           n_static_emb=max(len(X.index.get_level_values(1).unique()) + 8, 16),
                           **self._net_kw)
        W, fut, sid, order = self._tensors(X)
        yv = torch.tensor(y.iloc[order].values, dtype=torch.float32)
        n_val = max(1, int(len(W) * val_frac))
        rng = np.random.default_rng(self.seed)
        idx = rng.permutation(len(W))
        tr, va = idx[n_val:], idx[:n_val]
        dev = self.device
        self.net.to(dev)
        opt = torch.optim.Adam(self.net.parameters())
        lossf = QuantileLoss(self.quantiles)
        def batch(ix):
            f = fut[ix].to(dev) if fut is not None else None
            return W[ix].to(dev), f, sid[ix].to(dev)
        best, best_state, bad = math.inf, None, 0
        for _ in range(self.max_epochs):
            self.net.train()
            perm = rng.permutation(tr)
            for i in range(0, len(perm), 512):
                ix = perm[i:i + 512]
                p, f, si = batch(ix)
                opt.zero_grad()
                loss = lossf(self.net(p, f, si), yv[ix].to(dev))
                loss.backward()
                opt.step()
            self.net.eval()
            with torch.no_grad():
                p, f, si = batch(va)
                vloss = float(lossf(self.net(p, f, si), yv[va].to(dev)).cpu())
            if vloss < best - 1e-6:
                best, bad = vloss, 0
                best_state = {k: v.detach().clone() for k, v in self.net.state_dict().items()}
            else:
                bad += 1
                if bad >= self.patience:
                    break
        if best_state:
            self.net.load_state_dict(best_state)
        return self

    @torch.no_grad()
    def predict_quantiles(self, X: pd.DataFrame) -> pd.DataFrame:
        self.net.eval()
        W, fut, sid, order = self._tensors(X)
        outs = []
        for i in range(0, len(W), 2048):
            f = fut[i:i + 2048].to(self.device) if fut is not None else None
            outs.append(self.net(W[i:i + 2048].to(self.device), f,
                                 sid[i:i + 2048].to(self.device)).cpu().numpy())
        Q = np.concatenate(outs)
        inv = np.empty_like(order)
        inv[order] = np.arange(len(order))
        return pd.DataFrame(Q[inv], index=X.index,
                            columns=[f"q{q}" for q in self.quantiles])

    def predict(self, X: pd.DataFrame) -> pd.Series:
        qdf = self.predict_quantiles(X)
        mid = f"q{self.quantiles[len(self.quantiles) // 2]}"
        return qdf[mid].rename(self.name)

    def get_attention(self) -> Optional[np.ndarray]:
        """最近一次前向的时间注意力权重(按头平均;弱点⑪)。"""
        return None if self.net is None or self.net.last_attn is None \
            else self.net.last_attn.cpu().numpy()

    def get_variable_weights(self) -> Optional[np.ndarray]:
        return None if self.net is None or self.net.last_var_w is None \
            else self.net.last_var_w.cpu().numpy()

    def param_count(self) -> int:
        return sum(p.numel() for p in self.net.parameters() if p.requires_grad)
