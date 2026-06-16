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

Each returns a ``TradeAction`` (act/side/reason/urgency); the executor applies
risk caps + the hard $1 test cap before any order.
"""
from __future__ import annotations

from dataclasses import dataclass

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
    reason: str
    urgency: str = "normal"   # "normal" | "high"


def _fair(lp: LiveMatchProb, side: str) -> float:
    return {"home": lp.p_home, "draw": lp.p_draw, "away": lp.p_away}[side]


def convergence_take_profit(side: str, entry_price: float, lp: LiveMatchProb, *,
                            lock_fraction: float = LOCK_FRACTION,
                            min_gain: float = MIN_TAKE_PROFIT_GAIN) -> TradeAction:
    """Lock profit when a held position's live fair value spikes (plan 04 §7)."""
    fair = _fair(lp, side)
    if fair >= lock_fraction:
        return TradeAction("SELL", side, f"{side} fair {fair:.2f} near max payout — lock profit", "high")
    if fair - entry_price >= min_gain:
        return TradeAction("SELL", side, f"{side} fair {fair:.2f} vs entry {entry_price:.2f} — take {fair-entry_price:+.2f}")
    return TradeAction("HOLD", side, f"{side} fair {fair:.2f}, hold")


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
                               f"level at {lp.minute}', fair draw {lp.fair_draw:.2f} → lock profit (held from {draw_entry:.2f})", "high")
        return TradeAction("SELL", "draw", f"level at {lp.minute}', fair draw {lp.fair_draw:.2f} near max — sell if held", "high")
    # 2) Early time-value entry: 0:0, draw under-priced vs fair.
    if level and lp.minute <= EARLY_MINUTE and draw_market_price is not None and lp.fair_draw > draw_market_price + 0.03:
        return TradeAction("BUY", "draw",
                           f"tight {lp.home_goals}:{lp.away_goals} at {lp.minute}', fair draw {lp.fair_draw:.2f} > market {draw_market_price:.2f}")
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
        return TradeAction("SELL", "under", f"Under {line} fair {p_under:.2f} near max — lock", "high")
    if entry_under is not None and p_under - entry_under >= MIN_TAKE_PROFIT_GAIN:
        return TradeAction("SELL", "under", f"Under {line} {p_under:.2f} vs entry {entry_under:.2f} — take profit")
    return TradeAction("HOLD", "under", f"Under {line} fair {p_under:.2f}")


def momentum_value(*, xg_for: float, xg_against: float, goals_for: int, goals_against: int,
                   minute: int, side: str = "home", xg_edge: float = 1.0) -> TradeAction:
    """Flag a side that dominates xG without the scoreline to match (value next-goal/win)."""
    if xg_for - xg_against >= xg_edge and goals_for <= goals_against and minute <= 80:
        return TradeAction("BUY", side,
                           f"{side} xG {xg_for:.1f} vs {xg_against:.1f} but score {goals_for}:{goals_against} — under-priced", "normal")
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


def goal_overreaction_fade(lp: LiveMatchProb, *, prematch_fav_side: str | None,
                           last_goal_side: str | None, last_goal_minute: int | None) -> TradeAction:
    """Markets OVER-react to a SURPRISING goal then mean-revert (Choi & Hui; ~40%/min,
    gone by ~5-6'). If the pre-match UNDERDOG just scored, fade it — back the pre-match
    favourite, which the panic has under-priced — inside a short window."""
    if not last_goal_side or last_goal_minute is None or prematch_fav_side not in ("home", "away"):
        return TradeAction("HOLD", "home", "no recent goal to fade")
    mins_since = lp.minute - last_goal_minute
    surprising = last_goal_side != prematch_fav_side          # the underdog scored
    if surprising and 0 <= mins_since <= GOAL_FADE_WINDOW:
        return TradeAction("BUY", prematch_fav_side,
                           f"underdog scored {mins_since}' ago — market over-reacts, fade: back {prematch_fav_side}", "high")
    return TradeAction("HOLD", prematch_fav_side, "no overreaction window")


def favourite_comeback(lp: LiveMatchProb, *, prematch_fav_side: str | None,
                       prematch_fav_prob: float | None) -> TradeAction:
    """A clear pre-match favourite that is TRAILING still has large residual equity
    (high λ + time): back the comeback while there is time, before the market fully
    re-prices it. (Favourite-longshot bias makes the now-leading underdog over-priced.)"""
    if prematch_fav_side not in ("home", "away") or not prematch_fav_prob:
        return TradeAction("HOLD", "home", "no clear favourite")
    fav_goals = lp.home_goals if prematch_fav_side == "home" else lp.away_goals
    opp_goals = lp.away_goals if prematch_fav_side == "home" else lp.home_goals
    if prematch_fav_prob >= FAV_COMEBACK_MIN_PROB and fav_goals < opp_goals and lp.minute <= FAV_COMEBACK_MAX_MIN:
        return TradeAction("BUY", prematch_fav_side,
                           f"pre-match fav ({prematch_fav_prob:.0%}) trailing at {lp.minute}' — residual equity, back the comeback")
    return TradeAction("HOLD", prematch_fav_side, "favourite not trailing")


def red_card_value(lp: LiveMatchProb, *, carded_side: str | None, card_minute: int | None) -> TradeAction:
    """After a red card the opponent's near-term scoring is front-loaded (~56% of the
    extra goals land within 15'). Flag value on the 11-man side right after the card."""
    if carded_side not in ("home", "away") or card_minute is None:
        return TradeAction("HOLD", "home", "no recent red card")
    opp = "away" if carded_side == "home" else "home"
    if 0 <= lp.minute - card_minute <= RED_CARD_WINDOW:
        return TradeAction("BUY", opp,
                           f"red card on {carded_side} at {card_minute}' — opponent's next-goal value elevated", "high")
    return TradeAction("HOLD", opp, "no red-card window")


def knockout_late_draw(lp: LiveMatchProb, *, knockout: bool) -> TradeAction:
    """In a KNOCKOUT the 3-way settles on 90', so a late level DRAW is a terminal paying
    outcome AND both teams often play for extra time — the 90' draw is worth MORE than in
    a league game. Back it late when level (the opposite sign to a league late-draw)."""
    if knockout and lp.home_goals == lp.away_goals and lp.minute >= LATE_MINUTE:
        return TradeAction("BUY", "draw",
                           f"level knockout at {lp.minute}' — 90' draw pays (extra time ahead), back the draw", "high")
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
