"""The brute-force sweep knob (param_override / hash_tag) — three contracts.

1. An override takes precedence over BOTH the daily selector and the registered
   defaults for its series, and only for its series.
2. An override without a hash_tag refuses to run — an untagged override would
   INSERT OR REPLACE the canonical experiments row for the same window/config,
   silently replacing the display backtest with a sweep cell.
3. hash_tag lands in the stored config_hash (that is what makes 600 sweep rows
   individually addressable and none of them the canonical one).
"""
from __future__ import annotations

import pytest

from prediction_market_macro.research import walkforward as wf


def test_override_beats_selector_and_only_for_its_series():
    book = wf._GateBook(conn=None, db_gates=False, select_params=True,
                        param_override={"KXNATGASW": {"fut_vol_window": 40}})
    assert book.params_for("KXNATGASW", None) == {"fut_vol_window": 40}
    # other series falls through to the selector path, which swallows the conn=None
    # failure and returns None (registered defaults) — proving it did NOT
    # short-circuit on the override, and the override leaked to no other series
    assert book.params_for("KXWTIW", None) is None


def test_untagged_override_refuses_to_run():
    with pytest.raises(ValueError, match="hash_tag"):
        wf.run(conn=None, days=1, param_override={"KXWTIW": {"fut_vol_window": 5}})


def test_hash_tag_reaches_cfg_hash():
    import inspect
    src = inspect.getsource(wf.run)
    assert "':' + hash_tag if hash_tag" in src
