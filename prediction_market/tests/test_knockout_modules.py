"""Knockout-stage audit regression tests for the SUPPORT modules.

Core design under test (authoritative): the single-match contract (KXWCGAME / fwc) is the
90-MINUTE 3-way for BOTH group and knockout — a 1-1 knockout tie at 90' pays 'Tie'; extra
time + penalties only decide the SEPARATE reach-round / champion / bracket products (which
use p_adv). These tests pin the knockout behaviour of:

  * motivation        — must be a no-op for knockout fixtures (group-only psychology),
  * smart_exit        — cash-out only inside the regulation 90' window (no extra-time ticks),
  * tournament        — p_advance / stage-reach conservation (32→16→8→4→2→1),
  * knockout_bracket  — all 495 third-place combinations resolve; no same-group R32 clash,
  * golden_boot       — eliminated teams frozen at goals-so-far; opponent-aware future_opp.

They are additive and must not regress the group-stage behaviour.
"""
from __future__ import annotations

import sqlite3

import numpy as np

from prediction_market.config import CONFIG
from prediction_market.ingest import store


# ── helpers ──────────────────────────────────────────────────────────────────
def _mem_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init_db(c)
    return c


def _toy_strength(ratings: dict[str, float]):
    from prediction_market.model.strength import StrengthModel
    return StrengthModel(ratings=ratings, sigma={t: 0.1 for t in ratings},
                         host_ids=frozenset(), cfg=CONFIG.model)


# ── motivation: knockout no-op ───────────────────────────────────────────────
def test_motivation_noop_on_knockout():
    """The group-progression tilt must NOT fire on knockout rounds — _round_num returns
    None for any non-'Group' round, so motivation_multipliers short-circuits to neutral."""
    from prediction_market.model import motivation as M

    for rnd in ("Round of 32", "Round of 16", "Quarter-finals", "Semi-finals", "Final",
                "round of 16", None, ""):
        assert M._round_num(rnd) is None, rnd

    # End-to-end: even with motiv enabled + a conn, a knockout round returns neutral.
    c = _mem_db()
    cfg = CONFIG.model
    mh, ma, info = M.motivation_multipliers(
        c, {"brazil": 1, "ghana": 80}, "brazil", "ghana", "Round of 16", cfg)
    assert (mh, ma, info) == (1.0, 1.0, None)


def test_motivation_still_fires_on_group():
    """Regression guard: the group-stage rounds still parse (no over-broad KO guard)."""
    from prediction_market.model import motivation as M
    assert M._round_num("Group Stage - 1") == 1
    assert M._round_num("Group Stage - 2") == 2
    assert M._round_num("Group A - 3") == 3


# ── tournament: knockout conservation + future_opp ───────────────────────────
def test_tournament_stage_conservation():
    """p_advance/stage-reach masses must conserve the bracket cardinality: 32 teams enter
    R32, 16 win into R16, then 8 / 4 / 2 / 1. This pins the whole knockout cascade."""
    from prediction_market.model.tournament import simulate
    from prediction_market.model.strength import build_strength
    from prediction_market.ingest.prior_ingest import load_prior

    prior = load_prior()
    sm = build_strength(prior)
    res = simulate(prior, sm, n_sims=8000, seed=7)

    assert abs(sum(res.p_advance.values()) - 32.0) < 1e-6
    assert abs(sum(res.p_r16.values()) - 16.0) < 1e-6
    assert abs(sum(res.p_qf.values()) - 8.0) < 1e-6
    assert abs(sum(res.p_sf.values()) - 4.0) < 1e-6
    assert abs(sum(res.p_final.values()) - 2.0) < 1e-6
    assert abs(sum(res.p_champion.values()) - 1.0) < 1e-6
    # future_opp consumed by the golden boot — present, right shape, non-negative.
    assert res.future_opp is not None
    assert res.future_opp.shape == (res.n_sims, len(res.team_ids))
    assert not (res.future_opp < 0).any()


def test_eliminated_teams_empty_in_group_stage():
    """No knockout fixtures stored ⇒ nobody eliminated yet (group stage in progress)."""
    from prediction_market.model.tournament import eliminated_teams
    c = _mem_db()
    assert eliminated_teams(c, all_team_ids=["brazil", "ghana"]) == set()


def test_eliminated_teams_knockout_loser_and_nonqualifier():
    """Once the bracket is COMPLETE: the by-score loser is eliminated AND every team not
    drawn into it (group non-qualifier) is eliminated. The toy bracket here is 'complete'
    at 2 participants (knockout_field_size=2) — see the partial-bracket guard test below
    for why a real 48-team WC must wait for all 32."""
    from prediction_market.model.tournament import eliminated_teams
    c = _mem_db()
    # team_meta maps api_id -> canonical id
    for aid, cid in ((1, "brazil"), (2, "ghana"), (3, "spain"), (4, "iran")):
        store.upsert(c, "team_meta", {"api_id": aid, "canonical_team_id": cid,
                                      "updated_at": store.utcnow()}, pk=["api_id"])
    # A settled R32 tie: brazil 2-0 ghana (ghana out by score).
    store.upsert(c, "fixture", {
        "api_id": 100, "round": "Round of 32", "status_short": "FT",
        "home_api_id": 1, "away_api_id": 2, "home_goals": 2, "away_goals": 0,
        "updated_at": store.utcnow()}, pk=["api_id"])
    elim = eliminated_teams(c, all_team_ids=["brazil", "ghana", "spain", "iran"],
                            knockout_field_size=2)
    assert "ghana" in elim          # lost the KO tie
    assert "brazil" not in elim     # advanced
    # spain + iran were never drawn into the (now-complete) knockout bracket → out.
    assert "spain" in elim and "iran" in elim


def test_eliminated_teams_partial_bracket_does_not_strand_field():
    """REGRESSION (prod bug 2026-06-25): API-Football publishes knockout fixtures one at a
    time, so a SINGLE not-yet-played R32 tie can appear while the group stage is still under
    way. With the full field not yet drawn (participants < knockout_field_size), no team may
    be marked a 'non-qualifier' — else 46 of 48 teams get wrongly eliminated, collapsing the
    champion / reach-round / golden-boot views. Settled KO losers, if any, still go out."""
    from prediction_market.model.tournament import eliminated_teams
    c = _mem_db()
    for aid, cid in ((1, "south_africa"), (2, "canada")):
        store.upsert(c, "team_meta", {"api_id": aid, "canonical_team_id": cid,
                                      "updated_at": store.utcnow()}, pk=["api_id"])
    # One UNPLAYED R32 fixture (status NS) — exactly the prod condition.
    store.upsert(c, "fixture", {
        "api_id": 1561329, "round": "Round of 32", "status_short": "NS",
        "home_api_id": 1, "away_api_id": 2, "home_goals": None, "away_goals": None,
        "updated_at": store.utcnow()}, pk=["api_id"])
    field = ["south_africa", "canada"] + [f"team_{i}" for i in range(46)]  # 48-team field
    elim = eliminated_teams(c, all_team_ids=field)   # default knockout_field_size=32
    assert elim == set()            # bracket incomplete → nobody stranded


def test_eliminated_teams_penalty_shootout_winner_flag():
    """A KO tie level after extra time (PEN) reads the API-Football winner flag, not score."""
    import json
    from prediction_market.model.tournament import eliminated_teams
    c = _mem_db()
    for aid, cid in ((1, "brazil"), (2, "ghana")):
        store.upsert(c, "team_meta", {"api_id": aid, "canonical_team_id": cid,
                                      "updated_at": store.utcnow()}, pk=["api_id"])
    raw = json.dumps({"teams": {"home": {"winner": False}, "away": {"winner": True}}})
    store.upsert(c, "fixture", {
        "api_id": 101, "round": "Round of 16", "status_short": "PEN",
        "home_api_id": 1, "away_api_id": 2, "home_goals": 1, "away_goals": 1,
        "raw_json": raw, "updated_at": store.utcnow()}, pk=["api_id"])
    elim = eliminated_teams(c, all_team_ids=["brazil", "ghana"])
    assert "brazil" in elim and "ghana" not in elim   # away won the shootout


# ── knockout_bracket: official 495-combo bracket ─────────────────────────────
def test_all_495_third_place_combos_resolve():
    from itertools import combinations
    from prediction_market.model import knockout_bracket as KB
    total = len(list(combinations(KB.GROUPS, len(KB.THIRD_SLOTS))))
    assert total == 495
    assert len(KB.THIRD_ASSIGNMENT) == 495          # every combo has a valid official matching


def test_r32_has_no_same_group_clash():
    """Official R32 pairings: no group's W meets its own R, and a third's eligible set never
    contains the concrete group on the OTHER side of its match (FIFA same-group avoidance)."""
    from prediction_market.model import knockout_bracket as KB

    def concrete_group(slot):
        return slot[1] if slot[0] in ("W", "R") else None

    for mi, (a, b) in enumerate(KB.R32_MATCHES):
        ga, gb = concrete_group(a), concrete_group(b)
        if ga and gb:
            assert ga != gb, f"M{mi + 73} same-group W/R clash {ga}"
        for s_third, s_other in ((a, b), (b, a)):
            if s_third[0] == "T":
                og = concrete_group(s_other)
                if og is not None:
                    assert og not in s_third[1], (
                        f"M{mi + 73} third eligible set includes opponent group {og}")


def test_assign_slot_lookup_only_valid_masks_filled():
    """The vectorised (4096,8) lookup: every valid 8-group mask maps each slot to a real group
    index (>=0); an invalid mask (not a 495-combination) stays -1."""
    from itertools import combinations
    from prediction_market.model import knockout_bracket as KB
    look = KB.assign_slot_lookup()
    # a real combination → all 8 slots assigned
    combo = next(iter(KB.THIRD_ASSIGNMENT))
    mask = 0
    for letter in combo:
        mask |= 1 << KB.GROUPS.index(letter)
    assert (look[mask] >= 0).all()
    # 9 groups qualifying is not a valid 8-combo mask → row stays all -1
    bad = 0
    for letter in "ABCDEFGHI":
        bad |= 1 << KB.GROUPS.index(letter)
    assert (look[bad] == -1).all()


# ── golden_boot: knockout elimination + opponent-aware projection ────────────
def _toy_tournament(team_ids, n, mp_each, future_opp_each):
    from prediction_market.model.tournament import TournamentResult
    mp = np.full((n, len(team_ids)), mp_each, dtype=np.int8)
    fo = np.full((n, len(team_ids)), future_opp_each, dtype=np.float64)
    return TournamentResult(team_ids=team_ids, n_sims=n, p_champion={}, p_final={}, p_sf={},
                            p_qf={}, p_r16={}, p_advance={}, e_matches={},
                            matches_played=mp, future_opp=fo)


def test_golden_boot_eliminated_team_frozen():
    """An eliminated team's players score no MORE goals (future λ = 0): a non-leading
    eliminated striker lands at ~0% and his E[goals] equals his goals-so-far head start."""
    from prediction_market.model.golden_boot import simulate_golden_boot, Player
    tids = ["alive", "dead"]
    tour = _toy_tournament(tids, 4000, mp_each=5, future_opp_each=2.0)
    players = [
        Player("alive1", "Alive", "alive", 0.6, 1.0, False, goals_so_far=2),
        Player("dead1", "Dead", "dead", 0.6, 1.0, False, goals_so_far=1),  # behind + out
    ]
    gb = simulate_golden_boot(tour, players, seed=3,
                              games_played={"alive": 2, "dead": 2}, eliminated={"dead"})
    assert gb.e_goals["dead1"] == 1.0          # frozen at head start (no future goals)
    assert gb.p_golden_boot["dead1"] < 0.02    # behind + can't add → ~0 boot prob
    assert gb.p_golden_boot["alive1"] > 0.9    # only live contender takes the boot
    assert abs(sum(gb.p_golden_boot.values()) - 1.0) < 1e-6


def test_golden_boot_opponent_aware_future_opp():
    """future_opp scales projected goals: a soft route (future_opp=3) outscores a hard one
    (future_opp=1) for identical strikers, even with equal matches remaining."""
    from prediction_market.model.golden_boot import simulate_golden_boot, Player
    from prediction_market.model.tournament import TournamentResult
    tids = ["soft", "hard"]
    n = 4000
    mp = np.full((n, 2), 5, dtype=np.int8)
    fo = np.zeros((n, 2))
    fo[:, 0] = 3.0   # soft draw
    fo[:, 1] = 1.0   # hard draw
    tour = TournamentResult(team_ids=tids, n_sims=n, p_champion={}, p_final={}, p_sf={},
                            p_qf={}, p_r16={}, p_advance={}, e_matches={},
                            matches_played=mp, future_opp=fo)
    players = [
        Player("s", "Soft striker", "soft", 0.6, 1.0, False, goals_so_far=0),
        Player("h", "Hard striker", "hard", 0.6, 1.0, False, goals_so_far=0),
    ]
    gb = simulate_golden_boot(tour, players, seed=4,
                              games_played={"soft": 2, "hard": 2})
    # E[goals] = mu_eff * future_opp = 0.6*3 vs 0.6*1
    assert gb.e_goals["s"] > gb.e_goals["h"]
    assert abs(gb.e_goals["s"] - 1.8) < 0.15
    assert abs(gb.e_goals["h"] - 0.6) < 0.15


def test_golden_boot_falls_back_to_flat_count_without_future_opp():
    """Backward compat: an older sim with no future_opp projects over the FLAT remaining-match
    count (matches_played - games_played), never crashing."""
    from prediction_market.model.golden_boot import simulate_golden_boot, Player
    from prediction_market.model.tournament import TournamentResult
    tids = ["t"]
    n = 3000
    mp = np.full((n, 1), 5, dtype=np.int8)
    tour = TournamentResult(team_ids=tids, n_sims=n, p_champion={}, p_final={}, p_sf={},
                            p_qf={}, p_r16={}, p_advance={}, e_matches={},
                            matches_played=mp, future_opp=None)
    players = [Player("p", "P", "t", 0.5, 1.0, False, goals_so_far=1)]
    gb = simulate_golden_boot(tour, players, seed=5, games_played={"t": 2})
    # remaining = 5 - 2 = 3; E[goals] = head 1 + 0.5*3 = 2.5
    assert abs(gb.e_goals["p"] - 2.5) < 0.2


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
    from prediction_market.strategy.smart_exit import smart_exit_cashout
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
    from prediction_market.strategy.smart_exit import smart_exit_cashout
    out = smart_exit_cashout(c, sm, 201, "home", 55.0, "home", "away", "Round of 32", won=True)
    assert out is not None
    assert out["sold_min"] <= 95                 # sold inside regulation
    assert out["sold_c"] >= 90.0                 # locked the overshoot
