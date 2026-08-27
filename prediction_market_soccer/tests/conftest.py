"""Suite-wide guards for the soccer tests.

``model.strength_cache.cached_strength`` is a LOAD-OR-FIT-AND-SAVE helper: every
export that calls it persists ``data/output/ratings_<comp>.json`` and the live
loop loads that file back for up to two hours. A test drives those exports over
an in-memory store, so an unguarded run would leave a model fitted from an EMPTY
database sitting in the production output directory — and, whenever the real
store happens to agree on the settled-fixture count, the live loop would price
with it. Tests get the same per-league fit, in memory, written nowhere.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_ratings_cache_io(monkeypatch):
    from prediction_market_soccer.model import strength_cache
    from prediction_market_soccer.tests import clubctx

    def _in_memory_fit(conn, comp_key, *, xg_form: bool = True, max_age_s: float = 0.0):
        return clubctx.strength_for(comp_key)

    monkeypatch.setattr(strength_cache, "cached_strength", _in_memory_fit)
