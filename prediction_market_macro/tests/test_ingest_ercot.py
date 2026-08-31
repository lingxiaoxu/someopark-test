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


def test_no_model_reads_ercot_daily_yet():
    """§7-bis: SHADOW means shadow. This greps the model package for the table name so
    that the first consumer must come through a preregistration that updates this test."""
    from pathlib import Path
    import prediction_market_macro.model as M
    root = Path(M.__file__).parent
    hits = [p.name for p in root.glob("*.py") if "ercot_daily" in p.read_text()]
    assert hits == [], f"models reading a shadow table without a gate: {hits}"
