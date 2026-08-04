"""strategy/consistency — the four model-free checks, and the outage that hid them.

fed/0.2 -> 0.3 removed `_market_prior`, and `fed_mutual` kept importing it. The import
is inside the function, so the ImportError surfaced only at call time — where
ops/refresh.py's step() caught it, printed one line, and moved on. Because fed_mutual is
the FIRST entry in run()'s dict literal, its exception took the other three checks with
it: CPI MoM/YoY, ladder monotonicity and term structure were all silently dead for as
long as the stale import was.

So the first test here is not about Fed policy at all. It is that run() executes end to
end on an empty db. That alone would have caught it on the day.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.strategy import consistency

ASOF = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
PERIOD, TOK = "2026-09", "26SEP"


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


def _leg(conn, cat: str, bid: float, ask: float):
    t = f"KXFEDDECISION-{TOK}-{cat}"
    conn.execute(
        "INSERT OR REPLACE INTO contracts(ticker, series, event_ticker, period,"
        " strike_type, status, first_seen_ts) VALUES(?,?,?,?,?,?,?)",
        (t, "KXFEDDECISION", f"KXFEDDECISION-{TOK}", TOK, "categorical", "active",
         ASOF.isoformat()))
    conn.execute("INSERT OR REPLACE INTO quotes(ts, ticker, yes_bid, yes_ask,"
                 " bid_depth, ask_depth) VALUES(?,?,?,?,?,?)",
                 (ASOF.isoformat(), t, bid, ask, 100, 100))
    conn.commit()


def _book(conn, probs: dict[str, float]):
    """A decision book whose devigged categorical is `probs` (1c wide, so the mid is
    the leg probability and the renormalisation is a no-op)."""
    for cat, p in probs.items():
        _leg(conn, cat, round(p - 0.005, 4), round(p + 0.005, 4))


def _ladder_says(monkeypatch, move: float | None):
    monkeypatch.setattr(consistency, "_latest_legs", consistency._latest_legs)
    import prediction_market_macro.model.fed as fed
    monkeypatch.setattr(fed, "_market_move", lambda *a, **k: (move, "t"))


# ── the outage ──────────────────────────────────────────────────────────────
def test_run_completes_on_an_empty_db(conn):
    """The regression that matters: every check reachable, nothing raising."""
    out = consistency.run(conn)
    assert out == {"fed_mutual": 0, "cpi_mom_yoy": 0, "combo": 0, "monotone": 0,
                   "term_structure": 0}


def test_fed_mutual_imports_resolve(conn):
    """Named separately because an ImportError here used to take the other three
    checks down with it — run()'s dict literal evaluates fed_mutual first."""
    assert consistency.fed_mutual(conn, ASOF) == []


# ── the comparison ──────────────────────────────────────────────────────────
def test_books_that_agree_raise_nothing(conn, monkeypatch):
    # book mean = .1*-.25 + .8*0 + .1*.25 = 0.0
    _book(conn, {"C25": 0.10, "H0": 0.80, "H25": 0.10})
    _ladder_says(monkeypatch, 0.0)
    assert consistency.fed_mutual(conn, ASOF) == []


def test_a_gap_below_half_a_step_is_devig_noise(conn, monkeypatch):
    _book(conn, {"C25": 0.10, "H0": 0.80, "H25": 0.10})
    _ladder_says(monkeypatch, 0.12)              # < FED_MOVE_GAP_ALERT
    assert consistency.fed_mutual(conn, ASOF) == []


def test_a_gap_over_half_a_step_is_flagged_and_alerted(conn, monkeypatch):
    _book(conn, {"C25": 0.10, "H0": 0.80, "H25": 0.10})
    _ladder_says(monkeypatch, 0.30)              # ladder prices a hike, book prices none
    got = consistency.fed_mutual(conn, ASOF)
    assert len(got) == 1
    assert got[0]["move_ladder"] == 0.3 and got[0]["move_direct"] == 0.0
    assert got[0]["gap"] == 0.3
    msg = conn.execute("SELECT message FROM alerts WHERE source='consistency'"
                       ).fetchone()["message"]
    assert msg.startswith(f"FED-MUTUAL {TOK}:")


def test_the_direct_mean_uses_the_category_quanta(conn, monkeypatch):
    """A book leaning to a 50bp cut must read as -0.50, not as one generic 'cut'.
    Collapsing C26 onto the 25bp quantum would give -0.2375 here instead."""
    _book(conn, {"C26": 0.90, "C25": 0.05, "H0": 0.05})
    _ladder_says(monkeypatch, 0.0)
    got = consistency.fed_mutual(conn, ASOF)
    assert got[0]["move_direct"] == -0.4625


# ── the refusals ────────────────────────────────────────────────────────────
def test_an_unpriceable_ladder_is_skipped_not_treated_as_zero(conn, monkeypatch):
    """_market_move returns None for an unpriced predecessor or too thin an overlap.
    Reading that as a 0.00pp move would manufacture a gap against any leaning book —
    the 2027-03 failure mode, one layer up."""
    _book(conn, {"C26": 0.90, "C25": 0.05, "H0": 0.05})
    _ladder_says(monkeypatch, None)
    assert consistency.fed_mutual(conn, ASOF) == []


def test_a_book_too_thin_to_devig_is_skipped(conn, monkeypatch):
    _book(conn, {"H0": 0.90, "H25": 0.10})       # 2 legs < the 3 required
    _ladder_says(monkeypatch, 0.30)
    assert consistency.fed_mutual(conn, ASOF) == []


def test_a_period_with_no_fomc_meeting_is_skipped(conn, monkeypatch):
    for cat, p in (("C25", 0.1), ("H0", 0.8), ("H25", 0.1)):
        t = f"KXFEDDECISION-26AUG-{cat}"
        conn.execute(
            "INSERT OR REPLACE INTO contracts(ticker, series, event_ticker, period,"
            " strike_type, status, first_seen_ts) VALUES(?,?,?,?,?,?,?)",
            (t, "KXFEDDECISION", "KXFEDDECISION-26AUG", "26AUG", "categorical",
             "active", ASOF.isoformat()))
        conn.execute("INSERT OR REPLACE INTO quotes VALUES(?,?,?,?,?,?)",
                     (ASOF.isoformat(), t, p - 0.005, p + 0.005, 100, 100))
    conn.commit()
    _ladder_says(monkeypatch, 0.30)
    assert consistency.fed_mutual(conn, ASOF) == []      # no August FOMC meeting


def test_alerts_dedup_within_the_day(conn, monkeypatch):
    _book(conn, {"C25": 0.10, "H0": 0.80, "H25": 0.10})
    _ladder_says(monkeypatch, 0.30)
    consistency.fed_mutual(conn, ASOF)
    consistency.fed_mutual(conn, ASOF)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE source='consistency'"
                        ).fetchone()[0] == 1
