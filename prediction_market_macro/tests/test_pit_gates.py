"""The point of `pit_gates` is that the backtest and the live path run the same gates.
There are two ways it could fail silently, and both produce a plausible-looking number:

  1. **It leaks.** An event's own outcome reaches the map/ratio that gates its own trade,
     which is the exact objection that got these gates dropped from the walk-forward in
     the first place. A leaking version looks BETTER, so nothing flags it.
  2. **It diverges.** The gate is present but computed or applied differently from
     `decide_all` — a different threshold, a different order, an argmax leg that keeps
     firing on a series production has blocked. Then the displayed history and the live
     ledger are again two strategies wearing one track record.

Everything below pins one of those two.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.research import pit_gates as pg
from prediction_market_macro.strategy import conformal as _conformal
from prediction_market_macro.strategy import skill as _skill

D = datetime(2026, 7, 1, 16, tzinfo=timezone.utc)


def _state(**kw):
    base = {"series": "KXWTIW", "n_events": 5, "cal_xy": None, "capture": {},
            "skill_ratio": None, "conformal_factor": 1.0}
    return pg.GateState(**{**base, **kw})


# ── the skill thresholds are decide_all's, not near-misses ──────────────────────

@pytest.mark.parametrize("ratio, defensive, blocked", [
    (None, False, False),
    (1.00, False, False),
    (_skill.RATIO_MAX, False, False),            # boundary: `>` not `>=`
    (_skill.RATIO_MAX + 0.01, True, False),
    (_skill.BLOCK_RATIO, True, False),
    (_skill.BLOCK_RATIO + 0.01, True, True),
])
def test_skill_thresholds_match_the_live_module(ratio, defensive, blocked):
    s = _state(skill_ratio=ratio)
    assert (s.defensive is not None) is defensive
    assert (s.blocked is not None) is blocked


def test_apply_gates_compounds_conformal_and_defensive_the_way_decide_all_does():
    """decide_all multiplies max_size by the conformal factor and THEN halves it for a
    defensive series. Applying only one, or applying them in the other order to a bar
    that is also being doubled, silently changes the size of every trade."""
    g = {"max_size_usd": 1.0, "min_net_edge": 0.04, "fav_min_edge_per_day": 0.008,
         "max_entropy_norm": 0.95}
    out = _state(skill_ratio=1.2, conformal_factor=0.5).apply_gates(g)
    assert out["max_size_usd"] == pytest.approx(0.25)     # 1.0 * 0.5 * 0.5
    assert out["min_net_edge"] == pytest.approx(0.08)
    assert out["fav_min_edge_per_day"] == pytest.approx(0.016)
    assert g["max_size_usd"] == 1.0, "must not mutate the caller's gate dict"


def test_a_series_at_parity_gets_the_gates_untouched():
    g = {"max_size_usd": 1.0, "min_net_edge": 0.04, "fav_min_edge_per_day": 0.008}
    assert _state(skill_ratio=1.0).apply_gates(g) == g


# ── calibration: identity unless there is really a map ──────────────────────────

def test_calibrate_structs_is_identity_without_a_map():
    from prediction_market_macro.strategy.edge import Leg, Struct
    st = Struct(kind="single", desc="x", legs=(Leg("T", "yes", 0.40, 1e9),), fair=0.62,
                cost=0.40, max_loss=0.40)
    out = _state().calibrate_structs([st])
    assert out[0] is st, "no map ⇒ the same object, not a rebuilt one"


def test_calibrate_structs_uses_the_same_interp_the_live_path_uses():
    from prediction_market_macro.strategy.calibration import interp
    from prediction_market_macro.strategy.edge import Leg, Struct
    xy = ([0.0, 0.5, 1.0], [0.0, 0.3, 1.0])
    st = Struct(kind="single", desc="x", legs=(Leg("T", "yes", 0.40, 1e9),), fair=0.5,
                cost=0.40, max_loss=0.40)
    out = _state(cal_xy=xy).calibrate_structs([st])
    assert out[0].fair == pytest.approx(round(interp((None, *xy), 0.5), 6))
    assert out[0].fair == pytest.approx(0.3)


# ── the history slice: strictly earlier, and it moves when the sample moves ─────

def _history(monkeypatch, closes, per, dec_trades=(), params_pit=False, params_at=None):
    """The two replays stubbed out; these tests are about the SLICE, not the replay.

    `params_pit` and `params_at` are asserted rather than ignored: #198 threads the first
    through both calls and #199 threads the second, so a stub that silently swallowed
    either would let BOTH replays quietly fall back to the registered defaults while every
    test still passed. That is the precise failure #199 exists to fix, so it must not be
    reachable through the test helper.
    """
    def _replay(conn, series, max_events=200, collect_legs=False, **kw):
        assert kw.get("params_pit") is params_pit
        assert kw.get("params_at") is params_at
        return {"per_release": per}

    def _decide(conn, series, max_events=200, collect_trades=False, **kw):
        assert kw.get("params_pit") is params_pit
        assert kw.get("params_at") is params_at
        return {"trades": list(dec_trades)}
    monkeypatch.setattr(pg, "period_closes", lambda conn, series: closes)
    monkeypatch.setattr(
        "prediction_market_macro.research.backtest.replay_series", _replay)
    monkeypatch.setattr(
        "prediction_market_macro.research.eval.decision_replay", _decide)
    return pg.GateHistory(None, "KXWTIW", params_pit=params_pit, params_at=params_at)


def _rec(period, bm, bk, legs=()):
    return {"period": period, "brier_model-1h": bm, "brier_market-1h": bk,
            "legs-1h": list(legs)}


def test_an_event_closing_on_the_simulated_day_is_not_in_its_own_gate_state(monkeypatch):
    """The decision is taken at 16:00 on day D against an event closing at 16:00 on D.
    Including it would score the model with the outcome it is about to bet on — the
    leakage that justified excluding these gates entirely."""
    closes = {"a": D - timedelta(days=1), "b": D}
    h = _history(monkeypatch, closes, [_rec("a", 0.1, 0.2), _rec("b", 0.9, 0.1)])
    assert h.asof(D).n_events == 1
    assert h.asof(D + timedelta(seconds=1)).n_events == 2


def test_the_skill_ratio_is_the_trailing_paired_mean_over_earlier_events(monkeypatch):
    closes = {f"p{i}": D - timedelta(days=30 - i) for i in range(8)}
    per = [_rec(f"p{i}", 0.30, 0.10) for i in range(8)]        # model 3x worse
    h = _history(monkeypatch, closes, per)
    st = h.asof(D)
    assert st.n_events == 8
    assert st.skill_ratio == pytest.approx(3.0)
    assert st.blocked is not None, "3.0 > BLOCK_RATIO must block"


def test_the_ratio_stays_none_below_the_live_min_paired(monkeypatch):
    n = _skill.MIN_PAIRED - 1
    closes = {f"p{i}": D - timedelta(days=10 - i) for i in range(n)}
    h = _history(monkeypatch, closes, [_rec(f"p{i}", 0.3, 0.1) for i in range(n)])
    assert h.asof(D).skill_ratio is None


def test_the_ratio_ignores_events_with_no_market_score(monkeypatch):
    """`skill_ratio` pairs the two sources per event; an event the market was never
    scored on is not a pair and counting it as one biases the ratio by whichever side
    survives."""
    closes = {f"p{i}": D - timedelta(days=10 - i) for i in range(8)}
    per = ([_rec(f"p{i}", 0.30, 0.10) for i in range(6)]
           + [_rec("p6", 0.9, None), _rec("p7", None, 0.1)])
    assert _history(monkeypatch, closes, per).asof(D).skill_ratio == pytest.approx(3.0)


def test_calibration_is_pinned_to_identity_at_any_sample_size(monkeypatch):
    """2026-08-10 pin: the maps this harness used to fit were built on
    event-clustered per-leg pairs and produced 0/1-certainty plateaus (live decision
    #4346 bought a raw-fair-0.334 leg as fair 0.60 off the live twin of one), so BOTH
    the live path and this harness now apply no map. This test used to assert the
    opposite (fit once >= MIN_PAIRS); flipping it is the point, not an accident."""
    from prediction_market_macro.strategy.calibration import MIN_PAIRS
    closes = {"a": D - timedelta(days=1)}
    fat = [_rec("a", 0.1, 0.1,
                [(i / (2 * MIN_PAIRS), 0.5, float(i > MIN_PAIRS))
                 for i in range(2 * MIN_PAIRS)])]
    assert _history(monkeypatch, closes, fat).asof(D).cal_xy is None


def test_capture_memory_only_counts_trades_that_had_already_settled(monkeypatch):
    closes = {"a": D - timedelta(days=2), "b": D + timedelta(days=2)}
    dec = [{"period": "a", "cap_key": "yes@+1", "expected": 1.0, "realized": 0.1},
           {"period": "b", "cap_key": "yes@+1", "expected": 5.0, "realized": 5.0}]
    h = _history(monkeypatch, closes, [_rec("a", 0.1, 0.1), _rec("b", 0.1, 0.1)], dec)
    caps = h.asof(D).capture
    assert caps["yes@+1"] == {"n": 1, "expected": 1.0, "realized": 0.1}


def test_the_conformal_factor_is_the_live_evaluate_sequence(monkeypatch):
    closes = {f"p{i}": D - timedelta(days=40 - i) for i in range(12)}
    scores = [0.05] * 11 + [0.95]                     # the last one breaches
    per = [_rec(f"p{i}", scores[i], 0.1) for i in range(12)]
    st = _history(monkeypatch, closes, per).asof(D)
    assert st.conformal_factor == _conformal.evaluate_sequence(scores)["factor"]
    assert st.conformal_factor == _conformal.BREACH_FACTOR


def test_a_record_whose_period_has_no_close_is_dropped_not_dated_to_epoch(monkeypatch):
    """An unplaceable event sorted to the front of the timeline would be inside EVERY
    as-of slice, including days before it happened."""
    h = _history(monkeypatch, {"a": D - timedelta(days=1)},
                 [_rec("a", 0.1, 0.1), _rec("orphan", 0.9, 0.1)])
    assert h.asof(D - timedelta(days=5)).n_events == 0
    assert h.asof(D).n_events == 1


# ── the simulated risk book mirrors ops.risk ───────────────────────────────────

def test_sim_risk_veto_enforces_each_live_limit():
    from prediction_market_macro.ops.risk import LIMITS
    from prediction_market_macro.research.walkforward import _sim_risk_veto as veto
    assert veto([], "KXWTIW", "2026-07-31", 0.5) is None
    ev = [{"series": "KXWTIW", "period": "2026-07-31",
           "size_usd": LIMITS["per_event_usd"]}]
    assert "risk_per_event" in veto(ev, "KXWTIW", "2026-07-31", 0.5)
    assert "risk_release_day" in veto([], "KXWTIW", "2026-07-31", 0.5,
                                      opened_today=LIMITS["per_release_day_usd"])


def test_sim_risk_veto_counts_the_family_not_just_the_series():
    """`ops.risk` aggregates by family, so an energy position taken on KXNATGASW
    constrains KXWTIW. A per-series-only version would let the sim take exposure the
    live path refuses."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.ops.risk import LIMITS
    from prediction_market_macro.research.walkforward import _sim_risk_veto as veto
    assert REGISTRY["KXNATGASW"].family == REGISTRY["KXWTIW"].family
    book = [{"series": "KXNATGASW", "period": "2026-07-24",
             "size_usd": LIMITS["per_family_usd"]}]
    assert "risk_per_family" in veto(book, "KXWTIW", "2026-07-31", 0.5)


@pytest.fixture()
def conn():
    from prediction_market_macro.ingest.store import SCHEMA
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def test_gates_and_params_are_off_when_the_run_asks_for_pure_gates(conn):
    from prediction_market_macro.research.walkforward import _GateBook
    b = _GateBook(conn, db_gates=False, select_params=False)
    assert b.gates_for("KXWTIW", D) is None
    assert b.params_for("KXWTIW", D) is None


def test_the_param_book_rescores_once_per_sample_not_once_per_day(conn, monkeypatch):
    """Without the fingerprint cache a 60-day run is 60 full grid replays per series,
    which is not a slow test — it is a run nobody will ever wait for."""
    from prediction_market_macro.research import param_select as ps
    from prediction_market_macro.research.walkforward import _GateBook
    calls = []
    monkeypatch.setattr(ps, "_sample_key", lambda c, s, d: f"k{(d.day - 1) // 10}")
    monkeypatch.setattr(ps, "select_for", lambda c, s, d, log=None: (
        calls.append(d) or ({}, {})))
    b = _GateBook(conn, db_gates=False, select_params=True)
    for i in range(1, 21):                 # 20 days, two 10-day fingerprint blocks
        b.params_for("KXWTIW", D.replace(day=i))
    assert len(calls) == 2, "one rescore per distinct sample fingerprint"
    assert b.stats["param_rescores"] == 2


def test_a_selector_failure_falls_back_to_defaults_instead_of_aborting(conn,
                                                                      monkeypatch):
    from prediction_market_macro.research import param_select as ps
    from prediction_market_macro.research.walkforward import _GateBook
    monkeypatch.setattr(ps, "_sample_key", lambda c, s, d: "k")
    monkeypatch.setattr(ps, "select_for", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("grid blew up")))
    assert _GateBook(conn, db_gates=False, select_params=True).params_for("KXWTIW",
                                                                         D) is None


# ── #199: the gate history is built at the params the sim actually predicts with ──

def _spy_gatehistory(monkeypatch):
    """Capture the kwargs `_GateBook.gates_for` hands GateHistory, without building one."""
    seen = {}

    class _H:
        def __init__(self, conn, series, **kw):
            seen.update(kw)

        def asof(self, day):
            return "STATE"
    monkeypatch.setattr(pg, "GateHistory", _H)
    return seen


def test_the_gate_history_is_built_at_the_books_own_per_day_params(conn, monkeypatch):
    """The #199 fix. Before it, the sim predicted at the selected params while the skill /
    capture / conformal state was replayed at the registered defaults — a pairing that ran
    neither in production nor in any backtest."""
    from prediction_market_macro.research import param_select as ps
    from prediction_market_macro.research.walkforward import _GateBook
    monkeypatch.setattr(ps, "_sample_key", lambda c, s, d: "k")
    monkeypatch.setattr(ps, "select_for", lambda c, s, d, log=None: ({"w": 0.5}, {}))
    seen = _spy_gatehistory(monkeypatch)

    b = _GateBook(conn, db_gates=True, select_params=True)
    assert b.gates_for("KXWTIW", D) == "STATE"
    at = seen["params_at"]
    assert callable(at)
    # and it must resolve to what the PREDICTION path would get for that same moment
    assert at(D) == b.params_for("KXWTIW", D) == {"w": 0.5}


def test_the_ab_switch_restores_the_pre_199_pairing(conn, monkeypatch):
    from prediction_market_macro.research import param_select as ps
    from prediction_market_macro.research.walkforward import _GateBook
    monkeypatch.setattr(ps, "_sample_key", lambda c, s, d: "k")
    monkeypatch.setattr(ps, "select_for", lambda c, s, d, log=None: ({"w": 0.5}, {}))
    seen = _spy_gatehistory(monkeypatch)

    _GateBook(conn, db_gates=True, select_params=True,
              gate_params=False).gates_for("KXWTIW", D)
    assert seen["params_at"] is None


def test_a_research_param_override_reaches_the_gates_too(conn, monkeypatch):
    """`param_override` beats both selector lanes on the prediction path, so a gate state
    built without it would describe a config the sweep arm never ran."""
    from prediction_market_macro.research.walkforward import _GateBook
    seen = _spy_gatehistory(monkeypatch)
    b = _GateBook(conn, db_gates=True, select_params=False,
                  param_override={"KXWTIW": {"vol_window": 30}})
    b.gates_for("KXWTIW", D)
    assert seen["params_at"](D) == {"vol_window": 30}


def test_with_the_selector_off_the_thread_is_a_noop(conn, monkeypatch):
    """`--fixed-params` must be bit-identical before and after #199: the per-day choice is
    the registered defaults, so the callable returns None at every asof and both replays
    take their `eff = params` branch exactly as they did."""
    from prediction_market_macro.research.walkforward import _GateBook
    seen = _spy_gatehistory(monkeypatch)
    _GateBook(conn, db_gates=True, select_params=False).gates_for("KXWTIW", D)
    at = seen["params_at"]
    assert at is not None                       # passed, not omitted
    assert all(at(D + timedelta(days=i)) is None for i in range(-30, 5))


def test_the_gate_history_is_still_built_once_per_series(conn, monkeypatch):
    """`params_at` is a closure over `series`; building it per call would be harmless, but
    rebuilding the HISTORY per call is two full replays per simulated day."""
    from prediction_market_macro.research.walkforward import _GateBook
    builds = []

    class _H:
        def __init__(self, conn, series, **kw):
            builds.append(series)

        def asof(self, day):
            return "STATE"
    monkeypatch.setattr(pg, "GateHistory", _H)
    b = _GateBook(conn, db_gates=True, select_params=False)
    for i in range(10):
        b.gates_for("KXWTIW", D + timedelta(days=i))
    assert builds == ["KXWTIW"]
