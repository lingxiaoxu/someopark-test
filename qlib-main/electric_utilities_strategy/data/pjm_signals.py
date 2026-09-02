"""
AEUS PJM signals — Data Miner 2 feeds for the Data-Center-Alley grid
======================================================================
Wired 2026-09-01 (PJM_API_KEY in root .env + config external_sources.pjm.enabled).
Extended 2026-09-02 (config external_sources.pjm.extended) with four more feeds.

Feeds (Data Miner 2, header Ocp-Apim-Subscription-Key; non-member ≤6 conn/min):
  da_hrl_lmps          Western Hub DA hourly LMP → daily mean → z252  (pjm_hub_price,
                       pairs with the ERCOT hub leg inside composite.price_pulse)
  da_hrl_lmps  DOM     Dominion zone DA LMP → daily mean; basis = DOM − Western Hub
                       → z252 (pjm_dom_basis → price_pulse; data-center corridor premium)
  hrl_load_metered     DOM + PEPCO + BC(BGE) + AEP(4 sub-areas) metered MWh/day → 28d
                       mean YoY → z (pjm_zone_load_yoy → power_demand node blend)
  day_gen_capacity     hourly eco_max / total_committed → daily MIN reserve margin
                       → z252 (pjm_reserve_margin; inverted inside shortage_east)
  gen_outages_by_type  day-0 forced outages, PJM RTO (+ Dominion stored) → z252
                       (pjm_forced_outages → shortage_east)
  load_frcstd_hist     RTO day-ahead forecast (last evaluation before the operating
                       day) vs metered RTO actual → 30d MAPE → z252
                       (pjm_load_fcst_err → shortage_east)
  shortage_east        mean_z(−reserve_margin, forced_outages, fcst_err) → z
                       (blended into altdata.load_shortage_score, config-gated)

PIT discipline (AEUS convention, same as ercot_signals / altdata_signals):
  * every store is frozen append-only (pit.merge_frozen): a value first seen is
    never rewritten — metered load that PJM later "verifies" keeps the first-seen
    number, which is exactly what was knowable at the time;
  * availability lags: DA prices / capacity / outages are published before or on
    the operating day → keyed by that day; metered load + forecast error arrive
    ~10-11 days late → shifted by ZONE_LOAD_LAG_DAYS (12) before use;
  * archive wall: non-member DM2 refuses filtered queries older than ~731 days
    (HTTP 400 / 0 rows); extended feeds backfill from EXT_START, so their z252
    is usable from ~2025-10 and the composite blends simply ignore them before.

Tilt-cap discipline: ipp_wholesale already carries 2 external tilts (the cap), so
none of these become new tilts — they enter existing blends only (price_pulse
z-mean, power_demand node z-mean, shortage_score z-mean).

COMPLIANCE (PJM terms, verified 2026-08-30): non-member data is for INTERNAL
BUSINESS USE ONLY — stores live under gitignored price_data/ and must NEVER be
committed to the public repo.

CLI
---
    python -m electric_utilities_strategy.data.pjm_signals --init      # backfill all feeds
    python -m electric_utilities_strategy.data.pjm_signals --update    # incremental, all feeds
    python -m electric_utilities_strategy.data.pjm_signals --verify    # coverage + staleness
Every update/load function accepts ``fetch=`` (row provider) and ``path=`` /
``store_dir=`` overrides so tests run fully sandboxed (tmp_path, zero network).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_QLIB_DIR = _THIS_DIR.parents[1]
_PROJECT_DIR = _THIS_DIR.parents[2]
for _p in (str(_QLIB_DIR), str(_PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from electric_utilities_strategy.data import aeus_pit as pit
except Exception:  # pragma: no cover
    import aeus_pit as pit  # type: ignore

log = logging.getLogger("aeus.pjm")

ALTDATA_DIR = pit.SEMI_DATA_DIR / "altdata"
PJM_LMP_PATH = ALTDATA_DIR / "pjm_da_lmp.json"          # legacy single-column store
STORE_FILES = {
    "dom_lmp":       "pjm_dom_lmp.json",
    "zone_load":     "pjm_zone_load.json",
    "gen_capacity":  "pjm_gen_capacity.json",
    "gen_outages":   "pjm_gen_outages.json",
    "load_forecast": "pjm_load_forecast.json",
}

API_BASE = "https://api.pjm.com/api/v1"
LMP_FEED = "da_hrl_lmps"
WESTERN_HUB_ID = 51288                      # pnode_id, PJM Western Hub
DOM_ZONE_ID = 34964545                      # pnode_id, Dominion zone (type=ZONE, verified 2026-09-02)
HIST_START = "2016-01-01"                   # Western Hub (pre-wall data was never reachable either)
EXT_START = "2024-09-15"                    # extended feeds: inside the ~731-day non-member archive wall
ZONE_LOAD_LAG_DAYS = 12                     # metered load / forecast error arrive ~10-11d late (observed 2026-09-02)
TRACK_AREAS = ["DOM", "PEPCO", "BC", "AEPAPT", "AEPIMP", "AEPKPT", "AEPOPT"]   # BC = BGE; AEP = 4 sub-areas
AEP_PARTS = ["AEPAPT", "AEPIMP", "AEPKPT", "AEPOPT"]
OUTAGE_REGIONS = {"PJM RTO": "RTO", "Mid Atlantic - Dominion": "DOM"}
FORECAST_AREAS = ["RTO", "DOM"]
_REQ_SLEEP = 11.0                            # non-member ≤ 6 conn/min → ~5.5/min
_ROWCOUNT = 50000
_Z_WINDOW = 252


# ── config / gates ────────────────────────────────────────────────────────────
def _cfg_pjm() -> dict:
    try:
        import yaml
        cfg = yaml.safe_load((_THIS_DIR.parent / "config.yaml").read_text())
        return cfg.get("external_sources", {}).get("pjm", {}) or {}
    except Exception:
        return {}


def _enabled(loud: bool = True) -> bool:
    if not _cfg_pjm().get("enabled", False):
        if loud:
            log.info("PJM disabled (config external_sources.pjm.enabled=false)")
        return False
    if not os.environ.get("PJM_API_KEY"):
        if loud:
            log.warning("PJM enabled in config but PJM_API_KEY missing in .env")
        return False
    return True


def _extended_enabled(loud: bool = False) -> bool:
    """Extended feeds (DOM basis / zone load / capacity / outages / forecast) gate.
    Default True once PJM itself is wired; set external_sources.pjm.extended=false
    to fall back to the hub-price-only wiring byte-for-byte."""
    if not _enabled(loud=loud):
        return False
    return bool(_cfg_pjm().get("extended", True))


# ── HTTP ───────────────────────────────────────────────────────────────────────
def _api_get(feed: str, params: dict) -> list:
    url = f"{API_BASE}/{feed}?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={
        "Ocp-Apim-Subscription-Key": os.environ["PJM_API_KEY"],
        "Accept": "application/json",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read())
            return d.get("items", d if isinstance(d, list) else [])
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(30 * (attempt + 1))
                continue
            log.warning("PJM %s HTTP %s", feed, e.code)
            return []
        except Exception as e:  # noqa: BLE001
            log.warning("PJM %s failed: %s", feed, str(e)[:120])
            return []
    return []


def _fetch_range(feed: str, date_field: str, start: str, end: str, chunk_days: int,
                 extra: Optional[dict] = None, fetch: Optional[Callable] = None,
                 sleep: bool = True) -> List[dict]:
    """Chunked pull over [start, end] with startRow paging (DM2 caps rowCount at 50k)."""
    fetch = fetch or _api_get
    rows: List[dict] = []
    cur, last = pd.Timestamp(start), pd.Timestamp(end)
    while cur <= last:
        hi = min(cur + pd.Timedelta(days=chunk_days - 1), last)
        start_row = 1
        while True:
            params = {"rowCount": _ROWCOUNT, "startRow": start_row,
                      date_field: f"{cur.date()} 00:00 to {hi.date()} 23:59"}
            params.update(extra or {})
            got = fetch(feed, params)
            rows.extend(got)
            if len(got) < _ROWCOUNT:
                break
            start_row += _ROWCOUNT
            if sleep:
                time.sleep(_REQ_SLEEP)
        if sleep:
            time.sleep(_REQ_SLEEP)
        cur = hi + pd.Timedelta(days=1)
    return rows


# ── store helpers (multi-column, frozen append-only) ──────────────────────────
def _store_path(key: str, store_dir: Optional[Path] = None) -> Path:
    return (store_dir or ALTDATA_DIR) / STORE_FILES[key]


def _load_cols(path: Path) -> Dict[str, dict]:
    rec = pit.load_json(path, default={}).get("records", {})
    return {k: dict(v) for k, v in rec.items()} if isinstance(rec, dict) else {}


def _save_cols(path: Path, feed: str, cols: Dict[str, dict], extra_meta: Optional[dict] = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = max((len(v) for v in cols.values()), default=0)
    payload = {"meta": {"feed": feed, "updated_at": date.today().isoformat(),
                        "frozen_append_only": True, "n": n, **(extra_meta or {})},
               "records": cols}
    pit.save_json(path, payload)
    return n


def _merge_cols(existing: Dict[str, dict], fresh: Dict[str, dict]) -> Dict[str, dict]:
    out = dict(existing)
    for col, vals in fresh.items():
        out[col] = pit.merge_frozen(existing.get(col, {}), vals)
    return out


def _incr_start(existing: Dict[str, dict], full_start: str, refetch_days: int = 7) -> str:
    dates = [d for col in existing.values() for d in col]
    if not dates:
        return full_start
    return (pd.Timestamp(max(dates)) - pd.Timedelta(days=refetch_days)).date().isoformat()


def _z252(s: pd.Series, name: str) -> pd.Series:
    s = s.sort_index().astype(float)
    m = s.rolling(_Z_WINDOW, min_periods=_Z_WINDOW // 2)
    z = ((s - m.mean()) / m.std().replace(0, np.nan)).dropna()
    z.name = name
    return z


def _bound(z: pd.Series, start, end) -> pd.Series:
    if start:
        z = z.loc[pd.Timestamp(start):]
    if end:
        z = z.loc[:pd.Timestamp(end)]
    return z


def _series(col: dict) -> pd.Series:
    if not col:
        return pd.Series(dtype="float64")
    return pd.Series({pd.Timestamp(k): float(v) for k, v in col.items()}).sort_index()


# ── feed 1: Western Hub DA LMP (unchanged semantics) ──────────────────────────
def update_da_lmp(refreeze: bool = False, fetch: Optional[Callable] = None,
                  path: Optional[Path] = None) -> int:
    """Western Hub DA hourly LMPs → daily mean, frozen append-only."""
    if not _enabled():
        return 0
    path = path or PJM_LMP_PATH
    pit.ensure_dirs(); path.parent.mkdir(parents=True, exist_ok=True)
    existing = {} if refreeze else pit.load_json(path, default={}).get("records", {})
    d0 = HIST_START if not existing else \
        (pd.Timestamp(max(existing)) - pd.Timedelta(days=7)).date().isoformat()
    rows = _fetch_range(LMP_FEED, "datetime_beginning_ept", d0, date.today().isoformat(), 180,
                        {"pnode_id": WESTERN_HUB_ID, "fields": "datetime_beginning_ept,total_lmp_da"},
                        fetch=fetch, sleep=fetch is None)
    if not rows:
        log.error("PJM DA LMP: no rows")
        return 0
    df = pd.DataFrame(rows)
    df["d"] = pd.to_datetime(df["datetime_beginning_ept"]).dt.date.astype(str)
    df["total_lmp_da"] = pd.to_numeric(df["total_lmp_da"], errors="coerce")
    daily = df.groupby("d")["total_lmp_da"].mean().dropna()
    fresh = {k: round(float(v), 3) for k, v in daily.items()}
    records = pit.merge_frozen(existing, fresh)
    payload = {"meta": {"feed": LMP_FEED, "pnode_id": WESTERN_HUB_ID,
                        "updated_at": date.today().isoformat(),
                        "frozen_append_only": True, "n": len(records)},
               "records": records}
    pit.save_json(path, payload)
    log.info("PJM DA LMP: %d days, last %s $%.2f/MWh",
             len(records), max(records), records[max(records)])
    return len(records)


def load_pjm_hub_price(start=None, end=None, path: Optional[Path] = None) -> pd.Series:
    """PJM Western Hub DA price z252 (empty until wired — graceful-0)."""
    records = pit.load_json(path or PJM_LMP_PATH, default={}).get("records", {})
    if not records:
        return pd.Series(dtype="float64", name="pjm_hub_price")
    return _bound(_z252(_series(records), "pjm_hub_price"), start, end)


# ── feed 2: Dominion zone DA LMP → DOM basis ──────────────────────────────────
def update_dom_lmp(refreeze: bool = False, fetch: Optional[Callable] = None,
                   store_dir: Optional[Path] = None) -> int:
    if not _extended_enabled(loud=True):
        return 0
    path = _store_path("dom_lmp", store_dir)
    existing = {} if refreeze else _load_cols(path)
    d0 = _incr_start(existing, EXT_START)
    rows = _fetch_range(LMP_FEED, "datetime_beginning_ept", d0, date.today().isoformat(), 180,
                        {"pnode_id": DOM_ZONE_ID, "fields": "datetime_beginning_ept,total_lmp_da"},
                        fetch=fetch, sleep=fetch is None)
    if not rows:
        log.error("PJM DOM LMP: no rows")
        return 0
    df = pd.DataFrame(rows)
    df["d"] = pd.to_datetime(df["datetime_beginning_ept"]).dt.date.astype(str)
    df["v"] = pd.to_numeric(df["total_lmp_da"], errors="coerce")
    daily = df.groupby("d")["v"].mean().dropna()
    cols = _merge_cols(existing, {"DOM": {k: round(float(v), 3) for k, v in daily.items()}})
    n = _save_cols(path, LMP_FEED, cols, {"pnode_id": DOM_ZONE_ID})
    log.info("PJM DOM LMP: %d days, last %s", n, max(cols["DOM"]))
    return n


def load_dom_basis(start=None, end=None, store_dir: Optional[Path] = None,
                   hub_path: Optional[Path] = None) -> pd.Series:
    """DOM zone − Western Hub daily DA LMP ($/MWh) → z252. Positive = corridor premium."""
    dom = _series(_load_cols(_store_path("dom_lmp", store_dir)).get("DOM", {}))
    hub = _series(pit.load_json(hub_path or PJM_LMP_PATH, default={}).get("records", {}))
    if dom.empty or hub.empty:
        return pd.Series(dtype="float64", name="pjm_dom_basis")
    basis = (dom - hub).dropna()
    if basis.empty:
        return pd.Series(dtype="float64", name="pjm_dom_basis")
    return _bound(_z252(basis, "pjm_dom_basis"), start, end)


# ── feed 3: metered zone load → YoY ──────────────────────────────────────────
def update_zone_load(refreeze: bool = False, fetch: Optional[Callable] = None,
                     store_dir: Optional[Path] = None) -> int:
    """Daily MWh for tracked areas (+RTO) from hourly metered load. All areas are
    pulled per chunk and filtered client-side (DM2 filter takes one value)."""
    if not _extended_enabled(loud=True):
        return 0
    path = _store_path("zone_load", store_dir)
    existing = {} if refreeze else _load_cols(path)
    d0 = _incr_start(existing, EXT_START, refetch_days=21)
    rows = _fetch_range("hrl_load_metered", "datetime_beginning_ept", d0, date.today().isoformat(), 45,
                        {"fields": "datetime_beginning_ept,load_area,mw"},
                        fetch=fetch, sleep=fetch is None)
    if not rows:
        log.error("PJM zone load: no rows")
        return 0
    df = pd.DataFrame(rows)
    df = df[df["load_area"].isin(TRACK_AREAS + ["RTO"])].copy()
    if df.empty:
        log.error("PJM zone load: no tracked areas in rows")
        return 0
    df["d"] = pd.to_datetime(df["datetime_beginning_ept"]).dt.date.astype(str)
    df["mw"] = pd.to_numeric(df["mw"], errors="coerce")
    g = df.groupby(["load_area", "d"]).agg(mwh=("mw", "sum"), n=("mw", "count")).reset_index()
    g = g[g["n"] >= 23]                        # full-day rows only (DST day has 23/25)
    fresh: Dict[str, dict] = {}
    for area, sub in g.groupby("load_area"):
        fresh[area] = {r.d: round(float(r.mwh), 1) for r in sub.itertuples()}
    cols = _merge_cols(existing, fresh)
    n = _save_cols(path, "hrl_load_metered", cols, {"areas": TRACK_AREAS + ["RTO"]})
    log.info("PJM zone load: %d days, areas %s", n, sorted(cols))
    return n


def _tracked_zone_mwh(store_dir: Optional[Path] = None) -> pd.Series:
    cols = _load_cols(_store_path("zone_load", store_dir))
    parts = [_series(cols[a]) for a in TRACK_AREAS if a in cols]
    if not parts:
        return pd.Series(dtype="float64")
    df = pd.concat(parts, axis=1)
    need = len(parts)
    return df.dropna(thresh=need).sum(axis=1)     # only days where every tracked area reported


def load_zone_load_yoy(start=None, end=None, store_dir: Optional[Path] = None) -> pd.Series:
    """Tracked-zone (DOM+PEPCO+BGE+AEP) 28d-mean MWh YoY → z252, availability-shifted
    by ZONE_LOAD_LAG_DAYS (metered load lands ~10-11 days after the operating day)."""
    mwh = _tracked_zone_mwh(store_dir)
    if len(mwh) < 400:
        return pd.Series(dtype="float64", name="pjm_zone_load_yoy")
    daily = mwh.asfreq("D")
    ma = daily.rolling(28, min_periods=24).mean()
    yoy = (ma / ma.shift(364) - 1.0) * 100.0
    yoy = yoy.dropna()
    if yoy.empty:
        return pd.Series(dtype="float64", name="pjm_zone_load_yoy")
    yoy.index = yoy.index + pd.Timedelta(days=ZONE_LOAD_LAG_DAYS)
    return _bound(_z252(yoy, "pjm_zone_load_yoy"), start, end)


# ── feed 4: daily generation capacity → reserve margin ───────────────────────
def update_gen_capacity(refreeze: bool = False, fetch: Optional[Callable] = None,
                        store_dir: Optional[Path] = None) -> int:
    if not _extended_enabled(loud=True):
        return 0
    path = _store_path("gen_capacity", store_dir)
    existing = {} if refreeze else _load_cols(path)
    d0 = _incr_start(existing, EXT_START)
    rows = _fetch_range("day_gen_capacity", "bid_datetime_beginning_ept", d0, date.today().isoformat(), 180,
                        {"fields": "bid_datetime_beginning_ept,eco_max,emerg_max,total_committed"},
                        fetch=fetch, sleep=fetch is None)
    if not rows:
        log.error("PJM gen capacity: no rows")
        return 0
    df = pd.DataFrame(rows)
    df["d"] = pd.to_datetime(df["bid_datetime_beginning_ept"]).dt.date.astype(str)
    for c in ("eco_max", "emerg_max", "total_committed"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["margin"] = (df["eco_max"] - df["total_committed"]) / df["eco_max"].replace(0, np.nan)
    g = df.groupby("d").agg(margin_min=("margin", "min"), eco_max=("eco_max", "mean"),
                            committed=("total_committed", "mean")).dropna()
    fresh = {"margin_min": {k: round(float(v), 5) for k, v in g["margin_min"].items()},
             "eco_max":    {k: round(float(v), 1) for k, v in g["eco_max"].items()},
             "committed":  {k: round(float(v), 1) for k, v in g["committed"].items()}}
    cols = _merge_cols(existing, fresh)
    n = _save_cols(path, "day_gen_capacity", cols)
    log.info("PJM gen capacity: %d days, last margin_min %.3f", n, g["margin_min"].iloc[-1])
    return n


def load_reserve_margin(start=None, end=None, store_dir: Optional[Path] = None) -> pd.Series:
    """Daily MIN over hours of (eco_max − committed)/eco_max → z252. Higher = looser."""
    s = _series(_load_cols(_store_path("gen_capacity", store_dir)).get("margin_min", {}))
    if s.empty:
        return pd.Series(dtype="float64", name="pjm_reserve_margin")
    return _bound(_z252(s, "pjm_reserve_margin"), start, end)


# ── feed 5: generation outages (day-0) ───────────────────────────────────────
def update_gen_outages(refreeze: bool = False, fetch: Optional[Callable] = None,
                       store_dir: Optional[Path] = None) -> int:
    if not _extended_enabled(loud=True):
        return 0
    path = _store_path("gen_outages", store_dir)
    existing = {} if refreeze else _load_cols(path)
    d0 = _incr_start(existing, EXT_START)
    rows = _fetch_range("gen_outages_by_type", "forecast_execution_date_ept", d0, date.today().isoformat(), 90,
                        {"fields": "forecast_execution_date_ept,forecast_date,region,total_outages_mw,forced_outages_mw"},
                        fetch=fetch, sleep=fetch is None)
    if not rows:
        log.error("PJM gen outages: no rows")
        return 0
    df = pd.DataFrame(rows)
    df["exec_d"] = df["forecast_execution_date_ept"].astype(str).str[:10]
    df["fc_d"] = df["forecast_date"].astype(str).str[:10]
    df = df[(df["exec_d"] == df["fc_d"]) & df["region"].isin(OUTAGE_REGIONS)]   # day-0 rows only
    if df.empty:
        log.error("PJM gen outages: no day-0 rows")
        return 0
    fresh: Dict[str, dict] = {}
    for region, sub in df.groupby("region"):
        tag = OUTAGE_REGIONS[region]
        sub = sub.sort_values("exec_d").drop_duplicates("exec_d", keep="last")
        fresh[f"{tag}_forced"] = {r.exec_d: float(r.forced_outages_mw) for r in sub.itertuples()}
        fresh[f"{tag}_total"] = {r.exec_d: float(r.total_outages_mw) for r in sub.itertuples()}
    cols = _merge_cols(existing, fresh)
    n = _save_cols(path, "gen_outages_by_type", cols, {"regions": OUTAGE_REGIONS})
    log.info("PJM gen outages: %d days", n)
    return n


def load_forced_outages(start=None, end=None, store_dir: Optional[Path] = None,
                        region: str = "RTO") -> pd.Series:
    """Day-0 forced outages MW (region RTO default) → z252. Higher = tighter."""
    s = _series(_load_cols(_store_path("gen_outages", store_dir)).get(f"{region}_forced", {}))
    if s.empty:
        return pd.Series(dtype="float64", name="pjm_forced_outages")
    return _bound(_z252(s, "pjm_forced_outages"), start, end)


# ── feed 6: day-ahead load forecast → forecast error ──────────────────────────
def update_load_forecast(refreeze: bool = False, fetch: Optional[Callable] = None,
                         store_dir: Optional[Path] = None) -> int:
    """Per area/operating day: the LAST forecast evaluated before the day began,
    summed over its 24 hours (MWh)."""
    if not _extended_enabled(loud=True):
        return 0
    path = _store_path("load_forecast", store_dir)
    existing = {} if refreeze else _load_cols(path)
    d0 = _incr_start(existing, EXT_START)
    fresh: Dict[str, dict] = {}
    for area in FORECAST_AREAS:
        rows = _fetch_range("load_frcstd_hist", "forecast_hour_beginning_ept", d0, date.today().isoformat(), 30,
                            {"forecast_area": area,
                             "fields": "evaluated_at_ept,forecast_hour_beginning_ept,forecast_area,forecast_load_mw"},
                            fetch=fetch, sleep=fetch is None)
        if not rows:
            log.warning("PJM load forecast %s: no rows", area)
            continue
        df = pd.DataFrame(rows)
        df["fh"] = pd.to_datetime(df["forecast_hour_beginning_ept"])
        df["ev"] = pd.to_datetime(df["evaluated_at_ept"])
        df["d"] = df["fh"].dt.normalize()
        df = df[df["ev"] < df["d"]]                                   # evaluated before the operating day
        if df.empty:
            continue
        last_ev = df.groupby("d")["ev"].transform("max")
        df = df[df["ev"] == last_ev]
        df["forecast_load_mw"] = pd.to_numeric(df["forecast_load_mw"], errors="coerce")
        g = df.groupby("d").agg(mwh=("forecast_load_mw", "sum"), n=("forecast_load_mw", "count"))
        g = g[g["n"] >= 23]
        fresh[area] = {k.date().isoformat(): round(float(v), 1) for k, v in g["mwh"].items()}
    if not fresh:
        log.error("PJM load forecast: no rows")
        return 0
    cols = _merge_cols(existing, fresh)
    n = _save_cols(path, "load_frcstd_hist", cols, {"areas": FORECAST_AREAS,
                                                       "rule": "last evaluation before operating day"})
    log.info("PJM load forecast: %d days", n)
    return n


def load_forecast_error(start=None, end=None, store_dir: Optional[Path] = None) -> pd.Series:
    """|DA forecast − metered| / metered for RTO, 30d mean → z252, shifted by
    ZONE_LOAD_LAG_DAYS (the metered denominator is what arrives late)."""
    fc = _series(_load_cols(_store_path("load_forecast", store_dir)).get("RTO", {}))
    act = _series(_load_cols(_store_path("zone_load", store_dir)).get("RTO", {}))
    if fc.empty or act.empty:
        return pd.Series(dtype="float64", name="pjm_load_fcst_err")
    df = pd.concat([fc, act], axis=1).dropna()
    if len(df) < 60:
        return pd.Series(dtype="float64", name="pjm_load_fcst_err")
    ape = ((df.iloc[:, 0] - df.iloc[:, 1]).abs() / df.iloc[:, 1].replace(0, np.nan)) * 100.0
    m = ape.asfreq("D").rolling(30, min_periods=20).mean().dropna()
    if m.empty:
        return pd.Series(dtype="float64", name="pjm_load_fcst_err")
    m.index = m.index + pd.Timedelta(days=ZONE_LOAD_LAG_DAYS)
    return _bound(_z252(m, "pjm_load_fcst_err"), start, end)


# ── composite of the eastern-grid tightness evidence ─────────────────────────
def load_shortage_east(start=None, end=None, store_dir: Optional[Path] = None) -> pd.Series:
    """mean_z(−reserve_margin, forced_outages, forecast_error) → re-z. Consumed by
    altdata.load_shortage_score as the PJM/eastern leg (config-gated)."""
    parts = []
    rm = load_reserve_margin(end=end, store_dir=store_dir)
    if not rm.empty:
        parts.append(-rm)
    fo = load_forced_outages(end=end, store_dir=store_dir)
    if not fo.empty:
        parts.append(fo)
    fe = load_forecast_error(end=end, store_dir=store_dir)
    if not fe.empty:
        parts.append(fe)
    if not parts:
        return pd.Series(dtype="float64", name="pjm_shortage_east")
    df = pd.concat(parts, axis=1).sort_index().ffill().dropna(how="all")
    blend = df.mean(axis=1).dropna()
    if blend.empty:
        return pd.Series(dtype="float64", name="pjm_shortage_east")
    return _bound(_z252(blend, "pjm_shortage_east"), start, end)


# ── update-all / verify / CLI ────────────────────────────────────────────────
EXT_UPDATERS = [("dom_lmp", update_dom_lmp), ("zone_load", update_zone_load),
                ("gen_capacity", update_gen_capacity), ("gen_outages", update_gen_outages),
                ("load_forecast", update_load_forecast)]


def update_all(refreeze: bool = False, fetch: Optional[Callable] = None,
               store_dir: Optional[Path] = None, hub_path: Optional[Path] = None) -> dict:
    """Run every feed; one failing feed never blocks the others. Returns {key: n}."""
    out = {"lmp": update_da_lmp(refreeze=refreeze, fetch=fetch, path=hub_path)}
    if _extended_enabled(loud=True):
        for key, fn in EXT_UPDATERS:
            try:
                out[key] = fn(refreeze=refreeze, fetch=fetch, store_dir=store_dir)
            except Exception as e:  # noqa: BLE001
                log.error("PJM %s update failed: %s", key, str(e)[:160])
                out[key] = 0
    return out


def verify(store_dir: Optional[Path] = None, hub_path: Optional[Path] = None) -> bool:
    print("=" * 70)
    print("AEUS PJM SIGNALS")
    print("=" * 70)
    wired = _enabled(loud=False)
    ext = _extended_enabled(loud=False)
    ok = True

    def _row(name, s, required):
        nonlocal ok
        if len(s):
            # staleness judged on the availability-shifted index = the series as consumed
            tag = pit.stale_tag(s.index[-1].date(), "daily") if required else ""
            print(f"  {name:20}: {len(s):5} pts →{s.index[-1].date()} z={s.iloc[-1]:+.2f}{tag}")
            if tag:
                ok = False
        else:
            state = ("WIRED but no data yet" if required else
                     "NOT WIRED" if not wired else "extended feeds disabled")
            print(f"  {name:20}: EMPTY — {state}")
            if required:
                ok = False

    today = date.today().isoformat()      # lagged series are keyed by availability date → cap at today
    _row("pjm_hub_price", load_pjm_hub_price(end=today, path=hub_path), wired)
    _row("pjm_dom_basis", load_dom_basis(end=today, store_dir=store_dir, hub_path=hub_path), ext)
    _row("pjm_zone_load_yoy", load_zone_load_yoy(end=today, store_dir=store_dir), ext)
    _row("pjm_reserve_margin", load_reserve_margin(end=today, store_dir=store_dir), ext)
    _row("pjm_forced_outages", load_forced_outages(end=today, store_dir=store_dir), ext)
    _row("pjm_load_fcst_err", load_forecast_error(end=today, store_dir=store_dir), ext)
    _row("pjm_shortage_east", load_shortage_east(end=today, store_dir=store_dir), ext)
    print("=" * 70)
    print("RESULT:", "OK" if ok else "INCOMPLETE")
    return ok


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="AEUS PJM signals (Data Miner 2)")
    ap.add_argument("--init", action="store_true", help="backfill all feeds (hub 2016+, extended within the ~731d wall)")
    ap.add_argument("--update", action="store_true", help="incremental update, all feeds")
    ap.add_argument("--refreeze", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    did = False
    if args.init or args.update:
        res = update_all(refreeze=args.refreeze); did = True
        if _enabled(loud=False):
            # 2026-09-01 (approved small fix): a WIRED fetch with no records is a failure.
            # Extended feeds: only fail the step when the primary hub leg failed or when
            # ≥2 extended feeds produced nothing (one transient miss stays a WARN; the
            # weekly six-layer --verify catches real staleness).
            ext_fail = [k for k, n in res.items() if k != "lmp" and n == 0]
            if res.get("lmp", 0) == 0:
                log.error("PJM DA LMP update produced no records while wired — exit 1")
                sys.exit(1)
            if _extended_enabled(loud=False) and len(ext_fail) >= 2:
                log.error("PJM extended feeds failed: %s — exit 1", ext_fail)
                sys.exit(1)
            if ext_fail:
                log.warning("PJM extended feed(s) produced no new records: %s", ext_fail)
    if args.verify or did:
        ok = verify()
        if args.verify and not ok:
            sys.exit(1)
    if not did and not args.verify:
        print("Nothing to do. Use --init / --update / --verify.")


if __name__ == "__main__":
    main()
