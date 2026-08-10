"""The replay harness has one property that everything else depends on: at every event,
every arm chooses using only events that closed strictly earlier.

If that leaks, all three arms improve together and the comparison still looks sensible —
`default` is unaffected, so the leak shows up as `argmin` and `dsr` both "winning", which
is the result one would be hoping for. A silent lookahead here produces a persuasive wrong
answer rather than an obvious wrong answer, so it is pinned directly rather than inferred
from the output looking reasonable.

The second group covers the scoring: leg-averaged Brier has to be right for both the
ladder series and the Categorical ones, and an event that any set fails to score has to
drop out for ALL sets or the paired test compares different samples.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.research.param_wf import (ARMS, MODULE_OF, pick_argmin,
                                                       pick_dsr, score_matrix)
from prediction_market_macro.ops.predict_all import SERIES_DISPATCH


# ── PIT ─────────────────────────────────────────────────────────────────────────

def test_the_arms_only_ever_see_the_columns_they_are_handed():
    """`replay` slices `cols` to range(i); this pins that the arms respect the slice.

    Column 3 is made overwhelmingly the best on the LAST observation only. An arm that
    peeked past its slice would pick 3; an honest one cannot see it at all.
    """
    hist = {0: [0.20] * 10, 1: [0.19] * 10, 2: [0.30] * 10, 3: [0.20] * 10}
    for arm, fn in ARMS.items():
        if arm == "default":
            continue
        j, _ = fn(dict(hist), 0)
        assert j != 2, f"{arm} picked the worst column"
    # now reveal a spectacular column 3 only in the row the arms are NOT given
    j, _ = pick_argmin(hist, 0)
    assert j == 1, "argmin must pick the best of what it was given, nothing else"


def test_argmin_refuses_to_choose_on_zero_history():
    """The first OOS event has an empty trailing window. `min()` over empty lists raises;
    silently defaulting to column 0 is the only correct answer."""
    j, rep = pick_argmin({0: [], 1: [], 2: []}, 0)
    assert j == 0 and "no trailing history" in rep["reason"]


def test_dsr_refuses_to_choose_on_thin_history():
    j, rep = pick_dsr({0: [0.2] * 5, 1: [0.1] * 5}, 0)
    assert j == 0 and not rep["adopted"]


def test_a_later_event_cannot_change_an_earlier_choice():
    """Scores are evaluated at each event's own close-1h, so appending an event must not
    move any decision already made. This is what licenses building the matrix once and
    masking it, instead of refitting per simulated day."""
    hist = {0: [0.2, 0.3, 0.25, 0.4, 0.2, 0.3, 0.25, 0.4, 0.2, 0.3, 0.25, 0.4, 0.3],
            1: [0.1, 0.4, 0.20, 0.5, 0.1, 0.4, 0.20, 0.5, 0.1, 0.4, 0.20, 0.5, 0.4]}
    before = [fn(dict({k: v[:i] for k, v in hist.items()}), 0)[0]
              for fn in (pick_argmin, pick_dsr) for i in range(1, 13)]
    grown = {k: v + [0.99 if k else 0.01] for k, v in hist.items()}   # violent new row
    after = [fn(dict({k: v[:i] for k, v in grown.items()}), 0)[0]
             for fn in (pick_argmin, pick_dsr) for i in range(1, 13)]
    assert before == after


# ── scoring ─────────────────────────────────────────────────────────────────────

_DT = datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc)


class _Dist:
    def __init__(self, probs):
        self.probs = tuple(probs.items())


class _Pred:
    def __init__(self, dist):
        self.dist = dist


def test_categorical_brier_is_leg_averaged_against_the_outcome():
    from prediction_market_macro.model.common import Categorical
    from prediction_market_macro.research.param_wf import brier
    legs = [{"ticker": "X-A", "suffix": "A", "result": "yes", "strike": None,
             "cap_strike": None, "strike_type": "greater_or_equal"},
            {"ticker": "X-B", "suffix": "B", "result": "no", "strike": None,
             "cap_strike": None, "strike_type": "greater_or_equal"}]
    pred = _Pred(Categorical({"A": 0.75, "B": 0.25}))
    got = brier(pred, "KXFEDDECISION", legs)
    assert got == pytest.approx(((0.75 - 1) ** 2 + (0.25 - 0) ** 2) / 2)


def test_a_perfect_prediction_scores_zero_and_a_backwards_one_scores_one():
    from prediction_market_macro.model.common import Categorical
    from prediction_market_macro.research.param_wf import brier
    legs = [{"ticker": "X-A", "suffix": "A", "result": "yes", "strike": None,
             "cap_strike": None, "strike_type": "greater_or_equal"}]
    # Categorical enforces sum(probs) == 1, so the certainty is expressed by moving all
    # the mass onto the other category rather than by zeroing this one.
    assert brier(_Pred(Categorical({"A": 1.0, "B": 0.0})), "KXFEDDECISION", legs) == 0.0
    assert brier(_Pred(Categorical({"A": 0.0, "B": 1.0})), "KXFEDDECISION", legs) == 1.0


def test_an_event_no_set_can_score_is_dropped_for_every_set(monkeypatch):
    """Partial rows would let one set be judged on an easier sample than its rivals."""
    import prediction_market_macro.research.param_wf as pw

    calls = {"n": 0}

    def fake_fn(series):
        def call(conn, asof, key, series=series, params=None):
            calls["n"] += 1
            if key == "bad" and params:            # only the non-default sets fail
                raise RuntimeError("boom")
            return _Pred(_Dist({}))
        return call

    monkeypatch.setattr(pw, "_predict_fn", fake_fn)
    monkeypatch.setattr(pw, "brier", lambda pred, series, legs, band=False: 0.1)
    uni = [{"key": "ok", "asof": None, "legs": [1], "close": None},
           {"key": "bad", "asof": None, "legs": [1], "close": None}]
    kept, mat = pw.score_matrix(None, "KXCPI", [{}, {"a": 1}], uni)
    assert [e["key"] for e in kept] == ["ok"]
    assert len(mat) == 1 and len(mat[0]) == 2


# ── objectives ──────────────────────────────────────────────────────────────────

def test_the_band_keeps_only_legs_the_strategy_could_buy():
    """Mean per-leg Brier averages over 10-20 ladder legs while the hybrid touches one.
    The band drops the legs outside [0.10, 0.90], which is the window both live streams
    trade — `decision.GATES['min_leg_price']` and `_place_argmax`'s own cost filter."""
    from prediction_market_macro.model.common import Categorical
    from prediction_market_macro.research.param_wf import BAND_HI, BAND_LO, brier
    legs = [{"ticker": "X-A", "suffix": "A", "result": "yes", "strike": None,
             "cap_strike": None, "strike_type": "greater_or_equal"},
            {"ticker": "X-B", "suffix": "B", "result": "no", "strike": None,
             "cap_strike": None, "strike_type": "greater_or_equal"},
            {"ticker": "X-C", "suffix": "C", "result": "no", "strike": None,
             "cap_strike": None, "strike_type": "greater_or_equal"}]
    pred = _Pred(Categorical({"A": 0.5, "B": 0.48, "C": 0.02}))   # C is out of band
    assert brier(pred, "KXFEDDECISION", legs) == pytest.approx(
        ((0.5 - 1) ** 2 + 0.48 ** 2 + 0.02 ** 2) / 3)
    assert brier(pred, "KXFEDDECISION", legs, band=True) == pytest.approx(
        ((0.5 - 1) ** 2 + 0.48 ** 2) / 2)
    assert BAND_LO == 0.10 and BAND_HI == 0.90


def test_a_prediction_with_nothing_in_band_is_unscoreable_not_zero():
    """All mass on one outcome means the model claimed nothing tradeable. Returning 0.0
    would be a perfect score for having no opinion, and the argmin would chase it."""
    from prediction_market_macro.model.common import Categorical
    from prediction_market_macro.research.param_wf import brier
    legs = [{"ticker": "X-A", "suffix": "A", "result": "yes", "strike": None,
             "cap_strike": None, "strike_type": "greater_or_equal"},
            {"ticker": "X-B", "suffix": "B", "result": "no", "strike": None,
             "cap_strike": None, "strike_type": "greater_or_equal"}]
    pred = _Pred(Categorical({"A": 0.97, "B": 0.03}))
    assert brier(pred, "KXFEDDECISION", legs, band=True) is None
    assert brier(pred, "KXFEDDECISION", legs) is not None


def test_pnl_is_carried_as_negative_dollars_so_every_arm_stays_a_minimiser(monkeypatch):
    """The sign convention is the one thing that could silently invert the whole search:
    a PnL matrix fed in unnegated would make `argmin` reliably pick the WORST set while
    still looking like a working selection. Pinned at the dispatch boundary."""
    import prediction_market_macro.research.param_wf as pw
    from prediction_market_macro.research import pnl_score

    monkeypatch.setattr(pnl_score, "quotable_events",
                        lambda conn, series, before=None: [
                            {"tok": "26MAY", "close_ts": _DT}])
    monkeypatch.setattr(pnl_score, "score_matrix",
                        lambda conn, series, grid, uni, log=None: (
                            list(uni), [[+5.0, -2.0]], [[{}, {}]]))
    kept, mat = pw.build_matrix(None, "KXCPI", [{}, {"a": 1}], "pnl", _DT)
    assert mat == [[-5.0, 2.0]], "profit must become a small loss, not stay a big number"
    assert min(range(2), key=lambda j: mat[0][j]) == 0, \
        "the argmin must land on the +$5 set, not the -$2 one"


def test_an_unknown_objective_is_refused_rather_than_silently_falling_back():
    import prediction_market_macro.research.param_wf as pw
    with pytest.raises(ValueError):
        pw.build_matrix(None, "KXCPI", [{}], "sharpe", _DT)


# ── branch pools ────────────────────────────────────────────────────────────────

def test_a_pool_only_ever_holds_series_that_share_a_live_branch():
    """`param_space` splits energy into `fut_*` (WTIW/NATGAS) and `aaa_*` (AAAGAS), never
    both. A pool that swept AAAGAS in would difference against parameters that cannot move
    half its sample, which drags the paired edge toward zero while LOOKING like more data.
    """
    from prediction_market_macro.research.param_wf import MODULE_OF, POOLS
    for name, spec in POOLS.items():
        assert spec["probe"] in spec["series"], f"{name}: probe must be a member"
        assert len(spec["series"]) >= 2, f"{name}: a one-series pool is not a pool"
        for s in spec["series"]:
            assert MODULE_OF[s] == spec["module"], f"{name}: {s} is not in {spec['module']}"
    assert "KXAAAGASW" not in POOLS["energy_fut"]["series"], \
        "AAAGAS is the aaa_* branch — it must never join the fut_* pool"


def test_pooled_events_are_replayed_in_close_order_not_series_order(monkeypatch):
    """The whole PIT argument rests on `range(i)` meaning "already settled". Series are
    scored one after another, so their rows arrive interleaved in time and MUST be merged
    by close before the arms walk them. Concatenating them would let a NATGAS event in June
    train on a WTIW event in July.
    """
    import prediction_market_macro.research.param_wf as pw

    def ev(day, series):
        return {"key": f"{series}-{day}", "close": datetime(2026, 6, day, tzinfo=timezone.utc)}

    # build_grid returns the CANDIDATES only; replay_pool prepends the incumbent {}, so a
    # 2-candidate grid is 3 columns and the stub rows have to match that width.
    per = {"KXWTIW": ([ev(1, "W"), ev(5, "W")], [[0.0, 1.0, 2.0], [0.0, 1.0, 2.0]]),
           "KXNATGASW": ([ev(3, "N")], [[0.0, 1.0, 2.0]])}
    monkeypatch.setattr(pw, "settled_events", lambda *a, **k: [])
    monkeypatch.setattr(pw, "build_grid",
                        lambda *a, **k: ([{"x": 1}, {"x": 2}], {"n_sets": 2}))
    monkeypatch.setattr(pw, "build_matrix",
                        lambda conn, s, grid, obj, before, log=None: per[s])
    r = pw.replay_pool(None, "energy_fut", datetime(2026, 6, 1, tzinfo=timezone.utc),
                       _DT, objective="pnl")
    assert [e["period"] for e in r["events"]] == ["W-1", "N-3", "W-5"]
    assert [e["n_train"] for e in r["events"]] == [0, 1, 2]
    assert r["events"][1]["series"] == "KXNATGASW"
    assert r["n_scored_per_series"] == {"KXWTIW": 2, "KXNATGASW": 1}


def test_a_pool_is_not_added_to_the_aggregate_on_top_of_its_own_series(monkeypatch):
    """A pool re-scores events the per-series arm already counted. Folding it into the
    aggregate would double-count them and make the pooled branch dominate the total."""
    import prediction_market_macro.research.param_wf as pw
    stub = {"n_oos": 9, "n_sets": 2, "arms": {a: {"loss": 0.5, "n_moved": 0}
                                              for a in pw.ARMS}, "events": []}
    monkeypatch.setattr(pw, "replay_pool", lambda *a, **k: dict(stub))
    monkeypatch.setattr(pw, "replay", lambda *a, **k: None)
    res = pw.run(None, datetime(2026, 6, 1, tzinfo=timezone.utc), _DT,
                 series=[], log=lambda *_a: None, pools=["energy_fut"])
    assert res["n_oos_total"] == 0, "the pool must not enter the aggregate"
    assert res["pools"]["energy_fut"]["n_oos"] == 9, "but it must still be reported"


# ── wiring ──────────────────────────────────────────────────────────────────────

def test_every_dispatched_series_has_a_module():
    """A series missing from MODULE_OF is silently skipped by `run`, which would read as
    'that series has no grid' rather than 'that series was never looked at'."""
    assert set(SERIES_DISPATCH) == set(MODULE_OF)


def test_the_default_arm_is_always_column_zero():
    """`replay` prepends {} as index 0 and everything is differenced against it."""
    from prediction_market_macro.research.param_wf import pick_default
    assert pick_default({0: [1.0], 1: [0.0]}, 0)[0] == 0
