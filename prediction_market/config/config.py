"""Central, isolated configuration for the prediction_market project.

Everything this project needs (paths, model parameters, venue settings) is
resolved RELATIVE to ``prediction_market/`` so the project never reaches into
the surrounding someopark-test repo. All output is written under
``prediction_market/data/`` and (optionally) mirrored to the frontend.

Tunable trading/risk parameters live in ``config`` per the plan (05 §7): a
single place, changes leave a trace in git.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Project root = prediction_market/  (this file is prediction_market/config/config.py)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent


@dataclass(frozen=True)
class Paths:
    """All filesystem locations, anchored under prediction_market/."""

    root: Path = PROJECT_ROOT
    data: Path = PROJECT_ROOT / "data"
    priors: Path = PROJECT_ROOT / "data" / "priors"
    output: Path = PROJECT_ROOT / "data" / "output"
    raw_snapshots: Path = PROJECT_ROOT / "data" / "raw"  # append-only API snapshots (Parquet)
    logs: Path = PROJECT_ROOT / "data" / "logs"

    # EA Sports FC 26 player-rating CSVs (Kaggle, CC0). Used as the talent prior
    # for golden-boot scoring rates + squad strength. Append-only raw download.
    fc_raw: Path = PROJECT_ROOT / "data" / "raw" / "fc26"

    # The fully-specified static prior from plan file 10 §2.
    prior_ext_sim_v0: Path = PROJECT_ROOT / "data" / "priors" / "ext_sim_v0.json"

    # Frontend hand-off location (someo-park-investment-management reads JSON from here).
    # We only WRITE prediction-market-specific files; we never touch existing ones.
    frontend_data: Path = REPO_ROOT / "someo-park-investment-management" / "public" / "data"

    def ensure(self) -> None:
        """Create the writable data dirs if missing (idempotent)."""
        for p in (self.data, self.priors, self.output, self.raw_snapshots, self.logs):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class ModelConfig:
    """Modeling parameters (plan files 03, 10). Starting points — tune via backtest."""

    # Monte-Carlo sizing (plan 03 §5/§8). Champion/advance N>=200k for stable tails.
    n_sims_tournament: int = 1_000_000  # PUBLISHED champion run (low Monte-Carlo noise)
    n_sims_quicklook: int = 50_000      # fast correctness pass before scaling up

    # Dixon-Coles low-score correlation parameter (plan 03 §2). rho in (-0.2, 0).
    dc_rho: float = -0.05
    score_matrix_kmax: int = 10  # truncate score matrix at 10-10

    # League baseline log-goal intercept mu and home advantage (plan 03 §2).
    # base_mu = log(baseline goals per side); exp(0.30) ~= 1.35, typical WC group game.
    base_mu: float = 0.30
    # beta maps a team-rating difference to a log-goal difference (Poisson rating model).
    beta: float = 0.40
    rating_bound: float = 2.5      # clip calibrated ratings to [-bound, +bound]
    home_adv: float = 0.15         # per-match host advantage (group only; off in KO via host_neutral)
    host_nations: tuple[str, ...] = ("United States", "Mexico", "Canada")
    # Tournament-long host rating boost: WC hosts historically OVER-perform their ratings (crowd,
    # familiarity, travel, refereeing — every host except RSA 2010 / QAT 2022 reached ≥ R16). A
    # PRIOR-driven nudge, applied a priori in build_strength_live (PIT-safe). Does NOT stack with
    # home_adv: pair_lambdas cancels this baked boost in the per-match λ whenever home_adv applies
    # (group host = home_adv only; knockout host = this rating boost only).
    host_rating_boost: float = 0.10

    # Group round-3 qualification-incentive adjustments (plan 03 §3): teams that
    # have effectively clinched rotate (lower intensity); teams facing
    # elimination push (higher intensity). Applied to round-3 fixtures only.
    r3_incentives: bool = True
    r3_clinch_points: int = 6          # >= this after 2 games ⇒ likely to rotate
    r3_rotation_intensity: float = 0.90
    r3_desperation_intensity: float = 1.06

    # ── Match-motivation λ tilt (group-progression psychology) ──────────────────
    # LIVE betting-only (NOT used in calibration / OOS / the trade-grade gate). A strong
    # team (fifa_rank ≤ motiv_strong_rank) that dropped points early goes all-out against a
    # weaker, NON-host opponent in its remaining group games; a clinched team rotates a
    # little in game 3. Factor 1 (bounce) > factor 2 (rotation). See model/motivation.py.
    motiv_enabled: bool = True
    motiv_strong_rank: int = 24        # "strong team" = top half of 48 by FIFA rank
    motiv_bounce_attack: float = 1.12  # wounded favourite's attacking λ ×, at full intensity
    motiv_bounce_def: float = 0.96     # the weaker opponent's λ ×, at full intensity
    motiv_rotation_attack: float = 0.93  # clinched team's attacking λ × in game 3
    # (conviction-bet knobs live in DecisionConfig — they govern the bet decision, not pricing)

    # Knockout adjustments (plan 03 §4a): more cautious, fewer goals, more draws.
    knockout_lambda_scale: float = 0.85
    # Extra-time goal rate vs regulation (plan 03 §4b): lambda * 30/90.
    extra_time_fraction: float = 30.0 / 90.0
    # Penalty shootout bias toward the stronger/more-experienced side (52-55%).
    penalty_favorite_edge: float = 0.53

    # Rank -> strength prior mapping (plan 10 §5.1). Maps FIFA rank to a goal-
    # expectation prior; reverse-fit against the ext_sim_v0 advance probabilities.
    rank_strength_decay: float = 0.0080  # rank→rating curve shape (z-scored in the anchor,
                                         # so the EFFECTIVE rank influence is rank_anchor_weight)
    rank_top_tier_floor: int = 4         # clamp ranks 1..N to one tier — the top N teams share
                                         # a rank-rating (differentiated only by exp_points/form/
                                         # roster), so #1 sits only slightly above #4. 1 = no clamp

    # Fusion of the absolute-strength anchor (FIFA rank, plan 03 §1a) with the
    # external-sim prior fit (exp_points, §1d). exp_points conflates team strength
    # with GROUP DIFFICULTY (a team in a weak group looks too strong), so we
    # anchor on rank. 1.0 = rank only, 0.0 = exp_points only. 0.8 = rank-led so the
    # world top-4 (clamped to one tier via rank_top_tier_floor) lead the field by
    # ranking, with exp_points/form/roster only ordering them WITHIN the tier.
    # (Deliberately diverges from the sharp champion market per the owner's call:
    # the market over-rates the #1; the top four should be near-equal.)
    rank_anchor_weight: float = 0.8

    # Squad-strength blend (plan 17 B.3): nudge ratings by the z-scored squad index
    # (minutes-weighted club rating + attack). A modest weight is applied to the LIVE
    # model as a forward-looking bet (it slightly raised OOS Brier on the chaotic,
    # draw-heavy early group stage, but should help once the field separates in the
    # knockouts / with more data). 0 = off.
    squad_blend_weight: float = 0.15

    # Recent-form blend (plan 17 B.3): nudge ratings by the z-scored recent-form
    # index (time-weighted, friendly-discounted goal difference from nt_recent).
    # Applied to the LIVE model as a forward-looking bet, same discipline as squad.
    form_blend_weight: float = 0.10

    # EA Sports FC 26 squad-quality blend (plan 17 B.3): nudge ratings by the z-scored
    # top-16 FC overall — a clean, current talent anchor complementary to FIFA rank
    # (results) and the squad club-form blend. Tuned by the param sweep. 0 = off.
    fc_blend_weight: float = 0.12

    # xG-form blend (alpha from the API-Football match-stats pull): nudge ratings by the
    # z-scored net-xG/game from each team's PRIOR WC matches — a lower-noise predictor than
    # the scoreline. Weight set from the WALK-FORWARD study (ops/_xg_alpha_research: OOS
    # Brier 0.604→0.600 at 0.15, monotone improving; goals-form did NOT help), NOT the
    # in-sample sweep — so it can't be over-fit. Applied ONLY in PIT/live prediction paths
    # (build_strength_live(..., xg_form=True, as_of=kickoff)); never in the sweep/calibration
    # so a match never sees its own or later xG. 0 = off.
    xg_form_blend_weight: float = 0.15

    # ── Alternative-data λ adjustments (plan 19) ──────────────────────────────
    # Each is a SMALL, BOUNDED, parameter-controlled multiplier on the Dixon-Coles
    # lambdas (NOT a rating shift — these act asymmetrically on attack vs defence so
    # a bus-parking underdog raises DRAW probability without raising its win odds).
    # ALL DEFAULT 0.0 → the live model is byte-identical until explicitly enabled; the
    # total log-adjustment per match is clipped to ±adj_log_clip so no signal dominates.
    # Opponent-strength-adjusted recent form, split attack / defence (PIT, from nt_recent).
    # ENABLED at an IN-SAMPLE-tuned strength (plan 19, user-directed): on the 19 settled
    # matches this lifts pick accuracy 8→11/19 AND gives the best calibrated Brier (0.498
    # vs 0.571 baseline), entirely from the new alpha (draw-mass dc_rho left at baseline —
    # NO global draw inflation). NOTE: this is IN-SAMPLE optimised on a small sample and
    # WILL partly regress as more matches arrive — re-tune via the PIT walk-forward then.
    oppadj_def_weight: float = 0.45  # a defensively-resilient team lowers its opponent's λ
    oppadj_off_weight: float = 0.25  # a team that scores vs strong defences raises its own λ
    # Recent shot-quality (xGA process metric — cleaner than goals; needs xG history):
    xga_weight: float = 0.0
    # Venue climate (heat / altitude / closed-roof; static stadium table): suppresses goals:
    venue_climate_weight: float = 0.0
    # Confirmed starting-XI strength vs full-squad (rotation/rest detector; needs lineups):
    lineup_weight: float = 0.0
    adj_log_clip: float = 0.60       # max |log λ-adjustment| per side per match (bounds influence)

    # Golden-boot talent prior (plan 03 §6.1): EA FC 26 ratings give each player a
    # talent-grounded per-match goal rate. That FC rate is the PRIOR; observed club
    # (season-1) and WC-to-date scoring update it Bayesian-style with these
    # pseudo-match weights. A strong FC prior stops a 1-game burst (e.g. a weak-team
    # forward scoring twice in the opener) from inflating the forecast — the final
    # boot is dominated instead by talent x knockout depth (matches actually played).
    gb_fc_prior_alpha: float = 3.0      # pseudo-matches of FC-talent prior weight. Lower = the
                                        # ACTUAL tournament scoring dominates sooner, so a star
                                        # who hasn't scored by round 2 regresses out of the top
                                        # (was 8.0, which kept high-talent 0-goal players too high)
    gb_club_weight: float = 0.50        # weight on observed club rate vs FC prior in the talent estimate
    gb_pool_per_team: int = 5           # top-N attacking candidates kept per team for the sim
    gb_teammate_competition: float = 0.35  # secondary: discount a forward's rate when elite
                                           # teammates split the team's goals (France's Mbappé/
                                           # Dembélé/Olise; 2002-Brazil effect). Bounded, non-decisive.

    # Time-decay for recent results (plan 03 §1b): exp(-xi * delta_days).
    time_decay_xi: float = 0.0035

    # Reproducibility: fixed seed family (plan 03 §8).
    random_seed: int = 20260611


@dataclass(frozen=True)
class VenueConfig:
    """Venue endpoints + execution guard rails (plan 01, 07, 09)."""

    # Kalshi REST/WS — recommended external-api hosts (plan 12 §1); demo first.
    # Shared hosts (api.elections.kalshi.com / demo-api.kalshi.co) remain supported.
    kalshi_rest_demo: str = "https://external-api.demo.kalshi.co/trade-api/v2"
    kalshi_ws_demo: str = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
    kalshi_rest_prod: str = "https://external-api.kalshi.com/trade-api/v2"
    kalshi_ws_prod: str = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"

    # Polymarket US (execution + market data) and Global (read-only reference).
    pmus_rest: str = "https://api.polymarket.us"
    pmus_gateway: str = "https://gateway.polymarket.us"  # public gateway (SDK default)
    pmus_ws_markets: str = "wss://api.polymarket.us/v1/ws/markets"
    pmus_ws_private: str = "wss://api.polymarket.us/v1/ws/private"
    # Polymarket Global read-only hosts (no auth; US geoblock on ORDERS only).
    pmglobal_clob: str = "https://clob.polymarket.com"
    pmglobal_gamma: str = "https://gamma-api.polymarket.com"   # discovery / metadata
    pmglobal_data: str = "https://data-api.polymarket.com"     # trades / positions / OI

    # Execution is ONLY ever allowed on these venues (plan 05 venue_guard / 09).
    executable_venues: tuple[str, ...] = ("kalshi", "poly_us")

    # Rate limits (plan 01 §7, 07 §1.5).
    pmus_rest_req_per_min: int = 60

    @property
    def kalshi_env(self) -> str:
        return os.getenv("KALSHI_ENV", "demo").lower()

    @property
    def kalshi_trading_enabled(self) -> bool:
        """Hard gate on live Kalshi order placement (real-money safety).

        Defaults to False; the order layer must refuse to submit unless this is
        explicitly set true in the environment (KALSHI_TRADING_ENABLED=true).
        """
        return os.getenv("KALSHI_TRADING_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")

    @property
    def pmus_trading_enabled(self) -> bool:
        """Hard gate on live Polymarket US order placement (REAL money, no sandbox)."""
        return os.getenv("PMUS_TRADING_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")

    @property
    def kalshi_rest(self) -> str:
        return self.kalshi_rest_prod if self.kalshi_env == "prod" else self.kalshi_rest_demo

    @property
    def kalshi_ws(self) -> str:
        return self.kalshi_ws_prod if self.kalshi_env == "prod" else self.kalshi_ws_demo


@dataclass(frozen=True)
class SoccerConfig:
    """API-Football ingestion settings + request-budget guard rails (plan 02).

    Pro plan = 7,500 requests/DAY (resets at 00:00 UTC). The budget guard counts the
    current UTC DAY (daily_budget) — that's the real constraint; the monthly_budget is
    just a loose backstop. Discipline: pull once, store centrally; per-resource TTLs gate
    the watermark so a re-run within TTL costs ZERO requests.
    """

    api_host: str = "https://v3.football.api-sports.io"
    league_id: int = 1          # FIFA World Cup
    season: int = 2026

    # Budget guard rails — API-Football bills PER DAY (UTC reset), so the daily cap is the
    # real guard. (Was wrongly set to "7000/month", which exhausted mid-tournament.)
    daily_budget: int = 7500            # Pro plan: 7,500 requests/day
    monthly_budget: int = 200000        # loose backstop (~7500/day × 30); daily guard binds
    max_requests_per_run: int = 60      # hard stop per sync invocation (safety)

    # Per-resource freshness TTL in seconds — a sync within TTL is skipped.
    ttl_static: int = 7 * 24 * 3600     # teams / squads / draw — effectively once
    ttl_fixtures: int = 6 * 3600        # fixture list / schedule
    ttl_results: int = 3600             # finished-match results + events (hourly)
    ttl_standings: int = 3600
    ttl_h2h: int = 14 * 24 * 3600       # head-to-head history rarely changes
    ttl_injuries: int = 3600
    ttl_lineups: int = 600              # lineups land ~1h pre-match
    ttl_live: int = 30                  # in-play polling floor (only while matches live)

    @property
    def api_key(self) -> str:
        return os.getenv("API_FOOTBALL_KEY", "")


@dataclass(frozen=True)
class RiskConfig:
    """Position sizing + hard limits (plan 04 §6). Conservative starting points."""

    kelly_fraction: float = 0.25            # fractional Kelly 0.2-0.3
    shrink_k: float = 1.0                   # p_eff = p - k*sigma_p
    min_net_edge: float = 0.03              # theta: single-venue trade threshold
    min_net_lock: float = 0.02              # theta_arb: cross-venue lock threshold
    max_single_market_frac: float = 0.05    # <= 5% bankroll per market
    max_theme_frac: float = 0.10            # <= 10% bankroll per correlated theme (both venues)
    daily_loss_killswitch_frac: float = 0.08
    # HARD test cap: no single order's notional cost may exceed this (USD).
    # Enforced in code at the order layer — a strict, non-negotiable test limit.
    max_test_order_usd: float = 1.0


@dataclass(frozen=True)
class DecisionConfig:
    """Pre-match DECISION model (plan 20): value/edge selection + confidence-scaled
    stake. The model RECOMMENDS a stake in [min, max]; the actual order layer still
    clamps to RiskConfig.max_test_order_usd (the $1 hard cap) until that is raised."""

    # Stake envelope (USD). Base is the neutral bet; confidence scales it within [min,max].
    base_stake_usd: float = 1.0
    min_stake_usd: float = 0.2
    max_stake_usd: float = 2.0

    # Confidence multiplier k (stake = clip(base*(1 + k - conf_k_ref), min, max)). Bounds so
    # base $1 spans [0.2, 2.0]: k in [-0.8, +1.0]. conf_k_ref RE-CENTERS the stake on $1: k is
    # built from positive-only signals (edge ≥0 for any value bet + a positive calib constant),
    # so without an offset every bet sized ABOVE $1 (the "$1 or less when unsure" intent was never
    # hit — uncertain bets still bet $1.02). Subtracting the typical k (~0.25) makes a median-
    # confidence bet ≈ $1, a low-confidence one < $1, a high-confidence one > $1.
    k_min: float = -0.8
    k_max: float = 1.0
    conf_k_ref: float = 0.25      # k that maps to base_stake ($1); below → sub-$1, above → super-$1

    # Blend weights for k. edge dominates (it's the value signal); calibration,
    # recent form, then other alt-data nudge it. Sum need not be 1 (each is bounded).
    conf_w_edge: float = 0.60     # quarter-Kelly fraction → tanh
    conf_w_calib: float = 0.20    # how well the calibrated model beats the uniform baseline
    conf_w_form: float = 0.15     # recent-form agreement of the picked side
    conf_w_alt: float = 0.05      # other alt-data (oppadj/xga) agreement

    # Saturation references (signal value that maps to ~1.0 after tanh/clip).
    kelly_ref: float = 0.40       # quarter-Kelly fraction that saturates the edge component
    form_ref: float = 1.0         # form z that saturates the form component

    # Base net-edge threshold for the PRE-MATCH decision (separate from the in-play
    # risk.min_net_edge=0.03). Lowered to 0.02 because the smart-exit cash-out PROTECTS
    # marginal bets (sell the over-reaction before a settlement loss) — so we don't need
    # as much pre-match edge. PIT-validated (ops threshold sweep): 0.03→0.02 cut no-bets
    # 9→5 and lifted realised PnL +302→+448¢ (ROI 16.8→20.4%), robust across 0.02–0.01.
    min_net_edge: float = 0.02
    # Favourite–longshot bias: longshots are systematically overpriced (arXiv 1710.02824),
    # so a sub-threshold-cents pick must clear EXTRA edge (or it's skipped).
    longshot_cents: float = 15.0
    longshot_extra_theta: float = 0.05

    # Draw discipline: the draw edge in the early group stage is a REGIME-SPECIFIC,
    # mean-reverting effect (teams play not to lose; full-time draws are far rarer in
    # knockouts). The model is well-calibrated on draws in-sample (it predicts ~26% vs
    # 38% realised, i.e. under, not over), so we do NOT distort the model — instead a
    # draw pick must clear EXTRA net-edge before we stake it. This trims the lowest-
    # conviction draws (cutting draw-bet frequency) while keeping the high-edge ones.
    # Tuned by the PIT sweep in ops/_diag_draw_bias (draw bets 45%→37% at 0.06 with no
    # win-rate loss; the trimmed draws are the lowest-edge ones). See README plan 20.
    draw_extra_theta: float = 0.06

    # Motivation CONVICTION bet (decision behaviour): when the wounded-favourite bounce fires
    # (model/motivation.py) AND that team is the model's TOP pick, BET it even with no market
    # value — the thesis is the model/market under-rate an all-out favourite (e.g. Spain 4-0,
    # Netherlands 5-1 would both have been winning bets). Overrides the usual edge gate, so
    # top-pick-gated and modestly sized.
    motiv_conviction: bool = True
    motiv_conviction_min_prob: float = 0.40   # only force the bet if model rates the side ≥ this
    motiv_conviction_min_edge: float = -0.25  # …AND raw edge (model−ask) ≥ this: a small -EV is
                                              # the thesis, but a deep negative edge = market says
                                              # the model is wrong → defer to market, skip override


@dataclass(frozen=True)
class Config:
    paths: Paths = field(default_factory=Paths)
    model: ModelConfig = field(default_factory=ModelConfig)
    venue: VenueConfig = field(default_factory=VenueConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    soccer: SoccerConfig = field(default_factory=SoccerConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)


def _apply_selected_params(cfg: "Config") -> "Config":
    """Override the hand-set ModelConfig structural defaults with the param-sweep SELECTED
    best set, if present. The daily param_sweep (ops/param_sweep.py) re-scores the grid on all
    settled matches and writes data/output/param_selected.json — so production automatically
    adopts the rolling-best structural params (no manual edit, changes still traced in git via
    the JSON). Only keys that are real ModelConfig fields are applied; the hand-set defaults are
    the fallback when the file is absent/empty/unreadable. Set PM_DISABLE_PARAM_OVERRIDE=1 to
    pin the hand-set defaults (e.g. for a controlled backtest)."""
    if os.environ.get("PM_DISABLE_PARAM_OVERRIDE"):
        return cfg
    f = cfg.paths.output / "param_selected.json"
    try:
        if not f.exists():
            return cfg
        import json
        from dataclasses import fields, replace
        sel = (json.loads(f.read_text(encoding="utf-8")) or {}).get("params") or {}
        valid = {fld.name for fld in fields(cfg.model)}
        params = {k: v for k, v in sel.items() if k in valid}
        if not params:
            return cfg
        return replace(cfg, model=replace(cfg.model, **params))
    except Exception:
        return cfg


# Singleton used across the project. The param-sweep selection (if written) overrides the
# hand-set ModelConfig defaults — production tracks the rolling-best structural params.
CONFIG = _apply_selected_params(Config())
