"""Knockout-stage SETTLEMENT + accuracy口径 audit (performance_report / milestone_export).

Core design decision under test (see project memory / decision_model docstrings):
the per-match prediction market (Kalshi KXWCGAME / Poly fwc) settles on the
90-MINUTE 3-way result — home / Tie / away by REGULATION score — for BOTH the group
stage AND the knockout stage. A 1-1 knockout tie at 90' is a valid Tie payout; extra
time + penalties then decide who ADVANCES, which is a SEPARATE per-team reach-round
product (KXWCROUND) and must NOT leak into the 90-min 3-way PnL / Brier.

These tests lock in, for knockout fixtures:
  1. match_pick settles on the 90-min 3-way (a 1-1 tie → result/won = 'draw', model
     keeps a draw side, no 2-way advance switch);
  2. _settled (headline accuracy Brier) prices knockout matches with the SAME 90-min
     3-way model the bets use (knockout=False), NOT the down-scaled-λ advance model —
     so the accuracy口径 reconciles with the bet口径;
  3. _advancer is the PARALLEL "who went through"口径 (penalty winner flag) and is
     correct + independent of the 90-min 3-way result (it must not pollute won/pnl);
  4. milestone_export reconciles with match_pick on a knockout fixture (same pick).
"""
from __future__ import annotations

import json
import sqlite3

from prediction_market.ingest import store
from prediction_market.ingest.prior_ingest import load_prior
from prediction_market.model.match_pricing import price_match
from prediction_market.model.strength import build_strength
from prediction_market.ops import milestone_export, performance_report as PR


def _mem_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init_db(c)
    return c


def _teams(c):
    for api, cid in ((10, "brazil"), (20, "france")):
        store.upsert(c, "team_meta",
                     {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])


def _ko_fixture(c, *, gh, ga, status="FT", round_name="Round of 32", raw="{}", api=1):
    store.upsert(c, "fixture", {
        "api_id": api, "league_id": 1, "season": 2026, "status_short": status,
        "home_api_id": 10, "away_api_id": 20, "home_goals": gh, "away_goals": ga,
        "round": round_name, "kickoff_ts": "2026-07-01T19:00:00+00:00",
        "raw_json": raw, "updated_at": store.utcnow()}, pk=["api_id"])


# ── 1) match_pick: knockout settles on the 90-min 3-way (draw is valid) ──────────
def test_match_pick_knockout_1_1_settles_as_draw():
    c = _mem_db(); _teams(c)
    # 1-1 at 90', France advances on penalties (winner flag) — but the 90-min market is a TIE.
    raw = json.dumps({"teams": {"home": {"winner": False}, "away": {"winner": True}}})
    _ko_fixture(c, gh=1, ga=1, status="PEN", raw=raw)
    sm = build_strength(load_prior())
    fx = c.execute("SELECT * FROM fixture WHERE api_id=1").fetchone()
    mr = PR.match_pick(sm, None, "brazil", "france", fx, None, conn=c, quotes=None, pit=False)
    assert mr is not None
    assert mr["stage"] == "knockout"                 # stage labelled, but...
    assert mr["result"] == "draw"                    # ...settled on the 90-min Tie
    assert set(mr["model"].keys()) == {"home", "draw", "away"}  # 3-way, draw NOT dropped
    # legacy (no quotes) bets the model argmax at 90-min — a draw IS a legal pick here.
    assert mr["pick"] in ("home", "draw", "away")
    # The advance side (France=away) must NOT be forced as the 90-min result/won.
    assert mr["won"] == (mr["pick"] == "draw")


def test_match_pick_knockout_decisive_score_is_3way():
    c = _mem_db(); _teams(c)
    _ko_fixture(c, gh=2, ga=1, status="AET", round_name="Round of 16")
    sm = build_strength(load_prior())
    fx = c.execute("SELECT * FROM fixture WHERE api_id=1").fetchone()
    mr = PR.match_pick(sm, None, "brazil", "france", fx, None, conn=c, quotes=None, pit=False)
    assert mr["stage"] == "knockout" and mr["result"] == "home"   # 2-1 at 90' → home
    # model probs are the 90-min 3-way (knockout=False) — sum to 1, draw present.
    s = sum(mr["model"].values())
    assert abs(s - 1.0) < 1e-6 and mr["model"]["draw"] > 0.0


# ── 2) _settled accuracy口径 uses the SAME 90-min 3-way model the bets use ────────
def test_settled_knockout_uses_90min_3way_model():
    c = _mem_db(); _teams(c)
    _ko_fixture(c, gh=2, ga=1, status="AET", round_name="Round of 16")
    sm = build_strength(load_prior())
    data = PR._settled(c, sm)
    assert len(data) == 1
    probs, outcome = data[0]
    assert outcome == 0                              # 2-1 → home win (90-min 3-way outcome)
    assert abs(sum(probs) - 1.0) < 1e-6
    # The accuracy probs MUST equal the bet/MTM model (price_match knockout=False), NOT the
    # down-scaled-λ advance model. This is the regression that locks the fix.
    sm_pit = PR._pit_strength(c, "2026-07-01T19:00:00+00:00")
    mp = price_match(sm_pit, "brazil", "france", knockout=False)
    assert all(abs(a - b) < 1e-9 for a, b in zip(probs, [mp.p_home, mp.p_draw, mp.p_away]))
    # And it must DIFFER from the knockout=True (advance) λ-scaled probs — proving the flag
    # is genuinely off (else the test would pass vacuously).
    mk = price_match(sm_pit, "brazil", "france", knockout=True)
    assert any(abs(a - b) > 1e-4 for a, b in zip(probs, [mk.p_home, mk.p_draw, mk.p_away]))


def test_settled_knockout_draw_outcome():
    c = _mem_db(); _teams(c)
    raw = json.dumps({"teams": {"home": {"winner": True}, "away": {"winner": False}}})
    _ko_fixture(c, gh=0, ga=0, status="PEN", raw=raw)   # 0-0, Brazil advances on pens
    data = PR._settled(c, build_strength(load_prior()))
    # 90-min 3-way outcome is a DRAW (1), independent of who advanced on penalties.
    assert data[0][1] == 1


# ── 3) _advancer: parallel "who went through"口径 (penalties), correct + isolated ─
def test_advancer_by_score():
    assert PR._advancer("{}", 2, 0) == "home"
    assert PR._advancer("{}", 0, 3) == "away"


def test_advancer_level_uses_winner_flag():
    raw_away = json.dumps({"teams": {"home": {"winner": False}, "away": {"winner": True}}})
    raw_home = json.dumps({"teams": {"home": {"winner": True}, "away": {"winner": False}}})
    assert PR._advancer(raw_away, 1, 1) == "away"     # France advanced on penalties
    assert PR._advancer(raw_home, 1, 1) == "home"
    assert PR._advancer(None, 1, 1) is None           # level, no winner flag → undeterminable
    assert PR._advancer("{}", 1, 1) is None


def test_advancer_independent_of_90min_result():
    """The advancer (penalty winner) can DISAGREE with the 90-min 3-way result — a tie at
    90' is a 'draw' for settlement yet has a definite advancer. The two口径 are parallel;
    _advancer must never overwrite the 90-min result used for won/pnl."""
    c = _mem_db(); _teams(c)
    raw = json.dumps({"teams": {"home": {"winner": False}, "away": {"winner": True}}})
    _ko_fixture(c, gh=1, ga=1, status="PEN", raw=raw)
    sm = build_strength(load_prior())
    fx = c.execute("SELECT * FROM fixture WHERE api_id=1").fetchone()
    mr = PR.match_pick(sm, None, "brazil", "france", fx, None, conn=c, quotes=None, pit=False)
    assert mr["result"] == "draw"                       # 90-min settlement
    assert PR._advancer(raw, 1, 1) == "away"            # advance口径 disagrees → and stays separate


# ── 4) milestone_export reconciles with match_pick on a knockout fixture ─────────
def test_milestone_export_knockout_reconciles_with_bet_log():
    c = _mem_db(); _teams(c)
    _ko_fixture(c, gh=2, ga=1, status="AET", round_name="Round of 16")
    # A PRE milestone snapshot so milestone_export has a fixture to track.
    store.upsert(c, "milestone_snapshot", {
        "fixture_api_id": 1, "milestone": "PRE", "ts": "2026-07-01T18:40:00+00:00",
        "elapsed": 0, "status_short": "NS", "home_goals": 0, "away_goals": 0,
        "price_source": "live"}, pk=["fixture_api_id", "milestone"])
    doc = milestone_export.build(conn=c)
    assert doc["n"] == 1
    m = doc["matches"][0]
    assert m["round"] == "Round of 16" and m["settled"] is True
    assert m["result"] == "home"                        # 2-1 at 90' → home (90-min 3-way)
    # The pick comes from the SAME match_pick the bet log uses → 3-way, never a 2-way advance.
    assert m["our_bet"]["side"] in ("home", "draw", "away")


def test_full_report_builds_with_knockout_fixture():
    """End-to-end: building the report over a knockout fixture must not raise and must
    count the knockout match in the headline accuracy sample."""
    c = _mem_db(); _teams(c)
    _ko_fixture(c, gh=2, ga=1, status="AET", round_name="Quarter-finals")
    rep = PR.build(conn=c)
    assert rep.n_settled == 1
    assert 0.0 <= rep.brier <= 2.0
