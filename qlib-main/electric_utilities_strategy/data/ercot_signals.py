"""
AEUS ERCOT signals (AEUS_PLAN §4.3)
===================================
Texas is the AI-load ground zero; ERCOT gives daily price/scarcity/fuel data
the EIA monthly stack cannot.  Two complementary channels:

  1. HISTORICAL BACKFILL via the credentialed Public API (api.ercot.com;
     ROPC token + subscription key — validated end-to-end 2026-08-30).
     Two decision-critical series (endpoints per the official api-specs):
       DAM settlement point prices  /np4-190-cd/dam_stlmnt_pnt_prices
         (settlementPoint=HB_HUBAVG → daily average hub price)
       DAM ancillary-service MCPCs  /np4-188-cd/dam_clear_price_for_cap
         (per-AS-type clearing prices → daily mean scarcity thermometer)

  2. DAILY ACCRUAL (fuel mix / demand / RT prices) — REUSED READ-ONLY from the
     macro module's dashboard ingest (prediction_market_macro/ingest/ercot.py →
     ``ercot_daily`` table in its sqlite store).  We never write their store;
     if absent, the loaders return empty (graceful-0 tilts).

PIT: DAM prices for delivery day D publish the prior day (day-ahead) — using
them ON or AFTER D is PIT-safe.  knowledge lag = 0 days (conservative +1).

Consumption (per §4.2 discipline — monthly sampling only, no intraday triggers):
  hub_power_price  → ②tilt ipp_wholesale (with PJM leg when enabled)
  as_scarcity      → ②tilt ipp + input to shortage/exposure amplifier

CLI
---
    python -m electric_utilities_strategy.data.ercot_signals --init
    python -m electric_utilities_strategy.data.ercot_signals --update
    python -m electric_utilities_strategy.data.ercot_signals --verify
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

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

log = logging.getLogger("aeus.ercot")

ALTDATA_DIR = pit.SEMI_DATA_DIR / "altdata"
DAM_SPP_PATH = ALTDATA_DIR / "ercot_dam_spp.json"
DAM_AS_PATH = ALTDATA_DIR / "ercot_dam_as.json"

TOKEN_URL = ("https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
             "B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token")
CLIENT_ID = "fec253ea-0d06-4272-a5e6-b478baeecd70"     # public ROPC client (official docs)
API_BASE = "https://api.ercot.com/api/public-reports"
SPP_ENDPOINT = "/np4-190-cd/dam_stlmnt_pnt_prices"
AS_ENDPOINT = "/np4-188-cd/dam_clear_price_for_cap"
HUB = "HB_HUBAVG"
HIST_START = "2016-01-01"
_REQ_SLEEP = 1.2                                        # self-throttle
_Z_WINDOW = 252

# macro 模块的 sqlite(只读复用;表 ercot_daily(date, metric, value, ...))
MACRO_DB = _PROJECT_DIR / "prediction_market_macro" / "data" / "macro.db"


def _enabled() -> bool:
    """Gated by config external_sources.ercot.enabled + env credentials."""
    try:
        import yaml
        cfg = yaml.safe_load((_THIS_DIR.parent / "config.yaml").read_text())
        if not cfg.get("external_sources", {}).get("ercot", {}).get("enabled", True):
            log.warning("ERCOT disabled in config external_sources")
            return False
    except Exception:
        pass
    if not (os.environ.get("ERCOT_API_SUBSCRIPTION_KEY")
            and os.environ.get("ERCOT_API_USERNAME")
            and os.environ.get("ERCOT_API_PASSWORD")):
        log.warning("ERCOT credentials missing in env (.env: ERCOT_API_*)")
        return False
    return True


_token_cache: dict = {}


def _id_token() -> Optional[str]:
    """OAuth2 ROPC id_token (cached ~50 min; validated live 2026-08-30)."""
    now = time.time()
    if _token_cache.get("exp", 0) > now + 60:
        return _token_cache["tok"]
    body = urllib.parse.urlencode({
        "grant_type": "password",
        "username": os.environ["ERCOT_API_USERNAME"],
        "password": os.environ["ERCOT_API_PASSWORD"],
        "scope": f"openid {CLIENT_ID} offline_access",
        "client_id": CLIENT_ID,
        "response_type": "id_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=45) as r:
        d = json.loads(r.read())
    tok = d.get("id_token")
    if not tok:
        log.error("ERCOT token failed: %s", str(d)[:150])
        return None
    _token_cache.update({"tok": tok, "exp": now + 50 * 60})
    return tok


def _api_get(endpoint: str, params: dict) -> dict:
    tok = _id_token()
    if not tok:
        return {}
    url = f"{API_BASE}{endpoint}?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {tok}",
        "Ocp-Apim-Subscription-Key": os.environ["ERCOT_API_SUBSCRIPTION_KEY"],
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:                          # free tier: 6 req/min
                time.sleep(12 * (attempt + 1))
                continue
            log.warning("ERCOT %s HTTP %s: %s", endpoint, e.code, str(e)[:120])
            return {}
        except Exception as e:  # noqa: BLE001
            log.warning("ERCOT %s failed: %s", endpoint, str(e)[:120])
            return {}
    return {}


def _rows_to_df(payload: dict) -> pd.DataFrame:
    """Public API returns {fields:[{name,...}], data:[[...], ...]}."""
    fields = [f["name"] for f in payload.get("fields", [])]
    data = payload.get("data", [])
    if not fields or not data:
        return pd.DataFrame()
    return pd.DataFrame(data, columns=fields)


def _paged_fetch(endpoint: str, base_params: dict, date_field: str,
                 d0: str, d1: str, chunk_days: int = 90) -> pd.DataFrame:
    """Chunked date-range fetch (each chunk ≤ page limit; throttled)."""
    frames = []
    cur = pd.Timestamp(d0)
    end = pd.Timestamp(d1)
    while cur <= end:
        hi = min(cur + pd.Timedelta(days=chunk_days - 1), end)
        p = dict(base_params)
        p[f"{date_field}From"] = cur.date().isoformat()
        p[f"{date_field}To"] = hi.date().isoformat()
        p["size"] = 1000000
        df = _rows_to_df(_api_get(endpoint, p))
        if len(df):
            frames.append(df)
        time.sleep(_REQ_SLEEP)
        cur = hi + pd.Timedelta(days=1)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ===========================================================================
# DAM hub price (daily average of hourly HB_HUBAVG SPPs)
# ===========================================================================

def update_dam_spp(refreeze: bool = False) -> int:
    if not _enabled():
        return 0
    pit.ensure_dirs(); ALTDATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = {} if refreeze else pit.load_json(DAM_SPP_PATH, default={}).get("records", {})
    d0 = HIST_START if not existing else \
        (pd.Timestamp(max(existing)) - pd.Timedelta(days=7)).date().isoformat()
    df = _paged_fetch(SPP_ENDPOINT, {"settlementPoint": HUB}, "deliveryDate",
                      d0, date.today().isoformat())
    if df.empty:
        log.error("DAM SPP: no rows (endpoint/params may need the api-spec check)")
        return 0
    dcol = "deliveryDate" if "deliveryDate" in df else df.columns[0]
    pcol = "settlementPointPrice" if "settlementPointPrice" in df else df.columns[-1]
    df[pcol] = pd.to_numeric(df[pcol], errors="coerce")
    daily = df.groupby(dcol)[pcol].mean().dropna()
    fresh = {str(k)[:10]: round(float(v), 3) for k, v in daily.items()}
    records = pit.merge_frozen(existing, fresh)
    payload = {"meta": {"endpoint": SPP_ENDPOINT, "hub": HUB,
                        "updated_at": date.today().isoformat(),
                        "frozen_append_only": True, "n": len(records)},
               "records": records}
    pit.save_json(DAM_SPP_PATH, payload)
    log.info("ERCOT DAM SPP: %d days, last %s $%.2f/MWh",
             len(records), max(records), records[max(records)])
    return len(records)


def load_hub_power_price(start=None, end=None) -> pd.Series:
    """ERCOT DAM hub price z252 (daily; PIT-safe on/after delivery day)."""
    records = pit.load_json(DAM_SPP_PATH, default={}).get("records", {})
    if not records:
        return pd.Series(dtype="float64", name="ercot_hub_price")
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in records.items()}).sort_index()
    z = (s - s.rolling(_Z_WINDOW, min_periods=_Z_WINDOW // 2).mean()) / \
        s.rolling(_Z_WINDOW, min_periods=_Z_WINDOW // 2).std().replace(0, np.nan)
    z = z.dropna()
    z.name = "ercot_hub_price"
    if start:
        z = z.loc[pd.Timestamp(start):]
    if end:
        z = z.loc[:pd.Timestamp(end)]
    return z


# ===========================================================================
# DAM ancillary-service clearing prices (scarcity thermometer)
# ===========================================================================

def update_dam_as(refreeze: bool = False) -> int:
    if not _enabled():
        return 0
    pit.ensure_dirs(); ALTDATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = {} if refreeze else pit.load_json(DAM_AS_PATH, default={}).get("records", {})
    d0 = HIST_START if not existing else \
        (pd.Timestamp(max(existing)) - pd.Timedelta(days=7)).date().isoformat()
    df = _paged_fetch(AS_ENDPOINT, {}, "deliveryDate", d0, date.today().isoformat())
    if df.empty:
        log.error("DAM AS: no rows (endpoint/params may need the api-spec check)")
        return 0
    dcol = "deliveryDate" if "deliveryDate" in df else df.columns[0]
    num_cols = [c for c in df.columns
                if pd.to_numeric(df[c], errors="coerce").notna().mean() > 0.9
                and c not in (dcol, "hourEnding")]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    daily = df.groupby(dcol)[num_cols].mean().mean(axis=1).dropna()
    fresh = {str(k)[:10]: round(float(v), 3) for k, v in daily.items()}
    records = pit.merge_frozen(existing, fresh)
    payload = {"meta": {"endpoint": AS_ENDPOINT,
                        "note": "daily mean across AS types & hours",
                        "updated_at": date.today().isoformat(),
                        "frozen_append_only": True, "n": len(records)},
               "records": records}
    pit.save_json(DAM_AS_PATH, payload)
    log.info("ERCOT DAM AS: %d days, last %s $%.2f", len(records), max(records),
             records[max(records)])
    return len(records)


def load_as_scarcity(start=None, end=None) -> pd.Series:
    """AS clearing price z252 — 备用容量价格比电能价格先动的稀缺温度计."""
    records = pit.load_json(DAM_AS_PATH, default={}).get("records", {})
    if not records:
        return pd.Series(dtype="float64", name="ercot_as_scarcity")
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in records.items()}).sort_index()
    z = (s - s.rolling(_Z_WINDOW, min_periods=_Z_WINDOW // 2).mean()) / \
        s.rolling(_Z_WINDOW, min_periods=_Z_WINDOW // 2).std().replace(0, np.nan)
    z = z.dropna()
    z.name = "ercot_as_scarcity"
    if start:
        z = z.loc[pd.Timestamp(start):]
    if end:
        z = z.loc[:pd.Timestamp(end)]
    return z


# ===========================================================================
# Dashboard accrual (macro 模块只读复用)
# ===========================================================================

def load_macro_ercot_metric(metric: str) -> pd.Series:
    """READ-ONLY from prediction_market_macro's ercot_daily table (fuel mix /
    supply-demand / RT prices, accruing forward daily).  Absent → empty."""
    if not MACRO_DB.exists():
        return pd.Series(dtype="float64", name=metric)
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{MACRO_DB}?mode=ro", uri=True)
        df = pd.read_sql_query(
            "SELECT date, value FROM ercot_daily WHERE metric = ? ORDER BY date",
            con, params=(metric,))
        con.close()
    except Exception as e:  # noqa: BLE001
        log.warning("macro ercot_daily read failed: %s", e)
        return pd.Series(dtype="float64", name=metric)
    if df.empty:
        return pd.Series(dtype="float64", name=metric)
    s = pd.Series(df["value"].values, index=pd.to_datetime(df["date"]))
    s.name = metric
    return s


# ===========================================================================
# Verify / CLI
# ===========================================================================

def verify() -> bool:
    print("=" * 70)
    print("AEUS ERCOT SIGNALS")
    print("=" * 70)
    ok = True
    for name, s in (("hub_power_price", load_hub_power_price()),
                    ("as_scarcity", load_as_scarcity())):
        if len(s):
            tag = pit.stale_tag(s.index[-1].date(), "daily")
            print(f"  {name:16}: {len(s):5} pts →{s.index[-1].date()} z={s.iloc[-1]:+.2f}{tag}")
            if tag:
                ok = False
        else:
            print(f"  {name:16}: EMPTY")
            ok = False
    n_macro = 0
    if MACRO_DB.exists():
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{MACRO_DB}?mode=ro", uri=True)
            n_macro = con.execute("SELECT COUNT(*) FROM ercot_daily").fetchone()[0]
            con.close()
        except Exception:
            pass
    print(f"  macro accrual   : {n_macro} rows (read-only, fuel-mix/demand/RT)")
    print("=" * 70)
    print("RESULT:", "OK" if ok else "INCOMPLETE")
    return ok


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="AEUS ERCOT signals")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--refreeze", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    did = False
    if args.init or args.update:
        update_dam_spp(refreeze=args.refreeze)
        update_dam_as(refreeze=args.refreeze)
        did = True
    _ok = True
    if args.verify or did:
        _ok = verify()
    if not did and not args.verify:
        print("Nothing to do. Use --init / --update / --verify.")
    if args.verify and not _ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
