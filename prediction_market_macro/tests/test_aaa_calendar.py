"""AAA actuals follow same-date source evidence, never the delayed EIA proxy."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest import calendars as cal
from prediction_market_macro.ingest.store import init_db


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / "calendar.db")
    yield c
    c.close()


NOW = datetime(2026, 9, 9, 9, tzinfo=timezone.utc)
PERIOD = "2026-09-07"
SCHEDULED = "2026-09-07T13:00:00+00:00"
AAA_KNOWN = "2026-09-07T09:01:30+00:00"


def _release(conn, period=PERIOD, scheduled=SCHEDULED, actual=None):
    conn.execute("INSERT INTO releases VALUES('AAA_WEEKLY',?,?,?)",
                 (period, scheduled, actual))
    conn.execute("INSERT INTO coverage VALUES('KXAAAGASW',?,'scheduled',?)",
                 (period, NOW.isoformat()))
    conn.commit()


def _obs(conn, sid="AAA_DAILY", event=PERIOD, known=AAA_KNOWN):
    conn.execute("INSERT INTO fred_obs VALUES(?,?,?,?,?,?)",
                 (sid, event, 4.15, known[:10], known, known))
    conn.commit()


def _actual(conn, period=PERIOD):
    return conn.execute("SELECT actual_ts FROM releases WHERE cal='AAA_WEEKLY'"
                        " AND period=?", (period,)).fetchone()[0]


def test_same_date_aaa_before_scheduled_window_is_the_actual(conn):
    _release(conn)
    _obs(conn)
    # Weekly EIA is later and describes a different publisher's number.
    _obs(conn, sid="GASREGW", known="2026-09-07T22:00:00+00:00")
    out = cal.reconcile_actuals(conn, NOW)
    assert out == {"actual_filled": 1, "postponed_flagged": 0}
    assert _actual(conn) == AAA_KNOWN
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_same_date_reading_reconciles_during_the_morning_refresh(conn):
    _release(conn)
    _obs(conn)
    morning = datetime(2026, 9, 7, 9, 2, tzinfo=timezone.utc)
    assert cal.reconcile_actuals(conn, morning) == {"actual_filled": 1, "postponed_flagged": 0}
    assert _actual(conn) == AAA_KNOWN


@pytest.mark.parametrize("evidence", ["missing", "future_knowledge", "future_eastern_date"])
def test_early_aaa_path_never_borrows_future_or_absent_observations(conn, evidence):
    _release(conn)
    morning = datetime(2026, 9, 7, 9, 2, tzinfo=timezone.utc)
    if evidence == "future_knowledge":
        _obs(conn, known=(morning + timedelta(hours=1)).isoformat())
    elif evidence == "future_eastern_date":
        # UTC is already Monday, while the source's Eastern date is still Sunday.
        morning = datetime(2026, 9, 7, 2, tzinfo=timezone.utc)
        _obs(conn, known=(morning - timedelta(hours=1)).isoformat())
    assert cal.reconcile_actuals(conn, morning) == {"actual_filled": 0, "postponed_flagged": 0}
    assert _actual(conn) is None
    assert conn.execute("SELECT state FROM coverage").fetchone()[0] == "scheduled"
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_neighbouring_days_and_eia_do_not_fill_missing_aaa(conn):
    _release(conn)
    _obs(conn, event="2026-09-06", known="2026-09-06T09:00:00+00:00")
    _obs(conn, event="2026-09-08", known="2026-09-08T09:00:00+00:00")
    _obs(conn, sid="GASREGW", known="2026-09-07T22:00:00+00:00")
    first = cal.reconcile_actuals(conn, NOW)
    second = cal.reconcile_actuals(conn, NOW + timedelta(hours=2))
    assert _actual(conn) is None
    assert first["postponed_flagged"] == 1
    assert second["postponed_flagged"] == 0
    alert = conn.execute("SELECT message FROM alerts").fetchone()[0]
    assert "MISSING AAA reading AAA_WEEKLY/2026-09-07" in alert
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


def test_future_knowledge_does_not_reconcile_until_observed(conn):
    _release(conn)
    later = NOW + timedelta(hours=1)
    _obs(conn, known=later.isoformat())
    cal.reconcile_actuals(conn, NOW)
    assert _actual(conn) is None
    cal.reconcile_actuals(conn, later + timedelta(seconds=1))
    assert _actual(conn) == later.isoformat()


def test_aaa_missing_before_26_hours_does_not_alert(conn):
    _release(conn)
    out = cal.reconcile_actuals(conn, datetime(2026, 9, 8, 14, tzinfo=timezone.utc))
    assert out["postponed_flagged"] == 0
    assert _actual(conn) is None


def test_historical_unknown_aaa_stays_null_without_recurring_alert(conn):
    _release(conn, period="2026-07-06", scheduled="2026-07-06T13:00:00+00:00")
    assert cal.reconcile_actuals(conn, NOW)["postponed_flagged"] == 0
    assert _actual(conn, "2026-07-06") is None
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    # A subsequently imported, dated observation remains valid even for an old gap.
    _obs(conn, event="2026-07-06", known="2026-07-06T09:00:00+00:00")
    assert cal.reconcile_actuals(conn, NOW)["actual_filled"] == 1


def test_existing_legacy_alert_prevents_duplicate_warning(conn):
    _release(conn)
    conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                 (NOW.isoformat(), "warn", "calendar",
                  f"POSTPONED? AAA_WEEKLY/{PERIOD} scheduled {SCHEDULED} but no first print within 26h"))
    conn.commit()
    assert cal.reconcile_actuals(conn, NOW)["postponed_flagged"] == 0
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


def test_repair_preview_and_apply_only_change_aaa_actuals(conn):
    _release(conn, actual="2026-09-07T22:00:00+00:00")
    _release(conn, period="2026-07-06", scheduled="2026-07-06T13:00:00+00:00",
             actual="2026-07-06T22:00:00+00:00")
    _obs(conn)
    conn.execute("INSERT INTO releases VALUES('FOMC','2026-07',?,?)", (SCHEDULED, SCHEDULED))
    conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                 (NOW.isoformat(), "warn", "calendar", "old warning"))
    conn.commit()
    coverage = [tuple(r) for r in conn.execute("SELECT * FROM coverage")]
    alerts = [tuple(r) for r in conn.execute("SELECT * FROM alerts")]
    preview = cal.repair_aaa_actuals(conn, NOW)
    assert preview["applied"] is False and preview["n_changed"] == 2
    assert _actual(conn) == "2026-09-07T22:00:00+00:00"
    assert conn.in_transaction is False
    applied = cal.repair_aaa_actuals(conn, NOW, apply=True)
    assert applied["changes"] == preview["changes"]
    assert _actual(conn) == AAA_KNOWN
    assert _actual(conn, "2026-07-06") is None
    assert cal.repair_aaa_actuals(conn, NOW, apply=True)["n_changed"] == 0
    assert conn.execute("SELECT actual_ts FROM releases WHERE cal='FOMC'").fetchone()[0] == SCHEDULED
    assert [tuple(r) for r in conn.execute("SELECT * FROM coverage")] == coverage
    assert [tuple(r) for r in conn.execute("SELECT * FROM alerts")] == alerts


def test_repair_cli_defaults_to_read_only_preview(tmp_path, capsys):
    path = tmp_path / "legacy.db"
    c = init_db(path)
    _release(c, actual="2026-09-07T22:00:00+00:00")
    _obs(c)
    c.close()
    cal._main(["repair-aaa-actuals", "--db", str(path)])
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] is False and result["n_changed"] == 1
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT actual_ts FROM releases").fetchone()[0] == "2026-09-07T22:00:00+00:00"
    cal._main(["repair-aaa-actuals", "--db", str(path), "--apply"])
    assert json.loads(capsys.readouterr().out)["applied"] is True
    with sqlite3.connect(path) as check:
        assert check.execute("SELECT actual_ts FROM releases").fetchone()[0] == AAA_KNOWN
