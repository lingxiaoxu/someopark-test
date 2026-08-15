"""
bdc_stress.py — scenario stress engine for the BDC look-through book (§7.3).

Runs the REAL loan book (bdc_deal_start.csv, post credit-scoring) through a fixed
matrix of rate and macro-credit scenarios, the way a private-credit IC deck does:

  Rate ladder (income/floor channel, credit held flat):
      rates_+100 / +200 / +300      parallel up-shift of the forward SOFR curve
      rates_-100 / -200 / -300      parallel cut, curve floored at 0 (loan-level
                                    rate floors then bind inside the repricer)
  Macro scenarios (all three channels move together):
      mild_recession    rates −100 | PD excess ×1.75 | recovery −10pt | spreads +250bp | equity marks −10%
      severe_recession  rates −300 | PD excess ×3.00 | recovery −20pt | spreads +600bp | equity marks −30%
      stagflation       rates +300 | PD excess ×2.50 | recovery −15pt | spreads +400bp | equity marks −20%
                        (the worst case for a floating book: coverage crushed at peak rates)

Loan-level PD under stress finally CONSUMES the per-loan score-based stress
multiplier (credit_risk_module, 0.8–2.0) that has been computed daily since D4
but never read:  PD_s = clip( PD_base × (1 + (f−1)·mult), 0.5%, 45% )
— a resilient name (mult 0.8) sees 80% of the scenario's excess stress, a
vulnerable one (mult 2.0) sees double. LGD_s = 1 − clip(recovery − haircut).

Valuation method — survival-weighted expected cashflows (actuarial standard):
  * each loan is repriced per scenario on the shifted forward curve via the SAME
    engine the daily numbers use (bdc_cashflow.bdc_loan_cashflow: per-period
    floating repricing, floors, PIK compounding, non-accrual coupon stripping);
  * quarterly hazard h_t = 1−(1−PD_s)^yf;  survival chain S_t;
  * expected CF_t = S_{t−1}·[ (1−h_t)·CF_t + h_t·recovery_s·outstanding_t ];
  * the terminal PIK/unamortized balloon is repaid at maturity (contractual),
    so PIK economics enter the stressed view even though the base cash-only IRR
    (deliberately) excludes them;
  * entry leg = −fair_value at as_of (mark entry: what the sleeve pays TODAY),
    so the scenario net IRR is a forward-looking expected net return, not a
    par-cost artefact.
  * portfolio net IRR = XIRR of the SUMMED dollar flows (no per-loan Jensen).

Equity rows (no coupon, no PD) are not run through the loan engine; macro
scenarios apply an explicit mark haircut to their FV (labelled), rate-only
scenarios leave them unchanged.

Reported per scenario (all lt-book dollars, rates as decimals):
  net_irr        expected net IRR of the whole loan book, mark entry
  nii_1y         survival-weighted cash interest collected in the first year
  el_1y_rate     Σ PD_s·LGD_s·EAD / Σ FV  (1-year expected-loss rate)
  delta_ev_pct   (scenario expected PV + stressed equity) vs base, / (Σ FV + equity)
  floor_bound_share   FV share of floating loans whose floor binds ≥1 period
  pd_wavg / recovery_wavg   book-level stressed parameters (FV-weighted)

Runs inside RunBDCLookThrough (STEP D, 16:05 ET daily) right after the base
look-through; results land in the SAME summary/daily-report JSONs under "stress".
Env: someopark_run. Pure consumer — writes nothing itself.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import bdc_cashflow
from bond_utilities import calculate_xirr

# ── scenario matrix ─────────────────────────────────────────────────────────
# (rate_shift_bp, pd_excess_factor, recovery_haircut, spread_widen, equity_haircut)
SCENARIOS: dict[str, dict] = {
    "base":             dict(shift=0,    pd_f=1.00, rec_hc=0.00, widen=0.000, eq_hc=0.00),
    "rates_+100":       dict(shift=100,  pd_f=1.00, rec_hc=0.00, widen=0.000, eq_hc=0.00),
    "rates_+200":       dict(shift=200,  pd_f=1.00, rec_hc=0.00, widen=0.000, eq_hc=0.00),
    "rates_+300":       dict(shift=300,  pd_f=1.00, rec_hc=0.00, widen=0.000, eq_hc=0.00),
    "rates_-100":       dict(shift=-100, pd_f=1.00, rec_hc=0.00, widen=0.000, eq_hc=0.00),
    "rates_-200":       dict(shift=-200, pd_f=1.00, rec_hc=0.00, widen=0.000, eq_hc=0.00),
    "rates_-300":       dict(shift=-300, pd_f=1.00, rec_hc=0.00, widen=0.000, eq_hc=0.00),
    "mild_recession":   dict(shift=-100, pd_f=1.75, rec_hc=0.10, widen=0.025, eq_hc=0.10),
    "severe_recession": dict(shift=-300, pd_f=3.00, rec_hc=0.20, widen=0.060, eq_hc=0.30),
    "stagflation":      dict(shift=300,  pd_f=2.50, rec_hc=0.15, widen=0.040, eq_hc=0.20),
}
_PD_FLOOR, _PD_CAP = 0.005, 0.45
_REC_FLOOR, _REC_CAP = 0.05, 0.95


def _shifted_curve(base_curve: pd.Series, shift_bp: int) -> pd.Series:
    """Parallel shift of the forward-SOFR Series; SOFR itself floored at 0
    (loan-level contractual floors are applied inside the repricer)."""
    return (base_curve + shift_bp / 1e4).clip(lower=0.0)


def _loan_arrays(row, as_of: str, curve: pd.Series):
    """One repriced schedule → the numpy pieces the hazard layer needs.

    Returns (dates, cf, cash_int, out0, yf, floor_bound). cf already carries the
    terminal balloon (unamortized principal + PIK accretion repaid at maturity)."""
    sched, _ = bdc_cashflow.bdc_loan_cashflow(row, as_of, fwd_curve=curve)
    body = sched.iloc[1:]                                  # row 0 = funding leg, replaced by −FV
    cf = body["total_cashflow"].to_numpy(dtype=float).copy()
    if len(cf):
        cf[-1] += float(body["outstanding_end"].iloc[-1])  # contractual balloon at maturity
    return (body.index.to_numpy(),
            cf,
            body["CashInterest"].to_numpy(dtype=float),
            body["outstanding_start"].to_numpy(dtype=float),
            body["period_year_fraction"].to_numpy(dtype=float),
            bool(body["FloorApplied"].any()))


def _expected_flows(cf, cash_int, out0, yf, pd_s: float, rec_s: float):
    """Survival-weighted expected cashflows + expected cash interest (vectorised)."""
    h = 1.0 - np.power(1.0 - pd_s, np.clip(yf, 0.0, None))
    surv_prev = np.concatenate(([1.0], np.cumprod(1.0 - h)))[:-1]   # S_{t-1}
    ecf = surv_prev * ((1.0 - h) * cf + h * rec_s * out0)
    eint = surv_prev * (1.0 - h) * cash_int
    return ecf, eint


def run_stress_matrix(deals: pd.DataFrame, as_of: str,
                      base_curve: pd.Series | None = None) -> dict:
    """Scenario matrix over the enriched deal frame (post score/attrs/lt_weight)."""
    if base_curve is None:
        base_curve = bdc_cashflow.build_forward_sofr_curve(as_of)
    if base_curve is None:
        raise RuntimeError("no forward SOFR curve — fred_rates.csv missing?")

    loans = deals[deals["credit_mode"] != "equity"].copy()
    equity_fv = float(pd.to_numeric(
        deals.loc[deals["credit_mode"] == "equity", "fair_value"], errors="coerce").sum())
    fv = pd.to_numeric(loans["fair_value"], errors="coerce").fillna(0.0).to_numpy()
    ead = pd.to_numeric(loans["principal"], errors="coerce").fillna(0.0).to_numpy()
    ead = np.where(ead > 0, ead, fv)                       # EAD fallback = FV
    pd_base = pd.to_numeric(loans["default_prob"], errors="coerce").fillna(0.03).to_numpy()
    rec_base = pd.to_numeric(loans["recovery_rate"], errors="coerce").fillna(0.40).to_numpy()
    mult = pd.to_numeric(loans["stress_multiplier"], errors="coerce").fillna(1.0).to_numpy()
    spread = pd.to_numeric(loans["spread"], errors="coerce").to_numpy()
    allin = pd.to_numeric(loans["all_in_rate"], errors="coerce").fillna(0.10).to_numpy()
    is_float = ~np.isnan(spread)
    fv_total = float(fv.sum())

    # ── schedule cache: floating loans reprice per DISTINCT curve; fixed loans once ──
    shifts = sorted({s["shift"] for s in SCENARIOS.values()})
    curves = {sh: _shifted_curve(base_curve, sh) for sh in shifts}
    horizon_1y = pd.Timestamp(as_of) + pd.DateOffset(years=1)
    rows = list(loans.itertuples(index=False))
    cols = {c: i for i, c in enumerate(loans.columns)}

    def _row_dict(t):                                       # itertuples → the dict-like the engine reads
        return {c: t[i] for c, i in cols.items()}

    sched_fixed: list = [None] * len(rows)                  # fixed-rate: curve-invariant
    sched_float: dict[int, list] = {sh: [None] * len(rows) for sh in shifts}
    for i, t in enumerate(rows):
        r = _row_dict(t)
        if is_float[i]:
            for sh in shifts:
                sched_float[sh][i] = _loan_arrays(r, as_of, curves[sh])
        else:
            sched_fixed[i] = _loan_arrays(r, as_of, curves[0])

    # base-scenario expected PV (denominator/anchor for delta_ev)
    def _pv(dates, ecf, y: float, t0: pd.Timestamp) -> float:
        yrs = (pd.DatetimeIndex(dates) - t0).days / 365.0
        return float(np.sum(ecf / np.power(1.0 + max(y, -0.99), yrs)))

    t0 = pd.Timestamp(as_of)
    out_scen: dict[str, dict] = {}
    pv_base_total: float | None = None

    for name, sc in SCENARIOS.items():
        pd_s_v = np.clip(pd_base * (1.0 + (sc["pd_f"] - 1.0) * mult), _PD_FLOOR, _PD_CAP)
        rec_s_v = np.clip(rec_base - sc["rec_hc"], _REC_FLOOR, _REC_CAP)
        curve = curves[sc["shift"]]
        fwd_avg = float(curve.iloc[:20].mean())            # 5y average base rate for discounting

        agg: dict[pd.Timestamp, float] = {}
        nii_1y = 0.0
        pv_total = 0.0
        floor_fv = 0.0
        for i in range(len(rows)):
            dates, cf, cash_int, out0, yf, floored = \
                (sched_float[sc["shift"]][i] if is_float[i] else sched_fixed[i])
            ecf, eint = _expected_flows(cf, cash_int, out0, yf, pd_s_v[i], rec_s_v[i])
            # per-loan discount yield: floating = scenario base + spread (+widen);
            # fixed = its own all-in (+widen) — market-yield loan pricing convention
            y = (fwd_avg + spread[i] if is_float[i] else allin[i]) + sc["widen"]
            pv_total += _pv(dates, ecf, y, t0)
            in_1y = pd.DatetimeIndex(dates) <= horizon_1y
            nii_1y += float(eint[in_1y].sum())
            if floored:
                floor_fv += fv[i]
            for d, v in zip(dates, ecf):
                agg[d] = agg.get(d, 0.0) + float(v)

        flows = sorted(agg.items())
        irr_flows = [(t0, -fv_total)] + [(pd.Timestamp(d), v) for d, v in flows]
        net_irr = calculate_xirr(irr_flows, initial_guess=0.10)

        el_1y = float(np.sum(pd_s_v * (1.0 - rec_s_v) * ead))
        eq_stressed = equity_fv * (1.0 - sc["eq_hc"])
        ev = pv_total + eq_stressed
        if name == "base":
            pv_base_total = ev
        out_scen[name] = {
            "net_irr": round(net_irr, 6),
            "nii_1y": round(nii_1y, 0),
            "el_1y_rate": round(el_1y / fv_total, 6) if fv_total else None,
            "el_1y_usd": round(el_1y, 0),
            "delta_ev_pct": None,                          # filled after base known
            "_ev": ev,
            "floor_bound_share": round(floor_fv / fv_total, 4) if fv_total else None,
            "pd_wavg": round(float(np.average(pd_s_v, weights=fv)), 5),
            "recovery_wavg": round(float(np.average(rec_s_v, weights=fv)), 4),
        }

    anchor = pv_base_total or 1.0
    for name, m in out_scen.items():
        m["delta_ev_pct"] = round((m.pop("_ev") - anchor) / anchor, 5)

    worst = min((n for n in out_scen if n != "base"),
                key=lambda n: out_scen[n]["delta_ev_pct"])
    up100 = out_scen.get("rates_+100", {}).get("nii_1y")
    base_nii = out_scen["base"]["nii_1y"]
    return {
        "as_of": as_of,
        "method": ("survival-weighted expected cashflows; mark entry (−FV); "
                   "PD_s = PD×(1+(f−1)×score_stress_multiplier); terminal PIK balloon "
                   "repaid at maturity; equity haircut in macro scenarios only. "
                   "Per-loan rate floors are NOT tagged in any filer's SOI (0/4530), so "
                   "down-rate scenarios carry no floor benefit — income downside is "
                   "conservative (floor_bound_share activates automatically if filers "
                   "ever tag floors)"),
        "loan_fv": round(fv_total, 0), "equity_fv": round(equity_fv, 0),
        "scenarios": out_scen,
        "worst": {"name": worst, **{k: out_scen[worst][k]
                                    for k in ("net_irr", "delta_ev_pct", "el_1y_rate")}},
        "nii_dv100": (round(up100 - base_nii, 0)
                      if (up100 is not None and base_nii is not None) else None),
    }
