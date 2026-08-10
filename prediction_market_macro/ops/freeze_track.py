"""ops/freeze_track.py — derive the displayed history segment from a stored walk-forward.

`frontend_export` shows a two-part track record: a frozen backtest before
`TRACK_CUTOVER` and the live production ledger after it. The frozen part lives in the
`experiments` row named `track_history`, and until this module existed that row had been
inserted by hand — which means the displayed number could not be reproduced from anything,
and nothing stopped the next hand-written row from being a different window, a different
stream, or a window that overlapped the live segment.

This makes it a derivation with guards instead.

**Which stream, and why not the better-looking one.**
A walk-forward stores three: `edge` (the gated `decide()` stream), `argmax` (the flat-$1
favourite leg), and `hybrid` (the live rule — edge if it fired, else the favourite). The
live segment of the display counts `kind IN ('open','argmax','arb','snipe')`, i.e. BOTH
streams. So the history segment has to be `hybrid` or the two halves of the same displayed
track record are measuring different strategies, and the combined ROI is a number that
describes nothing.

On the frozen 2026-05-17..07-31 run `edge` shows ROI -9.5%, `argmax` -14.6% and `hybrid`
-9.2%, so today the flattering choice happens to also be `hybrid`. That is luck, not a
reason to relax: the first version of this display froze a run with the db-state gates OFF
and showed +10.0% for a strategy nobody runs. `STREAM` is not a tuning knob and
`--stream edge` has to be asked for explicitly, in writing, against this paragraph.

**And the source run has to be one production would recognise.** `build` cannot check
that — a `:pure` run (db_gates off, fixed default params) is bounded, has a hybrid stream
and passes every guard below, which is exactly how +10.0% got displayed for four days
while the same window under the real gates was -6.8%. `source.gates_note` is exported so
the discrepancy is at least visible in the payload; read it before freezing.

**Guards, each of which corresponds to a way a hand-written row went wrong.**
  bounded      an unbounded run's window is "the last N days from whenever it ran", so a
               frozen row derived from one silently means a different window every time it
               is regenerated. Only an explicitly bounded run may be frozen.
  no overlap   the last settle in the history must land at or before the cutover date, or
               the same days are counted once in the backtest and again in the live ledger.
  non-empty    a stream with no trades would export as a 0-trade history and read on the
               page as "no track record" rather than "the derivation broke".
  live rules   the run must have had the db-state gates and the daily param selector ON.
               A `:pure` run measures an ungated strategy; freezing one puts a number on
               the page that no live day can reproduce. `allow_ungated=True` exists for
               deliberate research exports and is not reachable from the CLI.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

STREAM = "hybrid"          # must match what frontend_export's live segment counts
EXPERIMENT = "track_history"


def build(conn, source_hash: str, cutover: str, stream: str = STREAM,
          source_name: str = "daily_walkforward", allow_ungated: bool = False) -> dict:
    """The `track_history` metrics payload derived from one stored walk-forward run.

    Shape is deliberately identical to the hand-written row it replaces — the frontend
    reads `n_trades/won/win_rate/staked/realized/roi/span/trades` and must not need a
    change to accept this. Everything new goes under `source`, which is additive.
    """
    row = conn.execute(
        "SELECT metrics_json, created_ts FROM experiments WHERE name=? AND config_hash=?",
        (source_name, source_hash)).fetchone()
    if row is None:
        raise LookupError(f"no {source_name} run with config_hash={source_hash!r}")
    m = json.loads(row["metrics_json"])

    if not m.get("bounded"):
        raise ValueError(
            f"{source_hash!r} is not a bounded run — its window is relative to the day it "
            "ran, so freezing it would silently redefine the displayed segment on every "
            "rebuild. Re-run walkforward with an explicit --end.")
    # `argmax_filter` predates nothing — runs stored before the flag existed all had the
    # filter on, so its absence means True rather than unknown.
    if not allow_ungated and not (m.get("db_gates") and m.get("select_params")
                                  and m.get("argmax_filter", True)):
        raise ValueError(
            f"{source_hash!r} ran with db_gates={m.get('db_gates')!r} "
            f"select_params={m.get('select_params')!r} "
            f"argmax_filter={m.get('argmax_filter', True)!r} — that is not the strategy "
            "production runs, and freezing it displays a track record no live day can "
            "reproduce. Re-run walkforward without the --no-* flags.")
    st = (m.get("streams") or {}).get(stream)
    if st is None:
        raise LookupError(f"{source_hash!r} has no {stream!r} stream "
                          f"(has: {sorted((m.get('streams') or {}))})")
    trades = st.get("trades") or []
    if not trades:
        raise ValueError(f"{stream!r} stream is empty — refusing to export a 0-trade "
                         "history, which would read as 'no track record'")
    # `_stream_summary` caps its trade list (to bound metrics_json), but its n_trades /
    # staked / realized are computed from the FULL list. Every aggregate below is
    # recomputed from the list, so a capped source would export a self-consistent,
    # silently-partial history — the one failure mode here that raises no error anywhere.
    if st.get("n_trades") is not None and len(trades) != st["n_trades"]:
        raise ValueError(
            f"{stream!r} carries {len(trades)} trades but ran {st['n_trades']} — the "
            "source run capped its trade list, so freezing it would display a partial "
            "history. Raise the cap in walkforward._stream_summary and re-run.")

    settles = sorted(t["settle"] for t in trades if t.get("settle"))
    days = sorted(t["day"] for t in trades if t.get("day"))
    if settles and settles[-1] > cutover[:10]:
        raise ValueError(
            f"history ends {settles[-1]} but the display cutover is {cutover[:10]} — the "
            "overlap would be counted once in the backtest and again in the live ledger")

    staked = round(sum(t.get("staked") or 0 for t in trades), 4)
    realized = round(sum(t.get("realized") or 0 for t in trades), 4)
    won = sum(1 for t in trades if (t.get("realized") or 0) > 0)
    return {
        "n_trades": len(trades), "won": won,
        "win_rate": round(won / len(trades), 4),
        "staked": staked, "realized": realized,
        "roi": round(realized / staked, 5) if staked else None,
        "span": [days[0], days[-1]] if days else None,
        "trades": trades,
        "note": (f"{stream} stream (the live rule), PIT walk-forward "
                 f"{m.get('window_start')}..{m.get('window_end')}, frozen at the "
                 f"new-rules cutover {cutover}"),
        "source": {"experiment": source_name, "config_hash": source_hash,
                   "stream": stream, "fair_mode": m.get("fair_mode"),
                   "window": [m.get("window_start"), m.get("window_end")],
                   "run_created_ts": row["created_ts"],
                   "settle_span": [settles[0], settles[-1]] if settles else None,
                   "gates_note": m.get("note")},
    }


def freeze(conn, source_hash: str, cutover: str | None = None, stream: str = STREAM,
           source_name: str = "daily_walkforward", allow_ungated: bool = False) -> dict:
    """Build and write the row. Returns the payload minus the trade list.

    Written under `config_hash='frozen:<source_hash>'` so previous frozen segments stay in
    `experiments` rather than being overwritten — `frontend_export` reads the newest by
    `created_ts`, so what is displayed is always the last freeze and what it replaced
    remains auditable.
    """
    from prediction_market_macro.ops.frontend_export import TRACK_CUTOVER
    cut = cutover or TRACK_CUTOVER
    payload = build(conn, source_hash, cut, stream=stream, source_name=source_name,
                    allow_ungated=allow_ungated)
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES(?,?,?,?,?,?)",
        (EXPERIMENT, f"frozen:{source_hash}", None,
         json.dumps(payload["source"]["window"]),
         json.dumps(payload), datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return {k: v for k, v in payload.items() if k != "trades"}


def main():
    import argparse
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    help="config_hash of the daily_walkforward run to freeze")
    ap.add_argument("--stream", default=STREAM,
                    help="hybrid (default; matches the live segment). See the module "
                         "docstring before choosing anything else.")
    ap.add_argument("--cutover")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    conn = init_db(load_settings().db_path)
    if a.dry_run:
        from prediction_market_macro.ops.frontend_export import TRACK_CUTOVER
        p = build(conn, a.source, a.cutover or TRACK_CUTOVER, stream=a.stream)
        print(json.dumps({k: v for k, v in p.items() if k != "trades"}, indent=1))
        return
    print(json.dumps(freeze(conn, a.source, cutover=a.cutover, stream=a.stream), indent=1))


if __name__ == "__main__":
    main()
