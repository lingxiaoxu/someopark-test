"""manual_params: the user-adoption override's three contracts.

1. An active row wins over the selector path, verbatim, flagged mode='manual'.
2. PIT: a `before` earlier than the adoption instant does NOT see the override —
   walkforward replays of pre-adoption days keep the behaviour they had.
3. clear_manual deactivates without deleting (the audit trail stays in experiments).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from prediction_market_macro.research import param_select as ps


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE experiments(name TEXT, config_hash TEXT, series TEXT,
                 window TEXT, metrics_json TEXT, created_ts TEXT,
                 PRIMARY KEY(name, config_hash))""")
    return c


def test_active_row_wins_and_is_flagged_manual():
    c = _conn()
    ps.set_manual(c, "KXNATGASW", {"fut_vol_window": 40, "fut_pool_bars": 750},
                  note="user adoption 2026-08-11 (75d sweep argmin)")
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    params, rep = ps.select_for(c, "KXNATGASW", later)
    assert params == {"fut_vol_window": 40, "fut_pool_bars": 750}
    assert rep["mode"] == "manual" and rep["adopted"] is True


def test_pit_a_replay_before_adoption_does_not_see_it():
    c = _conn()
    ps.set_manual(c, "KXNATGASW", {"fut_vol_window": 40}, note="x")
    earlier = datetime.now(timezone.utc) - timedelta(days=30)
    assert ps.manual_params(c, "KXNATGASW", earlier) is None


def test_clear_deactivates_and_history_keeps_both_rows():
    c = _conn()
    ps.set_manual(c, "KXWTIW", {"fut_vol_window": 20}, note="x")
    ps.clear_manual(c, "KXWTIW")
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert ps.manual_params(c, "KXWTIW", later) is None
    # history-preserving semantics (2026-08-11): every write is its own row,
    # so the change log holds the adoption AND the clear
    h = ps.history(c, "KXWTIW")
    assert len(h) == 2
    assert h[0]["active"] is True and h[1]["active"] is False
