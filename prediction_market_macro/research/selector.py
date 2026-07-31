"""research/selector.py — ML bet selector (user mandate 2026-07-31).

Learns, from EVERY candidate structure of EVERY settled candled event in history,
the probability that a bet WINS given decision-time features — then bets when the
learned p(win) makes the expected value positive. The hand-made defer-to-market
rule (argmax + fair<=cost) is the BASELINE: this model earns production only by
beating it on the same PIT windows, and every future selector must beat whatever
currently holds the belt.

Anti-leakage discipline (the whole point):
  * one row per (event, structure) at the event's PIT entry asof (first day inside
    the 7d window with candles, 16:00 UTC); features use only kt<=asof reads
  * walk-forward: predictions for event E use a model trained ONLY on events whose
    settlement CLOSED before E's entry day — expanding window, never shuffled
  * logistic regression (small-sample-honest; depth-2 trees optional later),
    class-balanced, standardized features

Features per candidate structure:
  fair, cost, edge=fair-cost, |edge|, is_argmax (max-fair of its event),
  market_backed (fair<=cost), spread_med, entropy_norm, lead_days,
  kind_bucket (single yes / single no / bucket), family one-hots.
"""
from __future__ import annotations

import importlib
import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.model.common import Categorical, grid_pmf
from prediction_market_macro.ops.decide_all import _structs_categorical
from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
from prediction_market_macro.strategy.edge import enumerate_structs, taker_fee
from prediction_market_macro.util.periods import kalshi_period_to_key

FAMS = ["inflation", "labor", "energy", "fed", "gdp"]
EV_MIN = 0.03            # learned p(win) − cost − fee must clear this
MIN_TRAIN = 150          # rows before the model may bet
STAKE = 1.0


def settle_cash(payoff: float, leg_prices: list[float], count: int) -> float:
    """True cash result: payoff − OUTLAY (sum of leg prices, NOT the eff cost —
    a bucket costs eff but outlays 1+eff and pays 1 even on a miss) − fees."""
    outlay = sum(leg_prices)
    fee = sum(taker_fee(pr, count) for pr in leg_prices)
    return (payoff - outlay) * count - fee


def _candle_quote(conn, ticker: str, asof: datetime):
    r = conn.execute(
        "SELECT yes_bid_close, yes_ask_close FROM candles WHERE ticker=? AND end_ts<=?"
        " ORDER BY end_ts DESC LIMIT 1", (ticker, int(asof.timestamp()))).fetchone()
    return (None, None) if r is None else (r["yes_bid_close"], r["yes_ask_close"])


def _event_rows(conn, series: str, tok: str, close_ts: datetime,
                max_lead: float = 7.0) -> list[dict] | None:
    """All candidate structures of one settled event at its PIT entry asof."""
    spec = REGISTRY[series]
    key = kalshi_period_to_key(tok)
    if not key:
        return None
    entry = None
    step = (close_ts - timedelta(days=max_lead)).replace(hour=16, minute=0,
                                                         second=0, microsecond=0)
    legs_rows = conn.execute(
        "SELECT c.ticker, c.floor_strike, c.cap_strike, c.strike_type, s.result"
        " FROM contracts c JOIN settlements s ON s.ticker=c.ticker"
        " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')",
        (series, tok)).fetchall()
    while step < close_ts - timedelta(hours=1):
        meta = []
        for l in legs_rows:
            b, a = _candle_quote(conn, l["ticker"], step)
            if b is None and a is None:
                continue
            meta.append({"ticker": l["ticker"], "strike": l["floor_strike"],
                         "cap_strike": l["cap_strike"],
                         "strike_type": l["strike_type"], "close_time": None,
                         "yes_bid": b, "yes_ask": a,
                         "bid_depth": 1e9, "ask_depth": 1e9})
        if meta:
            entry = (step, meta,
                     {l["ticker"]: l["result"] for l in legs_rows})
            break
        step += timedelta(days=1)
    if entry is None:
        return None
    asof, meta, results = entry
    disp = SERIES_DISPATCH[series]
    fn = getattr(importlib.import_module(disp[0]), disp[1])
    try:
        pred = fn(conn, asof, key, series=series)
    except Exception:                                  # noqa: BLE001
        return None
    if isinstance(pred.dist, Categorical):
        structs = _structs_categorical(meta, pred.dist.probs)
        pv = [v for v in pred.dist.probs.values() if v > 0]
    else:
        pmf = grid_pmf(pred.dist, spec.round_rule)
        structs = enumerate_structs(meta, pmf, strict=spec.strict_gt)
        pv = [v for v in pmf.values() if v > 0]
    if not structs:
        return None
    entropy = (-sum(p * math.log(p) for p in pv) / math.log(len(pv))
               if len(pv) > 1 else 0.5)
    spreads = [m["yes_ask"] - m["yes_bid"] for m in meta
               if m["yes_ask"] is not None and m["yes_bid"] is not None]
    spread_med = sorted(spreads)[len(spreads) // 2] if spreads else 0.05
    lead = (close_ts - asof).total_seconds() / 86400.0
    max_fair = max(st.fair for st in structs)
    rows = []
    for st in structs:
        if not (0.02 <= st.cost <= 0.98):
            continue
        payoff = 0.0
        ok = True
        for leg in st.legs:
            res = results.get(leg.ticker)
            if res is None:
                ok = False
                break
            payoff += (1.0 if res == leg.side else 0.0)
        if not ok:
            continue
        # bucket structs cost `eff` but OUTLAY 1+eff and pay 1 even on a miss —
        # profit must be measured against the true cash outlay + fees, exactly
        # like walkforward._settle_struct (the first run's 39W-1L was this bug)
        leg_prices = [float(l.price) for l in st.legs]
        outlay = sum(leg_prices)
        won = settle_cash(payoff, leg_prices, 1) > 0
        fam = [1.0 if spec.family == f else 0.0 for f in FAMS]
        kind_yes = 1.0 if (st.kind == "single" and st.legs[0].side == "yes") else 0.0
        kind_no = 1.0 if (st.kind == "single" and st.legs[0].side == "no") else 0.0
        rows.append({
            "series": series, "period": key, "close": close_ts,
            "entry_day": asof.date().isoformat(), "desc": st.desc,
            "cost": st.cost, "payoff": payoff, "lead_days": round(lead, 1),
            "leg_prices": leg_prices, "outlay": outlay,
            "x": [st.fair, st.cost, st.fair - st.cost, abs(st.fair - st.cost),
                  1.0 if st.fair >= max_fair - 1e-9 else 0.0,
                  1.0 if st.fair <= st.cost else 0.0,
                  spread_med, entropy, lead / 7.0, kind_yes, kind_no] + fam,
            "y": 1.0 if won else 0.0})
    return rows or None


def build_dataset(conn, max_events: int = 400) -> list[dict]:
    events = conn.execute(
        "SELECT s.series, s.period, MAX(c.close_time) ct FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker WHERE s.result IN ('yes','no')"
        " GROUP BY s.series, s.period ORDER BY ct DESC LIMIT ?",
        (max_events,)).fetchall()
    data = []
    for ev in events:
        if ev["series"] not in SERIES_DISPATCH or not ev["ct"]:
            continue
        close_ts = datetime.fromisoformat(ev["ct"].replace("Z", "+00:00"))
        rows = _event_rows(conn, ev["series"], ev["period"], close_ts)
        if rows:
            data.extend(rows)
    data.sort(key=lambda r: r["close"])
    return data


def walkforward_eval(conn, window_days: int = 30, max_events: int = 400) -> dict:
    """Expanding-window logistic selector: for each event (chronological), train on
    all rows whose event CLOSED before this event's entry day, pick the best-EV
    structure with EV = p̂(win)·payoff1 − cost − fee ≥ EV_MIN, flat $1.
    Reports the last `window_days` as the headline (the as-if-live window)."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    data = build_dataset(conn, max_events)
    if not data:
        return {"error": "no data"}
    # group rows by event
    events: dict[tuple, list[dict]] = {}
    for r in data:
        events.setdefault((r["series"], r["period"], r["close"]), []).append(r)
    ev_keys = sorted(events, key=lambda k: k[2])
    trades = []
    for k in ev_keys:
        rows = events[k]
        entry_day = rows[0]["entry_day"]
        train = [r for r in data if r["close"].date().isoformat() < entry_day]
        if len(train) < MIN_TRAIN:
            continue
        Xt = np.array([r["x"] for r in train])
        yt = np.array([r["y"] for r in train])
        if len(set(yt)) < 2:
            continue
        sc = StandardScaler().fit(Xt)
        clf = LogisticRegression(max_iter=500, class_weight="balanced")
        clf.fit(sc.transform(Xt), yt)
        best = None
        for r in rows:
            p = float(clf.predict_proba(sc.transform([r["x"]]))[0, 1])
            fee1 = sum(taker_fee(pr, 1) for pr in r["leg_prices"])
            # p̂ = P(profitable); win nets ≈ 1−cost, loss nets ≈ −cost (eff basis)
            ev_ = p * 1.0 - r["cost"] - fee1
            if ev_ >= EV_MIN and (best is None or ev_ > best[0]):
                best = (ev_, p, r)
        if best is None:
            continue
        ev_, p, r = best
        count = max(1, int(STAKE / max(r["cost"], 0.10)))   # risk ≤ $1, ≤10 lots
        realized = settle_cash(r["payoff"], r["leg_prices"], count)
        trades.append({"series": r["series"], "period": r["period"],
                       "day": r["entry_day"], "desc": r["desc"][:40],
                       "lead_days": r["lead_days"],
                       "p_win": round(p, 3), "ev": round(ev_, 3),
                       "cost": r["cost"], "count": count,
                       "staked": round(r["outlay"] * count, 4),
                       "realized": round(realized, 4), "won": realized > 0,
                       "settle": r["close"].date().isoformat()})
    def _sm(ts):
        stk = sum(t["staked"] for t in ts)
        rl = sum(t["realized"] for t in ts)
        return {"n_trades": len(ts), "won": sum(1 for t in ts if t["won"]),
                "win_rate": round(sum(1 for t in ts if t["won"]) / len(ts), 4)
                if ts else None,
                "staked": round(stk, 4), "realized": round(rl, 4),
                "roi": round(rl / stk, 5) if stk > 0 else None,
                "trades": ts[-60:]}
    now = datetime.now(timezone.utc)
    cut30 = (now - timedelta(days=window_days)).date().isoformat()
    cut60 = (now - timedelta(days=60)).date().isoformat()
    out = {"all": _sm(trades),
           "last30": _sm([t for t in trades if t["day"] >= cut30]),
           "last60": _sm([t for t in trades if t["day"] >= cut60]),
           "n_dataset_rows": len(data), "n_events": len(ev_keys),
           "note": "expanding-window logistic; baseline to beat ="
                   " defer-to-market argmax"}
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES('ml_selector',?,'*',?,?,?)",
        (f"lr:{now.date().isoformat()}", f"{window_days}d",
         json.dumps(out, ensure_ascii=False, default=str), now.isoformat()))
    conn.commit()
    return out


def main():
    import argparse
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=400)
    args = ap.parse_args()
    s = load_settings()
    conn = init_db(s.db_path)
    out = walkforward_eval(conn, max_events=args.events)
    slim = {k: {kk: v[kk] for kk in ("n_trades", "won", "win_rate", "realized",
                                     "roi")}
            for k, v in out.items() if isinstance(v, dict) and "n_trades" in v}
    print(json.dumps({**slim, "rows": out.get("n_dataset_rows"),
                      "events": out.get("n_events")}, indent=1))


if __name__ == "__main__":
    main()
