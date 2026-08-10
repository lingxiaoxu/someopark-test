"""PR-2 (#126) — the argmax defer-to-market filter, judged forward.

Two halves. The RECORDER (`decide_all`) must observe both arms without changing what the
live path does; the SCORER (`shadow_pr2`) must not grade before the registered n, must not
let the arms drift apart, and must settle a trade the same way the harness does.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import decide_all as da
from prediction_market_macro.research import shadow_pr2 as pr2

REG = datetime.fromisoformat(pr2.REGISTERED)


# ── fakes ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Leg:
    ticker: str
    side: str
    price: float


class _Struct:
    """The subset of `edge.Struct` the argmax path touches."""

    def __init__(self, fair, cost, legs, desc="S"):
        self.fair, self.cost, self.legs, self.desc = fair, cost, legs, desc

    def fill_cost(self, _count):
        return self.cost

    def fill_prices(self, _count):
        return [l.price for l in self.legs]

    def net_edge(self, _count=1):
        return self.fair - self.cost


class _Spec:
    ticker = "KXTEST"


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "t.db"))
    yield c
    c.close()


def _struct(fair, cost, tickers=("A",), sides=("yes",)):
    return _Struct(fair, cost, [_Leg(t, s, cost) for t, s in zip(tickers, sides)])


def _settle(conn, series, period, ticker, result):
    # `first_seen_ts` is NOT NULL in the real schema; a helper that omitted it was writing
    # against a table shape that does not exist, which is the failure mode a fake schema in
    # the test file would have hidden. `init_db` gives the tests the production DDL.
    ts = (REG + timedelta(days=30)).isoformat()
    conn.execute("INSERT OR REPLACE INTO settlements"
                 "(ticker, series, period, result, settled_ts, first_seen_ts)"
                 " VALUES(?,?,?,?,?,?)", (ticker, series, period, result, ts, ts))
    conn.commit()


def _record(conn, period, fair, cost, *, ts=None, result="yes", settle=True):
    """Drive the REAL `_place_argmax`, then settle the event."""
    tk = f"T{period}"
    st = _struct(fair, cost, (tk,), ("yes",))
    da._place_argmax(conn, _Spec(), period, [st], ts or (REG + timedelta(days=1)))
    if settle:
        _settle(conn, _Spec.ticker, period, tk, result)
    return st


# ── the recorder: both arms, and no change to what the live path does ────────────────

def test_the_filter_still_blocks_the_trade_it_always_blocked(conn):
    """The recorder must be a pure observer. `fair > cost` still means no `decisions` row
    and no `fills` row — if shadow logging ever started placing trades it would be writing
    real paper PnL against an unvalidated rule."""
    placed = da._place_argmax(conn, _Spec(), "p1", [_struct(0.90, 0.60)], REG)
    assert placed is False
    assert conn.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM fills").fetchone()["c"] == 0
    r = conn.execute("SELECT * FROM shadow_argmax").fetchone()
    assert r["arm"] == "deferred" and r["fair"] > r["cost"]


def test_an_allowed_leg_is_both_traded_and_recorded(conn):
    """The ON arm is not reconstructed from `decisions` later — it is written down at the
    same moment, by the same call, from the same candidate."""
    assert da._place_argmax(conn, _Spec(), "p2", [_struct(0.70, 0.80)], REG) is True
    assert conn.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"] == 1
    r = conn.execute("SELECT * FROM shadow_argmax").fetchone()
    assert r["arm"] == "placed" and r["fair"] <= r["cost"]


def test_turning_the_filter_off_does_not_change_which_structure_is_bought(conn):
    """`argmax_candidate` selects, and only then is `defers_to_market` consulted. If that
    order ever inverted, the OFF arm would be buying a different structure and the paired
    comparison would be between two different strategies."""
    # every cost is inside `argmax_candidate`'s 0.10-0.90 tradeable band, so the only thing
    # separating these three is `fair`. (A first draft used cost=0.99 and the top-fair
    # structure was silently not a candidate — the band, not the filter, was doing the
    # work, and the test would have "passed" the ordering claim without testing it.)
    structs = [_struct(0.95, 0.90), _struct(0.80, 0.20), _struct(0.60, 0.70)]
    st = da.argmax_candidate(structs)
    assert st.fair == 0.95                       # highest fair, regardless of the filter
    assert da.defers_to_market(st) is True       # ...and it is the one the filter kills
    assert da.defers_to_market(structs[2]) is False       # 0.60 <= 0.70
    # And this is why the order matters rather than being a stylistic choice: filtering
    # FIRST and selecting second would have bought structs[2] instead. Same three inputs,
    # different trade — the two arms would not be a paired comparison at all.
    filter_then_select = da.argmax_candidate(
        [s for s in structs if not da.defers_to_market(s)])
    assert filter_then_select is structs[2] and filter_then_select is not st


def test_the_recorded_leg_prices_are_the_fill_not_the_quote(conn):
    """`legs_json` must hold what the trade would have PAID. Storing the quote would make
    every shadow trade look a fill's worth cheaper than it was."""
    st = _Struct(0.9, 0.5, [_Leg("A", "yes", 0.55)])
    st.fill_prices = lambda _c: [0.57]
    da._place_argmax(conn, _Spec(), "p3", [st], REG)
    legs = json.loads(conn.execute("SELECT legs_json j FROM shadow_argmax").fetchone()["j"])
    assert legs[0]["price"] == 0.57


def test_one_row_per_event_even_if_the_cycle_runs_twice(conn):
    """`refresh` can run more than once in a day. A second look at the same event is the
    same opportunity, not a second one — double-counting it would let the sample size be
    reached by re-running rather than by trading."""
    for _ in range(3):
        da._place_argmax(conn, _Spec(), "p4", [_struct(0.90, 0.60)], REG)
    assert conn.execute("SELECT COUNT(*) c FROM shadow_argmax").fetchone()["c"] == 1


def test_a_leg_risk_would_have_rejected_is_in_neither_arm(conn, monkeypatch):
    """The asymmetry that would quietly inflate the OFF arm: crediting it with trades that
    could never have been sent. Both arms are held to `risk.check`."""
    from prediction_market_macro.ops import risk
    monkeypatch.setattr(risk, "check", lambda *_a, **_k: risk.Veto("risk_gross"))
    assert da._place_argmax(conn, _Spec(), "p5", [_struct(0.90, 0.60)], REG) is False
    assert conn.execute("SELECT COUNT(*) c FROM shadow_argmax").fetchone()["c"] == 0


def test_risk_check_is_only_ever_read_from(conn):
    """`_place_argmax` now calls `risk.check` before the filter rather than after. That is
    only safe because the call is a pure read — this pins it, so a future edit that made
    `check` record something would fail here instead of silently mutating state on trades
    that are never placed."""
    from prediction_market_macro.ops import risk

    def snapshot():
        return {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
                for t in ("decisions", "fills", "shadow_argmax", "alerts")}

    before = snapshot()
    risk.check(conn, "KXTEST", "p6", 1.0)
    assert snapshot() == before


# ── the scorer ───────────────────────────────────────────────────────────────────────

def test_no_verdict_before_the_registered_sample_size(conn):
    for i in range(pr2.N_FORWARD - 1):
        _record(conn, f"q{i}", 0.9, 0.6, result="yes")
    out = pr2.run(conn, asof=REG + timedelta(days=90))
    assert out["n_forward"] == pr2.N_FORWARD - 1
    assert out["verdict"].startswith("PENDING")
    assert "progress readout" in out["verdict"]


def test_an_unsettled_leg_is_not_a_pending_zero(conn):
    """Counting open trades toward `n_forward` would let the sample size be reached by
    opening positions instead of by resolving them."""
    _record(conn, "u1", 0.9, 0.6, settle=False)
    out = pr2.run(conn, asof=REG + timedelta(days=90))
    assert out["n_forward"] == 0


def test_a_leg_recorded_before_the_registration_is_not_counted(conn):
    _record(conn, "old", 0.9, 0.6, ts=REG - timedelta(days=2))
    _record(conn, "new", 0.9, 0.6, ts=REG + timedelta(days=2))
    out = pr2.run(conn, asof=REG + timedelta(days=90))
    assert out["n_forward"] == 1


def test_the_off_arm_is_the_superset_and_the_on_arm_is_the_placed_subset(conn):
    _record(conn, "a", 0.70, 0.80)                 # placed
    _record(conn, "b", 0.90, 0.60)                 # deferred
    out = pr2.run(conn, asof=REG + timedelta(days=90))
    assert out["n_placed"] == 1 and out["n_deferred"] == 1
    assert out["filter_on"]["n"] == 1
    assert out["filter_off"]["n"] == 2             # placed + deferred, nested not disjoint


def test_a_filter_that_skips_only_losers_is_kept(conn):
    """The direction that keeps the rule: deferred legs lose, placed legs win, so removing
    the filter drags ROI down and the gap clears the bar."""
    for i in range(pr2.N_FORWARD // 2):
        _record(conn, f"w{i}", 0.70, 0.80, result="yes")     # placed, wins
        _record(conn, f"l{i}", 0.90, 0.60, result="no")      # deferred, loses
    out = pr2.run(conn, asof=REG + timedelta(days=90))
    assert out["n_forward"] >= pr2.N_FORWARD
    assert out["roi_gap_pp"] >= pr2.MIN_GAP_PP
    assert out["verdict"].startswith("KEEP")


def test_a_filter_that_skips_winners_is_removed(conn):
    """The outcome the registration explicitly wants to be actionable: the rule drops
    trades and buys nothing. "No effect" must produce REMOVE, not a shrug."""
    for i in range(pr2.N_FORWARD // 2):
        _record(conn, f"w{i}", 0.70, 0.80, result="yes")     # placed, wins
        _record(conn, f"g{i}", 0.90, 0.60, result="yes")     # deferred, ALSO wins
    out = pr2.run(conn, asof=REG + timedelta(days=90))
    assert out["n_forward"] >= pr2.N_FORWARD
    assert out["roi_gap_pp"] < pr2.MIN_GAP_PP
    assert out["verdict"].startswith("REMOVE")
    assert "does not apply it" in out["verdict"], "the scorer must not edit the strategy"


def test_settlement_uses_the_harness_formula_and_not_a_lookalike(conn):
    """#141: a shared quantity with two implementations drifts. `shadow_pr2` settles
    through `edge.settle_struct`, which is the body `walkforward._settle_struct` now
    delegates to — so this asserts agreement with the harness, not with a copy of it."""
    from prediction_market_macro.strategy.edge import settle_struct, taker_fee
    _record(conn, "s1", 0.70, 0.80, result="yes")
    out = pr2.run(conn, asof=REG + timedelta(days=90))
    row = out["filter_on"]
    r = conn.execute("SELECT * FROM shadow_argmax WHERE period='s1'").fetchone()
    legs = [pr2._Leg(l["ticker"], l["side"], l["price"])
            for l in json.loads(r["legs_json"])]
    expect = settle_struct(legs, int(r["count"]), {"Ts1": "yes"})
    assert row["realized"] == pytest.approx(round(expect, 4))
    # and the fee really is in there, so this is not passing on a formula that dropped it
    assert taker_fee(0.80, int(r["count"])) > 0


def test_a_structure_with_one_unsettled_leg_is_dropped_whole(conn):
    """A bucket pays only if BOTH legs win. Scoring the settled half of a half-settled
    structure would invent a trade nobody could have held."""
    st = _Struct(0.9, 0.6, [_Leg("L1", "yes", 0.3), _Leg("L2", "no", 0.3)])
    da._place_argmax(conn, _Spec(), "half", [st], REG + timedelta(days=1))
    _settle(conn, _Spec.ticker, "half", "L1", "yes")          # L2 never settles
    assert pr2.run(conn, asof=REG + timedelta(days=90))["n_forward"] == 0


def test_the_entry_and_exit_rules_still_contradict_each_other(conn):
    """#148, pinned here because PR-2's reading DEPENDS on it.

    `_place_argmax` enters only when `fair <= cost` (net_edge <= 0); `ops/exits` liquidates
    below `EXIT_EDGE`. So a real argmax position survives only inside the strip
    [EXIT_EDGE, 0] — 3 of the first 4 legs fell below it and round-tripped in the same
    cycle. That is why `shadow_pr2` scores BOTH arms held to settlement and says so, rather
    than claiming the ON arm is the book's realised PnL.

    If either constant moves, the band moves, and PR-2's `exit_policy_note` becomes a stale
    description of a system that changed. This test is what makes that noisy instead of
    silent — it asserts the RELATIONSHIP, so it does not need editing when -0.06 is retuned,
    only when the contradiction is actually resolved (which is #148's job, not a retune's).
    """
    from prediction_market_macro.ops.exits import EXIT_EDGE

    entered = [_struct(f, c) for f, c in [(0.60, 0.70), (0.80, 0.80), (0.95, 0.90)]]
    for st in entered:
        if not da.defers_to_market(st):
            assert st.fair - st.cost <= 0, "argmax entered at POSITIVE edge — #148 changed"
    assert EXIT_EDGE < 0, "the survivable band is empty unless EXIT_EDGE is negative"
    # and the band really is narrow rather than a comfortable margin
    assert abs(EXIT_EDGE) < 0.10
    note = pr2.run(conn, asof=REG)["exit_policy_note"]
    assert "held-to-settlement" in note and "#148" in note


def test_the_constants_still_match_what_was_registered():
    """The criterion may not drift to fit the result. `docs/PREREGISTER.md` is the record;
    these constants are what the code enforces."""
    import pathlib
    import re
    doc = (pathlib.Path(pr2.__file__).resolve().parent.parent
           / "docs" / "PREREGISTER.md").read_text()
    block = doc.split("### PR-2")[1].split("### PR-3")[0]
    assert "2026-08-05" in block and pr2.REGISTERED.startswith("2026-08-05")
    assert re.search(r"前向\s*20\s*笔", block) and pr2.N_FORWARD == 20
    assert re.search(r"ROI\s*差\s*≥\s*5pp", block) and pr2.MIN_GAP_PP == 5.0
    assert re.search(r"\|\s*K\s*\|\s*1\s*\|", block) and pr2.K == 1
