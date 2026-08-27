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
    """Mid of the newest bar at or before asof, or None when the book is empty.

    The empty-book test must be SYMMETRIC. It used to be `a < 1.0`, which threw away
    every leg the market had priced as near-certain YES while keeping the mirror image
    on the NO side (no `b > 0.0` test). Measured over the stored candles: 1407 of the
    1408 bars with ask=1.00 carry a LIVE bid (0.99 in 951 of them) — those are real
    quotes, not a missing-ask sentinel, and dropping them conditioned the scored leg
    universe on price level, which correlates with the outcome. Only bid=0.00 AND
    ask=1.00 together (1 bar) means nobody is quoting.
    """
    r = conn.execute(
        "SELECT yes_bid_close, yes_ask_close FROM candles WHERE ticker=? AND end_ts<=?"
        " ORDER BY end_ts DESC LIMIT 1", (ticker, int(asof.timestamp()))).fetchone()
    if r is None:
        return None
    b, a = r["yes_bid_close"], r["yes_ask_close"]
    if b is None or a is None:
        return None
    if b <= 0.0 and a >= 1.0:
        return None                          # genuinely no two-sided market
    return (b + a) / 2


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


def _settle_release_ts(conn, spec, key: str) -> datetime | None:
    """Wall-clock moment the SETTLING print became public, read from the same vintage
    store the models themselves read (fred_obs.knowledge_time).

    Deliberately NOT the calendars module / releases table: those only cover 2026-01
    onward while the settled history reaches back to 2021, and they carry known-wrong
    dates (BLS_CPI "2026-01" says Feb 11; the print was Feb 13).

    The period -> settling-observation mapping is EXACT, never a nearest-match window.
    DCOILWTICO and GASREGW publish daily, so a fuzzy window cheerfully returns the
    PREVIOUS day's print and manufactures leaks that do not exist (measured: 2 bogus
    KXWTIW hits). No exact row => None => the caller leaves asof alone and counts it,
    so an unmapped series degrades to today's behaviour instead of being corrupted.
    """
    sid = spec.fred_first_release
    if not sid or not key:
        return None
    if spec.cadence == "monthly" and len(key) == 7:
        et = f"{key}-01"
    elif spec.cadence == "quarterly" and "-Q" in key:
        y, q = key.split("-Q")
        et = f"{y}-{(int(q) - 1) * 3 + 1:02d}-01"
    elif spec.cadence == "weekly" and len(key) == 10:
        # claims: the Thursday release reports the week ending the PRIOR Saturday
        # (verified: key−5d hits an ICSA event_time, key−6d/−7d never do). The energy
        # weeklies are indexed by the settle date itself.
        d = datetime.fromisoformat(key).date()
        et = (d - timedelta(days=5)).isoformat() if spec.calendar == "DOL_CLAIMS" else key
    else:
        return None                              # per_event (FOMC): no stable mapping
    r = conn.execute("SELECT MIN(knowledge_time) k FROM fred_obs WHERE sid=?"
                     " AND event_time=?", (sid, et)).fetchone()
    return datetime.fromisoformat(r["k"]) if r and r["k"] else None


def replay_series(conn, series: str, asof_offsets=("-24h", "-1h"),
                  max_events: int = 200, collect_legs: bool = False,
                  params: dict | None = None, params_pit: bool = False) -> dict:
    """Generic settled-history replay for ANY ladder/categorical series.

    Brier needs no y: settled leg results ARE the outcomes. For each settled event,
    asof = (event close − offset); the PRODUCTION model predicts at that asof (PIT via
    its own vintage reads); per-leg fair via leg_fair with the leg's OWN strike
    metadata; the market is scored from stored candles at the same asof.

    `params` (#118) overrides the model's parameters for this replay and is forwarded
    only when it is not None, so the production call is byte-identical to what it was.
    It exists so a candidate parameter set can be scored through THIS function rather
    than through `param_wf.score_matrix`: the two do not pick the same `asof`. This one
    steps back behind the release when the book closed after the print (see the clamp
    below) and drops an event whose `data_horizon` reached past it; `param_wf` uses a
    flat close−1h. Scoring a candidate one way and the market the other way would make
    the "paired" comparison a comparison of two different asofs.

    `params_pit` (#198) is the THIRD meaning, and it is the one the gate wants. `None`
    means the registered defaults and a dict means one fixed set; neither is what
    production did. Production predicts through `param_select.current()`, which has
    returned an adopted, CHANGING set since 2026-08-11 — so the gate that authorises
    real money was grading a configuration nobody has run since. Measured 2026-08-27 on
    the live adopted sets: KXU3 flips from beating the market (0.03774 < 0.04159) to
    losing to it (0.04270), and KXFED flips the other way. #196's observer cannot see
    this by construction — it identifies a branch from the input KEY NAMES, and adopted
    params change values, never keys.

    With `params_pit=True` each event is predicted at the params that were IN FORCE at
    that event's own asof (`param_select.params_asof`, the PIT reader added 2026-08-12
    for the health canary). Not today's set applied backwards: the selection lanes chose
    those params BY scoring these very events, so replaying history at today's choice is
    an in-sample number. It has a use — see `eval.run_series`, which requires it as an
    additional bar precisely because it is optimistic — but it is not the track record.

    `params_mix{off}` reports which sets the graded sample actually ran, so a caller can
    tell "12 events at the live config" from "12 events at a config we abandoned".
    """
    import importlib
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.model.common import Categorical, leg_fair
    from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
    from prediction_market_macro.research.branch_parity import branch_of as _branch_of
    from prediction_market_macro.research.branch_parity import params_of as _params_of
    from prediction_market_macro.util.periods import kalshi_period_to_key
    if params_pit and params is not None:
        raise ValueError(
            "replay_series: params and params_pit are two different questions —"
            " one fixed set over all history, versus the set in force at each event."
            " Passing both would silently answer only one of them.")
    spec = REGISTRY[series]
    disp = SERIES_DISPATCH[series]
    fn = getattr(importlib.import_module(disp[0]), disp[1])
    events = conn.execute(
        "SELECT s.period, MAX(c.close_time) ct, COUNT(*) n FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker WHERE s.series=?"
        " AND s.result IN ('yes','no') GROUP BY s.period ORDER BY ct DESC LIMIT ?",
        (series, max_events)).fetchall()
    per, skipped = [], 0
    n_clamped = n_leaked = n_unknown = 0
    # #196: which code path each graded event took. Recorded HERE, on the events that
    # survive every drop below, because the branch mix is only evidence about the gate if
    # it is the mix of the exact sample the gate scores — a separate walk would drift.
    branch_mix: dict[str, dict[str, int]] = {off: {} for off in asof_offsets}
    # #198: and which PARAMS each graded event ran. Same reasoning, same sample — the
    # blind spot `branch_parity`'s own docstring names ("blind to one that only changes
    # their values") is exactly the params layer, and this is the counter that closes it.
    params_mix: dict[str, dict[str, int]] = {off: {} for off in asof_offsets}
    scored_branch_mix: dict[str, dict[str, int]] = {off: {} for off in asof_offsets}
    # SQL orders newest-first so LIMIT keeps the MOST RECENT max_events; the returned
    # per_release must nonetheless be CHRONOLOGICAL. eval.run_series feeds it to a
    # pooled walk-forward accumulator ("weights learned only from past events") and to
    # drift_check — newest-first silently made both read the future / invert the sign.
    # decision_replay already does the same `reversed(...)` for the same reason.
    for ev in reversed(events):
        key = kalshi_period_to_key(ev["period"])
        if not key or not ev["ct"]:
            continue
        close_ts = datetime.fromisoformat(ev["ct"].replace("Z", "+00:00"))
        legs = conn.execute(
            "SELECT c.ticker, c.floor_strike, c.cap_strike, c.strike_type, s.result"
            " FROM contracts c JOIN settlements s ON s.ticker=c.ticker"
            " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')",
            (series, ev["period"])).fetchall()
        release_ts = _settle_release_ts(conn, spec, key)
        n_unknown += release_ts is None
        rec = {"period": key, "n_legs_settled": len(legs)}
        for off in asof_offsets:
            hours = 24 if off == "-24h" else 1
            asof = close_ts - timedelta(hours=hours)
            if release_ts is not None and asof >= release_ts:
                # the book closed AFTER the print — KXPAYROLLS/KXU3 2026-01 closed 90min
                # past the 13:30Z release — so a close-anchored asof hands the model the
                # very number it is about to be scored on. Step back behind the print.
                asof = release_ts - timedelta(seconds=1)
                n_clamped += 1
            if params_pit:
                # the set in force at THIS event's asof, not today's. `params_asof`
                # reads the manual override PIT on its adoption timestamp, else that
                # day's param_selection row, else {} — the same order `current()` uses,
                # with wall-clock swapped for asof so a later adoption cannot leak back.
                from prediction_market_macro.research.param_select import params_asof
                eff = params_asof(conn, series, asof) or None
            else:
                eff = params
            try:
                pred = (fn(conn, asof, key, series=series) if eff is None
                        else fn(conn, asof, key, series=series, params=eff))
            except Exception:                                    # noqa: BLE001
                skipped += 1
                rec = None
                break
            if (release_ts is not None and pred.data_horizon is not None
                    and pred.data_horizon >= release_ts):
                # model reached past the print regardless of asof (an input sid whose
                # vintage read is not asof-bounded). Never score it. Dropped rather than
                # raised so one bad event cannot kill the weekly replay_all sweep.
                n_leaked += 1
                rec = None
                break
            if isinstance(pred.dist, Categorical):
                probs = pred.dist.probs
                pmf = None
            else:
                pmf = grid_pmf(pred.dist, spec.round_rule)
            bs_m, bs_k, n_legs = 0.0, 0.0, 0
            leg_pairs = []
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
                if collect_legs:
                    leg_pairs.append((round(fair, 5), round(mp, 5), out))
            rec[f"branch{off}"] = _branch_of(pred.inputs)
            rec[f"params{off}"] = _params_of(eff)
            if n_legs:
                rec[f"brier_model{off}"] = bs_m / n_legs
                rec[f"brier_market{off}"] = bs_k / n_legs
                rec[f"n_legs{off}"] = n_legs
                if collect_legs:
                    rec[f"legs{off}"] = leg_pairs
        if rec:
            per.append(rec)
            for off in asof_offsets:
                b = rec.get(f"branch{off}")
                if b:
                    branch_mix[off][b] = branch_mix[off].get(b, 0) + 1
                # SCORED events only — see the agg block below for why this denominator
                # differs from branch_mix's.
                if rec.get(f"brier_model{off}") is not None:
                    p_ = rec.get(f"params{off}")
                    if p_:
                        params_mix[off][p_] = params_mix[off].get(p_, 0) + 1
                    if b:
                        scored_branch_mix[off][b] = scored_branch_mix[off].get(b, 0) + 1
    agg = {"n": len(per), "skipped": skipped, "n_asof_clamped": n_clamped,
           "n_leak_dropped": n_leaked, "n_release_unknown": n_unknown}
    for off in asof_offsets:
        bm = [p[f"brier_model{off}"] for p in per if f"brier_model{off}" in p]
        bk = [p[f"brier_market{off}"] for p in per if f"brier_market{off}" in p]
        agg[f"brier_model{off}"] = round(float(np.mean(bm)), 5) if bm else None
        agg[f"brier_market{off}"] = round(float(np.mean(bk)), 5) if bk else None
        agg[f"n_scored{off}"] = len(bm)
        agg[f"branch_mix{off}"] = dict(sorted(branch_mix[off].items(),
                                              key=lambda kv: -kv[1]))
        # #198. The two mixes above and below have DIFFERENT denominators and the
        # difference is not cosmetic. `branch_mix` counts every event that survived the
        # drops; the Brier in this same dict is a mean over the far smaller subset that
        # also had a market quote at asof — KXWTIW replays 156 settled events and scores
        # 14. So a parity check run against `branch_mix` can pass on 156 events while all
        # 14 that actually produced the gate's number ran a different path. The `_scored`
        # variants are the sample the criteria are computed on, and they are what
        # `eval.run_series` feeds the parity checks. `branch_mix{off}` is kept unchanged
        # because #196's stored rows and its alert history are keyed to it.
        agg[f"branch_mix_scored{off}"] = dict(sorted(scored_branch_mix[off].items(),
                                                     key=lambda kv: -kv[1]))
        agg[f"params_mix{off}"] = dict(sorted(params_mix[off].items(),
                                              key=lambda kv: -kv[1]))
    return {"series": series, "agg": agg, "per_release": per}


def _store_experiment(conn, name: str, series: str, window: str, agg: dict,
                      cfg: dict) -> None:
    # ISO week in the hash → one row per (series, week) ACCUMULATES instead of
    # replacing forever — health's 2-window/CRPS-spike detectors need history
    wk = datetime.now(timezone.utc).strftime("%G-W%V")
    cfg_hash = hashlib.sha1(
        json.dumps({**cfg, "week": wk}, sort_keys=True).encode()).hexdigest()[:12]
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
            # #198 params_pit: these rows are the track record `health`'s 2-window and
            # CRPS-spike detectors watch. Replayed at the defaults they were blind to the
            # one drift that is fully under our control — a parameter adoption that makes
            # the model worse. Verified not to move either detector on the transition:
            # `_detect_brier_2win` is a bm>bk comparison that no series is near, and
            # `_detect_crps_spike` reads `crps-1h`, which this function does not emit.
            rep = replay_series(conn, series, params_pit=True)
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
