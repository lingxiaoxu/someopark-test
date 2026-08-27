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
4. (#198, 2026-08-27) With SEVERAL adoptions, the one in force at `asof` — not the
   newest one, and not the defaults just because a newer one exists.

Contract 4 is the one that was broken for two weeks. Contracts 1-3 all hold with a
single adoption row, which is all there was on 2026-08-12; by 08-27 the argmin lane
had written 9 rows for KXJOBLESSCLAIMS and 5 for KXCPIYOY, and every asof before the
newest of them silently read the defaults.
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


def _adopt(c, series, params, ts):
    """`set_manual` stamps wall-clock now, so the multi-adoption timeline is written
    directly — the point of these tests is the ORDER of rows, not the writer."""
    import json
    c.execute(
        "INSERT INTO experiments(name, config_hash, series, window, metrics_json,"
        " created_ts) VALUES('manual_params',?,?,'live',?,?)",
        (f"manual:{series}:{ts}", series,
         json.dumps({"active": params is not None, "params": params or {},
                     "note": "test"}), ts))
    c.commit()


def test_a_later_adoption_does_not_erase_an_earlier_one():
    """The #198 bug. Three adoptions; an asof between the first and the second must see
    the FIRST. The old reader took the newest row, found `asof < created_ts`, and
    returned None — so the whole pre-newest history graded at the defaults, which is
    the very error `params_asof` exists to prevent, one layer further down.
    """
    c = _conn()
    _adopt(c, "KXCPIYOY", {"w_last": 0.3}, "2026-08-11T04:44:41+00:00")
    _adopt(c, "KXCPIYOY", {"w_last": 0.4}, "2026-08-13T09:12:02+00:00")
    _adopt(c, "KXCPIYOY", {"w_last": 0.5}, "2026-08-25T09:16:59+00:00")

    at = lambda d: ps.params_asof(c, "KXCPIYOY",                       # noqa: E731
                                 datetime.fromisoformat(d + "+00:00"))
    assert at("2026-08-10T12:00:00") == {}, "before the first adoption: defaults"
    assert at("2026-08-12T12:00:00") == {"w_last": 0.3}
    assert at("2026-08-20T12:00:00") == {"w_last": 0.4}
    assert at("2026-08-26T12:00:00") == {"w_last": 0.5}


def test_a_clear_is_an_adoption_of_the_defaults_and_is_also_pit():
    """`clear_manual` writes `active: False`. It must switch the series back to the
    defaults from its own instant forward, and must NOT reach backwards and un-adopt
    the days that really did run the override — KXPAYROLLS has exactly this shape
    (adopt 08-11, clear 08-21, adopt again 08-22)."""
    c = _conn()
    _adopt(c, "KXPAYROLLS", {"w_base": 0.6}, "2026-08-11T04:44:41+00:00")
    _adopt(c, "KXPAYROLLS", None, "2026-08-21T01:49:49+00:00")          # clear
    _adopt(c, "KXPAYROLLS", {"w_base": 0.7}, "2026-08-22T09:13:21+00:00")

    at = lambda d: ps.params_asof(c, "KXPAYROLLS",                     # noqa: E731
                                 datetime.fromisoformat(d + "+00:00"))
    assert at("2026-08-15T12:00:00") == {"w_base": 0.6}
    assert at("2026-08-21T12:00:00") == {}, "the clear applies from its own instant"
    assert at("2026-08-23T12:00:00") == {"w_base": 0.7}


def test_wall_clock_reads_are_unchanged_by_the_pit_fix():
    """`current()`, `select_for(now)` and `frontend_export` all pass `before = now`, and
    the newest row at-or-before now IS the newest row. The fix must therefore be
    invisible to production's own read — if this ever fails, a live prediction changed
    on the back of a backtest correction."""
    c = _conn()
    _adopt(c, "KXWTIW", {"fut_pool_bars": 750}, "2026-08-11T04:44:41+00:00")
    _adopt(c, "KXWTIW", {"fut_pool_bars": 375}, "2026-08-15T09:09:50+00:00")
    now = datetime.now(timezone.utc)
    assert ps.current(c, "KXWTIW") == {"fut_pool_bars": 375}
    assert ps.params_asof(c, "KXWTIW", now) == {"fut_pool_bars": 375}
    assert ps.select_for(c, "KXWTIW", now)[0] == {"fut_pool_bars": 375}
