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
- **Tactics** — late-equalizer draw take-profit, convergence take-profit, totals time-decay,
  xG momentum BUY, AND (new) the 4 event-driven tactics below.
- **Goal-overreaction fade** — when the pre-match UNDERDOG scores, the market over-moves; back the
  conceding favourite within a ~4-min window (Choi & Hui ~40%/min reversion).
- **Favourite-comeback** — a clear pre-match favourite (≥55%) that is trailing still has large
  residual equity until ~70'; back the comeback.
- **Red-card value** — right after a red card (~12-min window), the 11-man side's near-term scoring
  is front-loaded; flag value on it.
- **Knockout late-draw** — level + late + KNOCKOUT → back the 90' draw (terminal paying outcome +
  teams play for extra time) — the opposite sign to a league late-draw.

## Data-mined tactics (our own 26-match study — see INPLAY_FINDINGS.md)
Eight tactics added from the API-Football intra-game study of the first 26 WC fixtures
(xG, per-player stats, formations, event timing). Each is grounded in a recurring,
re-derivable pattern, not an anecdote (validated by `ops/_validate_signals.py`):
- **dormant_explosion** — quiet scoreline (≤1 goal) near HT WITH chances already created
  (high combined live xG) → back the OVER. (HT-quiet WC matches: 16/18 saw a 2H goal; the
  6 HT-goalless erupted 6/6, avg 3.0 2H goals; xG filter skips the 1 sterile case, Ghana.)
- **finishing_uplift_over** — WC finishing beats xG (goals−xG = +0.87, median +0.70); the
  pure-Poisson model under-prices the OVER → re-price with the empirical uplift.
- **xg_dominance_chase** — a side out-creating by ≥1.0 xG while NOT ahead is held back by
  variance that reverts → back it. (SUI 3.2, ESP 2.1, URU 1.7 all dominated xG yet drew.)
- **possession_trap_fade** — sterile control (≥58% possession, ≤0.8 xG) is not scoreboard
  value → fade it. (NED, PAN, POR all hoarded the ball without creating and failed to win.)
- **formation_fragility** — back-three/stretched shapes leak: 5-3-2 conceded 3.5/g, 3-4-2-1
  2.2/g vs 4-2-3-1's 1.27 → one-sided fragile lineup leans OVER / the solid side's attack.
- **lone_threat_removed** — a team whose shots funnel through ONE player (≥50% share: Messi
  60%, Haaland 56%, Ronaldo 60%) collapses when he is subbed off / sent off → fade it.
- **late_goal_bias** — 34% of WC goals land at 75'+, 59% in the 2H → don't lock UNDER early;
  back the late goal while the game is live with chances expected.
- **live_odds_crossval** — API-Football in-play bookmaker 1X2 as an independent third price
  source; when the sharp book leads the model / the venue lags both → earlier entry.

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
