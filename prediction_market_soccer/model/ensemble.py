"""model/ensemble.py — parameter uncertainty → the σ the edge shrink actually needs.

WC edition: perturb ``ModelConfig``, push every variant through the 48-team
tournament sim, report the cross-variant std of p_champion / p_advance. Both ends
of that pipe are gone in the club edition — ``model/tournament.py``'s group-of-4
engine has no club analogue, and the SEASON-level dispersion it produced is now
generated inside a single sim by ``league_season`` + ``cfg.season_rating_sigma``
(TRANSFORM_PLAN C4). Re-pointing the old design at clubs would have rebuilt, at
16× the cost, a number the season sim already publishes.

The dispersion this system is genuinely missing is per-MATCH and lives in
PROBABILITY space. ``strategy/edge.compute_edge`` gates on ``p_eff = p − k·σ_p``
and ``strategy/decision_model.decide`` accepts a ``{side: σ_p}`` dict — but no
production caller supplies one (σ_p = 0 ⇒ no shrink at all), except
``exec/executor.py`` which passes a hard-coded 0.04. That 0.04 is
``strength._PLACEHOLDER_SIGMA``, a RATING-unit constant being fed into a
PROBABILITY-unit shrink: a unit error that happens to look plausible. This module
supplies the real quantity — re-price ONE fixture under the model's own parameter
uncertainty and return the std of (home, draw, away).

WHERE THE UNCERTAINTY COMES FROM (four sources, no free constants):

  1. rating gap — σ_R = ``cfg.season_rating_sigma`` · √(P0/(P0+played)), the SAME
     shrinking rating uncertainty the season MC draws, quantised on the SAME
     5-point grid (``league_season._BINS``). One uncertainty story for both
     layers, so the match σ and the title odds can never contradict each other.
  2. per-competition ``base_mu`` / ``home_adv`` — these are FITTED
     (ops/fit_league_params), and their standard errors follow analytically from
     the counts that fit recorded: ha ≈ log(H̄/Ā) and μ ≈ log(Ā) over n matches
     of Poisson goals ⇒ SE(μ) = √(1/(n·Ā)), SE(ha) = √(1/(n·H̄) + 1/(n·Ā)).
     This is the term that separates a 380-match EPL fit from a 108-match UECL
     one — and thin-prior European qualifiers are exactly where the early club
     bets blew up (see decision_model's ABSURD-EDGE GUARD).
  3. ``beta`` / ``dc_rho`` — global knobs chosen by ops/param_select_club's grid,
     so their 1σ is half that grid's own step: we cannot claim to resolve them
     finer than the search that picks them.
  4. nothing else. The WC axes ``rank_strength_decay`` (unused by club
     ``build_strength``) and ``penalty_favorite_edge`` (shootouts only, not the
     90' 3-way) were dropped rather than carried along as decoration.

METHOD — deterministic, no Monte-Carlo noise. A trading gate that flickers
between runs on the same fixture is worse than no gate, so there is no RNG here:
the rating gap (large and nonlinear in λ) gets the exact 5-node weighted
quadrature, and the four small, near-linear parameter axes get a central ±1σ
difference combined in quadrature. ~11 re-prices per fixture, all through
``price_match`` so the σ is the dispersion of the price production actually
quotes (motivation λ tilt and venue-climate suppression included when passed).

NOT A PRICE CHANGE. ``p_mean`` is the uncertainty-averaged price and is reported
for diagnosis only. ``season_rating_sigma`` was deliberately kept out of
single-match pricing (config.py) and this module keeps it out: production still
quotes the point estimate; only the SIZING/GATING layer learns how firm it is.

WHAT THIS σ IS NOT — measured 2026-08-26, both read-only replays, so nobody has
to re-litigate it:

  * It is NOT a claim that today's probabilities are biased. Bucketing 1,304
    settled season-2026 matches by σ shows it does NOT order the realised Brier
    (Spearman −0.04), and that is structural, not a defect: for zero-mean
    parameter noise E[Brier | our own p] is unchanged — only the gap to an
    unobservable oracle grows by Σσ². Brier can never confirm or refute this σ.
  * It is therefore NOT safe to switch on blind. Replaying the 163 settled
    fixtures that have a real PRE venue snapshot through ``decide()``, a 1σ
    haircut at the current ``risk.shrink_k`` = 1.0 cuts 140 bets to 68; realised
    $1-flat ROI moves 16.7% → 17.0% (k=1.0), 31.0% (k=0.5), 9.9% (k=0.25) — every
    band ±17-25pp, i.e. pure noise at this sample size. The decision of WHICH k
    to run is a RiskConfig call the bet log cannot yet settle; the metric that
    will settle it first is CLV, not PnL (decision_model's own docstring).
  * On today's upcoming board σ_p runs 0.01–0.21 (median 0.11 at current-season
    evidence counts). The DISCRIMINATION is the point: it is smallest for a
    mature top-5 fixture and largest for a promoted or first-appearance club with
    no season history — exactly the thin-prior European qualifiers that
    decision_model's ABSURD-EDGE GUARD was hand-written to survive. A caller that
    wants that ordering without the blanket haircut should scale ``theta`` by σ
    rather than pass σ to ``sigma_p``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from prediction_market_soccer.config import CONFIG, ModelConfig
from prediction_market_soccer.config.leagues import active, fitted_params
from prediction_market_soccer.model.strength import StrengthModel

_SIDES = ("home", "draw", "away")

# league_season's 5-point discretisation of the standardised rating-gap shift
# (representatives at the 10/25/50/75/90th percentiles). Weights are the normal
# mass each representative stands for, i.e. between the midpoints of its
# neighbours — exactly the buckets league_season's normal draws fall into. Reusing
# the node set is the point: σ was calibrated against the market WITH this
# discretisation, so match and season must not quantise it differently.
_GAP_NODES = (-1.2816, -0.5244, 0.0, 0.5244, 1.2816)


def _gap_weights() -> tuple[float, ...]:
    edges = [0.5 * (_GAP_NODES[k] + _GAP_NODES[k + 1]) for k in range(len(_GAP_NODES) - 1)]
    phi = [0.5 * (1.0 + math.erf(e / math.sqrt(2.0))) for e in edges]
    cuts = [0.0, *phi, 1.0]
    return tuple(cuts[k + 1] - cuts[k] for k in range(len(_GAP_NODES)))


_GAP_WEIGHTS = _gap_weights()

# Evidence half-life for the rating uncertainty, identical to league_season's _P0:
# σ_eff = σ·√(P0/(P0+played)). Pre-season we know only last season + talent priors;
# by mid-table the results have answered most of the question.
_P0 = 6.0

# 1σ for the two GLOBAL structural knobs = half the step of the grid
# ops/param_select_club searches (beta 0.40/0.55/0.70/0.85, dc_rho ~0.04 apart).
# Selection cannot resolve a parameter finer than its own search resolution, so
# half a step is the honest floor — not a tuned number.
_BETA_SD = 0.075
_RHO_SD = 0.020

# Fallback when a competition's λ constants are INHERITED rather than fitted
# (libertadores / sudamericana: too little league history, so they borrow the
# cross-comp mean). There the error is not a fit SE at all — it is "we do not know
# this competition's home advantage, only how much leagues differ", i.e. the
# cross-competition spread of the fitted values. Computed once from the same file.
_SPREAD_CACHE: dict[str, float] | None = None


def _fitted_spread() -> dict[str, float]:
    """Population std of base_mu / home_adv across the GENUINELY fitted comps."""
    global _SPREAD_CACHE
    if _SPREAD_CACHE is None:
        mus, has = [], []
        for comp in active():
            fp = fitted_params(comp.key)
            if fp.get("inherited") or not fp.get("n_matches"):
                continue
            if fp.get("base_mu") is not None:
                mus.append(float(fp["base_mu"]))
            if fp.get("home_adv") is not None:
                has.append(float(fp["home_adv"]))

        def _sd(v: list[float]) -> float:
            if len(v) < 2:
                return 0.0
            m = sum(v) / len(v)
            return (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5

        _SPREAD_CACHE = {"base_mu": _sd(mus) or 0.15, "home_adv": _sd(has) or 0.13}
    return _SPREAD_CACHE


@dataclass(frozen=True)
class MatchDispersion:
    """One fixture's model uncertainty, in probability space."""

    home_id: str
    away_id: str
    n_prices: int                       # re-prices spent on this fixture
    p_point: dict[str, float]           # the production point estimate (unchanged)
    p_mean: dict[str, float]            # uncertainty-averaged price — DIAGNOSTIC ONLY
    p_sigma: dict[str, float]           # → decide(sigma=...) / compute_edge(sigma_p=)
    rating_sigma: float                 # σ_R after the √(P0/(P0+played)) shrink
    contributions: dict[str, dict[str, float]]   # axis -> per-side σ contribution


def rating_sigma(cfg: ModelConfig | None = None, played: float | tuple | None = None) -> float:
    """σ of ONE club's rating, shrunk by how much of the season has answered it.

    ``played`` is that club's matches played (or the pair's — the mean is used;
    ``None`` ⇒ pre-season, the widest and most conservative value).
    """
    cfg = cfg or CONFIG.model
    base = float(getattr(cfg, "season_rating_sigma", 0.0) or 0.0)
    if base <= 0:
        return 0.0
    if played is None:
        n = 0.0
    elif isinstance(played, (tuple, list)):
        vals = [float(p) for p in played if p is not None]
        n = sum(vals) / len(vals) if vals else 0.0
    else:
        n = float(played)
    return base * (_P0 / (_P0 + max(0.0, n))) ** 0.5


def comp_param_sigma(comp: str | None) -> dict[str, float]:
    """SE of a competition's fitted λ constants, from the counts the fit recorded.

    With λ_home = exp(μ + ha + βd) and λ_away = exp(μ − βd), the βd terms cancel
    over a balanced calendar, so the fit reduces to ha = log(H̄/Ā), μ = log(Ā) on
    n matches of Poisson goals — whose delta-method errors are the expressions
    below. Verified against data/priors/league_params.json: log(1.526/1.224) =
    0.2205 vs the stored EPL home_adv 0.221.
    """
    if not comp:
        return {"base_mu": 0.0, "home_adv": 0.0}
    fp = fitted_params(comp)
    n = fp.get("n_matches")
    h, a = fp.get("mean_home_goals"), fp.get("mean_away_goals")
    if fp.get("inherited") or not n or not h or not a:
        sp = _fitted_spread()
        return {"base_mu": sp["base_mu"], "home_adv": sp["home_adv"]}
    n, h, a = float(n), float(h), float(a)
    v_h, v_a = 1.0 / (n * h), 1.0 / (n * a)
    return {"base_mu": v_a ** 0.5, "home_adv": (v_h + v_a) ** 0.5}


def _p3(sm: StrengthModel, home_id: str, away_id: str, cal: dict | None, kw: dict) -> tuple:
    """(p_home, p_draw, p_away) from the SAME pricing path production quotes."""
    from prediction_market_soccer.model.match_pricing import price_match
    mp = price_match(sm, home_id, away_id, **kw)
    p = [mp.p_home, mp.p_draw, mp.p_away]
    if cal:
        from prediction_market_soccer.model.probability_calibration import apply_calibration
        p = apply_calibration(p, cal, knockout=bool(kw.get("knockout")))
    return tuple(p)


def match_dispersion(
    sm: StrengthModel,
    home_id: str,
    away_id: str,
    *,
    played: float | tuple | None = None,
    cal: dict | None = None,
    knockout: bool = False,
    host_neutral: bool | None = None,
    venue_name: str | None = None,
    lam_mult: tuple[float, float] | None = None,
    rating_sigma_override: float | None = None,
) -> MatchDispersion:
    """Model dispersion for one fixture — σ per outcome, deterministic.

    ``cal`` should be the calibration that will be applied to the price this σ is
    paired with: ``decide()`` shrinks CALIBRATED probabilities, and calibration
    compresses everything toward uniform, so a raw-space σ would over-shrink.
    ``knockout`` / ``host_neutral`` / ``venue_name`` / ``lam_mult`` mirror
    ``price_match`` so the σ measures the dispersion of the exact quantity being
    traded — pass the SAME values the price was quoted with.

    ``rating_sigma_override`` replaces σ_R outright. ``season_rating_sigma``
    bundles today's estimation error with a whole season of drift (injuries,
    transfers, managers), and only the first half applies to tomorrow's match, so
    the default is an upper bound. Once someone splits the two, re-anchor here
    instead of editing this module.
    """
    for cid in (home_id, away_id):
        if cid not in sm.ratings:
            raise ValueError(f"{cid} is not in this strength model (comp={sm.comp})")
    cfg = sm.cfg
    kw = {"knockout": knockout, "host_neutral": host_neutral,
          "venue_name": venue_name, "lam_mult": lam_mult}

    p_point = _p3(sm, home_id, away_id, cal, kw)

    # ── 1. rating gap: exact 5-node weighted quadrature (large + nonlinear in λ) ──
    # λ depends on the pair ONLY through d = R_home − R_away, so shifting the home
    # rating by δ shifts the gap by δ — no need to perturb both sides.
    sig_r = rating_sigma(cfg, played) if rating_sigma_override is None else float(rating_sigma_override)
    sd_gap = sig_r * math.sqrt(2.0)
    n_prices = 1
    if sd_gap > 0:
        nodes = []
        for z in _GAP_NODES:
            smz = replace(sm, ratings={**sm.ratings, home_id: sm.ratings[home_id] + z * sd_gap})
            nodes.append(_p3(smz, home_id, away_id, cal, kw))
            n_prices += 1
        p_mean = tuple(sum(w * nd[k] for w, nd in zip(_GAP_WEIGHTS, nodes)) for k in range(3))
        var = [sum(w * (nd[k] - p_mean[k]) ** 2 for w, nd in zip(_GAP_WEIGHTS, nodes))
               for k in range(3)]
    else:
        p_mean = p_point
        var = [0.0, 0.0, 0.0]
    contributions = {"rating_gap": {s: var[k] ** 0.5 for k, s in enumerate(_SIDES)}}

    # ── 2-3. the four small parameter axes: central ±1σ, added in quadrature ──
    # These are near-linear over ±1 SE, so a two-point difference captures their
    # variance without paying for a full product grid.
    cs = comp_param_sigma(sm.comp)
    axes = (
        ("beta", _BETA_SD, "cfg"),
        ("dc_rho", _RHO_SD, "cfg"),
        ("base_mu", cs["base_mu"], "model"),      # per-comp fit error…
        ("home_adv", cs["home_adv"], "model"),    # …vanishes by itself on a neutral venue
    )
    for name, sd, where in axes:
        if sd <= 0:
            contributions[name] = {s: 0.0 for s in _SIDES}
            continue
        cur = getattr(cfg, name) if where == "cfg" else (sm._mu if name == "base_mu" else sm._ha)
        legs = []
        for sign in (+1.0, -1.0):
            val = cur + sign * sd
            smv = (replace(sm, cfg=replace(cfg, **{name: val})) if where == "cfg"
                   else replace(sm, **{name: val}))
            legs.append(_p3(smv, home_id, away_id, cal, kw))
            n_prices += 1
        half = [0.5 * (legs[0][k] - legs[1][k]) for k in range(3)]
        contributions[name] = {s: abs(half[k]) for k, s in enumerate(_SIDES)}
        for k in range(3):
            var[k] += half[k] ** 2

    return MatchDispersion(
        home_id=home_id, away_id=away_id, n_prices=n_prices,
        p_point={s: p_point[k] for k, s in enumerate(_SIDES)},
        p_mean={s: p_mean[k] for k, s in enumerate(_SIDES)},
        p_sigma={s: var[k] ** 0.5 for k, s in enumerate(_SIDES)},
        rating_sigma=sig_r,
        contributions=contributions,
    )


def match_sigma(sm: StrengthModel, home_id: str, away_id: str, **kw) -> dict[str, float]:
    """``{side: σ_p}`` — the literal drop-in for ``decision_model.decide(sigma=...)``
    and ``edge.compute_edge(sigma_p=...)``. See ``match_dispersion`` for the kwargs."""
    return match_dispersion(sm, home_id, away_id, **kw).p_sigma


if __name__ == "__main__":
    # Read-only demo: how firm is the model on a top-league fixture vs a thin-prior
    # European qualifier, before and after a season of evidence? (writes nothing)
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.strength import build_strength

    for comp, played in (("epl", None), ("epl", 30), ("uecl", None)):
        prior = load_prior(comp)
        sm = build_strength(prior, league=comp)
        order = sorted(prior.teams, key=lambda t: -sm.ratings[t.club_id])
        h, a = order[0].club_id, order[-1].club_id
        d = match_dispersion(sm, h, a, played=played)
        print(f"— {comp} (played={played}) {h} vs {a}: σ_R={d.rating_sigma:.3f}, "
              f"{d.n_prices} re-prices")
        for s in _SIDES:
            print(f"    {s:<6} p={d.p_point[s]:.3f}  σ_p={d.p_sigma[s]:.4f}   "
                  + "  ".join(f"{k}={v[s]:.4f}" for k, v in d.contributions.items()))
