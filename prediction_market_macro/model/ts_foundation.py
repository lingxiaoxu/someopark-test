"""model/ts_foundation.py — Chronos-2 zero-shot shadow bridge (PLAN §7 ts_foundation,
§5-bis.3, §7-bis).

Copies the call shape of the house example VIXForecast.py (a context strictly ending at
asof — zero leakage by construction) WITHOUT touching that file or its fine-tuned ckpts.
Zero-shot base model `amazon/chronos-2` from the pre-existing shared HF cache (no new
state outside the macro tree is created; a fine-tune, when it ever happens, checkpoints
INSIDE prediction_market_macro/data/).

SHADOW ONLY (§7-bis stage 1): predictions land in preds with model_version
"chronos2/zero-shot-0.2.0" (and "chronos2/vol-bridge-0.1.0" for the width-only variant)
and are excluded from decisions by the production-model guard in ops/decide_all
(model_version must match the registry's model binding). Adoption gate: live-forward
shadow window (weekly series >= 8 periods, monthly >= 3) beating BOTH the stat model and
the market baseline on Brier/CRPS — pretraining has seen historical macro series, so
backtest evidence is contaminated and inadmissible.

PIT: context and covariates come exclusively from reads with knowledge_time <= asof;
data_horizon is stamped from the reads like every other model.

────────────────────────────────────────────────────────────────────────────────
0.2.0 — four measured defects in 0.1.0, all fixed here (review 2026-08-19, n=3 settled
events per weekly series; see research/ts_replay.py for the day-by-day PIT rerun):

1. DISTRIBUTION ENCODING.  0.1.0 asked for 99 quantile levels and handed the resulting
   vector to `Empirical` as if it were 99 SAMPLES. Two things were wrong. Chronos-2 is
   trained on 21 levels (0.01, 0.05, 0.10 ... 0.95, 0.99), so 78 of the 99 were linear
   interpolation carrying no information; and 99 atoms on a $0.01 settlement grid made
   grid_pmf a COMB — 99 grid points across a $39 support, versus 3086 for energy/0.6.0 —
   with hard zeros outside [q0.01, q0.99]. Measured consequence: 35% of WTI legs and 44%
   of NG legs were priced at exactly 0.0 or 1.0. Now: request the 21 TRAINED levels only,
   build a monotone quantile function, extrapolate both tails with a matched exponential
   (the xi->0 GPD limit, fatter than Gaussian and unbounded), and evaluate it on a
   stratified 20k grid — same resolution convention as energy/0.6.0's bootstrap, but with
   zero Monte-Carlo noise, so replay is bit-stable without an rng seed.

2. AAA WAS FORECASTING THE WRONG QUANTITY.  KXAAAGASW settles on AAA's daily national
   average; 0.1.0 forecast the WEEKLY EIA proxy GASREGW and quoted its level directly.
   Measured over the three settled periods: mean |error| 7.7c for chronos vs 4.4c for
   last-value-carried-forward — the model was worse than doing nothing, because it put a
   drift on a series that is nearly a random walk, and the legs are 1c apart. energy/0.6.0
   wins by 5x purely because it anchors on the AAA_DAILY reading (the settle number
   itself). Now: chronos forecasts the GASREGW PATH, and only its CHANGE is transported
   onto the AAA_DAILY anchor, time-scaled to the days actually remaining (drift linear,
   dispersion sqrt) — so the level comes from the settle source and only the increment
   comes from the model.

3. CLAIMS READ THE WRONG VINTAGE.  KXJOBLESSCLAIMS settles on the DoL ADVANCE (first)
   print; 0.1.0 read `fred_series` = latest visible vintage. ICSA revisions are almost
   always upward and round_rule is 250, so the context was systematically off-grid
   relative to the label. Now: first-print reads.

4. THE MODEL WAS BEING USED AS A UNIVARIATE BLACK BOX.  0.1.0 passed a bare 260-point
   ndarray. Chronos-2 supports 8192 of context, past covariates, and KNOWN-FUTURE
   covariates. Now: per-series context of 800-1500, the already-ingested drivers as past
   covariates, and deterministic seasonality plus the CPC 6-14 day temperature outlook as
   future covariates. See model/ts_covariates.py for the BAU data-dependency table.

Additionally 0.2.0 adds `predict_vol_bridge` (chronos2/vol-bridge-0.1.0): for WTI and NG,
forecasting the LEVEL of a liquid front-month future is unwinnable against a market that
is priced off that very future. The bridge keeps only the SHAPE of the chronos forecast,
re-centres it on the last close, and thereby contributes a volatility view instead of a
price view — the same "use it for what it can add" pattern as model/bridge.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from prediction_market_macro.model import ts_covariates as tc
from prediction_market_macro.model.common import Empirical, Pred, grid_pmf, pred_to_row

VERSION = "chronos2/zero-shot-0.2.0"
VOL_VERSION = "chronos2/vol-bridge-0.1.0"
MODEL_ID = "amazon/chronos-2"

# EXACTLY the levels chronos-2 is trained on (config.json:chronos_config.quantiles).
# Asking for anything else routes through interpolate_quantiles and fabricates points;
# asking for anything OUTSIDE this range is silently clamped to the end levels.
_QLEVELS = [0.01, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50,
            0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.99]
_N_SAMPLES = 20_000               # matches energy/0.6.0's encoding resolution
_MIN_CTX = 60                     # below this the context is not worth a foundation model
_AAA_FRESH_DAYS = 3               # same freshness rule as energy/0.6.0's daily anchor


@dataclass(frozen=True)
class _Target:
    kind: str                     # "fut" (fut_daily root) | "fred" (fred_obs sid)
    key: str
    step: str                     # "bday" | "week" — one forecast step
    context: int                  # bars/weeks of context to feed
    first_print: bool = False     # read the FIRST vintage (print-anchored settlement)
    anchor: str | None = None     # transport the forecast CHANGE onto this daily series
    past: tuple[tuple, ...] = ()  # (name, source-kind, key) past covariates
    future_cal: tuple[str, ...] = ()   # calendar features also passed as future covariates
    use_outlook: bool = False     # CPC 6-14d temperature outlook as a future covariate


# Past covariates are drivers the daily refresh already maintains (see ts_covariates).
# Note on "term structure": fut_daily carries ONE front-month continuous series per root,
# so an oil term structure is not available. The economically closest substitutes that ARE
# available are the crack complex (RB vs CL) and the fuel-substitution pair (CL vs NG),
# which is what is fed instead. Stating this rather than implying a curve exists.
_TARGETS: dict[str, _Target] = {
    "KXWTIW": _Target(
        kind="fut", key="CL", step="bday", context=1500,
        past=(("ng", "fut", "NG"), ("rb", "fut", "RB"), ("gold", "fut", "GC"),
              ("dgs10", "fred", "DGS10"), ("t5yie", "fred", "T5YIE"),
              ("crude_stocks", "fred", "CRUDE_STOCKS_WEEKLY"),
              ("gasoline_stocks", "fred", "GASOLINE_STOCKS_WEEKLY")),
        future_cal=("doy_sin", "doy_cos", "dow")),
    "KXNATGASW": _Target(
        kind="fut", key="NG", step="bday", context=1500,
        past=(("crude", "fut", "CL"), ("ng_storage", "fred", "NG_STORAGE_WEEKLY"),
              ("dgs10", "fred", "DGS10")),
        future_cal=("doy_sin", "doy_cos", "dow"), use_outlook=True),
    "KXAAAGASW": _Target(
        kind="fred", key="GASREGW", step="week", context=800, anchor="AAA_DAILY",
        past=(("rb", "fut", "RB"), ("crude", "fut", "CL"),
              ("gasoline_stocks", "fred", "GASOLINE_STOCKS_WEEKLY"),
              ("crude_stocks", "fred", "CRUDE_STOCKS_WEEKLY")),
        future_cal=("doy_sin", "doy_cos", "woy_sin", "woy_cos")),
    "KXJOBLESSCLAIMS": _Target(
        kind="fred", key="ICSA", step="week", context=1000, first_print=True,
        past=(("unrate", "fred", "UNRATE"), ("payrolls", "fred", "PAYEMS"),
              ("dgs10", "fred", "DGS10")),
        future_cal=("woy_sin", "woy_cos", "doy_sin", "doy_cos", "holiday_week")),
}

_PIPELINE = None


def _pipeline():
    global _PIPELINE
    if _PIPELINE is None:
        import torch
        from chronos import BaseChronosPipeline
        _PIPELINE = BaseChronosPipeline.from_pretrained(
            MODEL_ID, device_map="cpu", dtype=torch.float32)
    return _PIPELINE


# ── P0: quantile function -> samples ─────────────────────────────────────────
def samples_from_quantiles(levels, values, n: int = _N_SAMPLES) -> np.ndarray:
    """Turn a quantile forecast into `n` samples with unbounded, non-degenerate support.

    `values[i]` is the model's estimate of the `levels[i]` quantile. Between the end
    levels the quantile function is taken to be piecewise linear (a piecewise-uniform
    density — the least-committal reading of a quantile vector). BEYOND them it is
    extrapolated with a matched exponential:

        u < l0 :  F^-1(u) = v0 - b_lo * ln(l0 / u)          b_lo = (v1-v0)/ln(l1/l0)
        u > lN :  F^-1(u) = vN + b_hi * ln((1-lN)/(1-u))    b_hi = (vN-vN')/ln((1-lN')/(1-lN))

    which is the xi->0 limit of a generalized Pareto tail: continuous in value at the
    join, monotone, unbounded, and with a heavier tail than a Gaussian fitted to the same
    two points — the right default for a price. b is taken from the OUTERMOST INTERVAL of
    the model's own output, so the tail decay is the model's, not an assumption.

    Sampling is STRATIFIED (u_i = (i+0.5)/n) rather than random: the result is exact to
    O(1/n) with zero Monte-Carlo noise and is bit-identical on replay without carrying an
    rng seed. That matters because these samples feed grid_pmf on a $0.001 grid, where
    sampling noise would show up directly as leg-price noise.
    """
    lv = np.asarray(levels, dtype=float)
    vv = np.asarray(values, dtype=float)
    if lv.shape != vv.shape or lv.ndim != 1 or len(lv) < 4:
        raise ValueError(f"quantile shape mismatch: {lv.shape} vs {vv.shape}")
    order = np.argsort(lv)
    lv, vv = lv[order], vv[order]
    if not np.all(np.isfinite(vv)):
        raise ValueError("non-finite quantile values")
    # crossing repair: a quantile function must be non-decreasing
    vv = np.maximum.accumulate(vv)

    u = (np.arange(n, dtype=float) + 0.5) / n
    out = np.interp(u, lv, vv)

    span = float(vv[-1] - vv[0])
    if span <= 0:
        # fully degenerate forecast (all quantiles equal) — refuse rather than emit a
        # point mass that grid_pmf would turn into a 100% leg.
        raise ValueError("degenerate quantile forecast (zero spread)")

    lo_gap, hi_gap = float(vv[1] - vv[0]), float(vv[-1] - vv[-2])
    # a zero end-interval would kill the tail; fall back to the average interval width
    mean_gap = span / (len(vv) - 1)
    b_lo = max(lo_gap, mean_gap * 1e-3) / math.log(lv[1] / lv[0])
    b_hi = max(hi_gap, mean_gap * 1e-3) / math.log((1 - lv[-2]) / (1 - lv[-1]))

    lo = u < lv[0]
    hi = u > lv[-1]
    # NOTE the direction: for u < l0 the ratio u/l0 is < 1, so ln is NEGATIVE and the
    # term must be ADDED to push the tail below v0. Writing it as -b*ln(u/l0) bends the
    # left tail the wrong way, and the maximum.accumulate below then flattens it into a
    # point mass — a worse degeneracy than the one this function exists to remove.
    out[lo] = vv[0] + b_lo * np.log(u[lo] / lv[0])
    out[hi] = vv[-1] + b_hi * np.log((1 - lv[-1]) / (1 - u[hi]))

    out = np.maximum.accumulate(out)
    if not np.all(np.isfinite(out)):
        raise ValueError("non-finite samples after tail extrapolation")
    return out


# ── context + covariate assembly ─────────────────────────────────────────────
def _target_series(conn, series: str, asof: datetime) -> tuple[pd.Series, str | None]:
    t = _TARGETS[series]
    if t.kind == "fut":
        s, h = tc.fut_closes(conn, t.key, asof, n=max(t.context, 300))
    else:
        s, h = tc.fred(conn, t.key, asof, first_print=t.first_print)
    s = s.dropna()
    if len(s) > t.context:
        s = s.tail(t.context)
    return s, h


def _steps_to(period: str, last: pd.Timestamp, step: str) -> int:
    target = pd.Timestamp(period)
    if target <= last:
        return 1
    if step == "bday":
        return max(int(np.busday_count(last.date(), target.date())), 1)
    return max(math.ceil((target - last).days / 7.0), 1)


def _build_task(conn, series: str, asof: datetime) -> tuple[dict, pd.Series, list[str], dict]:
    """Assemble the chronos-2 task dict: target + past_covariates + future_covariates.

    Returns (task-without-future-covariates, target series, knowledge horizons, meta).
    Future covariates depend on h, which the caller resolves from the period, so they are
    attached in _forecast once h is known.
    """
    t = _TARGETS[series]
    s, h_target = _target_series(conn, series, asof)
    if len(s) < _MIN_CTX or h_target is None:
        raise RuntimeError(f"{series}: context too short (n={len(s)})")
    idx = pd.DatetimeIndex(s.index)
    horizons = [h_target]
    past: dict[str, np.ndarray] = {}
    dropped: list[str] = []

    for name, kind, key in t.past:
        if kind == "fut":
            # bound by DATE, not bar count: on a weekly target the index spans decades and
            # a bar-count lookback would cover <50% and get the covariate dropped.
            src, hz = tc.fut_closes(conn, key, asof, since=idx[0])
        else:
            src, hz = tc.fred(conn, key, asof)
        arr = tc.align(src, idx)
        if arr is None:
            dropped.append(name)
            continue
        past[name] = arr
        if hz:
            horizons.append(hz)

    cal = tc.calendar_features(idx)
    for name in t.future_cal:
        past[name] = cal[name]

    if t.use_outlook:
        ol, hz = tc.outlook_index(conn, asof)
        arr = tc.align(ol[ol.index <= idx[-1]], idx) if len(ol) else None
        if arr is None:
            dropped.append("outlook")
        else:
            past["outlook"] = arr
            if hz:
                horizons.append(hz)

    task = {"target": s.to_numpy(dtype=float), "past_covariates": past}
    meta = {"ctx_len": len(s), "n_past_cov": len(past), "dropped_cov": dropped,
            "last_obs_date": str(idx[-1].date())}
    return task, s, horizons, meta


def _attach_future(conn, series: str, asof: datetime, task: dict,
                   last: pd.Timestamp, h: int) -> None:
    """Known-future covariates for the h steps ahead. Every key here must already exist
    in past_covariates (chronos-2 requires future ⊆ past)."""
    t = _TARGETS[series]
    fidx = tc.future_index(last, h, t.step)
    cal = tc.calendar_features(fidx)
    fut: dict[str, np.ndarray] = {}
    for name in t.future_cal:
        if name in task["past_covariates"]:
            fut[name] = cal[name]
    if t.use_outlook and "outlook" in task["past_covariates"]:
        ol, _ = tc.outlook_index(conn, asof)
        arr = tc.align_future(ol, fidx)
        if arr is not None:
            fut["outlook"] = arr
    if fut:
        task["future_covariates"] = fut


def _forecast_quantiles(conn, series: str, asof: datetime, period: str
                        ) -> tuple[np.ndarray, pd.Series, list[str], dict, int]:
    """Terminal-step quantile vector on the model's own 21 trained levels."""
    task, s, horizons, meta = _build_task(conn, series, asof)
    last = pd.Timestamp(s.index[-1])
    h = _steps_to(period, last, _TARGETS[series].step)
    _attach_future(conn, series, asof, task, last, h)
    meta["h_steps"] = h
    meta["n_future_cov"] = len(task.get("future_covariates", {}))
    q_list, _ = _pipeline().predict_quantiles(
        [task], prediction_length=h, quantile_levels=_QLEVELS)
    q = np.asarray(q_list[0])                      # (n_variates, h, n_levels)
    terminal = np.asarray(q[0, h - 1, :], dtype=float)
    return terminal, s, horizons, meta, h


# ── public API ───────────────────────────────────────────────────────────────
def predict(conn, asof: datetime, period: str, series: str) -> Pred:
    if series not in _TARGETS:
        raise RuntimeError(f"{series}: not covered by ts_foundation")
    terminal, s, horizons, meta, h = _forecast_quantiles(conn, series, asof, period)
    samples = samples_from_quantiles(_QLEVELS, terminal)
    t = _TARGETS[series]
    mode = "level"
    last_obs = float(s.iloc[-1])

    if t.anchor:
        samples, anchor_meta, mode = _transport_to_anchor(
            conn, asof, period, s, samples, t.anchor, horizons)
        meta.update(anchor_meta)

    dist = Empirical(tuple(samples.tolist()))
    median = float(np.median(samples))
    inputs = {"model_id": MODEL_ID, "mode": mode, "last_obs": round(last_obs, 6),
              "median": round(median, 6),
              "q05": round(float(np.quantile(samples, 0.05)), 6),
              "q95": round(float(np.quantile(samples, 0.95)), 6),
              "n_samples": len(samples), **meta}
    return Pred(series=series, period=period, dist=dist, asof=asof,
                model_version=VERSION, inputs=inputs,
                data_horizon=datetime.fromisoformat(max(h for h in horizons if h)))


def _transport_to_anchor(conn, asof: datetime, period: str, s: pd.Series,
                         samples: np.ndarray, anchor_sid: str, horizons: list[str]
                         ) -> tuple[np.ndarray, dict, str]:
    """Move a proxy-series forecast onto the series that actually settles.

    KXAAAGASW settles on AAA's daily national average. GASREGW (weekly EIA retail) tracks
    it closely in LEVEL but is published once a week, so quoting the GASREGW forecast
    directly throws away every daily AAA reading since. What the model can contribute is
    the CHANGE; the level must come from the settle source.

    Let g = last GASREGW observation (date g_t), a = last AAA_DAILY reading (date a_t,
    required fresh), and D = days from g_t to settle, d = days from a_t to settle. Then

        drift      m - g        over D days   ->   (d/D) * (m - g)      linear in time
        dispersion q(u) - m     over D days   ->   sqrt(d/D) * (q(u)-m) diffusive in time

    so the transported sample is  a + (d/D)*(m-g) + sqrt(d/D)*(q(u)-m).

    When the anchor is missing or stale the forecast falls back to the raw proxy level and
    says so in `mode`; ops/decide_all._aaa_information_gate (§27.4) refuses to trade any
    KXAAAGASW pred whose mode is not "aaa_daily_anchor", and that refusal is CORRECT for
    this branch too — the information disadvantage it exists to catch is exactly the same.
    """
    a, h_a = tc.fred(conn, anchor_sid, asof)
    a = a.dropna()
    settle = pd.Timestamp(period)
    g_t = pd.Timestamp(s.index[-1])
    if len(a) == 0 or (asof.date() - a.index[-1].date()).days > _AAA_FRESH_DAYS:
        age = None if len(a) == 0 else (asof.date() - a.index[-1].date()).days
        return samples, {"anchor": None, "anchor_age_d": age}, "proxy_level"

    a_t = pd.Timestamp(a.index[-1])
    a_last = float(a.iloc[-1])
    g_last = float(s.iloc[-1])
    D = max((settle - g_t).days, 1)
    d = max((settle - a_t).days, 0)
    frac = min(max(d / D, 0.0), 1.0)

    m = float(np.median(samples))
    moved = a_last + frac * (m - g_last) + math.sqrt(frac) * (samples - m)
    if h_a:
        horizons.append(h_a)
    meta = {"anchor": anchor_sid, "anchor_value": round(a_last, 4),
            "anchor_date": str(a_t.date()), "proxy_last": round(g_last, 4),
            "proxy_median": round(m, 4), "days_left": d, "days_total": D,
            "time_frac": round(frac, 4)}
    return moved, meta, "aaa_daily_anchor"


def predict_vol_bridge(conn, asof: datetime, period: str, series: str) -> Pred:
    """Width-only variant for the liquid futures series (KXWTIW / KXNATGASW).

    Forecasting the LEVEL of a front-month future against a market that is quoted off that
    same future is a coin-flip by construction — measured at close-1h, chronos and the
    Kalshi mid land within 0.0014 Brier of each other on WTI, which is the tie you would
    expect from two people reading the same screen. What a foundation model can still
    contribute is a view on the WIDTH of the terminal distribution.

    So: keep chronos's SHAPE in log space, discard its drift, and re-centre on the last
    close. The result is a driftless distribution whose dispersion is the model's, which
    is the same division of labour model/bridge.py uses (independent signal, production
    anchoring).
    """
    if series not in ("KXWTIW", "KXNATGASW"):
        raise RuntimeError(f"{series}: vol bridge covers WTI/NG only")
    terminal, s, horizons, meta, h = _forecast_quantiles(conn, series, asof, period)
    samples = samples_from_quantiles(_QLEVELS, terminal)
    last = float(s.iloc[-1])
    m = float(np.median(samples))
    if m <= 0 or last <= 0 or np.any(samples <= 0):
        # log-space re-centring is undefined for non-positive prices; use the additive
        # equivalent rather than silently dropping the series.
        moved = last + (samples - m)
        space = "additive"
    else:
        moved = last * np.exp(np.log(samples) - math.log(m))
        space = "log"
    sig = float(np.std(np.log(moved / last))) if space == "log" else float(np.std(moved))
    dist = Empirical(tuple(moved.tolist()))
    inputs = {"model_id": MODEL_ID, "mode": "vol_bridge", "recenter_space": space,
              "anchor_close": round(last, 6), "chronos_median": round(m, 6),
              "drift_discarded": round(m - last, 6),
              "sigma_terminal": round(sig, 6),
              "q05": round(float(np.quantile(moved, 0.05)), 6),
              "q95": round(float(np.quantile(moved, 0.95)), 6),
              "n_samples": len(moved), **meta}
    return Pred(series=series, period=period, dist=dist, asof=asof,
                model_version=VOL_VERSION, inputs=inputs,
                data_horizon=datetime.fromisoformat(max(x for x in horizons if x)))


def shadow_run(conn, settings) -> int:
    """Write shadow preds for every open period of the covered series. Degradable:
    a per-series failure alerts and moves on; decisions never wait on this."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.util.periods import kalshi_period_to_key
    now = datetime.now(timezone.utc)
    n = 0
    for series in _TARGETS:
        spec = REGISTRY[series]
        rows = conn.execute(
            "SELECT DISTINCT period FROM contracts WHERE series=? AND status='active'",
            (series,)).fetchall()
        for r in rows:
            key = kalshi_period_to_key(r["period"])
            if not key:
                continue
            variants = [(VERSION, predict)]
            if series in ("KXWTIW", "KXNATGASW"):
                variants.append((VOL_VERSION, predict_vol_bridge))
            for _mv, fn in variants:
                try:
                    pred = fn(conn, now, key, series)
                    pmf = grid_pmf(pred.dist, spec.round_rule)
                    ladder = {str(k): round(v, 6) for k, v in pmf.items()}
                    conn.execute(
                        "INSERT OR REPLACE INTO preds(series, period, asof, model_version,"
                        " dist_json, ladder_json, inputs_json, data_horizon, created_ts)"
                        " VALUES(?,?,?,?,?,?,?,?,?)", pred_to_row(pred, ladder))
                    n += 1
                except Exception as e:                           # noqa: BLE001
                    conn.execute(
                        "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                        (now.isoformat(), "warn", "chronos_shadow",
                         f"{series}/{key}/{_mv}: {e}"))
    conn.commit()
    return n
