"""Team strength model — club edition (TRANSFORM_PLAN C2, §3.2/§3.3 base layer).

A single latent rating R_i per club drives a Poisson rating model:

    lambda_home = exp(base_mu + home_adv + beta*(R_home - R_away))
    lambda_away = exp(base_mu             - beta*(R_home - R_away))

The C2 semantic inversion vs the WC module: home advantage applies to EVERY
fixture's home side (real venues), not to three host nations — ``neutral=True``
(finals) is the exception, not the rule. ``base_mu``/``home_adv`` are
per-competition, fitted once from last-season results (ops/fit_league_params)
and loaded via ``leagues.fitted_params``.

Ratings are initialised from the club prior's ``anchor_points`` (expected
points-per-round, §3.2) and REVERSE-FITTED so the model-implied full
double-round-robin points-per-round matches that target — the same analytic
coordinate-descent bisection as the WC module, with the 3-game group replaced
by the season round-robin. The WC ``rank_anchor_weight`` patch (group-difficulty
confound) is gone: a league table has no group-difficulty confound.

Back-compat: ``StrengthModel.host_ids`` survives as an ALWAYS-EMPTY frozenset so
every copied constructor/consumer (squad_strength, xv_monitor, tests) works
unchanged — empty set ⇒ all host branches are structural no-ops.

``sigma`` remains a placeholder; real dispersion comes from the ensemble layer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from prediction_market_soccer.config import CONFIG, ModelConfig
from prediction_market_soccer.config.leagues import fitted_params
from prediction_market_soccer.ingest.club_prior import ClubPriorSnapshot, load_prior, team_id
from prediction_market_soccer.model.dixon_coles import score_matrix, wdl

_PLACEHOLDER_SIGMA = 0.04


@dataclass
class StrengthModel:
    """Calibrated club ratings + the parameters needed to price any match."""

    ratings: dict[str, float]            # club_id -> latent rating R_i
    sigma: dict[str, float]              # club_id -> posterior std (placeholder)
    host_ids: frozenset[str] = frozenset()   # ALWAYS EMPTY (WC back-compat shim)
    cfg: ModelConfig = None              # type: ignore[assignment]
    adj: dict | None = None              # alt-data λ adjustments (plan 19), unchanged
    comp: str | None = None              # competition key (per-league mu/home_adv)
    base_mu: float | None = None         # per-comp override (else cfg.base_mu)
    home_adv: float | None = None        # per-comp override (else cfg.home_adv)

    def __post_init__(self):
        if self.cfg is None:
            self.cfg = CONFIG.model
        if self.comp and (self.base_mu is None or self.home_adv is None):
            fp = fitted_params(self.comp)
            if self.base_mu is None:
                self.base_mu = fp.get("base_mu")
            if self.home_adv is None:
                self.home_adv = fp.get("home_adv")

    @property
    def _mu(self) -> float:
        return self.base_mu if self.base_mu is not None else self.cfg.base_mu

    @property
    def _ha(self) -> float:
        return self.home_adv if self.home_adv is not None else self.cfg.home_adv

    def _altdata_w(self) -> dict:
        """This competition's fitted alt-data weights ({} ⇒ use the cfg globals)."""
        if self.comp is None:
            return {}
        from prediction_market_soccer.config.leagues import altdata_weights
        return altdata_weights(self.comp)

    def _adj_lambdas(self, lam_i: float, lam_j: float, i: str, j: str) -> tuple[float, float]:
        """Bounded alt-data λ multipliers (plan 19) — mechanism unchanged from WC."""
        c = self.cfg
        # PER-COMPETITION weights (ops/fit_altdata_weights): a 34-round top-5 league,
        # a CONMEBOL two-legged cup and a 153-club European qualifying bracket each
        # carry recent form differently, so each competition uses the pair fitted on
        # its OWN history. The ModelConfig globals remain the fallback for a
        # competition with no fitted entry.
        _w = self._altdata_w()
        wd = _w.get("oppadj_def_weight", getattr(c, "oppadj_def_weight", 0.0))
        wo = _w.get("oppadj_off_weight", getattr(c, "oppadj_off_weight", 0.0))
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
        self, i: str, j: str, *, knockout: bool = False, host_neutral: bool | None = None,
        neutral: bool | None = None, rating_shift: float = 0.0,
    ) -> tuple[float, float]:
        """(lambda_i, lambda_j) with i = the HOME side of this fixture.

        Club semantics (C2): ``home_adv`` applies to i on every fixture unless the
        venue is neutral (finals). ``neutral`` is the club-native flag;
        ``host_neutral`` is accepted as a synonym so every copied call site
        (``host_neutral=True`` on finals/KO paths) keeps its meaning — on a
        neutral venue nobody gets the edge. ``knockout=True`` additionally scales
        λ down (cagier cup football; per-comp calibration in Phase 6).

        ``rating_shift`` perturbs the rating GAP (i − j) — used only by the season
        Monte-Carlo to carry parameter uncertainty; 0.0 leaves pricing untouched.
        """
        if neutral is None:
            neutral = bool(host_neutral)
        ri, rj = self.ratings[i], self.ratings[j]
        mu, beta = self._mu, self.cfg.beta
        ha = 0.0 if neutral else self._ha
        d = ri - rj + rating_shift
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


class _PPRGrid:
    """Expected home/away points as a 1-D function of the rating gap d = R_i − R_j.

    Under the model, λs (hence a match's expected points) depend on the pair ONLY
    through d — so the O(n²) fit loop collapses to a precomputed 1-D grid + linear
    interpolation. Without this, a 153-club comp (UECL) needs millions of score
    matrices per fit (the hang caught on the first pipeline run); with it, one grid
    (~1k matrices) serves every evaluation.
    """

    def __init__(self, mu: float, ha: float, cfg: ModelConfig):
        import numpy as np
        b = cfg.rating_bound
        self.lo, self.hi, n = -2.0 * b, 2.0 * b, 481
        self.step = (self.hi - self.lo) / (n - 1)
        self.home_pts = np.empty(n)
        self.away_pts = np.empty(n)
        for k in range(n):
            d = self.lo + k * self.step
            lam_h = math.exp(mu + ha + cfg.beta * d)
            lam_a = math.exp(mu - cfg.beta * d)
            m = score_matrix(lam_h, lam_a, cfg.dc_rho, cfg.score_matrix_kmax)
            pw, pd, pl = wdl(m)
            self.home_pts[k] = 3.0 * pw + pd        # i at home with gap d
            self.away_pts[k] = 3.0 * pl + pd        # i away when the HOME side's gap is d… see _at

    def _interp(self, arr, d: float) -> float:
        x = (min(max(d, self.lo), self.hi) - self.lo) / self.step
        k = int(x)
        if k >= len(arr) - 1:
            return float(arr[-1])
        f = x - k
        return float(arr[k] * (1 - f) + arr[k + 1] * f)

    def pts_home(self, d: float) -> float:
        """Expected points for the HOME side at rating gap d = R_home − R_away."""
        return self._interp(self.home_pts, d)

    def pts_away(self, d_home: float) -> float:
        """Expected points for the AWAY side when the home side's gap is d_home."""
        return self._interp(self.away_pts, d_home)


_GRID_CACHE: dict[tuple, _PPRGrid] = {}


def _ppr_grid(cfg: ModelConfig, comp: str | None) -> _PPRGrid:
    fp = fitted_params(comp) if comp else {}
    mu = fp.get("base_mu", cfg.base_mu)
    ha = fp.get("home_adv", cfg.home_adv)
    key = (round(mu, 4), round(ha, 4), cfg.beta, cfg.dc_rho, cfg.score_matrix_kmax, cfg.rating_bound)
    if key not in _GRID_CACHE:
        _GRID_CACHE[key] = _PPRGrid(mu, ha, cfg)
    return _GRID_CACHE[key]


def _expected_ppr(
    ratings: dict[str, float],
    members: list[str],
    cfg: ModelConfig,
    *,
    comp: str | None = None,
    only: str | None = None,
) -> dict[str, float]:
    """Analytic expected POINTS-PER-ROUND over a full home+away double round-robin,
    via the 1-D rating-gap grid (exact up to interpolation; alt-data adj not applied
    here by design — the fit anchors the BASE ratings)."""
    g = _ppr_grid(cfg, comp)
    out: dict[str, float] = {}
    targets = [only] if only else members
    n_opp = 2 * (len(members) - 1)
    for i in targets:
        ri = ratings[i]
        pts = 0.0
        for j in members:
            if i == j:
                continue
            d = ri - ratings[j]
            pts += g.pts_home(d)        # i hosts j
            pts += g.pts_away(-d)       # j hosts i (home gap = R_j − R_i = −d)
        out[i] = pts / max(1, n_opp)
    return out


def build_strength(
    prior: ClubPriorSnapshot | None = None,
    cfg: ModelConfig | None = None,
    *,
    league: str | None = None,
    sweeps: int = 12,
    tol: float = 1e-3,
) -> StrengthModel:
    """Calibrate ratings by reverse-fitting season points-per-round to the club
    prior's ``anchor_points`` (coordinate-descent bisection, same machinery as WC).
    """
    prior = prior or load_prior(league)
    cfg = cfg or CONFIG.model
    comp = prior.league if prior.league != "all" else None

    members = [t.club_id for t in prior.teams]
    targets = {t.club_id: (t.anchor_points if t.anchor_points is not None else 1.2)
               for t in prior.teams}

    # CENTER THE ANCHORS ON WHAT THE MODEL CAN ACTUALLY PRODUCE.
    # A league's average points-per-round is not free: it is fixed by the draw rate
    # this λ level implies (≈1.37 here), while the anchors carry the LEVEL of a real
    # past table (EPL +0.068, Serie A +0.081 above it, Bundesliga −0.024 below).
    # Fitting to an unreachable mean leaves a residual on EVERY club — 0.094 ppr,
    # unchanged by more sweeps because it is the level, not convergence — and the
    # bisection dumps that residual where the points curve is flattest, i.e. at the
    # ends of the table. That is how Coventry (anchor 0.894) ended up rated ABOVE
    # Ipswich (0.936). Only the SPREAD of the anchors carries club information; the
    # level is an artefact of a different season's scoring, so shift it away and let
    # the ordering survive intact.
    if len(members) > 1:
        _flat = dict.fromkeys(members, 0.0)
        _reachable = (sum(_expected_ppr(_flat, members, cfg, comp=comp).values())
                      / len(members))
        _have = sum(targets.values()) / len(targets)
        _shift = _reachable - _have
        if abs(_shift) > 1e-6:
            targets = {k: v + _shift for k, v in targets.items()}

    # initial ratings: z-scored anchor, softly scaled into the rating band
    ratings = {cid: z * 0.8 for cid, z in _zscore(targets).items()}

    bound = cfg.rating_bound
    for _ in range(sweeps):
        max_delta = 0.0
        for i in members:
            target = targets[i]
            lo, hi = -bound, bound
            for _ in range(24):
                mid = 0.5 * (lo + hi)
                ratings[i] = mid
                ep = _expected_ppr(ratings, members, cfg, comp=comp, only=i)[i]
                if ep < target:
                    lo = mid
                else:
                    hi = mid
            new_r = 0.5 * (lo + hi)
            max_delta = max(max_delta, abs(new_r - ratings[i]))
            ratings[i] = new_r
        if max_delta < tol:
            break

    sigma = {cid: _PLACEHOLDER_SIGMA for cid in ratings}
    return StrengthModel(ratings=ratings, sigma=sigma, cfg=cfg, comp=comp)


def update_with_results(
    sm: StrengthModel,
    results: list[dict],
    *,
    lr: float = 0.05,
    time_decay_xi: float | None = None,
) -> StrengthModel:
    """Nudge ratings by actual vs expected goals — mechanism unchanged from WC;
    expectations now correctly include the home side's advantage. With 34–38
    rounds a season this update path is the main in-season signal (§3.2)."""
    cfg = sm.cfg
    xi = cfg.time_decay_xi if time_decay_xi is None else time_decay_xi
    acc = {t: 0.0 for t in sm.ratings}
    wsum = {t: 0.0 for t in sm.ratings}
    for r in results:
        i, j = r["home_id"], r["away_id"]
        gh, ga = r.get("home_goals"), r.get("away_goals")
        if i not in sm.ratings or j not in sm.ratings or gh is None or ga is None:
            continue
        lam_i, lam_j = sm.pair_lambdas(i, j, neutral=bool(r.get("neutral")))
        w = math.exp(-xi * float(r.get("days_ago", 0) or 0))
        perf_i = (gh - lam_i) - (ga - lam_j)
        acc[i] += w * perf_i;  wsum[i] += w
        acc[j] += w * (-perf_i); wsum[j] += w

    new_ratings = dict(sm.ratings)
    b = cfg.rating_bound
    for t in new_ratings:
        if wsum[t] > 0:
            new_ratings[t] = max(-b, min(b, new_ratings[t] + lr * acc[t] / wsum[t]))
    return StrengthModel(ratings=new_ratings, sigma=dict(sm.sigma), cfg=cfg,
                         comp=sm.comp, base_mu=sm.base_mu, home_adv=sm.home_adv, adj=sm.adj)


def update_strength_from_store(sm: StrengthModel, conn=None, *, lr: float = 0.05,
                               league_id: int | None = None) -> StrengthModel:
    """Apply update_with_results using finished fixtures in the local store.

    ``league_id`` restricts to one competition's fixtures (per-comp models);
    None uses every finished fixture whose clubs are in the model."""
    from datetime import datetime, timezone

    from prediction_market_soccer.ingest import store

    conn = conn or store.init_db()
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    now = datetime.now(timezone.utc)
    q = ("SELECT home_api_id, away_api_id, home_goals, away_goals, kickoff_ts FROM fixture "
         "WHERE status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL")
    args: tuple = ()
    if league_id is not None:
        q += " AND league_id=?"
        args = (league_id,)
    results = []
    for r in conn.execute(q, args):
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
    for lg in ("epl", "brasileirao"):
        prior = load_prior(lg)
        sm = build_strength(prior, league=lg)
        members = [t.club_id for t in prior.teams]
        ep = _expected_ppr(sm.ratings, members, sm.cfg, comp=lg)
        print(f"— {lg}: calibrated ratings (reverse-fit to anchor ppr) —")
        for t in sorted(prior.teams, key=lambda x: -sm.ratings[x.club_id])[:6]:
            print(f"  {t.club_id:<26} R={sm.ratings[t.club_id]:+.3f}  "
                  f"ppr model={ep[t.club_id]:.3f} target={t.anchor_points:.3f}")
