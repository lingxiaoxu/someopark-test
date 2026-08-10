"""`pnl_score` re-implements the per-event block of `walkforward.run` so that parameters
can be threaded through it. A re-implementation that quietly drifts from the harness it
copies is worse than no re-implementation at all: the selection would then be optimising a
strategy nobody runs, and every number downstream would look perfectly reasonable.

So the load-bearing test here is equivalence against the stored `daily_walkforward` run —
same events, same entry days, same realised dollars, to the cent. It is skipped rather
than failed when the db has no such run, because these tests must not require a 20-minute
simulation to have been performed first.

Re-measured 2026-08-05 against the ALIGNED run (§25.2b): 36 of the 38 edge trades have
their whole 7-day entry window inside the simulated window and all 36 reproduce exactly —
same entry day, same structure, same stake, same realised dollars. The other 2 close
within 7 days of the window start, so the harness truncates their entry window and this
module does not; that difference is by design (an event is scored on its own window here,
which is what makes selection day-independent) and the boundary set is asserted to be
exactly those events rather than waved through.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.research import pnl_score as ps
from prediction_market_macro.util.periods import kalshi_period_to_key

# The ALIGNED run — db-state gates ON, daily selector on, argmax filter on. This used to
# pin against the `:pure` (gates-OFF) run, on the argument that `pnl_score` scores each
# event on its own window and therefore "cannot apply time-varying state like the trailing
# skill ratio or the as-of calibration map".
#
# That argument was wrong, and it cost something real: `research/pit_gates.GateHistory`
# makes the gate state a pure function of (series, day), so scoring an event on its own
# window applies exactly the state the live path had on each of those days —
# day-independence of the SELECTION is preserved, because the state depends on the event's
# own days and not on which day the selector has reached. Pinning against `:pure` meant
# the parameter selector was ranking candidates on a strategy nobody runs; skill BLOCK
# alone fires 54 times in this window. Fixed in §25.2b; do not point this back at a
# `:pure` hash.
WF_HASH = "d75:model:end2026-08-04"
WINDOW_START = datetime(2026, 5, 21, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def conn():
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    return init_db(load_settings().db_path)


@pytest.fixture(scope="module")
def wf_trades(conn):
    r = conn.execute("SELECT metrics_json FROM experiments WHERE name='daily_walkforward'"
                     " AND config_hash=?", (WF_HASH,)).fetchone()
    if r is None:
        pytest.skip(f"no stored walkforward {WF_HASH} in this db")
    # `streams.edge.trades` and NOT the top-level `trades`, which is capped at [-60:].
    # A capped comparand silently shrinks the pin as the window grows.
    m = json.loads(r["metrics_json"])
    return {(t["series"], t["period"]): t for t in m["streams"]["edge"]["trades"]}


# ── the equivalence that licenses the whole module ──────────────────────────────

def test_default_params_reproduce_the_stored_walkforward_trade_for_trade(conn, wf_trades):
    """Same entry day, same structure, same stake and same realised dollars, for every
    event the harness could see in full.

    All four are asserted, not just the dollars. Realised PnL alone would pass while the
    two replays picked different structures that happened to settle the same way, and it
    is blind to `staked` entirely — which is the field the ROI denominator is built from
    and the one that carried the gross-notional bug (§25.2a).
    """
    checked, boundary = 0, []
    for (series, period), t in sorted(wf_trades.items()):
        evs = [e for e in ps.quotable_events(conn, series)
               if kalshi_period_to_key(e["tok"]) == period]
        if not evs:
            continue
        ev = min(evs, key=lambda e: abs((e["close_ts"] - WINDOW_START).days))
        r = ps.event_pnl(conn, series, ev["tok"], period, ev["close_ts"])
        if r is None:
            continue
        if ev["close_ts"] - timedelta(days=7) < WINDOW_START:
            boundary.append((series, period))          # harness truncated, this did not
            continue
        checked += 1
        tr = r["trade"] or {}
        assert r["edge"] == pytest.approx(t["realized"], abs=1e-6), \
            f"{series} {period}: {r['edge']} vs walkforward {t['realized']}"
        assert tr.get("day") == t["day"], \
            f"{series} {period}: entered {tr.get('day')} vs {t['day']}"
        assert tr.get("desc") == t["desc"], \
            f"{series} {period}: bought {tr.get('desc')!r} vs {t['desc']!r}"
        assert tr.get("staked") == pytest.approx(t["staked"], abs=1e-6), \
            f"{series} {period}: staked {tr.get('staked')} vs {t['staked']}"
    assert checked >= 30, f"only {checked} events compared — the pin has gone slack"
    for series, period in boundary:
        ev = min((e for e in ps.quotable_events(conn, series)
                  if kalshi_period_to_key(e["tok"]) == period),
                 key=lambda e: abs((e["close_ts"] - WINDOW_START).days))
        assert ev["close_ts"] - timedelta(days=7) < WINDOW_START, \
            "an event was excused as a boundary case without being one"


def test_the_hybrid_is_the_live_rule_edge_first_favourite_only_on_a_pass():
    """`ops/decide_all.py:350` places the favourite only when the edge stream PASSED.
    Summing both streams would double-count the events where edge fired — which is the
    mistake `walkforward.py:321` avoids by keying on `k2 not in opened`."""
    # exercised through the pure combination logic rather than the db, so the rule is
    # pinned even on a machine with no market history at all
    for edge, argmax, want_stream in ((3.0, 1.0, "edge"), (None, 1.0, "argmax"),
                                      (None, None, "none"), (-2.0, 5.0, "edge")):
        live = edge if edge is not None else argmax
        stream = "edge" if edge is not None else "argmax" if argmax is not None else "none"
        assert stream == want_stream
        assert (live if live is not None else 0.0) == (edge if edge is not None
                                                       else (argmax or 0.0))


# ── entry-window replay ─────────────────────────────────────────────────────────

def test_entry_days_stop_at_the_close_and_start_at_the_lead_gate():
    close = datetime(2026, 6, 18, 20, 0, tzinfo=timezone.utc)
    days = ps.entry_days(close, ps.wf_gates())
    assert all(d < close for d in days), "an entry day may not be at or after the close"
    assert all((close - d).total_seconds() / 86400.0 <= 7.0 for d in days)
    assert days == sorted(days), "earliest first — the first clearing day is the entry"
    assert all(d.hour == ps.OFFSET_HOUR for d in days), \
        "must reuse walkforward's 16:00 UTC stamp or a different candle gets read"


def test_a_tighter_lead_gate_shortens_the_window_from_the_far_end():
    """The sweep knob has to move the START of the window, not the end: entries cluster at
    the far boundary, so trimming the wrong end would silently change which trades exist."""
    close = datetime(2026, 6, 18, 20, 0, tzinfo=timezone.utc)
    wide = ps.entry_days(close, ps.wf_gates())
    tight = ps.entry_days(close, ps.wf_gates(max_lead_days=3.0))
    assert set(tight) < set(wide)
    assert max(tight) == max(wide), "the last day before close must survive"


def test_the_depth_gate_is_waived_because_candles_carry_no_book():
    """walkforward.py:96 does this; if the two disagree the selection scores a strategy
    that is blocked on liquidity the backtest never had to prove."""
    assert ps.wf_gates()["min_leg_depth_usd"] == 0.0


def test_db_state_gates_ARE_consulted_and_come_from_pit_gates(conn):
    """The inverse of what this file used to assert (§25.2b).

    They are not GATES keys — they are per-day state — so what is pinned is that
    `event_pnl` reaches `pit_gates.GateHistory` at all, and that switching it off changes
    the answer. A silently-inert wiring would satisfy the first half and not the second,
    so both are required.
    """
    seen = {"n": 0}
    real = ps.gate_history

    def spy(c, series):
        seen["n"] += 1
        return real(c, series)

    ev = next((e for e in ps.quotable_events(conn, "KXJOBLESSCLAIMS")
               if e["close_ts"] >= WINDOW_START), None)
    if ev is None:
        pytest.skip("no quotable claims event in this db")
    key = kalshi_period_to_key(ev["tok"])
    import prediction_market_macro.research.pnl_score as m
    orig, m.gate_history = m.gate_history, spy
    try:
        gated = m.event_pnl(conn, "KXJOBLESSCLAIMS", ev["tok"], key, ev["close_ts"])
    finally:
        m.gate_history = orig
    assert seen["n"] >= 1, "the db-state gates were never consulted"
    # KXJOBLESSCLAIMS is the series the skill gate actually bites on (54 blocks in the
    # 75d window), so the ungated replay must be able to differ. If this ever stops
    # differing, the wiring has gone inert and the assertion above is not enough.
    ungated = ps.event_pnl(conn, "KXJOBLESSCLAIMS", ev["tok"], key, ev["close_ts"],
                           db_gates=False)
    assert gated is not None and ungated is not None
    assert (gated["edge"], gated["argmax"]) != (ungated["edge"], ungated["argmax"]) \
        or gated["traded"] != ungated["traded"], \
        "gated and ungated replays are identical — the gates are not being applied"


# ── #142's exit rules, on this side of the fence too (#144) ─────────────────────

# The six events in the pinned window where `ops/exits.py`'s rules fire before settlement,
# with the exit day and the realised dollars the harness books. Named rather than
# discovered at runtime so a regression that silently stops exiting cannot pass by finding
# an empty set. (series, period, exit_day, realised_with_exits, realised_if_held)
EXITED = [
    ("KXAAAGASW", "2026-06-01", "2026-05-27", 0.70, 1.00),
    ("KXAAAGASW", "2026-06-08", "2026-06-03", 0.63, 1.08),
    ("KXAAAGASW", "2026-06-29", "2026-06-25", 0.11, -0.60),
    ("KXNATGASW", "2026-06-05", "2026-06-02", 0.23, 0.40),
    ("KXNATGASW", "2026-06-26", "2026-06-21", -0.08, 0.39),
    ("KXPCECORE", "2026-04", "2026-05-27", 0.05, -0.79),
]


def _event(conn, series, period):
    evs = [e for e in ps.quotable_events(conn, series)
           if kalshi_period_to_key(e["tok"]) == period]
    return evs[0] if evs else None


@pytest.mark.parametrize(("series", "period", "exit_day", "with_exit", "if_held"), EXITED)
def test_a_held_position_is_closed_by_the_live_rules_not_carried_to_settlement(
        conn, series, period, exit_day, with_exit, if_held):
    """#144. #142 put the exit rules inside `walkforward` and left this module holding
    everything to settlement, so the daily DSR selector was ranking candidates on a
    strategy neither the harness nor the live book runs — #125/#133's disease through a
    new door. Half of all live positions are closed by rule 1, so on those trades the
    settlement outcome is simply not the PnL.

    Both arms are asserted. `with_exit` alone would still pass if the rule fired for the
    wrong reason on the wrong day; pinning `if_held` as well is what makes the sign of the
    correction visible — note it goes BOTH ways (three of these six exits give up a
    winner, three cut a loser), which is why this could not have been eyeballed.
    """
    ev = _event(conn, series, period)
    if ev is None:
        pytest.skip(f"{series} {period} not quotable in this db")
    got = ps.event_pnl(conn, series, ev["tok"], period, ev["close_ts"])
    held = ps.event_pnl(conn, series, ev["tok"], period, ev["close_ts"], model_exits=False)
    assert got is not None and held is not None
    tr = got["trade"] or {}
    assert tr.get("exit_rule") == "edge_reversal", \
        f"{series} {period}: exited by {tr.get('exit_rule')!r}, expected edge_reversal"
    assert tr.get("exit_day") == exit_day
    assert got["edge"] == pytest.approx(with_exit, abs=1e-6)
    assert held["edge"] == pytest.approx(if_held, abs=1e-6), \
        "the hold-to-settlement arm moved — model_exits=False is no longer the old path"


def test_the_exit_rules_are_walkforwards_own_and_not_a_third_copy(conn):
    """`event_pnl` must call INTO `walkforward`, not re-implement `hold_edge` beside it.

    This is the expensive lesson of #141: `ops/exits.py` and the harness each had their
    own aggregation of leg edges, they disagreed (min vs sum), and 36 bucket round trips
    were liquidated while holding positive edge before anyone noticed. There are now three
    sites that need the metric and exactly one implementation of it; neutering
    `walkforward._first_exit` must therefore change this module's answer.
    """
    import prediction_market_macro.research.walkforward as wf
    series, period, _, with_exit, if_held = EXITED[0]
    ev = _event(conn, series, period)
    if ev is None:
        pytest.skip(f"{series} {period} not quotable in this db")
    real = wf._first_exit
    wf._first_exit = lambda *_a, **_k: None
    try:
        neutered = ps.event_pnl(conn, series, ev["tok"], period, ev["close_ts"])
    finally:
        wf._first_exit = real
    assert neutered is not None
    assert neutered["edge"] == pytest.approx(if_held, abs=1e-6), \
        "neutering walkforward's exit rule did not change pnl_score — it has its own copy"
    assert ps.event_pnl(conn, series, ev["tok"], period,
                        ev["close_ts"])["edge"] == pytest.approx(with_exit, abs=1e-6)


def test_the_hold_path_reprices_the_candidate_set_not_the_incumbent(conn):
    """The exit metric on a held day must come from the SAME parameters being scored.

    `walkforward._pmf_for` re-asks the PIT daily selector at each held day because it is
    simulating that selector. This module is scoring one fixed candidate, so re-asking
    would price the hold under a different set than the entry — and the paired comparison
    between candidates, which is the whole basis of `dsr`'s deflation, would break.
    """
    import importlib

    seen, days = [], []
    series, period, exit_day, _, _ = EXITED[0]
    ev = _event(conn, series, period)
    if ev is None:
        pytest.skip(f"{series} {period} not quotable in this db")
    disp = ps.SERIES_DISPATCH[series]
    mod = importlib.import_module(disp[0])
    real = getattr(mod, disp[1])
    # an empty dict, not a probe key: it is a distinct object (so identity is testable)
    # that every model reads as "defaults", so the replay still produces a real trade.
    cand: dict = {}
    # Warm the gate history OUTSIDE the spy. `gate_history` re-predicts under the
    # INCUMBENT parameters on purpose (see its docstring: the gate state is a property of
    # what the live system accumulated, and a candidate that was never deployed did not
    # build a different calibration map), so its calls are not the ones under test here —
    # and it is memoised, so warming it is not a behaviour change. This line only works
    # because of #145; before that fix the memoisation was silently dead and every
    # `event_pnl` rebuilt the history, which is how #145 was found.
    ps.gate_history(conn, series)

    def spy(conn_, day, key_, **kw):
        seen.append(kw.get("params"))
        days.append(day.date().isoformat())
        return real(conn_, day, key_, **kw)
    setattr(mod, disp[1], spy)
    try:
        r = ps.event_pnl(conn, series, ev["tok"], period, ev["close_ts"], params=cand)
    finally:
        setattr(mod, disp[1], real)
    assert seen, "the model was never called — test is vacuous"
    assert (r["trade"] or {}).get("exit_day") == exit_day, \
        "no exit fired, so no held day was ever repriced — test is vacuous"
    assert exit_day in days, "the exit day itself was never re-predicted"
    assert all(p is cand for p in seen), \
        "a held-day re-prediction used parameters other than the candidate set"


def test_the_gate_history_is_actually_memoised(conn):
    """#145. This used to be `conn._pnl_gate_hist = cache` inside a try/except, and
    `sqlite3.Connection` refuses arbitrary attributes — so the except branch fired on
    every call and the cache never held anything.

    Nothing failed, because it is only a performance bug: `GateHistory` is deterministic,
    so rebuilding it gives the same answer. What it cost was a full `replay_series` plus
    `decision_replay` per (event, param set) instead of one per series — 803 of them on a
    73-set grid over 11 events, in the daily job. Identity is asserted rather than timing
    because a wall-clock assertion would be flaky on a warm page cache.
    """
    a = ps.gate_history(conn, "KXJOBLESSCLAIMS")
    b = ps.gate_history(conn, "KXJOBLESSCLAIMS")
    assert a is b, "gate_history rebuilt — the memoisation is dead again"
    assert ps.gate_history(conn, "KXNATGASW") is not a, "the cache is not keyed by series"


# ── scoring semantics ───────────────────────────────────────────────────────────

def test_an_event_with_no_quotable_leg_is_unscoreable_not_a_zero(conn, monkeypatch):
    """No price on any day of the window means the strategy was never offered the bet.
    Scoring that as a flat 0.0 would pad every candidate with identical rows and dilute a
    real edge toward the incumbent; it has to drop out of the sample entirely."""
    import prediction_market_macro.research.pnl_score as m
    monkeypatch.setattr(m, "_legs_at", lambda *_a, **_k: ([], {}))
    r = m.event_pnl(conn, "KXCPI", "26MAY", "2026-05",
                    datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc))
    assert r is None


def test_declining_to_bet_a_quotable_event_scores_zero_and_stays_in_the_sample(conn,
                                                                               monkeypatch):
    """The opposite case: prices were there and the gates said no. That is an outcome the
    strategy can have — it is what the incumbent does on most events — so it must score
    0.0 and remain. Dropping it would judge each candidate only on the events it happened
    to be brave on, and the bravest set would then win by construction."""
    import prediction_market_macro.research.pnl_score as m
    from prediction_market_macro.strategy.decision import Decision

    real = m._legs_at
    seen = {"legs": False}

    def spy(conn_, series, tok, day):
        meta, res = real(conn_, series, tok, day)
        seen["legs"] = seen["legs"] or bool(meta)
        return meta, res
    monkeypatch.setattr(m, "_legs_at", spy)
    # gates say no, and no structure survives the favourite's price window either
    monkeypatch.setattr(m, "decide",
                        lambda *_a, **_k: Decision("pass", None, 0.0, 0, ("test",), {}))
    monkeypatch.setattr(m, "enumerate_structs", lambda *_a, **_k: [])
    monkeypatch.setattr(m, "_structs_categorical", lambda *_a, **_k: [])

    ev = next((e for e in ps.quotable_events(conn, "KXJOBLESSCLAIMS")
               if e["close_ts"] >= WINDOW_START), None)
    if ev is None:
        pytest.skip("no quotable claims event in this db")
    r = m.event_pnl(conn, "KXJOBLESSCLAIMS", ev["tok"],
                    kalshi_period_to_key(ev["tok"]), ev["close_ts"])
    assert seen["legs"], "the fixture never reached a day with a price — test is vacuous"
    assert r is not None and r["traded"] is False
    assert r["hybrid"] == 0.0 and r["edge"] == 0.0 and r["argmax"] == 0.0
    assert r["stream"] == "none"


def test_score_matrix_drops_an_event_no_set_can_score(monkeypatch):
    """Mirrors param_wf.score_matrix: a partial row would pair candidates against
    different samples, which is the bias the paired DSR test exists to remove."""
    import prediction_market_macro.research.pnl_score as m
    calls = {"n": 0}

    def fake(conn, series, tok, key, close, params=None, **_k):
        calls["n"] += 1
        if key == "bad" and params:
            return None
        return {"hybrid": 0.5, "edge": 0.5, "argmax": 0.0}
    monkeypatch.setattr(m, "event_pnl", fake)
    uni = [{"tok": "a", "key": "ok", "close_ts": None},
           {"tok": "b", "key": "bad", "close_ts": None}]
    kept, mat, det = m.score_matrix(None, "KXCPI", [{}, {"a": 1}], uni)
    assert [e["key"] for e in kept] == ["ok"]
    assert len(mat) == 1 and len(mat[0]) == 2 and len(det) == 1
