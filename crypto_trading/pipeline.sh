#!/bin/bash
# crypto_trading pipeline (Plan 00 §7 / Plan 08 §5). Run from anywhere.
# Usage: ./crypto_trading/pipeline.sh <mode> [extra args passed through]
# Modes:
#   backfill   one-shot/daily top-up: candles 1m/1h/1d + funding, all active perps (keyless)
#   poll       prod REST poller daemon: books/trades/market stats (keyless)   [daemon]
#   record     WS recorder daemon (demo works with borrowed PM key today)     [daemon]
#   strips     event strike-strip recorder daemon (Plan 02 dataset, keyless)  [daemon]
#   pnl        generate PnL PDF report (Plan 07 §2)
#   risk       generate risk workbook/pdf/json/txt (Plan 07 §3; --trip to arm kill-switch)
#   daily      cron-able top-up: kalshi backfill + index composite + dominance + reports
#   signal     Plan 01 live dry-run signal (basis_meanrev; logs decay tracker)
#   bt         Plan 01 backtest on recorded data (--fees zero|projected)
#   implied    Plan 02 implied-dist + static-arb scan of today's captured strips
#   idxrecord  live 5s spot-composite recorder daemon (BTC,ETH)                [daemon]
#   liqrecord  offshore liquidation-stream recorder daemon (OKX proxy)         [daemon]
#   wf         run walk-forward + write validate artifacts: --strategy basis_meanrev|liq_reversion|perp_rotation|event_perp
#   select     MCPS daily param selection (smart_select; --build-centroids first time)
#   validate   PASS/FAIL gate: --strategy basis_meanrev|liq_reversion|perp_rotation|event_perp (exit 0=PASS)
#   review     weekly parameter health review (perp_rotation)
#   diskmon    disk free + growth-rate monitor; alerts (macOS notif + log) on breach
#   backup     tar the IRREPLACEABLE self-recorded data → ~/crypto_data_backup (repo-external)
#   brackets   TP/SL bracket watcher daemon (enforces armed stops; dry-run unless --live) [daemon]
#   status     heartbeats + storage usage + last data timestamps
#   test       pytest, no network
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
set -a; [ -f crypto_trading/.env ] && source crypto_trading/.env; [ -f .env ] && source .env; set +a

PY=(conda run -n someopark_run --no-capture-output python)
MODE="${1:-status}"; shift || true

case "$MODE" in
  backfill) exec "${PY[@]}" -m crypto_trading.crypto_common.kalshi.backfill "$@" ;;
  poll)     exec "${PY[@]}" -m crypto_trading.crypto_common.kalshi.poller "$@" ;;
  record)   exec "${PY[@]}" -m crypto_trading.crypto_common.kalshi.recorder "$@" ;;
  strips)   exec "${PY[@]}" -m crypto_trading.crypto_common.kalshi.strips "$@" ;;
  pnl)      exec "${PY[@]}" -m crypto_trading.crypto_common.reporting.pnl_report "$@" ;;
  risk)     exec "${PY[@]}" -m crypto_trading.crypto_common.reporting.risk_report "$@" ;;
  signal)   exec "${PY[@]}" -m crypto_trading.crypto_strategies.basis_meanrev.strategy signal "$@" ;;
  bt)       exec "${PY[@]}" -m crypto_trading.crypto_strategies.basis_meanrev.strategy backtest "$@" ;;
  implied)
    TODAY="$(date -u +%Y-%m-%d)"
    for S in KXBTC KXETH; do
      F="crypto_trading/price_data/kalshi/event_strips/prod/$S/markets/$TODAY.jsonl"
      [ -f "$F" ] && echo "=== $S $TODAY ===" && "${PY[@]}" -m crypto_trading.crypto_strategies.event_perp.signals.implied_dist "$F"
    done
    ;;
  idxrecord) exec "${PY[@]}" -m crypto_trading.crypto_common.refdata.index record --assets BTC,ETH "$@" ;;
  liqrecord) exec "${PY[@]}" -m crypto_trading.crypto_common.refdata.derivs liq-record "$@" ;;
  wf)        exec "${PY[@]}" -m crypto_trading.crypto_common.run_wf "$@" ;;
  select)    exec "${PY[@]}" -m crypto_trading.crypto_common.smart_select "$@" ;;
  validate)  exec "${PY[@]}" -m crypto_trading.crypto_common.validate "$@" ;;
  review)    exec "${PY[@]}" -m crypto_trading.crypto_strategies.perp_rotation.weekly_review "$@" ;;
  diskmon)   exec "${PY[@]}" -m crypto_trading.ops.disk_monitor "$@" ;;
  backup)    exec "${PY[@]}" -m crypto_trading.ops.backup_data "$@" ;;
  brackets)  exec "${PY[@]}" -m crypto_trading.crypto_common.bracket_watcher "$@" ;;
  watch)     exec "${PY[@]}" -m crypto_trading.crypto_strategies.live_watch.runner "$@" ;;
  watchlist) exec "${PY[@]}" -m crypto_trading.crypto_strategies.research_watchlist "$@" ;;
  daily)
    "${PY[@]}" -m crypto_trading.ops.backup_data --keep 5 || true    # protect recorded data FIRST
    "${PY[@]}" -m crypto_trading.ops.disk_monitor --quiet || true   # once/day = clean rate sample
    "${PY[@]}" -m crypto_trading.crypto_common.kalshi.backfill || true
    "${PY[@]}" -m crypto_trading.crypto_common.refdata.index backfill --assets BTC,ETH --days 3 || true
    "${PY[@]}" -m crypto_trading.crypto_common.refdata.onchain || true
    "${PY[@]}" -m crypto_trading.crypto_common.reporting.pnl_report || true
    "${PY[@]}" -m crypto_trading.crypto_common.reporting.risk_report || true
    echo "daily top-up done"
    ;;
  test)     exec "${PY[@]}" -m pytest crypto_trading/tests -q "$@" ;;
  status)
    echo "== heartbeats =="
    for hb in crypto_trading/price_data/kalshi/perps/ws/*/heartbeat.json \
              crypto_trading/price_data/kalshi/perps/poll/*/heartbeat.json \
              crypto_trading/price_data/kalshi/event_strips/*/heartbeat.json; do
      [ -f "$hb" ] && echo "--- $hb" && cat "$hb"
    done
    echo; echo "== storage =="
    du -sh crypto_trading/price_data/* 2>/dev/null || echo "(no data yet)"
    echo; echo "== parquet coverage =="
    "${PY[@]}" - <<'EOF'
from pathlib import Path
import pandas as pd
root = Path("crypto_trading/price_data/kalshi")
for sub in sorted(root.glob("perps/candles_*")):
    for f in sorted(sub.glob("*.parquet")):
        df = pd.read_parquet(f, columns=["ts"])
        lo = pd.to_datetime(df.ts.min(), unit="s", utc=True)
        hi = pd.to_datetime(df.ts.max(), unit="s", utc=True)
        print(f"{sub.name}/{f.stem}: {len(df)} bars  {lo:%Y-%m-%d %H:%M} → {hi:%Y-%m-%d %H:%M}")
for f in sorted((root / "funding").glob("*.parquet")):
    df = pd.read_parquet(f)
    nz = int((df.funding_rate != 0).sum())
    print(f"funding/{f.stem}: {len(df)} cycles ({nz} nonzero)")
EOF
    ;;
  *) echo "unknown mode: $MODE (backfill|poll|record|strips|pnl|risk|daily|signal|bt|implied|idxrecord|liqrecord|wf|select|validate|review|status|test)"; exit 2 ;;
esac
