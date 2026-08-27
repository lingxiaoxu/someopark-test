"""#196 — the gate must refuse to grade a model production does not run.

Every number in the gate row comes from replaying settled history through `predict()`.
KXFED and KXAAAGASW both replayed a fallback branch — no ZQ bar for the meeting month,
no AAA_DAILY before 2026-07-31 — so their rows described a model that has never placed a
bet, and the KXAAAGASW row is the worst model-vs-market Brier in the table. Neither
announced itself. Everything below pins the announcement.

Offline: the verdict logic is pure, and the two store-reading paths are exercised against
a tmp_path db built by hand.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.research import branch_parity as bp
from prediction_market_macro.research.eval import gate_verdict

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


# ── branch_of ────────────────────────────────────────────────────────────────


def test_mode_is_the_label_when_the_model_supplies_one():
    assert bp.branch_of({"mode": "aaa_daily_anchor", "anchor": 3.1}) == "aaa_daily_anchor"


def test_a_parameter_in_the_label_is_not_a_branch():
    """energy.py writes `drift_regression(n=17)`. Left alone, KXAAAGASW's 18 events on
    that path become 18 one-event branches and every share in the mix collapses to 1/73 —
    the mix stops being able to say anything at all."""
    labels = {bp.branch_of({"mode": f"drift_regression(n={n})"}) for n in range(10, 28)}
    assert labels == {"drift_regression"}


def test_key_signature_ignores_values_and_diagnostics():
    """Rule 2: a fallback omits the inputs it could not read, so the KEY SET moves with
    the branch. Values must not, or every event is its own branch."""
    a = bp.branch_of({"mu": 1.0, "sigma": 0.2})
    b = bp.branch_of({"sigma": 9.9, "mu": -4.0})            # same keys, other values
    assert a == b == "keys:mu,sigma"
    # `sigma_core_retired` is a config complaint that rides along on any branch
    assert bp.branch_of({"mu": 1.0, "sigma": 0.2,
                         "sigma_core_retired": 1}) == "keys:mu,sigma"
    assert a != bp.branch_of({"mu": 1.0})                   # a missing input IS a branch


def test_empty_inputs_are_not_silently_a_branch():
    assert bp.branch_of(None) == "unknown:no_inputs"
    assert bp.branch_of({}) == "unknown:no_inputs"


# ── parity_check ─────────────────────────────────────────────────────────────


def _mix(**counts):
    return bp.mix_from_counts(counts)


def test_the_kxaaagasw_case_fails_and_says_why():
    v = bp.parity_check(_mix(damped_trend_fallback=51, drift_regression=18,
                             aaa_daily_anchor=4),
                        _mix(aaa_daily_anchor=96))
    assert v["parity"] is False
    assert v["live_branch"] == "aaa_daily_anchor"
    assert v["hist_branch"] == "damped_trend_fallback"
    # the number a human needs is the live branch's share of the GRADED sample
    assert v["hist_share_of_live_branch"] == pytest.approx(4 / 73, abs=1e-4)
    for frag in ("damped_trend_fallback", "aaa_daily_anchor", "51/73"):
        assert frag in v["reason"]


def test_a_mixed_but_dominant_live_branch_passes():
    """Not "identical mixes". KXFED legitimately prices near meetings off ZQ and far ones
    off the rule, so demanding equality would fire forever and be ignored forever."""
    v = bp.parity_check(_mix(rule_ff=30, rule_dgs2=10), _mix(rule_ff=8, rule_dgs2=2))
    assert v["parity"] is True
    assert v["reason"] is None
    assert v["tvd"] > 0                                     # the mixes really do differ


def test_threshold_is_a_majority_of_the_graded_sample():
    exactly = bp.parity_check(_mix(a=50, b=50), _mix(a=10))
    assert exactly["hist_share_of_live_branch"] == 0.5
    assert exactly["parity"] is True, "MIN_HIST_SHARE is inclusive"
    assert bp.parity_check(_mix(a=49, b=51), _mix(a=10))["parity"] is False


def test_an_unverifiable_series_is_a_failure_not_a_pass():
    """Both directions of missing evidence. This feeds a real-money gate, so "I could not
    check" must not read the same as "I checked and it was fine"."""
    no_live = bp.parity_check(_mix(a=10), bp.mix_from_counts({}))
    assert (no_live["parity"], no_live["unknown"]) == (False, True)
    no_hist = bp.parity_check(bp.mix_from_counts({}), _mix(a=10))
    assert (no_hist["parity"], no_hist["unknown"]) == (False, True)


# ── the gate ─────────────────────────────────────────────────────────────────


def _bundles():
    """A bundle that clears all five of the 铁律7 criteria."""
    return ({"n_scored-1h": 20, "brier_model-1h": 0.05, "brier_market-1h": 0.09},
            {"roi": 0.2, "edge_capture": 0.8}, {"p": 0.01})


def test_a_perfect_row_is_still_refused_when_it_graded_the_wrong_branch():
    agg, dec, dm = _bundles()
    assert gate_verdict(agg, dec, dm)["real"] is True        # all five criteria clear
    bad = bp.parity_check(_mix(fallback=51, live=4), _mix(live=96))
    v = gate_verdict(agg, dec, dm, parity=bad)
    assert v["real"] is False
    assert any("branch parity" in r for r in v["reasons"])
    assert v["criteria"]["branch_parity"] is False


def test_parity_none_means_unchecked_not_passed():
    """The default exists so the five criteria stay unit-testable alone. It must be
    distinguishable in the stored row from a check that ran and passed, or a caller that
    forgets to pass it looks exactly like a clean series."""
    agg, dec, dm = _bundles()
    assert gate_verdict(agg, dec, dm)["criteria"]["branch_parity"] is None
    ok = bp.parity_check(_mix(live=10), _mix(live=5))
    assert gate_verdict(agg, dec, dm, parity=ok)["criteria"]["branch_parity"] is True


# ── the live side, against a store ───────────────────────────────────────────


def _contract(conn, series, token, close, ticker=None, status="active"):
    """`contracts.period` holds the KALSHI token ('26JUL'); `preds.period` holds the
    internal key ('2026-07'). Joining the two is the whole reason `recorded_branch_mix`
    runs every contract period through `kalshi_period_to_key` — a test that stored the
    key in both would pass while production found nothing."""
    period = token
    conn.execute(
        "INSERT OR REPLACE INTO contracts(ticker, series, event_ticker, period,"
        " close_time, status, first_seen_ts) VALUES(?,?,?,?,?,?,?)",
        (ticker or f"{series}-{period}-T", series, f"{series}-{period}", period,
         close.isoformat(), status, NOW.isoformat()))


def _pred(conn, series, period, asof, inputs, version="pce/0.1.0"):
    conn.execute(
        "INSERT OR REPLACE INTO preds(series, period, asof, model_version, dist_json,"
        " inputs_json, data_horizon, created_ts) VALUES(?,?,?,?,?,?,?,?)",
        (series, period, asof.isoformat(), version, "{}", json.dumps(inputs),
         asof.isoformat(), asof.isoformat()))


def test_recorded_mix_counts_only_what_was_tradeable(conn):
    """`decide` refuses an entry outside 0.03..7 days to close, so a prediction written
    for a period two months out is one production will never bet on. Counting it was the
    2026-08-27 KXPCECORE false positive: the far-month row said `bridge_on_predicted_cpi`
    while every in-window row said `bridge_on_actual_cpi`."""
    close = NOW + timedelta(days=3)
    _contract(conn, "KXPCECORE", "26JUL", close)
    _contract(conn, "KXPCECORE", "26OCT", NOW + timedelta(days=70))
    for k in range(3):
        _pred(conn, "KXPCECORE", "2026-07", close - timedelta(days=k + 1),
              {"mode": "bridge_on_actual_cpi"})
    _pred(conn, "KXPCECORE", "2026-07", close - timedelta(days=40),
          {"mode": "bridge_on_predicted_cpi"})        # same period, outside the window
    _pred(conn, "KXPCECORE", "2026-10", NOW, {"mode": "bridge_on_predicted_cpi"})
    conn.commit()
    mix = bp.recorded_branch_mix(conn, "KXPCECORE", NOW)
    assert mix["counts"] == {"bridge_on_actual_cpi": 3}


def test_recorded_mix_ignores_the_shadow_members(conn):
    """`preds` also holds ensemble/*, bridge/* and chronos2/*. They are not production."""
    close = NOW + timedelta(days=2)
    _contract(conn, "KXPCECORE", "26JUL", close)
    _pred(conn, "KXPCECORE", "2026-07", NOW, {"mode": "bridge_on_actual_cpi"})
    _pred(conn, "KXPCECORE", "2026-07", NOW - timedelta(hours=1),
          {"sources": 3, "weights": 1}, version="ensemble/0.1.0")
    conn.commit()
    mix = bp.recorded_branch_mix(conn, "KXPCECORE", NOW)
    assert mix["counts"] == {"bridge_on_actual_cpi": 1}
    assert mix["model_version"] == "pce/0.1.0"


def test_a_row_with_no_inputs_is_an_absence_not_a_branch(conn):
    close = NOW + timedelta(days=2)
    _contract(conn, "KXPCECORE", "26JUL", close)
    _pred(conn, "KXPCECORE", "2026-07", NOW, {"mode": "bridge_on_actual_cpi"})
    _pred(conn, "KXPCECORE", "2026-07", NOW - timedelta(hours=2), {})
    conn.commit()
    mix = bp.recorded_branch_mix(conn, "KXPCECORE", NOW)
    assert mix["counts"] == {"bridge_on_actual_cpi": 1}
    assert mix["n_no_inputs"] == 1


def test_tradeable_periods_prefers_the_window_and_falls_back_to_the_nearest(conn):
    _contract(conn, "KXCPI", "26SEP", NOW + timedelta(days=15))
    _contract(conn, "KXCPI", "26OCT", NOW + timedelta(days=45))
    conn.commit()
    keys, window = bp.tradeable_periods(conn, "KXCPI", NOW)
    assert (keys, window) == (["2026-09"], "nearest_open"), \
        "a monthly series is out of the entry window three weeks in four; returning" \
        " nothing would close its gate by the calendar rather than by evidence"
    _contract(conn, "KXCPI", "26AUG", NOW + timedelta(days=4))
    conn.commit()
    assert bp.tradeable_periods(conn, "KXCPI", NOW) == (["2026-08"], "entry_window")
    # a period that has already closed is not tradeable in either mode
    conn.execute("DELETE FROM contracts")
    _contract(conn, "KXCPI", "26JUL", NOW - timedelta(days=1))
    conn.commit()
    assert bp.tradeable_periods(conn, "KXCPI", NOW) == ([], "none_open")
