"""Tests for the performance/P&L report and risk report (plan 03 §9, 04 §6, 05 §5)."""
from __future__ import annotations

from prediction_market_soccer.ingest import store
from prediction_market_soccer.ops import (
    backtest_export,
    frontend_export,
    inplay_export,
    performance_report,
    risk_report,
    upcoming_export,
)
from prediction_market_soccer.tests import clubctx

_mem_db = clubctx.mem_db
_HOME, _AWAY = clubctx.ARSENAL, clubctx.IPSWICH


def _settled_epl(c, api=1, hg=2, ag=0, days_ago=3.0):
    """One settled EPL fixture — the smallest thing the settled-match paths accept
    (enabled competition + its season + a kickoff inside the 60-day window)."""
    clubctx.seed_teams(c, _HOME, _AWAY)
    return clubctx.seed_fixture(c, api, _HOME, _AWAY, hg=hg, ag=ag, days_ago=days_ago)


# ── performance / P&L ─────────────────────────────────────────────────────────
def test_performance_report_no_settled_matches():
    c = _mem_db()
    rep = performance_report.build(conn=c)
    assert rep.n_settled == 0
    assert rep.settled_signal_pnl == 0.0
    assert any("no settled" in n for n in rep.notes)


def test_performance_report_with_settled_match():
    c = _mem_db()
    _settled_epl(c)
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
    _settled_epl(c)
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
    """The club overview card is a compact snapshot (leagues + calibration gate + the
    per-comp venue series), not the WC module's static Chinese catalog."""
    c = _mem_db()
    doc = frontend_export.build(conn=c, as_of="2026-08-26")
    for key in ("schema_version", "as_of", "headline", "headline_i18n", "mode_key",
                "gate_open", "calibration", "leagues", "series", "model_notes"):
        assert key in doc, f"missing {key}"
    assert doc["as_of"] == "2026-08-26"
    # Every hand-written headline ships an i18n key beside it — the five-language
    # frontend must never have to render the Chinese fallback.
    assert doc["headline_i18n"]["key"].startswith("overview.")
    assert doc["mode_key"] == "overview.paperOnly"
    # The series map is registry-driven: one entry per enabled competition.
    from prediction_market_soccer.config.leagues import active
    assert set(doc["series"]) == {comp.key for comp in active()}
    assert doc["series"]["epl"]["kalshi_game"] == "KXEPLGAME"
    assert isinstance(doc["gate_open"], bool)


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
    clubctx.seed_teams(c, _HOME, _AWAY)
    ts = clubctx.seed_fixture(c, 1, _HOME, _AWAY, status="NS", days_ago=-1.0)
    rows = upcoming_export.build(limit=5, conn=c, with_venues=False)
    assert len(rows) == 1
    m = rows[0]
    assert m["home"]["id"] == "arsenal" and m["away"]["id"] == "ipswich"
    assert m["league"] == "epl" and m["et_date"]
    assert m["kickoff"] == ts
    assert abs(m["model"]["home"] + m["model"]["draw"] + m["model"]["away"] - 1.0) < 0.02
    # A league round: no advance product, no two-leg aggregate (§3.0 caps contract).
    assert m["caps"]["stage"] == "league"
    assert m["caps"]["advance"] is False and m["caps"]["two_leg"] is False
    assert m["knockout"] is False and m["advance"] is None
    assert m["kalshi"] is None and m["poly_us"] is None   # venues skipped → genuinely None


def test_upcoming_two_leg_tie_exposes_the_advance_product():
    """The same builder on a UEFA knockout leg must switch the caps on — the frontend
    renders the advance block from caps, never from the round name (C1)."""
    c = _mem_db()
    clubctx.seed_teams(c, clubctx.LYON, clubctx.CELTIC)
    clubctx.seed_fixture(c, 1, clubctx.LYON, clubctx.CELTIC, comp=clubctx.UCL,
                         round_name=clubctx.KO_ROUND, status="NS", days_ago=-1.0)
    rows = upcoming_export.build(limit=5, conn=c, with_venues=False)
    assert len(rows) == 1
    m = rows[0]
    assert m["league"] == "ucl"
    assert m["caps"]["stage"] == "cup_two_leg"
    assert m["caps"]["advance"] is True and m["caps"]["two_leg"] is True
    assert m["knockout"] is True
    assert m["advance"] is not None
    adv = m["advance"]["model"]
    assert abs(adv["home"] + adv["away"] - 1.0) < 1e-6   # 2-way, no draw


# ── live in-play export ───────────────────────────────────────────────────────
def test_inplay_export_no_live():
    c = _mem_db()
    doc = inplay_export.build(conn=c, with_venues=False)
    assert doc["n_live"] == 0 and doc["matches"] == []


def test_inplay_export_live_match():
    c = _mem_db()
    clubctx.seed_teams(c, _HOME, _AWAY)
    clubctx.seed_fixture(c, 1, _HOME, _AWAY, status="2H", hg=0, ag=0, elapsed=78,
                         days_ago=0.05)
    for tid, xgv in ((_HOME[0], 1.9), (_AWAY[0], 0.3)):
        store.upsert(c, "fixture_stats", {"fixture_api_id": 1, "team_api_id": tid, "xg": xgv,
                     "fetched_at": store.utcnow()}, pk=["fixture_api_id", "team_api_id"])
    doc = inplay_export.build(conn=c, with_venues=False)
    assert doc["n_live"] == 1
    m = doc["matches"][0]
    assert m["score"] == "0-0" and m["minute"] == 78
    # state-dependent model: at 78' 0-0 the draw should dominate the live 3-way
    assert m["model"]["draw"] > m["model"]["home"] and m["model"]["draw"] > m["model"]["away"]
    assert abs(m["model"]["home"] + m["model"]["draw"] + m["model"]["away"] - 1.0) < 0.02
    # xG-momentum trick should flag the home side (xG 1.9 vs 0.3, still 0-0) as a signal
    assert any(o["kind"] == "tactic" for o in m["opportunities"])


# ── OOS backtest export ───────────────────────────────────────────────────────
def test_backtest_export_with_settled():
    c = _mem_db()
    _settled_epl(c)
    doc = backtest_export.build(conn=c)
    assert doc["n_settled"] == 1
    assert doc["brier"]["uniform"] == round(2 / 3, 4)
    assert doc["brier"]["model"] is not None
    assert doc["matches"][0]["result"] == "H"          # Arsenal 2-0 → home win
    assert "conclusion" in doc


# ── walk-forward Elo result-update ────────────────────────────────────────────
def test_walkforward_update_moves_prediction():
    """Proves the result-update (Elo) genuinely shifts ratings + future predictions
    once a club has a prior game — i.e. #1 is implemented, not just inert. With 34-38
    rounds a season this is the club model's MAIN in-season signal (§3.2)."""
    from prediction_market_soccer.model.match_pricing import price_match
    from prediction_market_soccer.model.strength import update_with_results
    base = clubctx.epl_strength()
    a, b, opp = "brighton", "brentford", "chelsea"
    p_before = price_match(base, a, opp).p_home
    updated = update_with_results(
        base, [{"home_id": a, "away_id": b, "home_goals": 5, "away_goals": 0, "days_ago": 1}], lr=0.2)
    p_after = price_match(updated, a, opp).p_home
    assert updated.ratings[a] > base.ratings[a]   # rating rises on over-performance
    assert p_after > p_before                       # → higher win prob in the next game


def test_walkforward_eval_no_repeat_teams():
    """On a first-games-only sample the walk-forward equals baseline (inert)."""
    from prediction_market_soccer.ops import walkforward_eval
    c = _mem_db()
    clubctx.seed_teams(c, _HOME, _AWAY, clubctx.BRIGHTON, clubctx.BRENTFORD)
    # two matches, four DISTINCT clubs → no prior data for anyone
    clubctx.seed_fixture(c, 1, _HOME, _AWAY, hg=1, ag=0, days_ago=8)
    clubctx.seed_fixture(c, 2, clubctx.BRIGHTON, clubctx.BRENTFORD, hg=2, ag=2, days_ago=7)
    doc = walkforward_eval.run(conn=c)
    assert doc["n_matches_with_prior_data"] == 0
    assert doc["improves"] is False


# ── squad strength ────────────────────────────────────────────────────────────
def _appearance(c, fixture_id, team, pid, rating, minutes, goals=0, assists=0):
    store.upsert(c, "fixture_player_stats", {
        "fixture_api_id": fixture_id, "team_api_id": team[0], "player_api_id": pid,
        "player_name": f"P{pid}", "minutes": minutes, "rating": rating,
        "goals": goals, "assists": assists, "fetched_at": store.utcnow()},
        pk=["fixture_api_id", "player_api_id"])


def test_squad_index_and_export():
    c = _mem_db()
    from prediction_market_soccer.model.squad_strength import squad_index, squad_adjusted_ratings
    clubctx.seed_teams(c, _HOME, _AWAY)
    clubctx.seed_fixture(c, 1, _HOME, _AWAY, hg=2, ag=0, days_ago=3)
    _appearance(c, 1, _HOME, 1, rating=7.6, minutes=90, goals=2)
    _appearance(c, 1, _AWAY, 2, rating=6.8, minutes=90)
    idx = squad_index(c)
    assert idx["arsenal"].mw_rating > idx["ipswich"].mw_rating      # higher-rated squad
    assert idx["arsenal"].score_z > idx["ipswich"].score_z
    # blend with weight 0 leaves ratings unchanged
    sm = clubctx.epl_strength()
    assert squad_adjusted_ratings(sm, idx, 0.0) is sm


def test_squad_index_prefers_lineup_feed_over_topscorers():
    """`player_stat` is the TOPSCORERS feed — a club's 1-3 leading scorers. Built on
    that alone the index ranked clubs by which of their strikers happened to be in the
    top-20, and the distortion leaked into live ratings via squad_blend_weight. The
    per-fixture lineup feed wins wherever it covers a club; the topscorer feed only
    fills the clubs it does not reach yet."""
    from prediction_market_soccer.model.squad_strength import squad_index
    c = _mem_db()
    clubctx.seed_teams(c, _HOME, _AWAY)
    clubctx.seed_fixture(c, 1, _HOME, _AWAY, hg=1, ag=1, days_ago=3)
    # Arsenal covered by real lineups: a whole XI of ordinary 6.5 performances.
    for pid in range(1, 12):
        _appearance(c, 1, _HOME, pid, rating=6.5, minutes=90)
    # ...and one flattering topscorer row that must NOT count for a covered club.
    store.upsert(c, "player_stat", {"player_api_id": 99, "league_id": 39, "season": 2026,
                 "team_api_id": _HOME[0], "rating": 9.5, "goals": 12, "assists": 0,
                 "minutes": 900, "updated_at": store.utcnow()},
                 pk=["player_api_id", "league_id", "season"])
    # Ipswich has no lineup rows at all → the topscorer feed is its only source.
    store.upsert(c, "player_stat", {"player_api_id": 98, "league_id": 39, "season": 2026,
                 "team_api_id": _AWAY[0], "rating": 7.2, "goals": 6, "assists": 0,
                 "minutes": 900, "updated_at": store.utcnow()},
                 pk=["player_api_id", "league_id", "season"])
    idx = squad_index(c)
    assert idx["arsenal"].n_players == 11              # the 9.5 topscorer row is ignored
    assert abs(idx["arsenal"].mw_rating - 6.5) < 1e-6  # league mult 1.0 for the EPL
    assert idx["ipswich"].n_players == 1               # uncovered club → fallback feed


# ── recent form ───────────────────────────────────────────────────────────────
def _recent(c, fixture_id, team, gf, ga, days_ago, friendly=0):
    store.upsert(c, "nt_recent", {
        "fixture_api_id": fixture_id, "team_api_id": team[0], "opp_api_id": 999,
        "kickoff_ts": clubctx.ts_ago(days_ago), "league_id": clubctx.EPL.api_football_id,
        "is_friendly": friendly, "gf": gf, "ga": ga, "is_home": 1,
        "fetched_at": store.utcnow()}, pk=["fixture_api_id", "team_api_id"])


def test_form_index_ranks_winners_above_losers():
    from prediction_market_soccer.model.form_strength import form_index, form_adjusted_ratings
    c = _mem_db()
    clubctx.seed_teams(c, _HOME, _AWAY)
    _recent(c, 1, _HOME, 3, 0, days_ago=6)      # Arsenal winning
    _recent(c, 2, _AWAY, 0, 3, days_ago=6)      # Ipswich losing
    idx = form_index(c)
    assert idx["arsenal"].form_z > idx["ipswich"].form_z
    sm = clubctx.epl_strength()
    assert form_adjusted_ratings(sm, idx, 0.0) is sm
    boosted = form_adjusted_ratings(sm, idx, 0.3)
    assert boosted.ratings["arsenal"] > sm.ratings["arsenal"]   # good form lifts the rating


def test_form_index_shrinks_a_one_match_sample():
    """A club with ONE result used to carry the same z-weight as one with fifty, so a
    single 3-0 put a minnow above Roma and Inter — and that z leaks into live ratings
    via form_blend_weight. Shrinkage n/(n+3) keeps the ORDER but discounts the noise."""
    from prediction_market_soccer.model.form_strength import form_index
    c = _mem_db()
    clubctx.seed_teams(c, _HOME, _AWAY, clubctx.BRIGHTON)
    _recent(c, 1, _HOME, 3, 0, days_ago=5)                      # one big win, n=1
    for i, d in enumerate((5, 12, 19, 26, 33, 40)):             # six wins, n=6
        _recent(c, 10 + i, clubctx.BRIGHTON, 3, 0, days_ago=d)
    _recent(c, 30, _AWAY, 0, 3, days_ago=5)
    idx = form_index(c)
    assert idx["arsenal"].n == 1 and idx["brighton"].n == 6
    # Same raw weighted GD, but the one-match club keeps ~1/4 of it and the six-match
    # club ~2/3 → the better-evidenced club leads.
    assert abs(idx["arsenal"].weighted_gd - idx["brighton"].weighted_gd) < 0.2
    assert idx["brighton"].form_z > idx["arsenal"].form_z > 0


# ── probability calibration ───────────────────────────────────────────────────
def test_calibration_fixes_overconfidence():
    """An over-confident-but-skilled model is fixed by calibration → beats uniform."""
    from prediction_market_soccer.model.probability_calibration import fit_calibration, apply_calibration
    # 8 matches: model always sharply favours home (0.85), home wins ~half → over-confident
    P, Y = [], []
    for i in range(8):
        P.append([0.85, 0.10, 0.05])
        Y.append(0 if i % 2 == 0 else 2)   # home wins half, loses half
    cal = fit_calibration(P, Y)
    # raw is over-confident (Brier > uniform); calibration must not be worse than uniform
    assert cal["calibrated_brier"] <= cal["uniform_brier"] + 1e-9
    assert cal["method"] in ("temperature", "shrinkage")
    soft = apply_calibration([0.85, 0.10, 0.05], cal)
    assert abs(sum(soft) - 1.0) < 1e-9 and soft[0] < 0.85   # softened toward less confident


def test_calibration_passthrough_when_unfit():
    from prediction_market_soccer.model.probability_calibration import apply_calibration
    assert apply_calibration([0.5, 0.3, 0.2], None) == [0.5, 0.3, 0.2]
