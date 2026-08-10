"""The grid builder must never spend width it cannot use, and never do so quietly.

Both halves matter. A grid that includes a dead parameter looks 8x wider than it is
(claims.py, see test_claims_params.py). A grid that silently trims itself to fit reads
as "we searched everything" when it did not. So `build_grid` reports dead keys and
size-dropped keys separately, and these pin that it keeps doing so.
"""
from __future__ import annotations

import math
from datetime import timedelta

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model import claims
from prediction_market_macro.research.param_space import (CANDIDATES, MIN_EVENTS,
                                                          build_grid, live_keys,
                                                          max_sets)
from prediction_market_macro.tests.test_m1_claims import _seed_claims


@pytest.fixture()
def seeded(tmp_path):
    conn = init_db(tmp_path / "s.db")
    end = _seed_claims(conn)
    asof = end + timedelta(days=1)
    return conn, [(asof, (end + timedelta(days=6)).date().isoformat())]


def _predict(conn, asof, period, series, params=None):
    return claims.predict(conn, asof, period, series, params=params)


def test_thin_history_gets_no_search_at_all(seeded):
    """The failure this guards is a grid quietly doing table lookup: 200 candidates
    against 2 observations will always 'find' something."""
    conn, evs = seeded
    assert max_sets(0) == 1 and max_sets(MIN_EVENTS - 1) == 1
    grid, rep = build_grid(conn, "KXJOBLESSCLAIMS", "claims", _predict, n_events=2)
    assert grid == [{}] and rep["n_sets"] == 1
    assert "defaults only" in rep["reason"]


def test_width_grows_with_history_not_with_appetite(seeded):
    """max_sets inverts bias ~ sqrt(2 ln K / n). Pin the shape, not one magic number:
    monotone in n, and the returned K actually satisfies the bound it claims."""
    assert max_sets(20) < max_sets(50) < max_sets(150)
    for n in (20, 50, 150, 400):
        k = max_sets(n)
        if k < 512:
            assert math.sqrt(2 * math.log(k) / n) <= 0.35 + 1e-9


def test_dead_params_are_reported_not_silently_dropped(seeded):
    """A key that moves nothing must land in dead_keys, and must not reach the product."""
    conn, evs = seeded
    probes = {"vol_window": 8, "not_a_real_param": 999}
    live, dead = live_keys(conn, "KXJOBLESSCLAIMS", _predict, probes, evs)
    assert live == ["vol_window"]
    assert dead == ["not_a_real_param"]


def test_every_default_can_win_its_own_grid():
    """If a parameter's candidate list omits today's value, selection can only ever move
    AWAY from the production model — the incumbent would not be on the ballot."""
    from prediction_market_macro.model import (cpi, energy, fed, payrolls, pce, u3)
    mods = {"claims": claims.DEFAULT_PARAMS, "cpi": cpi.DEFAULT_PARAMS,
            "pce": pce.DEFAULT_PARAMS, "payrolls": payrolls.DEFAULT_PARAMS,
            "u3": u3.DEFAULT_PARAMS, "fed": fed.DEFAULT_PARAMS,
            "energy": energy.DEFAULT_PARAMS}
    for mod, defaults in mods.items():
        for key, (_probe, values) in CANDIDATES.get(mod, {}).items():
            assert key in defaults, f"{mod}.{key} is not a real parameter"
            cur = defaults[key]
            cur = tuple(cur) if isinstance(cur, (list, tuple)) else cur
            vals = [tuple(v) if isinstance(v, (list, tuple)) else v for v in values]
            assert cur in vals, f"{mod}.{key}: default {cur!r} missing from {vals!r}"


def test_probe_values_are_not_themselves_defaults():
    """A probe equal to the default proves nothing — the liveness check would pass every
    key by comparing the model against itself."""
    from prediction_market_macro.model import (cpi, energy, fed, payrolls, pce, u3)
    mods = {"claims": claims.DEFAULT_PARAMS, "cpi": cpi.DEFAULT_PARAMS,
            "pce": pce.DEFAULT_PARAMS, "payrolls": payrolls.DEFAULT_PARAMS,
            "u3": u3.DEFAULT_PARAMS, "fed": fed.DEFAULT_PARAMS,
            "energy": energy.DEFAULT_PARAMS}
    for mod, defaults in mods.items():
        for key, (probe, _values) in CANDIDATES.get(mod, {}).items():
            assert probe != defaults[key], f"{mod}.{key}: probe equals the default"
