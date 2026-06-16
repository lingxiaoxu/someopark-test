"""Tests for penalty model + in-play tactics (plan 03 §4b, 04 §4c, 15)."""
from __future__ import annotations

from prediction_market.ingest.prior_ingest import load_prior
from prediction_market.model.inplay import live_from_strength, live_match_prob
from prediction_market.model.penalties import kick_order_band, shootout_win_prob
from prediction_market.model.strength import build_strength
from prediction_market.strategy.inplay_tactics import (
    convergence_take_profit,
    draw_trade_signal,
    totals_time_decay,
)


def test_shootout_not_flat_and_clamped():
    sm = build_strength(load_prior())
    # Strong vs weak → favourite edge, but clamped (never 90/10).
    p = shootout_win_prob(sm, "spain", "saudi_arabia")
    assert 0.5 < p <= 0.65
    # Complementary.
    assert abs(shootout_win_prob(sm, "spain", "saudi_arabia") +
               shootout_win_prob(sm, "saudi_arabia", "spain") - 1.0) < 1e-9
    # Reputation tilts an otherwise even pair (Germany rep+ vs a neutral peer).
    first, second = kick_order_band(0.5)
    assert first > 0.5 > second   # kick-order advantage to first


def test_late_equalizer_take_profit():
    sm = build_strength(load_prior())
    # Level score, late → SELL the draw (lock profit), the user's tactic.
    lp = live_from_strength(sm, "brazil", "morocco", 82, 1, 1)
    sig = draw_trade_signal(lp, draw_entry=0.24)
    assert sig.act == "SELL" and sig.side == "draw" and sig.urgency == "high"


def test_draw_time_value_entry():
    sm = build_strength(load_prior())
    lp = live_from_strength(sm, "brazil", "morocco", 20, 0, 0)
    sig = draw_trade_signal(lp, draw_market_price=0.18)   # draw cheap vs fair
    assert sig.act == "BUY" and sig.side == "draw"


def test_convergence_take_profit():
    sm = build_strength(load_prior())
    lp = live_from_strength(sm, "brazil", "morocco", 80, 2, 0)   # home cruising
    assert convergence_take_profit("home", 0.55, lp).act == "SELL"
    # Not yet near max → hold.
    early = live_from_strength(sm, "brazil", "morocco", 10, 0, 0)
    assert convergence_take_profit("home", 0.55, early).act == "HOLD"


def test_totals_time_decay():
    lp = live_match_prob(1.4, 1.2, 80, 0, 0)   # 0:0 at 80' → Under very likely
    sig = totals_time_decay(lp, line=2.5)
    assert sig.act in ("SELL", "HOLD")
    assert lp.p_over_total[2.5] < 0.3          # over unlikely this late at 0:0


def test_live_momentum_from_store():
    import sqlite3
    from prediction_market.ingest import store
    from prediction_market.strategy.inplay_tactics import live_momentum_from_store
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; store.init_db(c)
    # Away team dominates xG (3.2) but score is level → BUY away (under-priced).
    for tid, xg in ((100, 0.6), (200, 3.2)):
        store.upsert(c, "fixture_stats", {"fixture_api_id": 9, "team_api_id": tid, "xg": xg,
                     "fetched_at": store.utcnow()}, pk=["fixture_api_id", "team_api_id"])
    sig = live_momentum_from_store(c, 9, 100, 200, minute=70, home_goals=1, away_goals=1)
    assert sig.act == "BUY" and sig.side == "away"
    # No xG yet → HOLD.
    assert live_momentum_from_store(c, 999, 100, 200, 70, 0, 0).act == "HOLD"


def test_fixture_stats_parser():
    from prediction_market.ingest.soccer_ingest import _stat
    stats = [{"type": "expected_goals", "value": 1.46}, {"type": "Total Shots", "value": 16}]
    assert _stat(stats, "expected_goals") == 1.46
    assert _stat(stats, "Total Shots") == 16
    assert _stat(stats, "missing") is None


def test_live_poller_generates_inplay_signals():
    import sqlite3
    from prediction_market.ingest import store
    from prediction_market.jobs.live_poller import generate_inplay_signals
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; store.init_db(c)
    for api, cid in ((10, "brazil"), (20, "morocco")):
        store.upsert(c, "team_meta", {"api_id": api, "canonical_team_id": cid,
                     "updated_at": store.utcnow()}, pk=["api_id"])
    # Live: level at 82' → late-equalizer draw take-profit must appear.
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "2H",
        "home_api_id": 10, "away_api_id": 20, "home_goals": 1, "away_goals": 1, "elapsed": 82,
        "updated_at": store.utcnow()}, pk=["api_id"])
    sigs = generate_inplay_signals(conn=c)
    assert any(s["act"] == "SELL" and s["side"] == "draw" for s in sigs)
    # No live fixtures → no signals.
    c2 = sqlite3.connect(":memory:"); c2.row_factory = sqlite3.Row; store.init_db(c2)
    assert generate_inplay_signals(conn=c2) == []


def test_xg_shades_live_lambda():
    from prediction_market.model.inplay import live_match_prob
    # Home out-creating xG (1.8 vs 0.3) at 60' 0:0 → higher home win prob than no-xG.
    base = live_match_prob(1.5, 1.5, 60, 0, 0)
    hot = live_match_prob(1.5, 1.5, 60, 0, 0, xg_home=1.8, xg_away=0.3)
    assert hot.p_home > base.p_home


def test_inplay_arb_finder():
    import sqlite3
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength
    from prediction_market.strategy.inplay_arb import find_opportunities
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; store.init_db(c)
    for api, cid in ((10, "france"), (20, "senegal")):
        store.upsert(c, "team_meta", {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "2H",
        "home_api_id": 10, "away_api_id": 20, "home_goals": 0, "away_goals": 0, "elapsed": 78,
        "updated_at": store.utcnow()}, pk=["api_id"])
    sm = build_strength(load_prior())
    # market under-prices the late draw → relative-value BUY draw opportunity.
    qs = {"kalshi": lambda fid: {"home": 0.30, "draw": 0.45, "away": 0.10}}
    opps = find_opportunities(conn=c, sm=sm, quote_sources=qs)
    assert any(o["kind"] == "relative_value" and o["side"] == "draw" for o in opps)
    # no live fixtures → no opportunities.
    c2 = sqlite3.connect(":memory:"); c2.row_factory = sqlite3.Row; store.init_db(c2)
    assert find_opportunities(conn=c2) == []


def test_inplay_lock_arb_only_on_real_gap():
    import sqlite3
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength
    from prediction_market.strategy.inplay_arb import find_opportunities
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; store.init_db(c)
    for api, cid in ((10, "iraq"), (20, "norway")):
        store.upsert(c, "team_meta", {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "1H",
        "home_api_id": 10, "away_api_id": 20, "home_goals": 0, "away_goals": 0, "elapsed": 25,
        "updated_at": store.utcnow()}, pk=["api_id"])
    sm = build_strength(load_prior())
    # Efficient/agreeing prices → NO lock arb (no free money).
    agree = {"kalshi": lambda f: {"home": 0.07, "draw": 0.13, "away": 0.82},
             "poly_us": lambda f: {"home": 0.06, "draw": 0.12, "away": 0.81}}
    assert [o for o in find_opportunities(conn=c, sm=sm, quote_sources=agree) if o["kind"] == "lock_arb"] == []
    # Real cross-venue gap on 'away' → lock arb fires.
    gap = {"kalshi": lambda f: {"home": 0.07, "draw": 0.13, "away": 0.82},
           "poly_us": lambda f: {"home": 0.20, "draw": 0.12, "away": 0.70}}
    locks = [o for o in find_opportunities(conn=c, sm=sm, quote_sources=gap) if o["kind"] == "lock_arb"]
    assert locks and all(o["edge"] > 0 for o in locks)


def test_lock_arb_uses_bid_for_sell_leg_no_false_positive():
    import sqlite3
    from prediction_market.ingest import store
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength
    from prediction_market.strategy.inplay_arb import find_opportunities
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; store.init_db(c)
    for api, cid in ((10, "iraq"), (20, "norway")):
        store.upsert(c, "team_meta", {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "1H",
        "home_api_id": 10, "away_api_id": 20, "home_goals": 0, "away_goals": 0, "elapsed": 25,
        "updated_at": store.utcnow()}, pk=["api_id"])
    sm = build_strength(load_prior())
    # Same ask but a spread: selling at the BID (0.81) < buying at the ASK (0.82) → no arb.
    qs = {"kalshi": lambda f: {"away": {"ask": 0.82, "bid": 0.81}},
          "poly_us": lambda f: {"away": {"ask": 0.82, "bid": 0.81}}}
    assert [o for o in find_opportunities(conn=c, sm=sm, quote_sources=qs) if o["kind"] == "lock_arb"] == []
    # Genuine dislocation: US bid 0.90 >> Kalshi ask 0.82 → real lock.
    qs2 = {"kalshi": lambda f: {"away": {"ask": 0.82, "bid": 0.81}},
           "poly_us": lambda f: {"away": {"ask": 0.91, "bid": 0.90}}}
    locks = [o for o in find_opportunities(conn=c, sm=sm, quote_sources=qs2) if o["kind"] == "lock_arb"]
    assert locks and locks[0]["edge"] > 0


def test_new_event_driven_tactics():
    """The 4 added tactics fire on their trigger conditions (and only then)."""
    from prediction_market.model.inplay import live_match_prob
    from prediction_market.strategy.inplay_tactics import (
        goal_overreaction_fade, favourite_comeback, red_card_value, knockout_late_draw)

    # Surprising goal (underdog scored) → fade: back the pre-match favourite, briefly.
    lp = live_match_prob(2.0, 0.5, 33, 0, 1)
    s = goal_overreaction_fade(lp, prematch_fav_side="home", last_goal_side="away", last_goal_minute=30)
    assert s.act == "BUY" and s.side == "home"
    # Outside the window → HOLD.
    assert goal_overreaction_fade(lp, prematch_fav_side="home", last_goal_side="away", last_goal_minute=20).act == "HOLD"
    # Favourite scored (not surprising) → HOLD.
    assert goal_overreaction_fade(lp, prematch_fav_side="home", last_goal_side="home", last_goal_minute=30).act == "HOLD"

    # Clear favourite trailing, still time → back the comeback.
    assert favourite_comeback(lp, prematch_fav_side="home", prematch_fav_prob=0.72).act == "BUY"
    # Favourite leading → HOLD.
    assert favourite_comeback(live_match_prob(2.0, 0.5, 33, 1, 0), prematch_fav_side="home", prematch_fav_prob=0.72).act == "HOLD"

    # Red card on away → value on home, within the window.
    lp2 = live_match_prob(1.3, 1.2, 48, 0, 0, red_away=1)
    assert red_card_value(lp2, carded_side="away", card_minute=40).act == "BUY"
    assert red_card_value(lp2, carded_side="away", card_minute=20).act == "HOLD"   # too old

    # Level knockout late → back the 90' draw; group stage → HOLD (opposite sign).
    lp3 = live_match_prob(1.2, 1.2, 85, 1, 1)
    assert knockout_late_draw(lp3, knockout=True).act == "BUY"
    assert knockout_late_draw(lp3, knockout=False).act == "HOLD"
