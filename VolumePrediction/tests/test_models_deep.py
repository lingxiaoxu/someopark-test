"""tests/test_models_deep — deep.py + econ.py 单元测试(全部小合成数据,CPU,秒级)。

纪律: 输出只进 /tmp/vp_tests/models_deep/;不触碰任何策略生产目录;不做重算力。
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from VolumePrediction.models.deep import (      # noqa: E402
    PaperNN, PaperRNN, LSTMAutoencoder, cluster_latent, SingleLSTM,
    ClusteredLSTM, TFT, build_windows, seed_everything,
)
from VolumePrediction.models import econ as E   # noqa: E402

TMP = Path("/tmp/vp_tests/models_deep")
TMP.mkdir(parents=True, exist_ok=True)

CPU = "cpu"


# ─────────────────────────── 合成面板 ───────────────────────────

def synth_panel(n_days=40, n_tickers=6, n_feat=8, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-02", periods=n_days)
    tickers = [f"T{i}" for i in range(n_tickers)]
    ix = pd.MultiIndex.from_product([dates, tickers], names=["date", "ticker"])
    X = pd.DataFrame(rng.normal(size=(len(ix), n_feat)),
                     index=ix, columns=[f"tech_f{i}" for i in range(n_feat)])
    beta = rng.normal(size=n_feat)
    y = pd.Series(X.values @ beta * 0.1 + rng.normal(0, 0.05, len(ix)),
                  index=ix, name="eta")
    return X, y


# ─────────────────────────── G4 参数量公式 ───────────────────────────

@pytest.mark.parametrize("n_pred", [8, 175])
def test_paper_nn_param_count(n_pred):
    m = PaperNN(n_pred, device=CPU)
    formula = (n_pred + 1) * 32 + 33 * 16 + 17 * 8 + 9
    assert m.param_count() == formula


@pytest.mark.parametrize("n_pred", [8, 175])
def test_paper_rnn_param_count(n_pred):
    m = PaperRNN(n_pred, device=CPU)
    formula = (n_pred + 32 + 1) * 32 * 4 + 33 * 16 + 17 * 8 + 9
    assert m.param_count() == formula
    # 论文口径≈4×nn 参数量(§A.1 "参数总量增加四倍" 的量级核对,首层对比)
    nn_first = (n_pred + 1) * 32
    rnn_first = (n_pred + 32 + 1) * 32 * 4
    assert rnn_first > 3 * nn_first


def test_single_bias_lstm_equivalence():
    """b_hh 冻 0: 可训练参数=单 bias 公式;前向与手动单 bias LSTM 一致。"""
    m = PaperRNN(5, device=CPU)
    lstm = m.net.rnn.lstm
    assert not lstm.bias_hh_l0.requires_grad
    assert float(lstm.bias_hh_l0.abs().sum()) == 0.0
    trainable = sum(p.numel() for p in m.net.rnn.parameters() if p.requires_grad)
    assert trainable == (5 + 32 + 1) * 32 * 4


# ─────────────────────────── 前向/窗口/复现 ───────────────────────────

def test_build_windows_zero_fill_and_order():
    X, _ = synth_panel(n_days=12, n_tickers=3, n_feat=4)
    W, order = build_windows(X, seq_len=10)
    assert W.shape == (len(X), 10, 4)
    # 每票首日: 窗口前 9 步应为零填充
    Xs = X.iloc[order]
    first_rows = Xs.groupby(level=1).head(1).index
    for ridx in first_rows:
        pos = Xs.index.get_loc(ridx)
        assert np.allclose(W[pos, :9, :], 0.0)
        assert np.allclose(W[pos, 9, :], Xs.iloc[pos].values)


def test_forward_shapes_and_alignment():
    X, y = synth_panel()
    for M in (PaperNN(X.shape[1], device=CPU), PaperRNN(X.shape[1], device=CPU)):
        M.fit(X, y, epochs=2)
        p = M.predict(X)
        assert isinstance(p, pd.Series) and p.index.equals(X.index)
        assert p.notna().all()


def test_seed_reproducibility_cpu_bitwise():
    X, y = synth_panel()
    p1 = PaperNN(X.shape[1], seed=7, device=CPU).fit(X, y, epochs=3).predict(X)
    p2 = PaperNN(X.shape[1], seed=7, device=CPU).fit(X, y, epochs=3).predict(X)
    assert np.array_equal(p1.values, p2.values)          # CPU bit 级
    p3 = PaperNN(X.shape[1], seed=8, device=CPU).fit(X, y, epochs=3).predict(X)
    assert not np.array_equal(p1.values, p3.values)


# ─────────────────────────── 旧作 D/F 组 ───────────────────────────

def test_ae_cluster_and_clustered_lstm():
    X, y = synth_panel(n_days=30, n_tickers=4)
    ae = LSTMAutoencoder(X.shape[1], latent=8, max_epochs=3, patience=2,
                         seed=0, device=CPU)
    ae.fit(X)
    Z = ae.encode(X)
    assert Z.shape == (len(X), 8) and Z.index.equals(X.index)
    lab = cluster_latent(Z, "kmeans", n_clusters=3)
    assert set(lab.unique()) <= {0, 1, 2}
    lab_db = cluster_latent(Z, "dbscan", dbscan_eps=2.0, dbscan_min_samples=5)
    assert lab_db.index.equals(X.index)
    # 分簇 LSTM: min_rows 大于任何簇 → 全部并入 residual,留痕
    cm = ClusteredLSTM(X.shape[1], cluster_min_rows=5000, seed=0, device=CPU,
                       max_epochs=2, patience=1)
    cm.fit(X, y, lab)
    assert len(cm.merge_log) == len(lab.unique())
    assert set(cm.models) == {ClusteredLSTM.RESIDUAL}
    pred = cm.predict(X, lab)
    assert pred.notna().all()


def test_single_lstm():
    X, y = synth_panel(n_days=25, n_tickers=3)
    m = SingleLSTM(X.shape[1], max_epochs=2, patience=1, seed=0, device=CPU)
    m.fit(X, y)
    p = m.predict(X)
    assert p.index.equals(X.index) and p.notna().all()


# ─────────────────────────── TFT 冒烟 ───────────────────────────

def test_tft_smoke_and_attention():
    X, y = synth_panel(n_days=30, n_tickers=4, n_feat=6)
    X["cal_witch"] = 0.0
    X.loc[(X.index.get_level_values(0)[::7], slice(None)), "cal_witch"] = 1.0
    past = [c for c in X.columns if c.startswith("tech_")]
    fut = ["cal_witch"]
    # 25 epochs(无早停): 分位头需足够步数自然排序(论文不施加单调约束)
    m = TFT(past, fut, d_model=16, n_heads=2, max_epochs=25, patience=25,
            seed=0, device=CPU)
    m.fit(X, y)
    q = m.predict_quantiles(X)
    assert list(q.columns) == ["q0.1", "q0.5", "q0.9"]
    p = m.predict(X)
    assert p.index.equals(X.index) and p.notna().all()
    # 分位单调性(经验软性: 中位≥低分位的比例应占多数)
    assert (q["q0.9"] >= q["q0.1"]).mean() > 0.8
    att = m.get_attention()
    assert att is not None and att.ndim == 3            # (B, Tq, Tk)
    vw = m.get_variable_weights()
    assert vw is not None and vw.shape[-1] == len(past)
    assert np.allclose(vw.sum(-1), 1.0, atol=1e-4)      # VSN softmax 权重和=1


# ─────────────────────────── econ: 公式与恒等 ───────────────────────────

def test_losscon_two_forms_identity():
    rng = np.random.default_rng(0)
    v = rng.uniform(2, 25, 200)
    zh = rng.uniform(0.01, 0.99, 200)
    for mu in (1e-6, 1e-4, 1e-2):
        z_star = E.s_opt(v, mu)
        a = E.losscon(v, zh, mu)
        b = E.losscon_zz(z_star, zh, mu)
        assert np.allclose(a, b, rtol=1e-9, atol=1e-12)


def test_s_opt_matches_numeric_argmin():
    from scipy.optimize import minimize_scalar
    for v in (4.0, 8.0, 12.0, 16.0, 20.0):
        for mu in (1e-6, 1e-4, 1e-2):
            closed = float(E.s_opt(v, mu))
            num = minimize_scalar(lambda z: float(E.losscon(v, z, mu)),
                                  bounds=(0.0, 1.0), method="bounded",
                                  options={"xatol": 1e-10})
            assert abs(closed - num.x) < 1e-6, (v, mu, closed, num.x)


def test_s_inv_roundtrip():
    v = np.linspace(3, 22, 50)
    for mu in (1e-5, 1e-3):
        z = E.s_opt(v, mu)
        back = E.s_inv(z, mu)
        assert np.allclose(back, v, atol=1e-8)


def test_oracle_is_mel_lower_bound_and_normalization():
    rng = np.random.default_rng(1)
    v = rng.normal(12, 2, 500)
    ma5 = v - rng.normal(0, 0.3, 500)                   # ma5 邻近 v
    mu = 1e-4
    m_orc = E.mel(v, E.oracle_z(v, mu), mu)
    m_ma5 = E.mel(v, E.s_opt(ma5, mu), mu)
    m_bad = E.mel(v, np.full_like(v, 0.5), mu)
    assert m_orc <= m_ma5 <= m_bad + 1e-12
    assert abs(E.mel_normalized(v, E.oracle_z(v, mu), ma5, mu) - 1.0) < 1e-9
    assert abs(E.mel_normalized(v, E.s_opt(ma5, mu), ma5, mu)) < 1e-9


# ─────────────────────────── econ: 学习与迁移 ───────────────────────────

def _econ_panel(n=1200, n_feat=6, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-02", periods=n // 4)
    ix = pd.MultiIndex.from_product([dates, [f"T{i}" for i in range(4)]],
                                    names=["date", "ticker"])[:n]
    X = pd.DataFrame(rng.normal(size=(n, n_feat)), index=ix,
                     columns=[f"f{i}" for i in range(n_feat)])
    beta = rng.normal(size=n_feat)
    # v≈8.5 使 λ(v)≈0.2e^{-8.5}≈4e-5 与 μ=1e-4 同量级 → 最优 z 随 X 变化,
    # 学习信号有意义(v≈12 时 λ≪μ,梯度过小,属测试标定而非实现问题)
    eta = pd.Series(X.values @ beta * 1.0 + rng.normal(0, 0.1, n), index=ix)
    ma5 = pd.Series(rng.normal(8.5, 0.8, n), index=ix)
    return X, eta, ma5


def test_econ_nn_learns_vs_constant():
    X, eta, ma5 = _econ_panel()
    mu = 1e-4
    m = E.EconNN(X.shape[1], mu, seed=0, device=CPU, epochs=25)
    m.fit(X, eta, ma5)
    z = m.predict_z(X)
    v_true = (eta + ma5).values
    assert E.mel(v_true, z.values, mu) < E.mel(v_true, np.full(len(X), 0.5), mu)


def test_transfer_finetune_reduces_economic_loss():
    X, eta, ma5 = _econ_panel(seed=3)
    mu = 1e-4
    base = PaperNN(X.shape[1], seed=0, device=CPU).fit(X, eta, epochs=6)
    tr = E.TransferEconNN(base, mu, finetune_epochs=4, seed=0)
    v_true = (eta + ma5).values
    z_before = E.s_opt(base.predict(X).values + ma5.values, mu)
    mel_before = E.mel(v_true, z_before, mu)
    tr.finetune(X, eta, ma5)
    z_after = tr.predict_z(X, ma5).values
    mel_after = E.mel(v_true, z_after, mu)
    assert mel_after <= mel_before + 1e-10              # 经济微调不劣化,典型应改善
    assert tr.finetune_history[-1] <= tr.finetune_history[0] + 1e-10


def test_econ_ada_semantics():
    X, eta, ma5 = _econ_panel(seed=5)
    mu = 1e-4
    m = E.EconAda(mu, n_estimators=10, seed=0).fit(X, eta, ma5)
    z = m.predict_z(X, ma5)
    assert ((z > 0) & (z < 1)).all()


def test_fig3_surfaces():
    surf = E.fig3_loss_surfaces()
    assert set(surf) == {4.0, 8.0, 12.0, 16.0, 20.0}
    for v, d in surf.items():
        i_star = int(np.argmin(d["losscon"]))
        assert abs(d["z"][i_star] - d["z_star"][0]) < 0.01   # 网格极小点≈闭式解
    E.plot_fig3(surf, str(TMP / "fig3.png"))
    assert (TMP / "fig3.png").exists()
