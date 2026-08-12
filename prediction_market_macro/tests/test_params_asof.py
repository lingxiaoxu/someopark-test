"""params_asof — the read a determinism replay must use (2026-08-12 incident).

health's replay canary re-predicted stored preds at registered DEFAULTS while the
preds had been computed under the adopted manual params: byte-mismatch on every
adopted-params series, four reds, breaker force-exited all three live positions 49
minutes before the CPI print. Three contracts pinned here:

1. asof AFTER adoption → the manual params (what predict_all actually used).
2. asof BEFORE adoption → that day's param_selection row / defaults — today's
   adoption must NOT leak backwards (current()'s manual check uses wall-clock now,
   which is exactly why params_asof cannot delegate to it).
3. No rows at all → {} (registered defaults), never an exception.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from prediction_market_macro.research import param_select as ps


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE experiments(name TEXT, config_hash TEXT, series TEXT, window TEXT,
        metrics_json TEXT, created_ts TEXT, PRIMARY KEY(name, config_hash));
    CREATE TABLE param_selection(series TEXT, day TEXT, params_json TEXT,
        adopted TEXT, n_obs INT, n_trials INT, dsr_p REAL, report_json TEXT,
        created_ts TEXT);
    """)
    return c


def test_post_adoption_asof_gets_manual_params():
    c = _conn()
    ps.set_manual(c, "KXNATGASW", {"fut_vol_window": 40}, note="x")
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    assert ps.params_asof(c, "KXNATGASW", later) == {"fut_vol_window": 40}


def test_pre_adoption_asof_does_not_leak_todays_manual():
    c = _conn()
    earlier = datetime.now(timezone.utc) - timedelta(days=5)
    c.execute("INSERT INTO param_selection(series, day, params_json) VALUES"
              " ('KXNATGASW', ?, '{\"vol_window\": 26}')",
              (earlier.date().isoformat(),))
    ps.set_manual(c, "KXNATGASW", {"fut_vol_window": 40}, note="x")
    got = ps.params_asof(c, "KXNATGASW", earlier + timedelta(hours=1))
    assert got == {"vol_window": 26}, \
        "a pre-adoption replay must see that day's row, not today's adoption"


def test_empty_history_returns_defaults_not_error():
    c = _conn()
    assert ps.params_asof(c, "KXWTIW", datetime.now(timezone.utc)) == {}
