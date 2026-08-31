"""model/ercot_cov.py — the ERCOT/EIA-930 covariate, PIT walk-forward (PR-31).

Screening (docs/ERCOT_NOTES.md) found no raw signal; the user ordered the full
production judgment anyway: both arms through the leak-free replay, PIT, walk-forward,
every existing input kept, the covariate ADDED behind a parameter gate. This module is
that covariate. `ercot_w = 0` (the default everywhere) is bit-identical to production.

PIT declarations, in the open:
  * EIA-930 day-D values are treated as knowable at D+2 00:00 UTC. The backfilled rows
    carry fetch-time knowledge_time; 930 publishes next-day with small revisions, so
    D+2 admits knowing it LATER than the world did — conservative in the safe
    direction, the same argument cleveland_nowcast registered for its 18:00 stamp.
  * The climatology (per day-of-year, ±7d window) uses PRIOR CALENDAR YEARS only.
  * The regression weight is fit at each asof on weeks that END fully before that
    asof — an expanding window that sees one more week per week, never the future.
    Below `_MIN_OBS` pairs the weight is 0 and the covariate is silent.

Signals per market family (from the screening's mechanism map):
  NG/CL (KXNATGASW/KXWTIW): gas-burn climatology z, week to date (lag applied)
  ICSA (KXJOBLESSCLAIMS):   |demand climatology z| — weather-severity
  CPI (headline family):    monthly mean burn z
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

_LAG_DAYS = 2
_MIN_OBS = 52          # weekly pairs (12 for the monthly CPI shape)
_SHIFT_CLIP = 1.5      # |mu shift| <= clip x fitted residual sd of the target

_cache: dict = {}


def _daily(conn, metric: str) -> pd.Series:
    key = ("daily", metric)
    if key not in _cache:
        rows = conn.execute(
            "SELECT date, value FROM ercot_daily WHERE metric=? ORDER BY date",
            (metric,)).fetchall()
        _cache[key] = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows})
    return _cache[key]


def _clim_z(conn, metric: str) -> pd.Series:
    """Full climatology-z series (prior-years-only, ±7d day-of-year window). The series
    itself is PIT row by row, so slicing it at an asof is PIT too — cached once."""
    key = ("climz", metric)
    if key not in _cache:
        s = _daily(conn, metric)
        doy = s.index.dayofyear.values
        yr = s.index.year.values
        v = s.values
        z = np.full(len(s), np.nan)
        for i in range(len(s)):
            m = (yr < yr[i]) & (np.abs(((doy - doy[i] + 182) % 365) - 182) <= 7)
            if m.sum() >= 20:
                sd = v[m].std()
                if sd > 0:
                    z[i] = (v[i] - v[m].mean()) / sd
        _cache[key] = pd.Series(z, index=s.index).dropna()
    return _cache[key]


def _visible(z: pd.Series, asof: datetime) -> pd.Series:
    cut = pd.Timestamp(asof.date()) - pd.Timedelta(days=_LAG_DAYS)
    return z[z.index <= cut]


def _fut_weekly(conn, root: str, asof: datetime) -> pd.Series:
    rows = conn.execute(
        "SELECT event_time, close FROM fut_daily WHERE root=? AND close IS NOT NULL"
        " AND event_time < ? ORDER BY event_time", (root, asof.date().isoformat())
    ).fetchall()
    s = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows})
    return np.log(s.resample("W-FRI").last().dropna()).diff().dropna()


def _icsa_weekly(conn, asof: datetime) -> pd.Series:
    rows = conn.execute(
        "SELECT event_time, value FROM fred_obs WHERE sid='ICSA' AND knowledge_time<=?"
        " ORDER BY event_time, vintage_date", (asof.isoformat(),)).fetchall()
    s = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows})
    return np.log(s[~s.index.duplicated(keep="last")].sort_index()).diff().dropna()


def _beta_shift(sig_w: pd.Series, tgt_w: pd.Series, asof: datetime,
                current_sig: float, min_obs: int) -> float:
    """Expanding-window OLS through the origin on pairs strictly before asof."""
    cut = pd.Timestamp(asof.date())
    d = pd.concat([sig_w.shift(0).rename("s"), tgt_w.rename("t")], axis=1).dropna()
    d = d[d.index < cut]
    if len(d) < min_obs or not math.isfinite(current_sig):
        return 0.0
    s, t = d["s"].values, d["t"].values
    denom = float((s * s).sum())
    if denom <= 0:
        return 0.0
    beta = float((s * t).sum()) / denom
    resid_sd = float(np.std(t - beta * s))
    shift = beta * current_sig
    clip = _SHIFT_CLIP * resid_sd
    return float(np.clip(shift, -clip, clip))


def mu_shift(conn, asof: datetime, series: str) -> float:
    """The covariate's additive shift for `series` at `asof`, in the model's own units:
    log-return units for NG/CL and ICSA, percentage points for CPI MoM. 0 whenever the
    walk-forward window is short, the signal is absent, or anything errs — the covariate
    must never be the reason a prediction fails."""
    try:
        if series in ("KXNATGASW", "KXWTIW"):
            root = "NG" if series == "KXNATGASW" else "CL"
            z = _visible(_clim_z(conn, "eia_gas_gen_mwh"), asof)
            if not len(z):
                return 0.0
            zw = z.resample("W-FRI").mean().dropna()
            tgt = _fut_weekly(conn, root, asof)
            cur_week = pd.Timestamp(asof.date()) + pd.offsets.Week(weekday=4)
            cur = z[z.index > cur_week - pd.Timedelta(days=7)]
            cur_sig = float(cur.mean()) if len(cur) else float("nan")
            return _beta_shift(zw, tgt, asof, cur_sig, _MIN_OBS)
        if series == "KXJOBLESSCLAIMS":
            z = _visible(_clim_z(conn, "eia_demand_mwh"), asof).abs()
            if not len(z):
                return 0.0
            zw = z.resample("W-FRI").mean().dropna()
            tgt = _icsa_weekly(conn, asof)
            tgt.index = tgt.index + pd.offsets.Week(weekday=4)   # print week -> W-FRI
            cur = z.tail(5)
            cur_sig = float(cur.mean()) if len(cur) else float("nan")
            return _beta_shift(zw, tgt, asof, cur_sig, _MIN_OBS)
        if series in ("KXCPI", "KXCPIYOY"):
            z = _visible(_clim_z(conn, "eia_gas_gen_mwh"), asof)
            if not len(z):
                return 0.0
            zm = z.resample("MS").mean().dropna()
            rows = conn.execute(
                "SELECT event_time, value FROM fred_obs WHERE sid='CPIAUCSL'"
                " AND knowledge_time<=? ORDER BY event_time, vintage_date",
                (asof.isoformat(),)).fetchall()
            cpi = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows})
            cpi = cpi[~cpi.index.duplicated(keep="last")].sort_index()
            tgt = (np.log(cpi).diff() * 100).dropna()
            cur = zm.iloc[-1] if len(zm) else float("nan")
            return _beta_shift(zm, tgt, asof, float(cur), 12)
    except Exception:                                            # noqa: BLE001
        return 0.0
    return 0.0


def clear_cache() -> None:
    _cache.clear()
