"""FOMC statement backfill (§28) — the PIT stamp and the key, offline.

Nothing here touches the network. What is worth pinning is not "can we GET a page" but
the three things that were actually wrong before the backfill:

  1. the table had no time column at all, so it could not be PIT-filtered;
  2. `period` alone as the primary key silently dropped the second statement of a
     month — and those are the interesting ones (2008-01-22, 2020-03-15);
  3. release time was never read off the page, so an emergency Sunday-evening action
     would have been stamped like a 2 p.m. Wednesday one.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from prediction_market_macro.ingest import fed_text
from prediction_market_macro.ingest.store import connect, init_db


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


def _put(conn, date_s, text, url=None):
    kt, src = fed_text._release_ts(date_s, text)
    conn.execute(
        "INSERT OR REPLACE INTO fed_statements(period, release_date, url, text,"
        " knowledge_time, time_source, fetched_ts) VALUES(?,?,?,?,?,?,?)",
        (date_s[:7], date_s, url or f"http://x/{date_s}", text, kt, src, "2026-08-04"))
    conn.commit()
    return kt, src


# ── release time ────────────────────────────────────────────────────────────
def test_release_line_is_read_off_the_page():
    kt, src = fed_text._release_ts("2026-07-29", "For release at 2:00 p.m. EDT ...")
    assert src == "page"
    assert kt == "2026-07-29T18:00:00+00:00"      # stored UTC, see _et_to_utc


def test_emergency_action_keeps_its_own_hour():
    """2020-03-15 went out at 5 p.m. on a Sunday. Stamping it 2 p.m. would let a
    replay trade three hours before the Fed spoke."""
    kt, src = fed_text._release_ts("2020-03-15", "For release at 5:00 p.m. EDT ...")
    assert (src, kt) == ("page", "2020-03-15T21:00:00+00:00")


def test_unstamped_statement_falls_back_late_not_early():
    """1994-2015 pages say only 'For immediate release'. The honest stamp is
    end-of-day, never the customary 14:15 — a guessed early time IS a leak."""
    kt, src = fed_text._release_ts("1994-02-04", "For immediate release Chairman ...")
    assert src == "eod"
    assert kt == "1994-02-05T04:59:00+00:00"          # EST (-05:00), and it rolls the UTC date


def test_noon_and_midnight_do_not_wrap():
    assert fed_text._release_ts("2020-03-03", "For release at 12:00 p.m. EST")[0] \
        == "2020-03-03T17:00:00+00:00"
    assert fed_text._release_ts("2020-03-03", "For release at 12:30 a.m. EST")[0] \
        == "2020-03-03T05:30:00+00:00"


def test_eod_knowledge_time_tracks_dst():
    assert fed_text.eod_knowledge_time("2026-01-28") == "2026-01-29T04:59:00+00:00"   # EST
    assert fed_text.eod_knowledge_time("2026-07-29") == "2026-07-30T03:59:00+00:00"   # EDT


# ── the key ─────────────────────────────────────────────────────────────────
def test_two_statements_in_one_month_both_survive(conn):
    _put(conn, "2020-03-03", "For release at 10:00 a.m. EST emergency cut ... Committee")
    _put(conn, "2020-03-15", "For release at 5:00 p.m. EDT ... Committee")
    rows = conn.execute("SELECT release_date FROM fed_statements WHERE period='2020-03'"
                        " ORDER BY release_date").fetchall()
    assert [r[0] for r in rows] == ["2020-03-03", "2020-03-15"]


# ── the PIT accessor ────────────────────────────────────────────────────────
def test_statements_asof_cannot_see_the_future(conn):
    _put(conn, "2026-06-17", "For release at 2:00 p.m. EDT June ... Committee")
    _put(conn, "2026-07-29", "For release at 2:00 p.m. EDT July ... Committee")
    mid_july = datetime(2026, 7, 15, tzinfo=timezone.utc)
    got = fed_text.statements_asof(conn, mid_july, limit=2)
    assert [g["release_date"] for g in got] == ["2026-06-17"]


def test_statements_asof_respects_the_hour_not_just_the_day(conn):
    """The whole point of parsing the release line: at 13:00 ET on decision day the
    statement does not exist yet."""
    _put(conn, "2026-07-29", "For release at 2:00 p.m. EDT ... Committee")
    before = datetime(2026, 7, 29, 17, 0, tzinfo=timezone.utc)      # 13:00 ET
    after = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)       # 15:00 ET
    assert fed_text.statements_asof(conn, before) == []
    assert len(fed_text.statements_asof(conn, after)) == 1


def test_statements_asof_orders_newest_first(conn):
    for d in ("2026-04-29", "2026-06-17", "2026-07-29"):
        _put(conn, d, "For release at 2:00 p.m. EDT ... Committee")
    got = fed_text.statements_asof(conn, datetime(2026, 8, 4, tzinfo=timezone.utc), limit=2)
    assert [g["release_date"] for g in got] == ["2026-07-29", "2026-06-17"]


# ── the migration ───────────────────────────────────────────────────────────
def test_v1_rows_are_carried_over_with_a_derived_date_and_stamp(tmp_path):
    p = tmp_path / "old.db"
    c = connect(p)
    c.executescript(
        "CREATE TABLE fed_statements(period TEXT PRIMARY KEY, url TEXT NOT NULL,"
        " text TEXT NOT NULL, fetched_ts TEXT NOT NULL);"
        "CREATE TABLE alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT,"
        " source TEXT, message TEXT, acked INTEGER DEFAULT 0);")
    c.execute("INSERT INTO fed_statements VALUES('2026-01',"
              "'https://x/newsevents/pressreleases/monetary20260128a.htm','body','t0')")
    c.commit()
    c.close()

    c = init_db(p)
    r = c.execute("SELECT * FROM fed_statements").fetchone()
    assert (r["period"], r["release_date"]) == ("2026-01", "2026-01-28")
    assert r["knowledge_time"] == "2026-01-29T04:59:00+00:00"
    assert r["time_source"] == "eod"          # backfill(refetch=True) upgrades it later
    assert r["text"] == "body"
    # the old table is renamed, never dropped — PLAN §5 "nothing is ever DELETEd"
    assert c.execute("SELECT COUNT(*) FROM fed_statements_v1").fetchone()[0] == 1


def test_migration_is_idempotent(tmp_path):
    p = tmp_path / "t.db"
    init_db(p).close()
    c = init_db(p)                             # second call must not re-rename
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='fed_statements_v1'"
                     ).fetchone()[0] == 0


# ── the calendar scrape, on saved markup ────────────────────────────────────
_HIST_SNIPPET = """
<a href="/aboutthefed/fed-financial-statements.htm">Fed Financial Statements</a>
<a href="/fomc/19940204default.htm">Statement</a>
<a href="/monetarypolicy/files/FOMC_LongerRunGoals_201501.pdf">Statement on Longer-Run
 Goals and Monetary Policy Strategy</a>
<a href="/newsevents/press/monetary/20080122b.htm">Statement</a>
"""

_CAL_SNIPPET = """
<strong>Statement:</strong><br>
<a href="/monetarypolicy/files/monetary20260729a1.pdf">PDF</a> |
<a href="/newsevents/pressreleases/monetary20260729a.htm">HTML</a><br>
</div>
<a href="/newsevents/pressreleases/monetary20260729a1.htm">Implementation Note</a>
"""


def _links(markup, monkeypatch):
    class R:
        status_code = 200
        text = markup
    monkeypatch.setattr(fed_text.requests, "get", lambda *a, **k: R())
    return dict(fed_text._statement_links("http://x"))


def test_historical_page_yields_statements_not_the_framework_pdf(monkeypatch):
    got = _links(_HIST_SNIPPET, monkeypatch)
    assert got == {
        "1994-02-04": "https://www.federalreserve.gov/fomc/19940204default.htm",
        "2008-01-22": "https://www.federalreserve.gov/newsevents/press/monetary/20080122b.htm"}


def test_calendar_page_yields_the_statement_html_not_the_implementation_note(monkeypatch):
    got = _links(_CAL_SNIPPET, monkeypatch)
    assert got == {"2026-07-29":
                   "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm"}
