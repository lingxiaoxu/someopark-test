"""
ml — Tier3 机器学习(附录 A: E34-41 ML 部分;弱点①的 torch 化)
==============================================================
- AdaBoostModel: AdaBoostRegressor(基学习器 DecisionTreeRegressor(max_depth=2)),
  旧规格;旧成绩 .280。
- NN2Model: 旧作 keras [32,16]+ReLU+L1 → torch 重实现。单 seed(多 seed 平均由
  walkforward 调用侧负责,DEV_CONTRACTS);L1 系数默认 1e-5(旧 notebook 值在 P1
  逐步移植时对齐,registry 记录);Adam 默认参、batch 1024、epochs 可配默认 50。
  设备: MPS 可用则用(弱点①),否则 CPU。旧成绩 .815(log_volume 目标全局最优)。
- LightGBMModel: 论文风格 GBM 主力的补齐(旧作缺失);lightgbm 延迟 import
  (安装由主协调者进行,未装时抛 ImportError——不静默降级)。
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd

from . import BaseModel, feature_cols


class AdaBoostModel(BaseModel):
    name = "adaboost_d2"

    def __init__(self, n_estimators: int = 100, random_state: int = 0) -> None:
        self.n_estimators = n_estimators
        self.random_state = random_state
        self._est = None
        self._cols: List[str] = []

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "AdaBoostModel":
        from sklearn.ensemble import AdaBoostRegressor
        from sklearn.tree import DecisionTreeRegressor
        self._cols = feature_cols(X)
        self._est = AdaBoostRegressor(
            estimator=DecisionTreeRegressor(max_depth=2),
            n_estimators=self.n_estimators,
            random_state=self.random_state,
        )
        self._est.fit(X[self._cols].values, y.values)
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        yhat = self._est.predict(X[self._cols].values)
        return pd.Series(yhat, index=X.index, name="eta_hat")

class NN2Model(BaseModel):
    """[32,16] 全连接 + ReLU + L1(旧作架构 torch 化)。"""

    name = "nn2"

    def __init__(self, epochs: int = 50, batch: int = 1024, l1: float = 1e-5,
                 seed: int = 0, device: Optional[str] = None) -> None:
        self.epochs, self.batch, self.l1, self.seed = epochs, batch, l1, seed
        self.device = device
        self._net = None
        self._cols: List[str] = []
        self._mu = self._sd = None

    def _pick_device(self):
        import torch
        if self.device:
            return torch.device(self.device)
        if torch.backends.mps.is_available():
            return torch.device("mps")
        return torch.device("cpu")

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "NN2Model":
        import torch
        from torch import nn
        torch.manual_seed(self.seed)
        np.random.seed(self.seed)
        self._cols = feature_cols(X)
        Xv = X[self._cols].values.astype(np.float32)
        # 标准化(旧作 pipeline 有 zscore;模型内再稳一层,记录参数供 predict)
        # 训练期恒定特征(std≈0)防护: 旧式 +1e-8 地板使测试期该特征一旦变化即被
        # 放大 1e8 倍 → 预测爆炸(2026-07-27 legacy 表实测 -4.1e9)。std<1e-6 视为
        # 常量,除数置 1(特征本身有界 ±5,居中后保持有界)
        self._mu = Xv.mean(axis=0)
        sd = Xv.std(axis=0)
        self._sd = np.where(sd < 1e-6, 1.0, sd)
        Xv = (Xv - self._mu) / self._sd
        yv = y.values.astype(np.float32).reshape(-1, 1)

        dev = self._pick_device()
        net = nn.Sequential(
            nn.Linear(Xv.shape[1], 32), nn.ReLU(),
            nn.Linear(32, 16), nn.ReLU(),
            nn.Linear(16, 1),
        ).to(dev)
        opt = torch.optim.Adam(net.parameters())
        lossf = nn.MSELoss()
        Xt = torch.from_numpy(Xv)
        yt = torch.from_numpy(yv)
        n = len(Xt)
        for _ in range(self.epochs):
            perm = torch.randperm(n)
            for i in range(0, n, self.batch):
                idx = perm[i:i + self.batch]
                xb, yb = Xt[idx].to(dev), yt[idx].to(dev)
                opt.zero_grad()
                out = net(xb)
                loss = lossf(out, yb)
                if self.l1 > 0:
                    loss = loss + self.l1 * sum(p.abs().sum() for p in net.parameters())
                loss.backward()
                opt.step()
        self._net = net.eval()
        self._dev = dev
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        import torch
        Xv = (X[self._cols].values.astype(np.float32) - self._mu) / self._sd
        with torch.no_grad():
            yhat = self._net(torch.from_numpy(Xv).to(self._dev)).cpu().numpy().ravel()
        return pd.Series(yhat, index=X.index, name="eta_hat")

    def param_count(self) -> int:
        return int(sum(p.numel() for p in self._net.parameters()))

    def gradient_attribution(self, X: pd.DataFrame, n_sample: int = 2048) -> pd.Series:
        """弱点⑪ NN 通道: |∂ŷ/∂x| 的样本均值,按特征。"""
        import torch
        Xv = (X[self._cols].values.astype(np.float32) - self._mu) / self._sd
        if len(Xv) > n_sample:
            sel = np.random.default_rng(0).choice(len(Xv), n_sample, replace=False)
            Xv = Xv[sel]
        xt = torch.from_numpy(Xv).to(self._dev).requires_grad_(True)
        self._net(xt).sum().backward()
        grad = xt.grad.abs().mean(dim=0).cpu().numpy()
        return pd.Series(grad, index=self._cols, name="grad_attr").sort_values(ascending=False)


class LightGBMModel(BaseModel):
    """论文主力 GBM 的补齐。默认参;lightgbm 未装时显式报错。

    **进程隔离(2026-07-23 定案)**: pip-lightgbm 与 torch 的 libomp 同进程共存
    不可靠(顺序+旗标下仍死锁,实测三轮)——torch 已载入时 fit/predict 自动路由到
    干净子进程(booster 以 txt 落盘交换,数据经 parquet);torch 未载入时进程内直跑。
    功能零删减,只是执行位置不同。
    """

    name = "lgbm"

    def __init__(self, n_estimators: int = 300, random_state: int = 0) -> None:
        self.n_estimators = n_estimators
        self.random_state = random_state
        self._est = None                 # 进程内模式
        self._booster_file = None        # 子进程模式(booster txt)
        self._cols: List[str] = []

    @staticmethod
    def _torch_loaded() -> bool:
        import sys as _sys
        return "torch" in _sys.modules

    def _run_sub(self, payload: dict) -> dict:
        """在无 torch 的干净子进程里执行 lightgbm 操作(worker=_lgbm_worker.py)。"""
        import json as _json
        import subprocess as _sp
        import sys as _sys
        import tempfile as _tf
        from pathlib import Path as _P
        worker = _P(__file__).parent / "_lgbm_worker.py"
        with _tf.NamedTemporaryFile("w", suffix=".json", delete=False,
                                    dir="/tmp") as f:
            _json.dump(payload, f)
            pf = f.name
        r = _sp.run([_sys.executable, str(worker), pf],
                    capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(f"lgbm subprocess failed: {r.stderr[-400:]}")
        return _json.loads(r.stdout.strip().splitlines()[-1])

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "LightGBMModel":
        self._cols = feature_cols(X)
        if not self._torch_loaded():
            import os as _os
            _os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
            import lightgbm as lgb   # 延迟 import;缺失即抛,不降级
            self._est = lgb.LGBMRegressor(
                n_estimators=self.n_estimators, random_state=self.random_state,
                verbose=-1)
            self._est.fit(X[self._cols].values, y.values)
            return self
        # 子进程模式
        import tempfile as _tf
        from pathlib import Path as _P
        d = _P(_tf.mkdtemp(prefix="vp_lgbm_", dir="/tmp"))
        X[self._cols].to_parquet(d / "x.parquet")
        y.to_frame("y").reset_index(drop=True).to_parquet(d / "y.parquet")
        self._booster_file = str(d / "booster.txt")
        self._run_sub({"op": "fit", "x": str(d / "x.parquet"),
                       "y": str(d / "y.parquet"),
                       "booster": self._booster_file,
                       "n_estimators": self.n_estimators,
                       "random_state": self.random_state})
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        if self._est is not None:
            yhat = self._est.predict(X[self._cols].values)
            return pd.Series(yhat, index=X.index, name="eta_hat")
        # 子进程模式
        import tempfile as _tf
        import pandas as _pd
        from pathlib import Path as _P
        d = _P(_tf.mkdtemp(prefix="vp_lgbm_p_", dir="/tmp"))
        X[self._cols].to_parquet(d / "x.parquet")
        out = d / "pred.parquet"
        self._run_sub({"op": "predict", "x": str(d / "x.parquet"),
                       "booster": self._booster_file, "out": str(out)})
        yhat = _pd.read_parquet(out)["yhat"].values
        return pd.Series(yhat, index=X.index, name="eta_hat")

    def param_count(self) -> int:
        if self._est is not None:
            return int(self._est.booster_.num_trees())
        if self._booster_file:
            # 子进程模式: 文本解析 booster(不 import lightgbm,免 libomp)
            with open(self._booster_file) as f:
                return sum(1 for ln in f if ln.startswith("Tree="))
        return 0
