"""
bdc_deal_loader.py — D2: bridge the SEC SOI snapshots (produced by the repo-root
RefreshBDCHoldings.py) into the private-credit module's deal contract.

Reads the PIT snapshots under price_data/bdc_holdings/{TICKER}/ (via latest_manifest)
and emits ONE combined deal table that is:
  * column-compatible with the module's legacy deal CSV
    (company / sector / instrument / currency / deal_size / coupon / maturity /
     ebitda / leverage / rev / risks) so run_deals.py's parsers keep working, PLUS
  * the full set of real SOI fields (fair_value / cost / spread / pik_rate /
    rate_floor / pct_nav / bdc / sleeve_w / bdc_fv_share / deal_uid / as_of / adsh /
    is_equity / non_accrual / unfunded / maturity_source / industry_source).

The module consumes deals through load_bdc_deals(); the daily pipeline writes the CSV
via write_bdc_deal_start(). No network, no module-internal rewrite — only an additive
loader (legacy load_csv_data path is untouched).
"""

from __future__ import annotations

import json
import os
import warnings

import numpy as np
import pandas as pd

# all-NA columns (e.g. affiliation) trip a pandas concat dtype-deprecation warning; harmless
warnings.filterwarnings("ignore", message=".*concatenation with empty or all-NA.*")

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_MODULE_DIR)
BDC_STORE = os.path.join(_REPO_ROOT, "price_data", "bdc_holdings")
DEALS_DIR = os.path.join(_MODULE_DIR, "deals_data")
BDC_DEAL_CSV = os.path.join(DEALS_DIR, "bdc_deal_start.csv")

# Legacy module deal columns (kept first, in order, for drop-in compatibility).
LEGACY_COLS = ["company", "sector", "instrument", "currency", "deal_size", "coupon",
               "maturity", "ebitda", "leverage", "rev", "risks"]
# Real SOI columns carried alongside (the module's bdc-mode reads these directly).
SOI_COLS = ["bdc", "sleeve_w", "bdc_fv_share", "deal_uid", "as_of", "adsh",
            "fair_value", "cost", "principal", "all_in_rate", "spread", "rate_floor",
            "pik_rate", "pct_nav", "affiliation", "is_equity", "unfunded", "non_accrual",
            "industry", "industry_source", "maturity_source"]


# ── snapshot loading ────────────────────────────────────────────────────────
def load_bdc_snapshots(store: str = BDC_STORE) -> pd.DataFrame:
    """Concatenate the latest per-BDC SOI snapshots named in latest_manifest.json."""
    manifest_path = os.path.join(store, "latest_manifest.json")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"no BDC manifest at {manifest_path} (run RefreshBDCHoldings)")
    manifest = json.load(open(manifest_path))
    frames = []
    for t, mf in manifest.items():
        snap = os.path.join(store, t, f"soi_{mf['reportDate']}_{mf['adsh']}.parquet")
        if os.path.exists(snap):
            frames.append(pd.read_parquet(snap))
    if not frames:
        raise FileNotFoundError(f"manifest names {list(manifest)} but no snapshot parquet found")
    # align columns across snapshots (some carry all-NA cols, e.g. affiliation) to avoid the
    # pandas concat all-NA-dtype FutureWarning, then one clean concat.
    cols = list(dict.fromkeys(c for f in frames for c in f.columns))
    return pd.concat([f.reindex(columns=cols) for f in frames], ignore_index=True)


# ── coupon synthesis (back-compat with run_deals.parse_coupon_rate) ─────────
def _coupon_str(row) -> str:
    """Build a coupon string the legacy parser understands; the precise numeric
    fields (spread/all_in_rate/rate_floor/pik_rate) travel in their own columns."""
    spr, allin = row.get("spread"), row.get("all_in_rate")
    if pd.notna(spr):
        bps = int(round(float(spr) * 1e4))
        s = f"SOFR + {bps} bps"
        if pd.notna(row.get("rate_floor")):
            s += f" (floor {float(row['rate_floor'])*100:.2f}%)"
        return s
    if pd.notna(allin):
        return f"{float(allin)*100:.3f}%"
    return ""                                     # equity / no-rate rows


def _risks_str(row) -> str:
    tags = []
    if row.get("non_accrual") is True:
        tags.append("NON-ACCRUAL")
    if pd.notna(row.get("pik_rate")) and float(row.get("pik_rate") or 0) > 0:
        tags.append(f"PIK {float(row['pik_rate'])*100:.2f}%")
    aff = row.get("affiliation")
    if isinstance(aff, str) and aff and "non-affiliated" not in aff.lower():
        tags.append(aff)
    if row.get("is_equity") is True or row.get("is_equity") == "True":
        tags.append("equity")
    return "; ".join(tags)


def to_deal_schema(snap: pd.DataFrame) -> pd.DataFrame:
    """Map combined SOI snapshots -> module deal schema (+ real SOI columns)."""
    d = pd.DataFrame(index=snap.index)
    d["company"] = snap["issuer"]
    d["sector"] = snap["industry"]                # raw SOI industry; D4 maps -> module sector
    d["instrument"] = snap["inv_type"]
    d["currency"] = "USD"                         # SOI rows are USD; FX rows flagged in P1.5
    # deal_size = principal for debt, fair_value for equity / principal-less rows
    prin = pd.to_numeric(snap["principal"], errors="coerce")
    fv = pd.to_numeric(snap["fair_value"], errors="coerce")
    d["deal_size"] = prin.where(prin.notna(), fv)
    d["coupon"] = snap.apply(_coupon_str, axis=1)
    d["maturity"] = snap["maturity"]             # 'YYYY-MM' (legacy parser takes the year)
    d["ebitda"] = np.nan                          # SOI does not disclose borrower financials
    d["leverage"] = ""
    d["rev"] = ""
    d["risks"] = snap.apply(_risks_str, axis=1)
    for c in SOI_COLS:
        d[c] = snap[c] if c in snap.columns else np.nan
    return d[LEGACY_COLS + SOI_COLS]


# ── public API ──────────────────────────────────────────────────────────────
def write_bdc_deal_start(store: str = BDC_STORE, out_csv: str = BDC_DEAL_CSV) -> str:
    snap = load_bdc_snapshots(store)
    deals = to_deal_schema(snap)
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    deals.to_csv(out_csv, index=False)
    return out_csv


def load_bdc_deals(csv_path: str = BDC_DEAL_CSV) -> pd.DataFrame:
    """Read the BDC deal table for the module (run_deals bdc-mode / look-through)."""
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"{csv_path} missing — run write_bdc_deal_start first")
    return pd.read_csv(csv_path)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Build the BDC deal table from SOI snapshots")
    ap.add_argument("--store", default=BDC_STORE)
    ap.add_argument("--out", default=BDC_DEAL_CSV)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    snap = load_bdc_snapshots(a.store)
    deals = to_deal_schema(snap)
    print(f"combined {len(deals)} deals from {snap['bdc'].nunique()} BDCs")
    print(deals[["company", "sector", "instrument", "coupon", "maturity",
                 "deal_size", "fair_value", "bdc", "bdc_fv_share"]].head(8).to_string())
    if not a.dry_run:
        out = write_bdc_deal_start(a.store, a.out)
        print(f"\nwrote {out}")
    else:
        print("\n[dry-run] nothing written")
