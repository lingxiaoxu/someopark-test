"""strategy/decision_model.py — the pre-match betting DECISION (plan 20).

Replaces the naive argmax ("bet whatever the model rates highest") with a
value/edge selection sized by fractional Kelly and a confidence multiplier — all
strictly point-in-time. ONE pure function, ``decide()``, is the single entry
point intended to be shared by:

  * the historical bet log (ops.performance_report)  → accuracy / PnL / price-track
  * production daily betting
  * the PIT backtest (ops.decision_backtest)

so every "what did/would we bet" answer comes from the same logic.

Research grounding (see the betting-strategy research + README):
  * **Value betting (EV>0):** pick the side whose CALIBRATED model prob most
    exceeds the de-vigged market prob — NOT the most-likely outcome. (A 48% home
    that the market prices at 50% is a worse bet than a 30% draw priced at 20%.)
  * **Fractional Kelly:** size by edge/odds at a fraction of full Kelly (¼ by
    default) — best empirical ROI with far lower drawdown than full Kelly
    (Springer 2024 football value-betting study).
  * **Favourite–longshot bias:** longshots are systematically overpriced, so a
    cheap (sub-``longshot_cents``) pick must clear extra edge or it's skipped
    (arXiv 1710.02824).
  * **Calibration gate:** only stake when the calibrated model beats the uniform
    Brier baseline — otherwise the "edge" is fictitious.
  * **CLV is the scorecard** (tracked downstream), not small-sample win rate.

The stake returned is the model's RECOMMENDATION in [min,max] USD. The actual
order layer still clamps to the $1 hard cap (RiskConfig.max_test_order_usd) until
that is explicitly raised — this module never places orders.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from prediction_market.config.config import CONFIG, DecisionConfig, RiskConfig
from prediction_market.strategy.edge import compute_edge

_SIDES = ("home", "draw", "away")


@dataclass(frozen=True)
class SideQuote:
    """One outcome's executable price + de-vigged market implied prob, from the
    best venue we'd actually trade. ``ask`` is what you pay (0-1) to buy YES."""
    ask: float | None = None
    devig: float | None = None
    venue: str | None = None


@dataclass(frozen=True)
class Decision:
    """The pre-match decision for one match. ``side=None`` ⇒ no bet (stake 0)."""
    side: str | None
    venue: str | None
    stake_usd: float
    price_cents: float | None
    model_prob: float | None
    market_prob: float | None        # de-vigged
    net_edge: float | None
    kelly_fraction: float | None     # quarter-Kelly bankroll fraction (informational)
    confidence_k: float | None       # the stake multiplier actually applied
    tradable: bool
    reason: str


def quotes_from_milestone_row(row) -> dict:
    """Build {side: SideQuote} from a milestone_snapshot row — the cheapest executable
    ask per side across the two venues, plus that side's de-vigged market prob. Shared
    by the bet log AND the price-track so both feed decide() the identical entry quotes
    (→ identical pick → the views reconcile by construction)."""
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    q = {}
    for s in _SIDES:
        asks = []
        ka, pa = row[f"kalshi_{s}_ask"], row[f"poly_{s}_ask"]
        if ka is not None:
            asks.append((ka, "kalshi"))
        if pa is not None:
            asks.append((pa, "poly_us"))
        if not asks:
            q[s] = SideQuote()
            continue
        ask, ven = min(asks, key=lambda t: t[0])
        dv = row[f"devig_{s}"] if f"devig_{s}" in keys else None
        q[s] = SideQuote(ask=ask, devig=dv, venue=ven)
    return q


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _form_component(side: str, form: dict | None, ref: float) -> float:
    """Recent-form agreement of the PICKED side, in [-1, 1].
       home/away → that team's form z (clipped/ref). draw → closeness of the two
       teams' form (even sides make a draw more credible)."""
    if not form:
        return 0.0
    hz, az = form.get("home_z"), form.get("away_z")
    if side == "home":
        return _clip((hz or 0.0) / ref, -1.0, 1.0)
    if side == "away":
        return _clip((az or 0.0) / ref, -1.0, 1.0)
    # draw: closeness → +1 when identical form, → -1 when a full ref apart
    if hz is None or az is None:
        return 0.0
    return _clip(1.0 - abs(hz - az) / ref, -1.0, 1.0)


def _kelly_fraction(p: float, ask: float, frac: float) -> float:
    """Fractional Kelly for a binary contract bought at ``ask`` (pays $1).
       Full Kelly f* = (p - ask) / (1 - ask). Returns ``frac`` × that, ≥ 0."""
    denom = max(1.0 - ask, 1e-6)
    return max(0.0, (p - ask) / denom) * frac


def decide(
    model: dict,
    quotes: dict,
    *,
    cfg: DecisionConfig | None = None,
    risk: RiskConfig | None = None,
    fee: float = 0.0,
    sigma: dict | None = None,
    calib_confidence: float = 0.0,
    form: dict | None = None,
    alt: dict | None = None,
    gate_open: bool = True,
    conviction_side: str | None = None,
) -> Decision:
    """Decide the single best value bet for one match (or no bet).

    Args:
      model: calibrated PIT probs {'home','draw','away'} (sum≈1).
      quotes: {side: SideQuote} — executable ask + de-vigged market prob per side.
      calib_confidence: model-quality signal in [-1,1] (e.g. how far calibrated
        Brier beats the uniform baseline); scales the stake and is informational.
      form: {'home_z','away_z'} recent-form z (optional).
      alt: {side: agreement in [-1,1]} other alt-data lean (optional).
      gate_open: discipline gate — if False, never stake.
    """
    cfg = cfg or CONFIG.decision
    risk = risk or CONFIG.risk
    sigma = sigma or {}

    def _no_bet(reason: str) -> Decision:
        return Decision(side=None, venue=None, stake_usd=0.0, price_cents=None,
                        model_prob=None, market_prob=None, net_edge=None,
                        kelly_fraction=None, confidence_k=None, tradable=False, reason=reason)

    if not gate_open:
        return _no_bet("discipline gate closed (model not trade-grade)")

    # 1) Value selection: the best net-edge side AMONG THE TRADABLE ones — each side is
    # judged against ITS OWN threshold (base + longshot + draw-discipline). Picking the
    # raw-highest-edge side first and only THEN checking tradability was a bug: a draw with
    # the biggest gap but failing the draw discipline would suppress a perfectly tradable
    # home/away value bet (e.g. USA-Australia: draw +8% needs 8% → skip, but away +6% clears
    # its 2% → should bet). So we now only consider sides that clear their own threshold.
    best = None  # (net_edge, side, er, q)
    best_any = None  # best raw-edge side (for an informative no-bet reason)
    for side in _SIDES:
        q = quotes.get(side)
        if not q or q.ask is None:
            continue
        price_c = q.ask * 100.0
        # Decision-layer base threshold (cfg.min_net_edge), lower than the in-play
        # risk.min_net_edge because the smart-exit protects marginal pre-match bets.
        theta = getattr(cfg, "min_net_edge", risk.min_net_edge)
        if price_c < cfg.longshot_cents:
            theta += cfg.longshot_extra_theta                  # favourite–longshot guard
        if side == "draw":
            theta += getattr(cfg, "draw_extra_theta", 0.0)     # draw discipline (regime guard)
        er = compute_edge(model[side], q.ask, sigma_p=sigma.get(side, 0.0),
                          k=risk.shrink_k, fee=fee, theta=theta)
        if best_any is None or er.net_edge > best_any[0]:
            best_any = (er.net_edge, side)
        if er.tradable and (best is None or er.net_edge > best[0]):
            best = (er.net_edge, side, er, q)

    # Motivation CONVICTION override: a wounded strong favourite going all-out vs a weaker
    # team. When the bounce flags this side AND it's the model's TOP pick, bet it even with no
    # market value — the thesis is the model/market under-rate the all-out favourite (e.g.
    # Spain 4-0, Netherlands 5-1 would both have been winning bets). -EV by market devig, so
    # it's a deliberate, top-pick-gated override (config motiv_conviction).
    if (conviction_side is not None and getattr(cfg, "motiv_conviction", False)):
        qc = quotes.get(conviction_side)
        argmax = max(_SIDES, key=lambda s: model.get(s, 0.0))
        # Floor: don't force the bet when the market disagrees BIG (raw edge model−ask below
        # the floor). A small -EV is the intended thesis (the all-out favourite is under-rated),
        # but a deep negative edge means the market is telling us the model is wrong — there we
        # defer to the market and skip the override (config motiv_conviction_min_edge, def -0.18).
        conv_floor = getattr(cfg, "motiv_conviction_min_edge", -0.25)
        if (qc and qc.ask is not None and conviction_side == argmax
                and model.get(conviction_side, 0.0) >= getattr(cfg, "motiv_conviction_min_prob", 0.4)
                and model.get(conviction_side, 0.0) - qc.ask >= conv_floor):
            erc = compute_edge(model[conviction_side], qc.ask, sigma_p=sigma.get(conviction_side, 0.0),
                               k=risk.shrink_k, fee=fee, theta=-1.0)   # theta=-1 → always "tradable"
            best = (erc.net_edge, conviction_side, erc, qc)            # override value selection

    if best is None:
        if best_any is not None:
            return _no_bet(f"best side {best_any[1]} net_edge {best_any[0]:+.3f} below its threshold")
        return _no_bet("no executable quotes")
    net_edge, side, er, q = best

    # 2) Confidence multiplier k from a bounded blend of signals.
    f_kelly = _kelly_fraction(er.p_eff, q.ask, risk.kelly_fraction)
    edge_comp = math.tanh(f_kelly / max(cfg.kelly_ref, 1e-6))            # value/Kelly
    calib_comp = _clip(calib_confidence, -1.0, 1.0)                       # model quality
    form_comp = _form_component(side, form, cfg.form_ref)                 # recent form
    alt_comp = _clip((alt or {}).get(side, 0.0), -1.0, 1.0)              # other alt-data

    k = (cfg.conf_w_edge * edge_comp + cfg.conf_w_calib * calib_comp
         + cfg.conf_w_form * form_comp + cfg.conf_w_alt * alt_comp)
    k = _clip(k, cfg.k_min, cfg.k_max)
    stake = _clip(cfg.base_stake_usd * (1.0 + k), cfg.min_stake_usd, cfg.max_stake_usd)

    reason = (f"value buy {side} @ {q.ask*100:.0f}¢ (model {model[side]*100:.0f}% vs "
              f"devig {(q.devig or 0)*100:.0f}%, net_edge {net_edge:+.1%}); "
              f"¼Kelly {f_kelly:.3f}, k {k:+.2f} → ${stake:.2f}")
    return Decision(
        side=side, venue=q.venue, stake_usd=round(stake, 2),
        price_cents=round(q.ask * 100.0, 1),
        model_prob=round(model[side], 4),
        market_prob=round(q.devig, 4) if q.devig is not None else None,
        net_edge=round(net_edge, 4),
        kelly_fraction=round(f_kelly, 4),
        confidence_k=round(k, 4),
        tradable=True, reason=reason,
    )
