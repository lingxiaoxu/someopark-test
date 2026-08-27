"""Single-match pricing — derives every single-game market from the Dixon-Coles
score matrix for a given pair of clubs (club edition).

C1 (TRANSFORM_PLAN §3.0): ``is_knockout`` is REGISTRY-DRIVEN — the WC module's
``"group" not in round`` substring guess is gone. Fixture-level pricing reads
the real fixture calendar (``price_upcoming_fixtures``), not a draw.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import Stage, active, by_api_id, stage_of
from prediction_market_soccer.ingest.club_prior import ClubPriorSnapshot, load_prior
from prediction_market_soccer.model.dixon_coles import (
    both_teams_score,
    knockout_advance_prob,
    over_under,
    score_matrix,
    two_leg_advance_prob,
    wdl,
)
from prediction_market_soccer.model.strength import StrengthModel, build_strength


@dataclass(frozen=True)
class MatchPrice:
    home_id: str
    away_id: str
    knockout: bool
    p_home: float
    p_draw: float
    p_away: float
    p_over_2_5: float
    p_under_2_5: float
    p_btts: float
    lam_home: float
    lam_away: float
    # Knockout-only: probability home advances (incl. ET + penalties).
    p_home_advance: float | None = None


def price_match(sm: StrengthModel, home_id: str, away_id: str, *, knockout: bool = False,
                venue_name: str | None = None, host_neutral: bool | None = None,
                lam_mult: tuple[float, float] | None = None) -> MatchPrice:
    cfg = sm.cfg
    # host_neutral decoupled from knockout: callers price the 90' market with knockout=False
    # (group-style draw/λ calibration) but pass host_neutral=is_knockout(round) so a host loses
    # its home-soil edge on a neutral KO venue. Defaults to `knockout` when not given.
    lam_h, lam_a = sm.pair_lambdas(home_id, away_id, knockout=knockout, host_neutral=host_neutral)
    # Optional motivation tilt (model/motivation.py): a modest λ scale for group-progression
    # psychology. No-op when lam_mult is None (the default → calibration / OOS / sim unchanged).
    if lam_mult:
        lam_h *= lam_mult[0]; lam_a *= lam_mult[1]
    # Venue-climate suppression (plan 19): symmetric λ trim for altitude/heat. No-op
    # when venue_name is None or venue_climate_weight=0 (the default → prod unchanged).
    # Altitude. The World Cup premise was a SYMMETRIC suppression (thin air ⇒ fewer
    # goals for both sides); the club fit says the opposite — matches at ≥2,800 m
    # produce MORE goals (2.88 vs 2.32 per match) and the effect that survives is an
    # extra edge for the acclimatised HOME side, so it is applied anti-symmetrically.
    # Zero weight (the default) and any venue below the fitted band leave λ untouched,
    # so a European or Brazilian fixture cannot be moved by this line.
    vw = getattr(cfg, "venue_climate_weight", 0.0)
    if venue_name and vw:
        import math as _math
        from prediction_market_soccer.model.venue_climate import altitude_home_log_edge
        edge = altitude_home_log_edge(venue_name, vw)
        if edge:
            lam_h *= _math.exp(edge); lam_a *= _math.exp(-edge)
    m = score_matrix(lam_h, lam_a, cfg.dc_rho, cfg.score_matrix_kmax)
    p_home, p_draw, p_away = wdl(m)
    p_over, p_under, _ = over_under(m, 2.5)

    p_adv = None
    if knockout:
        from prediction_market_soccer.model.penalties import shootout_win_prob
        edge = shootout_win_prob(sm, home_id, away_id)   # team-specific, not flat
        p_adv = knockout_advance_prob(
            lam_h, lam_a, rho=cfg.dc_rho, kmax=cfg.score_matrix_kmax,
            et_fraction=cfg.extra_time_fraction, penalty_home_edge=edge,
        )

    return MatchPrice(
        home_id=home_id, away_id=away_id, knockout=knockout,
        p_home=p_home, p_draw=p_draw, p_away=p_away,
        p_over_2_5=p_over, p_under_2_5=p_under, p_btts=both_teams_score(m),
        lam_home=lam_h, lam_away=lam_a, p_home_advance=p_adv,
    )


def price_match_calibrated(sm: StrengthModel, home_id: str, away_id: str, *,
                           knockout: bool = False, cal: dict | None = None,
                           host_neutral: bool | None = None,
                           lam_mult: tuple[float, float] | None = None) -> MatchPrice:
    """price_match, then apply the fitted probability calibration to the 3-way
    (model/probability_calibration.py). This is the model's CALIBRATED view — what
    the live exports and the trade-grade gate should use. O2.5/BTTS are left as-is.
    lam_mult forwards the optional motivation tilt (default None = unchanged).
    host_neutral (decoupled from knockout) drops a host's home-soil edge on a neutral KO venue."""
    from prediction_market_soccer.model.probability_calibration import apply_calibration, load_calibration
    mp = price_match(sm, home_id, away_id, knockout=knockout, host_neutral=host_neutral, lam_mult=lam_mult)
    cal = cal if cal is not None else load_calibration()
    if not cal:
        return mp
    # The draw-mass boost is a GROUP-STAGE correction only — a knockout cannot end
    # level (extra time + shootout decide a winner), so it must not inflate the draw.
    ph, pd, pa = apply_calibration([mp.p_home, mp.p_draw, mp.p_away], cal, knockout=knockout)
    return replace(mp, p_home=ph, p_draw=pd, p_away=pa)


def is_knockout(round_name: str | None, comp_key: str | None = None) -> bool:
    """Registry-driven knockout test (C1 — replaces the WC substring guess).

    With ``comp_key`` the answer is exact (``stage_of``). Without it (copied
    call sites that only have the round string), the round name is checked
    against EVERY enabled competition's rules — our comps' round vocabularies
    agree on stage semantics ("Regular Season/League Stage/Apertura-N" are
    league everywhere; "Round of 16/Quarter-finals/Final" are KO everywhere),
    so the union answer is well-defined; an unmatched name counts as league
    (advance paths never fire on unknowns, §3.0)."""
    if not round_name:
        return False
    if comp_key:
        return stage_of(comp_key, round_name) in (Stage.CUP_TWO_LEG, Stage.CUP_SINGLE)
    for comp in active():
        st = stage_of(comp.key, round_name)
        if st in (Stage.CUP_TWO_LEG, Stage.CUP_SINGLE):
            return True
        if st == Stage.LEAGUE:
            return False
    return False


def price_upcoming_fixtures(sm: StrengthModel, conn, comp_key: str, *,
                            days: float = 8.0, limit: int = 40) -> list[MatchPrice]:
    """Price the next ``days`` of REAL fixtures for one competition from the
    fixture table (replaces the WC draw-derived ``price_group_stage``).

    Two-legged deciding legs price the advance with the leg-1 aggregate carried
    in (C5); cup singles use the single-leg ET/pens path; league rounds are
    plain 3-way. Clubs missing from the strength model (rare registry gaps) are
    skipped, never guessed."""
    from prediction_market_soccer.config.leagues import caps_for, get
    from prediction_market_soccer.ingest.soccer_ingest import leg_of

    comp = get(comp_key)
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    rows = conn.execute(
        "SELECT api_id, round, home_api_id, away_api_id FROM fixture "
        "WHERE league_id=? AND season=? AND status_short IN ('NS','TBD') "
        "AND kickoff_ts <= datetime('now', ?) AND kickoff_ts >= datetime('now', '-6 hours') "
        "ORDER BY kickoff_ts LIMIT ?",
        (comp.api_football_id, comp.season, f"+{days} days", limit)).fetchall()
    out: list[MatchPrice] = []
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi in sm.ratings and ai in sm.ratings):
            continue
        leg, agg = leg_of(conn, r["api_id"])
        cp = caps_for(comp_key, r["round"], leg=leg)
        ko = cp.advance
        mp = price_match(sm, hi, ai, knockout=ko, host_neutral=cp.neutral)
        if ko and cp.two_leg and leg == 2 and agg:
            try:
                a_h, a_a = (int(x) for x in agg.split("-"))
                # tie table stores agg for team_a (leg-1 home = leg-2 AWAY side).
                # two_leg_advance_prob wants the LEG-2 HOME side's aggregate first.
                p_adv = two_leg_advance_prob(
                    mp.lam_home, mp.lam_away, a_a, a_h,
                    rho=sm.cfg.dc_rho, kmax=sm.cfg.score_matrix_kmax,
                    et_fraction=sm.cfg.extra_time_fraction,
                    penalty_home_edge=0.5, et_then_pens=cp.et_then_pens)
                mp = replace(mp, p_home_advance=p_adv)
            except (ValueError, AttributeError):
                pass
        out.append(mp)
    return out


if __name__ == "__main__":
    from prediction_market_soccer.ingest import store
    conn = store.init_db()
    for lg in ("epl", "ucl"):
        sm = build_strength(load_prior(lg), league=lg)
        priced = price_upcoming_fixtures(sm, conn, lg)
        print(f"— {lg}: {len(priced)} upcoming fixtures priced —")
        for mp in priced[:4]:
            adv = f" adv={mp.p_home_advance:.3f}" if mp.p_home_advance is not None else ""
            print(f"  {mp.home_id} v {mp.away_id}: {mp.p_home:.3f}/{mp.p_draw:.3f}/{mp.p_away:.3f}"
                  f" O2.5={mp.p_over_2_5:.3f}{adv} ko={mp.knockout}")
