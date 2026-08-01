"""VolumeService 门面测试(合成工件/raw 全部在 /tmp/vp_tests/service/)。"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from VolumePrediction.service import VolumeService
from VolumePrediction.outputs.recorder import Registry, RunRecorder

TMP = Path("/tmp/vp_tests/service")
ART = TMP / "artifacts"
RAW = TMP / "raw"
TICKERS = ["AAA", "BBB", "CCC"]


def _build_raw(n_days: int = 40, seed: int = 7) -> list[str]:
    RAW.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    dates = [d.strftime("%Y-%m-%d")
             for d in pd.bdate_range("2026-05-01", periods=n_days)]
    base_v = {"AAA": 1e6, "BBB": 5e4, "CCC": 2e5}
    base_p = {"AAA": 100.0, "BBB": 50.0, "CCC": 20.0}
    for i, d in enumerate(dates):
        rows = []
        for tk in TICKERS:
            shares = base_v[tk] * math.exp(rng.normal(0, 0.15))
            # 最后一天给 AAA 一个巨量+价格企稳(测试 capitulation)
            if tk == "AAA" and i == n_days - 1:
                shares = base_v[tk] * 30
            price = base_p[tk] * (1 + 0.001 * i)
            rows.append({"ticker": tk, "v": shares, "vw": price,
                         "o": price, "h": price, "l": price, "c": price,
                         "t": 0, "n": 100, "date": d})
        pd.DataFrame(rows).to_parquet(RAW / f"grouped_{d}.parquet", index=False)
    return dates


def _build_artifacts(dates: list[str]) -> None:
    (ART / "history").mkdir(parents=True, exist_ok=True)
    (ART / "registry").mkdir(parents=True, exist_ok=True)
    asof = dates[-1]
    rows = []
    for tk, pv in (("AAA", 1e6 * 100), ("BBB", 5e4 * 50)):     # CCC 缺 → 回退路径
        rows.append({"date": asof, "ticker": tk, "pred_v": math.log(pv),
                     "pred_V": pv, "pred_eta": 0.0,
                     "model_version": "test.v1", "trained_through": asof,
                     "generated_at": "test"})
    df = pd.DataFrame(rows)
    df.to_parquet(ART / "volume_forecast_latest.parquet", index=False)
    df.to_parquet(ART / "history" / f"volume_forecast_{asof}.parquet", index=False)
    Registry(ART).record_model("test.v1", kind="test", trained_through=asof,
                               resid_std=0.4, status="production")
    Registry(ART).promote("test.v1", by="test")


@pytest.fixture(scope="module")
def svc():
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    dates = _build_raw()
    _build_artifacts(dates)
    s = VolumeService(artifacts_dir=ART, raw_dir=RAW)
    s._dates = dates
    return s


# ── forecast ─────────────────────────────────────────────────────────────────
def test_forecast_get_model_and_fallback(svc):
    df = svc.forecast.get(TICKERS)
    assert set(df["symbol"]) == set(TICKERS)
    by = df.set_index("symbol")
    assert by.loc["AAA", "source"] == "model"
    assert by.loc["CCC", "source"] == "fallback_ma5"       # 工件缺票 → ma5 回退
    assert math.isfinite(by.loc["CCC", "pred_V"])
    assert by.loc["AAA", "ci_high"] > by.loc["AAA", "pred_v"]


def test_forecast_horizon_gt1_raises(svc):
    with pytest.raises(NotImplementedError):
        svc.forecast.get("AAA", horizon=2)


def test_forecast_baselines_four(svc):
    b = svc.forecast.baselines(["AAA"]).iloc[0]
    for k in ("ma5", "lag1", "ma22", "ma252"):
        assert k in b
    assert math.isfinite(b["ma5"]) and math.isfinite(b["lag1"])


def test_forecast_history_and_distribution(svc):
    d = svc._dates[-1]
    h = svc.forecast.history(d, d)
    assert len(h) == 2
    dist = svc.forecast.distribution("AAA")
    r = dist.iloc[0]
    assert r["v_q05"] < r["v_q50"] < r["v_q95"]


def test_feature_snapshot_graceful_none(svc):
    assert svc.forecast.feature_snapshot("AAA", svc._dates[-1]) is None


def test_list_models_and_info(svc):
    lm = svc.forecast.list_models()
    assert "test.v1" in set(lm["version"])
    assert lm.attrs["production"] == "test.v1"
    assert svc.forecast.model_info("test.v1")["kind"] == "test"


# ── econ ─────────────────────────────────────────────────────────────────────
def test_econ_price_impact_and_curve(svc):
    r = svc.econ.price_impact("AAA", 1e6)
    assert r["source"] == "model" and r["cost_dollars"] > 0
    assert abs(r["cost_dollars"] - 0.1 * 1e6 * 1e6 / r["V_hat"]) < 1e-6
    cc = svc.econ.cost_curve("AAA", amounts=[1e5, 1e6])
    assert (cc["cost_main"].diff().dropna() > 0).all()      # 凸性
    assert (cc["cost_sqrt"] > 0).all()


def test_econ_optimal_trade_rate_paths(svc):
    r = svc.econ.optimal_trade_rate("AAA", x0=0, x_star=1e6, mu=1e-4)
    assert 0 < r["z"] <= 1 and r["mu_source"] == "explicit"
    r2 = svc.econ.optimal_trade_rate("AAA", x0=0, x_star=1e6,
                                     objective="pairs_stop")
    assert r2["z"] == 1.0 or r2["capped"]                   # urgent → 尽量全量
    r3 = svc.econ.optimal_trade_rate("AAA", x0=0, x_star=1e6, aum=1e9)
    assert r3["mu_source"] == "paper_prior_aum_map"


def test_econ_calibrations_write_registry(svc):
    rng = np.random.default_rng(3)
    n = 200
    df = pd.DataFrame({"z_score": rng.uniform(1.5, 3, n),
                       "delay_days": rng.integers(0, 5, n),
                       "realized_pnl_frac": -0.003 * rng.integers(0, 5, n)
                       + rng.normal(0, 1e-4, n)})
    res = svc.econ.calibrate_mu(strategy="mrpt", signal_history_df=df)
    assert res["mu_key"] == "pairs_decay" and res["n"] == n
    saved = json.loads((ART / "registry" / "mu_calibration.json").read_text())
    assert "pairs_decay" in saved
    lam = svc.econ.calibrate_lambda_from_fills("mrpt")       # 无 fills → 先验
    assert lam["calibration_source"] == "paper_prior"


def test_economic_loss(svc):
    assert svc.econ.economic_loss(12.0, 1.0, 1e-4) > 0


# ── execute ──────────────────────────────────────────────────────────────────
def test_execute_advice_and_schedule(svc):
    a = svc.execute.advice("BBB", 20_000, strategy="mrpt", trade_type="stop")
    assert a["urgent"] and a["profile"] == "pairs_stop"
    sch = svc.execute.schedule("BBB", 20_000, strategy="mrpt", trade_type="stop",
                               max_days=10)
    assert abs(sch["shares"].sum() - 20_000) < 1e-6
    assert (sch["participation"] <= 0.2 + 1e-9).all()
    sch2 = svc.execute.schedule("AAA", 1_000, objective="aiss_rebalance")
    assert sch2.attrs["profile"] == "aiss_rebalance"
    assert sch2.attrs["mu_source"] in ("paper_prior", "fills_regression",
                                       "paper_prior_aum_map")


def test_execute_caps_and_dtl(svc):
    cap = svc.execute.participation_cap("AAA")
    assert cap["max_shares_per_day"] > 0
    dtl = svc.execute.days_to_liquidate("BBB", 50_000)
    assert dtl["days"] > 1
    bad = svc.execute.days_to_liquidate("ZZZ_NOPE", 100)
    assert bad["days"] is None and bad["source"] == "unavailable"


# ── tca ──────────────────────────────────────────────────────────────────────
def test_tca_record_fill_append_only(svc):
    p = ART / "fills" / "fills_test.jsonl"
    svc.tca.record_fill("test", "AAA", svc._dates[-1], 100, 100.0)
    svc.tca.record_fill("test", "BBB", svc._dates[-1], -50, 50.0)
    lines = p.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["side"] == "SELL"
    rep = svc.tca.implementation_report("test")
    assert rep["n"] == 2 and rep["gross_traded_dollars"] > 0
    rvp = svc.tca.realized_vs_predicted("test")
    assert rvp["n"] == 2 and rvp["n_matched"] >= 1


def test_tca_empty_graceful(svc):
    assert svc.tca.realized_vs_predicted("nope")["n"] == 0
    assert svc.tca.implementation_report("nope")["n"] == 0


# ── signals ──────────────────────────────────────────────────────────────────
def test_signals_capitulation_and_abnormal(svc):
    cap = svc.signals.capitulation(TICKERS).set_index("symbol")
    assert bool(cap.loc["AAA", "capitulation"])            # 巨量+价稳 → 触发
    assert not bool(cap.loc["BBB", "capitulation"])
    ab = svc.signals.abnormal_volume(["AAA"]).iloc[0]
    assert ab["abnormal"] and ab["eta_z"] > 2.5


def test_signals_outlook_and_calendar(svc):
    o = svc.signals.market_liquidity_outlook()
    assert o["source"] == "raw" and len(o["recent"]) > 0
    cal = svc.signals.upcoming_calendar(date="2026-06-15", horizon=5)
    # 2026-06-19 为 6 月第三个周五 → triple witching
    tw = cal.set_index("date").loc["2026-06-19"]
    assert bool(tw["triple_witching"])
    cal2 = svc.signals.upcoming_calendar(date="2026-06-22", horizon=5)
    rr = cal2.set_index("date").loc["2026-06-26"]
    assert bool(rr["russell_rebalance"])                   # 第 4 个周五


# ── adv ──────────────────────────────────────────────────────────────────────
def test_adv_blend_and_fallback(svc):
    v_model = svc.adv.get_adv_forecast("AAA")              # 有工件 → blend
    info = svc.adv.info("AAA")
    assert info["source"] == "blend" and v_model > 0
    info_c = svc.adv.info("CCC")                           # 无工件票 → trailing
    assert info_c["source"] == "fallback_trailing"
    assert math.isnan(svc.adv.get_adv_forecast("ZZZ_NOPE"))
    b = svc.adv.batch(TICKERS)
    assert set(b) == set(TICKERS)


# ── ops ──────────────────────────────────────────────────────────────────────
def test_ops_health_refresh_promote_coverage(svc):
    h = svc.ops.health()
    assert h["model_version"] == "test.v1" and h["coverage_n"] == 2
    r = svc.ops.refresh(fetch=False)                       # 不打网络
    assert r["status"] == "ok" and r["n"] == len(TICKERS)
    art = pd.read_parquet(ART / "volume_forecast_latest.parquet")
    assert set(art["ticker"]) == set(TICKERS)
    assert (ART / "history" / f"volume_forecast_{r['asof']}.parquet").exists()
    h2 = svc.ops.health()
    assert h2["coverage_n"] == 3
    req = svc.ops.retrain({"note": "t"})
    assert req["status"] == "requested"
    cov = svc.ops.coverage()
    assert cov["raw_days"] == 40
    prom = svc.ops.promote("baselines.ma5", by="tester")
    assert prom["version"] == "baselines.ma5"


def test_degrade_no_artifacts_at_all():
    """全新空 artifacts → 全链降级不抛异常。"""
    art2 = TMP / "art_empty2"
    art2.mkdir(parents=True, exist_ok=True)
    s = VolumeService(artifacts_dir=art2, raw_dir=RAW)
    f = s.forecast.get("AAA")
    assert f.iloc[0]["source"] == "fallback_ma5"
    assert s.adv.info("AAA")["source"] == "fallback_trailing"
    h = s.ops.health()
    assert h["stale"] is True and h["fresh_through"] is None


# ── 零重依赖(§5.1 硬约束) ────────────────────────────────────────────────────
def test_service_import_no_heavy_deps():
    code = ("import sys; sys.path.insert(0, %r); "
            "import VolumePrediction.service; "
            "bad=[m for m in ('torch','sklearn','lightgbm','shap','requests',"
            "'pymongo','statsmodels') if m in sys.modules]; "
            "print('BAD:'+','.join(bad) if bad else 'CLEAN')"
            ) % str(Path(__file__).resolve().parents[2])
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=120)
    assert "CLEAN" in out.stdout, out.stdout + out.stderr


# ── recorder ─────────────────────────────────────────────────────────────────
def test_run_recorder(tmp_path=None):
    rec = RunRecorder(TMP / "rec")
    df = pd.DataFrame({"model": ["ols", "nn"], "oos_r2": [0.03, 0.18]})
    p = rec.record_results_table(df.set_index("model"), "unit")
    assert p.exists() and p.with_suffix(".md").exists()
    cmp_ = rec.comparison_table({"a": df, "b": df})
    assert "a:oos_r2" in cmp_.columns and "b:oos_r2" in cmp_.columns
    meta = rec.save_training_meta({"seeds": 5}, "unit")
    assert json.loads(meta.read_text())["seeds"] == 5
