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


def run(conn, days: int = 30, offset_hour: int = 16, bankroll: float = 100.0,
        max_lead_days: float | None = None, fair_mode: str = "model") -> dict:
    """max_lead_days: entry allowed only when days-to-close <= this (the sweep
    knob — each lead sees DIFFERENT data and different predictions at its asof).

    fair_mode='pooled': decision fair = walk-forward log-pool of the model pmf
    with the DEVIGGED market pmf, weights ∝ 1/MSPE learned strictly inside the
    simulated timeline — an event's own outcome only enters the weights AFTER its
    close date passes in simulation (pending-score flush), so the pool at day D
    knows nothing later than D. Pooling with a sharper market kills fictional
    edges; surviving disagreements are where betting still makes sense."""
    from prediction_market_macro.model.ensemble import finite_pmf, log_pool
    from prediction_market_macro.strategy import devig as _devig
    now = datetime.now(timezone.utc)
    gates = dict(GATES)
    gates["min_leg_depth_usd"] = 0.0               # candles carry no depth
    if max_lead_days is not None:
        gates["max_days_to_close"] = float(max_lead_days)
    # walk-forward pool state (per series): cumulative Brier per source + pending
    # scores that unlock only when their event's close date passes in simulation
    pool_runner: dict[str, dict[str, list[float]]] = {}
    pending_scores: list[tuple[datetime, str, float, float]] = []  # (close, series, bm, bk)

    def _pool_weights(series: str) -> dict[str, float] | None:
        r = pool_runner.get(series)
        if not r or len(r.get("model", [])) < 3:
            return None
        mm = sum(r["model"]) / len(r["model"])
        mk = sum(r["market"]) / len(r["market"])
        wm_raw, wk_raw = 1.0 / max(mm, 1e-6), 1.0 / max(mk, 1e-6)
        wm = max(0.10, min(0.90, wm_raw / (wm_raw + wk_raw)))
        return {"model": wm, "market": 1.0 - wm}
    opened: dict[tuple[str, str], dict] = {}       # (series, key) -> edge trade
    opened_argmax: dict[tuple[str, str], dict] = {}  # favourite (argmax) stream
    daily: list[dict] = []
    for d in range(days, 0, -1):
        day = (now - timedelta(days=d)).replace(hour=offset_hour, minute=0,
                                                second=0, microsecond=0)
        # flush pending pool scores whose events have CLOSED by simulated `day`
        # (an event's own outcome must never influence its own pool weights)
        if fair_mode == "pooled":
            still = []
            for cts, ser, bm, bk in pending_scores:
                if cts <= day:
                    r = pool_runner.setdefault(ser, {"model": [], "market": []})
                    r["model"].append(bm)
                    r["market"].append(bk)
                else:
                    still.append((cts, ser, bm, bk))
            pending_scores[:] = still
        day_trades = 0
        for ev in _open_settled_events(conn, day, now):
            key = kalshi_period_to_key(ev["tok"])
            if not key or ((ev["series"], key) in opened
                           and (ev["series"], key) in opened_argmax):
                continue                       # both streams already entered
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
                probs = dict(pred.dist.probs)
                if fair_mode == "pooled":
                    asks = {l["ticker"].rsplit("-", 1)[-1]: l["yes_ask"]
                            for l in meta if l.get("yes_ask")}
                    tot = sum(asks.values())
                    w = _pool_weights(ev["series"])
                    if tot > 0 and w:
                        mkp = {k: v / tot for k, v in asks.items()}
                        lp = {k: w["model"] * _math.log(max(probs.get(k, 0), 1e-6))
                              + w["market"] * _math.log(max(mkp.get(k, 0), 1e-6))
                              for k in set(probs) | set(mkp)}
                        mx = max(lp.values())
                        ex = {k: _math.exp(v - mx) for k, v in lp.items()}
                        z = sum(ex.values())
                        probs = {k: v / z for k, v in ex.items()}
                structs = _structs_categorical(meta, probs)
                pv = [v for v in probs.values() if v > 0]
            else:
                pmf = grid_pmf(pred.dist, spec.round_rule)
                mk_pmf = None
                if fair_mode == "pooled":
                    impl = _devig.ladder_implied(
                        [{"strike": m["strike"], "yes_bid": m["yes_bid"],
                          "yes_ask": m["yes_ask"], "ticker": m["ticker"]}
                         for m in meta])
                    mk_pmf = finite_pmf({float(k): v
                                         for k, v in (impl.get("pmf") or {}).items()})
                    w = _pool_weights(ev["series"])
                    if mk_pmf and w:
                        pmf = log_pool({"model": pmf, "market": mk_pmf}, w)
                structs = enumerate_structs(meta, pmf, strict=spec.strict_gt)
                pv = [v for v in pmf.values() if v > 0]
                # queue this event's model/market scores for post-close flush
                if fair_mode == "pooled" and mk_pmf:
                    from prediction_market_macro.model.common import leg_fair
                    bm_s, bk_s, n_l = 0.0, 0.0, 0
                    raw_pmf = grid_pmf(pred.dist, spec.round_rule)
                    for m in meta:
                        res = results.get(m["ticker"])
                        if res is None or m["strike"] is None:
                            continue
                        try:
                            fm = leg_fair(raw_pmf, m["strike_type"] or "greater",
                                          m["strike"], m["cap_strike"])
                            fk = leg_fair(mk_pmf, m["strike_type"] or "greater",
                                          m["strike"], m["cap_strike"])
                        except Exception:                  # noqa: BLE001
                            continue
                        out01 = 1.0 if res == "yes" else 0.0
                        bm_s += (fm - out01) ** 2
                        bk_s += (fk - out01) ** 2
                        n_l += 1
                    if n_l and not any(s2 == ev["series"] and c2 == ev["close_ts"]
                                       for c2, s2, *_ in pending_scores):
                        pending_scores.append((ev["close_ts"], ev["series"],
                                               bm_s / n_l, bk_s / n_l))
            if len(pv) > 1:
                entropy_norm = -sum(p * _math.log(p) for p in pv) / _math.log(len(pv))

            def _settle_struct(st, count):
                realized = 0.0
                for leg in st.legs:
                    res = results.get(leg.ticker)
                    if res is None:
                        return None
                    won = (res == leg.side)
                    realized += ((1.0 if won else 0.0) - leg.price) * count \
                        - taker_fee(leg.price, count)
                return realized

            def _trade_row(st, count, realized):
                lead_days = (ev["close_ts"] - day).total_seconds() / 86400.0
                return {"series": ev["series"], "period": key,
                        "day": day.date().isoformat(), "desc": st.desc,
                        "fair": round(st.fair, 4), "cost": round(st.cost, 4),
                        "count": count,
                        "staked": round(sum(l.price for l in st.legs) * count, 4),
                        "realized": round(realized, 4), "won": realized > 0,
                        "lead_days": round(lead_days, 1),
                        "settle": ev["close_ts"].date().isoformat()}

            # ── stream 1: EDGE (value) line — the existing gated decision ──
            dec = decide(structs, now=day, close_time=ev["close_ts"],
                         release_ts=None, market_implied=None,
                         already_open=(ev["series"], key) in opened,
                         bankroll=bankroll, gates=gates, entropy_norm=entropy_norm)
            if dec.action == "open" and dec.struct is not None:
                realized = _settle_struct(dec.struct, dec.count)
                if realized is not None:
                    opened[(ev["series"], key)] = _trade_row(dec.struct, dec.count,
                                                             realized)
                    day_trades += 1
            # ── stream 2: ARGMAX (favourite) line — WC hybrid's other leg: buy
            # the MODEL's most likely structure, flat $1, no edge requirement.
            # Price window [0.10, 0.90] keeps payoff room net of fees. ──
            if (ev["series"], key) not in opened_argmax:
                inside = ((ev["close_ts"] - day).total_seconds() / 86400.0
                          <= gates.get("max_days_to_close", 7.0))
                # defer-to-the-stronger-forecaster: bet the favourite only when
                # the MARKET's confidence >= the model's (fair <= cost). A weaker
                # model claiming the favourite is underpriced is adverse
                # selection (dual-window: fair>cost lost -15%/-26%; fair<=cost
                # won 27W-2L across 60d+30d)
                cands = [st for st in structs
                         if 0.10 <= st.cost <= 0.90 and st.fair > 0.5]
                if inside and cands:
                    st_a = max(cands, key=lambda x: x.fair)
                    # select-THEN-filter: the favourite is the max-fair pick; it
                    # qualifies only when the market's confidence >= the model's
                    if st_a.fair > st_a.cost:
                        st_a = None
                    count_a = max(1, int(1.0 / st_a.cost)) if st_a else 0
                    realized_a = _settle_struct(st_a, count_a) if st_a else None
                    if st_a and realized_a is not None:
                        opened_argmax[(ev["series"], key)] = _trade_row(
                            st_a, count_a, realized_a)
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

    def _stream_summary(ts_list):
        stk = sum(x["staked"] for x in ts_list)
        rl = sum(x["realized"] for x in ts_list)
        return {"n_trades": len(ts_list),
                "won": sum(1 for x in ts_list if x["won"]),
                "win_rate": round(sum(1 for x in ts_list if x["won"])
                                  / len(ts_list), 4) if ts_list else None,
                "staked": round(stk, 4), "realized": round(rl, 4),
                "roi": round(rl / stk, 5) if stk > 0 else None,
                "trades": sorted(ts_list, key=lambda x: x["settle"])[-60:]}

    argmax_trades = list(opened_argmax.values())
    # hybrid = WC live rule: edge bet where it fired, favourite where it passed
    hybrid = list(trades) + [t for k2, t in opened_argmax.items()
                             if k2 not in opened]
    streams = {"edge": _stream_summary(trades),
               "argmax": _stream_summary(argmax_trades),
               "hybrid": _stream_summary(hybrid)}
    out = {"days": days, "fair_mode": fair_mode, "streams": streams,
           "n_trades": len(trades),
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
        (f"d{days}:{fair_mode}:{now.date().isoformat()}", f"{days}d:{fair_mode}",
         json.dumps(out, ensure_ascii=False), now.isoformat()))
    conn.commit()
    return out


def coverage(conn, days: int = 30) -> dict:
    """Which registered bets the window can even test: settled events with candled
    legs per series, plus the registered-but-untestable ones (no settle in window)."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    out = {}
    for spec in REGISTRY.values():
        r = conn.execute(
            "SELECT COUNT(DISTINCT s.period) n FROM settlements s"
            " JOIN contracts c ON c.ticker=s.ticker"
            " JOIN candles cd ON cd.ticker=s.ticker"
            " WHERE s.series=? AND s.result IN ('yes','no') AND s.settled_ts>=?",
            (spec.ticker, since.isoformat())).fetchone()
        out[spec.ticker] = {"family": spec.family, "cadence": spec.cadence,
                            "events_in_window": r["n"],
                            "testable": r["n"] > 0,
                            "in_dispatch": spec.ticker in SERIES_DISPATCH}
    return out


def sweep(conn, days: int = 30, leads=(1.0, 3.0, 5.0, 7.0)) -> dict:
    """Full WF per entry-lead: at lead L an entry opens the first day within L
    days of close (fresher data, different prediction, different price)."""
    now = datetime.now(timezone.utc)
    per_lead = {}
    for lead in leads:
        r = run(conn, days=days, max_lead_days=lead)
        per_lead[f"{lead:g}d"] = {k: r.get(k) for k in
                                  ("n_trades", "win_rate", "staked", "realized",
                                   "roi", "by_series", "curve")}
    cov = coverage(conn, days)
    out = {"days": days, "leads": per_lead, "coverage": cov,
           "generated_at": now.isoformat(),
           "note": "one full PIT walk-forward per lead; per-series cells have tiny"
                   " n — read the GLOBAL row, treat per-series as anecdotes"}
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES('walkforward_sweep',?,'*',?,?,?)",
        (f"d{days}:{fair_mode}:{now.date().isoformat()}", f"{days}d:{fair_mode}",
         json.dumps(out, ensure_ascii=False), now.isoformat()))
    conn.commit()
    return out


def main():
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--pooled", action="store_true")
    args = ap.parse_args()
    s = load_settings()
    conn = init_db(s.db_path)
    if args.sweep:
        out = sweep(conn, days=args.days)
        slim = {"leads": {k: {kk: v[kk] for kk in ("n_trades", "win_rate",
                                                   "realized", "roi")}
                          for k, v in out["leads"].items()},
                "coverage": {k: v["events_in_window"]
                             for k, v in out["coverage"].items()}}
        print(json.dumps(slim, indent=1, ensure_ascii=False))
        return
    out = run(conn, days=args.days,
              fair_mode="pooled" if args.pooled else "model")
    print(json.dumps({k: v for k, v in out.items() if k not in ("trades", "curve")},
                     indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
