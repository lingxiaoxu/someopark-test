"""select_mode='argmin' — the PIT contracts of the replayed 2026-08-11 selector.

1. Day-D selection sees only events that closed STRICTLY before D — an event closing
   at D must not vote (its settle is not known at D's decision time).
2. The trailing window bounds the mask from below too (WINDOW_DAYS).
3. When the default column wins, params_for returns None (registered defaults), and
   with no history it returns None — never an arbitrary set.
4. Guard rails: unknown select_mode / argmin without select_params refuse to run;
   ':argminsel' lands in cfg_hash source so the run cannot masquerade as a DSR run.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.research import walkforward as wf

D = datetime(2026, 8, 1, 16, 0, tzinfo=timezone.utc)


def _book(kept_closes, cols):
    """A book with an injected matrix: cols[j] is column j's per-event pnl."""
    b = wf._GateBook(conn=None, db_gates=False, select_params=True,
                     select_mode="argmin", argmin_start=D - timedelta(days=75),
                     argmin_end=D + timedelta(days=30))
    grid = [{}] + [{"p": j} for j in range(1, len(cols))]
    kept = [{"close": c} for c in kept_closes]
    mat = [[cols[j][i] for j in range(len(cols))] for i in range(len(kept_closes))]
    b._amx["S"] = (grid, kept, mat)
    return b


def test_day_selection_is_strictly_before_and_windowed():
    closes = [D - timedelta(days=80), D - timedelta(days=10), D]
    # col1 wins only on the day-80 event (outside window) and the day-0 event
    # (not yet closed at D) — col0 (default) wins on the only visible event
    cols = [[0.0, 1.0, 0.0], [5.0, 0.0, 5.0]]
    b = _book(closes, cols)
    assert b._argmin_params("S", D) is None            # default won the masked window
    # one day later the D-close event is visible and flips the argmin
    assert b._argmin_params("S", D + timedelta(days=1)) == {"p": 1}


def test_no_history_returns_defaults():
    b = _book([D + timedelta(days=5)], [[0.0], [9.9]])
    assert b._argmin_params("S", D) is None


def test_guard_rails():
    with pytest.raises(ValueError, match="select_mode"):
        wf.run(conn=None, days=1, select_mode="bogus")
    with pytest.raises(ValueError, match="select_params"):
        wf.run(conn=None, days=1, select_mode="argmin", select_params=False)
    assert "':argminsel' if select_mode == 'argmin'" in inspect.getsource(wf.run)
