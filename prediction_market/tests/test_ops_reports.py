"""Tests for the performance/P&L report and risk report (plan 03 §9, 04 §6, 05 §5)."""
from __future__ import annotations

import sqlite3

from prediction_market.ingest import store
from prediction_market.ops import (
    backtest_export,
    frontend_export,
    inplay_export,
    performance_report,
    risk_report,
    upcoming_export,
)


def _mem_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init_db(c)
    return c


# ── performance / P&L ─────────────────────────────────────────────────────────
def test_performance_report_no_settled_matches():
    c = _mem_db()
    rep = performance_report.build(conn=c)
    assert rep.n_settled == 0
    assert rep.settled_signal_pnl == 0.0
    assert any("no settled" in n for n in rep.notes)


def test_performance_report_with_settled_match():
    c = _mem_db()
    for api, cid in ((10, "france"), (20, "senegal")):
        store.upsert(c, "team_meta",
                     {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "FT",
                 "home_api_id": 10, "away_api_id": 20, "home_goals": 2, "away_goals": 0,
                 "updated_at": store.utcnow()}, pk=["api_id"])
    rep = performance_report.build(conn=c)
    assert rep.n_settled == 1
    assert rep.brier_uniform == round(2 / 3, 4)          # uniform baseline reference
    assert 0.0 <= rep.brier <= 2.0
    assert rep.n_settled_signals == 0


# ── risk ──────────────────────────────────────────────────────────────────────
def test_risk_report_gates_and_caps():
    c = _mem_db()
    rep = risk_report.build(conn=c)
    # Hard $1 cap always present + reported.
    assert rep.gates["hard_order_cap_usd"] == 1.0
    assert any("hard-capped at $1.00" in b for b in rep.blocked_summary)
    # No fills recorded → zero exposure.
    assert rep.exposure["open_positions"] == 0
    assert rep.exposure["total_at_risk_usd"] == 0.0
    # Prod balance never queried (standing rule).
    assert "not queried" in str(rep.venue_balances["kalshi_prod_usd"])
    # API budget present and within the monthly cap.
    assert 0 <= rep.api_budget["used"] <= rep.api_budget["cap"]


# ── PDF rendering (house style) ───────────────────────────────────────────────
def test_performance_pdf_renders(tmp_path):
    c = _mem_db()
    for api, cid in ((10, "france"), (20, "senegal")):
        store.upsert(c, "team_meta",
                     {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "FT",
                 "home_api_id": 10, "away_api_id": 20, "home_goals": 2, "away_goals": 0,
                 "updated_at": store.utcnow()}, pk=["api_id"])
    rep = performance_report.build(conn=c)
    out = tmp_path / "perf.pdf"
    performance_report.build_pdf(rep, str(out), as_of="2026-06-16")
    assert out.exists() and out.read_bytes()[:4] == b"%PDF"


def test_risk_pdf_renders(tmp_path):
    c = _mem_db()
    rep = risk_report.build(conn=c)
    out = tmp_path / "risk.pdf"
    risk_report.build_pdf(rep, str(out), as_of="2026-06-16")
    assert out.exists() and out.read_bytes()[:4] == b"%PDF"


# ── frontend data contract ────────────────────────────────────────────────────
def test_frontend_export_contract():
    c = _mem_db()
    doc = frontend_export.build(conn=c, as_of="2026-06-16")
    # Every section the UI relies on must be present.
    for key in ("schema_version", "headline", "interfaces", "modes", "schedule",
                "inputs", "outputs", "value", "performance", "risk", "predictions"):
        assert key in doc, f"missing {key}"
    assert doc["interfaces"] and "command" in doc["interfaces"][0]
    assert "gates" in doc["risk"] and "n_settled" in doc["performance"]
    assert "upcoming" in doc["predictions"]


# ── upcoming cross-venue export ───────────────────────────────────────────────
def test_upcoming_venue_devig():
    q = {"home": {"ask": 0.07, "bid": 0.06}, "draw": {"ask": 0.13, "bid": 0.12},
         "away": {"ask": 0.81, "bid": 0.80}}
    p = upcoming_export._venue_devig(q)
    assert abs(p["home"] + p["draw"] + p["away"] - 1.0) < 1e-6   # de-vigged → sums to 1
    assert upcoming_export._venue_devig(None) is None
    assert upcoming_export._venue_devig({"home": {"ask": None}}) is None


def test_upcoming_best_buy_edge_and_lock():
    model = {"home": 0.55, "draw": 0.24, "away": 0.21}
    # Venue cheap on home → model 0.55 vs ask 0.30 = big buy edge.
    vq = {"home": {"ask": 0.30, "bid": 0.29}, "draw": {"ask": 0.25, "bid": 0.24},
          "away": {"ask": 0.50, "bid": 0.49}}
    be = upcoming_export._best_buy_edge(model, vq, "kalshi", theta=0.03)
    assert be["side"] == "home" and be["tradable"] is True
    # Lock arb: buy home cheap on kalshi (0.30) + sell home high bid on poly (0.41) → locks.
    kq = {"home": {"ask": 0.30, "bid": 0.29}}
    pq = {"home": {"ask": 0.42, "bid": 0.41}}
    lock = upcoming_export._lock_arb(kq, pq)
    assert lock["buy_venue"] == "kalshi" and lock["sell_venue"] == "poly_us"
    assert lock["net_lock"] > 0 and lock["tradable"] is True


def test_upcoming_build_db_only():
    c = _mem_db()
    for api, cid in ((10, "france"), (20, "senegal")):
        store.upsert(c, "team_meta",
                     {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "NS",
                 "home_api_id": 10, "away_api_id": 20, "kickoff_ts": "2026-06-17T19:00:00+00:00",
                 "round": "Group Stage - 1", "updated_at": store.utcnow()}, pk=["api_id"])
    rows = upcoming_export.build(limit=5, conn=c, with_venues=False)
    assert len(rows) == 1
    m = rows[0]
    assert m["home"]["id"] == "france" and m["away"]["id"] == "senegal"
    assert m["et_date"] == "2026-06-17"
    assert abs(m["model"]["home"] + m["model"]["draw"] + m["model"]["away"] - 1.0) < 0.02
    assert m["kalshi"] is None and m["poly_us"] is None   # venues skipped → genuinely None


# ── live in-play export ───────────────────────────────────────────────────────
def test_inplay_export_no_live():
    c = _mem_db()
    doc = inplay_export.build(conn=c, with_venues=False)
    assert doc["n_live"] == 0 and doc["matches"] == []


def test_inplay_export_live_match():
    c = _mem_db()
    for api, cid in ((10, "france"), (20, "senegal")):
        store.upsert(c, "team_meta",
                     {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "2H",
                 "home_api_id": 10, "away_api_id": 20, "home_goals": 0, "away_goals": 0, "elapsed": 78,
                 "updated_at": store.utcnow()}, pk=["api_id"])
    for tid, xgv in ((10, 1.9), (20, 0.3)):
        store.upsert(c, "fixture_stats", {"fixture_api_id": 1, "team_api_id": tid, "xg": xgv,
                     "fetched_at": store.utcnow()}, pk=["fixture_api_id", "team_api_id"])
    doc = inplay_export.build(conn=c, with_venues=False)
    assert doc["n_live"] == 1
    m = doc["matches"][0]
    assert m["score"] == "0-0" and m["minute"] == 78
    # state-dependent model: at 78' 0-0 the draw should dominate the live 3-way
    assert m["model"]["draw"] > m["model"]["home"] and m["model"]["draw"] > m["model"]["away"]
    assert abs(m["model"]["home"] + m["model"]["draw"] + m["model"]["away"] - 1.0) < 0.02
    # xG-momentum trick should flag France (xG 1.9 vs 0.3, still 0-0) as a signal
    assert any(o["kind"] == "tactic" for o in m["opportunities"])


# ── OOS backtest export ───────────────────────────────────────────────────────
def test_backtest_export_with_settled():
    c = _mem_db()
    for api, cid in ((10, "france"), (20, "senegal")):
        store.upsert(c, "team_meta",
                     {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "FT",
                 "home_api_id": 10, "away_api_id": 20, "home_goals": 2, "away_goals": 0,
                 "updated_at": store.utcnow()}, pk=["api_id"])
    doc = backtest_export.build(conn=c)
    assert doc["n_settled"] == 1
    assert doc["brier"]["uniform"] == round(2 / 3, 4)
    assert doc["brier"]["model"] is not None
    assert doc["matches"][0]["result"] == "H"          # France 2-0 → home win
    assert "conclusion" in doc


# ── walk-forward Elo result-update ────────────────────────────────────────────
def test_walkforward_update_moves_prediction():
    """Proves the result-update (Elo) genuinely shifts ratings + future predictions
    once a team has a prior game — i.e. #1 is implemented, not just inert."""
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.match_pricing import price_match
    from prediction_market.model.strength import build_strength, update_with_results
    prior = load_prior()
    base = build_strength(prior)
    a, b, c = "france", "senegal", "iraq"
    p_before = price_match(base, a, c).p_home
    # France thrashes Senegal 5-0 → France over-performed
    updated = update_with_results(
        base, [{"home_id": a, "away_id": b, "home_goals": 5, "away_goals": 0, "days_ago": 1}], lr=0.2)
    p_after = price_match(updated, a, c).p_home
    assert updated.ratings[a] > base.ratings[a]   # rating rises on over-performance
    assert p_after > p_before                       # → higher win prob in the next game


def test_walkforward_eval_no_repeat_teams():
    """On a first-games-only sample the walk-forward equals baseline (inert)."""
    from prediction_market.ops import walkforward_eval
    c = _mem_db()
    for api, cid in ((10, "france"), (20, "senegal"), (30, "brazil"), (40, "germany")):
        store.upsert(c, "team_meta",
                     {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    # two matches, four DISTINCT teams → no prior data for anyone
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "FT",
                 "home_api_id": 10, "away_api_id": 20, "home_goals": 1, "away_goals": 0,
                 "kickoff_ts": "2026-06-11T19:00:00+00:00", "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 2, "league_id": 1, "season": 2026, "status_short": "FT",
                 "home_api_id": 30, "away_api_id": 40, "home_goals": 2, "away_goals": 2,
                 "kickoff_ts": "2026-06-12T19:00:00+00:00", "updated_at": store.utcnow()}, pk=["api_id"])
    doc = walkforward_eval.run(conn=c)
    assert doc["n_matches_with_prior_data"] == 0
    assert doc["improves"] is False


# ── squad strength ────────────────────────────────────────────────────────────
def test_squad_index_and_export():
    from prediction_market.model.squad_strength import squad_index, squad_adjusted_ratings
    from prediction_market.model.strength import build_strength
    from prediction_market.ingest.prior_ingest import load_prior
    c = _mem_db()
    # two NTs, each one player with club stats
    for api, cid in ((10, "france"), (20, "senegal")):
        store.upsert(c, "team_meta",
                     {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    for pid, team, rating, goals, mins in ((1, 10, 7.6, 20, 3000), (2, 20, 6.8, 3, 2000)):
        store.upsert(c, "player", {"api_id": pid, "name": f"P{pid}", "updated_at": store.utcnow()}, pk=["api_id"])
        store.upsert(c, "squad", {"team_api_id": team, "player_api_id": pid, "season": 2026,
                     "updated_at": store.utcnow()}, pk=["team_api_id", "player_api_id", "season"])
        store.upsert(c, "player_stat", {"player_api_id": pid, "league_id": 1, "season": 2025,
                     "team_api_id": team, "rating": rating, "goals": goals, "assists": 0, "minutes": mins,
                     "updated_at": store.utcnow()}, pk=["player_api_id", "league_id", "season"])
    idx = squad_index(c)
    assert idx["france"].mw_rating > idx["senegal"].mw_rating      # higher-rated squad
    assert idx["france"].score_z > idx["senegal"].score_z
    # blend with weight 0 leaves ratings unchanged
    sm = build_strength(load_prior())
    assert squad_adjusted_ratings(sm, idx, 0.0) is sm


# ── recent form ───────────────────────────────────────────────────────────────
def test_form_index_ranks_winners_above_losers():
    from prediction_market.model.form_strength import form_index, form_adjusted_ratings
    from prediction_market.model.strength import build_strength
    from prediction_market.ingest.prior_ingest import load_prior
    c = _mem_db()
    for api, cid in ((10, "france"), (20, "senegal")):
        store.upsert(c, "team_meta",
                     {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    # France wins big; Senegal loses big (both friendlies)
    store.upsert(c, "nt_recent", {"fixture_api_id": 1, "team_api_id": 10, "opp_api_id": 99,
                 "kickoff_ts": "2026-06-01T00:00:00+00:00", "league_id": 10, "is_friendly": 1,
                 "gf": 3, "ga": 0, "is_home": 1, "fetched_at": store.utcnow()}, pk=["fixture_api_id", "team_api_id"])
    store.upsert(c, "nt_recent", {"fixture_api_id": 2, "team_api_id": 20, "opp_api_id": 99,
                 "kickoff_ts": "2026-06-01T00:00:00+00:00", "league_id": 10, "is_friendly": 1,
                 "gf": 0, "ga": 3, "is_home": 1, "fetched_at": store.utcnow()}, pk=["fixture_api_id", "team_api_id"])
    idx = form_index(c)
    assert idx["france"].form_z > idx["senegal"].form_z
    sm = build_strength(load_prior())
    assert form_adjusted_ratings(sm, idx, 0.0) is sm
    boosted = form_adjusted_ratings(sm, idx, 0.3)
    assert boosted.ratings["france"] > sm.ratings["france"]   # good form lifts the rating
