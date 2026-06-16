# In-Play Live-Pricing Scenario Catalog (research → implementation map)

Sourced research catalog for the live double-Poisson model (`model/inplay.py`) and
in-play tactics (`strategy/inplay_tactics.py`, `strategy/inplay_arb.py`). Direction +
order-of-magnitude from football analytics; exact point-jumps are model-computed.

## Implemented now
- **Time decay** — remaining λ scales linearly with minutes left (`tau`).
- **Red card** — penalised side λ ×0.70 per red (RED_CARD_LAMBDA_MULT).
- **Lead/trail** — leader λ ×0.92, trailer λ ×1.10.
- **Strong vs weak scorer** — implicit via base lambdas: a strong favourite (high λ)
  leading locks (win ~92% @70'); an underdog leading leaves the favourite large
  comeback/draw equity (~37% @70'). Verified.
- **Live xG shading** — a side out-creating its pre-match xG gets λ shaded up (×, XG_WEIGHT=0.35).
- **Late level-draw correction** — when LEVEL past 70', deflate both λ (LATE_LEVEL_DEFLATE=0.30,
  ramp to 90'); fixes the documented double-Poisson late-draw under-pricing. 0-0 draw
  prob 60'→0.51, 85'→0.90, 89'→0.98.
- **Tactics** — late-equalizer draw take-profit, convergence take-profit, xG momentum BUY.

## Key research findings (to encode next)
1. **Two opposing clocks**: goal→win-prob impact RISES with minute; goal→Overs impact FALLS.
2. **Goal by who/when/state**: favourite scores 1-0 → +18–22 win pts; underdog scores → +25–30
   (crosses steep sigmoid). 2nd-half first goal ≈ +13 pts vs 1st-half. +2 lead in H2 wins >90%.
3. **Goal overreaction (tradable)**: market over-reacts to SURPRISING goals (si>0.41), decays
   ~40%/min, gone by +5–6 min → fade window +2..+4 min. Underreacts to expected goals.
4. **Red card timing**: carded late ≈ +62% more likely to still win than carded early; ~0.013–0.017
   xG/min transferred to opponent, scaled by minutes left; front-load (56% of conceded goals within 15 min).
5. **Penalty awarded**: inject ~+0.78 xG before the kick; shootout conversion ~76% (<60% under
   elimination pressure → lower λ in shootouts). VAR = no-trade window (~9% overturn prior).
6. **Half-time / fatigue**: H2 = 55–57% of goals; 76–90' hottest (~25%); skew remaining λ to 76–90,
   deflate 0–15. (Pre-HT "goal worth double" is mostly a myth — small home-only effect.)
7. **Subs**: trailing-team attacking subs before ~73' → ~2× comeback rate; nudge their λ up. Leader's
   defensive sub ≠ lock (turnovers keep Over live).
8. **KNOCKOUT flag flips late-draw sign**: 3-way/DNB settle on 90' only, so a late level draw is a
   TERMINAL paying outcome AND teams play for ET → back (don't lay) the 90' draw late in knockouts.

## Tricks (trigger → move → trade → caveat)
lay-the-draw, favourite-comeback, late-draw take-profit, O/U theta-decay scalp, goal-reaction
fade (on reopen), latency/courtsiding (mostly arbed away), knockout late-draw back.
See git history / research for full detail. Negative-skew on most (a goal at the wrong minute).
