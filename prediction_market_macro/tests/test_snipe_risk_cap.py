"""#151/F8 — `snipe.run_for` opens positions, so it must clear `risk.check` like every
other opening path.

`decide_all` runs `risk.check` before the edge leg (line ~476) and before the argmax leg
(`_place_argmax`, line ~145). `strategy/snipe.run_for` wrote straight to `decisions`, so the
only thing bounding it was its own `MAX_SNIPE_USD = 2.0`.

That gap is reachable rather than theoretical. `_has_open_snipe` caps this path at $2 per
(series, period), and the edge stream can already be holding up to `per_event_usd = 5.0` on
the very same period — the sum clears the limit and nothing said no. A snipe is directional
(it buys the leg the realised print implies), so its maximum loss IS its stake and the caps
apply to it unmodified.

Deliberately NOT extended to `arb.execute`: an arb's payoff floor is >= its cost, so its max
loss is not its stake. Whether a locked-profit structure belongs under a directional loss cap
is a real question with an argument on each side, and there is no evidence either way on a
book where `arb` has never fired — so it is written down (§25.22), not decided in passing.
"""
from __future__ import annotations

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import risk
from prediction_market_macro.strategy import snipe

S, KEY, TOK = "KXCPI", "2026-08", "KXCPI-26AUG"
LEG = {"ticker": "KXCPI-26AUG-T3.0", "strike_type": "greater", "strike": 3.0,
       "cap_strike": None, "yes_ask": 0.20, "ask_depth": 500, "yes_bid": 0.18,
       "bid_depth": 500}


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = init_db(str(tmp_path / "t.db"))
    c.execute("INSERT INTO contracts(ticker, series, event_ticker, period, status,"
              " first_seen_ts) VALUES(?,?,?,?,'active','2026-08-01T00:00:00+00:00')",
              (LEG["ticker"], S, TOK, TOK))
    c.commit()
    # The print is far from the 3.0 strike, so the BOUNDARY_FRAC guard does not fire.
    monkeypatch.setattr("prediction_market_macro.ops.pnl._realized_print",
                        lambda *a, **k: 4.0)
    monkeypatch.setattr("prediction_market_macro.ops.decide_all._legs_meta",
                        lambda *a, **k: [LEG])
    monkeypatch.setattr("prediction_market_macro.research.health._leg_expected",
                        lambda *a, **k: "yes")
    monkeypatch.setattr("prediction_market_macro.util.periods.kalshi_period_to_key",
                        lambda t: KEY)
    return c


def _hold(conn, usd: float, kind: str = "open") -> None:
    conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " inputs_json, model_version, gate_snapshot, note)"
        " VALUES('2026-08-06T12:00:00+00:00',?,?,'{}',?,?,'{}','m/1.0','{}','')",
        (S, KEY, kind, usd))
    conn.commit()


def _snipes(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='snipe'").fetchone()[0]


def test_the_fixture_actually_reaches_the_open(conn):
    """The negative tests below are only meaningful if this path opens at all — without
    this, a typo in the fixture would make every other test in the file pass vacuously."""
    assert snipe.run_for(conn, S, KEY) == 1
    assert _snipes(conn) == 1


def test_a_snipe_cannot_push_a_period_past_the_per_event_cap(conn):
    """The concrete reachable breach: the edge stream is already at the $5 per-event
    limit, and the snipe used to be added on top of it."""
    _hold(conn, risk.LIMITS["per_event_usd"])
    assert snipe.run_for(conn, S, KEY) == 0
    assert _snipes(conn) == 0


def test_a_snipe_respects_the_gross_cap(conn):
    """Not just the per-event cap — `risk.check` is applied whole, so the book-wide
    limits bind too. Booked on OTHER periods so the per-event cap is not what fires."""
    for i in range(20):
        _hold(conn, 5.0, kind="open")
        conn.execute("UPDATE decisions SET period=? WHERE id=(SELECT MAX(id) FROM"
                     " decisions)", (f"2027-{i + 1:02d}",))
    conn.commit()
    assert snipe.run_for(conn, S, KEY) == 0


def test_room_under_the_cap_still_snipes(conn):
    """The fix can only subtract, so pin that it does not subtract when it shouldn't."""
    _hold(conn, 1.0)
    assert snipe.run_for(conn, S, KEY) == 1


def test_the_recorded_stake_is_the_amount_that_was_checked(conn):
    """If `size_usd` and the value passed to `risk.check` ever drift apart, the cap is
    enforced against one number and the book accrues another — which is #132's bug shape."""
    assert snipe.run_for(conn, S, KEY) == 1
    r = conn.execute("SELECT size_usd, inputs_json FROM decisions WHERE kind='snipe'"
                     ).fetchone()
    import json
    assert r["size_usd"] == round(LEG["yes_ask"] * json.loads(r["inputs_json"])["count"], 4)
