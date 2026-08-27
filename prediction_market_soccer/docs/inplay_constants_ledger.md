# In-Play Tactic Constants — Re-Estimation Ledger (TRANSFORM_PLAN R5)

**Why this file exists.** All 17 in-play tactics in `strategy/inplay_tactics.py` were
carried into the club module unchanged, and their thresholds were calibrated on
**26 World Cup 2026 fixtures** (`docs/INPLAY_FINDINGS.md`, re-derivable with
`ops/_analyze_intragame.py`, validated by `ops/_validate_signals.py`). Twenty-six
national-team matches at a single tournament is not a club-football sample. Risk R5
of the transform plan therefore says: *ship them as-is, but register every constant
here with its provenance and the sample it needs before anyone re-estimates it.*

**Nothing in this file has been re-estimated or changed.** It is the register plus a
first look at the club data, so the eventual R5 study starts from evidence rather
than from scratch.

**Scope note.** The `*_advance` fork (`strategy/inplay_tactics_advance.py`) and the
corner block at the bottom of `inplay_tactics.py` were added after the n=26 study and
carry their own provenance; they are listed in §4 for completeness.

---

## 1. Club sample as of 2026-08-27

Counts are live reads from `data/soccer.db` (competitions: the 12 enabled entries in
`config/leagues.py`). Re-run them with the queries in §5.

| Data the re-estimation needs | Table | Club fixtures | vs WC study |
|---|---|---:|---:|
| Settled fixtures (incl. 2025–26 historical backfill) | `fixture` | **5,221** | 26 |
| Goal events, minute-stamped | `fixture_event` (`type='Goal'`) | **191** | 26 |
| Lineups with a formation | `lineup.formation` | **201** | 26 |
| Possession / shots / corners | `fixture_stats` | **202** (corners 203) | 26 |
| **xG** | `fixture_stats.xg` | **118** | 26 |
| Per-player shot counts | `fixture_player_stats.shots_total` | **162** | 26 |
| Milestone price snapshots (PRE…FT) | `milestone_snapshot` | **164** (163 with both PRE and FT) | 26 |
| **Per-minute venue prices** | `price_tick` | **1** (2,619 rows) | 26 |
| Per-minute advance prices | `price_tick_adv` | **0** | 26 |
| Live bookmaker 1X2 | `match_odds` (`live_consensus`) | **7** | 26 |
| Settled bet ledger | `settled_bet` | **163** | — |
| In-play signal review lines | `data/logs/inplay_review*.jsonl` | 106 + 91 (2 days) | ~3,600 (9 days) |

The 5,221 settled fixtures are a **results-only** backfill. Every intra-game table
starts at **2026-08-12** — the module's own live window — so the honest denominator
for any tactic that needs xG, formations, possession or prices is the 118–202 range,
not 5,221.

Per-competition split of the fixtures carrying the **full** intra-game set
(xG + lineup + events + player stats), n = 118:

| argentina | brasileirao | laliga | libertadores | sudamericana | seriea | ligue1 | epl | bundesliga · ucl · uel · uecl |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 28 | 19 | 18 | 14 | 13 | 10 | 9 | 7 | 0 |

Per-competition split of `milestone_snapshot`, n = 164: uecl 39 · uel 18 · laliga 16 ·
brasileirao 16 · argentina 15 · ucl 14 · seriea 10 · epl 10 · libertadores 9 ·
sudamericana 9 · ligue1 8 · bundesliga 0.

**Read those two rows together.** The competitions with the deepest intra-game data
are the ones with the shallowest price data, and vice versa. A tactic needing *both*
(anything that compares model fair value to a traded price) has a much smaller
effective n than either column suggests.

---

## 2. The register — 17 tactics

`Gate` = the sample the constant needs before it may be re-estimated.
`Status` = whether that sample exists **today**.

### 2a. Data-mined on the WC n=26 study — highest priority

| # | Tactic | Constant(s) → current value | WC evidence | Gate | Status |
|---|---|---|---|---|---|
| 9 | `dormant_explosion` | `DORMANT_MIN_FROM` 40, `DORMANT_MIN_TO` 70, `DORMANT_MAX_GOALS` 1, `DORMANT_XG_MIN` 1.0, `DORMANT_REMAINING_GOALS` 0.8 | 6/6 HT-goalless matches saw 2H goals (avg 3.0); 16/18 quiet HTs | ≥100 matches with goal events **and** xG | ✅ **ready** (118) |
| 10 | `finishing_uplift_over` | `FINISHING_UPLIFT` **0.4** (shrunk from the mined +0.87) | mean(goals − total xG) = +0.87, median +0.70, sd 1.78 | ≥100 matches with xG | ✅ **ready** (118) — **and it fails, see §3** |
| 11 | `xg_dominance_chase` | `XG_CHASE_EDGE` 1.0, `XG_CHASE_MAX_MIN` 80 | 3 sides out-created by ≥1.0 xG yet only drew | ≥100 matches with xG; ≥25 trigger events | ✅ sample / ⚠️ trigger count unmeasured |
| 12 | `possession_trap_fade` | `POSSESSION_TRAP_MIN` 0.58, `POSSESSION_TRAP_XG_MAX` 0.8, min 35' | possession-dominant side lost 6/26 (23%) | ≥100 matches with possession **and** xG | ✅ **ready** (118) |
| 13 | `formation_fragility` | `FRAGILE_FORMATIONS` = {`5-3-2`, `3-4-2-1`} | GA/g 3.50 and 2.20 vs 4-2-3-1's 1.27 | ≥20 matches **per shape** (not 100 total — this is a per-shape estimate) | ❌ **not ready** (5-3-2: n=8) — **and the sign is wrong, see §3** |
| 14 | `lone_threat_removed` | `LONE_THREAT_SHARE` 0.50, window ≤88' | Messi 60%, Haaland 56%, Ronaldo 60% of team shots | ≥100 matches with per-player shots; ≥25 removal events | ✅ sample (162) / ⚠️ removal events unmeasured |
| 15 | `late_goal_bias` | `LATE_GOAL_FROM_MIN` 70, exp-remaining ≥0.45, ≤88' | 34% of goals at 75'+, 59% in the 2H | ≥100 matches with goal events | ✅ **ready** (191) |

### 2b. Tuned on the WC per-minute price-tick study

| # | Tactic | Constant(s) → current value | WC evidence | Gate | Status |
|---|---|---|---|---|---|
| 2 | `model_overshoot_take_profit` | `OVERSHOOT_MARGIN` **0.22**, headroom-capped (`model/inplay_constants.overshoot_trigger`) | WC value was 0.12 — and lived as **four copies** (0.12/0.12/0.08/0.08) until 2026-08-27, so "the" threshold depended on which module asked. Club re-derivation: two-sided \|market−fair\| median 0.22 over 136 observations; 0.12 fired on 30% of all observations, i.e. on baseline noise. An absolute margin is also unreachable at a high fair (0.79 + 0.22 > 1.00), so the effective trigger is `min(0.22, 0.45 × (1 − fair))`. | ≥100 fixtures of live price history | ✅ **re-derived** from 117 live observations; revisit when `price_tick` fills |
| 1 | `convergence_take_profit` | `LOCK_FRACTION` 0.88, `MIN_TAKE_PROFIT_GAIN` 0.12, `_LOCK_BID_GAP` 0.05 | plan 04 §7 defaults, exit timing sanity-checked on the same tick study | ≥100 fixtures of `price_tick` | ❌ **blocked** — 1 fixture |
| 4 | `totals_time_decay` | shares `LOCK_FRACTION` / `MIN_TAKE_PROFIT_GAIN`; line 2.5 | same | as above, plus a recorded totals book | ❌ **blocked** |

`price_tick` is the binding constraint for the whole exit family. Until
`ops/backfill_price_ticks` has run across the settled club fixtures, three of the
highest-impact constants in the module **cannot be re-estimated at all** — they can
only be watched. This is the single most valuable unblock in R5.

### 2c. Literature-anchored, never fitted on WC data

These came from published football analytics, not from the n=26 mining, so a club
re-estimate is a *validation* rather than a correction. Lower priority — but the
direction should still be confirmed once the trigger counts exist.

| # | Tactic | Constant(s) → current value | Source | Gate | Status |
|---|---|---|---|---|---|
| 6 | `goal_overreaction_fade` | `GOAL_FADE_WINDOW` 4, `FADE_MIN_FAV_EQUITY` 0.15 | Choi & Hui: ~40%/min reversion, gone by 5–6' | ≥50 surprising-goal events with prices | ❌ blocked (prices) |
| 7 | `favourite_comeback` | `FAV_COMEBACK_MAX_MIN` 70, `FAV_COMEBACK_MIN_PROB` 0.55 | favourite-longshot bias literature | ≥50 trailing-favourite states | ⚠️ measurable from `fixture_event` |
| 8 | `red_card_value` | `RED_CARD_WINDOW` 12, `MIN_REMAINING_GOALS` 0.30 | ~56% of the extra goals land within 15' of the card | ≥30 red cards in club fixtures | ⚠️ count not yet taken |
| 3 | `draw_trade_signal` | `LATE_MINUTE` 75, `DRAW_LOCK_FAIR` 0.74, `EARLY_MINUTE` 35, entry edge 0.03 | plan 03 §4b live-model geometry | ≥100 fixtures of `price_tick` | ❌ blocked (prices) |
| 5 | `momentum_value` | `xg_edge` 1.0, ≤80' | weaker sibling of #11, same evidence base | with #11 | ✅ sample |
| 16 | `live_odds_crossval` | `move_threshold` 0.06 (+0.04 on the unconfirmed branch) | in-play book as an independent third price | ≥100 fixtures with `live_consensus` odds | ❌ **blocked** — 7 fixtures |

### 2d. Competition-dependent by construction

| # | Tactic | Constant(s) | Note |
|---|---|---|---|
| 17 | `knockout_late_draw` | `LATE_MINUTE` 75; stance from `model/knockout_late_draw._FIT` | ⚠️ **This row was wrong twice.** (a) The direction is NOT set by `ko_draw_semantics` — that only decides whether the fixture is a knockout at all; the stance comes from `family_for(caps)` via `caps.leg`/`caps.advance`. (b) The 2026-08-26 club refit that appeared to reverse the WC sign (cup_decider ratio 0.689, z=−3.58) was **survivorship-biased**: it read `WHERE status_short='FT'`, but a knockout level at 90' goes to extra time and settles AET/PEN, so the query dropped the very outcome it measured — and only in the knockout families (137 AET/PEN fixtures, 1 of them a league game). Refit including AET/PEN and reading the regulation score: n 185→249, ratio 0.689→**0.939**, z −3.58→**−0.77**; the league family is bit-identical. **No family is significant, so every stance shrinks to neutral** — the WC signal set was right to carry over, and this tactic simply has no measured edge either way. |

---

## 3. First look at the club data — **NOT adopted, for prioritisation only**

Computed read-only over the club fixtures above. These are not re-estimates: the
window is two weeks long and per-competition n is thin. They exist so R5 knows where
to look first.

**① `FINISHING_UPLIFT` (#10) does not survive contact with club football.**

| | WC n=26 | Club n=118 |
|---|---:|---:|
| mean(goals − total xG) | **+0.87** | **−0.15** |
| median | +0.70 | −0.26 |
| sd | 1.78 | 1.36 |

The tactic's entire premise — finishing beats chances, so a pure-xG live model
under-prices the OVER — reverses sign on club data. Both samples clear the gate. The
existing guards mean this is not currently losing money on its own (`finishing_uplift_over`
refuses to fire without a real quote and without the model *already* seeing an edge, so
the uplift only ever widens a pre-existing edge), but it is now widening edges in a
direction the club data does not support. **Highest-priority item in R5.**

**② `FRAGILE_FORMATIONS` (#13) points the wrong way and is below its gate.**

| Shape | WC GA/g | Club GA/g | Club n (team-matches) |
|---|---:|---:|---:|
| 4-2-3-1 (the WC "solid" reference) | 1.27 | **1.30** | 162 |
| 3-4-2-1 (flagged fragile) | 2.20 | **1.10** | 21 |
| 5-3-2 (flagged fragile) | 3.50 | **0.88** | 8 |

The reference shape reproduces almost exactly (1.27 → 1.30), which is reassuring about
the measurement. The two "fragile" shapes do not: in the club sample they concede *less*
than the reference, i.e. the tactic currently leans OVER on the two shapes that are
holding up best. n=8 for 5-3-2 is far below the per-shape gate, so this is a **warning,
not a verdict** — but it is the second thing R5 should test, and the WC 3.50/2.20 figures
should be treated as small-sample noise until a club sample says otherwise.

**③ The goal-timing family broadly holds.**

| | WC n=26 | Club n=190 (581 goals) |
|---|---:|---:|
| goals at 75'+ | 34% | **27%** |
| goals in the 2H | 59% | **59%** |
| quiet HT (≤1 goal) → a 2H goal | 16/18 = 89% | 113/125 = **90%** |

`dormant_explosion` (#9) reproduces almost exactly. `late_goal_bias` (#15) is directionally
intact but the late window is thinner than the WC's (27% vs 34%), so `LATE_GOAL_FROM_MIN`
and the 0.45 expected-remaining floor deserve a re-fit rather than a rewrite.

---

## 4. Adjacent constants, same provenance problem

Registered here so they are not forgotten, though the plan's "17" does not count them.

- **Corner block** (`corner_total_signal`, added after the n=26 study):
  `MIN_CORNER_MINUTE` 12, `MAX_CORNER_MINUTE` 89, `MIN_CORNER_EDGE` 0.07, plus
  `CORNER_TOTAL_PRIOR` in `model/inplay_corners.py`. Corner *counts* exist for 203 club
  fixtures, but no corner **quotes** have been recorded yet, so the edge threshold is
  unvalidatable on the market side.
- **Advance fork** (`strategy/inplay_tactics_advance.py`): its own constants, sourced
  from the WC advance-market review (plan 24). `price_tick_adv` holds **0** club
  fixtures, so it is blocked on the same tick backfill as §2b.
- **Live-model constants** (`model/inplay.py`): `RED_CARD_LAMBDA_MULT` 0.70, lead/trail
  0.92 / 1.10, `XG_WEIGHT` 0.35, `LATE_LEVEL_DEFLATE` 0.30 — documented in
  `docs/INPLAY_SCENARIOS.md`. These are model parameters rather than tactic thresholds
  and are re-fitted by the calibration path, not by this ledger.

---

## 5. How to refresh this ledger

Sample counts (read-only; safe against the live pipeline):

```sql
-- run with:  sqlite3 -readonly prediction_market_soccer/data/soccer.db
SELECT COUNT(*) FROM fixture f
 WHERE f.status_short IN ('FT','AET','PEN') AND f.home_goals IS NOT NULL
   AND f.league_id IN (39,140,135,78,61,2,3,848,13,11,71,128)
   AND EXISTS(SELECT 1 FROM fixture_stats s
               WHERE s.fixture_api_id=f.api_id AND s.xg IS NOT NULL)
   AND EXISTS(SELECT 1 FROM lineup       l WHERE l.fixture_api_id=f.api_id)
   AND EXISTS(SELECT 1 FROM fixture_event e WHERE e.fixture_api_id=f.api_id)
   AND EXISTS(SELECT 1 FROM fixture_player_stats p WHERE p.fixture_api_id=f.api_id);

SELECT COUNT(DISTINCT fixture_api_id) FROM price_tick;          -- the exit-family gate
SELECT COUNT(DISTINCT fixture_api_id) FROM milestone_snapshot;
SELECT COUNT(DISTINCT fixture_api_id) FROM match_odds WHERE bookmaker='live_consensus';
```

The re-estimation itself follows the WC methodology unchanged — `ops/_analyze_intragame.py`
to mine, `ops/_validate_signals.py` to confirm each threshold is a recurring pattern and
not an anecdote. Both are `[闲置]` research scripts, kept in the tree for exactly this.
Two cautions when they are re-run against club data:

1. They open `prediction_market_soccer/data/soccer.db` directly and their headers still
   say "26 finished WC fixtures". Point them at a **copy** — the live pipeline holds the
   production DB, and neither script was written for a 5,000-fixture table.
2. Split by competition before pooling. A UECL qualifier and a Premier League round are
   not one population — that is the same reasoning that gave every competition its own
   calibrator (§3.5) and its own alt-data weights.

**Update this file whenever a constant is re-estimated**: record the old value, the new
one, the club n it was fitted on, and the competition split. A constant that has been
re-estimated stops being an R5 item and becomes a normal fitted parameter.
