"""model/ensemble.py — adaptive log-pool of model + market + bridge (PLAN §19-2,
PLAN_EXTENSION §23.2-1: Bates-Granger inverse-MSPE weights + trimmed-mean guard).

Sources per (series, period), all read from what refresh already stores:
  model   the registry-bound production model's ladder pmf (preds)
  market  the devigged ladder implied from the latest quotes (strategy/devig)
  bridge  model/bridge.py shadow pred where supported

Weights: w_i ∝ 1/MSPE_i from the trailing TRAIL_N per-event Brier rows in
source_scores (research/eval writes them weekly), floored/ceilinged so no source ever
dies or dominates; uniform-prior fallback when history is thin. Trimmed guard: a source
whose trailing Brier is catastrophically worse than the best (×3) is dropped from the
pool for that series rather than averaged in.

§7-bis: SHADOW ONLY — shadow_run() writes preds with model_version='ensemble/0.1.0';
the production guard in decide_all keeps them out of live decisions until the adoption
gate (research/eval) promotes the member. 铁律 13.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import numpy as np

from prediction_market_macro.model.common import Empirical, Pred

VERSION = "ensemble/0.1.0"
N_STUB_SAMPLES = 4000
TRAIL_N = 60
W_FLOOR, W_CEIL = 0.10, 0.70
TRIM_RATIO = 3.0
PRIOR = {"model": 0.35, "market": 0.50, "bridge": 0.15}


MEMBERS = tuple(PRIOR)          # the pool's members — the only sources that may be weighted


def learn_weights(conn, series: str, offset: str = "-1h") -> dict[str, float]:
    """Inverse-MSPE weights from trailing source_scores; PRIOR fallback.

    Restricted to `MEMBERS`. `source_scores` is a general scoreboard, not this pool's
    membership list: it also holds `pooled` — written by `eval.run_series`, and a
    DETERMINISTIC FUNCTION OF model+market ON THE SAME EVENTS — and `chronos`, a §7-bis
    shadow member. Without this filter both entered the weights, the floor/ceiling clip,
    and (worse) set `best` for the trimmed-mean guard, so a shadow member decided how
    harshly real members were judged. On KXAAAGASW that produced
    `{'pooled': 0.49, 'market': 0.51}` — half the weight on a derivative of the pool
    itself, recorded into every `inputs_json["weights"]`.

    It did not misfire on the pmf: `log_pool` keeps only sources it actually has a pmf
    for and renormalizes, which preserves the ratios among the survivors, and no weight
    currently reaches the 0.10/0.70 clips. Checked all four series with learned weights —
    members-only trimming keeps exactly the same real members. So this is a latent fix,
    and the guard was load-bearing only for the day a clip binds.
    """
    rows = conn.execute(
        "SELECT source, brier FROM source_scores WHERE series=? AND offset=?"
        " ORDER BY period DESC LIMIT ?", (series, offset, TRAIL_N * 3)).fetchall()
    per: dict[str, list[float]] = {}
    for r in rows:
        if r["brier"] is not None and r["source"] in MEMBERS:
            per.setdefault(r["source"], []).append(float(r["brier"]))
    per = {k: v[:TRAIL_N] for k, v in per.items() if len(v) >= 6}
    if len(per) < 2:
        return dict(PRIOR)
    mspe = {k: max(sum(v) / len(v), 1e-6) for k, v in per.items()}
    best = min(mspe.values())
    kept = {k: m for k, m in mspe.items() if m <= best * TRIM_RATIO}   # trimmed guard
    raw = {k: 1.0 / m for k, m in kept.items()}
    tot = sum(raw.values())
    w = {k: v / tot for k, v in raw.items()}
    # floor/ceiling then renormalize
    w = {k: min(max(v, W_FLOOR), W_CEIL) for k, v in w.items()}
    tot = sum(w.values())
    return {k: round(v / tot, 4) for k, v in w.items()}


def finite_pmf(pmf: dict[float, float]) -> dict[float, float]:
    """Drop non-finite keys (devig emits an `inf` bucket for mass above the top
    strike) and renormalize — an inf grid point poisons quantiles/means downstream."""
    out = {k: v for k, v in pmf.items() if math.isfinite(k) and math.isfinite(v)}
    z = sum(out.values())
    return {k: v / z for k, v in out.items()} if z > 0 else {}


def ladder_samples(pmf: dict[float, float],
                   n: int = N_STUB_SAMPLES) -> tuple[float, ...]:
    """Encode a ladder pmf as exactly `n` samples for the Empirical stub dist.

    The previous encoder replicated each grid point `round(p*2000)` times, FLOORED AT 1
    so no point could vanish, then truncated the concatenation with `vals[:4000]`. Both
    halves are load-bearing and together they were a silent, order-dependent corruption:
    the floor makes the list at least as long as the grid, and `sorted()` emits the LOW
    grid points first, so any pooled ladder with more than ~4000 grid points kept only
    its bottom tail and threw the entire upper half of the distribution away.

    Measured on the 2026-08-18 preds before the fix:

        KXPAYROLLS  8739 grid pts  ladder mean  +84,376  ->  stub mean -1,886,973
                                   ladder span  [-3.88M, +4.85M] -> stub [-3.88M, -8k]
        KXWTIW      3088 grid pts  ladder span  [50, 115]        -> stub [50, 98]

    i.e. the encoder alone moved the payrolls forecast by two million jobs, and every
    "greater than zero" leg priced at exactly 0.0 against a market at 0.75.

    Stratified u_i=(i+0.5)/n through the inverse CDF instead of replicate-and-truncate:
    exactly n samples whatever the grid size, no order dependence, and bit-stable across
    processes without carrying an rng seed.
    """
    ks = sorted(k for k, v in pmf.items() if math.isfinite(k) and v > 0)
    if not ks:
        raise ValueError("ladder_samples: pmf has no finite positive-mass point")
    w = np.array([pmf[k] for k in ks], dtype=float)
    cdf = np.cumsum(w / w.sum())
    u = (np.arange(n) + 0.5) / n
    idx = np.searchsorted(cdf, u, side="left").clip(0, len(ks) - 1)
    return tuple(float(ks[i]) for i in idx)


def log_pool(pmfs: dict[str, dict[float, float]], weights: dict[str, float],
             eps: float = 1e-6) -> dict[float, float]:
    """Weighted geometric pool over the UNION grid, renormalized."""
    pmfs = {s: finite_pmf(p) for s, p in pmfs.items()}
    pmfs = {s: p for s, p in pmfs.items() if p}
    keys = sorted({k for p in pmfs.values() for k in p})
    used = {s: w for s, w in weights.items() if s in pmfs and w > 0}
    tot_w = sum(used.values())
    out = {}
    for k in keys:
        lp = 0.0
        for s, w in used.items():
            lp += (w / tot_w) * math.log(max(pmfs[s].get(k, 0.0), eps))
        out[k] = math.exp(lp)
    z = sum(out.values())
    return {k: v / z for k, v in out.items()}


def _model_pmf(conn, series: str, key: str, spec) -> tuple[dict, str] | None:
    r = conn.execute(
        "SELECT ladder_json, data_horizon FROM preds WHERE series=? AND period=?"
        " AND model_version LIKE ? ORDER BY asof DESC LIMIT 1",
        (series, key, spec.model + "/%")).fetchone()
    if r is None or not r["ladder_json"]:
        return None
    return ({float(k): v for k, v in json.loads(r["ladder_json"]).items()},
            r["data_horizon"])


def _bridge_pmf(conn, series: str, key: str) -> dict | None:
    """Newest bridge pred for this period, pinned to the CURRENT bridge version.

    Pinned rather than `LIKE 'bridge/%'` because predict() raises instead of emitting
    when a fit is not available, and shadow_run swallows that. With a wildcard match,
    a superseded version's row stays the newest one forever and keeps feeding the pool
    — which is exactly how bridge/0.1.0's mis-calibrated KXPAYROLLS ladder would have
    survived its own replacement.
    """
    from prediction_market_macro.model.bridge import VERSION as BRIDGE_VERSION
    r = conn.execute(
        "SELECT ladder_json FROM preds WHERE series=? AND period=?"
        " AND model_version=? ORDER BY asof DESC LIMIT 1",
        (series, key, BRIDGE_VERSION)).fetchone()
    if r is None or not r["ladder_json"]:
        return None
    return {float(k): v for k, v in json.loads(r["ladder_json"]).items()}


def median_spread(legs: list[dict]) -> float | None:
    """Median yes bid-ask spread across quoted legs — the market-noise gauge
    (PLAN_EXTENSION §23.2-3a)."""
    sp = [l["yes_ask"] - l["yes_bid"] for l in legs
          if l.get("yes_ask") is not None and l.get("yes_bid") is not None]
    if not sp:
        return None
    sp.sort()
    return sp[len(sp) // 2]


WIDE_SPREAD = 0.08       # beyond this the devigged market prob is mostly noise


def fl_correct_pmf(pmf: dict[float, float], cal) -> dict[float, float]:
    """Favorite-longshot bias correction (§23.2-3b): recalibrate the market pmf's
    survival curve through the isotonic map fit on (market prob, outcome) history,
    then re-derive the pmf. cal: p -> corrected p (monotone)."""
    ks = sorted(pmf)
    sv = []
    run = 1.0
    for k in ks:
        sv.append(cal(min(1.0, max(0.0, run))))
        run -= pmf[k]
    out = {}
    for i, k in enumerate(ks):
        nxt = sv[i + 1] if i + 1 < len(ks) else cal(0.0)
        out[k] = max(sv[i] - nxt, 0.0)
    z = sum(out.values())
    return {k: v / z for k, v in out.items()} if z > 0 else pmf


def _market_pmf(conn, series: str, tok: str) -> tuple[dict, float | None] | None:
    from prediction_market_macro.ops.decide_all import _legs_meta
    from prediction_market_macro.strategy import devig
    legs = _legs_meta(conn, series, tok)
    if not legs:
        return None
    is_bucket = any(l.get("strike_type") == "between" for l in legs)
    impl = devig.bucket_implied(legs) if is_bucket else devig.ladder_implied(legs)
    pmf = impl.get("pmf")
    if not pmf:
        return None
    pmf = finite_pmf({float(k): v for k, v in pmf.items()})
    if not pmf:
        return None
    # favorite-longshot correction through the stored market calibration map
    from prediction_market_macro.strategy import calibration as _cal
    entry = _cal._load_named(conn, series, "market_calibration_map")
    if entry is not None:
        pmf = fl_correct_pmf(pmf, lambda p: _cal.interp(entry, p))
    return pmf, median_spread(legs)


def shadow_run(conn, settings) -> int:
    """Blend and store shadow ensemble preds for every open ladder (series, period)."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.model.common import pred_to_row
    from prediction_market_macro.ops.predict_all import _open_periods
    now = datetime.now(timezone.utc)
    n = 0
    for spec in REGISTRY.values():
        if spec.structure == "categorical":
            continue                     # fed already log-pools rule×market internally
        for tok, key in _open_periods(conn, spec.ticker):
            try:
                m = _model_pmf(conn, spec.ticker, key, spec)
                if m is None:
                    continue
                model_pmf, horizon = m
                model_pmf = finite_pmf(model_pmf)
                if not model_pmf:
                    continue
                pmfs = {"model": model_pmf}
                spread = None
                mk = _market_pmf(conn, spec.ticker, tok)
                if mk:
                    pmfs["market"], spread = mk
                br = _bridge_pmf(conn, spec.ticker, key)
                if br:
                    br = finite_pmf(br)
                if br:
                    pmfs["bridge"] = br
                if len(pmfs) < 2:
                    continue             # nothing to pool
                w = learn_weights(conn, spec.ticker)
                # §23.2-3a: a wide book means the market prob is mostly noise —
                # halve its weight for this pool and renormalize
                if spread is not None and spread > WIDE_SPREAD and "market" in w:
                    w = dict(w)
                    w["market"] *= 0.5
                    tot = sum(w.values())
                    w = {k: v / tot for k, v in w.items()}
                pooled = log_pool(pmfs, w)
                # encode the pooled pmf; grid values ARE the support (already on the
                # settlement grid) — store as ladder + an Empirical stub dist
                pred = Pred(series=spec.ticker, period=key,
                            dist=Empirical(ladder_samples(pooled)), asof=now,
                            model_version=VERSION,
                            inputs={"weights": w, "sources": sorted(pmfs)},
                            data_horizon=datetime.fromisoformat(horizon))
                ladder = {str(k): round(v, 6) for k, v in pooled.items()}
                conn.execute(
                    "INSERT OR REPLACE INTO preds(series, period, asof, model_version,"
                    " dist_json, ladder_json, inputs_json, data_horizon, created_ts)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", pred_to_row(pred, ladder))
                n += 1
            except Exception:                                # noqa: BLE001
                continue                                      # shadow: silent skip
    conn.commit()
    return n
