"""Cross-venue funding research tests — synthetic, no network."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_strategies.funding_carry import research_cross_venue as cv


def test_nonzero_streaks_skip_zeros_and_reset_on_flip():
    idx = pd.date_range("2026-06-04 04:00", periods=8, freq="8h", tz="UTC")
    r = pd.Series([-1e-4, 0.0, -1e-4, -1e-4, 0.0, 1e-4, 1e-4, -1e-4], index=idx)
    s = cv.nonzero_streaks(r)
    # zeros carry the streak value; sign flip resets to 1 with new sign
    assert list(s) == [-1, -1, -2, -3, -3, 1, 2, -1]


def test_daily_alignment_across_grids():
    # Kalshi 04/12/20 vs OKX 00/08/16 on the same UTC day → both sum to that day
    k_idx = pd.to_datetime(["2026-07-01 04:00", "2026-07-01 12:00", "2026-07-01 20:00"], utc=True)
    o_idx = pd.to_datetime(["2026-07-01 00:00", "2026-07-01 08:00", "2026-07-01 16:00"], utc=True)
    kd = cv.daily_funding(pd.Series([1e-4] * 3, index=k_idx))
    od = cv.daily_funding(pd.Series([2e-4] * 3, index=o_idx))
    day = pd.Timestamp("2026-07-01", tz="UTC")
    assert kd[day] == pytest.approx(3e-4) and od[day] == pytest.approx(6e-4)
    assert list(kd.index) == list(od.index)          # same daily grid


def test_streak_rule_collects_differential_net_of_fees(monkeypatch):
    # Kalshi persistently NEGATIVE (long collects), OKX slightly positive
    days = pd.date_range("2026-06-10", periods=40, freq="1D", tz="UTC")
    kd = pd.Series(-3e-4, index=days)                # −3bps/day Kalshi
    od = pd.Series(+1e-4, index=days)                # +1bps/day OKX
    cyc = pd.date_range("2026-06-08 04:00", periods=130, freq="8h", tz="UTC")
    k_cycles = pd.Series(-1e-4, index=cyc)           # perfect negative streak
    monkeypatch.setattr(cv, "load_pair", lambda t: (kd, od))
    monkeypatch.setattr(cv, "load_funding",
                        lambda t: pd.DataFrame({"funding_rate": k_cycles}))
    r = cv.streak_rule_backtest("KXBCHPERP", enter_k=6)
    assert r["entries"] == 1                          # one persistent episode
    # long Kalshi (−k = +3bps) + short OKX (+o = +1bps) = 4bps/day gross
    assert r["gross_total"] == pytest.approx(4e-4 * r["days_in"], rel=1e-6)
    assert r["net_total"] < r["gross_total"]          # fees deducted once
    assert r["nw_t_gross_daily"] > 0


def test_fee_amortization_one_round_trip_per_entry(monkeypatch):
    days = pd.date_range("2026-06-10", periods=10, freq="1D", tz="UTC")
    monkeypatch.setattr(cv, "load_pair",
                        lambda t: (pd.Series(-1e-4, index=days), pd.Series(0.0, index=days)))
    cyc = pd.date_range("2026-06-08 04:00", periods=40, freq="8h", tz="UTC")
    monkeypatch.setattr(cv, "load_funding",
                        lambda t: pd.DataFrame({"funding_rate": pd.Series(-1e-4, index=cyc)}))
    r = cv.streak_rule_backtest("KXBCHPERP", enter_k=6)
    expected_fees = 1 * (2 * cv.KALSHI_FEE + 2 * cv.OKX_FEE)
    assert r["fees_total"] == pytest.approx(expected_fees)


def test_sign_consistency():
    r = pd.Series([-1, -1, -1, 0, 0, 1], dtype=float)
    assert cv.sign_consistency(r) == pytest.approx(3 / 4)    # 3 of 4 nonzero same sign
    assert cv.sign_consistency(pd.Series([0.0, 0.0])) == 0.0
