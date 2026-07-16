# crypto_trading — Kalshi crypto-perp strategies (data layer)

Standalone tree (nothing outside this folder is touched), `someopark_run` env.
Plans live in `crypto-dev/` at repo root — read `00_INDEX.md` (Addendum) and
`08_data_preparation.md` §0 (probe-verified API facts) first.

## Status (2026-07-12)
- **All 8 plans built.** Plan 00 infra (kalshi connectivity, refdata, loader, costs,
  sizing, risk_kill, execution, regime, metrics, walk_forward, smart_select, validate,
  intraday_sim + daily_engine); Plans 01/02/05 strategies; Plan 03 carry + Plan 04
  mechanism (prototype/gated per plan); Plan 06 risk (2-layer); Plan 07 reporting;
  Plan 08 data (backfill + 5 recorders). **195 tests green; deep-tested 12/12 without
  the prod key** (public reads + local data + dry-run only).
- **Order wire format VERIFIED against docs.kalshi.com + a real prod fill (2026-07-12).**
  Fixed imagined fields that would have failed every live order: `side` bid/ask (not
  buy/sell), `count` 2-dp string "1.00", `subaccount` (real acct = #64). See execution.py.
- **Backtest headlines on real data (candle-tape, optimistic depth — directional, not a
  validation gate):**
  - Plan 01 basis, 33 days, real fees (maker 5bps/taker 10bps): zero-fee +$99/$1000,
    **TAKER −$19, MAKER +$40** → passive execution is the difference between loss and
    profit. `pipeline.sh bt --role maker`.
  - Plan 05 rotation: OKX 2yr weekly +27.4% Sharpe 0.36 (beats EW); Kalshi 33d Sharpe 1.31;
    maxDD −51% (vol-target uncalibrated).
  - Plan 03 carry: funding harvest real but tiny (~4%/yr), swamped by directional P&L,
    negative under fees → belongs in Plan 05 cross-section.
  - Plan 02 event: no fee-positive static arb; dislocation IC +0.13/+0.20 (weak, 4-day).
  - Plan 04 liq: naive taker fade dead; OI-drop + liquidation signature reverts 27–31bps
    (beats cost) — promising, needs the native detector + more data.
- **Account margin ENABLED (verified 2026-07-12).** Biggest operator gate cleared. Remaining
  operator items: (1) dedicated crypto prod key, (2) fund margin subaccount. DO NOT trade
  live until `validate` PASSES — being able-to-trade ≠ ready (Plan 01 taker is still −$19).
- **TP/SL brackets — ADDED + ENFORCED (`bracket.py` + `bracket_watcher.py`).** Kalshi's margin API
  has NO native stop/trigger/OCO (verified) — so the app AND this code do TP/SL client-side. Plan 01
  ARMS a bracket (config `bracket:`, honored) into a PERSISTED state file on entry; the
  `brackets` watcher daemon loads it, polls the public mark, and fires a reduce_only IOC close on
  trigger (demo-first gated → dry-run until live). Persistence = a restart re-arms, not forgets.
  Matches app geometry (short → TP below / SL above). Order price is tick-snapped, count validated
  to 0.01 multiples, reconcile is subaccount-aware. CAVEAT: client-side only fires while the daemon
  runs — for an always-on backstop set TP/SL in the Kalshi app too. `pipeline.sh brackets`.

## Quick start
```bash
./crypto_trading/pipeline.sh status          # heartbeats + data coverage
./crypto_trading/pipeline.sh backfill        # daily candle/funding top-up (keyless)
./crypto_trading/pipeline.sh poll            # prod poller daemon (keyless)
./crypto_trading/pipeline.sh strips          # event-strip daemon (keyless)
./crypto_trading/pipeline.sh record          # WS recorder (demo now; prod after key)
./crypto_trading/pipeline.sh test            # pytest, no network
./crypto_trading/pipeline.sh diskmon         # disk monitor (exit 1=warn, 2=critical — by design)
./crypto_trading/pipeline.sh bt --role maker # Plan 01 backtest, passive(maker)-fee scenario
./crypto_trading/pipeline.sh validate --strategy basis_meanrev   # PASS/FAIL gate (exit 0=PASS)
```
No prod key is needed for any of the above — REST market data is public, and the
order path stays dry-run behind the demo-first gate. Only prod WS recording and
live orders need the dedicated key + funded margin subaccount.

## Disk / ops monitoring
`diskmon` watches free space + daily growth rate and alerts (macOS notification
+ `logs/disk_monitor.log` + `logs/disk_monitor_status.json`) when: free < 25 GB
(warn) / < 10 GB (critical), daily growth > 2 GB/day (catches runaway/duplicate
recorders), or < 45 days to full. Thresholds are constants at the top of
`ops/disk_monitor.py`. It runs automatically inside `pipeline.sh daily` (one
clean rate sample/day, gzip-sawtooth-aware). Recorder footprint ≈ 80–90 MB/day
compressed (gzip rotation at UTC midnight); event_strips dominates.

## Keys (Plan 08 §2)
REST data needs no key. Demo WS borrows the prediction_market demo key
(read-only, enforced in `config.kalshi_key`). Prod WS + any trading need the
dedicated keys in `crypto_trading/.env`. Account margin opt-in
(`/margin/enabled`) — **DONE** (verified 2026-07-12 on the prod account). The
live order gate (execution.py) still requires ALL of: `KALSHI_ENV=prod` +
`ALLOW_LIVE_ORDERS=1` + `/margin/enabled=true` + a dedicated (non-borrowed) key.

## Layout
```
crypto_common/
  config.py timeutils.py io_jsonl.py
  kalshi/  auth ratelimit enums rest_margin rest_event ws book
           recorder backfill poller strips
tests/                     # pytest, no network
price_data/kalshi/         # gitignored: perps/{candles_*,ws,poll}, funding/, event_strips/, refdata/
```
