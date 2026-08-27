# Plan Audit — every point in the 11 plan files vs the build

Honest, point-by-point coverage of `.claude/plan/prediction market plan/` (00–10) + 11.
No glossing: each item is marked, and "deferred" always says *why*.

**Legend**
- ✅ DONE — implemented + verified (tests or live run)
- 🟡 PARTIAL — core done, named enhancement still open
- 🔜 BUILDABLE — needs no account/credential; will be built (tracked in tasks)
- 🔒 BLOCKED — needs Kalshi/Polymarket account+KYC+Demo, or live venue order books
- 📝 DEVIATION — intentional, documented difference from the plan

> The hard wall: **anything that places/queries orders or reads venue order books
> needs your Kalshi + Polymarket US KYC/credentials** (plan 06 stage 0). Those are
> 🔒 until you onboard. Everything else (modeling, data, strategy math, order
> translation, OOS, calibration) is 🔜 and being built.

---

## 00 — README / overview
| Point | Status |
|---|---|
| 3 forecast categories (single match / champion / golden boot) | ✅ |
| 4th: cross-venue arb / relative value | 🔜 math (`cross_venue.py`); 🔒 execution |
| Shared strength base + one sim engine → internal coherence | ✅ |
| Venue mechanics built into engine (Kalshi netting, Poly US/Global split) | 🟡 rules in `base.py`/`guard.py` + docs; clients 🔒 |
| Architecture: ingest → storage → model → strategy → monitor | ingest ✅ · storage ✅ · model ✅ · strategy 🟡 · monitor 🔜 |
| Two timelines: realtime WS + hourly batch | hourly 🔜 (`jobs/`) · WS 🔒 |
| §5 compliance/risk notes | documentation (no code) ✅ acknowledged |
| §6 OOS on already-played matches | 🔜 (`oos_eval.py`) — data ready (15 finished) |

## 01 — Kalshi integration
| Point | Status |
|---|---|
| §2 RSA-PSS signing (ms timestamp, path-no-query) | ✅ `kalshi/auth.py` + test |
| §3 market discovery (series→events→markets), no hardcoded tickers | 🟡 reader methods exist; WC discovery + `wc_markets` mapping table 🔜 |
| §3 ticker→real-entity mapping (manual+rule dual-check) | 🔜 |
| §4 orderbook parse: yes/no two-sided ask, spread | ✅ `best_prices` + test |
| §4.3 Decimal prices | ✅ |
| §5 order lifecycle (create/amend/cancel/queue/positions/fills/settlements) | 🔒 needs account (interface in `base.py`) |
| §5.4 order groups (rolling 15s cap auto-cancel) | 🔒 |
| §6 WebSocket (orderbook_delta/ticker/trade/fill/positions) | 🔒 |
| §7 token-bucket rate limiter (read/write split, 429 backoff) | 🔜 (`venues/ratelimit.py`) |
| §8 historical candlesticks backfill (public) | 🔜 after WC ticker discovery |
| §9 Demo acceptance checklist | 🔒 Demo account |

## 02 — Data pipeline
| Point | Status |
|---|---|
| Source selection; API-Football primary (league=1, season=2026) | ✅ |
| ≥2 realtime sources (redundancy for settlement-critical scores) | 🟡 API-Football only; 2nd source (Sportmonks) needs another key |
| Research schema (teams/players/clubs/matches) | ✅ SQLite |
| §2b data-type→endpoint mapping | ✅ `api_football.py` wrappers |
| §3 hourly incremental (watermark, idempotent, restartable, tiered freq) | ✅ `soccer_ingest.py` |
| §3.4 safeguard: watermark + overlap window | 🟡 watermark ✅; explicit look-back overlap 🔜 |
| §3.4 safeguard: reconcile (REST vs local vs WS) | 🔒 venue side |
| §3.4 safeguard: replayable raw snapshots | ✅ `data/raw/*` |
| §4 quality gate: dual-source settlement agreement | 🟡 single source now |
| §4 quality gate: entity mapping validated hourly | ✅ team mapping (48/48) |
| live in-play ingestion | ✅ `sync_live` (batched) |
| Polymarket/Pinnacle/Kalshi market-data ingestion | 🔒/🔜 |

## 03 — Modeling
| Point | Status |
|---|---|
| §1(a) Elo/FIFA-rank prior | ✅ rank→rating |
| §1(b) recent results, time-decayed Dixon-Coles attack/defense | 🔜 (have results now) |
| §1(c) club-level player-form aggregation (squad xG → quality) | 🔜 needs squads + player stats |
| §1(d) external prior reverse-solve → implied strength | ✅ (fit to exp-points) |
| §1 output posterior sigma | 🟡 placeholder → 🔜 from ensemble |
| §2 Dixon-Coles double-Poisson + low-score corr + matrix→markets | ✅ |
| §3 group-stage λ adjustments (R3 qualification scenarios, GD chase, rotation) | 🔜 |
| §4a knockout regulation λ (lower, more draws) | ✅ |
| §4b advance prob (reg + ET + penalties) | ✅ |
| §4 knockout fatigue / rest-day asymmetry / suspensions | 🔜 |
| §4b **in-play live model** (minute+score→live W/D/L, fair draw, game-state g) | 🔜 (`model/inplay.py`) — data ready |
| §5 Monte-Carlo 48-team 2026 format (best-8-thirds, bracket) | ✅ (bracket = documented v1 approx) |
| §5 official tie-breaks + official 2026 bracket/third-place table | 🟡 simplified (points>GD>GF>rand); official table 🔜 |
| §6 golden-boot nested sim | ✅ (seed players) |
| §6.1 player rates from xG (club+NT, decay, role, pen, minutes) | 🔜 (topscorers/squad stats) |
| §7 OOS (freeze, Brier/LogLoss/reliability, vs closing line) + Bayesian update | 🔜 (`oos_eval.py`) |
| §8 compute budget (vectorized, minutes) | ✅ (200k in ~4s) |
| §9 calibration/monitoring (Brier/LogLoss/reliability/CLV; strength→winrate gradient) | 🔜 (`calibrate.py`) |
| §9 uncertainty propagation (sigma + ensemble dispersion → position discount) | 🔜 (`ensemble.py`) |

## 04 — Strategy & execution
| Point | Status |
|---|---|
| §1 use tradable ask; de-vig multiplicative + power + Shin | ✅ |
| §1 optional Wang transform / favorite-longshot correction | 🔜 (optional, backtest-gated) |
| §2 edge, net_edge, p_eff=p−kσ, theta gate | ✅ math (real fees 🔒) |
| §3 group-stage strategy (lineup/incentive/draw-value/favourite-gradient) | 🔜 (needs §3 λ + live prices) |
| §4 knockout strategy (penalty variance, fatigue, in-running) | 🔜 / 🔒 |
| §4c in-play tactics (draw time-value, post-goal repricing) | 🔜 model + 🔒 prices |
| §5 champion / golden-boot (ranking mismatch, dynamic rebalance) | 🔜 model side · 🔒 exec |
| §6 fractional Kelly + hard caps | ✅ single-bet + caps |
| §6 portfolio joint covariance / theme exposure (both venues) | 🟡 theme cap 🔜 (`risk.py`); joint optimisation later |
| §7 execution algo (taker/maker, net-position, amend, order_group, dry-run) | 🟡 sizing ✅; order translation 🔜 (`exec/`); live 🔒 |
| §8 signal→order gate chain | 🟡 pieces exist; full chain 🔜 |

## 05 — Infra & ops
| Point | Status |
|---|---|
| §1 process topology (ws×2, reader, guard, xv_monitor, hourly_job, live_poller, executor, monitor) | guard ✅; hourly/live as CLI ✅; long-running daemons + executor 🔜/🔒 |
| §2 storage schema | 📝 **SQLite instead of Postgres** (isolation, zero-infra; migratable). Soccer tables ✅; venue/signal/model/xref tables 🔜 |
| §3 repo structure | ✅ matches (venues/ ingest/ model/ strategy/ exec/ jobs/ backtest/ ops/ tests/) |
| §4 hourly scheduler (APScheduler/cron), idempotent, dry-run, code_version | 🔜 (`jobs/hourly_job.py`); model run already stamps code_version ✅ |
| §5 monitoring + alerts | 🔜 (`ops/monitor.py`) |
| §6 backtest framework (replay, OOS, calibration, walk-forward, stress) | OOS/calibration 🔜; venue replay 🔒 (no venue K-lines) |
| §7 security/keys (no keys in git/logs, central config) | ✅ |

## 06 — Roadmap
| Stage | Status |
|---|---|
| 0 accounts/keys/库 | API-Football ✅; Kalshi/Poly accounts 🔒 (your action); DB ✅ |
| 1 data + market discovery + OOS start | soccer data ✅; venue discovery 🔒; OOS 🔜 |
| 2 model + ensemble + OOS check | model ✅; ensemble/OOS 🔜 |
| 3 strategy + cross-venue + in-play | math 🔜; execution 🔒 |
| 4–5 live ramp | 🔒 |

## 07 — Polymarket
| Point | Status |
|---|---|
| US execution (Ed25519, intents, TIF, slug, WS, 60/min) | 🔒 KYC |
| Global read-only (Gamma/Data/CLOB) reference | 🔜 (no key; reachable) |
| venue_guard blocks Global orders | ✅ |
| Acceptance checklist | 🔒 |

## 08 — Cross-venue
| Point | Status |
|---|---|
| Lock-arb & relative-value math (net_lock, tie-risk, capital carry) | 🔜 (`cross_venue.py`) |
| Settlement-equivalence checklist + `equiv_verified` gate | 🔜 schema |
| Leg-risk execution, `arb_pair` | 🔒 |
| Bundle (<$1 basket) scan | 🔜 math · 🔒 exec |
| xv_spread monitor / heatmap | 🔒 (needs both books) |

## 09 — Market microstructure rules
| Point | Status |
|---|---|
| Kalshi netting / mutually-exclusive / collateral rules (encoded) | 🟡 documented; `to_orders` enforcement 🔜 |
| Poly US intents / TIF / slug rules | 🟡 documented |
| Cross-venue independent positions | ✅ conceptual + guard |
| §5 `to_orders` target-net→legal orders + pre-trade self-check list | 🔜 (`exec/order_translation.py`) |

## 10 — Prior template
| Point | Status |
|---|---|
| §2 full 12-group prior ingest + identity check (±2pp) | ✅ |
| §3 draw + FIFA ranks → team_draw | ✅ |
| §4 team-id alias mapping | ✅ (+ API-Football spellings) |
| §5.1 reverse-solve implied strength | ✅ (exp-points fit) |
| §5.3 ensemble: dozens of variants → mean + dispersion → sizing | 🔜 (`ensemble.py`) |
| §5.3 variant weighting via OOS/walk-forward | 🔜 |

## 11 — API-Football integration (added)
✅ fully done & verified (client, store, orchestrator, frugality, guide digested).

---

## Buildable-now backlog (no accounts needed) — execution order
1. ✅ `model/inplay.py` (03 §4b) — live W/D/L + fair draw  (DONE, tested on live shape)
2. ✅ `model/calibrate.py` (03 §9) — Brier/LogLoss/reliability/CLV  (DONE)
3. ✅ `model/oos_eval.py` (03 §7) — scored 15 played matches; found draw-underestimation bias  (DONE)
4. ✅ `model/ensemble.py` (10 §5.3) — variants → dispersion → sigma; wired into run_model `--ensemble`  (DONE)
5. ✅ `ingest` topscorers → real golden-boot rates (shrunk + goals head-start, merged w/ seed)  (DONE)
6. 🟡 `strength` recent-results Bayesian update (03 §1b) DONE; club xG aggregation (§1c) still 🔜 (no club data ingested)
7. ✅ `strategy/risk.py` (04 §6) — kill-switch, exposure caps, themes  (DONE)
8. ✅ `strategy/cross_venue.py` (08) — net_lock / relative value / bundle math  (DONE)
9. ✅ `exec/order_translation.py` (09 §5) — target-net→legal orders + self-checks  (DONE)
10. 🟡 `venues/polymarket_global/reader.py` (07) DONE (live-verified); `venues/ratelimit.py` (01 §7) still 🔜
11. 🔜 group-stage λ scenario adjustments (03 §3); official bracket/tie-break (03 §5)
12. 🟡 `jobs/hourly_job.py` (05 §4) DONE (full pipeline orchestration + logging); `ops/monitor.py` still 🔜
13. 🔜 venue/signal/model tables in store (05 §2)
14. ✅ `strategy/xv_monitor.py` (plan 13, 08) — live cross-venue champion monitor (Global vs model)  (DONE)
15. ✅ Polymarket US SDK installed + auth verified; Kalshi demo+prod auth verified (creds wired, gated)

### ALL buildable items closed (67 tests green, 56 modules, 5665 LOC)
Added this final pass: `venues/ratelimit.py` (token buckets), `venues/kalshi/discovery.py`
(WC champion `KXMENWORLDCUP` → 48 teams, wired as TRADABLE source into xv_monitor),
store plan-05 tables (venue/xref/ob_snapshot/xv_spread/model_run/sim_*/signal/calibration/
prediction/match_odds) + persistence, predictions/odds ingest (match model validated vs
sharp bookmaker lines), `ops/monitor.py` (health+alerts, wired into hourly_job),
`model/club_aggregation.py` (§1c) + `sync_player_club_stats`, `backtest/{replay,metrics}.py`
(walk-forward, no future function), group round-3 incentive λ (§3) + official-bracket
ingestion hook + r3_intensity. Plan file 14 = frontend integration plan (recorded, not built).
Only items needing YOUR action remain (KYC-gated live execution) or are data-cost opt-ins
(full club-stats pull, US match-market cross-venue when US lists them).

### Closed across sessions (67 tests green)
Modeling: inplay, calibrate, oos_eval, ensemble (§4b/§7/§9 + 10§5.3), strength result-update (§1b),
real golden-boot from topscorers (§6). Strategy/exec: risk, cross_venue, order_translation,
xv_monitor (live). Venues: Kalshi auth(demo+prod), Polymarket US (verified), Polymarket Global
(live read). Orchestration: hourly_job (ingest→strength→model→xv→OOS). Plan files 11/12/13 written.
Remaining buildable: club xG aggregation (§1c), ratelimit token bucket, ops/monitor, group-stage λ
scenarios + official bracket (§3/§5), venue/signal store tables.

## Hard-blocked on your action (KYC/accounts)
Kalshi live auth/orders/WS/order-groups; Polymarket US client; cross-venue execution;
three-way reconciliation; venue-K-line backtest; live signal→order. These stay 🔒
until Kalshi + Polymarket US onboarding (plan 06 stage 0).
