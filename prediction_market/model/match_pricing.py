"""Single-match pricing (plan 03 §2/§3) — derives every single-game market
from the Dixon-Coles score matrix for a given pair of teams.

Used to price the group-stage fixtures from the draw (a demonstration set, since
the real fixture list + kickoff times come from API-Football, plan 02). The same
function prices any pair, group or knockout.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from prediction_market.config import CONFIG
from prediction_market.ingest.prior_ingest import PriorSnapshot, load_prior
from prediction_market.model.dixon_coles import (
    both_teams_score,
    knockout_advance_prob,
    over_under,
    score_matrix,
    wdl,
)
from prediction_market.model.strength import StrengthModel, build_strength


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
                venue_name: str | None = None) -> MatchPrice:
    cfg = sm.cfg
    lam_h, lam_a = sm.pair_lambdas(home_id, away_id, knockout=knockout)
    # Venue-climate suppression (plan 19): symmetric λ trim for altitude/heat. No-op
    # when venue_name is None or venue_climate_weight=0 (the default → prod unchanged).
    vw = getattr(cfg, "venue_climate_weight", 0.0)
    if venue_name and vw:
        import math as _math
        from prediction_market.model.venue_climate import venue_log_suppression
        sup = venue_log_suppression(venue_name, vw)
        if sup:
            lam_h *= _math.exp(-sup); lam_a *= _math.exp(-sup)
    m = score_matrix(lam_h, lam_a, cfg.dc_rho, cfg.score_matrix_kmax)
    p_home, p_draw, p_away = wdl(m)
    p_over, p_under, _ = over_under(m, 2.5)

    p_adv = None
    if knockout:
        from prediction_market.model.penalties import shootout_win_prob
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
                           knockout: bool = False, cal: dict | None = None) -> MatchPrice:
    """price_match, then apply the fitted probability calibration to the 3-way
    (model/probability_calibration.py). This is the model's CALIBRATED view — what
    the live exports and the trade-grade gate should use. O2.5/BTTS are left as-is."""
    from prediction_market.model.probability_calibration import apply_calibration, load_calibration
    mp = price_match(sm, home_id, away_id, knockout=knockout)
    cal = cal if cal is not None else load_calibration()
    if not cal:
        return mp
    # The draw-mass boost is a GROUP-STAGE correction only — a knockout cannot end
    # level (extra time + shootout decide a winner), so it must not inflate the draw.
    ph, pd, pa = apply_calibration([mp.p_home, mp.p_draw, mp.p_away], cal, knockout=knockout)
    return replace(mp, p_home=ph, p_draw=pd, p_away=pa)


def is_knockout(round_name: str | None) -> bool:
    """A WC round is knockout unless it is the group stage (API-Football names group
    rounds 'Group Stage - N'; everything else — Round of 32/16, QF, SF, Final — is KO)."""
    return bool(round_name) and "group" not in round_name.lower()


def price_group_stage(sm: StrengthModel, prior: PriorSnapshot) -> list[MatchPrice]:
    """Price all intra-group pairings (demonstration fixture set)."""
    out: list[MatchPrice] = []
    for members in prior.draw().values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                out.append(price_match(sm, members[i], members[j], knockout=False))
    return out


if __name__ == "__main__":
    prior = load_prior()
    sm = build_strength(prior)
    # Spotlight a marquee group fixture.
    mp = price_match(sm, "spain", "uruguay")
    print(f"Spain vs Uruguay: home={mp.p_home:.3f} draw={mp.p_draw:.3f} away={mp.p_away:.3f} "
          f"O2.5={mp.p_over_2_5:.3f} BTTS={mp.p_btts:.3f} (lam {mp.lam_home:.2f}/{mp.lam_away:.2f})")
    print(f"priced {len(price_group_stage(sm, prior))} group fixtures")
