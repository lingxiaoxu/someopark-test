"""The calibration live-path pin (2026-08-10 incident).

What happened: weekly isotonic refits on per-leg pairs (clustered by event) produced
step maps ending in exact 0.0/1.0 plateaus; KXNATGASW mapped [0.27, 0.55] -> 0.60 and
decision #4346 bought a raw-fair-0.334 leg at 0.36 as if fair were 0.60. The remedy is
discipline, not a cleverer fit: live maps are pinned to identity, fitted maps go to
the 'calibration_map_shadow' row, and un-pinning needs a preregistered criterion.

These tests pin the two halves of that remedy:
  1. eval's refit block writes identity to the LIVE row even when the fit produces a
     map — asserted at the store level (store_map(None)) plus source-text guard, so a
     future edit that quietly restores `store_map(conn, series, fit_map(pairs))`
     fails a test instead of re-arming Kelly.
  2. After store_map(None), apply() is identity even though a poisoned map row was
     stored earlier — REPLACE semantics plus cache invalidation both hold.
"""
from __future__ import annotations

import inspect
import sqlite3

from prediction_market_macro.strategy import calibration


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE experiments(name TEXT, config_hash TEXT, series TEXT,
                 window TEXT, metrics_json TEXT, created_ts TEXT,
                 PRIMARY KEY(name, config_hash))""")
    return c


POISON = {"x": [0.0, 0.2724, 0.2747, 0.54575, 0.7404, 1.0],
          "y": [0.0, 0.0, 0.6, 0.6, 1.0, 1.0], "n_pairs": 480}


def test_store_none_pins_identity_over_a_poisoned_map():
    c = _conn()
    calibration.store_map(c, "KXNATGASW", POISON)
    assert abs(calibration.apply(c, "KXNATGASW", 0.334) - 0.6) < 1e-9   # poison live
    calibration.store_map(c, "KXNATGASW", None)                          # the pin
    assert calibration.apply(c, "KXNATGASW", 0.334) == 0.334
    assert calibration.apply(c, "KXNATGASW", 0.9) == 0.9


def test_shadow_row_never_reaches_apply():
    c = _conn()
    calibration.store_named_map(c, "KXWTIW", "calibration_map_shadow", POISON)
    assert calibration.apply(c, "KXWTIW", 0.7) == 0.7    # shadow is research-only


def test_eval_source_keeps_the_live_map_pinned():
    """Text-level guard on the one line that would re-arm the live path. Crude on
    purpose: run_series needs a full replay harness, but the failure mode this
    prevents is a one-line revert, which text inspection catches exactly."""
    from prediction_market_macro.research import eval as eval_mod
    src = inspect.getsource(eval_mod)
    assert "store_named_map(conn, series, \"calibration_map_shadow\"" in src
    assert "store_map(conn, series, None)" in src
    assert "store_map(conn, series, fit_map(pairs))" not in src
