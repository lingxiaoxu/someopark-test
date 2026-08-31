"""
AEUS PJM signals — CODE COMPLETE, WIRING GATED (AEUS_PLAN: 写代码不接线)
=======================================================================
PJM Western Hub is Data-Center-Alley's market.  Non-member Data Miner 2 API
access was requested 2026-08-30 (email to accountmanager@pjm.com per the API
guide); this module is fully written and activates the moment two things are
true:
  1. config.yaml external_sources.pjm.enabled = true
  2. PJM_API_KEY present in the repo-root .env

Until then every update returns 0 with a single clear log line, and loaders
return empty series (graceful-0 tilts, AISS convention).

Feeds (Data Miner 2, header Ocp-Apim-Subscription-Key; non-member ≤6 conn/min):
  da_hrl_lmps      Western Hub day-ahead hourly LMPs → daily mean → z252
                   (pairs with the ERCOT hub leg into hub_power_price)

COMPLIANCE (PJM terms, verified 2026-08-30): non-member data is for INTERNAL
BUSINESS USE ONLY — this store lives under gitignored price_data/ and must
NEVER be committed to the public repo.

CLI
---
    python -m electric_utilities_strategy.data.pjm_signals --init
    python -m electric_utilities_strategy.data.pjm_signals --update
    python -m electric_utilities_strategy.data.pjm_signals --verify
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
from datetime import date
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

log = logging.getLogger("aeus.pjm")

ALTDATA_DIR = pit.SEMI_DATA_DIR / "altdata"
PJM_LMP_PATH = ALTDATA_DIR / "pjm_da_lmp.json"

API_BASE = "https://api.pjm.com/api/v1"
LMP_FEED = "da_hrl_lmps"
WESTERN_HUB_ID = 51288                      # pnode_id, PJM Western Hub
HIST_START = "2016-01-01"
_REQ_SLEEP = 11.0                            # non-member ≤ 6 conn/min → ~5.5/min
_Z_WINDOW = 252


def _enabled(loud: bool = True) -> bool:
    try:
        import yaml
        cfg = yaml.safe_load((_THIS_DIR.parent / "config.yaml").read_text())
        if not cfg.get("external_sources", {}).get("pjm", {}).get("enabled", False):
            if loud:
                log.info("PJM disabled (config external_sources.pjm.enabled=false — "
                         "awaiting API key approval; AEUS_PLAN 写代码不接线)")
            return False
    except Exception:
        return False
    if not os.environ.get("PJM_API_KEY"):
        if loud:
            log.warning("PJM enabled in config but PJM_API_KEY missing in .env")
        return False
    return True


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


def update_da_lmp(refreeze: bool = False) -> int:
    """Western Hub DA hourly LMPs → daily mean, frozen append-only."""
    if not _enabled():
        return 0
    pit.ensure_dirs(); ALTDATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = {} if refreeze else pit.load_json(PJM_LMP_PATH, default={}).get("records", {})
    d0 = HIST_START if not existing else \
        (pd.Timestamp(max(existing)) - pd.Timedelta(days=7)).date().isoformat()
    frames = []
    cur, end = pd.Timestamp(d0), pd.Timestamp(date.today())
    while cur <= end:
        hi = min(cur + pd.Timedelta(days=180), end)
        rows = _api_get(LMP_FEED, {
            "rowCount": 50000, "startRow": 1,
            "datetime_beginning_ept": f"{cur.date()} 00:00 to {hi.date()} 23:59",
            "pnode_id": WESTERN_HUB_ID,
            "fields": "datetime_beginning_ept,total_lmp_da",
        })
        if rows:
            frames.append(pd.DataFrame(rows))
        time.sleep(_REQ_SLEEP)
        cur = hi + pd.Timedelta(days=1)
    if not frames:
        log.error("PJM DA LMP: no rows")
        return 0
    df = pd.concat(frames, ignore_index=True)
    df["d"] = pd.to_datetime(df["datetime_beginning_ept"]).dt.date.astype(str)
    df["total_lmp_da"] = pd.to_numeric(df["total_lmp_da"], errors="coerce")
    daily = df.groupby("d")["total_lmp_da"].mean().dropna()
    fresh = {k: round(float(v), 3) for k, v in daily.items()}
    records = pit.merge_frozen(existing, fresh)
    payload = {"meta": {"feed": LMP_FEED, "pnode_id": WESTERN_HUB_ID,
                        "updated_at": date.today().isoformat(),
                        "frozen_append_only": True, "n": len(records)},
               "records": records}
    pit.save_json(PJM_LMP_PATH, payload)
    log.info("PJM DA LMP: %d days, last %s $%.2f/MWh",
             len(records), max(records), records[max(records)])
    return len(records)


def load_pjm_hub_price(start=None, end=None) -> pd.Series:
    """PJM Western Hub DA price z252 (empty until wired — graceful-0)."""
    records = pit.load_json(PJM_LMP_PATH, default={}).get("records", {})
    if not records:
        return pd.Series(dtype="float64", name="pjm_hub_price")
    s = pd.Series({pd.Timestamp(k): float(v) for k, v in records.items()}).sort_index()
    z = (s - s.rolling(_Z_WINDOW, min_periods=_Z_WINDOW // 2).mean()) / \
        s.rolling(_Z_WINDOW, min_periods=_Z_WINDOW // 2).std().replace(0, np.nan)
    z = z.dropna()
    z.name = "pjm_hub_price"
    if start:
        z = z.loc[pd.Timestamp(start):]
    if end:
        z = z.loc[:pd.Timestamp(end)]
    return z


def verify() -> bool:
    print("=" * 70)
    print("AEUS PJM SIGNALS")
    print("=" * 70)
    wired = _enabled(loud=False)
    s = load_pjm_hub_price()
    if len(s):
        print(f"  pjm_hub_price : {len(s):5} pts →{s.index[-1].date()} z={s.iloc[-1]:+.2f}")
    else:
        state = "WIRED but no data yet" if wired else \
            "NOT WIRED (awaiting PJM_API_KEY + config flip — code complete)"
        print(f"  pjm_hub_price : EMPTY — {state}")
    print("=" * 70)
    print("RESULT:", "OK" if (len(s) or not wired) else "INCOMPLETE")
    return bool(len(s) or not wired)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="AEUS PJM signals (gated)")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--refreeze", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    did = False
    if args.init or args.update:
        update_da_lmp(refreeze=args.refreeze); did = True
    if args.verify or did:
        ok = verify()
        if args.verify and not ok:
            sys.exit(1)
    if not did and not args.verify:
        print("Nothing to do. Use --init / --update / --verify.")


if __name__ == "__main__":
    main()
