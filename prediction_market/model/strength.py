"""Team strength model — the shared base for all three forecast categories
(plan 03 §1, 10 §5.1).

A single latent rating R_i per team drives a Poisson rating model:

    lambda_home = exp(base_mu + home_adv*[host] + beta*(R_home - R_away))
    lambda_away = exp(base_mu                    - beta*(R_home - R_away))

Ratings are initialised from the pre-tournament FIFA rank and then
REVERSE-FITTED so that the model-implied expected group points match the
external prior's ``exp_points`` (plan 10 §5.1). The fit is analytic
(coordinate-descent bisection on each rating against expected points computed
from the Dixon-Coles kernel) — no Monte Carlo inside the calibration loop, so
it is fast and deterministic.

NOTE (honesty): ``sigma`` (posterior uncertainty, plan 03 §1 output) is a
placeholder constant here; the real per-market dispersion comes from the
ensemble layer (plan 10 §5.3), which is not yet built.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from prediction_market.config import CONFIG, ModelConfig
from prediction_market.ingest.prior_ingest import PriorSnapshot, load_prior, team_id
from prediction_market.model.dixon_coles import score_matrix, wdl

# Placeholder posterior sigma until the ensemble layer (plan 10) supplies real
# dispersion. Used by the sizing layer as p_eff = p - k*sigma.
_PLACEHOLDER_SIGMA = 0.04


@dataclass
class StrengthModel:
    """Calibrated team ratings + the parameters needed to price any match."""

    ratings: dict[str, float]            # team_id -> latent rating R_i
    sigma: dict[str, float]              # team_id -> posterior std (placeholder for now)
    host_ids: frozenset[str]
    cfg: ModelConfig
    # Optional alt-data λ adjustments (plan 19): team_id -> TeamAdj(def_z/off_z/xga_z).
    # None ⇒ no adjustment (the default everywhere → prod unchanged unless attached
    # AND a weight is non-zero). Attached last in build_strength_live so blends are
    # already applied to ratings.
    adj: dict | None = None

    def _adj_lambdas(self, lam_i: float, lam_j: float, i: str, j: str) -> tuple[float, float]:
        """Apply the bounded alt-data λ multipliers (plan 19). A team's defence /
        xGA suppresses the OPPONENT's λ; its attack form raises its OWN λ. The total
        log-adjustment per side is clipped to ±cfg.adj_log_clip so no signal dominates.
        Returns the lambdas unchanged when no adj is attached or all weights are 0."""
        c = self.cfg
        wd = getattr(c, "oppadj_def_weight", 0.0); wo = getattr(c, "oppadj_off_weight", 0.0)
        wx = getattr(c, "xga_weight", 0.0)
        if not self.adj or (wd == 0.0 and wo == 0.0 and wx == 0.0):
            return lam_i, lam_j
        ai = self.adj.get(i); aj = self.adj.get(j)
        if ai is None and aj is None:
            return lam_i, lam_j
        z = lambda a, f: getattr(a, f, 0.0) if a is not None else 0.0
        clip = getattr(c, "adj_log_clip", 0.40)
        di = -wd * z(aj, "def_z") - wx * z(aj, "xga_z") + wo * z(ai, "off_z")
        dj = -wd * z(ai, "def_z") - wx * z(ai, "xga_z") + wo * z(aj, "off_z")
        di = max(-clip, min(clip, di)); dj = max(-clip, min(clip, dj))
        return lam_i * math.exp(di), lam_j * math.exp(dj)

    def pair_lambdas(
        self, i: str, j: str, *, knockout: bool = False
    ) -> tuple[float, float]:
        """(lambda_i, lambda_j) for team i vs team j.

        Host advantage applies only when one side is a host nation (group stage;
        knockouts treated as neutral in v1). Knockout games scale lambda down
        (more cautious, fewer goals — plan 03 §4a).
        """
        ri, rj = self.ratings[i], self.ratings[j]
        mu, beta = self.cfg.base_mu, self.cfg.beta
        i_host, j_host = i in self.host_ids, j in self.host_ids

        if knockout:
            i_host = j_host = False  # neutral venues in knockout (v1 simplification)

        if j_host and not i_host:
            d = rj - ri
            lam_j = math.exp(mu + self.cfg.home_adv + beta * d)
            lam_i = math.exp(mu - beta * d)
        else:
            ha = self.cfg.home_adv if i_host else 0.0
            d = ri - rj
            lam_i = math.exp(mu + ha + beta * d)
            lam_j = math.exp(mu - beta * d)

        if knockout:
            s = self.cfg.knockout_lambda_scale
            lam_i, lam_j = lam_i * s, lam_j * s
        return self._adj_lambdas(lam_i, lam_j, i, j)


def _mean_std(d: dict[str, float]) -> tuple[float, float]:
    vals = list(d.values())
    mu = sum(vals) / len(vals)
    sd = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5 or 1.0
    return mu, sd


def _zscore(d: dict[str, float]) -> dict[str, float]:
    mu, sd = _mean_std(d)
    return {k: (v - mu) / sd for k, v in d.items()}


def _rank_to_rating(rank: int, decay: float, top_tier_floor: int = 1) -> float:
    """Monotone initial rating from FIFA rank (rank 1 strongest).

    Centred so the median-ranked team is ~0. Uses a log-rank transform so the
    gap between top teams is larger than between minnows.

    top_tier_floor: ranks ABOVE this are clamped to it, so the top tier (ranks
    1..floor) share one rank-rating and are differentiated only by exp_points /
    form / roster — deliberately flattening the very top (the log transform alone
    over-separates #1 from #4). floor=1 = no clamp (original behaviour).
    """
    eff = max(rank, top_tier_floor)
    return -decay * (math.log(eff) - math.log(20)) / math.log(2) * 40.0


def _expected_points(
    ratings: dict[str, float],
    groups: dict[str, list[str]],
    host_ids: frozenset[str],
    cfg: ModelConfig,
) -> dict[str, float]:
    """Analytic expected group points for every team given current ratings."""
    out: dict[str, float] = {}
    tmp = StrengthModel(ratings=ratings, sigma={}, host_ids=host_ids, cfg=cfg)
    for members in groups.values():
        for i in members:
            pts = 0.0
            for j in members:
                if i == j:
                    continue
                lam_i, lam_j = tmp.pair_lambdas(i, j, knockout=False)
                m = score_matrix(lam_i, lam_j, cfg.dc_rho, cfg.score_matrix_kmax)
                pw_i, pd, _ = wdl(m)
                pts += 3.0 * pw_i + pd
            out[i] = pts
    return out


def build_strength(
    prior: PriorSnapshot | None = None,
    cfg: ModelConfig | None = None,
    *,
    sweeps: int = 40,
    tol: float = 1e-3,
) -> StrengthModel:
    """Calibrate ratings by reverse-fitting expected group points to the prior.

    Coordinate descent: for each team, bisect its rating so its model-implied
    expected points equal the prior target (monotone in the rating). Repeat
    sweeps until the max rating update is below ``tol`` or ``sweeps`` reached.
    """
    prior = prior or load_prior()
    cfg = cfg or CONFIG.model

    groups = prior.draw()  # group_code -> [team_id]
    targets = {t.team_id: t.exp_points for t in prior.teams}
    host_ids = frozenset(team_id(n) for n in cfg.host_nations)

    ratings = {
        t.team_id: _rank_to_rating(t.fifa_rank, cfg.rank_strength_decay, cfg.rank_top_tier_floor)
        for t in prior.teams
    }

    bound = cfg.rating_bound
    for _ in range(sweeps):
        max_delta = 0.0
        for members in groups.values():
            for i in members:
                target = targets[i]
                lo, hi = -bound, bound
                # Bisect rating of team i; expected points monotone increasing in R_i.
                for _ in range(30):
                    mid = 0.5 * (lo + hi)
                    ratings[i] = mid
                    ep = _expected_points(ratings, {"_": members}, host_ids, cfg)[i]
                    if ep < target:
                        lo = mid
                    else:
                        hi = mid
                new_r = 0.5 * (lo + hi)
                max_delta = max(max_delta, abs(new_r - ratings[i]))
                ratings[i] = new_r
        if max_delta < tol:
            break

    # Fuse with the FIFA-rank absolute-strength anchor (plan 03 §1). The
    # exp_points fit conflates strength with group difficulty (over-rates teams
    # in weak groups, e.g. Brazil); the rank anchor restores absolute ordering.
    # Both are z-scored to a common scale before blending so the weight is
    # meaningful, then rescaled to the exp-points fit's spread.
    w = cfg.rank_anchor_weight
    if w > 0:
        rank_r = {t.team_id: _rank_to_rating(t.fifa_rank, cfg.rank_strength_decay, cfg.rank_top_tier_floor)
                  for t in prior.teams}
        fit_z = _zscore(ratings)
        rank_z = _zscore(rank_r)
        fit_mu, fit_sd = _mean_std(ratings)
        b = cfg.rating_bound
        for tid in ratings:
            blended_z = (1 - w) * fit_z[tid] + w * rank_z[tid]
            ratings[tid] = max(-b, min(b, fit_mu + fit_sd * blended_z))

    sigma = {tid: _PLACEHOLDER_SIGMA for tid in ratings}
    return StrengthModel(ratings=ratings, sigma=sigma, host_ids=host_ids, cfg=cfg)


def update_with_results(
    sm: StrengthModel,
    results: list[dict],
    *,
    lr: float = 0.05,
    time_decay_xi: float | None = None,
) -> StrengthModel:
    """Nudge ratings by actual vs expected goals (plan 03 §1b/§7).

    For each played match, a team's over/under-performance is
    ``(goals_for − E[goals_for]) − (goals_against − E[goals_against])``; the
    rating moves by ``lr × time-weighted-mean performance``. The learning rate is
    deliberately small (early-tournament results are noisy, plan 03 §7) and the
    update is clipped to the rating bound.

    ``results``: list of ``{home_id, away_id, home_goals, away_goals, days_ago}``.
    NOTE: uses goals; plan 03 §7 prefers xG (e.g. a 0:1 loss dominating on xG
    should not crater attack) — wire xG here once fixture stats are ingested.
    """
    cfg = sm.cfg
    xi = cfg.time_decay_xi if time_decay_xi is None else time_decay_xi
    acc = {t: 0.0 for t in sm.ratings}
    wsum = {t: 0.0 for t in sm.ratings}
    for r in results:
        i, j = r["home_id"], r["away_id"]
        gh, ga = r.get("home_goals"), r.get("away_goals")
        if i not in sm.ratings or j not in sm.ratings or gh is None or ga is None:
            continue
        lam_i, lam_j = sm.pair_lambdas(i, j)
        w = math.exp(-xi * float(r.get("days_ago", 0) or 0))
        perf_i = (gh - lam_i) - (ga - lam_j)   # >0 ⇒ home over-performed
        acc[i] += w * perf_i;  wsum[i] += w
        acc[j] += w * (-perf_i); wsum[j] += w

    new_ratings = dict(sm.ratings)
    b = cfg.rating_bound
    for t in new_ratings:
        if wsum[t] > 0:
            new_ratings[t] = max(-b, min(b, new_ratings[t] + lr * acc[t] / wsum[t]))
    return StrengthModel(ratings=new_ratings, sigma=dict(sm.sigma), host_ids=sm.host_ids, cfg=cfg)


def update_strength_from_store(sm: StrengthModel, conn=None, *, lr: float = 0.05) -> StrengthModel:
    """Apply update_with_results using finished fixtures in the local store."""
    from datetime import datetime, timezone

    from prediction_market.ingest import store

    conn = conn or store.init_db()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    now = datetime.now(timezone.utc)
    results = []
    for r in conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals, kickoff_ts FROM fixture "
        "WHERE status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL"):
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai):
            continue
        days_ago = 0.0
        if r["kickoff_ts"]:
            try:
                days_ago = max(0.0, (now - datetime.fromisoformat(r["kickoff_ts"])).total_seconds() / 86400)
            except ValueError:
                pass
        results.append({"home_id": hi, "away_id": ai, "home_goals": r["home_goals"],
                        "away_goals": r["away_goals"], "days_ago": days_ago})
    return update_with_results(sm, results, lr=lr)


if __name__ == "__main__":
    prior = load_prior()
    sm = build_strength(prior)
    ep = _expected_points(sm.ratings, prior.draw(), sm.host_ids, sm.cfg)
    print("Calibrated ratings (reverse-fit to prior expected points):")
    for t in sorted(prior.teams, key=lambda x: -sm.ratings[x.team_id])[:8]:
        print(
            f"  {t.name:<16} rank={t.fifa_rank:>2}  R={sm.ratings[t.team_id]:+.3f}  "
            f"exp_pts model={ep[t.team_id]:.2f} target={t.exp_points:.2f}"
        )
