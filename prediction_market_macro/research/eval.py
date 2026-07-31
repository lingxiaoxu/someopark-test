"""research/eval.py — evaluation layer + real-money adoption gate (PLAN §9.5, §7-bis;
also fills the mother-template oos_eval role from §15).

This module answers the system's terminal question: WHEN may a series go live?

  dm_test            Diebold–Mariano (Harvey-Leybourne-Newbold small-sample t) on the
                     per-event Brier differential (model − market)
  bootstrap_ci       percentile CI of the mean Brier differential (deterministic seed)
  calibration_table  decile reliability bins from replayed per-leg (fair, outcome) pairs
  decision_replay    full scan→decide→PnL replay through the PRODUCTION decide() —
                     answers "would we have MADE MONEY", not just "were we accurate"
  drift_check        trailing-vs-prior Brier degradation alarm → alerts table
  gate               §9.5 real-money gate; writes the experiments 'series_gate' row that
                     exec.kalshi_exec.trading_allowed() reads. No row ⇒ paper forever.

Gate criteria (§9.5 — ALL must hold before real=true):
  n_scored(-1h) >= 12                    (enough replayed prints)
  brier_model(-1h) < brier_market(-1h)   (beats the market where it counts)
  decision-replay ROI > 0                (net-of-fee, through production gates)
  edge_capture > 0.4                     (realized PnL / expected edge)
  DM p < 0.10                            (win is statistically real, not noise)
Small samples therefore stay paper even when the point estimate wins — 铁律 7.

    python -m prediction_market_macro.research.eval [--series KXCPI]
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np

# ── statistics ───────────────────────────────────────────────────────────────


def dm_test(diffs: list[float]) -> dict:
    """Diebold–Mariano on a per-event loss differential (model − market; negative =
    model better), HLN small-sample correction, h=1 (event-level losses, no overlap).
    Returns {n, mean, t, p} — p is one-sided P(model better | H0 equal)."""
    d = np.asarray([x for x in diffs if x is not None], dtype=float)
    n = len(d)
    if n < 4:
        return {"n": n, "mean": round(float(d.mean()), 6) if n else None,
                "t": None, "p": None, "note": "n<4 — no test"}
    mean = float(d.mean())
    var = float(d.var(ddof=1))
    if var <= 0:
        return {"n": n, "mean": round(mean, 6), "t": None, "p": None,
                "note": "zero variance"}
    dm = mean / math.sqrt(var / n)
    hln = dm * math.sqrt((n + 1 - 2 + 0) / n)          # h=1 ⇒ factor sqrt((n-1)/n)
    from scipy import stats
    p = float(stats.t.cdf(hln, df=n - 1))              # one-sided: mean<0 ⇒ small p
    return {"n": n, "mean": round(mean, 6), "t": round(hln, 4), "p": round(p, 5)}


def bootstrap_ci(diffs: list[float], n_boot: int = 2000, alpha: float = 0.05,
                 seed: int = 0) -> dict:
    d = np.asarray([x for x in diffs if x is not None], dtype=float)
    if len(d) < 4:
        return {"lo": None, "hi": None, "note": "n<4"}
    rng = np.random.RandomState(seed)
    means = np.sort([d[rng.randint(0, len(d), len(d))].mean() for _ in range(n_boot)])
    return {"lo": round(float(means[int(n_boot * alpha / 2)]), 6),
            "hi": round(float(means[int(n_boot * (1 - alpha / 2)) - 1]), 6)}


def calibration_table(pairs: list[tuple[float, float]], bins: int = 10) -> list[dict]:
    """pairs: [(fair, outcome01)]. Decile reliability table (PLAN §9.5/§12 weekly)."""
    out = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        sel = [(f, o) for f, o in pairs if lo <= f < hi or (i == bins - 1 and f == 1.0)]
        out.append({
            "bin": f"[{lo:.1f},{hi:.1f})", "n": len(sel),
            "mean_fair": round(float(np.mean([f for f, _ in sel])), 4) if sel else None,
            "freq": round(float(np.mean([o for _, o in sel])), 4) if sel else None})
    return out


def drift_check(per_event_model_brier: list[float], trail: int = 8) -> dict:
    """Trailing `trail` events vs everything before: relative + absolute degradation.
    drift=True ⇒ caller writes a warn alert (feeds the health red-light path)."""
    xs = [x for x in per_event_model_brier if x is not None]
    if len(xs) < trail + 4:
        return {"drift": False, "note": f"n={len(xs)}<{trail + 4}"}
    prior, trailing = xs[:-trail], xs[-trail:]
    pm, tm = float(np.mean(prior)), float(np.mean(trailing))
    drift = tm > pm * 1.5 and (tm - pm) > 0.02
    return {"drift": drift, "prior_mean": round(pm, 5), "trail_mean": round(tm, 5),
            "trail": trail}


# ── decision replay (scan→decide→PnL through production code) ────────────────


def _candle_quote(conn, ticker: str, asof: datetime):
    r = conn.execute(
        "SELECT yes_bid_close, yes_ask_close FROM candles WHERE ticker=? AND end_ts<=?"
        " ORDER BY end_ts DESC LIMIT 1", (ticker, int(asof.timestamp()))).fetchone()
    if r is None:
        return None, None
    return r["yes_bid_close"], r["yes_ask_close"]


def decision_replay(conn, series: str, offset_hours: float = 1.0,
                    max_events: int = 200, bankroll: float = 100.0) -> dict:
    """For every settled event: rebuild the book at asof from candles, run the SAME
    enumerate_structs + decide() the production path uses, settle the opened structure
    against real results, and account PnL net of fees.

    Candles carry no depth ⇒ the depth gate is waived here (recorded in the output);
    every other gate (net-edge, sanity, freeze, Kelly) applies exactly as in prod."""
    import importlib
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.model.common import Categorical, grid_pmf
    from prediction_market_macro.ops.decide_all import _structs_categorical
    from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
    from prediction_market_macro.strategy.decision import GATES, decide
    from prediction_market_macro.strategy.edge import enumerate_structs, taker_fee
    from prediction_market_macro.util.periods import kalshi_period_to_key

    spec = REGISTRY[series]
    disp = SERIES_DISPATCH[series]
    fn = getattr(importlib.import_module(disp[0]), disp[1])
    gates = dict(GATES)
    gates["min_leg_depth_usd"] = 0.0                       # candles carry no depth
    events = conn.execute(
        "SELECT s.period, MAX(c.close_time) ct FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker WHERE s.series=?"
        " AND s.result IN ('yes','no') GROUP BY s.period ORDER BY ct DESC LIMIT ?",
        (series, max_events)).fetchall()
    trades, skipped = [], 0
    for ev in reversed(events):                            # chronological
        key = kalshi_period_to_key(ev["period"])
        if not key or not ev["ct"]:
            continue
        close_ts = datetime.fromisoformat(ev["ct"].replace("Z", "+00:00"))
        asof = close_ts - timedelta(hours=offset_hours)
        legs = conn.execute(
            "SELECT c.ticker, c.floor_strike strike, c.cap_strike, c.strike_type,"
            " c.close_time, s.result FROM contracts c"
            " JOIN settlements s ON s.ticker=c.ticker"
            " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')",
            (series, ev["period"])).fetchall()
        meta, results = [], {}
        for l in legs:
            b, a = _candle_quote(conn, l["ticker"], asof)
            if b is None and a is None:
                continue
            meta.append({"ticker": l["ticker"], "strike": l["strike"],
                         "cap_strike": l["cap_strike"], "strike_type": l["strike_type"],
                         "close_time": l["close_time"], "yes_bid": b, "yes_ask": a,
                         "bid_depth": 1e9, "ask_depth": 1e9})
            results[l["ticker"]] = l["result"]
        if not meta:
            continue
        try:
            pred = fn(conn, asof, key, series=series)
        except Exception:                                  # noqa: BLE001
            skipped += 1
            continue
        model_mean = None
        if isinstance(pred.dist, Categorical):
            structs = _structs_categorical(meta, pred.dist.probs)
        else:
            pmf = grid_pmf(pred.dist, spec.round_rule)
            model_mean = float(sum(k * v for k, v in pmf.items()))
            structs = enumerate_structs(meta, pmf, strict=spec.strict_gt)
        rel = conn.execute("SELECT scheduled_ts FROM releases WHERE cal=? AND period=?",
                           (spec.calendar, key)).fetchone()
        release_ts = datetime.fromisoformat(rel["scheduled_ts"]) if rel else None
        d = decide(structs, now=asof, close_time=close_ts, release_ts=release_ts,
                   market_implied=None, already_open=False, bankroll=bankroll,
                   gates=gates)
        if d.action != "open" or d.struct is None:
            continue
        st, count = d.struct, d.count
        realized = 0.0
        for leg in st.legs:
            res = results.get(leg.ticker)
            if res is None:
                realized = None
                break
            won = (res == leg.side)
            realized += ((1.0 if won else 0.0) - leg.price) * count \
                - taker_fee(leg.price, count)
        if realized is None:
            skipped += 1
            continue
        expected = st.net_edge(count) * count              # $ edge net of fee
        staked = sum(l.price for l in st.legs) * count
        # per-strike capture key: distance from the model mean in grid steps
        if model_mean is not None and st.legs[0].ticker and st.kind == "single":
            strike = next((m["strike"] for m in meta
                           if m["ticker"] == st.legs[0].ticker), None)
            off = (int(round((strike - model_mean) / spec.round_rule))
                   if strike is not None else 0)
            cap_key = f"{st.legs[0].side}@{max(-5, min(5, off)):+d}"
        else:
            cap_key = f"{st.kind}:{st.legs[0].side}"
        trades.append({"period": key, "desc": st.desc, "count": count,
                       "staked": round(staked, 4), "expected": round(expected, 4),
                       "realized": round(realized, 4), "cap_key": cap_key})
    tot_staked = sum(t["staked"] for t in trades)
    tot_real = sum(t["realized"] for t in trades)
    tot_exp = sum(t["expected"] for t in trades)
    caps: dict[str, dict] = {}
    for t in trades:
        c = caps.setdefault(t["cap_key"], {"n": 0, "expected": 0.0, "realized": 0.0})
        c["n"] += 1
        c["expected"] = round(c["expected"] + t["expected"], 4)
        c["realized"] = round(c["realized"] + t["realized"], 4)
    curve, run = [], 0.0
    for t in trades:
        run += t["realized"]
        curve.append(round(run, 4))
    return {"series": series, "n_events": len(events), "n_trades": len(trades),
            "skipped": skipped, "staked": round(tot_staked, 4),
            "realized": round(tot_real, 4),
            "roi": round(tot_real / tot_staked, 5) if tot_staked > 0 else None,
            "edge_capture": round(tot_real / tot_exp, 5) if tot_exp > 0 else None,
            "pnl_curve": curve[-60:], "strike_capture": caps,
            "depth_gate": "waived (candles carry no depth)"}


# ── the gate ─────────────────────────────────────────────────────────────────


def gate_verdict(replay_agg: dict, dec: dict, dm: dict) -> dict:
    """Pure §9.5 verdict from the three metric bundles (unit-testable)."""
    reasons = []
    n = replay_agg.get("n_scored-1h") or 0
    bm, bk = replay_agg.get("brier_model-1h"), replay_agg.get("brier_market-1h")
    if n < 12:
        reasons.append(f"n_scored {n}<12")
    if bm is None or bk is None or not bm < bk:
        reasons.append(f"brier model {bm} !< market {bk}")
    roi = dec.get("roi")
    if roi is None or not roi > 0:
        reasons.append(f"decision-replay roi {roi} !> 0")
    ec = dec.get("edge_capture")
    if ec is None or not ec > 0.4:
        reasons.append(f"edge_capture {ec} !> 0.4")
    p = dm.get("p")
    if p is None or not p < 0.10:
        reasons.append(f"dm p {p} !< 0.10 (not significant — 铁律7)")
    return {"real": not reasons, "reasons": reasons,
            "criteria": {"n_scored": n, "brier_model": bm, "brier_market": bk,
                         "roi": roi, "edge_capture": ec, "dm_p": p}}


def _store(conn, name: str, series: str, window: str, metrics: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES(?,?,?,?,?,?)",
        (name, "eval", series, window, json.dumps(metrics, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()


def run_series(conn, series: str) -> dict:
    """Replay → decision replay → DM/CI → calibration → drift → gate. Stores
    experiments rows: decision_replay / calibration / series_gate."""
    from prediction_market_macro.research.backtest import replay_series
    rep = replay_series(conn, series, collect_legs=True)
    agg, per = rep["agg"], rep["per_release"]
    if agg["n"] == 0:
        return {"series": series, "skip": "no settled history"}
    diffs = [p["brier_model-1h"] - p["brier_market-1h"] for p in per
             if p.get("brier_model-1h") is not None
             and p.get("brier_market-1h") is not None]
    dm = dm_test(diffs)
    ci = bootstrap_ci(diffs)
    pairs = [(f, o) for p in per for f, _mp, o in p.get("legs-1h", [])]
    calib = calibration_table(pairs)
    dec = decision_replay(conn, series)
    dr = drift_check([p.get("brier_model-1h") for p in per])
    if dr.get("drift"):
        conn.execute(
            "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), "warn", "drift",
             f"{series}: trailing Brier {dr['trail_mean']} vs prior {dr['prior_mean']}"))
        conn.commit()
    verdict = gate_verdict(agg, dec, dm)
    _store(conn, "decision_replay", series, f"n{dec['n_trades']}", dec)
    _store(conn, "calibration", series, f"n{len(pairs)}",
           {"bins": calib, "n_pairs": len(pairs)})
    _store(conn, "series_gate", series, f"n{agg['n']}",
           {**verdict, "dm": dm, "ci": ci, "drift": dr})
    return {"series": series, "real": verdict["real"], "reasons": verdict["reasons"],
            "roi": dec.get("roi"), "edge_capture": dec.get("edge_capture"),
            "dm_p": dm.get("p")}


def run_all(conn) -> dict:
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
    out = {}
    for series in REGISTRY:
        if series not in SERIES_DISPATCH:
            continue
        try:
            out[series] = run_series(conn, series)
        except Exception as e:                             # noqa: BLE001
            out[series] = {"series": series, "error": str(e)[:200]}
    return out


def main():
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default=None)
    args = ap.parse_args()
    s = load_settings()
    conn = init_db(s.db_path)
    res = run_series(conn, args.series) if args.series else run_all(conn)
    (s.output_dir / "eval_gates.json").write_text(json.dumps(res, indent=1,
                                                             ensure_ascii=False))
    print(json.dumps(res, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
