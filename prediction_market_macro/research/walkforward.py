"""research/walkforward.py — daily-granularity walk-forward simulation (user ask
2026-07-31): run the CURRENT system as if it had been live for the past N days,
advancing one day at a time under strict PIT.

Each simulated day D at 16:00 UTC (~noon ET):
  * every (series, period) whose market was OPEN at D and has since SETTLED is a
    candidate (settled-only so every position can be scored to the end)
  * the book is rebuilt from stored daily candles as of D (depth unknown → waived)
  * the PRODUCTION model predicts at asof=D (its internal fits — AAA drift
    regression, first prints, GDPNow vintages — all read knowledge_time <= D)
  * the PRODUCTION decide() runs with pure gates (db-state gates — calibration
    map, capture memory, skill ratio, conformal — are deliberately EXCLUDED: they
    were fit on data that includes this window; using them here would be
    self-referential leakage)
  * one open per (series, period) for the whole run (first day the gates clear —
    exactly the live no-averaging rule); positions settle at their real results

Outputs: day-by-day equity curve, win rate, per-series PnL, entry-lead-time
buckets (how many days before settlement the entry happened — the diagnostic for
"are early entries the losers?"). Stored as experiments row 'daily_walkforward'.

Discipline: this is an EVALUATION harness. Improve → rerun is allowed for
STRUCTURAL fixes only; never tune thresholds until this window looks good
(overfit guarantee). Hold out the trailing third when judging any change.

    python -m prediction_market_macro.research.walkforward [--days 30]
"""
from __future__ import annotations

import argparse
import importlib
import json
from datetime import datetime, timedelta, timezone

import numpy as np

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.model.common import Categorical, grid_pmf
from prediction_market_macro.ops.decide_all import _structs_categorical
from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
from prediction_market_macro.strategy.decision import GATES, decide
from prediction_market_macro.strategy.edge import enumerate_structs, taker_fee
from prediction_market_macro.util.periods import kalshi_period_to_key


def _candle_quote(conn, ticker: str, asof: datetime):
    r = conn.execute(
        "SELECT yes_bid_close, yes_ask_close FROM candles WHERE ticker=? AND end_ts<=?"
        " ORDER BY end_ts DESC LIMIT 1", (ticker, int(asof.timestamp()))).fetchone()
    if r is None:
        return None, None
    return r["yes_bid_close"], r["yes_ask_close"]


def _open_settled_events(conn, asof: datetime, now: datetime) -> list[dict]:
    """(series, period tok, close_ts) open at asof and settled by now."""
    rows = conn.execute(
        "SELECT s.series, s.period, MAX(c.close_time) ct FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker"
        " WHERE s.result IN ('yes','no') GROUP BY s.series, s.period").fetchall()
    out = []
    for r in rows:
        if r["series"] not in SERIES_DISPATCH or not r["ct"]:
            continue
        close_ts = datetime.fromisoformat(r["ct"].replace("Z", "+00:00"))
        if asof < close_ts <= now:
            out.append({"series": r["series"], "tok": r["period"],
                        "close_ts": close_ts})
    return out


def run(conn, days: int = 30, offset_hour: int = 16, bankroll: float = 100.0) -> dict:
    now = datetime.now(timezone.utc)
    gates = dict(GATES)
    gates["min_leg_depth_usd"] = 0.0               # candles carry no depth
    opened: dict[tuple[str, str], dict] = {}       # (series, key) -> trade
    daily: list[dict] = []
    for d in range(days, 0, -1):
        day = (now - timedelta(days=d)).replace(hour=offset_hour, minute=0,
                                                second=0, microsecond=0)
        day_trades = 0
        for ev in _open_settled_events(conn, day, now):
            key = kalshi_period_to_key(ev["tok"])
            if not key or (ev["series"], key) in opened:
                continue
            spec = REGISTRY[ev["series"]]
            legs_rows = conn.execute(
                "SELECT c.ticker, c.floor_strike, c.cap_strike, c.strike_type,"
                " c.close_time, s.result FROM contracts c"
                " JOIN settlements s ON s.ticker=c.ticker"
                " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')",
                (ev["series"], ev["tok"])).fetchall()
            meta, results = [], {}
            for l in legs_rows:
                b, a = _candle_quote(conn, l["ticker"], day)
                if b is None and a is None:
                    continue
                meta.append({"ticker": l["ticker"], "strike": l["floor_strike"],
                             "cap_strike": l["cap_strike"],
                             "strike_type": l["strike_type"],
                             "close_time": l["close_time"], "yes_bid": b,
                             "yes_ask": a, "bid_depth": 1e9, "ask_depth": 1e9})
                results[l["ticker"]] = l["result"]
            if not meta:
                continue
            disp = SERIES_DISPATCH[ev["series"]]
            fn = getattr(importlib.import_module(disp[0]), disp[1])
            try:
                pred = fn(conn, day, key, series=ev["series"])
            except Exception:                      # noqa: BLE001
                continue
            import math as _math
            entropy_norm = None
            if isinstance(pred.dist, Categorical):
                structs = _structs_categorical(meta, pred.dist.probs)
                pv = [v for v in pred.dist.probs.values() if v > 0]
            else:
                pmf = grid_pmf(pred.dist, spec.round_rule)
                structs = enumerate_structs(meta, pmf, strict=spec.strict_gt)
                pv = [v for v in pmf.values() if v > 0]
            if len(pv) > 1:
                entropy_norm = -sum(p * _math.log(p) for p in pv) / _math.log(len(pv))
            dec = decide(structs, now=day, close_time=ev["close_ts"],
                         release_ts=None, market_implied=None, already_open=False,
                         bankroll=bankroll, gates=gates, entropy_norm=entropy_norm)
            if dec.action != "open" or dec.struct is None:
                continue
            st, count = dec.struct, dec.count
            realized = 0.0
            ok = True
            for leg in st.legs:
                res = results.get(leg.ticker)
                if res is None:
                    ok = False
                    break
                won = (res == leg.side)
                realized += ((1.0 if won else 0.0) - leg.price) * count \
                    - taker_fee(leg.price, count)
            if not ok:
                continue
            lead_days = (ev["close_ts"] - day).total_seconds() / 86400.0
            opened[(ev["series"], key)] = {
                "series": ev["series"], "period": key, "day": day.date().isoformat(),
                "desc": st.desc, "count": count,
                "staked": round(sum(l.price for l in st.legs) * count, 4),
                "realized": round(realized, 4), "won": realized > 0,
                "lead_days": round(lead_days, 1),
                "settle": ev["close_ts"].date().isoformat()}
            day_trades += 1
        daily.append({"day": day.date().isoformat(), "n_opened": day_trades})

    trades = sorted(opened.values(), key=lambda t: t["settle"])
    curve, run_pnl = [], 0.0
    for t in trades:
        run_pnl += t["realized"]
        curve.append({"day": t["settle"], "pnl": round(run_pnl, 4)})
    by_series: dict[str, dict] = {}
    for t in trades:
        b = by_series.setdefault(t["series"], {"n": 0, "won": 0, "staked": 0.0,
                                               "realized": 0.0})
        b["n"] += 1
        b["won"] += 1 if t["won"] else 0
        b["staked"] = round(b["staked"] + t["staked"], 4)
        b["realized"] = round(b["realized"] + t["realized"], 4)
    lead_buckets: dict[str, dict] = {}
    for t in trades:
        k = ("0-1d" if t["lead_days"] <= 1 else "1-3d" if t["lead_days"] <= 3
             else "3-7d" if t["lead_days"] <= 7 else ">7d")
        b = lead_buckets.setdefault(k, {"n": 0, "won": 0, "realized": 0.0})
        b["n"] += 1
        b["won"] += 1 if t["won"] else 0
        b["realized"] = round(b["realized"] + t["realized"], 4)
    tot_staked = sum(t["staked"] for t in trades)
    tot_real = sum(t["realized"] for t in trades)
    out = {"days": days, "n_trades": len(trades),
           "win_rate": round(sum(1 for t in trades if t["won"]) / len(trades), 4)
           if trades else None,
           "staked": round(tot_staked, 4), "realized": round(tot_real, 4),
           "roi": round(tot_real / tot_staked, 5) if tot_staked > 0 else None,
           "by_series": by_series, "lead_buckets": lead_buckets,
           "curve": curve[-60:], "trades": trades[-60:],
           "note": "pure gates; db-state gates (calibration/skill/capture/conformal)"
                   " excluded as self-referential over this window"}
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES('daily_walkforward',?,'*',?,?,?)",
        (f"d{days}:{now.date().isoformat()}", f"{days}d",
         json.dumps(out, ensure_ascii=False), now.isoformat()))
    conn.commit()
    return out


def main():
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    s = load_settings()
    conn = init_db(s.db_path)
    out = run(conn, days=args.days)
    print(json.dumps({k: v for k, v in out.items() if k not in ("trades", "curve")},
                     indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
