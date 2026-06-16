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
    n_sims_tournament: int = 200_000
    n_sims_quicklook: int = 50_000  # fast correctness pass before scaling up

    # Dixon-Coles low-score correlation parameter (plan 03 §2). rho in (-0.2, 0).
    dc_rho: float = -0.05
    score_matrix_kmax: int = 10  # truncate score matrix at 10-10

    # League baseline log-goal intercept mu and home advantage (plan 03 §2).
    # base_mu = log(baseline goals per side); exp(0.30) ~= 1.35, typical WC group game.
    base_mu: float = 0.30
    # beta maps a team-rating difference to a log-goal difference (Poisson rating model).
    beta: float = 0.40
    rating_bound: float = 2.5      # clip calibrated ratings to [-bound, +bound]
    home_adv: float = 0.25         # applied only to host nations on home soil
    host_nations: tuple[str, ...] = ("United States", "Mexico", "Canada")

    # Group round-3 qualification-incentive adjustments (plan 03 §3): teams that
    # have effectively clinched rotate (lower intensity); teams facing
    # elimination push (higher intensity). Applied to round-3 fixtures only.
    r3_incentives: bool = True
    r3_clinch_points: int = 6          # >= this after 2 games ⇒ likely to rotate
    r3_rotation_intensity: float = 0.90
    r3_desperation_intensity: float = 1.06

    # Knockout adjustments (plan 03 §4a): more cautious, fewer goals, more draws.
    knockout_lambda_scale: float = 0.85
    # Extra-time goal rate vs regulation (plan 03 §4b): lambda * 30/90.
    extra_time_fraction: float = 30.0 / 90.0
    # Penalty shootout bias toward the stronger/more-experienced side (52-55%).
    penalty_favorite_edge: float = 0.53

    # Rank -> strength prior mapping (plan 10 §5.1). Maps FIFA rank to a goal-
    # expectation prior; reverse-fit against the ext_sim_v0 advance probabilities.
    rank_strength_decay: float = 0.0125  # strength ~ exp(-decay * (rank - 1))

    # Fusion of the absolute-strength anchor (FIFA rank, plan 03 §1a) with the
    # external-sim prior fit (exp_points, §1d). exp_points conflates team strength
    # with GROUP DIFFICULTY (a team in a weak group looks too strong), so we
    # anchor on rank. 1.0 = rank only, 0.0 = exp_points only. 0.5 minimises
    # divergence from the sharp Kalshi/Global champion market (validation, not fit).
    rank_anchor_weight: float = 0.5

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

    # Golden-boot talent prior (plan 03 §6.1): EA FC 26 ratings give each player a
    # talent-grounded per-match goal rate. That FC rate is the PRIOR; observed club
    # (season-1) and WC-to-date scoring update it Bayesian-style with these
    # pseudo-match weights. A strong FC prior stops a 1-game burst (e.g. a weak-team
    # forward scoring twice in the opener) from inflating the forecast — the final
    # boot is dominated instead by talent x knockout depth (matches actually played).
    gb_fc_prior_alpha: float = 8.0      # pseudo-matches of FC-talent prior weight
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

    Pro plan = 7000 req/month. Discipline: pull once, store centrally, never
    re-pull; the frontend reads our stored data. Per-resource TTLs gate the
    watermark so a re-run within TTL costs ZERO requests.
    """

    api_host: str = "https://v3.football.api-sports.io"
    league_id: int = 1          # FIFA World Cup
    season: int = 2026

    # Budget guard rails.
    monthly_budget: int = 7000
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
class Config:
    paths: Paths = field(default_factory=Paths)
    model: ModelConfig = field(default_factory=ModelConfig)
    venue: VenueConfig = field(default_factory=VenueConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    soccer: SoccerConfig = field(default_factory=SoccerConfig)


# Singleton used across the project.
CONFIG = Config()
