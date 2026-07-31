"""tests/test_step7_micro_conformal.py — microstructure + ACI conformal units."""
from __future__ import annotations

import numpy as np
import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model.ensemble import fl_correct_pmf, median_spread
from prediction_market_macro.strategy.conformal import (aci_update,
                                                        evaluate_sequence,
                                                        sizing_factor)


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


def test_median_spread():
    legs = [{"yes_bid": 0.40, "yes_ask": 0.44}, {"yes_bid": 0.10, "yes_ask": 0.30},
            {"yes_bid": None, "yes_ask": 0.5}]
    assert abs(median_spread(legs) - 0.20) < 1e-9 or abs(median_spread(legs) - 0.04) < 1e-9
    assert median_spread([{"yes_bid": None, "yes_ask": None}]) is None


def test_fl_correct_pmf_shrinks_longshots():
    pmf = {1.0: 0.02, 2.0: 0.48, 3.0: 0.48, 4.0: 0.02}
    shrink = lambda p: 0.5 + 0.9 * (p - 0.5)          # linear shrink toward 0.5
    out = fl_correct_pmf(pmf, shrink)
    assert abs(sum(out.values()) - 1.0) < 1e-9
    assert all(v >= 0 for v in out.values())
    assert out[1.0] > pmf[1.0]                        # tail mass pulled UP toward 0.5


def test_fl_identity_map_is_noop():
    pmf = {1.0: 0.25, 2.0: 0.5, 3.0: 0.25}
    out = fl_correct_pmf(pmf, lambda p: p)
    for k in pmf:
        assert abs(out[k] - pmf[k]) < 1e-9


def test_aci_update_moves_alpha():
    a1 = aci_update(0.10, breached=True)              # breach → alpha shrinks
    assert a1 < 0.10
    a2 = aci_update(0.10, breached=False)             # quiet → alpha grows
    assert a2 > 0.10


def test_evaluate_sequence_flags_regime_break():
    rng = np.random.RandomState(0)
    calm = list(0.05 + 0.01 * rng.rand(30))
    st = evaluate_sequence(calm + [0.30])              # sudden 6x error
    assert st["latest_breached"] is True and st["factor"] == 0.5
    st2 = evaluate_sequence(calm + [0.05])
    assert st2["latest_breached"] is False and st2["factor"] == 1.0


def test_sizing_factor_default(conn):
    assert sizing_factor(conn, "KXCPI") == 1.0
