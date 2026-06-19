"""In-play trading tactics (plan 04 §4c, 15) — driven by the live model.

Concrete, research-grounded tactics that turn the live model (model/inplay.py)
into trade actions. All are "trade out before settlement" style: a binary
contract's fair value moves toward $1 (or $0) as the game state resolves, so we
take profit when fair value spikes rather than holding to settlement.

Tactics implemented:
  1. **Late-equalizer / level-late draw take-profit** (user's tactic): holding a
     draw, once the score is level with little time left, the draw's fair value
     races toward max payout — SELL to lock it in rather than risk a late winner.
  2. **Draw time-value entry**: early in a tight 0:0, buy the draw cheap; its fair
     value rises monotonically as the clock runs (plan 03 §4b).
  3. **Convergence take-profit**: any position whose live fair value reaches a
     high fraction of max payout is sold (lock, plan 04 §7).
  4. **Totals time-decay**: back Under in low-λ games; sell once enough time has
     passed goalless and Under's fair value has climbed.
  5. **Momentum / xG value**: a side dominating xG/shots without scoring is
     under-priced for the next goal / win (flag only; needs live stats feed).

Event-driven tactics (research catalog): goal-overreaction fade, favourite-comeback,
red-card value, knockout late-draw.

Data-mined tactics (our 26-match intra-game study; see docs/INPLAY_FINDINGS.md): the
eight below — dormant_explosion, finishing_uplift_over, xg_dominance_chase,
possession_trap_fade, formation_fragility, lone_threat_removed, late_goal_bias,
live_odds_crossval — each grounded in a recurring, re-derivable pattern.

Each returns a ``TradeAction`` (act/side/reason/urgency); the executor applies
risk caps + the hard $1 test cap before any order.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from prediction_market.model.inplay import LiveMatchProb

# Defaults (calibrate from data; plan 04 §4c keeps these conservative).
LOCK_FRACTION = 0.88           # fair value >= 88% of $1 → take profit
MIN_TAKE_PROFIT_GAIN = 0.12    # or fair − entry >= this
LATE_MINUTE = 75              # "late" in the match
DRAW_LOCK_FAIR = 0.74         # draw fair value that triggers the level-late sell
EARLY_MINUTE = 35


@dataclass(frozen=True)
class TradeAction:
    act: str          # "BUY" | "SELL" | "HOLD"
    side: str         # "home" | "draw" | "away" | "under" | "over"
    reason: str       # English fallback / log / PDF text (numbers baked in)
    urgency: str = "normal"   # "normal" | "high"
    # i18n: a stable template key + the raw numeric/enum args, so the frontend can
    # render the localized backbone with the live numbers spliced in (5 languages).
    # `reason` stays the English fallback; HOLD actions leave these empty (not shown).
    reason_key: str = ""
    reason_args: dict = field(default_factory=dict)


def _fair(lp: LiveMatchProb, side: str) -> float:
    return {"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away}[side]


def convergence_take_profit(side: str, entry_price: float, lp: LiveMatchProb, *,
                            lock_fraction: float = LOCK_FRACTION,
                            min_gain: float = MIN_TAKE_PROFIT_GAIN) -> TradeAction:
    """Lock profit when a held position's live fair value spikes (plan 04 §7)."""
    fair = _fair(lp, side)
    if fair >= lock_fraction:
        return TradeAction("SELL", side, f"{side} fair {fair:.2f} near max payout — lock profit", "high",
                           reason_key="convergence_lock", reason_args={"side": side, "fair": round(fair, 2)})
    if fair - entry_price >= min_gain:
        return TradeAction("SELL", side, f"{side} fair {fair:.2f} vs entry {entry_price:.2f} — take {fair-entry_price:+.2f}",
                           reason_key="convergence_take",
                           reason_args={"side": side, "fair": round(fair, 2), "entry": round(entry_price, 2),
                                        "gain": round(fair - entry_price, 2)})
    return TradeAction("HOLD", side, f"{side} fair {fair:.2f}, hold")


# Market this far ABOVE the live model fair = an over-reaction worth locking. Tuned on
# the per-minute price_tick study (ops/_smart_exit_research): selling a held value pick
# when market ≥ live-fair + this margin returned ~2.8× hold-to-FT (8/10 sells improved),
# converting 'spiked then reverted' losers (Korea/Czechia led early then dropped) into
# profits while genuine winners — price tracking fair — are held to settlement.
OVERSHOOT_MARGIN = 0.12


def model_overshoot_take_profit(side: str, market_bid: float | None, lp: LiveMatchProb, *,
                                entry_price: float | None = None,
                                margin: float = OVERSHOOT_MARGIN) -> TradeAction:
    """SELL a held position when the MARKET price over-reacts above what the live model
    fair (current score + time left + xG shading) supports — lock the overshoot before it
    reverts. HOLD while the market is still ≤ fair (the value hasn't been realised yet).

    This is the model-AWARE take-profit (vs the price-only convergence lock): it sells the
    market's over-reaction to in-play events, not merely a high absolute price."""
    fair = _fair(lp, side)
    if market_bid is None:
        return TradeAction("HOLD", side, "no market bid to sell into")
    if market_bid >= fair + margin and lp.tau > 0.0:
        held = f" (held from {entry_price:.2f})" if entry_price is not None else ""
        return TradeAction("SELL", side,
                           f"market {market_bid:.2f} > live fair {fair:.2f} +{margin:.2f} — over-reaction, "
                           f"lock profit{held}", "high",
                           reason_key="overshoot_lock",
                           reason_args={"side": side, "mkt": round(market_bid, 2), "fair": round(fair, 2),
                                        "margin": round(margin, 2)})
    return TradeAction("HOLD", side, f"{side} market {market_bid:.2f} ≤ fair {fair:.2f}+{margin:.2f}")


def draw_trade_signal(lp: LiveMatchProb, *, draw_entry: float | None = None,
                      draw_market_price: float | None = None) -> TradeAction:
    """Draw lifecycle (plan 03 §4b): cheap-early entry, level-late take-profit.

    * Level score + late + high fair draw → SELL (the equalizer take-profit).
    * Early, tight, draw trading below fair → BUY the time-value.
    """
    level = lp.home_goals == lp.away_goals
    # 1) Level-late: draw fair value races to max payout → sell to lock.
    if level and lp.minute >= LATE_MINUTE and lp.fair_draw >= DRAW_LOCK_FAIR:
        if draw_entry is not None:
            return TradeAction("SELL", "draw",
                               f"level at {lp.minute}', fair draw {lp.fair_draw:.2f} → lock profit (held from {draw_entry:.2f})", "high",
                               reason_key="draw_lock_held",
                               reason_args={"min": lp.minute, "fair": round(lp.fair_draw, 2), "entry": round(draw_entry, 2)})
        return TradeAction("SELL", "draw", f"level at {lp.minute}', fair draw {lp.fair_draw:.2f} near max — sell if held", "high",
                           reason_key="draw_lock", reason_args={"min": lp.minute, "fair": round(lp.fair_draw, 2)})
    # 2) Early time-value entry: 0:0, draw under-priced vs fair.
    if level and lp.minute <= EARLY_MINUTE and draw_market_price is not None and lp.fair_draw > draw_market_price + 0.03:
        return TradeAction("BUY", "draw",
                           f"tight {lp.home_goals}:{lp.away_goals} at {lp.minute}', fair draw {lp.fair_draw:.2f} > market {draw_market_price:.2f}",
                           reason_key="draw_entry",
                           reason_args={"gh": lp.home_goals, "ga": lp.away_goals, "min": lp.minute,
                                        "fair": round(lp.fair_draw, 2), "mkt": round(draw_market_price, 2)})
    return TradeAction("HOLD", "draw", f"draw fair {lp.fair_draw:.2f} at {lp.minute}'")


def totals_time_decay(lp: LiveMatchProb, line: float = 2.5, *,
                      entry_under: float | None = None) -> TradeAction:
    """Back Under in low-scoring games; sell Under once it has decayed up.

    Uses the live over/under fair value: as time passes goalless, P(Under) rises.
    """
    p_over = lp.p_over_total.get(line)
    if p_over is None:
        return TradeAction("HOLD", "under", "no totals line available")
    p_under = 1.0 - p_over
    if p_under >= LOCK_FRACTION:
        return TradeAction("SELL", "under", f"Under {line} fair {p_under:.2f} near max — lock", "high",
                           reason_key="under_lock", reason_args={"line": line, "fair": round(p_under, 2)})
    if entry_under is not None and p_under - entry_under >= MIN_TAKE_PROFIT_GAIN:
        return TradeAction("SELL", "under", f"Under {line} {p_under:.2f} vs entry {entry_under:.2f} — take profit",
                           reason_key="under_take",
                           reason_args={"line": line, "fair": round(p_under, 2), "entry": round(entry_under, 2)})
    return TradeAction("HOLD", "under", f"Under {line} fair {p_under:.2f}")


def momentum_value(*, xg_for: float, xg_against: float, goals_for: int, goals_against: int,
                   minute: int, side: str = "home", xg_edge: float = 1.0) -> TradeAction:
    """Flag a side that dominates xG without the scoreline to match (value next-goal/win)."""
    if xg_for - xg_against >= xg_edge and goals_for <= goals_against and minute <= 80:
        return TradeAction("BUY", side,
                           f"{side} xG {xg_for:.1f} vs {xg_against:.1f} but score {goals_for}:{goals_against} — under-priced", "normal",
                           reason_key="momentum",
                           reason_args={"side": side, "xg_for": round(xg_for, 1), "xg_against": round(xg_against, 1),
                                        "gf": goals_for, "ga": goals_against})
    return TradeAction("HOLD", side, "no momentum mispricing")


def live_momentum_from_store(conn, fixture_api_id: int, home_api_id: int, away_api_id: int,
                             minute: int, home_goals: int, away_goals: int) -> TradeAction:
    """Read live xG from the store (sync_fixture_stats) and emit a momentum signal."""
    xg = {r["team_api_id"]: r["xg"] for r in conn.execute(
        "SELECT team_api_id, xg FROM fixture_stats WHERE fixture_api_id=?", (fixture_api_id,))}
    xh, xa = xg.get(home_api_id), xg.get(away_api_id)
    if xh is None or xa is None:
        return TradeAction("HOLD", "home", "no live xG yet")
    # Evaluate both sides; return the stronger momentum mispricing.
    h = momentum_value(xg_for=xh, xg_against=xa, goals_for=home_goals, goals_against=away_goals,
                       minute=minute, side="home")
    if h.act == "BUY":
        return h
    return momentum_value(xg_for=xa, xg_against=xh, goals_for=away_goals, goals_against=home_goals,
                          minute=minute, side="away")


# ── Event-driven tactics (research catalog, docs/INPLAY_SCENARIOS.md) ─────────
GOAL_FADE_WINDOW = 4          # minutes after a surprising goal that the over-move reverts
RED_CARD_WINDOW = 12          # minutes after a red card that the opponent's λ is front-loaded
FAV_COMEBACK_MAX_MIN = 70     # a trailing pre-match favourite still has equity until ~here
FAV_COMEBACK_MIN_PROB = 0.55  # "clear" pre-match favourite threshold
# A reversal/value tactic only makes sense while the game is still LIVE enough to revert:
FADE_MIN_FAV_EQUITY = 0.15    # don't "fade back the favourite" once the live model rates it dead
                              # (e.g. 90'/stoppage, 0-1 down → ~0% → no over-reaction left to fade)
MIN_REMAINING_GOALS = 0.30    # a "next-goal value" flag needs goals still expected to come


def goal_overreaction_fade(lp: LiveMatchProb, *, prematch_fav_side: str | None,
                           last_goal_side: str | None, last_goal_minute: int | None,
                           fav_basis: str | None = None) -> TradeAction:
    """Markets OVER-react to a SURPRISING goal then mean-revert (Choi & Hui; ~40%/min,
    gone by ~5-6'). If the pre-match UNDERDOG just scored, fade it — back the pre-match
    favourite, which the panic has under-priced — inside a short window.

    ``fav_basis`` (Part 2): why this side is the favourite — an explicit strength+form
    combination (aligned / strong_poor_form / in_form_underdog), surfaced in the reason."""
    if not last_goal_side or last_goal_minute is None or prematch_fav_side not in ("home", "away"):
        return TradeAction("HOLD", "home", "no recent goal to fade")
    mins_since = lp.minute - last_goal_minute
    surprising = last_goal_side != prematch_fav_side          # the underdog scored
    # Liveness guard: only fade when the favourite STILL has real comeback equity in the
    # live model. Late/decided games (e.g. 90'+, 0-1 down → fav ~0%) have nothing left to
    # revert — backing the favourite there is nonsense, not an over-reaction.
    fav_equity = lp.p_home if prematch_fav_side == "home" else lp.p_away
    if surprising and 0 <= mins_since <= GOAL_FADE_WINDOW and fav_equity >= FADE_MIN_FAV_EQUITY:
        when = "just now" if mins_since == 0 else f"{mins_since}' ago"
        return TradeAction("BUY", prematch_fav_side,
                           f"underdog scored {when} — markets over-react to surprise goals "
                           f"(~40%/min, reverts by {GOAL_FADE_WINDOW}'); fade back the favourite", "high",
                           reason_key=("fade_now" if mins_since == 0 else "fade_ago"),
                           reason_args={"mins": mins_since, "window": GOAL_FADE_WINDOW, "basis": fav_basis or "aligned"})
    return TradeAction("HOLD", prematch_fav_side, "no overreaction window")


def favourite_comeback(lp: LiveMatchProb, *, prematch_fav_side: str | None,
                       prematch_fav_prob: float | None, fav_basis: str | None = None) -> TradeAction:
    """A clear pre-match favourite that is TRAILING still has large residual equity
    (high λ + time): back the comeback while there is time, before the market fully
    re-prices it. (Favourite-longshot bias makes the now-leading underdog over-priced.)

    ``fav_basis`` (Part 2): the explicit strength+form basis for calling this side the
    favourite, surfaced in the reason."""
    if prematch_fav_side not in ("home", "away") or not prematch_fav_prob:
        return TradeAction("HOLD", "home", "no clear favourite")
    fav_goals = lp.home_goals if prematch_fav_side == "home" else lp.away_goals
    opp_goals = lp.away_goals if prematch_fav_side == "home" else lp.home_goals
    if prematch_fav_prob >= FAV_COMEBACK_MIN_PROB and fav_goals < opp_goals and lp.minute <= FAV_COMEBACK_MAX_MIN:
        return TradeAction("BUY", prematch_fav_side,
                           f"pre-match fav ({prematch_fav_prob:.0%}) trailing at {lp.minute}' — residual equity, back the comeback",
                           reason_key="comeback",
                           reason_args={"pct": round(prematch_fav_prob * 100), "min": lp.minute, "basis": fav_basis or "aligned"})
    return TradeAction("HOLD", prematch_fav_side, "favourite not trailing")


def red_card_value(lp: LiveMatchProb, *, carded_side: str | None, card_minute: int | None) -> TradeAction:
    """After a red card the opponent's near-term scoring is front-loaded (~56% of the
    extra goals land within 15'). Flag value on the 11-man side right after the card."""
    if carded_side not in ("home", "away") or card_minute is None:
        return TradeAction("HOLD", "home", "no recent red card")
    opp = "away" if carded_side == "home" else "home"
    # A "next-goal value" flag is pointless once the game is effectively over (no goals
    # expected to come — e.g. a red shown deep in stoppage time).
    if 0 <= lp.minute - card_minute <= RED_CARD_WINDOW and lp.exp_remaining_goals >= MIN_REMAINING_GOALS:
        return TradeAction("BUY", opp,
                           f"red card on {carded_side} at {card_minute}' — opponent's next-goal value elevated", "high",
                           reason_key="red_card", reason_args={"carded": carded_side, "min": card_minute})
    return TradeAction("HOLD", opp, "no red-card window")


# ── Data-mined tactics (26-match intra-game study, docs/INPLAY_FINDINGS.md) ───
# All thresholds are grounded in the finished-fixture analysis (ops/_analyze_intragame):
#   * 6/6 matches goalless at HT saw 2H goals (avg 3.0); 59% of all goals fell in
#     the 2H, 34% at 75'+  →  dormant_explosion + late_goal_bias.
#   * goals − total_xG = +0.87 (median +0.70): WC finishing beats chances  →
#     finishing_uplift_over (a measured OVER lean, NOT baked into the core model).
#   * xG-dominant-but-not-winning happened 3× (SUI 3.2, ESP 2.1, URU 1.7 all drew) →
#     xg_dominance_chase.
#   * possession-dominant side LOST 6/26 (23%), incl. TUR 72% poss 0-2  →
#     possession_trap_fade.
#   * 5-3-2 / 3-4-2-1 conceded 3.5 / 2.2 per game vs 4-2-3-1's 1.27  →
#     formation_fragility.
#   * single player took ≥50% of a team's shots (Messi 60%, Haaland 56%) → if that
#     lone threat leaves the pitch the team's scoring collapses → lone_threat_removed.
DORMANT_MIN_FROM = 40         # only flag the "quiet but loaded" window from ~HT…
DORMANT_MIN_TO = 70           # …until it is too late for the 2H surge to pay
DORMANT_MAX_GOALS = 1         # combined goals so far (≤1 = still "quiet")
DORMANT_XG_MIN = 1.0          # combined live xG (chances ARE being created)
DORMANT_REMAINING_GOALS = 0.8 # model still expects goals to come
FINISHING_UPLIFT = 0.4        # goals−xG edge (+0.87 mined, sd 1.78 / n=26 → heavily shrunk);
                              # only ever REINFORCES a model-vs-market over edge, never carries it
XG_CHASE_EDGE = 1.0           # live xG lead that flags an "应得未得" chase
XG_CHASE_MAX_MIN = 80         # still time for the deserved goal to arrive
POSSESSION_TRAP_MIN = 0.58    # ≥58% possession…
POSSESSION_TRAP_XG_MAX = 0.8  # …but ≤0.8 xG = sterile control, fade the over-rated side
# Only formations the 26-match data ACTUALLY flags as leaky (5-3-2 GA 3.5/g, 3-4-2-1
# GA 2.2/g vs 4-2-3-1's 1.27). 3-4-3 / 3-5-2 had 1-match samples → excluded as unproven.
FRAGILE_FORMATIONS = {"5-3-2", "3-4-2-1"}
LONE_THREAT_SHARE = 0.50      # one player ≥50% of team shots = single point of failure
LATE_GOAL_FROM_MIN = 70       # the late-goal-rich window (34% of goals at 75'+)


def dormant_explosion(lp: LiveMatchProb, *, combined_xg: float | None) -> TradeAction:
    """A quiet scoreline (≤1 goal) around half-time WITH chances already created
    (high combined live xG) precedes a 2H goal surge. Mined: 6/6 HT-goalless WC
    matches saw 2H goals, avg 3.0; 59% of all goals land in the 2H. Back the OVER
    (next goal coming) while the game is still 'quiet but loaded'."""
    if combined_xg is None:
        return TradeAction("HOLD", "over", "no live xG for dormant check")
    goals = lp.home_goals + lp.away_goals
    if (DORMANT_MIN_FROM <= lp.minute <= DORMANT_MIN_TO and goals <= DORMANT_MAX_GOALS
            and combined_xg >= DORMANT_XG_MIN and lp.exp_remaining_goals >= DORMANT_REMAINING_GOALS):
        return TradeAction("BUY", "over",
                           f"quiet {lp.home_goals}:{lp.away_goals} at {lp.minute}' but combined xG {combined_xg:.1f} — "
                           f"chances building, 2nd-half goal likely — back OVER", "high",
                           reason_key="dormant_explosion",
                           reason_args={"gh": lp.home_goals, "ga": lp.away_goals, "min": lp.minute,
                                        "xg": round(combined_xg, 1)})
    return TradeAction("HOLD", "over", "no dormant-explosion setup")


def finishing_uplift_over(lp: LiveMatchProb, line: float = 2.5, *,
                          market_over_price: float | None = None,
                          uplift: float = FINISHING_UPLIFT) -> TradeAction:
    """A small, shrunk finishing adjustment that ONLY fires against a real OVER/UNDER
    market AND only when the model ALREADY sees the OVER as under-priced — the uplift
    then reinforces that edge, it never creates one on its own.

    Deliberately conservative:
      * NO market quote → HOLD (we have nothing to value against; a free-floating
        "model+uplift vs model" lean is circular and is not emitted).
      * model's own P(OVER) must already exceed the market (the edge must exist first).
      * the finishing uplift (heavily shrunk from the noisy +0.87/n=26 estimate) only
        widens an existing edge; the trade must still clear the take-profit threshold."""
    import math

    from scipy.stats import poisson
    if market_over_price is None or line not in lp.p_over_total:
        return TradeAction("HOLD", "over", "no OVER/UNDER market to value against")
    if lp.minute >= 80 or lp.tau <= 0.10:
        return TradeAction("HOLD", "over", "too late for finishing adjustment")
    goals_now = lp.home_goals + lp.away_goals
    need = line - goals_now            # additional goals required to clear the line

    def _over(lam: float) -> float:
        # P(remaining goals > need) = 1 − P(remaining ≤ floor(need)) for a Poisson tail.
        return 1.0 if need <= 0 else float(1.0 - poisson.cdf(math.floor(need), lam))

    lam_base = lp.exp_remaining_goals
    lam_adj = lam_base + (uplift * lp.tau)        # shrunk uplift, scaled by remaining time
    p_base, p_adj = _over(lam_base), _over(lam_adj)
    # Gate 1: the model itself must already price the OVER above the market (edge exists).
    # Gate 2: with the small finishing reinforcement, the edge clears take-profit.
    if p_base - market_over_price >= 0.03 and p_adj - market_over_price >= MIN_TAKE_PROFIT_GAIN:
        return TradeAction("BUY", "over",
                           f"OVER {line}: model {p_base:.2f} > market {market_over_price:.2f}, "
                           f"finishing tendency reinforces to {p_adj:.2f}", "normal",
                           reason_key="finishing_uplift_mkt",
                           reason_args={"line": line, "base": round(p_base, 2), "fair": round(p_adj, 2),
                                        "mkt": round(market_over_price, 2)})
    return TradeAction("HOLD", "over", f"OVER {line} no model-vs-market edge")


def xg_dominance_chase(*, xg_for: float | None, xg_against: float | None,
                       goals_for: int, goals_against: int, minute: int,
                       side: str = "home", edge: float = XG_CHASE_EDGE) -> TradeAction:
    """A side OUT-creating its opponent by a clear xG margin while NOT ahead is being
    held back by finishing/keeping variance that mean-reverts. Mined: SUI(3.2), ESP(2.1),
    URU(1.7) all dominated xG by ≥1.0 yet only drew. Back that side (next goal / result)
    — a stronger-bar, higher-urgency sibling of momentum_value, also valid when BEHIND."""
    if xg_for is None or xg_against is None:
        return TradeAction("HOLD", side, "no live xG for chase")
    if xg_for - xg_against >= edge and goals_for <= goals_against and minute <= XG_CHASE_MAX_MIN:
        state = "level" if goals_for == goals_against else "trailing"
        return TradeAction("BUY", side,
                           f"{side} xG {xg_for:.1f} vs {xg_against:.1f} (+{xg_for-xg_against:.1f}) but {state} "
                           f"{goals_for}:{goals_against} at {minute}' — deserved goal coming, back {side}", "high",
                           reason_key="xg_chase",
                           reason_args={"side": side, "xgf": round(xg_for, 1), "xga": round(xg_against, 1),
                                        "gf": goals_for, "ga": goals_against, "min": minute})
    return TradeAction("HOLD", side, "no xG dominance to chase")


def possession_trap_fade(*, possession: float | None, xg_for: float | None,
                         goals_for: int, goals_against: int, minute: int,
                         side: str = "home") -> TradeAction:
    """Sterile possession (high control, low xG) is NOT scoreboard value: possession-
    dominant WC sides lost 23% (TUR 72% poss → 0-2). When a side hoards the ball without
    creating, FADE the market's over-rating of it (back the opponent / draw)."""
    if possession is None or xg_for is None:
        return TradeAction("HOLD", side, "no possession/xG for trap check")
    opp = "away" if side == "home" else "home"
    if (possession >= POSSESSION_TRAP_MIN and xg_for <= POSSESSION_TRAP_XG_MAX
            and goals_for <= goals_against and minute >= 35):
        return TradeAction("BUY", opp,
                           f"{side} {possession:.0%} possession but only {xg_for:.1f} xG — sterile control, "
                           f"not scoreboard value — fade it, value on {opp}", "normal",
                           reason_key="possession_trap",
                           reason_args={"side": side, "poss": round(possession * 100), "xg": round(xg_for, 1),
                                        "opp": opp, "min": minute})
    return TradeAction("HOLD", side, "no possession trap")


def formation_fragility(lp: LiveMatchProb, *, home_formation: str | None,
                        away_formation: str | None) -> TradeAction:
    """Back-three / stretched formations leak goals: 5-3-2 conceded 3.5/g, 3-4-2-1 2.2/g
    vs 4-2-3-1's 1.27. If exactly one side lines up fragile while the game is still open,
    lean OVER / toward the solid side's attack."""
    hf = home_formation in FRAGILE_FORMATIONS
    af = away_formation in FRAGILE_FORMATIONS
    if hf == af or lp.minute > LATE_MINUTE or lp.exp_remaining_goals < MIN_REMAINING_GOALS:
        return TradeAction("HOLD", "over", "no one-sided fragile formation")
    fragile_side, fragile_form = ("home", home_formation) if hf else ("away", away_formation)
    attack = "away" if fragile_side == "home" else "home"
    return TradeAction("BUY", "over",
                       f"{fragile_side} in fragile {fragile_form} (concedes-heavy) — lean OVER / {attack} attack", "normal",
                       reason_key="formation_fragility",
                       reason_args={"side": fragile_side, "formation": fragile_form, "attack": attack})


def lone_threat_removed(*, side: str, lone_player: str | None, shot_share: float | None,
                        removed: bool, minute: int, exp_remaining_goals: float) -> TradeAction:
    """A team whose shots funnel through ONE player (≥50% share: Messi 60%, Haaland 56%)
    has a single point of failure. Once that player is subbed off / sent off, the team's
    scoring threat collapses — FADE its remaining goals (back its opponent / UNDER)."""
    if not lone_player or shot_share is None or not removed:
        return TradeAction("HOLD", side, "no removed lone threat")
    opp = "away" if side == "home" else "home"
    if exp_remaining_goals >= MIN_REMAINING_GOALS and minute <= 88:
        return TradeAction("BUY", opp,
                           f"{side}'s lone threat {lone_player} ({shot_share:.0%} of shots) left the pitch — "
                           f"scoring collapses, fade {side} (value {opp} / UNDER)", "normal",
                           reason_key="lone_threat",
                           reason_args={"side": side, "player": lone_player,
                                        "share": round(shot_share * 100), "opp": opp})
    return TradeAction("HOLD", side, "lone threat window closed")


def late_goal_bias(lp: LiveMatchProb, line: float = 2.5, *,
                   entry_under: float | None = None) -> TradeAction:
    """34% of WC goals land at 75'+ and 59% in the 2H, so an UNDER that looks 'safe' at
    70-80' is more vulnerable than a memoryless Poisson implies (late legs tire, chasing
    teams commit). Warn against locking UNDER early; flag BACK-the-late-goal when the game
    is still live with chances expected."""
    if lp.minute < LATE_GOAL_FROM_MIN or lp.tau <= 0.02:
        return TradeAction("HOLD", "over", "not in late-goal window")
    if lp.exp_remaining_goals >= 0.45 and lp.minute <= 88:
        msg = f"{lp.minute}' and {lp.exp_remaining_goals:.2f} goals still expected — late-goal-rich window"
        if entry_under is not None:
            return TradeAction("HOLD", "under",
                               f"hold UNDER {line} cautiously: {msg}", "normal",
                               reason_key="late_goal_hold", reason_args={"line": line, "min": lp.minute,
                                                                         "exp": round(lp.exp_remaining_goals, 2)})
        return TradeAction("BUY", "over",
                           f"late OVER value — {msg}", "normal",
                           reason_key="late_goal_back", reason_args={"line": line, "min": lp.minute,
                                                                     "exp": round(lp.exp_remaining_goals, 2)})
    return TradeAction("HOLD", "over", "late but little expected")


def live_odds_crossval(*, model_fair: float, book_prob: float | None, side: str,
                       our_venue_price: float | None = None,
                       move_threshold: float = 0.06) -> TradeAction:
    """API-Football live BOOKMAKER odds as an independent third source. Sharp books move
    fast; when the book's implied prob agrees with our model but our tradable venue
    (Kalshi/Poly) hasn't caught up, that confirmation flags an earlier, higher-confidence
    entry. Fires only when book and model agree AND the venue lags both."""
    if book_prob is None:
        return TradeAction("HOLD", side, "no live book odds")
    # Book and model must AGREE that `side` is under-priced somewhere.
    if our_venue_price is not None:
        venue_gap = min(model_fair, book_prob) - our_venue_price
        if venue_gap >= move_threshold:
            return TradeAction("BUY", side,
                               f"{side}: book {book_prob:.2f} & model {model_fair:.2f} both > venue {our_venue_price:.2f} "
                               f"— sharp book confirms, venue lagging", "high",
                               reason_key="odds_crossval_venue",
                               reason_args={"side": side, "book": round(book_prob, 2), "model": round(model_fair, 2),
                                            "venue": round(our_venue_price, 2)})
        return TradeAction("HOLD", side, "venue not lagging book+model")
    # No venue quote yet: a book that LEADS the model is only a weak advisory flag — it
    # trusts the book over our calibrated model, so require a clearly BIGGER gap (move +0.04)
    # than the venue-confirmed branch, to avoid firing on quote-cadence noise.
    if book_prob - model_fair >= move_threshold + 0.04:
        return TradeAction("BUY", side,
                           f"{side}: live book {book_prob:.2f} has moved above model {model_fair:.2f} — "
                           f"sharp money leading, watch for entry", "normal",
                           reason_key="odds_crossval_lead",
                           reason_args={"side": side, "book": round(book_prob, 2), "model": round(model_fair, 2)})
    return TradeAction("HOLD", side, "book aligned with model")


def knockout_late_draw(lp: LiveMatchProb, *, knockout: bool) -> TradeAction:
    """In a KNOCKOUT the 3-way settles on 90', so a late level DRAW is a terminal paying
    outcome AND both teams often play for extra time — the 90' draw is worth MORE than in
    a league game. Back it late when level (the opposite sign to a league late-draw)."""
    if knockout and lp.home_goals == lp.away_goals and lp.minute >= LATE_MINUTE:
        return TradeAction("BUY", "draw",
                           f"level knockout at {lp.minute}' — 90' draw pays (extra time ahead), back the draw", "high",
                           reason_key="ko_draw", reason_args={"min": lp.minute})
    return TradeAction("HOLD", "draw", "not a late level knockout")


if __name__ == "__main__":
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.inplay import live_from_strength
    from prediction_market.model.strength import build_strength

    sm = build_strength(load_prior())
    print("Draw lifecycle — Brazil vs Morocco, bought draw early at 0.24:")
    # Early 0:0 entry, then level-late take-profit.
    early = live_from_strength(sm, "brazil", "morocco", 20, 0, 0)
    print("  20' 0:0 :", draw_trade_signal(early, draw_market_price=0.20))
    late = live_from_strength(sm, "brazil", "morocco", 82, 1, 1)   # equalised late
    print("  82' 1:1 :", draw_trade_signal(late, draw_entry=0.24))
    print("Convergence take-profit on a held home position, 2:0 at 80':")
    lead = live_from_strength(sm, "brazil", "morocco", 80, 2, 0)
    print("  ", convergence_take_profit("home", 0.55, lead))
