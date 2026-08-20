"""tests/test_bridge.py — regression cover for the five defects fixed in bridge/0.2.0.

Every test here is pinned to a specific defect that shipped in v0.1.0 and produced a
KXPAYROLLS mean of 484,862 jobs against production's 74,967. In-memory / tmp db only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model import bridge


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


def _put(conn, sid, event_time, value, vintage_date, kt=None):
    """Insert one ALFRED-shaped vintage row."""
    kt = kt or f"{vintage_date}T13:30:00+00:00"
    conn.execute(
        "INSERT OR REPLACE INTO fred_obs(sid, event_time, value, vintage_date,"
        " knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?)",
        (sid, event_time, value, vintage_date, kt, kt))


ASOF = datetime(2026, 8, 20, tzinfo=timezone.utc)


# ── defect 1: the target must be the PRINTED change, not a cross-vintage diff ──

def test_published_changes_uses_same_vintage_not_cross_vintage_diff(conn):
    """The canonical failure: a revision to m-1 must NOT leak into m's change.

    PAYEMS ships three months of history per vintage. Month 1 first prints at 100
    (level 1000 vs a 900 base). Month 2's release revises month 1 DOWN to 950 and
    prints month 2 at 1010. The printed change for month 2 is 1010-950 = +60.
    Diffing first prints across vintages gives 1010-1000 = +10 -- it silently folds
    the -50 revision into the target. v0.1.0 did the latter.
    """
    _put(conn, "PAYEMS", "2025-11-01", 900.0, "2025-12-05")
    _put(conn, "PAYEMS", "2025-12-01", 1000.0, "2026-01-09")
    _put(conn, "PAYEMS", "2025-11-01", 900.0, "2026-01-09")
    # February release: revises Dec down to 950, prints Jan at 1010
    _put(conn, "PAYEMS", "2026-01-01", 1010.0, "2026-02-06")
    _put(conn, "PAYEMS", "2025-12-01", 950.0, "2026-02-06")
    conn.commit()

    ch = bridge._published_changes(conn, "PAYEMS", ASOF)
    assert ch.loc[pd.Period("2026-01", freq="M")] == pytest.approx(60.0)

    naive = bridge._first_print_levels(conn, "PAYEMS", ASOF).diff().dropna()
    assert naive.loc[pd.Period("2026-01", freq="M")] == pytest.approx(10.0)


def test_published_changes_survives_one_row_per_vintage_series(conn):
    """CPIAUCSL / UNRATE store ONE row per vintage, so m and m-1 NEVER share a
    vintage_date. A same-vintage matching rule yields an empty series here and the
    model raises 'insufficient overlapping history' forever. The PIT rule -- latest
    print of m-1 known at m's release -- must still pair them.
    """
    _put(conn, "CPIAUCSL", "2026-01-01", 100.0, "2026-02-12")
    _put(conn, "CPIAUCSL", "2026-02-01", 101.0, "2026-03-11")
    _put(conn, "CPIAUCSL", "2026-03-01", 102.01, "2026-04-10")
    conn.commit()

    pct = bridge._published_mom_pct(conn, "CPIAUCSL", ASOF)
    assert len(pct) == 2
    assert pct.loc[pd.Period("2026-02", freq="M")] == pytest.approx(1.0, abs=1e-9)
    assert pct.loc[pd.Period("2026-03", freq="M")] == pytest.approx(1.0, abs=1e-9)


def test_prev_as_known_is_pit_and_ignores_later_revisions(conn):
    """_prev_as_known must not see a revision published after the release it models."""
    by_month = {pd.Period("2026-01", freq="M"): [
        ("2026-02-06T13:30:00+00:00", 950.0),
        ("2026-09-01T13:30:00+00:00", 700.0),      # much later revision
    ]}
    at_release = bridge._prev_as_known(
        by_month, pd.Period("2026-01", freq="M"), "2026-03-06T13:30:00+00:00")
    assert at_release == 950.0
    # and nothing is visible before the first print
    assert bridge._prev_as_known(
        by_month, pd.Period("2026-01", freq="M"), "2026-01-01T00:00:00+00:00") is None


# ── defect 2: bounded window + COVID exclusion ────────────────────────────────

def test_fit_window_is_bounded_and_strictly_past():
    """A fit for ref must not see ref itself, and must not reach past `window`."""
    idx = pd.period_range("2000-01", "2026-07", freq="M")
    y = pd.Series(np.arange(len(idx), dtype=float), index=idx)
    seen = []

    def xf(m):
        seen.append(m)
        return 1.0 + (m.year % 3)

    ref = pd.Period("2026-08", freq="M")
    out = bridge._fit_bridge(y, xf, ref, window=60, min_n=10, ic_months=0)
    assert out is not None
    assert max(seen) < ref                        # strictly past
    assert min(seen) >= ref - 60                  # bounded


def test_covid_months_are_excluded_and_counted():
    idx = pd.period_range("2015-01", "2026-07", freq="M")
    y = pd.Series(1.0, index=idx)
    y.loc[pd.Period("2020-04", freq="M")] = -20_500.0    # the -20.5M jobs month
    used = []

    def xf(m):
        used.append(m)
        return float(m.month)

    out = bridge._fit_bridge(y, xf, pd.Period("2026-08", freq="M"), ic_months=0)
    assert out is not None
    _a, _b, _s, _n, n_cov, _ic = out
    assert n_cov == 34                            # 2020-03 .. 2022-12 inclusive
    assert not any(bridge.COVID_FROM <= m <= bridge.COVID_TO for m in used)


def test_single_covid_outlier_cannot_flip_the_slope():
    """The v0.1.0 failure mode in miniature: one 2020 observation with huge leverage
    inverted the fitted slope. With the exclusion in place the sign must survive."""
    idx = pd.period_range("2010-01", "2026-07", freq="M")
    rng = np.random.RandomState(0)
    xs = {m: float(rng.randn()) for m in idx}
    y = pd.Series({m: 2.0 * xs[m] + 0.1 * rng.randn() for m in idx})
    # a catastrophic, high-leverage COVID point pulling the other way
    xs[pd.Period("2020-04", freq="M")] = 60.0
    y.loc[pd.Period("2020-04", freq="M")] = -400.0

    out = bridge._fit_bridge(y, lambda m: xs[m], pd.Period("2026-08", freq="M"),
                             ic_months=0)
    assert out is not None
    _a, b, _s, _n, _nc, _ic = out
    assert b > 1.5                                # true slope is 2.0, sign intact


# ── defect 4: sigma is a robust scale, emitted as a fat-tailed mixture ────────

def test_robust_scale_ignores_the_tail_that_huber_downweighted():
    core = np.random.RandomState(1).randn(200)
    contaminated = np.concatenate([core, [500.0, -500.0, 900.0]])
    assert bridge._robust_scale(contaminated) < 2.0
    assert float(np.std(contaminated)) > 50.0     # what v0.1.0 would have emitted


def test_mixture_shape_and_effective_width():
    mix = bridge._mixture(100.0, 10.0)
    assert len(mix.comps) == 2
    assert sum(w for w, _m, _s in mix.comps) == pytest.approx(1.0)
    mu = sum(w * m for w, m, _s in mix.comps)
    var = sum(w * (s * s + m * m) for w, m, s in mix.comps) - mu * mu
    assert mu == pytest.approx(100.0)
    # fat tail widens the effective sd above the core sigma but stays finite
    assert 10.0 < var ** 0.5 < 20.0


# ── the Huber scale-floor degeneracy (would have shipped as a live crash) ─────

def test_huber_survives_degenerate_residuals():
    """U3 monthly deltas are mostly exactly 0.0 or +/-0.1. More than half the residuals
    coincide, the MAD collapses to ~0, IRLS weights explode and lstsq raises
    LinAlgError. Flooring the scale keeps it finite."""
    y = np.array([0.0] * 40 + [0.1] * 5 + [-0.1] * 5)
    X = np.arange(len(y), dtype=float)[:, None]
    beta = bridge._huber(X, y)
    assert np.all(np.isfinite(beta))


def test_huber_on_constant_target_is_finite():
    y = np.zeros(50)
    X = np.arange(50, dtype=float)[:, None]
    assert np.all(np.isfinite(bridge._huber(X, y)))


# ── defect 5: intercept correction ────────────────────────────────────────────

def test_intercept_correction_shifts_level_toward_recent_regime():
    """A level break in the last 12 months must move the intercept, and the size of
    the move must be the mean of those residuals -- not a rescaled slope."""
    idx = pd.period_range("2006-01", "2026-07", freq="M")
    xs = {m: 0.0 for m in idx}
    y = pd.Series(190.0, index=idx)
    for m in idx[-12:]:
        y.loc[m] = 70.0                            # regime shift, last 12 months

    ref = pd.Period("2026-08", freq="M")
    raw = bridge._fit_bridge(y, lambda m: xs[m], ref, ic_months=0)
    cor = bridge._fit_bridge(y, lambda m: xs[m], ref, ic_months=12)
    assert raw is not None and cor is not None
    assert cor[0] < raw[0]                         # corrected downward
    assert cor[0] == pytest.approx(raw[0] + cor[5])
    assert cor[0] == pytest.approx(70.0, abs=1.0)  # tracks the new regime


def test_intercept_correction_is_disabled_by_zero():
    idx = pd.period_range("2006-01", "2026-07", freq="M")
    y = pd.Series(np.linspace(0.0, 10.0, len(idx)), index=idx)
    out = bridge._fit_bridge(y, lambda m: 0.0, pd.Period("2026-08", freq="M"),
                             ic_months=0)
    assert out is not None and out[5] == 0.0


# ── horizon-agnostic emission must be distinguishable from real flat data ────

def test_hf_complete_flags_a_month_whose_window_has_not_arrived():
    idx = pd.date_range("2026-06-06", periods=8, freq="W-SAT")
    hf = pd.Series(np.arange(8, dtype=float), index=idx)
    assert bridge._hf_complete(hf, idx[-1])
    assert not bridge._hf_complete(hf, idx[-1] + timedelta(days=30))
    assert not bridge._hf_complete(pd.Series(dtype=float), idx[0])


def test_future_month_regressor_collapses_to_zero_not_to_signal():
    """Documents WHY the flag exists: for a month past the data, the m and m-1 windows
    are the same rows, so the dlog regressor is identically 0.0 and is otherwise
    indistinguishable from 'claims were flat'."""
    idx = pd.date_range("2026-06-06", periods=8, freq="W-SAT")
    claims = pd.Series(np.linspace(200_000, 240_000, 8), index=idx)
    x = bridge._claims_refweek_dlog(claims, pd.Period("2026-12", freq="M"))
    assert x == pytest.approx(0.0)


# ── significant-figure reporting ─────────────────────────────────────────────

def test_sig_preserves_tiny_and_huge_coefficients():
    """round(b, 5) reported KXU3's real ~8e-06 slope as exactly 0.0."""
    assert bridge._sig(8.08061e-06) != 0.0
    assert bridge._sig(8.08061e-06) == pytest.approx(8.08061e-06, rel=1e-5)
    assert bridge._sig(-701867.2039) == pytest.approx(-701867.0, rel=1e-6)
    assert bridge._sig(0.0) == 0.0


# ── config sanity: the constants the docstring's numbers were measured at ────

def test_shipped_constants_match_the_documented_calibration():
    assert bridge.VERSION == "bridge/0.2.0"
    assert bridge.WINDOW_MONTHS == 240
    assert bridge.IC_MONTHS == 12
    assert bridge.COVID_FROM == pd.Period("2020-03", freq="M")
    assert bridge.COVID_TO == pd.Period("2022-12", freq="M")
    assert (bridge.TAIL_WEIGHT, bridge.TAIL_MULTIPLE) == (0.15, 3.0)


def test_unsupported_series_raises(conn):
    with pytest.raises(ValueError):
        bridge.predict(conn, ASOF, "2026-08", series="KXNOPE")


# ── the stale-version hazard in the ensemble ─────────────────────────────────

def test_bridge_pmf_is_pinned_to_the_current_version(conn):
    """predict() raises rather than emitting when a fit is unavailable, and shadow_run
    swallows that. Under a LIKE 'bridge/%' match a superseded version's row stays the
    newest one forever -- which is exactly how v0.1.0's mis-calibrated ladder would
    have outlived its own replacement.
    """
    from prediction_market_macro.model import ensemble
    for ver, asof in ((bridge.VERSION, "2026-08-01T00:00:00+00:00"),
                      ("bridge/0.1.0", "2026-08-19T00:00:00+00:00")):
        conn.execute(
            "INSERT OR REPLACE INTO preds(series, period, asof, model_version,"
            " dist_json, ladder_json, inputs_json, data_horizon, created_ts)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            ("KXPAYROLLS", "2026-08", asof, ver, "{}",
             '{"1.0": 1.0}' if ver == bridge.VERSION else '{"484862.0": 1.0}',
             "{}", asof, asof))
    conn.commit()

    pmf = ensemble._bridge_pmf(conn, "KXPAYROLLS", "2026-08")
    assert pmf is not None
    # the NEWER v0.1.0 row must not win despite its later asof
    assert list(pmf.keys()) == [1.0]


def test_refweek_end_is_the_first_saturday_on_or_after_the_12th():
    """The worst-case 12th+6 cutoff reported a closed reference week as still open."""
    # 2026-08-12 is a Wednesday -> reference week ends Sat 2026-08-15, not the 18th
    assert bridge._refweek_end(pd.Period("2026-08", freq="M")) == pd.Timestamp("2026-08-15")
    for key in ("2024-01", "2025-06", "2026-02", "2026-11"):
        end = bridge._refweek_end(pd.Period(key, freq="M"))
        twelfth = pd.Timestamp(f"{key}-12")
        assert end.weekday() == 5                       # a Saturday
        assert 0 <= (end - twelfth).days <= 6            # covering the 12th


def test_refweek_end_when_the_twelfth_is_itself_saturday():
    assert pd.Timestamp("2026-09-12").weekday() == 5
    assert bridge._refweek_end(pd.Period("2026-09", freq="M")) == pd.Timestamp("2026-09-12")
