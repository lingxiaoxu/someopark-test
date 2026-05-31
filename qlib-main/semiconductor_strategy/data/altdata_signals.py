"""
AISS alternative-data layer
===========================
Backfillable, no-extra-key (FRED) alt-data signals + a forward-only GPU-pricing
pipeline.  Stored under ``price_data/semi_strategy/altdata/`` — purely additive,
never touches existing data.  Every series here is consumed by ``supply_chain``
as a small *confirmation tilt* (gated by config; missing → 0 → graceful V1
fallback), per plan §8.

Sources (all endpoints verified live 2026-05-30):

  N2  Semiconductor hiring pulse  (FRED Indeed job postings, weekly, 2020-02+)
        IHLIDXUSTPELECENGI / ...PRMA / ...INDUENGI / ...SOFTDEVE
        → composite hiring index (6-month change, z-scored) → equipment / global tilt
  N3  Semiconductor fundamentals  (FRED, deep history)
        IPG3344S      semiconductor industrial production (1972+)
        PCU33443344   semiconductor PPI                   (1984+)
        A34SNO        computers & electronics new orders  (1992+)
        → YoY z-scores → global cycle tilt; semi_ip also backs N6 (SIA)
  N4-proxy  Korea goods exports  VALEXPKRM052N  (FRED, 2006+)  → memory_hbm tilt
  N7-proxy  Taiwan goods exports VALEXPTWM052N  (FRED, 2006+)  → foundry tilt
  N8  GPU cloud pricing  (ComputePrices.com API) — FORWARD-ONLY (no history) →
        daily snapshot appended to a self-built parquet; NOT a backtest factor.

PIT: FRED series are observation-date indexed; we shift availability forward by a
conservative per-frequency publication lag (monthly +45d, weekly +7d) before
forward-filling to a daily calendar, so a value is never visible before it could
realistically have been published.

CLI
---
    python -m semiconductor_strategy.data.altdata_signals --init-fred
    python -m semiconductor_strategy.data.altdata_signals --update-fred
    python -m semiconductor_strategy.data.altdata_signals --snapshot-gpu
    python -m semiconductor_strategy.data.altdata_signals --init-gpu
    python -m semiconductor_strategy.data.altdata_signals --verify
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_QLIB_DIR = _THIS_DIR.parents[1]
_PROJECT_DIR = _THIS_DIR.parents[2]
for _p in (str(_QLIB_DIR), str(_PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from semiconductor_strategy.data import aiss_pit as pit
except Exception:  # pragma: no cover
    import aiss_pit as pit  # type: ignore

log = logging.getLogger("aiss.altdata")

ALTDATA_DIR = pit.SEMI_DATA_DIR / "altdata"
FRED_ALTDATA_PATH = ALTDATA_DIR / "fred_altdata.json"
GPU_HISTORY_PATH = ALTDATA_DIR / "gpu_pricing_history.parquet"

# --- FRED series (verified) -----------------------------------------------
# name -> (series_id, frequency)  freq in {"M","W"} drives the PIT lag
FRED_HIRING: Dict[str, tuple] = {
    "hiring_elec_eng":  ("IHLIDXUSTPELECENGI", "W"),
    "hiring_prod_mfg":  ("IHLIDXUSTPPRMA",     "W"),
    "hiring_indu_eng":  ("IHLIDXUSTPINDUENGI", "W"),
    "hiring_soft_dev":  ("IHLIDXUSTPSOFTDEVE", "W"),
}
FRED_SEMI: Dict[str, tuple] = {
    "semi_ip":        ("IPG3344S",     "M"),   # semiconductor industrial production
    "semi_ppi":       ("PCU33443344",  "M"),   # semiconductor PPI
    "electronics_no": ("A34SNO",       "M"),   # computers & electronics new orders
}
FRED_EXPORTS: Dict[str, tuple] = {
    "korea_exports":  ("VALEXPKRM052N", "M"),  # N4-proxy
    "taiwan_exports": ("VALEXPTWM052N", "M"),  # N7-proxy
}
ALL_FRED = {**FRED_HIRING, **FRED_SEMI, **FRED_EXPORTS}

# Conservative publication lag (days) by frequency → PIT availability shift
LAG_DAYS = {"M": 45, "W": 7}
FRED_HISTORY_START = "2006-01-01"   # deep enough for proxies; semi series go earlier
_TS_Z_WINDOW = 36                   # months for z-scores
_TS_Z_MIN = 12

# --- GPU pricing (forward-only) --------------------------------------------
GPU_API = "https://computeprices.com/api/v1/gpu-prices"
GPU_MODELS = ("H100", "H200", "B200", "B100")


# ===========================================================================
# FRED fetch / persist
# ===========================================================================

def _fetch_fred(series_id: str, start: str) -> pd.Series:
    """Observation-date-indexed FRED series (reuses fredapi like loader.py)."""
    from fredapi import Fred
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise ValueError("FRED_API_KEY not set")
    s = Fred(api_key=key).get_series(series_id, observation_start=start)
    s.index = pd.to_datetime(s.index).normalize()
    return s.dropna()


def update_fred_altdata(start: str = FRED_HISTORY_START) -> int:
    """Fetch all FRED alt-data series and persist raw observations + meta."""
    ALTDATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"meta": {"updated_at": date.today().isoformat(),
                        "lag_days": LAG_DAYS, "z_window": _TS_Z_WINDOW}, "series": {}}
    import time
    n_ok = 0
    for name, (sid, freq) in ALL_FRED.items():
        s = None
        for attempt in range(4):                 # retry w/ backoff on FRED 429
            try:
                s = _fetch_fred(sid, start)
                break
            except Exception as e:  # noqa: BLE001
                if "Too Many Requests" in str(e) or "Rate Limit" in str(e):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                log.warning("FRED %s (%s) failed: %s", sid, name, e)
                break
        if s is None:
            log.warning("FRED %s (%s) failed after retries", sid, name)
            continue
        time.sleep(0.7)                           # gentle pacing between series
        payload["series"][name] = {
            "series_id": sid, "freq": freq, "n": int(len(s)),
            "first": s.index[0].date().isoformat() if len(s) else None,
            "last": s.index[-1].date().isoformat() if len(s) else None,
            "obs": {d.date().isoformat(): float(v) for d, v in s.items()},
        }
        n_ok += 1
        log.info("FRED %-16s %-16s %4d obs %s→%s", name, sid, len(s),
                 s.index[0].date() if len(s) else "-", s.index[-1].date() if len(s) else "-")
    pit.save_json(FRED_ALTDATA_PATH, payload)
    log.info("FRED alt-data: %d/%d series saved → %s", n_ok, len(ALL_FRED), FRED_ALTDATA_PATH)
    return n_ok


def _raw_fred(name: str) -> pd.Series:
    payload = pit.load_json(FRED_ALTDATA_PATH, default={})
    node = payload.get("series", {}).get(name)
    if not node or not node.get("obs"):
        return pd.Series(dtype="float64", name=name)
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in node["obs"].items()}).sort_index()
    s.name = name
    return s


def _pit_daily(name: str, start: Optional[str], end: Optional[str]) -> pd.Series:
    """Observation series shifted forward by the publication lag, ffilled daily."""
    s = _raw_fred(name)
    if s.empty:
        return s
    _, freq = ALL_FRED.get(name, (None, "M"))
    lag = LAG_DAYS.get(freq, 45)
    s = s.copy()
    s.index = s.index + pd.Timedelta(days=lag)          # availability = obs + lag
    s = s[~s.index.duplicated(keep="last")]
    end_ts = pd.Timestamp(end) if end else pd.Timestamp(date.today())
    idx = pd.date_range(start=s.index[0], end=end_ts, freq="D")
    daily = s.reindex(idx).ffill()
    if start:
        daily = daily.loc[pd.Timestamp(start):]
    daily.name = name
    return daily


def _ts_z(s: pd.Series, window_months: int = _TS_Z_WINDOW) -> pd.Series:
    """Z-score a daily series on a ~monthly cadence (window in months → ~21d each)."""
    if s.empty:
        return s
    w = window_months * 21
    mu = s.rolling(w, min_periods=_TS_Z_MIN * 21).mean()
    sd = s.rolling(w, min_periods=_TS_Z_MIN * 21).std().replace(0, np.nan)
    return ((s - mu) / sd).dropna()


# ===========================================================================
# Public signal loaders (PIT-correct daily series)
# ===========================================================================

def load_semi_hiring(start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """Composite semiconductor hiring pulse: 6m change of (elec+prod+indu), z-scored.

    Indeed series start 2020-02 → before that returns empty (caller treats as 0).
    """
    # Compute on FULL history (no pre-truncation) so the 6m-change + z-score
    # warm-up isn't lost; truncate to ``start`` only at the very end.
    parts = [_pit_daily(n, None, end) for n in ("hiring_elec_eng", "hiring_prod_mfg", "hiring_indu_eng")]
    parts = [p for p in parts if not p.empty]
    if not parts:
        return pd.Series(dtype="float64", name="semi_hiring")
    df = pd.concat(parts, axis=1).ffill().dropna(how="all")
    comp = df.mean(axis=1)
    chg = comp - comp.shift(126)           # ~6-month change
    z = _ts_z(chg.dropna())
    z.name = "semi_hiring"
    if start:
        z = z.loc[pd.Timestamp(start):]
    return z


def _yoy_z(name: str, start: Optional[str], end: Optional[str], periods: int = 252) -> pd.Series:
    # Full history for YoY + z-score warm-up; truncate to ``start`` at the end.
    s = _pit_daily(name, None, end)
    if s.empty:
        return s
    yoy = s.pct_change(periods)
    z = _ts_z(yoy.dropna())
    z.name = name
    if start:
        z = z.loc[pd.Timestamp(start):]
    return z


def load_semi_ip_yoy(start=None, end=None) -> pd.Series:       # N3
    return _yoy_z("semi_ip", start, end)


def load_semi_ppi_yoy(start=None, end=None) -> pd.Series:      # N3
    return _yoy_z("semi_ppi", start, end)


def load_electronics_orders_yoy(start=None, end=None) -> pd.Series:  # N3
    return _yoy_z("electronics_no", start, end)


def load_korea_exports_yoy(start=None, end=None) -> pd.Series:  # N4-proxy
    return _yoy_z("korea_exports", start, end)


def load_taiwan_exports_yoy(start=None, end=None) -> pd.Series:  # N7-proxy
    return _yoy_z("taiwan_exports", start, end)


# ===========================================================================
# AI-infra DEMAND CYCLE index (pro-cyclical exposure amplifier, plan §8 Path A)
# ===========================================================================

def load_ai_demand_cycle(start: Optional[str] = None, end: Optional[str] = None,
                         sources: Optional[list] = None) -> pd.Series:
    """PIT daily z-score of AI-infrastructure demand strength.

    Blends the demand-side alt-data accelerants (all already publication-lagged
    & PIT-safe):
        hyperscaler_capex_yoy  (N1, 2018+)  — the prime AI-capex driver
        korea_exports_yoy      (N4p, 2006+) — Samsung/Hynix memory demand
        electronics_no_yoy     (N3, 2006+)  — computers/electronics new orders
    Each is time-series z-scored, averaged across whatever is available, then the
    blend is z-scored again so it is centred and ~unit-scale.  Used by the engine
    to scale gross exposure: high (demand accelerating) → stay fully invested;
    low/negative (decelerating, e.g. 2022) → trim.  Missing data → empty → engine
    leaves exposure at 1.0 (V1).
    """
    try:
        from semiconductor_strategy.data import company_signals as comp
    except Exception:  # pragma: no cover
        import company_signals as comp  # type: ignore
    src = sources or ["hyperscaler_capex_yoy", "korea_exports_yoy", "electronics_no_yoy"]
    parts = []
    if "hyperscaler_capex_yoy" in src:
        h = comp.load_hyperscaler_capex_yoy(end=end)        # raw YoY% → z over time
        if not h.empty:
            parts.append(_ts_z(h.dropna()))
    if "korea_exports_yoy" in src:
        parts.append(load_korea_exports_yoy(end=end))       # already z
    if "electronics_no_yoy" in src:
        parts.append(load_electronics_orders_yoy(end=end))  # already z
    parts = [p for p in parts if p is not None and not p.empty]
    if not parts:
        return pd.Series(dtype="float64", name="ai_demand_cycle")
    df = pd.concat(parts, axis=1).sort_index().ffill().dropna(how="all")
    blend = df.mean(axis=1)
    z = _ts_z(blend.dropna())
    z.name = "ai_demand_cycle"
    if start:
        z = z.loc[pd.Timestamp(start):]
    return z


# ===========================================================================
# N8: GPU cloud pricing (FORWARD-ONLY — no historical backfill possible)
# ===========================================================================

def _fetch_gpu_snapshot() -> pd.DataFrame:
    req = urllib.request.Request(GPU_API, headers={"User-Agent": "AISS lxu912@gmail.com"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    rows = data.get("data") if isinstance(data, dict) else data
    return pd.DataFrame(rows or [])


def snapshot_gpu(as_of: Optional[str] = None) -> int:
    """Append today's median on-demand price for tracked GPUs to the parquet.

    Forward-only: ComputePrices exposes only the current snapshot (no history),
    so we build our own time series one day at a time.  NOT used as a backtest
    factor (see plan §8.2 N8) — production supply/demand thermometer only.
    """
    ALTDATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        df = _fetch_gpu_snapshot()
    except Exception as e:  # noqa: BLE001
        log.warning("GPU snapshot fetch failed: %s", e)
        return 0
    if df.empty:
        log.warning("GPU snapshot empty")
        return 0
    day = pd.Timestamp(as_of) if as_of else pd.Timestamp(date.today())
    gpu_col = "gpu" if "gpu" in df.columns else ("gpu_model" if "gpu_model" in df.columns else None)
    price_col = "price_per_hour_usd" if "price_per_hour_usd" in df.columns else None
    rec = {"date": day.date().isoformat()}
    if gpu_col and price_col:
        df["_g"] = df[gpu_col].astype(str).str.upper()
        ondemand = df[df.get("pricing_type", "on_demand").astype(str).str.contains("on", case=False, na=True)] \
            if "pricing_type" in df.columns else df
        for model in GPU_MODELS:
            sub = ondemand[ondemand["_g"].str.contains(model, na=False)]
            rec[f"{model}_median_usd_hr"] = round(float(sub[price_col].median()), 4) if len(sub) else np.nan
            rec[f"{model}_n"] = int(len(sub))
    new = pd.DataFrame([rec])
    if GPU_HISTORY_PATH.exists():
        hist = pd.read_parquet(GPU_HISTORY_PATH)
        hist = hist[hist["date"] != rec["date"]]      # idempotent per day
        out = pd.concat([hist, new], ignore_index=True).sort_values("date")
    else:
        out = new
    out.to_parquet(GPU_HISTORY_PATH, index=False)
    log.info("GPU snapshot %s: H100=%.2f H200=%.2f B200=%.2f (history=%d days)",
             rec["date"], rec.get("H100_median_usd_hr", np.nan),
             rec.get("H200_median_usd_hr", np.nan), rec.get("B200_median_usd_hr", np.nan), len(out))
    return len(out)


def load_gpu_price_history() -> pd.DataFrame:
    if GPU_HISTORY_PATH.exists():
        return pd.read_parquet(GPU_HISTORY_PATH)
    return pd.DataFrame()


def init_gpu() -> None:
    """Create an empty GPU history parquet (forward accumulation starts now)."""
    ALTDATA_DIR.mkdir(parents=True, exist_ok=True)
    if not GPU_HISTORY_PATH.exists():
        cols = ["date"] + [f"{m}_median_usd_hr" for m in GPU_MODELS] + [f"{m}_n" for m in GPU_MODELS]
        pd.DataFrame(columns=cols).to_parquet(GPU_HISTORY_PATH, index=False)
        log.info("Initialised empty GPU history → %s (forward-only)", GPU_HISTORY_PATH)


# ===========================================================================
# Snapshot + verify
# ===========================================================================

def get_altdata_snapshot(as_of_date) -> dict:
    """Latest alt-data signal values available as of ``as_of_date`` (for reports)."""
    snap: dict = {}
    for label, fn in (("semi_hiring_z", load_semi_hiring),
                      ("semi_ip_yoy_z", load_semi_ip_yoy),
                      ("semi_ppi_yoy_z", load_semi_ppi_yoy),
                      ("electronics_orders_yoy_z", load_electronics_orders_yoy),
                      ("korea_exports_yoy_z", load_korea_exports_yoy),
                      ("taiwan_exports_yoy_z", load_taiwan_exports_yoy)):
        s = fn(end=str(as_of_date))
        snap[label] = float(s.iloc[-1]) if len(s) else None
    return snap


def verify() -> bool:
    print("=" * 74)
    print("AISS ALT-DATA LAYER")
    print("=" * 74)
    payload = pit.load_json(FRED_ALTDATA_PATH, default={})
    series = payload.get("series", {})
    ok = bool(series)
    for name in ALL_FRED:
        node = series.get(name)
        if node:
            print(f"  {name:18} {node['series_id']:16} {node['n']:5} obs  {node['first']}→{node['last']}")
        else:
            print(f"  {name:18} MISSING")
            ok = False
    print("  " + "-" * 70)
    for label, fn in (("semi_hiring", load_semi_hiring), ("semi_ip_yoy", load_semi_ip_yoy),
                      ("korea_exports_yoy", load_korea_exports_yoy),
                      ("taiwan_exports_yoy", load_taiwan_exports_yoy)):
        s = fn(start="2019-01-01")
        tail = f"{s.index[0].date()}→{s.index[-1].date()} last={s.iloc[-1]:+.2f}" if len(s) else "EMPTY (pre-2020 ok for hiring)"
        print(f"  signal {label:20}: {len(s):5} pts  {tail}")
    gh = load_gpu_price_history()
    print(f"  GPU history (forward-only): {len(gh)} days"
          + (f", latest {gh['date'].iloc[-1]}" if len(gh) else " (empty — accumulates from first snapshot)"))
    print("=" * 74)
    print("RESULT:", "OK" if ok else "INCOMPLETE")
    return ok


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="AISS alternative-data layer")
    ap.add_argument("--init-fred", "--update-fred", dest="fred", action="store_true",
                    help="fetch + persist all FRED alt-data series (backfillable)")
    ap.add_argument("--snapshot-gpu", action="store_true", help="append today's GPU pricing snapshot")
    ap.add_argument("--init-gpu", action="store_true", help="create empty GPU history parquet")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--start", default=FRED_HISTORY_START)
    args = ap.parse_args()

    did = False
    if args.fred:
        update_fred_altdata(start=args.start); did = True
    if args.init_gpu:
        init_gpu(); did = True
    if args.snapshot_gpu:
        snapshot_gpu(); did = True
    if args.verify or did:
        verify()
    if not did and not args.verify:
        print("Nothing to do. Use --init-fred / --snapshot-gpu / --init-gpu / --verify.")


if __name__ == "__main__":
    main()
