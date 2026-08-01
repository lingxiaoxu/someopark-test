"""
models/econ — Tier5 经济学习(Plan B G6/G7/G8 + 58 步 G44-G51)
=============================================================
经济损失(论文式 losscon):
    losscon(v, z; μ) = λ(v)·z² + μ·(1−z)²,   λ(v) = 0.2·e^{−v} = 0.2/V
最优交易率闭式解(论文 §4.3):
    s(v̄; μ) = 1 / (1 + exp(−v̄ + ln 0.2 − ln μ))          [= μ/(μ+λ(v̄))]

B.2 等价形式(losscon_zz): 以"最优交易率 z*"参数化——由 z*=μ/(μ+λ) ⟹
    λ = μ(1−z*)/z*,故
    losscon_zz(z*, ẑ; μ) = μ·((1−z*)/z*)·ẑ² + μ·(1−ẑ)²
恒等式(测试断言): losscon_zz(s(v;μ), ẑ;μ) ≡ losscon(v, ẑ;μ)。

双学习范式(G7):
- 统计学习: min MSE 得 v̂,交易 z=s(v̂;μ)
- 经济学习: z=net(X)(sigmoid 输出),直接 min mean losscon(v_true, z;μ)
- oracle: z=s(v_true;μ)(完美预见,MEL 下界)
- 命题 1(Bregman): E[v|X] 插入解 ≢ 经济最优——由 MEL 排序在实证中验证

迁移学习(G8): 预训练统计网 → 经济损失微调,仅更新网络权重;
"+ma5" 与 "s(·;μ)" 为**固定运算**(不含参数);少量 epoch(默认 5,论文"少量")。

旧作命名保全(58 步 G 组):
- NN2_econ ≙ EconNN(网络直接输出 z 经 sigmoid)
- Ada_econ ≙ EconAda —— 树模型无法反传经济损失;旧作语义=AdaBoost 统计预测 v̂
  后经 s(·;μ) 转换的"统计学习-Ada"变体(论文 §5.1 注:经济学习均以神经网络实现)。
  此读法在 docstring 留档,不冒充梯度经济学习。

MEL 归一化(论文表 2 口径): oracle=100%,ma5 基线(η̂=0 ⟹ v̂=ma5)=0%。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

import torch
import torch.nn as nn

from .deep import PaperNN, _NNCore, pick_device, seed_everything, _sort_panel

__all__ = [
    "lam", "losscon", "s_opt", "s_inv", "losscon_zz",
    "oracle_z", "mel", "mel_normalized",
    "EconNN", "EconAda", "TransferEconNN",
    "fig3_loss_surfaces",
]

LN_02 = math.log(0.2)


# ═══════════════════ 核心公式(numpy/torch 双实现) ═══════════════════

def lam(v, form: str = "0.2/V"):
    """λ(v): 0.2/V = 0.2·e^{−v};替代形式 0.2·sqrt(1/V)=0.2·e^{−v/2}(注16)。"""
    v = np.asarray(v, dtype=float)
    if form == "0.2/V":
        return 0.2 * np.exp(-v)
    if form == "0.2*sqrt(1/V)":
        return 0.2 * np.exp(-v / 2.0)
    raise ValueError(form)


def losscon(v, z, mu: float, form: str = "0.2/V"):
    v = np.asarray(v, dtype=float)
    z = np.asarray(z, dtype=float)
    return lam(v, form) * z ** 2 + mu * (1.0 - z) ** 2


def s_opt(v_bar, mu: float):
    """闭式最优交易率 s(v̄;μ)=1/(1+exp(−v̄+ln0.2−lnμ))。"""
    v_bar = np.asarray(v_bar, dtype=float)
    return 1.0 / (1.0 + np.exp(-v_bar + LN_02 - math.log(mu)))


def s_inv(z, mu: float):
    """s 的逆: 由 z 反推隐含 v̄(B.2)。z∈(0,1)。"""
    z = np.asarray(z, dtype=float)
    return LN_02 - math.log(mu) + np.log(z / (1.0 - z))


def losscon_zz(z_star, z_hat, mu: float):
    """B.2 等价形式: 以最优率 z* 参数化的经济损失(与 losscon 恒等,见模块 docstring)。"""
    z_star = np.asarray(z_star, dtype=float)
    z_hat = np.asarray(z_hat, dtype=float)
    lam_implied = mu * (1.0 - z_star) / z_star
    return lam_implied * z_hat ** 2 + mu * (1.0 - z_hat) ** 2


def oracle_z(v_true, mu: float):
    return s_opt(v_true, mu)


def mel(v_true, z, mu: float, form: str = "0.2/V") -> float:
    """Mean Economic Loss。"""
    return float(np.mean(losscon(v_true, z, mu, form)))


def mel_normalized(v_true, z, ma5_v, mu: float) -> float:
    """表 2 口径: (MEL_ma5 − MEL) / (MEL_ma5 − MEL_oracle);oracle=100%, ma5=0%。"""
    m = mel(v_true, z, mu)
    m_ma5 = mel(v_true, s_opt(ma5_v, mu), mu)          # η̂=0 ⟹ v̂=ma5
    m_orc = mel(v_true, oracle_z(v_true, mu), mu)
    denom = m_ma5 - m_orc
    return float((m_ma5 - m) / denom) if abs(denom) > 1e-18 else float("nan")


def _losscon_torch(v_true: torch.Tensor, z: torch.Tensor, mu: float) -> torch.Tensor:
    return 0.2 * torch.exp(-v_true) * z ** 2 + mu * (1.0 - z) ** 2


# ═══════════════════ 经济学习模型 ═══════════════════

class _EconNet(nn.Module):
    """NN2_econ 架构: 32-16-8 主干 + sigmoid 输出 z∈(0,1)。"""

    def __init__(self, n_pred: int):
        super().__init__()
        self.core = _NNCore(n_pred)

    def forward(self, x):
        return torch.sigmoid(self.core(x)).squeeze(-1)


class EconNN:
    """经济学习(G7 方法2): z=net(X),min mean losscon(v_true, z;μ)。
    v_true = eta_true + ma5_v(v=η+ma5)。旧作名 NN2_econ。"""

    name = "nn2_econ"

    def __init__(self, n_pred: int, mu: float, seed: int = 0,
                 device: Optional[str] = None, epochs: int = 50, batch: int = 1024):
        self.n_pred, self.mu = n_pred, mu
        self.seed, self.epochs, self.batch = seed, epochs, batch
        self.device = pick_device(device)
        seed_everything(seed)
        self.net = _EconNet(n_pred)

    def fit(self, X: pd.DataFrame, eta_true: pd.Series, ma5_v: pd.Series,
            epochs: Optional[int] = None) -> "EconNN":
        Xs, es, order = _sort_panel(X, eta_true)
        ms = ma5_v.iloc[order]
        xt = torch.tensor(Xs.values, dtype=torch.float32)
        vt = torch.tensor((es.values + ms.values), dtype=torch.float32)   # v_true
        dev = self.device
        self.net.to(dev)
        opt = torch.optim.Adam(self.net.parameters())
        g = torch.Generator().manual_seed(self.seed)
        n = len(xt)
        for _ in range(epochs or self.epochs):
            perm = torch.randperm(n, generator=g)
            for i in range(0, n, self.batch):
                ix = perm[i:i + self.batch]
                xb, vb = xt[ix].to(dev), vt[ix].to(dev)
                opt.zero_grad()
                z = self.net(xb)
                loss = _losscon_torch(vb, z, self.mu).mean()
                loss.backward()
                opt.step()
        return self

    @torch.no_grad()
    def predict_z(self, X: pd.DataFrame) -> pd.Series:
        self.net.eval()
        z = self.net(torch.tensor(X.values, dtype=torch.float32).to(self.device))
        return pd.Series(z.cpu().numpy(), index=X.index, name=self.name)


class EconAda:
    """Ada_econ(旧作保全): AdaBoost(depth-2 树,旧规格)统计预测 η̂ →
    v̂=η̂+ma5 → z=s(v̂;μ)。树系不可反传经济损失,此为旧作实际语义(docstring 留档)。"""

    name = "ada_econ"

    def __init__(self, mu: float, n_estimators: int = 50, max_depth: int = 2,
                 seed: int = 0):
        from sklearn.ensemble import AdaBoostRegressor
        from sklearn.tree import DecisionTreeRegressor
        self.mu = mu
        self.model = AdaBoostRegressor(
            estimator=DecisionTreeRegressor(max_depth=max_depth, random_state=seed),
            n_estimators=n_estimators, random_state=seed)

    def fit(self, X: pd.DataFrame, eta_true: pd.Series, ma5_v: pd.Series) -> "EconAda":
        self.model.fit(X.values, eta_true.values)
        return self

    def predict_z(self, X: pd.DataFrame, ma5_v: pd.Series) -> pd.Series:
        eta_hat = self.model.predict(X.values)
        v_hat = eta_hat + ma5_v.values
        return pd.Series(s_opt(v_hat, self.mu), index=X.index, name=self.name)


class TransferEconNN:
    """迁移学习(G8): 统计预训练网(η̂) → 经济微调。
    管线 z = s(η̂(X)+ma5 ; μ);"+ma5" 与 "s" 为固定运算(零参数),
    微调只更新网络权重,少量 epoch(默认 5)。"""

    name = "transfer_econ"

    def __init__(self, pretrained: PaperNN, mu: float, finetune_epochs: int = 5,
                 batch: int = 1024, seed: int = 0):
        self.base = pretrained
        self.mu = mu
        self.finetune_epochs = finetune_epochs
        self.batch = batch
        self.seed = seed
        self.device = pretrained.device
        self.finetune_history: List[float] = []

    def _z_from_eta_hat(self, eta_hat: torch.Tensor, ma5: torch.Tensor) -> torch.Tensor:
        v_hat = eta_hat + ma5                                     # 固定 "+ma5"
        logit = v_hat - LN_02 + math.log(self.mu)                 # 固定 s(·;μ)
        return torch.sigmoid(logit)

    def finetune(self, X: pd.DataFrame, eta_true: pd.Series, ma5_v: pd.Series,
                 epochs: Optional[int] = None) -> "TransferEconNN":
        Xs, es, order = _sort_panel(X, eta_true)
        ms = ma5_v.iloc[order]
        xt = torch.tensor(Xs.values, dtype=torch.float32)
        mt = torch.tensor(ms.values, dtype=torch.float32)
        vt = torch.tensor(es.values + ms.values, dtype=torch.float32)
        net = self.base.net
        dev = self.device
        net.to(dev).train()
        opt = torch.optim.Adam(net.parameters())
        g = torch.Generator().manual_seed(self.seed)
        n = len(xt)
        for _ in range(epochs or self.finetune_epochs):
            perm = torch.randperm(n, generator=g)
            tot, nb = 0.0, 0
            for i in range(0, n, self.batch):
                ix = perm[i:i + self.batch]
                xb, mb, vb = xt[ix].to(dev), mt[ix].to(dev), vt[ix].to(dev)
                opt.zero_grad()
                eta_hat = net(xb).squeeze(-1)
                z = self._z_from_eta_hat(eta_hat, mb)
                loss = _losscon_torch(vb, z, self.mu).mean()
                loss.backward()
                opt.step()
                tot += float(loss.detach().cpu())
                nb += 1
            self.finetune_history.append(tot / max(nb, 1))
        return self

    @torch.no_grad()
    def predict_z(self, X: pd.DataFrame, ma5_v: pd.Series) -> pd.Series:
        net = self.base.net
        net.eval()
        eta_hat = net(torch.tensor(X.values, dtype=torch.float32).to(self.device)).squeeze(-1)
        mb = torch.tensor(ma5_v.values, dtype=torch.float32).to(self.device)
        z = self._z_from_eta_hat(eta_hat, mb)
        return pd.Series(z.cpu().numpy(), index=X.index, name=self.name)

    def predict_eta(self, X: pd.DataFrame) -> pd.Series:
        """微调后网络的统计口径预测(用于\"微调后 MSE 略降/MEL 显著升\"签名验证)。"""
        return self.base.predict(X)


# ═══════════════════ 图 3 复刻 ═══════════════════

def fig3_loss_surfaces(v_list: Sequence[float] = (4, 8, 12, 16, 20),
                       mu: float = 1e-4, n_grid: int = 401
                       ) -> Dict[float, Dict[str, np.ndarray]]:
    """论文图 3: 各真值 v 下,经济损失 vs 最小二乘损失的形状对比(数据版,不依赖 mpl)。
    经济损失以 z 网格给出;lossls 以 v̂ 网格 ½(v̂−v)² 并经 z=s(v̂;μ) 映射到同一 z 轴。"""
    out: Dict[float, Dict[str, np.ndarray]] = {}
    z = np.linspace(1e-4, 1 - 1e-4, n_grid)
    for v in v_list:
        v_hat = s_inv(z, mu)                     # 该 z 隐含的预测 v̂
        out[float(v)] = {
            "z": z,
            "losscon": losscon(v, z, mu),
            "lossls": 0.5 * (v_hat - v) ** 2,
            "z_star": np.array([float(s_opt(v, mu))]),
        }
    return out


def plot_fig3(surfaces: Dict[float, Dict[str, np.ndarray]], path: str) -> None:
    """可选画图(matplotlib 仅在此函数内 import)。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, len(surfaces), figsize=(4 * len(surfaces), 3.2),
                             sharey=False)
    for ax, (v, d) in zip(np.atleast_1d(axes), sorted(surfaces.items())):
        ax.plot(d["z"], d["losscon"], label="losscon")
        ax.plot(d["z"], d["lossls"] / max(d["lossls"].max(), 1e-12) * d["losscon"].max(),
                "--", label="lossls(scaled)")
        ax.axvline(d["z_star"][0], color="k", lw=0.6)
        ax.set_title(f"v={v:g}")
        ax.set_xlabel("z")
    np.atleast_1d(axes)[0].legend()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
