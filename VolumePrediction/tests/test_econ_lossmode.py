"""EconNN loss_mode(E11-T3)测试:默认行为不变 + 三变体机制正确(小合成数据)。"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from VolumePrediction.models.econ import (
    EconNN, _losscon_torch, s_opt, mel_normalized)

TMP = Path("/tmp/vp_tests/econ_lossmode")
TMP.mkdir(parents=True, exist_ok=True)

RNG = np.random.default_rng(3)
N, P = 600, 6
X = pd.DataFrame(RNG.normal(0, 1, (N, P)), columns=[f"f{i}" for i in range(P)])
# v = η + ma5,量级仿真实(log 美元量 ~ 10-20)
MA5 = pd.Series(14.0 + RNG.normal(0, 1, N))
ETA = pd.Series(0.5 * X["f0"].values + 0.1 * RNG.normal(0, 1, N))
MU = 1e-6


def _mk(mode, epochs=3):
    m = EconNN(P, mu=MU, seed=0, epochs=epochs, batch=256, loss_mode=mode)
    # 面板排序依赖 MultiIndex(date,ticker)——单日多名字即可
    idx = pd.MultiIndex.from_product([["2026-01-02"], range(N)],
                                     names=["date", "ticker"])
    Xi = X.copy(); Xi.index = idx
    ei = ETA.copy(); ei.index = idx
    mi = MA5.copy(); mi.index = idx
    return m, Xi, ei, mi


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        EconNN(P, mu=MU, loss_mode="banana")


def test_default_absolute_loss_identical_to_original_formula():
    m, *_ = _mk("absolute")
    vb = torch.tensor([14.0, 15.0, 16.0])
    z = torch.tensor([0.3, 0.6, 0.9])
    assert torch.allclose(m._loss(vb, z, 0.0), _losscon_torch(vb, z, MU).mean())


def test_regret_loss_is_O1_scale():
    m, *_ = _mk("regret")
    vb = torch.tensor([14.0, 15.0, 16.0])
    z_orc = torch.tensor([float(s_opt(v, MU)) for v in vb])
    at_opt = m._loss(vb, z_orc, 0.0)
    assert abs(float(at_opt)) < 1e-4              # 最优点 regret≈0
    off = m._loss(vb, z_orc * 0.5, 0.0)
    assert float(off) > 0.01                      # 偏离 → O(1) 惩罚(可传梯度)
    # 对照: 绝对损失在同样偏离下的量级(梯度淹没的病根)
    m_abs, *_ = _mk("absolute")
    assert float(m_abs._loss(vb, z_orc * 0.5, 0.0)) < 1e-6


def test_anneal_interpolates():
    m, *_ = _mk("anneal")
    vb = torch.tensor([14.0, 15.0])
    z = torch.tensor([0.4, 0.7])
    l0 = float(m._loss(vb, z, 0.0))               # 纯 MSE 端
    l1 = float(m._loss(vb, z, 1.0))               # 纯 regret 端
    m_r, *_ = _mk("regret")
    assert abs(l1 - float(m_r._loss(vb, z, 0.0))) < 1e-6
    z_orc = torch.tensor([float(s_opt(v, MU)) for v in vb])
    assert abs(l0 - float(((z - z_orc) ** 2).mean())) < 1e-6


def test_analytic_s_z_matches_closed_form():
    m, Xi, ei, mi = _mk("analytic_s")
    xb = torch.tensor(Xi.values[:8], dtype=torch.float32)
    v_hat = m.net.core(xb).squeeze(-1)
    z = m._z_of(xb)
    expect = torch.sigmoid(v_hat - math.log(0.2 / MU))
    assert torch.allclose(z, expect)
    # 恒等: sigmoid(v−log(0.2/μ)) == μ/(μ+0.2e^{−v})
    z_np = np.array([s_opt(float(v), MU) for v in v_hat.detach()])
    assert np.allclose(z.detach().numpy(), z_np, atol=1e-6)


@pytest.mark.parametrize("mode", ["regret", "anneal", "analytic_s"])
def test_variants_train_without_nan_and_predict_in_unit_interval(mode):
    m, Xi, ei, mi = _mk(mode)
    m.fit(Xi, ei, mi)
    z = m.predict_z(Xi)
    assert np.isfinite(z.values).all()
    assert ((z.values >= 0) & (z.values <= 1)).all()
    # MEL 可计算(协议兼容)
    v_true = (ei + mi).values
    mel = mel_normalized(v_true, z.values, mi.values, MU)
    assert np.isfinite(mel)
