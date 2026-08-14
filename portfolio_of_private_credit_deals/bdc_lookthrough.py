"""
bdc_lookthrough.py — D4 §6.3: sleeve-level look-through over the real BDC underlying
loans. This is the core user-facing analytic ("what deals / issuers / sectors does our
BDC sleeve actually touch?").

Pipeline:
  load_bdc_deals (D2)  ->  SOI-mode credit scoring (bdc_credit, D4 §6.1)
                       ->  per-deal cash-flow metrics (bdc_cashflow, D4 §6.5)
                       ->  look-through aggregation (this module)

Look-through weight of a deal:
  lt_weight = BDC_ALLOC(0.50) × sleeve_w(bdc) × bdc_fv_share(deal)
  (bdc_fv_share = deal FV / Σtranche FV for its BDC; weights inside a BDC sum to 1, so
   the sleeve weight ties to the 50%-BDC allocation. companyfacts net is the QA anchor.)

Aggregations: top issuers (cross-BDC), sector/industry exposure, weighted spread /
all-in / PIK% / non-accrual% / mark distribution, maturity ladder, per-BDC summary,
and the credit-quality early-warning signals.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import bdc_credit
import bdc_cashflow
from bdc_deal_loader import load_bdc_deals, BDC_DEAL_CSV, _REPO_ROOT

# sleeve allocation — indexed from inventory_bdc.json (single source of truth
# since 2026-08-11), no longer hard-coded to 0.50.
import sys as _sys
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)
from bdc_inventory import load_inventory as _load_bdc_inventory

BDC_ALLOC = _load_bdc_inventory()["allocation"]["bdc"]


def _wavg(values, weights):
    v = pd.to_numeric(values, errors="coerce")
    w = pd.to_numeric(weights, errors="coerce")
    m = v.notna() & w.notna() & (w > 0)
    return float((v[m] * w[m]).sum() / w[m].sum()) if m.any() and w[m].sum() else None


def _maturity_ladder(maturity, weight, as_of):
    """Weighted maturity ladder (0-1y / 1-3y / 3-5y / 5y+)."""
    a = pd.Timestamp(as_of)
    yrs = pd.to_datetime(maturity.astype(str).where(maturity.astype(str).str.len() > 7,
                                                     maturity.astype(str) + "-01"),
                         errors="coerce").map(lambda d: (d - a).days / 365.25 if pd.notna(d) else None)
    bucket = pd.cut(yrs, [-1, 1, 3, 5, 100], labels=["0-1y", "1-3y", "3-5y", "5y+"])
    return weight.groupby(bucket, observed=False).sum().round(5).to_dict()


def compute_lookthrough(csv_path: str = BDC_DEAL_CSV, as_of: str | None = None,
                        run_cashflows: bool = True, bdc_non_accrual: dict | None = None) -> dict:
    deals = load_bdc_deals(csv_path)
    as_of = as_of or (deals["as_of"].dropna().iloc[0] if "as_of" in deals else None)

    # 1) credit scoring + attributes
    deals = bdc_credit.score_soi_deals(deals)
    deals = bdc_credit.attach_credit_attributes(deals)

    # 2) per-deal cash-flow metrics (IRR / cash-vs-PIK / OID / exit), floating loans
    #    re-priced on the forward SOFR curve (built once from fred_rates.csv + NS)
    if run_cashflows:
        fwd = bdc_cashflow.build_forward_sofr_curve(as_of)
        cfm = bdc_cashflow.portfolio_cashflow_metrics(deals, as_of, fwd_curve=fwd)
        deals = pd.concat([deals, cfm.add_prefix("cf_")], axis=1)

    # 3) look-through weights
    fv = pd.to_numeric(deals["fair_value"], errors="coerce").fillna(0.0)
    deals["lt_weight"] = (BDC_ALLOC * pd.to_numeric(deals["sleeve_w"], errors="coerce")
                          * pd.to_numeric(deals["bdc_fv_share"], errors="coerce")).fillna(0.0)
    w = deals["lt_weight"]
    pik = pd.to_numeric(deals["pik_rate"], errors="coerce").fillna(0.0)
    na = deals["non_accrual"] == True                                       # noqa: E712
    mark = pd.to_numeric(deals["fair_value"], errors="coerce") / \
        pd.to_numeric(deals["cost"], errors="coerce")

    # 4) aggregations
    by_issuer = (deals.assign(_w=w, _fv=fv)
                 .groupby("company")
                 .agg(lt_weight=("_w", "sum"), fair_value=("_fv", "sum"),
                      bdcs=("bdc", lambda s: sorted(set(s))),
                      sectors=("sector", lambda s: sorted({x for x in s if pd.notna(x)})))
                 .sort_values("lt_weight", ascending=False))
    import bdc_sector
    deals["_canon_sector"] = deals["sector"].map(bdc_sector.canonical_sector)
    by_sector = (deals.assign(_w=w).groupby("_canon_sector")["_w"].sum()
                 .sort_values(ascending=False))
    # G4: equal-weight-BDC sector view — each BDC's within-book sector mix averaged equally
    # across the 5 BDCs, so GBDC's 80% sleeve weight doesn't dominate the picture.
    fv_s = pd.to_numeric(deals["fair_value"], errors="coerce")
    per_bdc_sector = (deals.assign(_fv=fv_s)
                      .groupby(["bdc", "_canon_sector"])["_fv"].sum())
    bdc_tot = deals.assign(_fv=fv_s).groupby("bdc")["_fv"].sum()
    eq = (per_bdc_sector / bdc_tot).groupby("_canon_sector").mean().sort_values(ascending=False)
    by_bdc = deals.assign(_w=w, _fv=fv).groupby("bdc").agg(
        deals=("company", "size"), fv=("_fv", "sum"),
        wavg_spread=("spread", lambda s: _wavg(s, w.loc[s.index])),
        pik_pct=("pik_rate", lambda s: float((pd.to_numeric(s, errors="coerce") > 0).mean())))

    def _band(x):
        return ("<0.90" if x < 0.90 else "0.90-0.95" if x < 0.95 else
                "0.95-0.99" if x < 0.99 else "0.99-1.01" if x <= 1.01 else ">1.01")
    mark_dist = mark.dropna().map(_band).value_counts(normalize=True).round(3).to_dict()

    # G8: expected loss as a RATE = Σ expected_loss($) / Σ fair_value (not a wavg of $)
    el = pd.to_numeric(deals["expected_loss"], errors="coerce")
    el_rate = float(el.sum() / fv.sum()) if fv.sum() else None
    # G3: rate sensitivity — the book is mostly floating (reprices), so +50bps SOFR lifts
    # income ~floating_share×50bps with minimal price impact; fixed loans carry duration.
    floating_share = float(w[pd.to_numeric(deals["spread"], errors="coerce").notna()].sum())
    sleeve_floating = floating_share  # already lt-weighted
    # G5: sleeve-weighted BDC-level non-accrual (per-BDC rate × that BDC's lt weight)
    na_sleeve = None
    if bdc_non_accrual:
        bdc_w = deals.assign(_w=w).groupby("bdc")["_w"].sum()
        num = sum((bdc_non_accrual.get(t) or 0) * bw for t, bw in bdc_w.items()
                  if bdc_non_accrual.get(t) is not None)
        den = sum(bw for t, bw in bdc_w.items() if bdc_non_accrual.get(t) is not None)
        na_sleeve = round(num / den, 5) if den else None

    out = {
        "as_of": as_of, "deal_count": int(len(deals)),
        "issuer_count": int(deals["company"].nunique()),
        "sleeve_alloc": BDC_ALLOC,
        "weighted": {
            "spread": _wavg(deals["spread"], w),
            "all_in_rate": _wavg(deals["all_in_rate"], w),
            "credit_score": _wavg(deals["credit_score"], w),
            "pik_share_of_book": float((w[pik > 0].sum())),
            "non_accrual_pct_loans": float(w[na].sum()),          # per-loan flagged (P1.5)
            "non_accrual_pct_fv_bdc_disclosed": na_sleeve,        # G5: BDC-disclosed (P1)
            "expected_loss_rate": el_rate,                        # G8 fixed: EL$/FV
        },
        "rate_sensitivity": {                                     # G3
            "floating_share": round(sleeve_floating, 4),
            "fixed_share": round(float(w.sum()) - sleeve_floating, 4),
            "note": "+50bps SOFR ≈ +{:.1f}bps book all-in (floating reprices, low price duration; "
                    "fixed loans carry rate duration)".format(sleeve_floating /
                    (float(w.sum()) or 1) * 50),
        },
        "maturity_ladder": _maturity_ladder(deals["maturity"], w, as_of),  # G4
        "bdc_non_accrual": bdc_non_accrual or {},                 # G5 per-BDC
        "mark_distribution": mark_dist,
        "top_issuers": [
            {"company": idx, "lt_weight": round(row.lt_weight, 5),
             "fair_value": round(row.fair_value, 0), "bdcs": row.bdcs, "sectors": row.sectors}
            for idx, row in by_issuer.head(25).iterrows()],
        "sector_exposure": {k: round(v, 5) for k, v in by_sector.head(20).items()
                            if pd.notna(k)},
        "sector_exposure_equal_bdc": {k: round(v, 4) for k, v in eq.head(20).items()
                                      if pd.notna(k)},   # G4
        "by_bdc": {t: {"deals": int(r.deals), "fv": round(r.fv, 0),
                       "wavg_spread": (round(r.wavg_spread, 4) if r.wavg_spread else None),
                       "pik_pct": round(r.pik_pct, 3)}
                   for t, r in by_bdc.iterrows()},
        "early_warning": {
            "mark_below_90_weight": float(w[(mark < 0.90)].sum()),
            "pik_loans": int((pik > 0).sum()),
            "non_accrual_loans": int(na.sum()),
        },
    }
    if run_cashflows and "cf_irr" in deals:
        out["weighted"]["irr"] = _wavg(deals["cf_irr"], w)
        out["cash_vs_pik"] = {
            "cash_interest_total": float(pd.to_numeric(deals.get("cf_cash_interest_total"), errors="coerce").sum()),
            "pik_interest_total": float(pd.to_numeric(deals.get("cf_pik_interest_total"), errors="coerce").sum()),
        }
    return {"summary": out, "deals": deals}


if __name__ == "__main__":
    import argparse, json, time
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=BDC_DEAL_CSV)
    ap.add_argument("--no-cashflows", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    res = compute_lookthrough(a.csv, run_cashflows=not a.no_cashflows)
    print(f"computed in {time.time()-t0:.1f}s")
    print(json.dumps(res["summary"], indent=2, default=str)[:2600])
