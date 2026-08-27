"""Cup/knockout regression tests for the club SUPPORT modules.

Core design under test (authoritative): the single-match contract (KXUCLGAME / the Poly
per-match market) is the 90-MINUTE 3-way for BOTH a league round and a knockout leg — a
1-1 knockout tie at 90' pays 'Tie'; extra time + penalties only decide the SEPARATE
advance / champion products. These tests pin the knockout behaviour of:

  * config.leagues   — the stage/caps matrix (§3.0): which fixtures own an advance
                       market, a two-leg aggregate, ET-before-pens, a neutral venue.
                       This replaces the WC module's hard-coded 48-team R32 bracket:
                       a club competition's shape is REGISTRY data, not a constant;
  * motivation       — league-table psychology must be a no-op on any knockout round;
  * ucl_phase        — a cup-state competition's champion odds come from the remaining
                       KO TREE (the season table has nothing to say about a bracket);
  * top_scorer       — talent shrinkage (a 2-in-1 burst must not project a 38-goal
                       pace) and the deliberate cup skip;
  * smart_exit       — cash-out only inside the regulation 90' window (no extra-time ticks).

They are additive and must not regress the league-round behaviour.
"""
from __future__ import annotations

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import Stage, caps_dict, caps_for, stage_of
from prediction_market_soccer.ingest import store
from prediction_market_soccer.tests import clubctx


def _mem_db():
    return clubctx.mem_db()


def _toy_strength(ratings: dict[str, float], comp: str | None = None):
    from prediction_market_soccer.model.strength import StrengthModel
    return StrengthModel(ratings=ratings, sigma={t: 0.1 for t in ratings},
                         host_ids=frozenset(), cfg=CONFIG.model, comp=comp)


# ── registry stage/caps matrix (§3.0) ────────────────────────────────────────
def test_league_rounds_own_no_advance_product():
    """A league round — domestic or a European league phase — is a terminal 3-way:
    no advance market, no aggregate, no ET. The draw is a real outcome, not a way
    station."""
    for comp, rnd in (("epl", "Regular Season - 3"), ("brasileirao", "Regular Season - 21"),
                      ("ucl", "League Phase - 3"), ("argentina", "Apertura - 12"),
                      # CONMEBOL's "Group Stage - N" is league-shaped too. Reading the WORD
                      # "group" as World-Cup group progression is bug class C1.
                      ("libertadores", "Group Stage - 3")):
        assert stage_of(comp, rnd) == Stage.LEAGUE, (comp, rnd)
        cp = caps_for(comp, rnd)
        assert not cp.advance and not cp.two_leg and not cp.et_then_pens
        assert not cp.neutral and not cp.ko_draw_semantics


def test_two_leg_tie_carries_et_only_on_the_deciding_leg_and_only_where_the_rules_say():
    """UEFA plays extra time before penalties in the SECOND leg; CONMEBOL goes straight
    to penalties. Leg 1 never decides anything, so it never carries ET either."""
    assert stage_of("ucl", "Round of 16") == Stage.CUP_TWO_LEG
    leg1 = caps_for("ucl", "Round of 16", leg=1)
    leg2 = caps_for("ucl", "Round of 16", leg=2)
    assert leg1.advance and leg1.two_leg and leg1.leg == 1 and not leg1.et_then_pens
    assert leg2.leg == 2 and leg2.et_then_pens              # UEFA: ET before pens
    conmebol = caps_for("libertadores", "Round of 16", leg=2)
    assert conmebol.two_leg and conmebol.leg == 2
    assert not conmebol.et_then_pens                        # CONMEBOL: straight to pens
    # Both are knockout-shaped for the in-play tactics' draw direction.
    assert leg2.ko_draw_semantics and conmebol.ko_draw_semantics


def test_final_is_a_single_neutral_match_with_extra_time():
    cp = caps_for("ucl", "Final")
    assert stage_of("ucl", "Final") == Stage.CUP_SINGLE
    assert cp.advance and not cp.two_leg and cp.neutral and cp.et_then_pens


def test_argentina_playoffs_are_knockout_despite_the_league_prefix():
    """REGRESSION (C1): Argentina's playoff rounds keep the tournament prefix —
    "Apertura - Round of 16" — so a naive "starts with Apertura → league" rule would
    swallow the entire knockout phase. Knockout patterns are checked FIRST."""
    assert stage_of("argentina", "Apertura - Round of 16") == Stage.CUP_SINGLE
    assert stage_of("argentina", "Clausura - Final") == Stage.CUP_SINGLE
    assert stage_of("argentina", "Apertura - 12") == Stage.LEAGUE
    cp = caps_for("argentina", "Apertura - Final")
    assert cp.advance and not cp.et_then_pens    # ARG playoffs: straight pens (R11 default)


def test_unmatched_round_is_surfaced_as_unknown_never_guessed():
    """An unrecognised round name must read UNKNOWN (the G1 gate counts those) and fall
    back to the SAFE league shape — no advance path may fire on a guess."""
    assert stage_of("epl", "Matchday 3") == Stage.UNKNOWN
    assert stage_of("epl", None) == Stage.UNKNOWN
    assert stage_of("not_a_comp", "Regular Season - 3") == Stage.UNKNOWN
    cp = caps_for("epl", "Matchday 3")
    assert not cp.advance and not cp.two_leg


def test_caps_dict_is_the_frontend_contract():
    """Every exported fixture carries this exact payload; the UI renders advance blocks
    and badges from it and never from the competition or round name."""
    cp = caps_for("ucl", "Round of 16", leg=2)
    d = caps_dict(cp, stage_of("ucl", "Round of 16"), agg="1-1")
    assert d == {"stage": "cup_two_leg", "advance": True, "two_leg": True, "leg": 2,
                 "agg": "1-1", "et_then_pens": True, "neutral": False}


# ── motivation: knockout no-op ───────────────────────────────────────────────
def test_motivation_noop_on_knockout():
    """The league-table incentive tilt must NOT fire on knockout rounds — _round_num
    returns None for any round without a trailing "- N", so motivation_multipliers
    short-circuits to neutral."""
    from prediction_market_soccer.model import motivation as M

    for rnd in ("Round of 32", "Round of 16", "Quarter-finals", "Semi-finals", "Final",
                "Apertura - Round of 16", "round of 16", None, ""):
        assert M._round_num(rnd) is None, rnd

    # End-to-end: even with a conn, a knockout round returns neutral.
    c = _mem_db()
    mh, ma, info = M.motivation_multipliers(
        c, {}, "lyon", "celtic", "Round of 16", CONFIG.model)
    assert (mh, ma, info) == (1.0, 1.0, None)


def test_motivation_still_parses_numbered_league_rounds():
    """Regression guard: every league-shaped round vocabulary still parses (no over-broad
    knockout guard), and each numbered round is attributed to its own tournament."""
    from prediction_market_soccer.model import motivation as M
    assert M._round_num("Regular Season - 12") == 12
    assert M._round_num("League Stage - 3") == 3
    assert M._round_num("Apertura - 7") == 7
    assert M._round_num("Group Stage - 4") == 4
    # Argentina runs Apertura and Clausura as two separate tournaments.
    assert M._round_key("Apertura - 7") != M._round_key("Clausura - 7")


def test_motivation_ships_disabled():
    """It is off until a per-league PIT study earns the constants; two independent kill
    switches, either of which makes it an exact no-op."""
    assert CONFIG.model.motiv_enabled is False
    assert CONFIG.model.motiv_weight == 0.0


# ── ucl_phase: a cup's champion odds come from the remaining KO tree ─────────
def _tie(c, key, round_name, leg1, leg2, a, b, *, comp="libertadores", decided=0,
         agg_a=0, agg_b=0):
    store.upsert(c, "tie", {"tie_key": key, "comp": comp, "round": round_name,
                            "leg1_fixture_id": leg1, "leg2_fixture_id": leg2,
                            "team_a_api_id": a[0], "team_b_api_id": b[0],
                            "agg_a": agg_a, "agg_b": agg_b, "decided": decided,
                            "updated_at": store.utcnow()}, pk=["tie_key"])


_LIB = tuple(clubctx.comp_clubs("libertadores"))
_L1, _L2, _L3, _L4 = _LIB[0], _LIB[1], _LIB[2], _LIB[3]


def _lib_bracket(c):
    """Two undecided semi-final ties, neither leg played."""
    from prediction_market_soccer.config.leagues import get
    lib = get("libertadores")
    clubctx.seed_teams(c, _L1, _L2, _L3, _L4)
    for fid, (h, a) in enumerate([(_L1, _L2), (_L2, _L1), (_L3, _L4), (_L4, _L3)], start=1):
        clubctx.seed_fixture(c, fid, h, a, comp=lib, round_name="Semi-finals",
                             status="NS", days_ago=-7.0)
    _tie(c, "lib-sf-1", "Semi-finals", 1, 2, _L1, _L2)
    _tie(c, "lib-sf-2", "Semi-finals", 3, 4, _L3, _L4)
    return c


def test_cup_champion_comes_from_the_knockout_tree():
    """A cup in its knockout phase has no season table to rank — its title odds are the
    Monte-Carlo over the REMAINING tie tree, and they form a distribution over exactly
    the clubs still alive."""
    from prediction_market_soccer.model.ucl_phase import ko_champion
    c = _lib_bracket(_mem_db())
    alive = [_L1[1], _L2[1], _L3[1], _L4[1]]
    sm = _toy_strength({alive[0]: 1.2, alive[1]: 0.0, alive[2]: 0.1, alive[3]: 0.0},
                       comp="libertadores")
    p = ko_champion(c, "libertadores", sm, n_sims=4000, seed=11)
    assert p is not None
    assert set(p) <= set(alive)
    assert abs(sum(p.values()) - 1.0) < 5e-3
    # The strongest club leads, and nobody is a certainty over two ET-capable ties.
    assert max(p, key=p.get) == alive[0]
    assert p[alive[0]] > p[alive[1]] and p[alive[0]] < 0.95


def test_cup_champion_is_none_before_there_is_a_bracket():
    """No alive ties → nothing to simulate. Returning None (rather than a flat guess)
    is what keeps the pre-draw champion card honestly empty."""
    from prediction_market_soccer.model.ucl_phase import ko_champion
    c = _mem_db()
    clubctx.seed_teams(c, _L1, _L2)
    sm = _toy_strength({_L1[1]: 0.5, _L2[1]: 0.0}, comp="libertadores")
    assert ko_champion(c, "libertadores", sm, n_sims=100, seed=1) is None


def test_predraw_swiss_champion_is_deliberately_none():
    """Pricing a 36-club swiss field before the draw exists would be noise, so the
    champion number is withheld rather than invented (§3.4 regime 2)."""
    from prediction_market_soccer.model.ucl_phase import ko_champion
    c = _lib_bracket(_mem_db())      # even WITH ties present, a swiss comp opts out
    sm = _toy_strength({_L1[1]: 0.5, _L2[1]: 0.0, _L3[1]: 0.2, _L4[1]: 0.0}, comp="ucl")
    assert ko_champion(c, "ucl", sm, n_sims=100, seed=1) is None


# ── top_scorer: talent shrinkage + the cup skip ──────────────────────────────
def _scorer(c, pid, name, team, goals, apps):
    store.upsert(c, "player", {"api_id": pid, "name": name}, pk=["api_id"])
    store.upsert(c, "player_stat", {
        "player_api_id": pid, "league_id": clubctx.EPL.api_football_id,
        "season": clubctx.EPL.season, "team_api_id": team[0], "goals": goals,
        "appearances": apps, "minutes": apps * 90, "updated_at": store.utcnow()},
        pk=["player_api_id", "league_id", "season"])


def _scorer_db(n_remaining=10):
    c = _mem_db()
    clubctx.seed_teams(c, clubctx.ARSENAL, clubctx.IPSWICH)
    for i in range(n_remaining):
        clubctx.seed_fixture(c, 100 + i, clubctx.ARSENAL, clubctx.IPSWICH,
                             status="NS", days_ago=-(i + 1))
    return c


def test_top_scorer_regresses_a_one_match_burst_to_talent():
    """The Balogun bug in club form: two goals in a single appearance is a 2.0 rate on
    its face, which over a season projects an impossible board-topping pace. The
    shrinkage prior has to pull it back to something a season can sustain."""
    from prediction_market_soccer.model.top_scorer import top_scorer_board
    c = _scorer_db()
    _scorer(c, 1, "S. Burst", clubctx.ARSENAL, goals=2, apps=1)
    _scorer(c, 2, "R. Steady", clubctx.ARSENAL, goals=10, apps=20)
    board = {r["name"]: r for r in top_scorer_board(c, "epl", n_sims=4000, seed=3)}
    burst, steady = board["S. Burst"], board["R. Steady"]
    assert burst["goals"] == 2                        # real goals kept as the head start
    assert burst["rate"] < 0.5                        # regressed, nowhere near 2.0
    assert burst["talent_source"] == "position_default"
    # Both have the same fixtures left, so the head start decides the race.
    assert burst["matches_left"] == steady["matches_left"] == 10
    assert steady["p_top_scorer"] > burst["p_top_scorer"]
    assert abs(sum(r["p_top_scorer"] for r in board.values()) - 1.0) < 0.02


def test_top_scorer_uses_the_fc26_talent_rate_when_the_player_is_licensed():
    from prediction_market_soccer.model.top_scorer import top_scorer_board
    c = _scorer_db()
    _scorer(c, 1, "S. Burst", clubctx.ARSENAL, goals=2, apps=1)
    c.execute("INSERT INTO fc_player (fc_id, name, canonical_team_id, position_type, "
              "overall, goal_rate, source) VALUES (?,?,?,?,?,?,?)",
              (1, "Sam Burst", "arsenal", "Attack", 88, 0.80, "ea_fc26"))
    c.commit()
    row = top_scorer_board(c, "epl", n_sims=2000, seed=3)[0]
    assert row["talent_source"] == "fc26"
    assert row["prior_rate"] == 0.80
    # A high talent prior lifts the shrunk rate above the position default's ~0.34.
    assert row["rate"] > 0.5


def test_top_scorer_skips_cup_competitions():
    """"Matches remaining" in a bracket is a function of surviving it — a different
    problem from a league season, so the board is withheld rather than faked."""
    from prediction_market_soccer.model.top_scorer import top_scorer_board
    c = _scorer_db()
    _scorer(c, 1, "S. Burst", clubctx.ARSENAL, goals=2, apps=1)
    assert top_scorer_board(c, "libertadores", n_sims=1000, seed=3) == []
    assert top_scorer_board(c, "ucl", n_sims=1000, seed=3) == []


def test_top_scorer_empty_without_a_scorer_feed():
    from prediction_market_soccer.model.top_scorer import top_scorer_board
    assert top_scorer_board(_scorer_db(), "epl", n_sims=1000, seed=3) == []


def test_top_scorer_does_not_count_a_walkover_as_a_chance_to_score():
    """An awarded/walkover fixture is decided off the pitch — nobody will play it, so
    it must not appear in a striker's remaining matches (the same class of double-count
    that inflated the season sim)."""
    from prediction_market_soccer.model.top_scorer import top_scorer_board
    c = _scorer_db(n_remaining=0)
    for i, status in enumerate(("NS", "AWD", "WO", "CANC", "FT")):
        clubctx.seed_fixture(c, 200 + i, clubctx.ARSENAL, clubctx.IPSWICH,
                             status=status, days_ago=-(i + 1))
    _scorer(c, 1, "S. Burst", clubctx.ARSENAL, goals=2, apps=1)
    row = top_scorer_board(c, "epl", n_sims=1000, seed=3)[0]
    assert row["matches_left"] == 1          # only the NS fixture is still to be played


# ── smart_exit: regulation-window cash-out (no extra-time ticks) ──────────────
def _seed_smart_exit_fixture(c, fid, round_name, *, et_ticks: bool):
    """A knockout fixture level 0-0 through 90' with a price overshoot ONLY in extra time.

    NB: price_tick.rel_min is WALL-CLOCK minutes since kickoff (not match minutes), so a
    90' regulation game — 90 play + ~15 half-time + stoppage — runs to rel_min ~110-115,
    and real extra time lands at rel_min ~120-150. smart_exit scans up to _CASHOUT_MAX_RELMIN
    (~115, regulation) so the ET overshoot (seeded beyond 120 below) must NOT trigger a
    cash-out. The pre-regulation ticks sit calm near fair, so nothing fires inside 90'."""
    store.upsert(c, "fixture", {
        "api_id": fid, "round": round_name, "status_short": "AET" if et_ticks else "FT",
        "home_api_id": 1, "away_api_id": 2,
        "home_goals": 1 if et_ticks else 0, "away_goals": 0,  # AET: ET goal makes it 1-0 final
        "updated_at": store.utcnow()}, pk=["api_id"])
    # Goal event only in extra time (minute 105) — regulation score is 0-0.
    if et_ticks:
        c.execute("INSERT INTO fixture_event (fixture_api_id, seq, minute, team_api_id, type, detail) "
                  "VALUES (?,?,?,?,?,?)", (fid, 0, 105, 1, "Goal", None))
    # Pre-90' ticks: a CALM market that tracks the declining 0-0 fair (the favourite's home
    # price falls as a level game runs down and the draw mass rises) — so it never overshoots
    # fair+margin inside regulation. Kept comfortably UNDER the live fair at every minute.
    base_ts = 1_700_000_000
    rows = []
    for m in range(5, 91, 5):                 # 5'..90'
        calm = max(0.02, 0.28 - 0.0030 * m)   # 0.27 @5' → 0.01-floored late; always < fair
        rows.append((fid, "home", base_ts + m * 60, m, calm, "poly_global"))
    # Extra-time overshoot ticks at WALL-CLOCK rel_min 120..150 (real ET window; post-90'
    # settlement / stale for the 90' market). The ~115 regulation cap must exclude these.
    if et_ticks:
        for m in range(120, 151, 5):
            rows.append((fid, "home", base_ts + m * 60, m, 0.95, "poly_global"))
    c.executemany("INSERT INTO price_tick (fixture_api_id, side, ts, rel_min, price, venue) "
                  "VALUES (?,?,?,?,?,?)", rows)
    c.commit()


def test_smart_exit_ignores_extra_time_overshoot():
    """KNOCKOUT BUG FIX: a price overshoot that happens only in EXTRA TIME (wall-clock
    rel_min>120) must never fire the cash-out — the 90' 3-way contract already settled at
    90'. With the scan capped at the regulation window (~115), the ET spike stays invisible."""
    c = _mem_db()
    _seed_smart_exit_fixture(c, 200, "Round of 16", et_ticks=True)
    sm = _toy_strength({"home": 0.2, "away": 0.0})  # near-even toy ratings (keys are team ids)
    from prediction_market_soccer.strategy.smart_exit import smart_exit_cashout
    # pick 'home' entered at 34¢; regulation ticks ~0.34 never overshoot fair+margin → None.
    out = smart_exit_cashout(c, sm, 200, "home", 34.0, "home", "away", "Round of 16", won=True)
    assert out is None, out   # ET overshoot ignored → held to FT (no phantom cash-out)


def test_smart_exit_fires_inside_regulation():
    """Positive control: a genuine in-regulation overshoot (price >> fair+margin before 90')
    DOES fire the cash-out, locking the overshoot. Confirms the window cap didn't disable it."""
    c = _mem_db()
    store.upsert(c, "fixture", {
        "api_id": 201, "round": "Round of 32", "status_short": "FT",
        "home_api_id": 1, "away_api_id": 2, "home_goals": 1, "away_goals": 0,
        "updated_at": store.utcnow()}, pk=["api_id"])
    # Real regulation goal at 20' → home leads; market overshoots to 0.97 at 60'.
    c.execute("INSERT INTO fixture_event (fixture_api_id, seq, minute, team_api_id, type, detail) "
              "VALUES (?,?,?,?,?,?)", (201, 0, 20, 1, "Goal", None))
    base_ts = 1_700_000_000
    # Calm ticks BELOW fair (market under-pricing home) → never a sell, at any margin; only
    # the genuine 0.97 overshoot should fire. (0.55 sat right at fair+margin for a tuned margin
    # and fired spuriously once the margin tightened — keep the control unambiguous.)
    rows = [(201, "home", base_ts + m * 60, m, 0.40, "poly_global") for m in range(5, 56, 5)]
    rows += [(201, "home", base_ts + 60 * 60, 60, 0.97, "poly_global")]   # 60' overshoot
    rows += [(201, "home", base_ts + m * 60, m, 0.9, "poly_global") for m in range(65, 91, 5)]
    c.executemany("INSERT INTO price_tick (fixture_api_id, side, ts, rel_min, price, venue) "
                  "VALUES (?,?,?,?,?,?)", rows)
    c.commit()
    sm = _toy_strength({"home": 0.6, "away": 0.0})   # home favoured + 1-0 up → fair well below 0.97
    from prediction_market_soccer.strategy.smart_exit import smart_exit_cashout
    out = smart_exit_cashout(c, sm, 201, "home", 55.0, "home", "away", "Round of 32", won=True)
    assert out is not None
    assert out["sold_min"] <= 95                 # sold inside regulation
    assert out["sold_c"] >= 90.0                 # locked the overshoot
