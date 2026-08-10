"""§25.5 — the held-position path: "was it green for a while and we didn't get out?"

Before this, `walkforward` jumped from entry straight to the 0/1 settlement, so the
question was unanswerable rather than answered. The path added by `_mtm_path` has to be
priced the way `ops/exits.py` actually exits — into the bid, minus slippage, minus both
taker fees — or it answers a question about a price nobody would have paid.

The tests below pin exactly that, plus the two ways the aggregate could quietly lie:
counting un-observed trades as "never green", and reporting the hindsight oracle as if it
were an achievable rule.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops.exits import SLIP
from prediction_market_macro.research import walkforward as wf
from prediction_market_macro.strategy.edge import Leg, Struct, taker_fee

DEPTH = 1e9
ENTRY = datetime(2026, 6, 1, 16, tzinfo=timezone.utc)
CLOSE = datetime(2026, 6, 6, 16, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _candle(conn, ticker, day, bid, ask):
    conn.execute(
        "INSERT INTO candles(ticker, end_ts, yes_bid_close, yes_ask_close)"
        " VALUES(?,?,?,?)", (ticker, int(day.timestamp()), bid, ask))
    conn.commit()


def _single(px=0.40, fair=0.55):
    return Struct("single", (Leg("T-1", "yes", px, DEPTH),), fair=fair, cost=px,
                  max_loss=px, desc="YES T-1")


# ── pricing ──────────────────────────────────────────────────────────────────────────

def test_path_is_priced_into_the_bid_minus_slippage_and_both_fees(conn):
    st, count = _single(0.40), 2
    d = ENTRY + timedelta(days=1)
    _candle(conn, "T-1", d, 0.70, 0.90)

    path = wf._mtm_path(conn, st, count, ENTRY, CLOSE)
    exit_px = 0.70 - SLIP
    want = ((exit_px - 0.40) * count - taker_fee(0.40, count)
            - taker_fee(exit_px, count))
    assert path[0]["mtm"] == round(want, 4)


def test_the_mid_is_reported_separately_and_is_the_optimistic_one(conn):
    """The bid/mid gap IS the answer to "could we have taken it" on a wide book."""
    st = _single(0.40)
    _candle(conn, "T-1", ENTRY + timedelta(days=1), 0.18, 0.98)
    p = wf._mtm_path(conn, st, 1, ENTRY, CLOSE)[0]
    assert p["mtm_mid"] > p["mtm"], "mid must not be reported as the tradeable mark"
    assert p["mtm"] < 0 < p["mtm_mid"], (
        "the 0.18/0.98 book that made exits.py refuse mids must show the split")


def test_a_no_leg_closes_against_the_ask(conn):
    st = Struct("single", (Leg("T-1", "no", 0.30, DEPTH),), fair=0.8, cost=0.30,
                max_loss=0.30, desc="NO T-1")
    _candle(conn, "T-1", ENTRY + timedelta(days=1), 0.10, 0.20)
    # a NO leg is worth 1 - ask on exit, i.e. 0.80 here, not 1 - bid = 0.90
    exit_px = (1 - 0.20) - SLIP
    want = (exit_px - 0.30) * 1 - taker_fee(0.30, 1) - taker_fee(exit_px, 1)
    assert wf._mtm_path(conn, st, 1, ENTRY, CLOSE)[0]["mtm"] == round(want, 4)


# ── the window ───────────────────────────────────────────────────────────────────────

def test_path_starts_after_entry_and_stops_before_close(conn):
    st = _single()
    for i in range(0, 7):
        _candle(conn, "T-1", ENTRY + timedelta(days=i), 0.5, 0.6)
    days = [p["day"] for p in wf._mtm_path(conn, st, 1, ENTRY, CLOSE)]
    assert days[0] == (ENTRY + timedelta(days=1)).date().isoformat()
    assert all(d < CLOSE.date().isoformat() for d in days)


def test_a_leg_without_a_candle_drops_the_whole_day(conn):
    """A two-leg structure half-quoted is not a mark; it must not be half-counted."""
    st = Struct("bucket", (Leg("T-LO", "yes", 0.55, DEPTH),
                           Leg("T-HI", "no", 0.54, DEPTH)),
                fair=0.3, cost=0.09, max_loss=0.09, desc="BUCKET")
    _candle(conn, "T-LO", ENTRY + timedelta(days=1), 0.6, 0.7)   # T-HI missing
    assert wf._mtm_path(conn, st, 1, ENTRY, CLOSE) == []


# ── the aggregate ────────────────────────────────────────────────────────────────────

def _row(realized, peak, days=3, staked=1.0, peak_mid=None):
    return {"realized": realized, "mtm_peak": peak, "mtm_days": days,
            "staked": staked, "mtm_peak_mid": peak if peak_mid is None else peak_mid}


def test_unobserved_trades_are_excluded_not_counted_as_never_green():
    """A trade opened the day before close has no path. That is missing data.

    Counting it as "never green" would answer the user's question with an artefact of
    the entry timing rather than with the market.
    """
    out = _held([_row(-1.0, None, days=0), _row(-1.0, +0.5)])
    assert out["n_trades"] == 2 and out["n_observed"] == 1
    assert out["n_gave_back"] == 1


def test_oracle_never_reports_a_loss_versus_settling():
    """max(peak, realized) — a trade that only ever got worse contributes zero, not a
    negative "gain". Otherwise a big loser that never rallied would offset a real
    give-back and the bound would stop being a bound."""
    out = _held([_row(+0.5, -0.9), _row(-1.0, +0.4)])
    assert out["oracle_gain"] == pytest.approx(1.4)


def test_oracle_roi_is_labelled_as_a_bound():
    out = _held([_row(-1.0, +0.4)])
    assert "UPPER BOUND" in out["note"] and out["oracle_roi"] > out["roi"]


def test_no_observed_trades_says_so_instead_of_dividing():
    assert _held([_row(-1.0, None, days=0)])["n_observed"] == 0


def _held(ts):
    """`_held_analysis` is a closure inside `run`; rebuild it here from source.

    Extracting it to module scope would be cleaner, but it lives next to the stream
    summaries it mirrors and moving it for testability alone would put the definition
    far from the two call sites. This keeps the test honest about which code it runs.
    """
    import inspect
    import textwrap
    src = inspect.getsource(wf.run)
    body = src[src.index("    def _held_analysis"):]
    body = body[:body.index("\n    argmax_trades")]
    ns: dict = {}
    exec(textwrap.dedent(body), ns)                                  # noqa: S102
    return ns["_held_analysis"](ts)
