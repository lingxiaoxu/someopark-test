# Intra-Game Findings — 26-Match API-Football Study

Source: the first 26 finished World Cup 2026 fixtures, mined from previously-untapped
API-Football data now ingested into the store:

| Table | What it holds | Pulled by |
|---|---|---|
| `fixture_stats` | per-team xG, shots, possession, corners (live + final) | `sync_fixture_stats` |
| `fixture_player_stats` | per-player per-match rating/shots/passes/dribbles/duels/tackles/fouls (1340 rows) | `sync_fixture_players` |
| `lineup` | formations, starting XI, coach | `sync_lineups` |
| `fixture_event` | minute-stamped goals/cards/subs | `sync_results` / `sync_live` |
| `match_odds` (`live_consensus`) | in-play bookmaker 1X2 | `sync_live_odds` |

Re-derive any number below with `python -m prediction_market.ops._analyze_intragame`;
validate the signal thresholds with `python -m prediction_market.ops._validate_signals`.

## Findings → signals

1. **Quiet half-times explode.** 18 matches had ≤1 goal at HT; **16 (89%) saw a 2H goal**.
   The 6 that were 0-0 at HT scored in the 2H **6/6, averaging 3.0 goals**. 59% of all 82
   goals fell in the 2H; **34% at 75'+**. → `dormant_explosion`, `late_goal_bias`.

2. **WC finishing beats xG.** mean(goals − total_xG) = **+0.87** (median +0.70, sd 1.78);
   goals exceeded total xG in 15/26 matches. A pure-xG/Poisson live model under-prices the
   OVER. → `finishing_uplift_over` (measured lean, NOT baked into the core model).

3. **xG-dominant-but-not-winning is a buy.** 3 sides dominated xG by ≥1.0 yet only drew:
   Switzerland 3.2 v 0.6 (1-1), Spain 2.1 v 0.2 (0-0), Uruguay 1.7 v 0.7 (1-1). The
   deserved goal tends to arrive. → `xg_dominance_chase`.

4. **Possession ≠ result.** The possession-dominant team **lost 6/26 (23%)** (TUR 72% poss
   → 0-2). The tradable subset is *sterile* control (high possession, low xG): NED 60%/0.8,
   PAN 64%/0.1, POR 75%/0.6 all failed to win. → `possession_trap_fade` (≥58% poss & ≤0.8 xG).

5. **Formation leaks.** Goals-against per game by shape: 5-3-2 **3.50**, 3-4-2-1 **2.20** vs
   4-2-3-1 **1.27** (most efficient: 4-2-3-1 also scored 2.09/g). → `formation_fragility`
   (only 5-3-2 / 3-4-2-1 — the two with real sample support).

6. **Single points of failure.** One player took ≥50% of his team's shots in several sides:
   Messi 60% (10), Haaland 56% (9), Ronaldo 60% (5). If that player leaves the pitch the
   team's threat collapses. → `lone_threat_removed` (subst OFF = event `assist`; or red card).

7. **Independent price source.** In-play bookmaker 1X2 (API-Football Odds In-Play) gives a
   third, often-faster price to cross-check against the model and our tradable venues
   (Kalshi/Poly). → `live_odds_crossval`.

All eight are pure, unit-tested functions in `strategy/inplay_tactics.py`, wired into
`strategy/inplay_arb.find_opportunities`, and fed live by `ingest/soccer_ingest.sync_live`
(stats + lineups + player stats + live odds every poll). They gracefully HOLD when their
data is absent, so an early-match poll with no stats yet never errors.
