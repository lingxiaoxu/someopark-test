"""ops/venue_liquidity.py — measure WHEN a match market actually becomes tradable.

WHY
    On 2026-09-02 the demo book for that night's Brasileirão match was sampled once, about
    six hours before kickoff, found empty on all three legs, and that single reading was
    turned into a forecast: "no demo liquidity for Brasileirão, tonight's mirror will not
    fill". It filled — twice — because the market maker posted close to kickoff. The same
    afternoon's readings also showed Ligue 1 and Serie A quoting FORTY hours out. So
    "when does this competition's book fill up" varies by competition and by venue, and a
    one-off reading taken at an arbitrary time cannot answer it.

WHAT THIS DOES
    Samples each upcoming fixture's three legs at fixed KICKOFF-RELATIVE buckets — T-48h,
    T-24h, T-12h, T-6h, T-3h, T-90m, T-45m, T-20m, T-10m, T-3m — on both the demo book
    (where the mirror trades) and the production book (the real market, for contrast), and
    stores one row per (fixture, side, venue, bucket) in ``venue_book_probe``. A bucket is
    recorded once; re-running is a no-op. The live loop calls it BEFORE its match-window
    check (most cycles nothing is due, so it costs nothing), so the far-out buckets are
    captured on days with no matches at all.

READING THE RESULT
    python -m prediction_market_soccer.ops.venue_liquidity --summary
        per competition and bucket: how often an executable ask existed, and the median
        spread — i.e. the empirical answer to "from when can we actually trade this".
    python -m prediction_market_soccer.ops.venue_liquidity --probe [--include-prod]
        run the due buckets now.

Only the ask side makes a market tradable for an ENTRY (we buy), and only the bid for an
EXIT (we sell), so both are recorded and the summary reports them separately.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG

# (label, minutes before kickoff, half-width of the window that counts as "at" the bucket)
BUCKETS: list[tuple[str, float, float]] = [
    ("T-48h", 2880, 90), ("T-24h", 1440, 60), ("T-12h", 720, 45), ("T-6h", 360, 30),
    ("T-3h", 180, 20), ("T-90m", 90, 12), ("T-45m", 45, 7), ("T-20m", 20, 4),
    ("T-10m", 10, 2.5), ("T-3m", 3, 1.5),
]
MAX_FIXTURES_PER_RUN = 12      # a busy weekend must not turn one cycle into a sweep
_SIDES = ("home", "draw", "away")
PROD_PUBLIC = "https://api.elections.kalshi.com/trade-api/v2"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _due_buckets(conn, *, horizon_h: float = 50.0) -> list[dict]:
    """[{fixture, comp, kickoff, bucket, minutes}] for every (fixture, bucket) whose window
    contains 'now' and that has not been recorded yet."""
    from prediction_market_soccer.config.leagues import active
    comp_of = {c.api_football_id: c.key for c in active()}
    lids = tuple(comp_of)
    ph = ",".join("?" * len(lids))
    rows = conn.execute(
        f"SELECT api_id, home_api_id, away_api_id, kickoff_ts, league_id FROM fixture "
        f"WHERE league_id IN ({ph}) AND status_short='NS' AND kickoff_ts IS NOT NULL "
        f"AND kickoff_ts >= strftime('%Y-%m-%dT%H:%M:%S','now','-10 minutes') "
        f"AND kickoff_ts <= strftime('%Y-%m-%dT%H:%M:%S','now',?) "
        f"ORDER BY kickoff_ts", (*lids, f"+{horizon_h} hours")).fetchall()
    have = {(r["fixture_api_id"], r["bucket"]) for r in conn.execute(
        "SELECT DISTINCT fixture_api_id, bucket FROM venue_book_probe")}
    out = []
    now = _now()
    for r in rows:
        try:
            ko = datetime.fromisoformat(r["kickoff_ts"])
        except ValueError:
            continue
        mins = (ko - now).total_seconds() / 60.0
        for label, centre, half in BUCKETS:
            if abs(mins - centre) <= half and (r["api_id"], label) not in have:
                out.append({"fixture": r["api_id"], "comp": comp_of.get(r["league_id"]),
                            "home": r["home_api_id"], "away": r["away_api_id"],
                            "kickoff": r["kickoff_ts"], "bucket": label, "minutes": round(mins, 1)})
                break          # one bucket per fixture per run
    return out[:MAX_FIXTURES_PER_RUN]


def _prod_book(ticker: str):
    import requests

    from prediction_market_soccer.venues.kalshi.market_data import best_prices
    r = requests.get(f"{PROD_PUBLIC}/markets/{ticker}/orderbook", timeout=15)
    r.raise_for_status()
    return best_prices(r.json(), market_key=ticker)


def probe(conn=None, *, include_prod: bool = True, verbose: bool = False) -> dict:
    """Record every due bucket. Never raises into the caller."""
    from prediction_market_soccer.ingest import store
    conn = conn or store.init_db()
    due = _due_buckets(conn)
    if not due:
        return {"due": 0, "rows": 0}
    from prediction_market_soccer.exec.kalshi_mirror import DemoBroker, _Tickers, _cmap
    cmap = _cmap(conn)
    tickers = _Tickers()
    try:
        broker = DemoBroker()
    except Exception as e:  # noqa: BLE001 — mirror disabled / wrong env: prod-only still useful
        broker = None
        if verbose:
            print(f"[venue_liquidity] demo broker unavailable: {str(e)[:120]}")
    n = 0
    skipped_unreachable: list[str] = []
    ts = _now().isoformat(timespec="seconds")
    for d in due:
        hi, ai = cmap.get(d["home"]), cmap.get(d["away"])
        if not (hi and ai and d["comp"]):
            continue
        tk = tickers.for_match(d["comp"], hi, ai)
        if not tk:
            if not tickers.index_ok(d["comp"]):
                # the LISTING call failed (rate limit / outage). Record nothing: leaving the
                # bucket unrecorded lets the next cycle retry, whereas a "no market" row here
                # would be a transient 429 frozen into the measurement as a fact about the
                # competition — the exact error this table exists to prevent.
                skipped_unreachable.append(d["comp"])
                continue
            # the listing WAS retrieved and does not carry this pairing — that is the answer
            conn.execute(
                "INSERT OR IGNORE INTO venue_book_probe (fixture_api_id, comp, venue, side, bucket, "
                "minutes_to_kickoff, ticker, ts) VALUES (?,?,?,?,?,?,?,?)",
                (d["fixture"], d["comp"], "demo", "home", d["bucket"], d["minutes"], None, ts))
            n += 1
            continue
        for venue, getter in (("demo", (broker.book if broker else None)), ("prod", _prod_book if include_prod else None)):
            if getter is None:
                continue
            for side in _SIDES:
                try:
                    ob = getter(tk[side])
                except Exception:  # noqa: BLE001 — a venue hiccup is data too, recorded as NULL
                    ob = None
                conn.execute(
                    "INSERT OR IGNORE INTO venue_book_probe (fixture_api_id, comp, venue, side, bucket, "
                    "minutes_to_kickoff, ticker, bid, ask, bid_depth, ask_depth, ts) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d["fixture"], d["comp"], venue, side, d["bucket"], d["minutes"], tk[side],
                     (float(ob.yes_bid) if (ob and ob.yes_bid is not None) else None),
                     (float(ob.yes_ask) if (ob and ob.yes_ask is not None) else None),
                     (float(ob.yes_depth) if (ob and ob.yes_depth is not None) else None),
                     (float(ob.no_depth) if (ob and ob.no_depth is not None) else None), ts))
                n += 1
    conn.commit()
    if verbose and n:
        print(f"[venue_liquidity] {len(due)} fixture-bucket(s) → {n} rows "
              + ", ".join(f"{d['comp']}@{d['bucket']}" for d in due))
    out = {"due": len(due), "rows": n,
           "buckets": [f"{d['comp']}@{d['bucket']}" for d in due]}
    if skipped_unreachable:
        out["unreachable"] = sorted(set(skipped_unreachable))     # retried next cycle
    return out


def summary(conn=None) -> dict:
    """Per competition × bucket × venue: share of legs with an executable ask (entry) and a
    bid (exit), and the median spread. This is the table that answers 'from when can we
    trade competition X', with n so a thin sample is visible as thin."""
    import statistics
    from prediction_market_soccer.ingest import store
    conn = conn or store.init_db()
    rows = conn.execute(
        "SELECT comp, venue, bucket, side, bid, ask FROM venue_book_probe WHERE ticker IS NOT NULL").fetchall()
    order = {b[0]: i for i, b in enumerate(BUCKETS)}
    agg: dict = {}
    for r in rows:
        k = (r["comp"], r["venue"], r["bucket"])
        a = agg.setdefault(k, {"n": 0, "ask": 0, "bid": 0, "spread": []})
        a["n"] += 1
        if r["ask"] is not None:
            a["ask"] += 1
        if r["bid"] is not None:
            a["bid"] += 1
        if r["ask"] is not None and r["bid"] is not None:
            a["spread"].append(round((r["ask"] - r["bid"]) * 100, 1))
    out = []
    for (comp, venue, bucket), a in agg.items():
        out.append({"comp": comp, "venue": venue, "bucket": bucket, "n": a["n"],
                    "ask_pct": round(100 * a["ask"] / a["n"]), "bid_pct": round(100 * a["bid"] / a["n"]),
                    "median_spread_c": (round(statistics.median(a["spread"]), 1) if a["spread"] else None)})
    out.sort(key=lambda x: (x["comp"] or "", x["venue"], order.get(x["bucket"], 99)))
    doc = {"ts": _now().isoformat(timespec="seconds"), "n_rows": len(rows), "table": out}
    CONFIG.paths.ensure()
    (CONFIG.paths.output / "venue_liquidity.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="When does a match market become tradable?")
    ap.add_argument("--probe", action="store_true", help="record the buckets due now")
    ap.add_argument("--summary", action="store_true", help="print the measured picture")
    ap.add_argument("--include-prod", action="store_true", default=True)
    ap.add_argument("--demo-only", action="store_true")
    a = ap.parse_args()
    from prediction_market_soccer.ingest import store
    conn = store.init_db()
    if a.probe or not a.summary:
        print("[venue_liquidity] probe:", probe(conn, include_prod=not a.demo_only, verbose=True))
    if a.summary or not a.probe:
        doc = summary(conn)
        print(f"  {'comp':13s} {'venue':5s} {'bucket':7s} {'n':>4s} {'有卖价':>7s} {'有买价':>7s} {'中位价差':>9s}")
        for r in doc["table"]:
            print(f"  {str(r['comp']):13s} {r['venue']:5s} {r['bucket']:7s} {r['n']:4d} "
                  f"{r['ask_pct']:6d}% {r['bid_pct']:6d}% "
                  + (f"{r['median_spread_c']:8.1f}¢" if r["median_spread_c"] is not None else "        —"))
        print(f"[venue_liquidity] {doc['n_rows']} probe rows")


if __name__ == "__main__":
    main()
