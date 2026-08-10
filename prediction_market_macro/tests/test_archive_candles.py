"""#120 — the archival step's job is to beat a deadline it does not control.

Most of what can go wrong here is invisible in the happy path: a queue ordered the wrong
way, a cap that binds on the wrong end, an alert that fires the day the data is already
gone. Those are the properties pinned below. The fetch itself is `md.candles`, which is
`research.backtest.backfill_candles`'s call and already covered — this module is the
scheduling and the triage around it, so that is what these tests are about.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import archive_candles as ac

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _market(conn, ticker, age_days, series="KXWTIW", result="yes", period="p1"):
    close = (NOW - timedelta(days=age_days)).isoformat()
    conn.execute("INSERT INTO settlements(ticker, series, period, result, settled_ts,"
                 " first_seen_ts) VALUES(?,?,?,?,?,?)",
                 (ticker, series, period, result, close, close))
    conn.execute("INSERT INTO contracts(ticker, series, event_ticker, period, close_time,"
                 " status, first_seen_ts) VALUES(?,?,?,?,?,?,?)",
                 (ticker, series, f"{series}-{period}", period, close, "settled", close))
    conn.commit()


class FakeMD:
    """Records calls; `fail_on` raises the way a 429 storm would, `empty` answers 200 with
    an empty candlestick list the way an untraded strike does."""

    def __init__(self, fail_on=(), empty=()):
        self.calls, self.fail_on, self.empty = [], set(fail_on), set(empty)

    def candles(self, series, ticker, start_ts, end_ts, interval_min=1440):
        self.calls.append(ticker)
        if ticker in self.fail_on:
            raise RuntimeError("429")
        return 0 if ticker in self.empty else 7


# ── the queue ────────────────────────────────────────────────────────────────────────

def test_a_market_with_candles_is_not_refetched(conn):
    _market(conn, "T-HAVE", 10)
    conn.execute("INSERT INTO candles(ticker, end_ts, price_close) VALUES('T-HAVE',1,0.5)")
    conn.commit()
    assert ac.pending(conn) == []


def test_the_404_sentinel_counts_as_having_been_answered(conn):
    """`backfill`'s sentinel is `end_ts = 0` with NULL prices. Re-probing those would be
    the bulk of the daily work for a guaranteed zero yield — 6.7k of them in production."""
    _market(conn, "T-DEAD", 10)
    conn.execute("INSERT INTO candles(ticker, end_ts, price_close) VALUES('T-DEAD',0,NULL)")
    conn.commit()
    assert ac.pending(conn) == []


def test_expired_markets_are_not_queued(conn):
    """Past RETENTION_DAYS there is nothing to fetch; queueing them would burn the daily
    API budget re-confirming a loss and would inflate the overdue alert forever."""
    _market(conn, "T-GONE", ac.RETENTION_DAYS + 5)
    assert ac.pending(conn) == []


def test_an_unsettled_market_is_not_queued(conn):
    _market(conn, "T-OPEN", 10, result=None)
    assert ac.pending(conn) == []


def test_the_queue_is_ordered_by_remaining_lifetime(conn):
    """The load-bearing property. `run` truncates at MAX_FETCH, so the queue MUST hand
    back the closest-to-expiry first — a newest-first list would make the cap delete
    exactly the markets that cannot wait another day."""
    _market(conn, "T-NEW", 3)
    _market(conn, "T-OLD", 70)
    _market(conn, "T-MID", 40)
    assert [q["ticker"] for q in ac.pending(conn)] == ["T-OLD", "T-MID", "T-NEW"]


# ── the cap ──────────────────────────────────────────────────────────────────────────

def test_the_cap_defers_the_ones_with_time_left_never_the_urgent_ones(conn):
    _market(conn, "T-NEW", 3)
    _market(conn, "T-OLD", 70)
    md = FakeMD()
    out = ac.run(conn, md, max_fetch=1)
    assert md.calls == ["T-OLD"], "the cap dropped the market that was about to expire"
    assert out["deferred"] == 1


def test_a_deferred_market_is_reported_not_silently_dropped(conn):
    _market(conn, "T-A", 10)
    _market(conn, "T-B", 20)
    out = ac.run(conn, FakeMD(), max_fetch=1)
    assert out["deferred"] == 1 and out["pending"] == 2


def test_deferred_work_survives_to_the_next_run(conn):
    _market(conn, "T-A", 10)
    _market(conn, "T-B", 20)
    ac.run(conn, FakeMD(), max_fetch=1)          # FakeMD writes nothing to `candles`
    assert len(ac.pending(conn)) == 2, "a deferred market must stay queued"


# ── the alert ────────────────────────────────────────────────────────────────────────

def _alerts(conn):
    return conn.execute("SELECT * FROM alerts WHERE source='archive_candles'").fetchall()


def test_the_alert_fires_with_slack_left_not_at_the_deadline(conn):
    """An alert at RETENTION_DAYS is a post-mortem. WARN_AGE_DAYS has to leave enough room
    to notice and act, and the constants must stay in that order."""
    assert ac.WARN_AGE_DAYS < ac.RETENTION_DAYS - 14
    _market(conn, "T-LATE", ac.WARN_AGE_DAYS + 1)
    ac.run(conn, FakeMD())
    rows = _alerts(conn)
    assert len(rows) == 1 and rows[0]["level"] == "warn"
    assert "KXWTIW" in rows[0]["message"]


def test_a_healthy_lane_is_silent(conn):
    _market(conn, "T-FRESH", 2)
    ac.run(conn, FakeMD())
    assert _alerts(conn) == []


def test_the_warning_is_recorded_even_if_every_fetch_then_fails(conn):
    """Written before the fetch loop on purpose: the alert describes the LANE's health,
    and a run that dies partway is exactly when someone needs to have been told."""
    _market(conn, "T-LATE", ac.WARN_AGE_DAYS + 1)
    out = ac.run(conn, FakeMD(fail_on=["T-LATE"]))
    assert out["failed"] == 1 and len(_alerts(conn)) == 1


# ── the third state: asked, and there is genuinely nothing ───────────────────────────

def test_a_permanently_untraded_market_leaves_the_queue(conn):
    """All 14 markets in the production queue were deep-OTM strikes answering 200-empty.
    Without a terminal state they are re-fetched daily until expiry AND they pin the
    overdue alert on forever, which is how a warning becomes noise."""
    _market(conn, "T-EMPTY", 30)
    out = ac.run(conn, FakeMD(empty=["T-EMPTY"]))
    assert out["empty"] == 1 and out["bars"] == 0
    assert ac.pending(conn) == [], "an untraded market stayed queued"


def test_a_freshly_closed_empty_market_is_not_written_off_yet(conn):
    """The one way this could destroy data: marking a market terminal before its final bar
    has posted. Inside EMPTY_CONFIRM_DAYS it stays queued and is retried."""
    _market(conn, "T-FRESH", 1)
    out = ac.run(conn, FakeMD(empty=["T-FRESH"]))
    assert out["empty"] == 0
    assert [q["ticker"] for q in ac.pending(conn)] == ["T-FRESH"]


def test_confirming_empty_does_not_fabricate_a_price(conn):
    """The sentinel must read as "no market", never as a real quote — it is shaped exactly
    like `kalshi_md.candles`'s 404 row, and `_market_leg_prob` returns None on NULL."""
    from prediction_market_macro.research.backtest import _market_leg_prob
    _market(conn, "T-EMPTY", 30)
    ac.run(conn, FakeMD(empty=["T-EMPTY"]))
    r = conn.execute("SELECT * FROM candles WHERE ticker='T-EMPTY'").fetchone()
    assert r["end_ts"] == 0 and r["yes_bid_close"] is None and r["price_close"] is None
    assert _market_leg_prob(conn, "T-EMPTY", NOW) is None


def test_an_empty_market_stops_counting_as_overdue(conn):
    """The point of the third state: the alert has to be able to go quiet again."""
    _market(conn, "T-EMPTY", ac.WARN_AGE_DAYS + 1)
    ac.run(conn, FakeMD(empty=["T-EMPTY"]))
    conn.execute("DELETE FROM alerts")
    assert ac.run(conn, FakeMD())["overdue"] == 0
    assert _alerts(conn) == []


# ── failure handling ─────────────────────────────────────────────────────────────────

def test_one_failure_does_not_strand_the_rest_of_the_queue(conn):
    _market(conn, "T-BAD", 60)
    _market(conn, "T-OK1", 30)
    _market(conn, "T-OK2", 10)
    md = FakeMD(fail_on=["T-BAD"])
    out = ac.run(conn, md)
    assert md.calls == ["T-BAD", "T-OK1", "T-OK2"]
    assert out["fetched"] == 2 and out["failed"] == 1 and out["bars"] == 14


def test_dry_run_reports_the_queue_without_touching_the_api(conn):
    _market(conn, "T-A", 70)
    md = FakeMD()
    out = ac.run(conn, md, dry_run=True)
    assert md.calls == []
    assert out["pending"] == 1 and out["oldest_age_days"] >= 69.9


# ── the constant ─────────────────────────────────────────────────────────────────────

def test_retention_stays_inside_the_measured_bracket(conn):
    """Probed 2026-08-06 across four series: 73d still answered, 76d was the first 404.
    RETENTION_DAYS must sit at the conservative end — over-estimating it means skipping a
    market we could still have saved, and that error cannot be undone."""
    assert 73 <= ac.RETENTION_DAYS <= 75
