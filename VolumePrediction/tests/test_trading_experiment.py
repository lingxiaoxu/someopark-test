"""G9 交易实验层测试(DEV_CONTRACTS: 微型合成面板;输出只进 /tmp/vp_tests/)。

全链断言(任务协议):
- 份额演化恒等式 x⁰_t = x_{t-1}·(1+r^raw_t)
- 成本非负
- 换手率(式15)与 AUM 无关
- z=1 时一步到位(x=x*)
- μ→∞ 时 z→1(模拟收敛到 z=1 路径)
附加: 先知信号目标两组各 50% 归一、月调仓目标 50 分位多空归一、端到端工件落 /tmp。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from VolumePrediction.replication_trading import (
    prep_arrays, slice_period, simulate, perf_stats,
    oracle_signal_targets, build_monthly_targets, zoo_factor_columns, run,
)
from VolumePrediction.models.econ import s_opt

TMP = Path("/tmp/vp_tests/trading")
TMP.mkdir(parents=True, exist_ok=True)

N_TICKERS, N_DAYS = 3, 120


def _synth_panel(seed: int = 0) -> pd.DataFrame:
    """3 票 × 120 交易日,横跨 paper split 边界(2021-10 → 2022-03)。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-10-08", periods=N_DAYS)
    tickers = ["AAA", "BBB", "CCC"]
    base_V = {"AAA": 1e8, "BBB": 1e7, "CCC": 1e6}
    rows = []
    for tk in tickers:
        ret = rng.normal(0.0005, 0.02, N_DAYS)
        V = base_V[tk] * np.exp(rng.normal(0, 0.3, N_DAYS))
        v = np.log(V)
        ma5 = pd.Series(v).shift(1).rolling(5).mean().to_numpy()
        for i, d in enumerate(dates):
            rows.append({"date": d, "ticker": tk, "ret": ret[i], "V": V[i],
                         "v": v[i], "ma5_v": ma5[i], "eta": v[i] - ma5[i],
                         "close": 10.0,
                         "fund2_mom_12_1": float(rng.normal()),
                         "fund1_size_ln_mcap": float(rng.normal()),
                         "tech_ret_ma22": float(rng.normal())})
    return pd.DataFrame(rows).set_index(["date", "ticker"]).sort_index()


@pytest.fixture(scope="module")
def arrs():
    return prep_arrays(_synth_panel())


@pytest.fixture(scope="module")
def target(arrs):
    tgt, _ = oracle_signal_targets(arrs["ret"], arrs["present"], p=0.2, seed=3)
    assert np.abs(tgt).sum() > 0, "synthetic signal stream must be non-empty"
    return tgt


# ── 记账协议核心断言 ─────────────────────────────────────────────────────────

def test_share_evolution_identity(arrs, target):
    sim = simulate(arrs["ret"], arrs["V"], target, aum=1e7, mu=1e-6,
                   vhat=arrs["ma5_v"], return_paths=True)
    x, x0 = sim["x_path"], sim["x0_path"]
    ret_fill = np.nan_to_num(arrs["ret"], nan=0.0)
    assert np.allclose(x0[0], 0.0)                        # 首日 x⁰=0(论文注21)
    for t in range(1, len(x)):
        np.testing.assert_allclose(x0[t], x[t - 1] * (1.0 + ret_fill[t]),
                                   rtol=0, atol=1e-14)


def test_costs_nonnegative_and_net_le_gross(arrs, target):
    sim = simulate(arrs["ret"], arrs["V"], target, aum=1e8, mu=1e-4,
                   vhat=arrs["ma5_v"])
    assert (sim["cost_ret"] >= 0).all()
    assert (sim["r_net"] <= sim["r_gross"] + 1e-15).all()
    assert np.isfinite(sim["r_net"]).all()


def test_turnover_independent_of_aum(arrs, target):
    """式15: 给定 μ 与 v̂,换手率(权重空间)不随 AUM 变;成本随 AUM 变。"""
    s1 = simulate(arrs["ret"], arrs["V"], target, aum=1e7, mu=1e-6, vhat=arrs["v"])
    s2 = simulate(arrs["ret"], arrs["V"], target, aum=1e10, mu=1e-6, vhat=arrs["v"])
    np.testing.assert_allclose(s1["turnover"], s2["turnover"], rtol=0, atol=0)
    assert s2["cost_ret"].mean() > s1["cost_ret"].mean()   # 大 AUM 冲击成本占比更高


def test_z_one_reaches_target_in_one_step(arrs, target):
    sim = simulate(arrs["ret"], arrs["V"], target, aum=1e7, z_override=1.0,
                   return_paths=True)
    tgt_fill = np.nan_to_num(target, nan=0.0)
    # 合成面板全 tradable → 每步 x == x*
    np.testing.assert_allclose(sim["x_path"], tgt_fill[:len(sim["x_path"])],
                               rtol=0, atol=1e-14)


def test_mu_inf_converges_to_z_one(arrs, target):
    assert float(s_opt(np.log(1e6), 1e12)) > 1 - 1e-6      # 闭式解端点
    assert float(s_opt(np.log(1e6), float("inf"))) == 1.0
    s_big = simulate(arrs["ret"], arrs["V"], target, aum=1e7, mu=1e12,
                     vhat=arrs["v"], return_paths=True)
    s_one = simulate(arrs["ret"], arrs["V"], target, aum=1e7, z_override=1.0,
                     return_paths=True)
    np.testing.assert_allclose(s_big["x_path"], s_one["x_path"], atol=1e-6)
    np.testing.assert_allclose(s_big["r_gross"], s_one["r_gross"], atol=1e-6)


def test_missing_vhat_means_no_trade(arrs, target):
    vhat = arrs["ma5_v"].copy()
    vhat[:, 0] = np.nan                                    # 第 1 票 v̂ 全缺
    sim = simulate(arrs["ret"], arrs["V"], target, aum=1e7, mu=1e-3, vhat=vhat,
                   return_paths=True)
    assert np.allclose(sim["x_path"][:, 0], 0.0)           # 起始 0 且永不交易


# ── 实验目标构造 ─────────────────────────────────────────────────────────────

def test_oracle_signal_group_normalization(arrs, target):
    pos = np.where(target > 0, target, 0.0).sum(axis=1)
    neg = np.where(target < 0, target, 0.0).sum(axis=1)
    for s, ref in ((pos, 0.5), (neg, -0.5)):
        active = np.abs(s) > 1e-12
        assert np.allclose(s[active], ref, atol=1e-12)     # 各组恰 50% AUM


def test_oracle_signal_direction_is_prescient(arrs):
    """信号方向 = 未来 5 日对数收益和的符号(先知定义自洽)。"""
    tgt, info = oracle_signal_targets(arrs["ret"], arrs["present"], p=1.0, seed=1)
    assert info["n_signal_starts"] > 0
    logret = np.log1p(np.nan_to_num(arrs["ret"], nan=0.0))
    T = len(logret)
    t = 30                                                  # p=1 → 每日新信号覆盖
    fwd = logret[t + 1:t + 6].sum(axis=0)
    np.testing.assert_array_equal(np.sign(tgt[t]), np.sign(fwd))


def test_monthly_targets_median_split(arrs):
    dates = arrs["dates"]
    month = pd.DatetimeIndex(dates).to_period("M")
    rb_idx = [0] + [i for i in range(1, len(dates)) if month[i] != month[i - 1]]
    rng = np.random.default_rng(5)
    fw = pd.DataFrame(rng.normal(size=(len(rb_idx), len(arrs["tickers"]))),
                      index=dates[rb_idx], columns=arrs["tickers"])
    tgt, info = build_monthly_targets(arrs, fw, min_names=2)
    assert info["n_effective"] == len(rb_idx)
    assert np.allclose(np.where(tgt > 0, tgt, 0).sum(axis=1), 0.5)
    assert np.allclose(np.where(tgt < 0, tgt, 0).sum(axis=1), -0.5)
    for k, ridx in enumerate(rb_idx):                       # 月内目标恒定
        end = rb_idx[k + 1] if k + 1 < len(rb_idx) else len(dates)
        assert (tgt[ridx:end] == tgt[ridx]).all()


def test_zoo_factor_columns_declaration():
    panel = _synth_panel()
    cols, decls = zoo_factor_columns(panel)
    assert set(cols) == {"fund2_mom_12_1", "fund1_size_ln_mcap", "tech_ret_ma22"}
    assert decls and "fund2" in decls[0]


# ── 端到端(工件只进 /tmp) ──────────────────────────────────────────────────

def test_end_to_end_artifacts_tmp_only():
    out = TMP / "e2e"
    res = run(panel_tag="synthetic", quick=True, seed=3, panel=_synth_panel(),
              out_dir=out, zoo_min_names=2)
    for f in ("fig5_data.csv", "fig5_oracle_signal.png", "table3_trading.csv",
              "factor_zoo_c3.csv", "factor_zoo_c4.csv", "fig7_factor_zoo.png",
              "trading_summary.json"):
        assert (out / f).exists(), f
    summ = json.loads((out / "trading_summary.json").read_text())
    assert summ["tiers"] == ["ma5", "oracle"] or "model" in summ["tiers"]
    assert any("eta predictions" in d for d in summ["declarations"])
    t3 = pd.read_csv(out / "table3_trading.csv")
    assert "caveat" in t3.columns and len(t3) >= 3          # ma5/oracle/gross_z1
    fig5 = pd.read_csv(out / "fig5_data.csv")
    assert set(fig5["tier"]) >= {"ma5", "oracle"}
    assert (fig5.groupby("tier")["mu"].count() >= 2).all()
