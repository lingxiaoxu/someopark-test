"""PR-11 (#197) — the screened seasonal centre, judged forward.

Same failure modes as every other pre-registered route: a criterion that drifts to match
the result, a counter that counts nothing, a "paired" comparison whose arms sat at
different asofs, a verdict printed early. PR-11 adds one of its own, and it is the reason
this file exists at all:

    **a criterion that passes on arithmetic rather than on evidence.**

The screen changes 5 of 53 ISO weeks. On the other 48 the two arms are the same number and
dLL is exactly 0.0. Take the mean over all settled events and eight zeros plus one small
win is "positive mean, 9/9 non-negative" — a pass, on a sample where the change did
nothing. So the tests below spend most of their effort on one property: non-firing events
must not be able to carry the verdict.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.research import shadow_seasonal as ss

REG = datetime.fromisoformat(ss.REGISTERED)


def _rep(dlls, fired=None):
    """A report whose only real content is the per-event dLL column."""
    fired = [True] * len(dlls) if fired is None else fired
    events = [{"period": f"w{i}", "dll": d, "fired": f}
              for i, (d, f) in enumerate(zip(dlls, fired))]
    firing = [e for e in events if e["fired"]]
    rep = {"n_settled_since_registration": len(events), "n_firing": len(firing),
           "events": events}
    if firing:
        d = [e["dll"] for e in firing]
        rep["primary"] = {"mean_dll": sum(d) / len(d),
                          "n_positive": sum(x > 0 for x in d),
                          "n_required_positive": ss.MIN_POSITIVE}
    return rep


def test_the_constants_still_match_what_was_registered():
    """`docs/PREREGISTER.md` is the record; these constants are what the code enforces.
    If they drift apart this is the alarm — that is the whole value of a registration."""
    import pathlib
    doc = (pathlib.Path(ss.__file__).resolve().parent.parent
           / "docs" / "PREREGISTER.md").read_text()
    block = doc.split("### PR-11")[1]
    assert "2026-08-27" in block and ss.REGISTERED.startswith("2026-08-27")
    assert "mad_screen:10" in block
    assert ss.CANDIDATE == {"seasonal_estimator": "mad_screen:10"}
    assert ss.BASELINE == {"seasonal_estimator": "mean"}
    assert re.search(r"前向\s*≥\s*6\s*个开火事件", block) and ss.N_FIRING == 6
    assert re.search(r"≥\s*4\s*/\s*6", block) and ss.MIN_POSITIVE == 4
    assert re.search(r"\|\s*K\s*\|\s*\*\*10\*\*", block) and ss.K == 10
    # the registered metric is the interval LL, chosen because it is defined on every
    # settled event. An edit to per-leg Brier would silently restrict the sample to
    # quote-covered events, which is the model choosing which events grade it.
    assert "区间对数似然" in block


def test_the_candidate_arm_is_the_shipped_default():
    """PR-11 grades what production runs. If `DEFAULT_PARAMS` moved and this constant did
    not, the scorer would be grading a model nobody is using — #196's disease, one level
    down at the params rather than at the input key set."""
    from prediction_market_macro.model import claims
    assert claims.DEFAULT_PARAMS["seasonal_estimator"] == \
        ss.CANDIDATE["seasonal_estimator"]
    assert ss.BASELINE["seasonal_estimator"] == "mean", \
        "the baseline must be claims/0.1.0's centre, or the test is not about 0.2.0"


def test_no_verdict_before_the_registered_firing_count():
    rep = _rep([0.5] * (ss.N_FIRING - 1))
    v = ss.verdict(rep)
    assert v.startswith("PENDING") and f"{ss.N_FIRING - 1}/{ss.N_FIRING}" in v
    assert "PASS" not in v and "FALSIFIED" not in v


def test_a_pile_of_untouched_events_cannot_reach_a_verdict():
    """THE test. Fifty settled events, none of which the screen touched: every dLL is
    exactly 0.0, the diluted mean is 0.0, and the count that matters is still zero. A
    scorer that averaged over all settled events would report "50 events, mean 0.0,
    50/50 non-negative" and a criterion phrased as "non-negative" would call that a pass."""
    rep = _rep([0.0] * 50, fired=[False] * 50)
    assert rep["n_firing"] == 0 and "primary" not in rep
    assert ss.verdict(rep).startswith("PENDING")


def test_one_real_win_buried_in_zeros_still_does_not_count_as_six():
    rep = _rep([0.0] * 20 + [3.0], fired=[False] * 20 + [True])
    assert rep["n_firing"] == 1
    assert ss.verdict(rep).startswith("PENDING")


def test_a_clear_win_on_firing_events_passes():
    rep = _rep([2.0, 1.0, 3.0, 0.5, -0.2, -0.1])
    v = ss.verdict(rep)
    assert v.startswith("PASS") and "4/6" in v


def test_a_positive_mean_carried_by_one_event_fails_the_per_event_floor():
    """The registration asks for BOTH a positive mean and 4/6 individually positive,
    precisely so one 20-nat rescue cannot buy five losses — that is the shape PR-10
    rejected KXCPICORE for (a single event carrying 101.8% of the gain)."""
    rep = _rep([20.0, -0.5, -0.5, -0.5, -0.5, -0.5])
    assert rep["primary"]["mean_dll"] > 0
    assert ss.verdict(rep).startswith("FALSIFIED")


def test_a_losing_sample_says_what_the_registration_says_to_do():
    v = ss.verdict(_rep([-1.0, -2.0, 0.5, -0.5, 0.2, -0.3]))
    assert v.startswith("FALSIFIED") and "seasonal_estimator='mean'" in v


def test_firing_is_decided_by_the_arms_not_by_a_week_list():
    """A hardcoded {12,13,14,15,18} would be a constant that drifts away from what the
    code does the moment `seasonal_years` or the store changes — and `seasonal_years=15`
    has been adopted in production before. `run` compares the two arms' own `seasonal`."""
    import inspect
    src = inspect.getsource(ss.run)
    assert '"fired": abs(s_cand - s_base) > 1e-9' in src
    assert not re.search(r"\b12\s*,\s*13\s*,\s*14\b", inspect.getsource(ss))


def test_both_arms_are_scored_at_one_asof():
    """A paired test whose arms sat at different asofs is not a paired test. Both go
    through `_asof_for` once, per event, and the result is reused."""
    import inspect
    src = inspect.getsource(ss.run)
    assert src.count("_asof_for(") == 1
    assert 'for name, params in (("base", BASELINE), ("cand", CANDIDATE))' in src


def test_the_asof_rule_is_the_one_the_replay_uses(monkeypatch):
    """When the book closes after the print, asof must step back BEHIND the print. The
    2026-01 KXPAYROLLS/KXU3 events closed 90 minutes past their release; a close-anchored
    asof there hands the model the number it is about to be scored on."""
    close = datetime(2026, 3, 26, 14, 0, tzinfo=timezone.utc)
    release = datetime(2026, 3, 26, 12, 30, tzinfo=timezone.utc)
    monkeypatch.setattr("prediction_market_macro.research.backtest._settle_release_ts",
                        lambda *_a, **_k: release)
    asof, got = ss._asof_for(None, None, "2026-03-26", close)
    assert got == release and asof == release - timedelta(seconds=1)
    # and when the book closes before the print, the plain offset stands
    monkeypatch.setattr("prediction_market_macro.research.backtest._settle_release_ts",
                        lambda *_a, **_k: release + timedelta(hours=6))
    asof, _ = ss._asof_for(None, None, "2026-03-26", close)
    assert asof == close - timedelta(hours=1)


# ── the label ────────────────────────────────────────────────────────────────

def _db(tmp_path, legs, close="2026-03-26T12:25:00+00:00"):
    from prediction_market_macro.ingest.store import init_db
    conn = init_db(tmp_path / "s.db")
    for i, (strike, result) in enumerate(legs):
        t = f"KXJOBLESSCLAIMS-26MAR26-T{strike:g}"
        conn.execute(
            "INSERT INTO contracts(ticker, series, event_ticker, period, strike_type,"
            " floor_strike, close_time, first_seen_ts) VALUES(?,?,?,?,?,?,?,?)",
            (t, "KXJOBLESSCLAIMS", "E", "26MAR26", "greater_or_equal", float(strike),
             close, "2026-03-01T00:00:00+00:00"))
        conn.execute(
            "INSERT INTO settlements(ticker, series, period, result, first_seen_ts)"
            " VALUES(?,?,?,?,?)",
            (t, "KXJOBLESSCLAIMS", "26MAR26", result, "2026-03-27T00:00:00+00:00"))
    conn.commit()
    return conn


def test_the_print_is_bracketed_by_the_settled_ladder(tmp_path):
    """Not read from `fred_obs`. That is the same door the model reads its inputs
    through, and a label taken from it is one PIT mistake away from grading the model on
    its own inputs. `[max(yes), min(no))` needs nothing but the settlements."""
    conn = _db(tmp_path, [(200_000, "yes"), (205_000, "yes"),
                          (210_000, "no"), (215_000, "no")])
    b = ss.settled_brackets(conn)["26MAR26"]
    assert (b["lo"], b["hi"]) == (205_000.0, 210_000.0)


def test_an_event_the_ladder_cannot_bracket_is_dropped_not_guessed(tmp_path):
    """The print landed off the end of the ladder — every leg settled the same way — so
    there is no interval. Inventing one would put a fabricated label into a registration."""
    conn = _db(tmp_path, [(200_000, "yes"), (205_000, "yes")])
    assert ss.settled_brackets(conn) == {}
    (tmp_path / "b").mkdir()
    conn = _db(tmp_path / "b", [(200_000, "no"), (205_000, "no")])
    assert ss.settled_brackets(conn) == {}


def test_events_that_settled_before_the_registration_are_not_forward(tmp_path):
    """Five of them exist and every one is a firing week the discovery already used. A
    scorer that counted them would reach 6/6 on the day it was written."""
    conn = _db(tmp_path, [(200_000, "yes"), (210_000, "no")],
               close="2026-03-26T12:25:00+00:00")
    rep = ss.run(conn)
    assert rep["n_settled_since_registration"] == 0 and rep["n_firing"] == 0
    assert datetime.fromisoformat("2026-03-26T12:25:00+00:00") < REG


def test_an_empty_grid_is_floored_rather_than_minus_infinity(monkeypatch):
    """Six events decide PR-11. One -inf would decide it alone, and in whichever
    direction the narrower arm happened to fall — so the blow-up case is floored too.
    Both arms hit the same floor, so a real blow-up still costs the narrower arm."""
    import math
    monkeypatch.setattr("prediction_market_macro.model.common.grid_pmf",
                        lambda *_a, **_k: {})

    class _P:
        dist = None
    assert ss._interval_ll(_P, 210_000.0) == pytest.approx(math.log(ss.MASS_FLOOR))


def test_the_interval_ll_is_the_models_own_grid_probability():
    """`grid_pmf` at the ladder step, not a re-derived density: it is the same
    discretisation `leg_fair` prices the settled legs off."""
    import math

    from prediction_market_macro.model.common import GaussianMix, grid_pmf

    class _P:
        dist = GaussianMix(((1.0, 210_000.0, 8_000.0),))
    pmf = grid_pmf(_P.dist, ss.GRID)
    cell = min(pmf, key=lambda k: abs(k - 207_500.0))
    assert ss._interval_ll(_P, 207_500.0) == pytest.approx(math.log(pmf[cell]))
    # a print far off the end of the grid must stay finite and floored. The floor is what
    # keeps ONE such event from becoming -inf and deciding the whole registration by
    # itself — an unbounded penalty is not a measurement, it is a veto.
    # a print far off the end of the grid must stay finite and floored
    far = ss._interval_ll(_P, 1e9)
    assert math.isfinite(far) and far <= math.log(ss.MASS_FLOOR) + 1.0
