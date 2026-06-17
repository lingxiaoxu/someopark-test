"""Tests for the alternative-data λ adjustments (plan 19): opponent-adjusted form /
xGA, venue climate, the bounded parameter-controlled application, and — critically —
that the live model is BYTE-IDENTICAL at the default (all weights 0)."""
from __future__ import annotations

import sqlite3
from dataclasses import replace

from prediction_market.config import CONFIG
from prediction_market.ingest import store
from prediction_market.model.venue_climate import venue_index, venue_log_suppression


def _mem_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init_db(c)
    return c


# ── venue climate (static, deterministic) ─────────────────────────────────────
def test_venue_index_altitude_dominates():
    assert venue_index("Estadio Azteca") > venue_index("Lumen Field")     # altitude >> mild
    assert venue_index("Estadio Azteca") > 0.6
    assert venue_index("Lumen Field") < 0.3
    assert venue_index(None) == 0.0 and venue_index("Unknown Park") == 0.0


def test_venue_controlled_roof_neutralises_heat():
    # AT&T (Dallas) brutal heat but closed roof + AC → only its modest altitude counts,
    # so it ranks BELOW an open-air very-hot venue (Monterrey).
    assert venue_index("AT&T Stadium") < venue_index("Estadio BBVA")


def test_venue_log_suppression_weight_gated():
    assert venue_log_suppression("Estadio Azteca", 0.0) == 0.0          # weight 0 → off
    assert venue_log_suppression("Estadio Azteca", 1.0) > 0.0
    assert venue_log_suppression("Lumen Field", 1.0) < venue_log_suppression("Estadio Azteca", 1.0)


# ── alt-data index (opponent-adjusted form + xGA) ─────────────────────────────
def _seed_nt(c):
    c.execute("INSERT INTO team(api_id,name) VALUES (10,'A'),(11,'B'),(13,'C'),(12,'Strong')")
    c.execute("INSERT INTO team_meta(api_id,canonical_team_id) VALUES (10,'a'),(11,'b'),(13,'cc'),(12,'strong')")
    # A held a strong opponent to 0 (resilient); B shipped 3; C a neutral 1-1.
    c.execute("INSERT INTO nt_recent(fixture_api_id,team_api_id,opp_api_id,kickoff_ts,is_friendly,gf,ga,is_home) VALUES "
              "(1,10,12,'2026-06-01T00:00:00+00:00',0,0,0,1),"
              "(2,11,12,'2026-06-01T00:00:00+00:00',0,1,3,1),"
              "(4,13,12,'2026-06-01T00:00:00+00:00',0,1,1,1),"
              "(3,10,12,'2026-06-20T00:00:00+00:00',0,0,5,1)")   # a LATE match for A (post-cutoff)
    c.commit()


def test_altdata_index_defensive_resilience_and_pit():
    from prediction_market.model.altdata_adjust import altdata_index
    c = _mem_db(); _seed_nt(c)
    ratings = {"a": 0.0, "b": 0.0, "cc": 0.0, "strong": 2.0}
    # PIT: only matches before 06-10 → A's resilient clean sheet counts, its later 0-5 does NOT.
    idx = altdata_index(c, ratings, as_of="2026-06-10T00:00:00+00:00")
    assert idx["a"].def_z > idx["b"].def_z          # A more defensively resilient than B
    # without the cutoff, A's later 0-5 drags its raw defence down → lower z vs the PIT view.
    idx_all = altdata_index(c, ratings)
    assert idx_all["a"].def_z < idx["a"].def_z       # leak-free cutoff really excludes the late game


# ── the crux: ZERO weights are a byte-identical no-op (the safety mechanism) ───
def test_zero_weights_are_a_noop():
    from prediction_market.model.strength import StrengthModel
    from prediction_market.model.altdata_adjust import TeamAdj
    # Explicitly-zero alt-data weights → adj is ignored, lambdas unchanged (the property
    # that guarantees prod is unaffected whenever a signal's weight is left at 0).
    cfg = replace(CONFIG.model, oppadj_def_weight=0.0, oppadj_off_weight=0.0, xga_weight=0.0)
    sm = StrengthModel(ratings={"x": 0.3, "y": -0.2}, sigma={}, host_ids=frozenset(), cfg=cfg,
                       adj={"x": TeamAdj(def_z=1.5, off_z=1.5, xga_z=1.5), "y": TeamAdj(def_z=-1.0)})
    sm0 = replace(sm, adj=None)
    assert sm.pair_lambdas("x", "y") == sm0.pair_lambdas("x", "y")   # adj ignored at weight 0


def test_unfit_signals_still_off_by_default():
    # xGA / venue-climate / lineup are scaffolded but NOT yet enabled (data thin / not
    # ingested) — they must stay 0 until validated, so prod can't be moved by them.
    cfg = CONFIG.model
    assert cfg.xga_weight == 0.0 and cfg.venue_climate_weight == 0.0 and cfg.lineup_weight == 0.0


def test_adj_applies_and_is_clipped_when_enabled():
    import math
    from prediction_market.model.strength import StrengthModel
    from prediction_market.model.altdata_adjust import TeamAdj
    cfg = replace(CONFIG.model, oppadj_def_weight=0.20, oppadj_off_weight=0.0, adj_log_clip=0.10)
    adj = {"x": TeamAdj(), "y": TeamAdj(def_z=5.0)}   # y hugely resilient → suppresses x's λ
    sm = StrengthModel(ratings={"x": 0.3, "y": -0.2}, sigma={}, host_ids=frozenset(), cfg=cfg, adj=adj)
    lx, ly = sm.pair_lambdas("x", "y")
    base = StrengthModel(ratings=sm.ratings, sigma={}, host_ids=frozenset(), cfg=cfg, adj=None).pair_lambdas("x", "y")
    assert lx < base[0]                                  # x's λ suppressed by y's defence
    # clip caps it: 0.20*5.0=1.0 would be huge, but clip=0.10 → factor ≥ exp(-0.10)
    assert lx >= base[0] * math.exp(-0.10) - 1e-9


def test_walk_forward_runs():
    # Smoke: the honest PIT walk-forward returns baseline + candidates without error.
    from prediction_market.ops import param_sweep
    c = _mem_db()
    wf = param_sweep.walk_forward(conn=c, start=0)
    assert "candidates" in wf and any(x["label"] == "baseline" for x in wf["candidates"])
