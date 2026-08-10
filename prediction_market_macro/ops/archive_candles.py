"""ops/archive_candles.py — #120. Pull daily candles into the local store before Kalshi
drops them, because after that they are gone for good.

**The deadline is real and it was measured, not assumed.** Probing settled contracts by
age against `/series/{s}/markets/{t}/candlesticks` on 2026-08-06:

    age 62d KXNATGASW       -> 7 bars      age 76d KXNATGASW       -> HTTP 404
    age 63d KXJOBLESSCLAIMS -> 7 bars      age 77d KXJOBLESSCLAIMS -> HTTP 404
    age 69d KXNATGASW       -> 7 bars      age 80d KXAAAGASW       -> HTTP 404
    age 70d KXPCECORE       -> 8 bars      age 83d KXNATGASW       -> HTTP 404
    age 73d KXAAAGASW       -> 0 bars      age 84d KXJOBLESSCLAIMS -> HTTP 404

Four different series agree on the boundary, so it is a platform retention rule and not a
per-series quirk: **the last age that still answers is 73-75 days.** (The 0-bar row at 73d
is a market that barely traded, not an expiry — expiry is the 404.) The local `candles`
table is permanent, so anything fetched inside the window is kept forever; anything not
fetched by day ~75 is unrecoverable from any source we have.

**Why this needs its own scheduled step rather than the status quo.** The fetch already
exists — `research.backtest.backfill_candles` — but nothing calls it on a schedule. It
runs only as a side effect of somebody running a backtest by hand. That side-effect path
demonstrably leaks: when this module was written there were 14 settled markets inside the
window with zero candle rows, 2 of them within a week of expiring. A history that only
accrues when a human remembers to run a research script is not an archive.

**What it unblocks (#138).** The PnL backtest universe is bounded by candle coverage, and
`dsr.MIN_OBS = 12` events is the floor for the parameter selector to return anything but
registered defaults. A weekly series can hold at most ~75/7 = 10.7 events inside the API
window, so on the API alone it can *never* reach 12 — the selector is unreachable by
construction. Against an accruing local store the count is monotone and the weekly series
(currently 10-11 covered periods) cross the threshold within a fortnight.

    conda run -n someopark_run python -m prediction_market_macro.ops.archive_candles [--dry-run]
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

# Last age proven to still answer (73d observed; 76d is the first 404). Deliberately the
# conservative end of the measured bracket: over-estimating retention would mean skipping
# a market we could still have saved, and that error is irreversible.
RETENTION_DAYS = 74

# An unfetched market this old means the lane has been failing for weeks and there are
# only ~19 days of slack left. Alerting here rather than at RETENTION_DAYS is the whole
# point: an alert that fires on the day the data dies is a post-mortem, not a warning.
WARN_AGE_DAYS = 55

# Per-run ceiling, so the daily lane cannot turn into an hour-long API walk after an
# outage. Never silent — `run` reports what it deferred, and `pending` hands back the
# most-urgent-first ordering that makes deferral safe (see `run`).
DEFAULT_MAX_FETCH = 400

# A market this far past close whose candlestick list comes back EMPTY never traded, and
# never will — see `_confirm_empty`. Three days rather than zero because the daily lane
# gets three attempts in that time, and there is no cost to waiting when the deadline is
# 74 days out; marking eagerly could strand a market whose final bar had not yet posted.
EMPTY_CONFIRM_DAYS = 3


def pending(conn, max_age_days: int = RETENTION_DAYS) -> list[dict]:
    """Settled markets still inside the window that have NO candle row of any kind.

    Ordered **oldest close first** — i.e. by remaining lifetime, ascending. That ordering
    is load-bearing rather than cosmetic: it is what makes `MAX_FETCH` safe. Truncating a
    newest-first list would defer exactly the markets that are about to expire, so the cap
    would quietly destroy data every time it bound. Truncating an oldest-first list defers
    the ones with the most time left, and tomorrow's run picks them up.

    "No candle row of any kind" also excludes the ~6.7k tickers carrying `backfill`'s
    404 sentinel (`end_ts = 0`). Those are permanently unavailable and re-probing them
    every day would be the bulk of the work for a guaranteed zero yield. Verified when
    this was written that **0** sentinel-only tickers were still inside the window, so the
    sentinel is not masking anything recoverable.
    """
    floor = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    rows = conn.execute(
        "SELECT s.series, s.period, s.ticker, ct.close_time"
        " FROM settlements s JOIN contracts ct ON ct.ticker = s.ticker"
        " WHERE s.result IN ('yes','no') AND ct.close_time IS NOT NULL"
        "   AND ct.close_time >= ?"
        "   AND NOT EXISTS (SELECT 1 FROM candles cd WHERE cd.ticker = s.ticker)"
        " ORDER BY ct.close_time ASC", (floor,)).fetchall()
    return [dict(r) for r in rows]


def _age_days(close_time: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(close_time.replace("Z", "+00:00"))).total_seconds() / 86400.0


def _confirm_empty(conn, ticker: str) -> None:
    """Record "asked, and there is genuinely nothing" using the SAME sentinel row shape
    `kalshi_md.candles` already writes on a 404: `end_ts = 0` with NULL prices.

    Without this there is no third state. A market that never traded answers 200 with an
    empty list, so nothing is written, so it stays queued and is re-fetched every single
    day until it expires — and, far worse, it keeps the overdue alert firing forever. An
    alert that is always on is not an alert. Measured when this landed: all 14 markets in
    the production queue were deep-OTM KXAAAGASW strikes (4.515, 4.505, 4.165, ...) that
    answered empty at ages 17d through 73d — permanently untraded, not merely early.

    Reusing the 404 sentinel rather than inventing a state is deliberate: it means the
    same thing ("no candle data exists for this ticker"), ~6.7k of them are already in
    production, and every reader is therefore already exposed to it. `_market_leg_prob`
    returns None on NULL bid/ask, which is the correct "no market" answer.
    """
    conn.execute(
        "INSERT OR IGNORE INTO candles(ticker, end_ts, yes_bid_close, yes_ask_close,"
        " price_close, volume) VALUES(?,0,NULL,NULL,NULL,NULL)", (ticker,))
    conn.commit()


def run(conn, md, max_fetch: int = DEFAULT_MAX_FETCH, dry_run: bool = False) -> dict:
    """Fetch every pending market inside the window, most-urgent first.

    A per-market failure is counted and skipped, never fatal — one 429 must not strand the
    rest of the queue, and the row stays pending so tomorrow retries it. There is no
    cooldown/abort heuristic here (`backfill_candles` has one for its thousands-of-markets
    walk); this queue is normally single digits, so the simple thing is the correct thing.
    """
    now = datetime.now(timezone.utc)
    queue = pending(conn)
    oldest = _age_days(queue[0]["close_time"], now) if queue else 0.0

    # Escalate BEFORE fetching: if the run then dies partway, the warning is already
    # recorded. `WARN_AGE_DAYS` measures the lane's health, not this run's outcome.
    overdue = [q for q in queue if _age_days(q["close_time"], now) >= WARN_AGE_DAYS]
    if overdue:
        conn.execute(
            "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
            (now.isoformat(), "warn", "archive_candles",
             f"{len(overdue)} settled markets unfetched at >={WARN_AGE_DAYS}d "
             f"(oldest {oldest:.1f}d, hard loss at ~{RETENTION_DAYS}d): "
             + ", ".join(sorted({q['series'] for q in overdue}))[:160]))
        conn.commit()

    out = {"pending": len(queue), "oldest_age_days": round(oldest, 1),
           "overdue": len(overdue), "fetched": 0, "bars": 0, "empty": 0, "failed": 0,
           "deferred": max(0, len(queue) - max_fetch), "dry_run": dry_run}
    if dry_run:
        return out

    for q in queue[:max_fetch]:
        end = datetime.fromisoformat(q["close_time"].replace("Z", "+00:00"))
        try:
            # Same 12-day lookback the backtest's own backfill uses, so an archived
            # market is indistinguishable from a hand-backfilled one. The backtest reads
            # the -24h and -1h bars, both well inside 12 days for every series cadence.
            n = md.candles(q["series"], q["ticker"],
                           int((end - timedelta(days=12)).timestamp()),
                           int(end.timestamp()))
            out["bars"] += n
            out["fetched"] += 1
            if n == 0 and _age_days(q["close_time"], now) >= EMPTY_CONFIRM_DAYS:
                _confirm_empty(conn, q["ticker"])
                out["empty"] += 1
        except Exception:                                            # noqa: BLE001
            out["failed"] += 1
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report the queue and the alert without calling Kalshi")
    ap.add_argument("--max-fetch", type=int, default=DEFAULT_MAX_FETCH)
    a = ap.parse_args()
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.kalshi_md import KalshiMD
    from prediction_market_macro.ingest.store import init_db
    conn = init_db(load_settings().db_path)
    out = run(conn, KalshiMD(conn), max_fetch=a.max_fetch, dry_run=a.dry_run)
    print("[archive_candles]", out)
    if out["deferred"]:
        print(f"  NOTE: {out['deferred']} markets deferred by --max-fetch"
              f" (oldest-first, so nothing near expiry was dropped)")


if __name__ == "__main__":
    main()
