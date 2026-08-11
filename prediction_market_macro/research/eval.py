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
                    max_events: int = 200, bankroll: float = 100.0,
                    collect_trades: bool = False) -> dict:
    """For every settled event: rebuild the book from candles, run the SAME
    enumerate_structs + decide() the production path uses, settle the opened
    structure against real results, and account PnL net of fees.

    ENTRY POLICY = PRODUCTION POLICY: scan daily from close−max_days_to_close to
    close−offset_hours and open on the FIRST day the gates clear (the lead sweep
    proved −1h-only entry is the WORST timing — evaluating the strategy at a
    timing it never uses was systematically pessimistic).

    Candles carry no depth ⇒ the depth gate is waived here (recorded in the output).
    The entropy gate applies as in prod (pure function of the model pmf). The
    calibration map and the per-strike capture filter are deliberately EXCLUDED:
    both are fit ON this very replay — using them inside it would be self-referential
    leakage. Consequence: replay ROI measures the raw strategy; the live path is
    strictly more conservative (fewer/smaller trades).

    collect_trades: also return the per-trade list. The aggregate `strike_capture` is a
    sum over the whole history, which cannot be sliced to an as-of date; `research/
    pit_gates` needs the individual (period, cap_key, expected, realized) rows to rebuild
    the capture memory as it stood on a given simulated day. Off by default because the
    result is persisted into an `experiments` row and the trade list would bloat it."""
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
        legs = conn.execute(
            "SELECT c.ticker, c.floor_strike strike, c.cap_strike, c.strike_type,"
            " c.close_time, s.result FROM contracts c"
            " JOIN settlements s ON s.ticker=c.ticker"
            " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')",
            (series, ev["period"])).fetchall()
        rel = conn.execute("SELECT scheduled_ts FROM releases WHERE cal=? AND period=?",
                           (spec.calendar, key)).fetchone()
        release_ts = datetime.fromisoformat(rel["scheduled_ts"]) if rel else None
        # production entry scan: earliest qualifying day inside the entry window
        max_lead = float(gates.get("max_days_to_close", 7.0))
        asof_candidates = []
        step = close_ts - timedelta(days=max_lead)
        step = step.replace(hour=16, minute=0, second=0, microsecond=0)
        while step < close_ts - timedelta(hours=offset_hours):
            asof_candidates.append(step)
            step += timedelta(days=1)
        asof_candidates.append(close_ts - timedelta(hours=offset_hours))
        d, st, asof, meta, results = None, None, None, [], {}
        import math as _math
        model_mean = None
        for cand in asof_candidates:
            meta, results = [], {}
            for l in legs:
                b, a = _candle_quote(conn, l["ticker"], cand)
                if b is None and a is None:
                    continue
                meta.append({"ticker": l["ticker"], "strike": l["strike"],
                             "cap_strike": l["cap_strike"],
                             "strike_type": l["strike_type"],
                             "close_time": l["close_time"], "yes_bid": b, "yes_ask": a,
                             "bid_depth": 1e9, "ask_depth": 1e9})
                results[l["ticker"]] = l["result"]
            if not meta:
                continue
            try:
                pred = fn(conn, cand, key, series=series)
            except Exception:                              # noqa: BLE001
                continue
            model_mean, entropy_norm = None, None
            if isinstance(pred.dist, Categorical):
                structs = _structs_categorical(meta, pred.dist.probs)
                pv = [v for v in pred.dist.probs.values() if v > 0]
            else:
                pmf = grid_pmf(pred.dist, spec.round_rule)
                model_mean = float(sum(k * v for k, v in pmf.items()))
                pv = [v for v in pmf.values() if v > 0]
                structs = enumerate_structs(meta, pmf, strict=spec.strict_gt)
            entropy_norm = (-sum(p * _math.log(p) for p in pv) / _math.log(len(pv))
                            if len(pv) > 1 else None)
            cand_d = decide(structs, now=cand, close_time=close_ts,
                            release_ts=release_ts, market_implied=None,
                            already_open=False, bankroll=bankroll,
                            gates=gates, entropy_norm=entropy_norm)
            if cand_d.action == "open" and cand_d.struct is not None:
                d, asof = cand_d, cand
                break
        if d is None:
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
        # capital at risk on production's definition — the third and last site of the
        # gross-notional bug fixed in `walkforward._trade_row` and `pnl_score._staked`
        # (§25.2a). `decide` books size_usd = count * fill_cost(count), and fill_cost
        # returns the NET debit (sum(px) - 1) on a bucket because one leg pays in every
        # branch; summing raw leg prices charges a bucket ~$1/contract too much. Realised
        # dollars are untouched, but the ROI DENOMINATOR inflates, so `roi` here — which
        # `gate_verdict` tests and `series_enable` now folds over — read better than the
        # strategy is. Only bucket structures differ, which is why it hid three times.
        # 2026-08-11 convention: stake includes entry taker fees (ROI floor -100%)
        staked = st.fill_cost(count) * count + sum(
            taker_fee(l.price, count) for l in st.legs)
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
    out = {"series": series, "n_events": len(events), "n_trades": len(trades),
           "skipped": skipped, "staked": round(tot_staked, 4),
           "realized": round(tot_real, 4),
           "roi": round(tot_real / tot_staked, 5) if tot_staked > 0 else None,
           "edge_capture": round(tot_real / tot_exp, 5) if tot_exp > 0 else None,
           "pnl_curve": curve[-60:], "strike_capture": caps,
           "depth_gate": "waived (candles carry no depth)"}
    if collect_trades:
        out["trades"] = trades
    return out


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
    # experiments PK is (name, config_hash) — the hash MUST carry the series or
    # every series' row REPLACEs the previous one (series_gate would only ever
    # exist for the last-evaluated series and exec's gate read goes blind)
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES(?,?,?,?,?,?)",
        (name, f"eval:{series}", series, window,
         json.dumps(metrics, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()


def chronos_replay_scores(conn, series: str, max_events: int = 20) -> dict | None:
    """Chronos-2 HISTORICAL replay scoring (backlog item): ts_foundation.predict is
    PIT by construction (FeatureStore reads kt<=asof), so it can be scored at
    -1h over settled history exactly like the production model — evidence for the
    §7-bis shadow gate accrues from the past, not just from the daily shadow."""
    try:
        from prediction_market_macro.model import ts_foundation
        if series not in ts_foundation._SOURCES:
            return None
    except Exception:                                      # noqa: BLE001
        return None
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.model.common import grid_pmf as _gp, leg_fair
    from prediction_market_macro.research.backtest import (_market_leg_prob,
                                                           _settle_release_ts)
    from prediction_market_macro.util.periods import kalshi_period_to_key
    spec = REGISTRY[series]
    events = conn.execute(
        "SELECT s.period, MAX(c.close_time) ct FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker WHERE s.series=?"
        " AND s.result IN ('yes','no') GROUP BY s.period ORDER BY ct DESC LIMIT ?",
        (series, max_events)).fetchall()
    now_iso = datetime.now(timezone.utc).isoformat()
    per_b, mkt_b, scored = [], [], 0
    for ev in events:
        key = kalshi_period_to_key(ev["period"])
        if not key or not ev["ct"]:
            continue
        close_ts = datetime.fromisoformat(ev["ct"].replace("Z", "+00:00"))
        asof = close_ts - timedelta(hours=1)
        # same close-vs-print hazard replay_series has: where the book closed after the
        # release, close−1h hands the model the number it is scored on. Step behind it.
        release_ts = _settle_release_ts(conn, spec, key)
        if release_ts is not None and asof >= release_ts:
            asof = release_ts - timedelta(seconds=1)
        try:
            pred = ts_foundation.predict(conn, asof, key, series)
            pmf = _gp(pred.dist, spec.round_rule)
        except Exception:                                  # noqa: BLE001
            continue
        legs = conn.execute(
            "SELECT c.ticker, c.floor_strike, c.cap_strike, c.strike_type, s.result"
            " FROM contracts c JOIN settlements s ON s.ticker=c.ticker"
            " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')",
            (series, ev["period"])).fetchall()
        bs, bk, n = 0.0, 0.0, 0
        for l in legs:
            # one shared market reader with replay_series, so the shadow track and the
            # production track score the SAME leg universe and stay comparable
            mp = _market_leg_prob(conn, l["ticker"], asof)
            if mp is None or l["floor_strike"] is None:
                continue
            try:
                fair = leg_fair(pmf, l["strike_type"] or "greater",
                                l["floor_strike"], l["cap_strike"])
            except Exception:                              # noqa: BLE001
                continue
            out01 = 1.0 if l["result"] == "yes" else 0.0
            bs += (fair - out01) ** 2
            bk += (mp - out01) ** 2
            n += 1
        if n:
            scored += 1
            per_b.append(bs / n)
            mkt_b.append(bk / n)
            conn.execute(
                "INSERT OR REPLACE INTO source_scores(series, period, source,"
                " offset, brier, n_legs, created_ts) VALUES(?,?,?,?,?,?,?)",
                (series, key, "chronos", "-1h", round(bs / n, 6), n, now_iso))
    conn.commit()
    if not scored:
        return None
    m = {"n": scored, "brier_model-1h": round(float(np.mean(per_b)), 5),
         "brier_market-1h": round(float(np.mean(mkt_b)), 5),
         "n_scored-1h": scored,
         "note": "chronos2 zero-shot replayed at -1h (PIT); evidence for §7-bis"}
    _store(conn, "chronos_replay", series, f"n{scored}", m)
    return m


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
    # collect_trades because §25.4's per-series switch is a fold over the individual
    # trades, not over the aggregate: the hysteresis makes the verdict path-dependent.
    # The trade list is stripped back out before the row is stored (see below).
    dec = decision_replay(conn, series, collect_trades=True)
    dr = drift_check([p.get("brier_model-1h") for p in per])
    if dr.get("drift"):
        conn.execute(
            "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), "warn", "drift",
             f"{series}: trailing Brier {dr['trail_mean']} vs prior {dr['prior_mean']}"))
        conn.commit()
    verdict = gate_verdict(agg, dec, dm)
    # per-event source scores → ensemble weight learning (§19-2)
    now_iso = datetime.now(timezone.utc).isoformat()
    for p in per:
        for src, keyn in (("model", "brier_model-1h"), ("market", "brier_market-1h")):
            if p.get(keyn) is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO source_scores(series, period, source,"
                    " offset, brier, n_legs, created_ts) VALUES(?,?,?,?,?,?,?)",
                    (series, p["period"], src, "-1h", round(p[keyn], 6),
                     p.get("n_legs-1h"), now_iso))
    conn.commit()
    # walk-forward POOLED source (improvement C): per-leg binary log-pool of model
    # and market probs with inverse-MSPE weights learned ONLY from past events —
    # measures what the ensemble would have scored, no leakage. When the market is
    # sharper, pooling drags fair toward it and kills the false edges the decision
    # replay showed realizing as losses.
    pooled_b, mkt_b_paired = [], []
    cum_m, cum_k, n_seen = 0.0, 0.0, 0
    for p in per:
        legs = p.get("legs-1h") or []
        bm, bk = p.get("brier_model-1h"), p.get("brier_market-1h")
        if legs and n_seen >= 3:
            wm_raw = 1.0 / max(cum_m / n_seen, 1e-6)
            wk_raw = 1.0 / max(cum_k / n_seen, 1e-6)
            wm = max(0.10, min(0.90, wm_raw / (wm_raw + wk_raw)))
            wk = 1.0 - wm
            bs, n = 0.0, 0
            for f, mp, out in legs:
                f = min(max(f, 1e-4), 1 - 1e-4)
                mp = min(max(mp, 1e-4), 1 - 1e-4)
                num = (f ** wm) * (mp ** wk)
                den = num + ((1 - f) ** wm) * ((1 - mp) ** wk)
                pp = num / den
                bs += (pp - out) ** 2
                n += 1
            if n:
                pooled_b.append(bs / n)
                mkt_b_paired.append(bk)
                conn.execute(
                    "INSERT OR REPLACE INTO source_scores(series, period, source,"
                    " offset, brier, n_legs, created_ts) VALUES(?,?,?,?,?,?,?)",
                    (series, p["period"], "pooled", "-1h", round(bs / n, 6), n,
                     now_iso))
        if bm is not None and bk is not None:
            cum_m += bm
            cum_k += bk
            n_seen += 1
    if pooled_b:
        _store(conn, "pooled_replay", series, f"n{len(pooled_b)}", {
            "n": len(pooled_b),
            "brier_model-1h": round(float(np.mean(pooled_b)), 5),
            "brier_market-1h": round(float(np.mean(
                [b for b in mkt_b_paired if b is not None])), 5),
            "n_scored-1h": len(pooled_b),
            "note": "pooled = walk-forward log-pool(model, market), no leakage"})
    conn.commit()
    # isotonic calibration map refit (OOS pairs only, §19-3) — SHADOW ONLY since
    # 2026-08-10. The live map is pinned to identity: the 08-09 refit produced cliff
    # maps on the three energy series (KXNATGASW mapped every model prob in
    # [0.27, 0.55] to 0.60 and >=0.74 to 1.00) because `pairs` are per-LEG samples
    # clustered by event (~480 legs ~ 43 events — the #129 clustering lesson again),
    # and decision #4346 then bought a raw-fair-0.334 leg at cost 0.36 as if fair
    # were 0.60. A skill-less model isotonically calibrated onto its base rate feeds
    # Kelly a flat probability, which manufactures edge from the price structure.
    # Re-enabling a live map requires its own preregistered forward criterion
    # (docs/PREREGISTER.md); until then the fit lands in 'calibration_map_shadow'
    # for research visibility only.
    from prediction_market_macro.strategy.calibration import (fit_map, store_map,
                                                              store_named_map)
    store_named_map(conn, series, "calibration_map_shadow", fit_map(pairs))
    store_map(conn, series, None)
    # favorite-longshot map for the MARKET input (§23.2-3b): (market prob, outcome)
    mkt_pairs = [(mp, o) for p in per for _f, mp, o in p.get("legs-1h", [])]
    store_named_map(conn, series, "market_calibration_map", fit_map(mkt_pairs))
    # ACI conformal throttle state (§23.2-4) — uses the rows written above
    from prediction_market_macro.strategy import conformal
    conformal.evaluate(conn, series)
    # §25.4 per-series switch, folded over the trades above. Stored as its own row so
    # `decide_all` can read a verdict in one cheap query instead of re-running the
    # replay; the trade list itself is dropped from the decision_replay row, which is
    # why `collect_trades` defaults off (the docstring's "would bloat it").
    from prediction_market_macro.strategy import series_enable as _se
    se = _se.evaluate(dec.get("trades") or [])
    _store(conn, "series_enable", series, f"n{se['n']}", se)
    dec = {k: v for k, v in dec.items() if k != "trades"}
    _store(conn, "decision_replay", series, f"n{dec['n_trades']}", dec)
    _store(conn, "calibration", series, f"n{len(pairs)}",
           {"bins": calib, "n_pairs": len(pairs)})
    _store(conn, "series_gate", series, f"n{agg['n']}",
           {**verdict, "dm": dm, "ci": ci, "drift": dr})
    return {"series": series, "real": verdict["real"], "reasons": verdict["reasons"],
            "roi": dec.get("roi"), "edge_capture": dec.get("edge_capture"),
            "dm_p": dm.get("p"), "enabled": se["enabled"]}


def shadow_gate(conn, series: str, prefix: str, min_weekly: int = 8,
                min_monthly: int = 3) -> dict:
    """§7-bis promotion counter for shadow members (chronos2/*, bridge/*,
    ensemble/*): count settled periods where the shadow had a pred, score its
    ladder vs the market where possible, write an experiments row. Promotion stays
    MANUAL — this makes the count/verdict visible instead of living in comments."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.model.common import leg_fair
    spec = REGISTRY[series]
    cadence = spec.cadence
    need = min_weekly if cadence == "weekly" else min_monthly
    events = conn.execute(
        "SELECT s.period, MAX(c.close_time) ct FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker WHERE s.series=?"
        " AND s.result IN ('yes','no') GROUP BY s.period", (series,)).fetchall()
    from prediction_market_macro.util.periods import kalshi_period_to_key
    scored, b_shadow, b_market = 0, [], []
    for ev in events:
        key = kalshi_period_to_key(ev["period"])
        if not key or not ev["ct"]:
            continue
        close_ts = datetime.fromisoformat(ev["ct"].replace("Z", "+00:00"))
        asof = close_ts - timedelta(hours=1)
        pr = conn.execute(
            "SELECT ladder_json FROM preds WHERE series=? AND period=? AND"
            " model_version LIKE ? AND asof<=? ORDER BY asof DESC LIMIT 1",
            (series, key, prefix + "%", asof.isoformat())).fetchone()
        if pr is None or not pr["ladder_json"]:
            continue
        pmf = {float(k): v for k, v in json.loads(pr["ladder_json"]).items()}
        legs = conn.execute(
            "SELECT c.ticker, c.floor_strike, c.cap_strike, c.strike_type, s.result"
            " FROM contracts c JOIN settlements s ON s.ticker=c.ticker"
            " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')",
            (series, ev["period"])).fetchall()
        bs, bk, n = 0.0, 0.0, 0
        for l in legs:
            mp = None
            r = conn.execute(
                "SELECT yes_bid_close, yes_ask_close FROM candles WHERE ticker=?"
                " AND end_ts<=? ORDER BY end_ts DESC LIMIT 1",
                (l["ticker"], int(asof.timestamp()))).fetchone()
            if r and r["yes_bid_close"] is not None and r["yes_ask_close"] is not None:
                mp = (r["yes_bid_close"] + r["yes_ask_close"]) / 2
            if mp is None or l["floor_strike"] is None:
                continue
            try:
                fair = leg_fair(pmf, l["strike_type"] or "greater",
                                l["floor_strike"], l["cap_strike"])
            except Exception:                             # noqa: BLE001
                continue
            out = 1.0 if l["result"] == "yes" else 0.0
            bs += (fair - out) ** 2
            bk += (mp - out) ** 2
            n += 1
        if n:
            scored += 1
            b_shadow.append(bs / n)
            b_market.append(bk / n)
    bm = round(float(np.mean(b_shadow)), 5) if b_shadow else None
    bk_ = round(float(np.mean(b_market)), 5) if b_market else None
    verdict = {"prefix": prefix, "cadence": cadence, "n_scored": scored,
               "n_required": need, "brier_shadow": bm, "brier_market": bk_,
               "eligible": bool(scored >= need and bm is not None and bk_ is not None
                                and bm < bk_),
               "note": "promotion is manual; eligible only surfaces the §7-bis count"}
    _store(conn, f"shadow_gate_{prefix.rstrip('/')}", series, f"n{scored}", verdict)
    return verdict


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
        for prefix in ("chronos2/", "bridge/", "ensemble/"):
            try:
                shadow_gate(conn, series, prefix)
            except Exception:                              # noqa: BLE001
                continue
        try:
            chronos_replay_scores(conn, series)
        except Exception:                                  # noqa: BLE001
            pass
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
