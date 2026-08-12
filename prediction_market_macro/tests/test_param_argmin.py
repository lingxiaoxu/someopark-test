"""param_argmin.daily — the standing-user-policy loop's four contracts.

1. Fingerprint cache: an unchanged sample never rescored (the morning pass must be
   cheap on no-news days).
2. A changed argmin writes a NEW manual_params row (history grows, nothing replaced)
   and the note carries the DSR objection.
3. An unchanged argmin writes NO manual row (no history spam), but still logs.
4. The grid always carries the default at index 0 — argmin can return to defaults.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.research import param_argmin as pa
from prediction_market_macro.research import param_select as ps

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE experiments(name TEXT, config_hash TEXT, series TEXT,
        window TEXT, metrics_json TEXT, created_ts TEXT,
        PRIMARY KEY(name, config_hash));
    CREATE TABLE settlements(ticker TEXT, series TEXT, period TEXT, result TEXT,
        settled_ts TEXT, first_seen_ts TEXT);
    CREATE TABLE contracts(ticker TEXT, series TEXT, period TEXT, status TEXT,
        close_time TEXT);
    """)
    return c


def _patch(monkeypatch, best_params, fp="3:2026-08-08"):
    monkeypatch.setattr(pa, "MARKETS", ["KXNATGASW"])
    monkeypatch.setattr(pa, "_fingerprint", lambda *a: fp)
    monkeypatch.setattr(pa, "rescore", lambda *a, **k: {
        "grid": [{}, best_params], "grid_report": {}, "n_events": 3,
        "best_idx": 1, "best_params": best_params,
        "pnl_best": 1.0, "pnl_default": -1.0})


def _hermetic_manual(monkeypatch):
    """Clock-free manual store. The real set_manual stamps wall-clock created_ts
    while these tests run at a FIXED simulated NOW — the PIT comparison inside
    manual_params turns that into a date-dependent time bomb (this file passed on
    2026-08-11 and failed on 08-12 with zero code changes). daily()'s changed/
    unchanged logic only needs get/set semantics, so give it exactly that."""
    store: dict = {}
    monkeypatch.setattr(pa, "set_manual",
                        lambda c, s, p, note: store.__setitem__(s, dict(p)))
    monkeypatch.setattr(pa, "manual_params",
                        lambda c, s, now: (store[s], "ts") if s in store else None)
    return store


def test_changed_argmin_adopts_and_history_grows(conn, monkeypatch):
    _patch(monkeypatch, {"fut_vol_window": 40})
    out = pa.daily(conn, now=NOW, log=None)
    assert out["KXNATGASW"].startswith("ADOPTED")
    h = ps.history(conn, "KXNATGASW")
    assert len(h) == 1 and h[0]["params"] == {"fut_vol_window": 40}
    assert "DSR" in h[0]["note"]
    # second run, same fingerprint -> cached, no new history
    out2 = pa.daily(conn, now=NOW + timedelta(hours=1), log=None)
    assert out2["KXNATGASW"] == "cached"
    assert len(ps.history(conn, "KXNATGASW")) == 1


def test_new_sample_same_argmin_logs_but_writes_no_manual_row(conn, monkeypatch):
    store = _hermetic_manual(monkeypatch)
    _patch(monkeypatch, {"fut_vol_window": 40})
    pa.daily(conn, now=NOW, log=None)
    assert store["KXNATGASW"] == {"fut_vol_window": 40}
    _patch(monkeypatch, {"fut_vol_window": 40}, fp="4:2026-08-09")   # new settle
    out = pa.daily(conn, now=NOW + timedelta(days=1), log=None)
    assert out["KXNATGASW"] == "unchanged"
    logs = conn.execute("SELECT COUNT(*) FROM experiments"
                        " WHERE name='param_argmin'").fetchone()[0]
    assert logs == 2


def test_argmin_can_return_to_defaults(conn, monkeypatch):
    store = _hermetic_manual(monkeypatch)
    _patch(monkeypatch, {"fut_vol_window": 40})
    pa.daily(conn, now=NOW, log=None)
    _patch(monkeypatch, {}, fp="5:2026-08-10")      # defaults win the new sample
    out = pa.daily(conn, now=NOW + timedelta(days=2), log=None)
    assert out["KXNATGASW"].startswith("ADOPTED")
    assert store["KXNATGASW"] == {}                 # active override: the defaults


def test_real_build_keeps_default_at_index_zero(conn):
    # build() needs a db with settled events for the probe; on an empty db the grid
    # must still be default-only rather than crashing.
    grid, rep = pa.build(conn, "KXNATGASW", NOW)
    assert grid[0] == {}
