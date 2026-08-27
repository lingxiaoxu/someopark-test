"""strategy/inplay_confidence.py — confidence tier for an in-play signal.

Encodes the validated effectiveness rules from the 9-day / 32-match signal review
(`.claude/plan/prediction market plan/20_inplay_signal_effectiveness_review.md`
plus 21 winners / 22 rescue). Every in-play opportunity gets a tier so the desk
sees WHICH signals to trust — without deleting any signal (the project rule: keep
all signals, only differentiate).

Tiers (per the review's thresholds):
    high   ≥80% historical hit-rate band
    medium 50–80%
    low    <50% (surface, but small size / advisory only)

This is a transparent rule engine, not a fitted model: each rule mirrors a measured
effect (leading +82%, top-10 +89%, large-edge −40%, direct-style −16%, …). It
returns (tier, reasons[]) so the UI can show the tier and the why.

Pure / stdlib-only; the rank + style maps are loaded lazily and cached.
"""
from __future__ import annotations

import json
from functools import lru_cache

# Style buckets the review found the model reads WELL vs POORLY.
_GOOD_STYLES = {"possession", "high_press", "dominant_attack"}
_BAD_STYLES = {"direct", "high_volume"}


@lru_cache(maxsize=1)
def _rank_by_name() -> dict:
    from prediction_market_soccer.config import CONFIG
    try:
        d = json.loads(CONFIG.paths.prior_ext_sim_v0.read_text(encoding="utf-8"))
        return {t["team"]: t.get("fifa_rank") for t in d.get("teams", [])}
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _style_by_id() -> dict:
    from prediction_market_soccer.config import CONFIG
    try:
        d = json.loads((CONFIG.paths.output / "team_styles.json").read_text(encoding="utf-8"))
        return {t["team_id"]: (t.get("styles") or [{}])[0].get("code") for t in d.get("teams", [])}
    except Exception:
        return {}


def match_context(home_id: str, away_id: str, home_name: str, away_name: str,
                  gh: int, ga: int, minute: int, model: dict | None = None,
                  knockout: bool = False) -> dict:
    """Resolve the per-match context the tiering + gate need (ranks + styles +
    score + live model 3-way for the underdog model-aligned rescue check).

    `knockout`: the per-match market is the 90' 3-way (home/Tie/away) in BOTH stages,
    but KO games run lower-scoring with more 90' draws (→ extra time). The tiering
    softens the draw penalty and tilts totals (UNDER↑ / OVER↓) accordingly.
    """
    rk, st = _rank_by_name(), _style_by_id()
    return {
        "home_rank": rk.get(home_name), "away_rank": rk.get(away_name),
        "home_style": st.get(home_id), "away_style": st.get(away_id),
        "gh": gh, "ga": ga, "minute": minute,
        "model": model or {},
        "knockout": bool(knockout),
    }


def assess(opp: dict, ctx: dict) -> tuple[str, list[str]]:
    """Return (tier, reasons) for one opportunity given its match context.

    `opp` needs: kind, side, edge, intent (optional). `ctx` from match_context().
    """
    kind = opp.get("kind")
    side = opp.get("side")
    edge = opp.get("edge")
    minute = ctx.get("minute") or 0
    gh, ga = ctx.get("gh") or 0, ctx.get("ga") or 0

    # Riskless cross-venue lock — always the top tier.
    if kind == "lock_arb":
        return "high", ["riskless cross-venue lock"]

    reasons: list[str] = []
    score = 0
    ko = bool(ctx.get("knockout"))
    leading = (side == "home" and gh > ga) or (side == "away" and ga > gh)
    level = gh == ga

    if side in ("home", "away"):
        team_rank = ctx.get("home_rank") if side == "home" else ctx.get("away_rank")
        team_style = ctx.get("home_style") if side == "home" else ctx.get("away_style")
        if leading:
            score += 2; reasons.append("leading side (82%)")
        if team_rank is not None and team_rank <= 10:
            score += 2; reasons.append("top-10 team (89%)")
        elif team_rank is not None and team_rank >= 26:
            score -= 2; reasons.append("weak team rank 26+ (30%)")
        if team_style in _GOOD_STYLES:
            score += 1; reasons.append(f"{team_style} style (model reads well)")
        elif team_style in _BAD_STYLES:
            score -= 2; reasons.append(f"{team_style} style (model overrates, 16%)")
        if edge is not None:
            big = abs(edge) >= 0.10
            if big and not (leading and team_rank is not None and team_rank <= 10):
                score -= 2; reasons.append("edge >0.10 w/o lead+top10 (model error, 40%)")
            elif 0.02 <= abs(edge) < 0.10:
                score += 1; reasons.append("edge in sweet spot 0.02–0.10")
        styles = {ctx.get("home_style"), ctx.get("away_style")}
        if styles == {"direct"} or styles == {"balanced", "direct"}:
            score -= 1; reasons.append("chaotic direct matchup (low signal)")

    elif side == "draw":
        if minute > 70 and level:
            # KO: a late level game very likely ends 90' level → Tie pays (then ET).
            score += 2 if ko else 1
            reasons.append("KO late & level → likely 90' tie" if ko else "draw late & still level (67%)")
        else:
            # KO draws at 90' are more common than group → softer penalty.
            score -= 1 if ko else 2
            reasons.append("draw early/not-level (KO: tie still likely)" if ko else "draw without late+level (33%)")

    elif side == "under":
        score += 1; reasons.append("UNDER read (65%, converges)")
        if minute <= 15:
            score += 1; reasons.append("early under (67%)")
        if ko:
            score += 1; reasons.append("KO games run lower-scoring (under↑)")

    elif side == "over":
        total = gh + ga
        if minute <= 15 or total == 1:
            reasons.append("over only early / 1-goal (medium)")
        else:
            score -= 1; reasons.append("over not early/1-goal (coinflip)")
        if ko:
            score -= 1; reasons.append("KO games run lower-scoring (over↓)")

    # Locking a held position that's now winning is reliable (manage intent + leading).
    if opp.get("intent") == "manage" and leading:
        score += 1; reasons.append("lock a leading position")

    tier = "high" if score >= 2 else ("low" if score <= -1 else "medium")
    return tier, reasons


def gate(opp: dict, ctx: dict) -> dict:
    """Actual staking gate (the betting threshold) — keeps the signal but decides
    whether we'd STAKE it, from the plan-22 rescue HARD conditions on top of the
    confidence tier. Returns {actionable, stake_frac, gate_reason}.

    Rules (signal kept either way; this only gates the stake):
      * lock_arb            → always stake (riskless).
      * tier == 'low'       → never stake (advisory only).
      * draw                → stake only if minute>70 AND still level (else advisory).
      * over (totals)       → stake only if minute≤15 OR exactly 1 goal so far.
      * directional underdog→ stake only if the LIVE model is aligned (its side is the
                              model argmax) or that side is already leading.
      * direct/high_volume  → stake only if that side is leading (else advisory).
      * large edge ≥0.10    → stake only if leading AND top-10 (the rescue combo).
    Stake fraction of the hard test cap scales with tier: high=1.0, medium=0.5.
    """
    tier, reasons = assess(opp, ctx)
    kind, side, edge = opp.get("kind"), opp.get("side"), opp.get("edge")
    minute = ctx.get("minute") or 0
    gh, ga = ctx.get("gh") or 0, ctx.get("ga") or 0
    model = ctx.get("model") or {}

    if kind == "lock_arb":
        return {"confidence": "high", "confidence_reason": "riskless cross-venue lock",
                "actionable": True, "stake_frac": 1.0, "gate_reason": "riskless lock"}

    actionable = tier in ("high", "medium")
    blocked: list[str] = []
    leading = (side == "home" and gh > ga) or (side == "away" and ga > gh)
    level = gh == ga

    if side == "draw" and not (minute > 70 and level):
        actionable = False; blocked.append("draw needs >70' & level")
    if side == "over" and not (minute <= 15 or (gh + ga) == 1):
        actionable = False; blocked.append("over needs ≤15' or 1-goal")
    if side in ("home", "away"):
        team_rank = ctx.get("home_rank") if side == "home" else ctx.get("away_rank")
        opp_rank = ctx.get("away_rank") if side == "home" else ctx.get("home_rank")
        team_style = ctx.get("home_style") if side == "home" else ctx.get("away_style")
        is_underdog = team_rank is not None and opp_rank is not None and team_rank > opp_rank
        if is_underdog:
            mp = model.get(side)
            argmax_ok = mp is not None and mp >= max(model.get("home", 0.0),
                                                     model.get("draw", 0.0), model.get("away", 0.0))
            if not (leading or argmax_ok):
                actionable = False; blocked.append("underdog needs model-aligned/leading")
        if team_style in _BAD_STYLES and not leading:
            actionable = False; blocked.append(f"{team_style} side not leading")
        if edge is not None and abs(edge) >= 0.10 and not (
                leading and team_rank is not None and team_rank <= 10):
            actionable = False; blocked.append("large edge w/o lead+top10")

    if tier == "low":
        actionable = False
    stake_frac = (1.0 if tier == "high" else 0.5 if tier == "medium" else 0.0) if actionable else 0.0
    return {"confidence": tier, "confidence_reason": reasons[0] if reasons else "",
            "actionable": actionable, "stake_frac": stake_frac,
            "gate_reason": ("; ".join(blocked) if blocked else f"stake {tier}")}


def annotate(opp: dict, ctx: dict, *, cap_usd: float | None = None) -> dict:
    """Mutate+return `opp` with confidence tier + the staking gate (actionable +
    stake_usd + gate_reason). `cap_usd` is the hard test-order cap; the stake is a
    tier fraction of it. The signal is ALWAYS kept — only `actionable` changes."""
    g = gate(opp, ctx)
    if cap_usd is None:
        try:
            from prediction_market_soccer.config import CONFIG
            cap_usd = CONFIG.risk.max_test_order_usd
        except Exception:
            cap_usd = 1.0
    opp["confidence"] = g["confidence"]
    opp["confidence_reason"] = g["confidence_reason"]
    opp["actionable"] = g["actionable"]
    opp["stake_usd"] = round(g["stake_frac"] * float(cap_usd), 2)
    opp["gate_reason"] = g["gate_reason"]
    return opp


if __name__ == "__main__":
    # Smoke test on synthetic opportunities (no DB needed beyond the loaded maps).
    ctx_lead_top10 = match_context("argentina", "algeria", "Argentina", "Algeria", 1, 0, 60)
    ctx_draw_late = match_context("argentina", "algeria", "Argentina", "Algeria", 0, 0, 80)
    cases = [
        ({"kind": "lock_arb", "side": "home", "edge": 0.1}, ctx_lead_top10),
        ({"kind": "relative_value", "side": "home", "edge": 0.06}, ctx_lead_top10),
        ({"kind": "relative_value", "side": "home", "edge": 0.18}, ctx_lead_top10),
        ({"kind": "tactic", "side": "draw", "edge": None}, ctx_draw_late),
        ({"kind": "relative_value", "side": "under", "edge": 0.08}, ctx_lead_top10),
    ]
    for o, c in cases:
        t, r = assess(o, c)
        print(f"  {o['kind']:<14} {o['side']:<5} edge={o.get('edge')}  → {t.upper():<6} ({r[0] if r else '-'})")
