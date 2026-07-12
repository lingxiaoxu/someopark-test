# crypto_trading — Kalshi crypto-perp strategies (data layer)

Standalone tree (nothing outside this folder is touched), `someopark_run` env.
Plans live in `crypto-dev/` at repo root — read `00_INDEX.md` (Addendum) and
`08_data_preparation.md` §0 (probe-verified API facts) first.

## Status (2026-07-07)
- **Plan 00 infra: BUILT + tested.** kalshi/ connectivity, refdata/ (spot composite =
  BRTI proxy, offshore proxy w/ OKX+KrakenFutures fallback for US geo-blocks, BTC
  dominance), loader (no-NaN feature frames), costs (fees/funding/depth-walk slippage),
  sizing, risk_kill (persistent halts), execution (HARD demo-first gate, dry-run),
  regime + metrics (copied from sector_rotation, 365-day), walk_forward (full
  DSR/WFE/MCPS chain copied), backtest/intraday_sim (event-driven, depth-walking).
- **Plan 01 basis_meanrev: BUILT.** signals (z + OU half-life + hysteresis), candle-tape
  backtest on real data (day-1 result: +$4.47/$1000 zero-fee, +$1.18 projected — fees eat
  73%, passive-first execution is mandatory), live dry-run signal + decay tracker.
- **Plan 06 risk: BUILT.** tier-0–5 metrics (RiskManager.py-faithful VaR/CF/CVaR/CDaR/
  Litterman), portfolio aggregator + amber/red limits + kill-switch.
- **Plan 07 reporting + Plan 08 data: BUILT.** ledger + PnL/risk reports; 513k candles
  backfilled (13 perps since launch), daemons: prod poller, event strips, demo WS.
- **Not built yet (staged per plans):** Plan 02 implied-dist signal (its dataset is
  recording NOW), Plan 03 (gated: measured skew 39+/1− but thin ~5.4%/yr), Plan 04
  (needs cascade observations), Plan 05 rotation cluster (next phase: portfolio/
  optimizer/universe/daily_engine copies). LIVE ORDERS blocked on operator items:
  Kalshi margin opt-in + dedicated prod key.

## Quick start
```bash
./crypto_trading/pipeline.sh status          # heartbeats + data coverage
./crypto_trading/pipeline.sh backfill        # daily candle/funding top-up (keyless)
./crypto_trading/pipeline.sh poll            # prod poller daemon (keyless)
./crypto_trading/pipeline.sh strips          # event-strip daemon (keyless)
./crypto_trading/pipeline.sh record          # WS recorder (demo now; prod after key)
./crypto_trading/pipeline.sh test            # pytest, no network
./crypto_trading/pipeline.sh diskmon         # disk free + growth-rate monitor (alerts on breach)
```

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
dedicated keys in `crypto_trading/.env` + the Kalshi margin/perps account
opt-in (`/margin/enabled` must be true).

## Layout
```
crypto_common/
  config.py timeutils.py io_jsonl.py
  kalshi/  auth ratelimit enums rest_margin rest_event ws book
           recorder backfill poller strips
tests/                     # pytest, no network
price_data/kalshi/         # gitignored: perps/{candles_*,ws,poll}, funding/, event_strips/, refdata/
```
