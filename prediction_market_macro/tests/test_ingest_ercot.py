"""ercot.py — parser + PIT invariants on synthetic payloads (no network)."""
import sqlite3

from prediction_market_macro.ingest import ercot


def _fm():
    return {"data": {"2026-08-30": {
        "t1": {"Natural Gas": {"gen": 30000.0}, "Wind": {"gen": 10000.0}},
        "t2": {"Natural Gas": {"gen": 32000.0}, "Wind": {"gen": 8000.0}}}}}


def test_fuel_mix_gas_share_and_average():
    d = ercot._fuel_mix_days(_fm())["2026-08-30"]
    assert d["gas_gen_avg_mw"] == 31000.0
    assert d["total_gen_avg_mw"] == 40000.0
    assert abs(d["gas_share"] - 0.775) < 1e-12


def test_refresh_upserts_and_preserves_first_seen(monkeypatch):
    """A partial day is overwritten as it completes, but first_seen_ts must survive —
    it is the PIT answer to 'when did we first see this day at all'."""
    payloads = {"fuel-mix": _fm(),
                "supply-demand": {"data": [{"timestamp": "2026-08-30 08:20:00-0500",
                                            "demand": 60000, "capacity": 80000}]},
                "system-wide-prices": {"rtSppData": [{"hbHubAvg": 25.0}],
                                       "damSppData": []},
                "combine-wind-solar": {"currentDay": {"date": "2026-08-30 03:00:00-0500",
                                                      "data": {"k": {"actualWind": 9000,
                                                                     "actualSolar": 5000}}}}}
    monkeypatch.setattr(ercot, "_get", lambda name: payloads[name])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    r = ercot.refresh(conn)
    assert r["rows"] >= 7
    first = conn.execute("SELECT first_seen_ts FROM ercot_daily WHERE date='2026-08-30'"
                         " AND metric='gas_gen_avg_mw'").fetchone()[0]
    payloads["fuel-mix"]["data"]["2026-08-30"]["t3"] = {"Natural Gas": {"gen": 40000.0}}
    ercot.refresh(conn)
    row = conn.execute("SELECT value, first_seen_ts FROM ercot_daily WHERE"
                       " date='2026-08-30' AND metric='gas_gen_avg_mw'").fetchone()
    assert row["value"] != 31000.0                     # revised as the day completed
    assert row["first_seen_ts"] == first               # PIT anchor survives


def test_ercot_daily_is_read_only_through_the_registered_covariate():
    """§7-bis, updated by PR-31: the ONE registered consumer is model/ercot_cov.py,
    whose shifts reach models only behind params['ercot_w'] (default 0). Any other
    model file touching the table must arrive with its own registration and update
    this list."""
    from pathlib import Path
    import prediction_market_macro.model as M
    root = Path(M.__file__).parent
    hits = sorted(p.name for p in root.glob("*.py") if "ercot_daily" in p.read_text())
    assert hits == ["ercot_cov.py"], f"unregistered ercot_daily consumers: {hits}"


def test_ercot_w_zero_is_bit_identical_and_cov_never_raises():
    """PR-31's ground rule: the default arm must be production bit-for-bit, and the
    covariate must never be the reason a prediction fails (empty table -> shift 0)."""
    import sqlite3
    from datetime import datetime, timezone
    from prediction_market_macro.model import ercot_cov
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE ercot_daily(date TEXT, metric TEXT, value REAL,"
                 " knowledge_time TEXT, first_seen_ts TEXT)")
    conn.execute("CREATE TABLE fut_daily(root TEXT, event_time TEXT, close REAL)")
    conn.execute("CREATE TABLE fred_obs(sid TEXT, event_time TEXT, value REAL,"
                 " vintage_date TEXT, knowledge_time TEXT)")
    ercot_cov.clear_cache()
    now = datetime(2026, 8, 30, tzinfo=timezone.utc)
    for s in ("KXNATGASW", "KXWTIW", "KXJOBLESSCLAIMS", "KXCPI", "KXCPIYOY", "KXOTHER"):
        assert ercot_cov.mu_shift(conn, now, s) == 0.0
    ercot_cov.clear_cache()
