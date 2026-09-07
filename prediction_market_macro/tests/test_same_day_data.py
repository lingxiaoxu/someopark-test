"""2026-09-06: three data-latency fixes, each pinned on the incident that motivated it."""
import sqlite3
from datetime import datetime, timedelta, timezone

from prediction_market_macro.ingest import treasury
from prediction_market_macro.ingest.fred import _knowledge_time
from prediction_market_macro.jobs import tick

XML = """<feed><entry><content><m:properties>
<d:NEW_DATE m:type="Edm.DateTime">2026-09-03T00:00:00</d:NEW_DATE>
<d:BC_2YEAR m:type="Edm.Double">4.34</d:BC_2YEAR><d:BC_10YEAR m:type="Edm.Double">4.77</d:BC_10YEAR>
</m:properties></content></entry><entry><content><m:properties>
<d:NEW_DATE m:type="Edm.DateTime">2026-09-04T00:00:00</d:NEW_DATE>
<d:BC_2YEAR m:type="Edm.Double">4.37</d:BC_2YEAR><d:BC_5YEAR m:type="Edm.Double">4.50</d:BC_5YEAR>
<d:BC_10YEAR m:type="Edm.Double">4.78</d:BC_10YEAR><d:BC_30YEAR m:type="Edm.Double">5.10</d:BC_30YEAR>
</m:properties></content></entry></feed>"""


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE fred_obs(sid TEXT, event_time TEXT, value REAL, vintage_date TEXT,"
        " knowledge_time TEXT, first_seen_ts TEXT, PRIMARY KEY(sid, event_time, vintage_date));"
        "CREATE TABLE alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT,"
        " source TEXT, message TEXT, acked INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE fut_daily(root TEXT, event_time TEXT, close REAL, PRIMARY KEY(root, event_time))")
    return conn


def test_treasury_rows_carry_freds_exact_stamping_and_fred_dedupes_against_them():
    """The 09-04 incident: Treasury has 4.37 on Friday; FRED will have it Monday. The row
    must be stamped exactly as fred.py stamps a market sid, so the later FRED insert is a
    PK duplicate of an identical value."""
    conn = _db()
    got = treasury.pull(conn, ["202609"], fetcher=lambda ym: XML)
    assert got["inserted"] == 6 and got["mismatch"] == 0 and got["latest"] == "2026-09-04"
    r = conn.execute("SELECT * FROM fred_obs WHERE sid='DGS2' AND event_time='2026-09-04'").fetchone()
    assert r["value"] == 4.37 and r["vintage_date"] == "2026-09-04"
    assert r["knowledge_time"] == _knowledge_time("DGS2", "2026-09-04")
    # FRED arrives later with the same value under the same PK -> ignored, nothing changes
    n = conn.execute("INSERT OR IGNORE INTO fred_obs VALUES('DGS2','2026-09-04',4.37,"
                     "'2026-09-04',?, 'later')", (_knowledge_time("DGS2", "2026-09-04"),)).rowcount
    assert n == 0
    # a second Treasury pull is a no-op
    assert treasury.pull(conn, ["202609"], fetcher=lambda ym: XML)["inserted"] == 0


def test_a_disagreeing_fred_row_is_never_overwritten_and_raises_a_flag():
    conn = _db()
    conn.execute("INSERT INTO fred_obs VALUES('DGS2','2026-09-03',4.30,'2026-09-03','k','f')")
    got = treasury.pull(conn, ["202609"], fetcher=lambda ym: XML)
    assert got["mismatch"] == 1
    assert conn.execute("SELECT value FROM fred_obs WHERE sid='DGS2' AND event_time='2026-09-03'").fetchone()[0] == 4.30
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE message LIKE 'treasury_fred_mismatch:DGS2:2026-09-03%'").fetchone()[0] == 1


def test_treasury_due_only_on_weekday_afternoons_when_todays_row_is_missing():
    conn = _db()
    sun = datetime(2026, 9, 6, 20, 0, tzinfo=timezone.utc)
    assert treasury.due(conn, sun) is False                      # weekend
    mon_early = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
    assert treasury.due(conn, mon_early) is False                # before Treasury posts
    mon_late = datetime(2026, 9, 7, 20, 0, tzinfo=timezone.utc)
    assert treasury.due(conn, mon_late) is True
    conn.execute("INSERT INTO fred_obs VALUES('DGS2','2026-09-07',4.4,'2026-09-07','k','f')")
    assert treasury.due(conn, mon_late) is False                 # already have today


def test_post_release_fred_pulls_fire_once_each_at_2_and_15_minutes():
    """The T+3m reassess runs predict_all; the +2m pull must precede it."""
    done = set()
    key = ("KXPAYROLLS", "2026-09-04T12:30:00+00:00")
    assert tick.due_fred_pulls(60, done, key) == []
    assert tick.due_fred_pulls(130, done, key) == [120]
    done.add((key, 120))
    assert tick.due_fred_pulls(400, done, key) == []             # not yet 15 min
    assert tick.due_fred_pulls(901, done, key) == [900]
    done.add((key, 900))
    assert tick.due_fred_pulls(2000, done, key) == []            # never a third time


def test_late_futures_detection_knows_weekends_and_the_morning_refresh():
    conn = _db()
    for root in ("CL", "NG", "RB", "GC"):
        conn.execute("INSERT INTO fut_daily VALUES(?, '2026-09-03', 1.0)", (root,))
    # Friday 09-04 12:00Z: last completed session is Thursday 09-03 -> nothing missing
    assert tick.late_futures_roots(conn, datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)) == []
    # Monday 09-07 12:00Z: last completed session is Friday 09-04 -> all four missing
    assert tick.late_futures_roots(conn, datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)) == ["CL", "NG", "RB", "GC"]
    # ...but not before the morning refresh has had its chance, and never on a weekend
    assert tick.late_futures_roots(conn, datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)) == []
    assert tick.late_futures_roots(conn, datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)) == []
    conn.execute("INSERT INTO fut_daily VALUES('CL', '2026-09-04', 1.0)")
    assert tick.late_futures_roots(conn, datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)) == ["NG", "RB", "GC"]


def test_late_futures_repull_is_capped_per_session_date(tmp_path, monkeypatch):
    """A holiday looks like a missing bar every fire; the marker caps attempts at 3 and
    spaces them 2h apart so the tick never spins on yfinance."""
    conn = _db()
    for root in ("CL", "NG", "RB", "GC"):
        conn.execute("INSERT INTO fut_daily VALUES(?, '2026-09-03', 1.0)", (root,))
    calls = []
    from prediction_market_macro.ingest import market_data
    monkeypatch.setattr(market_data, "pull_futures", lambda conn, roots=None, **kw: calls.append(list(roots)) or 0)

    class S:
        output_dir = tmp_path
    base = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    tick._repull_late_futures(conn, S, base)
    tick._repull_late_futures(conn, S, base + timedelta(minutes=15))     # too soon
    tick._repull_late_futures(conn, S, base + timedelta(hours=2, minutes=1))
    tick._repull_late_futures(conn, S, base + timedelta(hours=4, minutes=2))
    tick._repull_late_futures(conn, S, base + timedelta(hours=6, minutes=3))  # capped
    assert len(calls) == 3
