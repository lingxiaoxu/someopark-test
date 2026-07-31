"""tests/test_tick_window.py — event-window densification units (§8.1)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.jobs.tick import _active_windows, snap_interval


def test_snap_interval_bands():
    assert snap_interval(-3 * 3600) is None          # 3h before: outside window
    assert snap_interval(-7000) == 300               # ~2h before: 5-min
    assert snap_interval(-300) == 60                 # 5 min before: 1-min
    assert snap_interval(120) == 60                  # 2 min after: 1-min (reassess)
    assert snap_interval(1200) == 300                # 20 min after: 5-min tail
    assert snap_interval(4000) is None               # >30 min after: closed


def test_active_windows(tmp_path):
    conn = init_db(tmp_path / "t.db")
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    conn.execute("INSERT INTO releases(cal, period, scheduled_ts) VALUES(?,?,?)",
                 ("BLS_CPI", "2026-06", (now + timedelta(hours=1)).isoformat()))
    conn.execute("INSERT INTO releases(cal, period, scheduled_ts) VALUES(?,?,?)",
                 ("DOL_CLAIMS", "2026-07-16", (now + timedelta(days=2)).isoformat()))
    conn.commit()
    wins = _active_windows(conn, now)
    series = {w[0] for w in wins}
    # CPI calendar covers the four KXCPI* series; claims is outside the window
    assert "KXCPI" in series and "KXCPIYOY" in series
    assert "KXJOBLESSCLAIMS" not in series
