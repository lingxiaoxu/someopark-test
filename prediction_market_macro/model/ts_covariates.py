"""model/ts_covariates.py — PIT covariate assembly for the Chronos-2 shadow (PLAN §7-bis).

Chronos-2 accepts `past_covariates` (known only up to the forecast origin) and
`future_covariates` (known THROUGH the forecast horizon). This module turns the tables the
daily refresh already maintains into those two dicts, aligned onto the target's own index.

BAU data dependencies — every source below is already a step in ops/refresh.py, so no new
feed is introduced and nothing here can go stale independently of the production models:

    table            refresh.py step     used by
    ---------------  ------------------  ---------------------------------------------
    fut_daily        "futures"           CL / NG / RB / GC closes  (WTI, NG, AAA)
    fred_obs         "fred_core"         GASREGW, ICSA, DGS10, DCOILWTICO, T5YIE
    fred_obs         "aaa_daily"         AAA_DAILY (the settle number itself)
    fred_obs         "eia_storage"       CRUDE/GASOLINE/NG weekly stocks
    weather_daily    "weather"           per-city HDD/CDD  (NG demand, past covariate)
    weather_fcst     "weather"           CPC 6-10 / 8-14 day outlooks (FUTURE covariate)

PIT: every read filters `knowledge_time <= asof` and returns the max knowledge_time it
consumed, so the caller can stamp `data_horizon` exactly like every other model. A
covariate whose source is empty or too short is DROPPED (never zero-filled) — a silently
zeroed covariate is indistinguishable from a real zero to the model.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

# City set used for the national heating/cooling aggregate. Weighted toward the
# gas-consuming population centres rather than a flat national mean, which is what
# moves Henry Hub. Weights are population-of-metro rounded to one decimal; they are a
# fixed constant, not a fitted parameter.
_GAS_CITY_W = {"CHI": 1.0, "NYC": 1.0, "BOS": 0.6, "DET": 0.5, "MSP": 0.5,
               "PHL": 0.6, "WDC": 0.5, "ATL": 0.4, "DAL": 0.4, "DEN": 0.3}

_HOLIDAYS: pd.DatetimeIndex | None = None

# outlook_index() expands >130k forecast windows into a daily series; the result depends
# only on (conn, asof), and a single forecast call asks for it twice (past + future). The
# replay harness marches asof forward one day at a time, so a tiny cache is enough.
_OUTLOOK_CACHE: dict[tuple[int, str], tuple[pd.Series, str | None]] = {}


# ── raw PIT readers ──────────────────────────────────────────────────────────
def fut_closes(conn, root: str, asof: datetime, n: int = 4000,
               since: pd.Timestamp | None = None) -> tuple[pd.Series, str | None]:
    """Front-month daily closes for `root`, completed bars only, PIT.

    `since` bounds the read by DATE rather than by bar count. That distinction matters
    when this series is a covariate for a WEEKLY target: 1500 daily bars is 6 years while
    1500 weeks is 29, so a bar-count lookback silently covers <50% of the target index and
    align() then drops the covariate entirely. Callers aligning onto a weekly index must
    pass since=index[0].
    """
    if since is not None:
        rows = conn.execute(
            "SELECT event_time, close, knowledge_time FROM fut_daily WHERE root=?"
            " AND knowledge_time<=? AND event_time>=? ORDER BY event_time",
            (root, asof.isoformat(), pd.Timestamp(since).strftime("%Y-%m-%d"))).fetchall()
        s = pd.Series({pd.Timestamp(r["event_time"]): r["close"] for r in rows},
                      dtype=float).sort_index()
        return s, max((r["knowledge_time"] for r in rows), default=None)
    rows = conn.execute(
        "SELECT event_time, close, knowledge_time FROM fut_daily WHERE root=?"
        " AND knowledge_time<=? ORDER BY event_time DESC LIMIT ?",
        (root, asof.isoformat(), n)).fetchall()
    s = pd.Series({pd.Timestamp(r["event_time"]): r["close"] for r in rows},
                  dtype=float).sort_index()
    return s, max((r["knowledge_time"] for r in rows), default=None)


def fred(conn, sid: str, asof: datetime, first_print: bool = False
         ) -> tuple[pd.Series, str | None]:
    """A FRED/ALFRED series, PIT.

    first_print=False → latest vintage visible at asof (the right read for a CONTEXT: it
    is what a forecaster would actually see on their screen).
    first_print=True  → the FIRST vintage of each observation (the right read when the
    contract settles on the advance print, e.g. KXJOBLESSCLAIMS).
    """
    if first_print:
        # exactly ONE aggregate — SQLite's bare-column-from-that-row rule only holds
        # then (same constraint FeatureStore.fred_first_prints documents).
        rows = conn.execute(
            "SELECT event_time, value, MIN(vintage_date) FROM fred_obs WHERE sid=?"
            " AND knowledge_time<=? GROUP BY event_time ORDER BY event_time",
            (sid, asof.isoformat())).fetchall()
        h = conn.execute(
            "SELECT MAX(knowledge_time) m FROM fred_obs WHERE sid=? AND knowledge_time<=?",
            (sid, asof.isoformat())).fetchone()
        horizon = h["m"] if h else None
    else:
        rows = conn.execute(
            "SELECT event_time, value, MAX(vintage_date), knowledge_time FROM fred_obs"
            " WHERE sid=? AND knowledge_time<=? GROUP BY event_time ORDER BY event_time",
            (sid, asof.isoformat())).fetchall()
        horizon = max((r["knowledge_time"] for r in rows), default=None)
    s = pd.Series({pd.Timestamp(r["event_time"]): r["value"] for r in rows}, dtype=float)
    return s, horizon


def degree_days(conn, asof: datetime) -> tuple[pd.DataFrame, str | None]:
    """Population-weighted national HDD/CDD from weather_daily, PIT.

    PAST covariate only: weather_daily rows are observations, published the day after.
    """
    qs = ",".join("?" * len(_GAS_CITY_W))
    rows = conn.execute(
        f"SELECT region, event_time, hdd, cdd, knowledge_time FROM weather_daily"
        f" WHERE region IN ({qs}) AND knowledge_time<=? ORDER BY event_time",
        (*_GAS_CITY_W, asof.isoformat())).fetchall()
    if not rows:
        return pd.DataFrame(columns=["hdd", "cdd"]), None
    df = pd.DataFrame([dict(r) for r in rows])
    df["w"] = df["region"].map(_GAS_CITY_W)
    df["event_time"] = pd.to_datetime(df["event_time"])
    g = df.groupby("event_time")
    out = pd.DataFrame({
        "hdd": g.apply(lambda x: np.average(x["hdd"].astype(float), weights=x["w"]),
                       include_groups=False),
        "cdd": g.apply(lambda x: np.average(x["cdd"].astype(float), weights=x["w"]),
                       include_groups=False),
    }).sort_index()
    return out, str(df["knowledge_time"].max())


def outlook_index(conn, asof: datetime) -> tuple[pd.Series, str | None]:
    """CPC 6-10 / 8-14 day temperature outlook as a signed warm index, PIT.

    This is a genuine KNOWN-FUTURE covariate: each row is published at `knowledge_time`
    and describes a window (start_date..end_date) that lies 6-14 days AHEAD of it. The
    row carries the dominant category and its probability, so the natural scalar is

        +prob  if 'Above'   (warm)      -prob  if 'Below'  (cold)
         0     if 'Normal' or 'EC'      (equal chances / no signal)

    averaged over the gas-weighted cities and expanded to one value per calendar day of
    the window. Where 6-10 and 8-14 day windows overlap, the mean is taken.

    Returns a daily series indexed by the FORECAST TARGET date — which extends past asof.
    That is the point; using it as a past covariate would be a leak, so callers must only
    pass it via `future_covariates` (and its own past values via `past_covariates`).
    """
    ck = (id(conn), asof.isoformat())
    if ck in _OUTLOOK_CACHE:
        return _OUTLOOK_CACHE[ck]

    qs = ",".join("?" * len(_GAS_CITY_W))
    rows = conn.execute(
        f"SELECT region, product, fcst_date, start_date, end_date, cat, prob,"
        f" knowledge_time FROM weather_fcst WHERE region IN ({qs}) AND knowledge_time<=?"
        f" ORDER BY fcst_date", (*_GAS_CITY_W, asof.isoformat())).fetchall()
    if not rows:
        return pd.Series(dtype=float), None
    df = pd.DataFrame([dict(r) for r in rows])
    df["val"] = (df["cat"].map({"Above": 1.0, "Below": -1.0}).fillna(0.0)
                 * df["prob"].astype(float).fillna(0.0) / 100.0)
    # expand each (start_date..end_date) window to one row per target day. Done with
    # integer day arithmetic + repeat rather than a per-row pd.date_range: the table holds
    # >130k windows and the naive loop dominated the whole forecast call.
    start = pd.to_datetime(df["start_date"]).to_numpy("datetime64[D]").astype("int64")
    end = pd.to_datetime(df["end_date"]).to_numpy("datetime64[D]").astype("int64")
    span = (end - start + 1).clip(min=0)
    keep = span > 0
    start, span = start[keep], span[keep]
    sub = df.loc[keep].reset_index(drop=True)
    offs = np.concatenate([np.arange(k) for k in span]) if len(span) else np.zeros(0, int)
    rep = np.repeat(np.arange(len(sub)), span)
    exp = pd.DataFrame({
        "day": np.repeat(start, span) + offs,
        "region": sub["region"].to_numpy()[rep],
        "product": sub["product"].to_numpy()[rep],
        "fcst_date": sub["fcst_date"].to_numpy()[rep],
        "val": sub["val"].to_numpy()[rep],
    })
    # per (target-day, region, product) keep only the MOST RECENTLY ISSUED outlook
    exp = (exp.sort_values("fcst_date")
              .drop_duplicates(subset=["day", "region", "product"], keep="last"))
    exp["w"] = exp["region"].map(_GAS_CITY_W)
    exp["wv"] = exp["w"] * exp["val"]
    g = exp.groupby("day")[["wv", "w"]].sum()
    g = g[g["w"] > 0]
    s = pd.Series((g["wv"] / g["w"]).to_numpy(),
                  index=pd.to_datetime(g.index.to_numpy().astype("datetime64[D]")),
                  dtype=float).sort_index()
    out = (s, max((r["knowledge_time"] for r in rows), default=None))
    if len(_OUTLOOK_CACHE) > 8:          # asof marches forward in replay; keep it small
        _OUTLOOK_CACHE.clear()
    _OUTLOOK_CACHE[ck] = out
    return out


# ── alignment ────────────────────────────────────────────────────────────────
def align(src: pd.Series, index: pd.DatetimeIndex) -> np.ndarray | None:
    """Carry `src` onto `index` with as-of (backward) semantics.

    Forward-fill ONLY: the value used at index date t is the last src observation dated
    <= t. A src point dated after t is never visible at t, which is what keeps a weekly
    covariate (published Wednesday) out of a Monday context row. Leading positions with
    no prior observation are back-filled from the first available value — a constant
    prefix, which the model's own scaling makes inert — and the covariate is DROPPED
    entirely if it covers less than half the index.
    """
    if src is None or len(src) == 0:
        return None
    s = src[~src.index.duplicated(keep="last")].sort_index()
    out = s.reindex(s.index.union(index)).ffill().reindex(index)
    covered = float(out.notna().mean())
    if covered < 0.5:
        return None
    out = out.bfill()
    if out.isna().any():
        return None
    return out.to_numpy(dtype=float)


def align_future(src: pd.Series, future_index: pd.DatetimeIndex) -> np.ndarray | None:
    """Same as align() but for the horizon. A future covariate must be defined at every
    future step; if the source runs out before the horizon ends the LAST known value is
    held flat (that is the honest 'no further information' state, not an extrapolation).
    """
    if src is None or len(src) == 0:
        return None
    s = src[~src.index.duplicated(keep="last")].sort_index()
    out = s.reindex(s.index.union(future_index)).ffill().reindex(future_index)
    if out.isna().all():
        return None
    return out.bfill().ffill().to_numpy(dtype=float)


# ── deterministic calendar covariates (known arbitrarily far into the future) ─
def _us_holidays() -> pd.DatetimeIndex:
    global _HOLIDAYS
    if _HOLIDAYS is None:
        from pandas.tseries.holiday import USFederalHolidayCalendar
        _HOLIDAYS = USFederalHolidayCalendar().holidays(start="1960-01-01", end="2035-12-31")
    return _HOLIDAYS


def calendar_features(index: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    """Purely deterministic functions of the date — valid as BOTH past and future
    covariates, which is what lets the model use seasonality at the horizon rather than
    only inferring it from the context.

      doy_sin/doy_cos   annual cycle (driving season, heating season)
      woy_sin/woy_cos   52-week cycle (claims' seasonal-adjustment residual)
      dow               day of week, 0-6 (futures weekly effects; flat for weekly series)
      holiday_week      1.0 if a US federal holiday falls in the 7 days ending on t
    """
    idx = pd.DatetimeIndex(index)
    doy = idx.dayofyear.to_numpy(dtype=float)
    woy = idx.isocalendar().week.to_numpy(dtype=float)
    hol = _us_holidays()
    hset = set(hol.normalize())
    holiday_week = np.array(
        [1.0 if any((t.normalize() - pd.Timedelta(days=k)) in hset for k in range(7))
         else 0.0 for t in idx], dtype=float)
    return {
        "doy_sin": np.sin(2 * np.pi * doy / 365.25),
        "doy_cos": np.cos(2 * np.pi * doy / 365.25),
        "woy_sin": np.sin(2 * np.pi * woy / 52.0),
        "woy_cos": np.cos(2 * np.pi * woy / 52.0),
        "dow": idx.dayofweek.to_numpy(dtype=float),
        "holiday_week": holiday_week,
    }


def future_index(last: pd.Timestamp, h: int, step: str) -> pd.DatetimeIndex:
    """The h future timestamps the forecast covers, on the target's own step.

    step='bday' → business days after `last`; step='week' → weekly on `last`'s weekday.
    Used ONLY to evaluate deterministic calendar features and to look up already-published
    outlooks at their target dates; no observation is read from these timestamps.
    """
    if step == "bday":
        return pd.bdate_range(last + pd.Timedelta(days=1), periods=h)
    return pd.DatetimeIndex([last + pd.Timedelta(weeks=k) for k in range(1, h + 1)])
