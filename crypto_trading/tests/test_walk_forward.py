"""walk_forward.py tests — synthetic, no network.

Covers: (a) fold generation lengths + embargo on a 24/7 daily grid;
(b) full WF loop with a fake injected engine picks the known-best param set;
(c) DSR collapses significance vs naive Sharpe under many-trial adjustment;
(d) WFE = OOS_SR / IS_SR exactly as the module computes metrics.
"""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common.walk_forward import (WalkForwardAnalyzer,
                                                       _compute_metrics_from_equity,
                                                       deflated_sharpe_ratio,
                                                       expected_max_sharpe)

IDX = pd.date_range("2026-01-01", periods=365, freq="D", tz="UTC")  # 24/7 daily grid


def _equity(daily_rets: np.ndarray) -> pd.Series:
    return pd.Series((1 + pd.Series(daily_rets, index=IDX)).cumprod(), index=IDX)


def _good_engine_returns() -> np.ndarray:
    # deterministic, std > 0, strongly positive: alternate +0.3% / +0.1%
    r = np.full(len(IDX), 0.001)
    r[::2] = 0.003
    return r


def _bad_engine_returns() -> np.ndarray:
    # deterministic, negative drift: alternate −0.2% / +0.05%
    r = np.full(len(IDX), 0.0005)
    r[::2] = -0.002
    return r


def _fake_run_backtest(params, start, end):
    r = _good_engine_returns() if params["kind"] == "good" else _bad_engine_returns()
    return {"equity_curve": _equity(r)}


PARAM_SETS = {"good_set": {"kind": "good"}, "bad_set": {"kind": "bad"}}
PRICES = pd.DataFrame({"close": np.linspace(100, 120, len(IDX))}, index=IDX)
MACRO = pd.DataFrame({"btc_rvol": np.full(len(IDX), 40.0),
                      "funding": np.full(len(IDX), 1e-4)}, index=IDX)


def _analyzer(**kw):
    defaults = dict(is_days_min=90, oos_days=14, step_days=7, embargo_hours=48,
                    mode="anchored")
    defaults.update(kw)
    return WalkForwardAnalyzer(_fake_run_backtest, PARAM_SETS, PRICES, MACRO,
                               **defaults)


# ── (a) fold generation ─────────────────────────────────────────────────────

def test_fold_generation_lengths_and_embargo():
    an = _analyzer()                       # embargo 48h → 2 daily steps
    folds = an.generate_folds()
    assert folds, "no folds generated"
    f = folds[0]
    # first IS window: exactly the minimum, anchored at series start
    assert f.is_start == IDX[0]
    assert (f.oos_start - f.is_end).days == 3      # embargo 2 days + 1 (exclusive gap)
    assert (f.embargo_end - f.is_end).days == 2    # 48h → 2 daily steps
    is_len = len(PRICES.loc[f.is_start:f.is_end])
    oos_len = len(PRICES.loc[f.oos_start:f.oos_end])
    assert is_len >= 90 - 2 and oos_len == 14
    # consecutive folds step forward by step_days
    assert (folds[1].oos_start - folds[0].oos_start).days == 7
    # every fold: IS strictly before embargo strictly before OOS
    for f in folds:
        assert f.is_end < f.oos_start
        assert f.is_end <= f.embargo_end < f.oos_start


def test_rolling_mode_fixed_is_width():
    an = _analyzer(mode="rolling")
    folds = an.generate_folds()
    widths = [len(PRICES.loc[f.is_start:f.is_end]) for f in folds[1:]]
    assert len(set(widths)) == 1 and widths[0] == 90


def test_embargo_zero_hours():
    an = _analyzer(embargo_hours=0)
    f = an.generate_folds()[0]
    assert (f.oos_start - f.is_end).days == 1      # adjacent, no gap


# ── (b) full loop selects the known-best param ──────────────────────────────

def test_full_loop_selects_good_param():
    an = _analyzer()
    result = an.run()
    assert result.folds, "no folds evaluated"
    selected = {fr.is_best_name for fr in result.folds}
    assert selected == {"good_set"}
    assert result.synthetic_metrics["sharpe"] > 0
    assert result.n_param_sets == 2
    # oracle/static layers populated + oracle ≥ synthetic by construction
    assert result.static_best_name == "good_set"
    assert result.comparison["oracle"]["sharpe"] >= result.comparison["synthetic"]["sharpe"] - 1e-9
    # selection log serializes
    d = result.to_detail_dict()
    assert d["n_folds"] == len(result.folds)


# ── (c) DSR collapses many-trial noise ──────────────────────────────────────

def test_dsr_deflates_vs_naive():
    # NOTE: per-period (daily) Sharpe units here — the DSR statistic scales by
    # sqrt(T-1) of the SAME periodicity. (The template/module call sites feed
    # ANNUALIZED SR with daily T, which inflates the statistic — flagged as a
    # template quirk; behavior preserved per copy-first.)
    rng = np.random.default_rng(11)
    T = 200
    n_trials = 100
    sharpes = []
    for _ in range(n_trials):
        r = rng.normal(0, 0.01, T)
        sharpes.append(r.mean() / r.std())
    sr_best = max(sharpes)
    sr_0 = expected_max_sharpe(n_trials, float(np.var(sharpes)))
    assert sr_0 > 0
    naive_p = deflated_sharpe_ratio(sr_best, 0.0, T)        # no trial adjustment
    deflated_p = deflated_sharpe_ratio(sr_best, sr_0, T)    # N-trial adjusted
    assert deflated_p < naive_p                             # deflation bites
    assert deflated_p < 0.95                                # noise does not survive


def test_expected_max_sharpe_grows_with_trials():
    assert expected_max_sharpe(100, 0.25) > expected_max_sharpe(10, 0.25) > 0
    assert expected_max_sharpe(1, 0.25) == 0.0


# ── (d) WFE = OOS_SR / IS_SR ────────────────────────────────────────────────

def test_wfe_equals_oos_over_is():
    an = _analyzer()
    result = an.run()
    fr = result.folds[0]
    eq = _fake_run_backtest(PARAM_SETS[fr.is_best_name], None, None)["equity_curve"]
    eq_is = eq[(eq.index >= fr.fold.is_start) & (eq.index <= fr.fold.is_end)]
    seg = eq[(eq.index >= fr.fold.oos_start) & (eq.index <= fr.fold.oos_end)]
    is_sr = _compute_metrics_from_equity(eq_is)["sharpe"]
    oos_sr = _compute_metrics_from_equity(seg / seg.iloc[0])["sharpe"]
    assert fr.wfe == pytest.approx(oos_sr / is_sr, rel=1e-9)
    assert not np.isnan(result.mean_wfe)


# ── extras: metrics annualization + guards ──────────────────────────────────

def test_metrics_use_365():
    r = np.full(len(IDX), 0.001) + np.where(np.arange(len(IDX)) % 2, 0.0005, -0.0005)
    m = _compute_metrics_from_equity(_equity(r))
    # pct_change() on the equity recovers r[1:] (first day has no return)
    rets = pd.Series(r[1:])
    expected = rets.mean() / rets.std() * np.sqrt(365)
    assert m["sharpe"] == pytest.approx(expected, rel=1e-6)


def test_param_sets_required():
    with pytest.raises(ValueError):
        WalkForwardAnalyzer(_fake_run_backtest, {}, PRICES, MACRO)
