"""econ/execution 层测试(DEV_CONTRACTS: 小合成数据;输出只进 /tmp/vp_tests/)。"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from VolumePrediction.econ import policy, calibration, objective as obj
from VolumePrediction.execution.scheduler import schedule_single, evolve_x0
from VolumePrediction.execution.leg_joint import schedule_pair
from VolumePrediction.execution.basket import schedule_basket

TMP = Path("/tmp/vp_tests/econ")
TMP.mkdir(parents=True, exist_ok=True)


# ── policy ───────────────────────────────────────────────────────────────────
def _numeric_argmin(v, mu, form):
    zs = np.linspace(0, 1, 2_000_001)
    losses = policy.lambda_of_v(v, form) * zs**2 + mu * (1 - zs) ** 2
    return zs[int(np.argmin(losses))]


@pytest.mark.parametrize("v,mu", [(10.0, 1e-6), (14.0, 1e-4), (18.0, 1e-3),
                                  (12.0, 1e-8), (20.0, 1e-5)])
def test_closed_form_matches_numeric(v, mu):
    for form in (policy.FORM_MAIN, policy.FORM_SQRT):
        z_star = policy.s_opt(v, mu, form)
        z_num = _numeric_argmin(v, mu, form)
        assert abs(z_star - z_num) < 1e-6, (v, mu, form, z_star, z_num)


def test_s_opt_sigmoid_identity_and_bounds():
    v, mu = 15.0, 1e-5
    sig = 1.0 / (1.0 + math.exp(-v + math.log(0.2) - math.log(mu)))
    assert abs(policy.s_opt(v, mu) - sig) < 1e-12
    assert policy.s_opt(v, float("inf")) == 1.0
    assert policy.s_opt(v, 0.0) == 0.0


def test_losscon_inf_semantics():
    assert math.isinf(policy.losscon(12.0, 0.5, float("inf")))
    assert math.isfinite(policy.losscon(12.0, 1.0, float("inf")))


def test_constrained_capping_flag():
    # 极高 μ → z*≈1;小成交量+大目标 → cap 激活
    cr = policy.s_opt_constrained(v_bar=math.log(1e6), mu=1.0,
                                  target_dollars=5e6, pred_dollar_volume=1e6,
                                  participation_cap=0.2)
    assert cr.capped and abs(cr.cap - 0.2 * 1e6 / 5e6) < 1e-12
    assert cr.z == cr.cap and "participation cap" in cr.reason
    cr2 = policy.s_opt_constrained(v_bar=math.log(1e9), mu=1e-6,
                                   target_dollars=1e4, pred_dollar_volume=1e9,
                                   participation_cap=0.2)
    assert not cr2.capped


def test_solve_urgent_conservation_and_cap():
    adv = pd.Series([100_000.0, 80_000.0, 120_000.0, 90_000.0],
                    index=pd.bdate_range("2026-07-01", periods=4))
    df = policy.solve_urgent(50_000, adv, participation_cap=0.2)
    assert abs(df["shares"].sum() - 50_000) < 1e-6          # 守恒
    caps = 0.2 * adv.loc[df["date"]].values
    assert (df["shares"].values <= caps + 1e-9).all()        # cap 遵守
    assert df.attrs["completed"]
    # 前两天应打满 cap(贪心)
    assert abs(df["shares"].iloc[0] - caps[0]) < 1e-9


def test_solve_urgent_incomplete_flag():
    adv = pd.Series([1000.0, 1000.0], index=pd.bdate_range("2026-07-01", periods=2))
    df = policy.solve_urgent(10_000, adv, participation_cap=0.2, max_days=2)
    assert not df.attrs["completed"] and df.attrs["remaining"] > 0


def test_mu_from_aum_monotone():
    grid = [1e-9, 1e-8, 1e-7, 1e-6, 1e-5, 1e-4, 1e-3]
    aums = [1e7, 1e8, 1e9, 1e10]
    mus = [policy.mu_from_aum(a, aums, grid) for a in aums]
    assert all(mus[i] > mus[i + 1] for i in range(len(mus) - 1))   # AUM↑ → μ↓


# ── objective ────────────────────────────────────────────────────────────────
def test_profile_registry_six_profiles():
    reg = obj.registry()
    for name in ("pairs_entry", "pairs_exit", "pairs_stop",
                 "aiss_rebalance", "aiss_emergency", "ssrs_rebalance"):
        assert name in reg, name
    assert reg["pairs_stop"].is_urgent
    assert reg["aiss_rebalance"].mode == "tracking"
    assert reg["pairs_entry"].constraints.get("leg_joint") is True


def test_resolve_mapping_and_errors():
    assert obj.resolve(strategy="mtfs", trade_type="stop").name == "pairs_stop"
    assert obj.resolve(strategy="AISS", trade_type="rebalance").name == "aiss_rebalance"
    assert obj.resolve(objective="ssrs_rebalance").name == "ssrs_rebalance"
    with pytest.raises(KeyError):
        obj.resolve(objective="nope")
    with pytest.raises(ValueError):
        obj.resolve()


def test_resolve_mu_paths(tmp_path=None):
    art = TMP / "art_empty"
    import shutil
    shutil.rmtree(art, ignore_errors=True)   # 幂等: 清除上轮残留(本测试后半会写工件)
    (art / "registry").mkdir(parents=True, exist_ok=True)
    reg = obj.registry()
    mu, src = obj.resolve_mu(reg["pairs_stop"], artifacts_dir=art)
    assert math.isinf(mu) and src == "inf"
    mu, src = obj.resolve_mu(reg["pairs_entry"], artifacts_dir=art)
    assert src == "paper_prior" and mu > 0                     # 缺工件冷启动
    mu, src = obj.resolve_mu(reg["pairs_entry"], aum=1e8, artifacts_dir=art)
    assert src == "paper_prior_aum_map"
    # 写校准工件后应命中
    (art / "registry" / "mu_calibration.json").write_text(
        json.dumps({"pairs_decay": {"mu": 3.3e-4,
                                    "calibration_source": "fills_regression"}}))
    mu, src = obj.resolve_mu(reg["pairs_entry"], artifacts_dir=art)
    assert abs(mu - 3.3e-4) < 1e-12 and src == "fills_regression"


# ── calibration ──────────────────────────────────────────────────────────────
def test_calibrate_mu_pairs_recovers_slope():
    rng = np.random.default_rng(0)
    n = 400
    delay = rng.integers(0, 6, n)
    z = rng.uniform(1.5, 3.0, n)
    true_slope = -0.002
    y = 0.01 * z + true_slope * delay + rng.normal(0, 0.0005, n)
    df = pd.DataFrame({"z_score": z, "delay_days": delay, "realized_pnl_frac": y})
    res = calibration.calibrate_mu_pairs(df)
    assert res["calibration_source"] == "fills_regression"
    assert abs(res["mu"] - 0.002) < 3e-4 and res["n"] == n


def test_calibrate_mu_pairs_insufficient():
    df = pd.DataFrame({"z_score": [2.0] * 10, "delay_days": [1] * 10,
                       "realized_pnl_frac": [0.001] * 10})
    res = calibration.calibrate_mu_pairs(df)
    assert res["calibration_source"] == "paper_prior" and res["n"] == 10


def test_calibrate_lambda_recovers_params():
    rng = np.random.default_rng(1)
    n = 300
    part = rng.uniform(0.001, 0.2, n)
    k, expn = 0.1, 1.0
    impact = k * part**expn * np.exp(rng.normal(0, 0.05, n))
    res = calibration.calibrate_lambda_from_fills(
        pd.DataFrame({"participation": part, "impact": impact}))
    assert res["calibration_source"] == "fills_regression"
    assert abs(res["k"] - k) < 0.02 and abs(res["exponent"] - expn) < 0.05


def test_calibrate_lambda_insufficient():
    res = calibration.calibrate_lambda_from_fills(pd.DataFrame())
    assert res["calibration_source"] == "paper_prior"
    assert res["k"] == 0.1 and res["exponent"] == 1.0


def test_calibrate_mu_momentum():
    curve = pd.Series([1.0, 0.995, 0.991, 0.988, 0.986], index=range(5))
    res = calibration.calibrate_mu_momentum(curve)
    assert res["calibration_source"] == "alpha_decay_curve"
    assert 0.001 < res["mu"] < 0.01


# ── scheduler ────────────────────────────────────────────────────────────────
def _mk_profile(**kw):
    base = dict(name="t", mode="tracking", mu_source="calibrated",
                constraints={"adv_cap": 0.2})
    base.update(kw)
    return obj.Profile(**base)


def test_schedule_single_tracking_geometric():
    adv = pd.Series(1e6, index=pd.bdate_range("2026-07-01", periods=10))
    prof = _mk_profile()
    df = schedule_single("XYZ", 10_000, 50.0, adv, prof, mu=1e-4, max_days=10)
    # 每日 z 相同(平推预测) → 剩余几何衰减
    zs = df["z"].values
    assert np.allclose(zs, zs[0])
    assert (np.diff(df["remaining_after"].values) < 0).all()
    assert (df["participation"] <= 0.2 + 1e-9).all()


def test_schedule_single_urgent_path():
    adv = pd.Series(10_000.0, index=pd.bdate_range("2026-07-01", periods=10))
    prof = _mk_profile(name="u", mode="urgent", mu_source="inf")
    df = schedule_single("XYZ", 5_000, 20.0, adv, prof, mu=float("inf"))
    assert df.attrs["mode"] == "urgent"
    assert abs(df["shares"].sum() - 5_000) < 1e-6
    assert (df["participation"] <= 0.2 + 1e-9).all()
    assert "est_cost_dollars" in df.columns


def test_evolve_x0():
    assert abs(evolve_x0(100.0, 0.02) - 102.0) < 1e-12


# ── leg_joint ────────────────────────────────────────────────────────────────
def test_leg_joint_hedge_sync_and_completion():
    idx = pd.bdate_range("2026-07-01", periods=15)
    adv1 = pd.Series(50_000.0, index=idx)    # 流动腿
    adv2 = pd.Series(5_000.0, index=idx)     # 瓶颈腿
    df = schedule_pair("AAA", "BBB", 10_000, 4_000, 30.0, 60.0,
                       adv1, adv2, participation_cap=0.2, max_days=15)
    # 对冲同步不变式: 两腿逐日比例一致
    f1 = df["s1_shares"] / 10_000
    f2 = df["s2_shares"] / 4_000
    assert np.allclose(f1.values, df["frac"].values)
    assert np.allclose(f2.values, df["frac"].values)
    assert df.attrs["completed"]
    assert df.attrs["bottleneck_leg"] == "BBB"
    assert abs(df["s1_shares"].sum() - 10_000) < 1e-6
    assert abs(df["s2_shares"].sum() - 4_000) < 1e-6
    assert (df["s2_participation"] <= 0.2 + 1e-9).all()


def test_leg_joint_validates_targets():
    idx = pd.bdate_range("2026-07-01", periods=3)
    with pytest.raises(ValueError):
        schedule_pair("A", "B", 0, 100, 1.0, 1.0,
                      pd.Series(1.0, index=idx), pd.Series(1.0, index=idx))


# ── basket ───────────────────────────────────────────────────────────────────
def _basket_inputs():
    idx = pd.bdate_range("2026-07-01", periods=12)
    trades = [{"ticker": "NVDA", "shares": 1000},
              {"ticker": "ARM", "shares": -6000},
              {"ticker": "MU", "shares": 0}]          # 0 股应被剔除
    prices = {"NVDA": 100.0, "ARM": 150.0}
    adv = {"NVDA": pd.Series(1e6, index=idx), "ARM": pd.Series(10_000.0, index=idx)}
    return trades, prices, adv


def test_basket_independent():
    trades, prices, adv = _basket_inputs()
    prof = _mk_profile(name="u", mode="urgent", mu_source="inf",
                       constraints={"adv_cap": 0.2, "basket": True})
    out = schedule_basket(trades, prices, adv, prof, mu=float("inf"),
                          align="independent", max_days=12)
    assert set(out["per_ticker"]) == {"NVDA", "ARM"}
    s = out["summary"].set_index("ticker")
    assert s.loc["ARM", "direction"] == "SELL"
    assert s.loc["NVDA", "days"] == 1                 # 流动票一天完成
    assert s.loc["ARM", "days"] == 3                  # 6000/(0.2×10000)=3
    assert bool(s.loc["ARM", "completed"])


def test_basket_aligned_common_fraction():
    trades, prices, adv = _basket_inputs()
    prof = _mk_profile(constraints={"adv_cap": 0.2, "basket": True})
    out = schedule_basket(trades, prices, adv, prof, mu=1e-4,
                          align="aligned", max_days=12)
    nv, ar = out["per_ticker"]["NVDA"], out["per_ticker"]["ARM"]
    assert np.allclose(nv["frac"].values, ar["frac"].values)   # 公共比例
    assert bool(ar.attrs["completed"])


def test_basket_rejects_bad_align():
    trades, prices, adv = _basket_inputs()
    prof = _mk_profile()
    with pytest.raises(ValueError):
        schedule_basket(trades, prices, adv, prof, mu=1e-4, align="wat")
