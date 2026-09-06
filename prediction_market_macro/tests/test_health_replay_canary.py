"""The determinism canary must not call late-arriving DATA a code drift (2026-09-05)."""
import sqlite3

from prediction_market_macro.research.health import _late_data_after


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE fut_daily(root TEXT, event_time TEXT, open REAL, high REAL, low REAL,"
        " close REAL, volume REAL, knowledge_time TEXT, first_seen_ts TEXT,"
        " PRIMARY KEY(root, event_time));"
        "CREATE TABLE fred_obs(sid TEXT, event_time TEXT, value REAL, vintage_date TEXT,"
        " knowledge_time TEXT, first_seen_ts TEXT, PRIMARY KEY(sid, event_time, vintage_date))")
    return conn


def test_a_bar_that_arrived_after_the_pred_was_written_explains_the_diff():
    """The 2026-09-05 incident, exactly: NG 09-03 bar, knowledge_time 09-03T22:00,
    first_seen 09-05T09:02; pred written 09-04T09:16 at asof 09-04T09:16."""
    conn = _db()
    conn.execute("INSERT INTO fut_daily VALUES('NG','2026-09-03',3,3,2.9,2.913,1,"
                 "'2026-09-03T22:00:00','2026-09-05T09:02:23')")
    conn.execute("INSERT INTO fut_daily VALUES('NG','2026-09-02',3,3,2.9,2.956,1,"
                 "'2026-09-02T22:00:00','2026-09-03T09:02:43')")
    late = _late_data_after(conn, "2026-09-04T09:16:00+00:00", "2026-09-04T09:16:30+00:00")
    assert late == ["NG:2026-09-03"]          # the 09-02 bar was there in time


def test_data_that_was_present_in_time_is_not_late():
    conn = _db()
    conn.execute("INSERT INTO fut_daily VALUES('NG','2026-09-02',3,3,2.9,2.956,1,"
                 "'2026-09-02T22:00:00','2026-09-03T09:02:43')")
    assert _late_data_after(conn, "2026-09-04T09:16:00+00:00",
                            "2026-09-04T09:16:30+00:00") == []


def test_data_knowable_only_after_asof_is_not_the_explanation():
    """A bar with knowledge_time AFTER asof is invisible to both the pred and the
    re-prediction, so it cannot explain a diff and must not be reported."""
    conn = _db()
    conn.execute("INSERT INTO fut_daily VALUES('NG','2026-09-04',3,3,2.9,2.975,1,"
                 "'2026-09-04T22:00:00','2026-09-05T09:02:23')")
    assert _late_data_after(conn, "2026-09-04T09:16:00+00:00",
                            "2026-09-04T09:16:30+00:00") == []


def test_a_broken_lookup_never_decides_a_breaker():
    conn = sqlite3.connect(":memory:")            # no tables at all
    assert _late_data_after(conn, "2026-09-04T09:16:00+00:00",
                            "2026-09-04T09:16:30+00:00") == []
