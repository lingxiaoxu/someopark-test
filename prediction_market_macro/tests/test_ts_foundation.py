"""chronos2/zero-shot-0.2.0 — the four defects the rebuild fixed, pinned.

None of these tests loads chronos-2. Every one of them is about how the model's OUTPUT is
turned into a tradeable ladder, which is where all four measured defects lived and where a
regression would be invisible (a wrong pmf still looks like a pmf).

The one that would have caught a real bug: `test_lower_tail_points_down`. The first
implementation wrote the left tail as `v0 - b*ln(u/l0)`, but for u < l0 that logarithm is
NEGATIVE, so the tail bent UPWARD and the monotonicity repair then flattened it into a
point mass — a degeneracy strictly worse than the truncation it replaced. The smoke test
caught it because it asserted the direction rather than just "no exception".
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from prediction_market_macro.model import ts_covariates as tc
from prediction_market_macro.model import ts_foundation as tf
from prediction_market_macro.model.common import Empirical, grid_pmf


def _logistic_q(levels, mu=60.0, s=2.0):
    return [mu + s * math.log(l / (1 - l)) for l in levels]


# ── P0: distribution encoding ────────────────────────────────────────────────
def test_trained_levels_only():
    """Asking chronos-2 for a level it was not trained on fabricates it by
    interpolation. 0.1.0 asked for 99; the model knows 21."""
    assert tf._QLEVELS == [0.01, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45,
                           0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95,
                           0.99]


def test_samples_reproduce_the_input_quantiles():
    q = _logistic_q(tf._QLEVELS)
    s = tf.samples_from_quantiles(tf._QLEVELS, q)
    assert len(s) == tf._N_SAMPLES
    for lvl, v in zip(tf._QLEVELS, q):
        assert abs(float(np.quantile(s, lvl)) - v) < 0.01


def test_lower_tail_points_down():
    """The regression guard. Both tails must extend AWAY from the body."""
    q = _logistic_q(tf._QLEVELS)
    s = tf.samples_from_quantiles(tf._QLEVELS, q)
    assert s.min() < q[0], "left tail did not extrapolate below q0.01"
    assert s.max() > q[-1], "right tail did not extrapolate above q0.99"
    # and it must be a genuine spread, not a flattened point mass at the join
    left = s[s < q[0]]
    assert left.std() > 0, "left tail collapsed to a point mass"
    assert np.all(np.diff(s) >= 0), "quantile function is not monotone"


def test_tail_is_heavier_than_the_gaussian_through_the_same_points():
    """The xi->0 GPD tail exists to price wings a normal would call impossible."""
    q = _logistic_q(tf._QLEVELS)
    s = tf.samples_from_quantiles(tf._QLEVELS, q)
    mu, sd = float(np.mean(s)), float(np.std(s))
    far = mu + 4 * sd
    p_emp = float(np.mean(s > far))
    p_norm = 0.5 * math.erfc(4 / math.sqrt(2))
    assert p_emp > p_norm


def test_degenerate_forecast_is_refused_not_priced():
    """All-equal quantiles would become a 100%-certain leg. Refuse instead."""
    with pytest.raises(ValueError):
        tf.samples_from_quantiles(tf._QLEVELS, [5.0] * len(tf._QLEVELS))


def test_crossing_quantiles_are_repaired():
    q = _logistic_q(tf._QLEVELS)
    q[7], q[8] = q[8], q[7]                       # induce a crossing
    s = tf.samples_from_quantiles(tf._QLEVELS, q)
    assert np.all(np.diff(s) >= 0)


def test_sampling_is_deterministic():
    """Replay must be bit-stable without carrying an rng seed."""
    q = _logistic_q(tf._QLEVELS)
    assert np.array_equal(tf.samples_from_quantiles(tf._QLEVELS, q),
                          tf.samples_from_quantiles(tf._QLEVELS, q))


def test_grid_is_not_a_comb():
    """0.1.0 put 99 atoms on the settlement grid; a $0.01 WTI ladder needs thousands,
    and the hard zeros outside [q0.01, q0.99] priced live legs at exactly 0/1."""
    q = _logistic_q(tf._QLEVELS)
    old = Empirical(tuple(np.round(_logistic_q(np.arange(0.01, 1.0, 0.01)), 4).tolist()))
    new = Empirical(tuple(tf.samples_from_quantiles(tf._QLEVELS, q).tolist()))
    assert len(grid_pmf(new, 0.01)) > 10 * len(grid_pmf(old, 0.01))
    assert new.quantile(0.001) < old.quantile(0.001)
    assert new.quantile(0.999) > old.quantile(0.999)


# ── P1 / P2 / P3: the spec table ─────────────────────────────────────────────
def test_claims_reads_the_first_print():
    """KXJOBLESSCLAIMS settles on the DoL ADVANCE print; ICSA revisions are almost
    always upward and round_rule is 250, so the latest vintage is the wrong label."""
    assert tf._TARGETS["KXJOBLESSCLAIMS"].first_print is True
    assert all(not t.first_print for k, t in tf._TARGETS.items()
               if k != "KXJOBLESSCLAIMS")


def test_aaa_is_anchored_on_the_settling_series():
    """The model forecasts the GASREGW proxy; the LEVEL must come from AAA_DAILY."""
    assert tf._TARGETS["KXAAAGASW"].anchor == "AAA_DAILY"


def test_context_is_no_longer_260():
    for name, t in tf._TARGETS.items():
        assert t.context >= 800, f"{name} context {t.context} wastes chronos-2's 8192"


def test_every_series_has_covariates():
    for name, t in tf._TARGETS.items():
        assert t.past, f"{name} has no past covariates"
        assert t.future_cal, f"{name} has no known-future covariates"


def test_future_covariates_are_a_subset_of_past():
    """chronos-2 rejects a future key absent from past_covariates. The calendar features
    are added to BOTH in _build_task/_attach_future; this pins the invariant."""
    for name, t in tf._TARGETS.items():
        idx = pd.bdate_range("2024-01-01", periods=200)
        cal = tc.calendar_features(idx)
        for k in t.future_cal:
            assert k in cal, f"{name}: future_cal '{k}' is not a calendar feature"


# ── the AAA transport ────────────────────────────────────────────────────────
class _FakeConn:
    """Minimal stand-in: only tc.fred(AAA_DAILY) is reached by _transport_to_anchor."""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=()):
        class _R(list):
            def fetchall(self_inner):
                return self_inner

            def fetchone(self_inner):
                return self_inner[0] if self_inner else None
        if "MAX(knowledge_time)" in sql:
            return _R([{"m": "2026-08-18T09:00:00+00:00"}])
        return _R(self.rows)


def _rows(date, value):
    return [{"event_time": date, "value": value,
             "knowledge_time": "2026-08-18T09:00:00+00:00"}]


def test_transport_moves_level_to_anchor_and_damps_drift():
    asof = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)
    proxy = pd.Series({pd.Timestamp("2026-08-10"): 4.00})
    # chronos says the proxy falls 20c over the 14 days to settle
    samples = np.linspace(3.70, 3.90, 1001)
    conn = _FakeConn(_rows("2026-08-18", 4.10))
    moved, meta, mode = tf._transport_to_anchor(
        conn, asof, "2026-08-24", proxy, samples, "AAA_DAILY", [])
    assert mode == "aaa_daily_anchor"
    # only 6 of the 14 days remain, so only 6/14 of the drift survives
    assert meta["days_left"] == 6 and meta["days_total"] == 14
    frac = 6 / 14
    expect = 4.10 + frac * (float(np.median(samples)) - 4.00)
    assert abs(float(np.median(moved)) - expect) < 1e-9
    # the level is anchored on AAA (4.10), NOT on the proxy (4.00)
    assert float(np.median(moved)) > 3.95


def test_transport_shrinks_dispersion_with_the_square_root_of_time():
    asof = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)
    proxy = pd.Series({pd.Timestamp("2026-08-10"): 4.00})
    samples = np.linspace(3.80, 4.20, 1001)
    conn = _FakeConn(_rows("2026-08-18", 4.10))
    moved, meta, _ = tf._transport_to_anchor(
        conn, asof, "2026-08-24", proxy, samples, "AAA_DAILY", [])
    # against the EXACT fraction: meta["time_frac"] is rounded to 4dp for the inputs blob
    assert abs(float(np.std(moved)) / float(np.std(samples))
               - math.sqrt(6 / 14)) < 1e-9
    assert abs(meta["time_frac"] - 6 / 14) < 1e-4


def test_stale_anchor_degrades_to_proxy_and_says_so():
    """§27.4's _aaa_information_gate refuses any mode != 'aaa_daily_anchor'. The
    fallback must therefore be honestly labelled, not silently priced."""
    asof = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)
    proxy = pd.Series({pd.Timestamp("2026-08-10"): 4.00})
    samples = np.linspace(3.80, 4.20, 101)
    conn = _FakeConn(_rows("2026-07-01", 4.10))          # far older than _AAA_FRESH_DAYS
    moved, meta, mode = tf._transport_to_anchor(
        conn, asof, "2026-08-24", proxy, samples, "AAA_DAILY", [])
    assert mode == "proxy_level"
    assert np.array_equal(moved, samples)
    assert meta["anchor"] is None


# ── covariate PIT / alignment ────────────────────────────────────────────────
def test_align_never_looks_forward():
    """A weekly covariate published Wednesday must not appear on Monday's row."""
    src = pd.Series({pd.Timestamp("2026-08-05"): 1.0, pd.Timestamp("2026-08-12"): 2.0})
    idx = pd.DatetimeIndex(["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"])
    out = tc.align(src, idx)
    assert list(out) == [1.0, 1.0, 2.0, 2.0]


def test_align_drops_rather_than_zero_fills():
    """A silently zeroed covariate is indistinguishable from a real zero."""
    src = pd.Series({pd.Timestamp("2026-08-20"): 1.0})
    idx = pd.bdate_range("2026-01-01", periods=200)
    assert tc.align(src, idx) is None


def test_calendar_features_are_pure_functions_of_the_date():
    a = tc.calendar_features(pd.DatetimeIndex(["2026-08-17", "2026-08-18"]))
    b = tc.calendar_features(pd.DatetimeIndex(["2026-08-17", "2026-08-18"]))
    for k in a:
        assert np.array_equal(a[k], b[k])
    assert a["dow"][0] == 0 and a["dow"][1] == 1


def test_future_index_respects_the_target_step():
    last = pd.Timestamp("2026-08-14")            # a Friday
    b = tc.future_index(last, 3, "bday")
    assert list(b.strftime("%Y-%m-%d")) == ["2026-08-17", "2026-08-18", "2026-08-19"]
    w = tc.future_index(last, 2, "week")
    assert list(w.strftime("%Y-%m-%d")) == ["2026-08-21", "2026-08-28"]
