"""pjm.py — parser + PIT invariants on synthetic payloads (no network).

Deliberately the twin of test_ingest_ercot.py: the PJM lane earns trust the same way the
ERCOT lane did, and the shadow tripwire below is the reason a consumer cannot appear
without a preregistration.
"""
import sqlite3

from prediction_market_macro.ingest import pjm


def _items(hours=24, gas=48000.0, coal=18000.0):
    out = []
    for h in range(hours):
        ts = f"2026-08-28T{h:02d}:00:00"
        out.append({"datetime_beginning_ept": ts, "fuel_type": "Gas", "mw": gas})
        out.append({"datetime_beginning_ept": ts, "fuel_type": "Coal", "mw": coal})
    return out


def test_fuel_mix_averages_and_share():
    d = pjm._fuel_mix_days(_items())["2026-08-28"]
    assert d["gas_gen_avg_mw"] == 48000.0
    assert d["total_gen_avg_mw"] == 66000.0
    assert abs(d["gas_share"] - 48000.0 / 66000.0) < 1e-12


def test_a_boundary_day_with_too_few_hours_is_not_a_day():
    """The request window always clips a day at each end; an 11-hour stub is a window
    artifact, not an observation. ERCOT learned this on 5-minute rows (>=12 intervals);
    the same guard on hourly rows is >=12 hours."""
    assert pjm._fuel_mix_days(_items(hours=11)) == {}
    assert "2026-08-28" in pjm._fuel_mix_days(_items(hours=12))


def test_refresh_upserts_and_preserves_first_seen(monkeypatch):
    """A partial day is overwritten as it completes, but first_seen_ts must survive — it
    is the PIT answer to 'when did we first see this day at all'."""
    monkeypatch.setattr(pjm, "_dm_get", lambda feed, params: _items(hours=12))
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    r = pjm.refresh(conn)
    assert r["days"] == 1 and r["rows"] == 3
    first = conn.execute("SELECT first_seen_ts FROM pjm_daily WHERE date='2026-08-28'"
                         " AND metric='gas_gen_avg_mw'").fetchone()[0]
    monkeypatch.setattr(pjm, "_dm_get", lambda feed, params: _items(hours=24, gas=52000.0))
    pjm.refresh(conn)
    row = conn.execute("SELECT value, first_seen_ts FROM pjm_daily WHERE"
                       " date='2026-08-28' AND metric='gas_gen_avg_mw'").fetchone()
    assert row["value"] == 52000.0                     # revised as the day completed
    assert row["first_seen_ts"] == first               # PIT anchor survives


def test_a_nan_is_never_stored():
    """value REAL NOT NULL accepts NaN in sqlite; the guard is in code, so it needs a
    test that would notice if it were removed."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(pjm._SCHEMA)
    n = pjm._upsert(conn, {"2026-08-28": {"gas_gen_avg_mw": float("nan"),
                                          "total_gen_avg_mw": 1.0}}, "now")
    assert n == 1
    assert conn.execute("SELECT COUNT(*) FROM pjm_daily").fetchone()[0] == 1


def test_mirror_is_empty_without_deep_history_and_never_raises():
    conn = sqlite3.connect(":memory:")
    conn.executescript(pjm._SCHEMA)
    conn.executescript(
        "CREATE TABLE fred_obs(sid TEXT, event_time TEXT, value REAL,"
        " vintage_date TEXT, knowledge_time TEXT, first_seen_ts TEXT,"
        " PRIMARY KEY(sid, event_time, vintage_date))")
    assert pjm.mirror_weekly_burn(conn) == 0


def test_pjm_daily_has_no_model_consumer_yet():
    """§7-bis: SHADOW means shadow. The first consumer must arrive together with a
    preregistered gate, and updating this list is how that gate announces itself —
    exactly as test_ingest_ercot.py did for ercot_cov.py."""
    from pathlib import Path
    import prediction_market_macro.model as M
    root = Path(M.__file__).parent
    hits = sorted(p.name for p in root.glob("*.py") if "pjm_daily" in p.read_text())
    # PR-33 registered pjm_cov.py as the ONE consumer; it reaches models only behind
    # params['pjm_w'] (default 0). Any other model file touching the table must arrive
    # with its own registration and update this list.
    assert hits == ["pjm_cov.py"], f"unregistered pjm_daily consumers: {hits}"


def test_pjm_cov_is_silent_by_default_and_never_raises():
    """PR-33's ground rule: pjm_w=0 is bit-identical, and the covariate can never be the
    reason a prediction fails — empty tables, wrong series, missing columns all give 0.0."""
    import sqlite3
    from datetime import datetime, timezone
    from prediction_market_macro.model import pjm_cov
    conn = sqlite3.connect(":memory:")
    conn.executescript(pjm._SCHEMA)
    conn.executescript(
        "CREATE TABLE ercot_daily(date TEXT, metric TEXT, value REAL,"
        " knowledge_time TEXT, first_seen_ts TEXT);"
        "CREATE TABLE fred_obs(sid TEXT, event_time TEXT, value REAL,"
        " vintage_date TEXT, knowledge_time TEXT, first_seen_ts TEXT)")
    pjm_cov.clear_cache()
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    for s in ("KXJOBLESSCLAIMS", "KXNATGASW", "KXCPI", "KXOTHER"):
        assert pjm_cov.mu_shift(conn, now, s) == 0.0
    pjm_cov.clear_cache()
