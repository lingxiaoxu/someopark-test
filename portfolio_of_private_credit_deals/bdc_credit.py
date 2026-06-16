"""
bdc_credit.py — D4 §6.1: SOI-mode credit scoring for real BDC underlying loans.

The legacy credit score (run_deals.py:982-1011) needs EBITDA / leverage / revenue
growth — none of which the SEC Schedule of Investments discloses (those are the
borrower's private financials). So this module scores each loan from the信息 the
*market* and the *BDC* DO disclose in the SOI:

    base 60
      + mark   = FV/cost − 1            → ±25   (the strongest signal: the BDC's own
                                                 fair-value mark already prices credit)
      + spread = z vs same (bdc,type)   → ±15   (higher spread = market prices more risk)
      + pik    = PIK present / dominant  → −10 / −20  (classic stress tell)
      + non-accrual                      → −40   (forces the lowest band)
      + seniority                        → +8 … −12
      + affiliation (Control/Affiliated) → −5    (valuation discretion → conservative)

Output is a 0–120 score that feeds the SAME downstream recovery/PD/stress mapping as
the fundamental path (credit_risk_module.CreditRiskCashflowIntegrator), so nothing
downstream changes. SOI-mode is a MARKET-INFORMATION proxy — it is labelled `mode`
and must never be compared head-to-head with fundamental-mode scores.

This file is additive; credit_risk_module.py is untouched (fundamental path unchanged).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from credit_risk_module import CreditRiskCashflowIntegrator

SENIORITY_ADJ = {
    "First Lien": 8, "Senior Secured": 8, "Unitranche": 0, "Revolver": 4,
    "Second Lien": -8, "Subordinated": -12, "Unsecured": -10, "Equity": 0,
}


def score_soi_deals(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorised SOI-mode credit score (0–120) + mode label, added as columns.

    Equity rows get NaN score (they don't carry loan credit risk — handled as a
    separate FV-held bucket in the look-through)."""
    d = df.copy()
    fv = pd.to_numeric(d.get("fair_value"), errors="coerce")
    cost = pd.to_numeric(d.get("cost"), errors="coerce")
    spread = pd.to_numeric(d.get("spread"), errors="coerce")
    pik = pd.to_numeric(d.get("pik_rate"), errors="coerce").fillna(0.0)
    allin = pd.to_numeric(d.get("all_in_rate"), errors="coerce")
    is_equity = d.get("is_equity").astype("boolean").fillna(False) \
        if "is_equity" in d else pd.Series(False, index=d.index)
    non_accrual = d.get("non_accrual").astype("boolean").fillna(False) \
        if "non_accrual" in d else pd.Series(False, index=d.index)

    score = pd.Series(60.0, index=d.index)

    # 1) mark = FV/cost − 1  → ±25 (clip the mark band to ±20% before scaling)
    mark = (fv / cost.replace(0, np.nan)) - 1.0
    score = score + (mark.clip(-0.20, 0.20) / 0.20 * 25).fillna(0.0)

    # 2) spread z-score within (bdc, instrument) cohort → ±15 (high spread = lower score)
    cohort = d.groupby(["bdc", "instrument"])["spread"].transform(
        lambda s: (pd.to_numeric(s, errors="coerce") -
                   pd.to_numeric(s, errors="coerce").mean()) /
                  (pd.to_numeric(s, errors="coerce").std() or np.nan))
    score = score - (cohort.clip(-2, 2) / 2 * 15).fillna(0.0)

    # 3) PIK signal
    score = score - np.where(pik > 0, 10.0, 0.0)
    pik_frac = (pik / allin.replace(0, np.nan)).fillna(0.0)
    score = score - np.where(pik_frac > 0.5, 10.0, 0.0)        # extra −10 (−20 total)

    # 4) non-accrual → floor to lowest band
    score = score - np.where(non_accrual, 40.0, 0.0)

    # 5) seniority
    sen = d.get("instrument").map(SENIORITY_ADJ).fillna(0.0) if "instrument" in d else 0.0
    score = score + sen

    # 6) affiliation (Control / Affiliated, not Non-Affiliated)
    aff = d.get("affiliation").astype(str).str.lower() if "affiliation" in d else pd.Series("", index=d.index)
    is_affiliated = aff.str.contains("control|affiliat", na=False) & ~aff.str.contains("non-affiliat", na=False)
    score = score - np.where(is_affiliated, 5.0, 0.0)

    # 7) sector risk multiplier (§6.2): riskier sector (mult>1) lowers the score
    import bdc_sector
    sector_mult = d.get("sector").map(bdc_sector.risk_multiplier) if "sector" in d \
        else pd.Series(1.0, index=d.index)
    score = score - ((sector_mult.fillna(1.0) - 1.0) * 25)     # ±0.25 mult -> ∓6.25 pts

    score = score.clip(lower=0)
    score[is_equity] = np.nan                                  # equity → separate bucket
    d["credit_score"] = score.round(1)
    d["credit_mode"] = np.where(is_equity, "equity", "soi")
    return d


def attach_credit_attributes(df: pd.DataFrame,
                             base_default_prob: float = 0.03) -> pd.DataFrame:
    """Map credit_score → recovery / PD / stress / expected-loss via the SAME engine
    the fundamental path uses (CreditRiskCashflowIntegrator)."""
    integ = CreditRiskCashflowIntegrator(advanced_mode=True)
    d = df.copy()
    recs = []
    for _, r in d.iterrows():
        if r.get("credit_mode") == "equity" or pd.isna(r.get("credit_score")):
            recs.append({"default_prob": np.nan, "recovery_rate": np.nan,
                         "stress_multiplier": np.nan, "expected_loss": np.nan})
            continue
        loan = {
            "current_spread": float(r.get("spread") or 0.0),
            "default_prob": base_default_prob,
            "credit_score": float(r["credit_score"]),
            "risk_adjusted_spread": float(r.get("spread") or 0.0),
            "principal": (float(pd.to_numeric(r.get("principal"), errors="coerce"))
                          if pd.notna(pd.to_numeric(r.get("principal"), errors="coerce"))
                          else float(pd.to_numeric(r.get("fair_value"), errors="coerce") or 0.0)),
            "instrument": str(r.get("instrument")) if pd.notna(r.get("instrument")) else "",
            "rate_type": "floating" if pd.notna(r.get("spread")) else "fixed",
        }
        a = integ.get_credit_adjusted_attributes(loan)
        recs.append({"default_prob": a.default_prob, "recovery_rate": a.recovery_rate,
                     "stress_multiplier": a.stress_multiplier, "expected_loss": a.expected_loss})
    return pd.concat([d, pd.DataFrame(recs, index=d.index)], axis=1)
