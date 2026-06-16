"""
bdc_calibration.py — D4 §6.6: reposition the two auxiliary modules onto the REAL
BDC look-through book instead of arbitrary synthetic assumptions.

  bdc_to_enhanced_loans(deals)  — adapter: real SOI deals -> enriched_bond_portfolio's
                                  EnhancedLoanSpec list, so its exposure/OU analytics can
                                  run on the real book (not synthetic_data/*.csv).
  calibrate_synthetic(deals)    — derive the real book's sector mix / spread distribution
                                  (by seniority) / term distribution / PIK incidence, and
                                  emit a calibrated synthetic-loan CSV so run_synthetic's
                                  Monte-Carlo stress scenarios reflect reality.

Note (honest scope, §6.6): enriched_bond_portfolio's OU *mark-to-market* return modelling
needs a market return series, which private BDC loans do not have — so the portfolio-
EXPOSURE role (sector/issuer/concentration/weighted metrics) is served by the purpose-built
bdc_lookthrough.py; this adapter covers the structural fields. run_synthetic calibration is
the fully-applicable repositioning.
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

import bdc_sector

_SYN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "synthetic_data")


def _rating_from_score(score) -> str:
    if pd.isna(score):
        return "NR"
    s = float(score)
    return ("BB" if s >= 90 else "BB-" if s >= 80 else "B+" if s >= 70 else
            "B" if s >= 60 else "B-" if s >= 50 else "CCC")


def _months_to_maturity(maturity, as_of) -> int:
    try:
        m = pd.Timestamp(str(maturity) if len(str(maturity)) > 7 else f"{maturity}-01")
        return max(6, int(round((m - pd.Timestamp(as_of)).days / 30.44)))
    except Exception:  # noqa: BLE001
        return 60


def bdc_to_enhanced_loans(deals: pd.DataFrame, as_of: str | None = None) -> list:
    """Real SOI deals -> EnhancedLoanSpec list (structural fields from real data;
    analytics-only fields get neutral defaults)."""
    from enriched_bond_portfolio import EnhancedLoanSpec
    as_of = as_of or (deals["as_of"].dropna().iloc[0] if "as_of" in deals else "2026-03-31")
    out = []
    for _, r in deals.iterrows():
        if r.get("credit_mode") == "equity":
            continue
        spread = pd.to_numeric(r.get("spread"), errors="coerce")
        prin = pd.to_numeric(r.get("principal"), errors="coerce")
        fv = pd.to_numeric(r.get("fair_value"), errors="coerce")
        out.append(EnhancedLoanSpec(
            loan_id=str(r.get("deal_uid")), borrower=str(r.get("company")),
            sector=bdc_sector.canonical_sector(r.get("sector")),
            principal=float(prin if pd.notna(prin) else (fv if pd.notna(fv) else 0.0)),
            rate_type="floating" if pd.notna(spread) else "fixed",
            base_rate="SOFR" if pd.notna(spread) else "none",
            spread=float(spread) if pd.notna(spread) else 0.0,
            origination_date=str(as_of),
            maturity_months=_months_to_maturity(r.get("maturity"), as_of),
            io_months=0, amort_style="annuity",
            origination_rate=float(pd.to_numeric(r.get("all_in_rate"), errors="coerce") or 0.09),
            current_spread=float(spread) if pd.notna(spread) else 0.0,
            ltv=0.45, dscr=1.8,                       # not disclosed in SOI; neutral defaults
            credit_rating=_rating_from_score(r.get("credit_score")),
            prepay_penalty=0.01,
            default_prob=float(pd.to_numeric(r.get("default_prob"), errors="coerce") or 0.03),
            seniority=str(r.get("instrument") or "First Lien"),
            collateral_type="senior_secured", geography="US",
            vintage_yield=float(pd.to_numeric(r.get("all_in_rate"), errors="coerce") or 0.09),
            coupon_freq="quarterly",
            floating_reset_freq="quarterly" if pd.notna(spread) else "none",
        ))
    return out


def calibrate_synthetic(deals: pd.DataFrame, n: int = 150, seed: int = 7,
                        write: bool = True) -> dict:
    """Derive the real book's distributions and emit a calibrated synthetic-loan CSV."""
    d = deals[deals.get("credit_mode") != "equity"].copy()
    d["_sector"] = d["sector"].map(bdc_sector.canonical_sector)
    spread = pd.to_numeric(d["spread"], errors="coerce")
    pik = pd.to_numeric(d["pik_rate"], errors="coerce").fillna(0.0)
    term = d["maturity"].map(lambda m: _months_to_maturity(m, "2026-03-31"))
    stats = {
        "sector_mix": (d["_sector"].value_counts(normalize=True).round(4).to_dict()),
        "spread_mean": float(spread.mean()), "spread_std": float(spread.std()),
        "term_months_mean": float(term.mean()), "term_months_std": float(term.std()),
        "pik_incidence": float((pik > 0).mean()),
        "floating_share": float(spread.notna().mean()),
    }
    rng = np.random.default_rng(seed)
    sectors = list(stats["sector_mix"]); probs = list(stats["sector_mix"].values())
    rows = []
    for i in range(n):
        is_float = rng.random() < stats["floating_share"]
        spr = max(0.01, rng.normal(stats["spread_mean"], stats["spread_std"] or 0.01))
        rows.append({
            "loan_id": f"SYN_BDC_{i:04d}",
            "borrower": f"Calibrated Borrower {i}",
            "sector": rng.choice(sectors, p=probs),
            "principal": float(rng.integers(2, 80) * 1_000_000),
            "rate_type": "floating" if is_float else "fixed",
            "base_rate": "SOFR" if is_float else "none",
            "spread": round(spr, 4),
            "maturity_months": int(max(12, rng.normal(stats["term_months_mean"],
                                                      stats["term_months_std"] or 6))),
            "pik_rate": round(rng.uniform(0.02, 0.10), 4) if rng.random() < stats["pik_incidence"] else 0.0,
        })
    if write:
        os.makedirs(_SYN_DIR, exist_ok=True)
        path = os.path.join(_SYN_DIR, "synthetic_loans_bdc_calibrated.csv")
        pd.DataFrame(rows).to_csv(path, index=False)
        stats["written"] = path
    return stats


if __name__ == "__main__":
    import argparse, json
    from bdc_deal_loader import load_bdc_deals
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    deals = load_bdc_deals(a.csv)
    import bdc_credit
    deals = bdc_credit.attach_credit_attributes(bdc_credit.score_soi_deals(deals))
    enhanced = bdc_to_enhanced_loans(deals)
    print(f"adapter: {len(enhanced)} real deals -> EnhancedLoanSpec")
    stats = calibrate_synthetic(deals, write=not a.dry_run)
    print("calibration stats:")
    print(json.dumps({k: v for k, v in stats.items() if k != "sector_mix"}, indent=2, default=str))
    print("sector_mix:", json.dumps(stats["sector_mix"], default=str))
