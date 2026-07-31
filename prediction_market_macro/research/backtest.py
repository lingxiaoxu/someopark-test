"""research/backtest.py — PIT replay on real settled history (PLAN §9.4).

Claims replay (the reference implementation; other series follow the same shape):
for every historical release visible in the ALFRED vintage store, rebuild the world at
asof = release − 24h and − 1h via the SAME predict() the production path uses (PIT by
construction), score against the FIRST print (y_first == settlement truth), and score the
MARKET at the same asof from stored daily candles (bid/ask close of the pre-release bar).

    python -m prediction_market_macro.research.backtest [--series KXJOBLESSCLAIMS] [--n 26]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone

import numpy as np

from prediction_market_macro.model import claims as claims_model
from prediction_market_macro.model.common import crps_grid, grid_pmf, survival


def backfill_candles(conn, md, series: str, max_markets: int = 4000) -> int:
    """Daily candles for every settled contract of the series (idempotent, skip if
    present). A failing market (429 storm / pruned) is skipped, never fatal; a run of
    consecutive failures triggers a long cooldown sleep rather than an abort, so a
    transient rate-limit storm doesn't permanently strand the rest of the series —
    only a sustained >90% failure ratio (API genuinely down) gives up early. The next
    run resumes where it left off because present tickers are skipped."""
    import time as _t
    rows = conn.execute(
        "SELECT s.ticker, s.settled_ts, COALESCE(c.close_time, s.settled_ts) end_ts"
        " FROM settlements s LEFT JOIN contracts c ON c.ticker=s.ticker"
        " WHERE s.series=?", (series,)).fetchall()
    n, consec_fail, total_fail, total_try = 0, 0, 0, 0
    for r in rows[:max_markets]:
        have = conn.execute("SELECT COUNT(*) c FROM candles WHERE ticker=?",
                            (r["ticker"],)).fetchone()["c"]
        if have > 0 or not r["end_ts"]:
            continue
        end = datetime.fromisoformat(r["end_ts"].replace("Z", "+00:00"))
        total_try += 1
        try:
            n += md.candles(series, r["ticker"],
                            int((end - timedelta(days=12)).timestamp()),
                            int(end.timestamp()))
            consec_fail = 0
        except Exception:                                        # noqa: BLE001
            consec_fail += 1
            total_fail += 1
            if total_try >= 20 and total_fail / total_try > 0.9:
                break                                              # API genuinely down
            if consec_fail >= 8:
                _t.sleep(45.0)
                consec_fail = 0
    return n


def _market_leg_prob(conn, ticker: str, asof: datetime) -> float | None:
    r = conn.execute(
        "SELECT yes_bid_close, yes_ask_close FROM candles WHERE ticker=? AND end_ts<=?"
        " ORDER BY end_ts DESC LIMIT 1", (ticker, int(asof.timestamp()))).fetchone()
    if r is None:
        return None
    b, a = r["yes_bid_close"], r["yes_ask_close"]
    if b is not None and a is not None and a < 1.0:
        return (b + a) / 2
    return None


def replay_claims(conn, n_releases: int = 26, asof_offsets=("-24h", "-1h")) -> dict:
    """Walk the last n historical claims releases; score model vs market vs y_first."""
    firsts = conn.execute(
        "SELECT event_time, value, MIN(knowledge_time) kt FROM fred_obs WHERE sid='ICSA'"
        " GROUP BY event_time ORDER BY event_time DESC LIMIT ?", (n_releases + 2,)).fetchall()
    rows = list(reversed(firsts))[-n_releases:]
    per, skipped = [], 0
    for r in rows:
        release_ts = datetime.fromisoformat(r["kt"])
        y = float(r["value"])
        period = release_ts.date().isoformat()
        rec = {"period": period, "y_first": y}
        for off in asof_offsets:
            hours = 24 if off == "-24h" else 1
            asof = release_ts - timedelta(hours=hours)
            try:
                pred = claims_model.predict(conn, asof, period)
            except Exception:                                    # noqa: BLE001
                skipped += 1
                rec = None
                break
            pmf = grid_pmf(pred.dist, 250.0)
            rec[f"crps{off}"] = crps_grid(pmf, y)
            rec[f"mu{off}"] = pred.dist.comps[0][1]
            # per-strike Brier vs the market on the SAME asof (settled contracts of that release)
            legs = conn.execute(
                "SELECT c.ticker, c.floor_strike, c.strike_type, s.result FROM contracts c"
                " JOIN settlements s ON s.ticker=c.ticker WHERE c.series='KXJOBLESSCLAIMS'"
                " AND c.event_ticker LIKE ?",
                (f"%{release_ts.strftime('%y%b%d').upper()}",)).fetchall()
            bs_m, bs_k, n_legs = 0.0, 0.0, 0
            for l in legs:
                if l["floor_strike"] is None or l["result"] not in ("yes", "no"):
                    continue
                mp = _market_leg_prob(conn, l["ticker"], asof)
                if mp is None:
                    continue
                strict = (l["strike_type"] == "greater")
                fair = survival(pmf, float(l["floor_strike"]), strict=strict)
                out = 1.0 if l["result"] == "yes" else 0.0
                bs_m += (fair - out) ** 2
                bs_k += (mp - out) ** 2
                n_legs += 1
            if n_legs:
                rec[f"brier_model{off}"] = bs_m / n_legs
                rec[f"brier_market{off}"] = bs_k / n_legs
                rec[f"n_legs{off}"] = n_legs
        if rec:
            per.append(rec)
    agg = {"n": len(per), "skipped": skipped}
    for off in asof_offsets:
        bm = [p[f"brier_model{off}"] for p in per if f"brier_model{off}" in p]
        bk = [p[f"brier_market{off}"] for p in per if f"brier_market{off}" in p]
        cr = [p[f"crps{off}"] for p in per if f"crps{off}" in p]
        agg[f"brier_model{off}"] = round(float(np.mean(bm)), 5) if bm else None
        agg[f"brier_market{off}"] = round(float(np.mean(bk)), 5) if bk else None
        agg[f"crps{off}"] = round(float(np.mean(cr)), 1) if cr else None
        agg[f"n_scored{off}"] = len(bm)
    return {"series": "KXJOBLESSCLAIMS", "agg": agg, "per_release": per}


def replay_series(conn, series: str, asof_offsets=("-24h", "-1h"),
                  max_events: int = 200) -> dict:
    """Generic settled-history replay for ANY ladder/categorical series.

    Brier needs no y: settled leg results ARE the outcomes. For each settled event,
    asof = (event close − offset); the PRODUCTION model predicts at that asof (PIT via
    its own vintage reads); per-leg fair via leg_fair with the leg's OWN strike
    metadata; the market is scored from stored candles at the same asof."""
    import importlib
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.model.common import Categorical, leg_fair
    from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
    from prediction_market_macro.util.periods import kalshi_period_to_key
    spec = REGISTRY[series]
    disp = SERIES_DISPATCH[series]
    fn = getattr(importlib.import_module(disp[0]), disp[1])
    events = conn.execute(
        "SELECT s.period, MAX(c.close_time) ct, COUNT(*) n FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker WHERE s.series=?"
        " AND s.result IN ('yes','no') GROUP BY s.period ORDER BY ct DESC LIMIT ?",
        (series, max_events)).fetchall()
    per, skipped = [], 0
    for ev in events:
        key = kalshi_period_to_key(ev["period"])
        if not key or not ev["ct"]:
            continue
        close_ts = datetime.fromisoformat(ev["ct"].replace("Z", "+00:00"))
        legs = conn.execute(
            "SELECT c.ticker, c.floor_strike, c.cap_strike, c.strike_type, s.result"
            " FROM contracts c JOIN settlements s ON s.ticker=c.ticker"
            " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')",
            (series, ev["period"])).fetchall()
        rec = {"period": key, "n_legs_settled": len(legs)}
        for off in asof_offsets:
            hours = 24 if off == "-24h" else 1
            asof = close_ts - timedelta(hours=hours)
            try:
                pred = fn(conn, asof, key, series=series)
            except Exception:                                    # noqa: BLE001
                skipped += 1
                rec = None
                break
            if isinstance(pred.dist, Categorical):
                probs = pred.dist.probs
                pmf = None
            else:
                pmf = grid_pmf(pred.dist, spec.round_rule)
            bs_m, bs_k, n_legs = 0.0, 0.0, 0
            for l in legs:
                mp = _market_leg_prob(conn, l["ticker"], asof)
                if mp is None:
                    continue
                try:
                    if pmf is None:                              # categorical leg
                        fair = float(probs.get(l["ticker"].rsplit("-", 1)[-1], 0.0))
                    else:
                        st = l["strike_type"] or ("greater" if spec.strict_gt
                                                  else "greater_or_equal")
                        fair = leg_fair(pmf, st, l["floor_strike"], l["cap_strike"])
                except Exception:                                # noqa: BLE001
                    continue
                out = 1.0 if l["result"] == "yes" else 0.0
                bs_m += (fair - out) ** 2
                bs_k += (mp - out) ** 2
                n_legs += 1
            if n_legs:
                rec[f"brier_model{off}"] = bs_m / n_legs
                rec[f"brier_market{off}"] = bs_k / n_legs
                rec[f"n_legs{off}"] = n_legs
        if rec:
            per.append(rec)
    agg = {"n": len(per), "skipped": skipped}
    for off in asof_offsets:
        bm = [p[f"brier_model{off}"] for p in per if f"brier_model{off}" in p]
        bk = [p[f"brier_market{off}"] for p in per if f"brier_market{off}" in p]
        agg[f"brier_model{off}"] = round(float(np.mean(bm)), 5) if bm else None
        agg[f"brier_market{off}"] = round(float(np.mean(bk)), 5) if bk else None
        agg[f"n_scored{off}"] = len(bm)
    return {"series": series, "agg": agg, "per_release": per}


def _store_experiment(conn, name: str, series: str, window: str, agg: dict,
                      cfg: dict) -> None:
    cfg_hash = hashlib.sha1(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES(?,?,?,?,?,?)",
        (name, cfg_hash, series, window, json.dumps(agg),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()


def replay_all(conn, md=None, deep: bool = False, max_candle_markets: int = 4000) -> dict:
    """Backfill (optionally deep/historical) + replay every dispatchable series with
    settled history. Returns {series: agg}. Wired into refresh --weekly."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.model import registry as _mr  # noqa: F401 (cards exist)
    from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
    out = {}
    for series in REGISTRY:
        if series not in SERIES_DISPATCH:
            continue
        if md is not None:
            if deep:
                ns = md.sync_settlements(series, deep=True)
                print(f"[bt] {series}: settlements synced {ns}", flush=True)
            nc = backfill_candles(conn, md, series, max_markets=max_candle_markets)
            print(f"[bt] {series}: candles +{nc}", flush=True)
        if series == "KXJOBLESSCLAIMS":
            rep = replay_claims(conn, n_releases=104)      # full vintage history window
            name = "claims_replay"
        else:
            rep = replay_series(conn, series)
            name = f"{series[2:].lower()}_replay"
        if rep["agg"]["n"] == 0:
            continue
        _store_experiment(conn, name, series, f"n{rep['agg']['n']}", rep["agg"],
                          {"series": series, "deep": deep})
        out[series] = rep["agg"]
    return out


def main():
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.kalshi_md import KalshiMD
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=26)
    ap.add_argument("--deep", action="store_true",
                    help="walk /historical/markets back to series launch first")
    ap.add_argument("--all", action="store_true", help="replay every series")
    args = ap.parse_args()
    s = load_settings()
    conn = init_db(s.db_path)
    # the candlestick endpoint rate-limits much harder than /markets — slow the batch
    md = KalshiMD(conn, spacing=0.6 if args.all else 0.18)
    if args.all:
        res = replay_all(conn, md, deep=args.deep)
        (s.output_dir / "bt_all.json").write_text(json.dumps(res, indent=1))
        print("[bt] all:", json.dumps(res, indent=1))
        return
    print("[bt] candles backfill:", backfill_candles(conn, md, "KXJOBLESSCLAIMS"))
    rep = replay_claims(conn, n_releases=args.n)
    _store_experiment(conn, "claims_replay", "KXJOBLESSCLAIMS", f"last{args.n}",
                      rep["agg"], {"n": args.n, "v": claims_model.VERSION})
    out = s.output_dir / "bt_claims.json"
    out.write_text(json.dumps(rep, indent=1))
    print("[bt] agg:", json.dumps(rep["agg"], indent=1))
    print(f"[bt] wrote {out}")


if __name__ == "__main__":
    main()
