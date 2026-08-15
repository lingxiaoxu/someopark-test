"""
bdc_stress.py — scenario stress engine for the BDC look-through book (§7.3).

Runs the REAL loan book (bdc_deal_start.csv, post credit-scoring) through a fixed
matrix of rate and macro-credit scenarios, the way a private-credit IC deck does:

  Rate ladder (income/floor channel, credit held flat):
      rates_+100 / +200 / +300      parallel up-shift of the forward SOFR curve
      rates_-100 / -200 / -300      parallel cut, curve floored at 0 (loan-level
                                    rate floors then bind inside the repricer)
  Macro scenarios (all channels move together):
      mild_recession    rates −100 | PD excess ×1.75 | recovery −10pt | spreads +250bp | equity marks −10%
      severe_recession  rates −300 | PD excess ×3.00 | recovery −20pt | spreads +600bp | equity marks −30%
      stagflation       rates +300 | PD excess ×2.50 | recovery −15pt | spreads +400bp | equity marks −20%
                        (the worst case for a floating book: coverage crushed at peak rates)

Loan-level PD under stress consumes the per-loan score-based stress multiplier
(credit_risk_module, 0.8–2.0):  PD_s = clip( PD_base × (1 + (f−1)·mult), 0.5%, 45% )
— a resilient name (mult 0.8) sees 80% of the scenario's excess stress, a
vulnerable one (mult 2.0) sees double. LGD_s = 1 − clip(recovery − haircut).

Rate floors (2026-08-15 disclosure audit — per-filer provenance, no fabrication):
  BXSL  per-loan floors 0.50–3.00% extracted at INGEST from SOI footnote
        definitions (RefreshBDCHoldings.extract_html_row_attrs);
  GBDC  disclosed "98.0% of loans floored, weighted average floor 0.76%";
  OBDC  disclosed "weighted average floor 0.8%, majority at 0.75%";
  ARCC  disclosed "96% of variable-rate contained floors" — LEVEL not disclosed,
        1.00% ASSUMED (sponsor direct-lending standard, labelled);
  TSLX  disclosed "100.0% of floaters subject to floors" — level ASSUMED 1.00%.
  Overlay fills ONLY floating loans with no per-loan floor; OBDC/GBDC use their
  disclosed weighted-average level.

Valuation method — survival-weighted expected cashflows (actuarial standard):
  * each loan is repriced per scenario on the shifted forward curve via the SAME
    engine the daily numbers use (bdc_cashflow.bdc_loan_cashflow: per-period
    floating repricing, floors, PIK compounding, non-accrual coupon stripping);
  * quarterly hazard h_t = 1−(1−PD_s)^yf;  survival chain S_t;
  * expected CF_t = S_{t−1}·[ (1−h_t)·CF_t + h_t·recovery_s·outstanding_t ];
  * terminal PIK/unamortized balloon repaid at maturity (contractual);
  * entry leg = −fair_value at as_of (mark entry).
  * portfolio net IRR = XIRR of the SUMMED dollar flows (no per-loan Jensen).

Mark-anchored discounting (implied-margin calibration): the expected flows are
already default-adjusted, so discounting them at the loan's FULL credit spread
would double-count credit. Instead each loan's discount yield y* is SOLVED so
that PV(base expected flows, y*) == today's fair value — the book is priced off
its own marks. Scenario PV then discounts scenario flows at
    y*_i + (fwd_avg_scenario − fwd_avg_base) + spread_widen,
so base ΔEV ≡ 0 by construction and scenario deltas are anchored to the marks.

Reported per scenario (lt-book dollars, rates as decimals):
  net_irr / nii_1y / el_1y_rate / delta_ev_pct / floor_bound_share /
  pd_wavg / recovery_wavg;  plus by_bdc ΔEV, a base↔stress reconciliation
  bridge, and worst-scenario callout.

Runs inside RunBDCLookThrough (STEP D, 16:05 ET daily); results land in the SAME
summary/daily-report JSONs under "stress". Pure consumer — writes nothing itself.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import bdc_cashflow
from bond_utilities import calculate_xirr

# ── scenario matrix ─────────────────────────────────────────────────────────
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

# per-BDC floor overlay for floating loans with no per-loan floor (provenance above)
FLOOR_OVERLAY: dict[str, float | None] = {
    "GBDC": 0.0076,   # disclosed weighted-average
    "OBDC": 0.0080,   # disclosed weighted-average
    "ARCC": 0.0100,   # share disclosed (96%), level ASSUMED
    "TSLX": 0.0100,   # share disclosed (100%), level ASSUMED
    "BXSL": None,     # per-loan floors extracted at ingest; unmarked rows = no floor
}


def _shifted_curve(base_curve: pd.Series, shift_bp: int) -> pd.Series:
    return (base_curve + shift_bp / 1e4).clip(lower=0.0)


def _loan_arrays(row, as_of: str, curve: pd.Series, t0: pd.Timestamp):
    """One repriced schedule → numpy pieces for the hazard/PV layers.

    Returns (yrs, cf, cash_int, out0, yf, floor_bound, dates). cf carries the
    terminal balloon (unamortized principal + PIK accretion repaid at maturity)."""
    sched, _ = bdc_cashflow.bdc_loan_cashflow(row, as_of, fwd_curve=curve)
    body = sched.iloc[1:]                                  # row 0 = funding leg → replaced by −FV
    cf = body["total_cashflow"].to_numpy(dtype=float).copy()
    if len(cf):
        cf[-1] += float(body["outstanding_end"].iloc[-1])
    yrs = ((body.index - t0).days / 365.0).to_numpy(dtype=float)
    return (yrs, cf,
            body["CashInterest"].to_numpy(dtype=float),
            body["outstanding_start"].to_numpy(dtype=float),
            body["period_year_fraction"].to_numpy(dtype=float),
            bool(body["FloorApplied"].any()),
            body.index.to_numpy())


def _expected_flows(cf, cash_int, out0, yf, pd_s: float, rec_s: float):
    """Survival-weighted expected cashflows + expected cash interest (vectorised)."""
    h = 1.0 - np.power(1.0 - pd_s, np.clip(yf, 0.0, None))
    surv_prev = np.concatenate(([1.0], np.cumprod(1.0 - h)))[:-1]   # S_{t-1}
    ecf = surv_prev * ((1.0 - h) * cf + h * rec_s * out0)
    eint = surv_prev * (1.0 - h) * cash_int
    return ecf, eint


def _pv(ecf, yrs, y: float) -> float:
    return float(np.sum(ecf / np.power(1.0 + max(y, -0.95), yrs)))


def _implied_yield(ecf, yrs, target_pv: float) -> float | None:
    """Bisection: y* with PV(ecf, y*) = target_pv. None if not bracketable
    (degenerate rows: zero FV, zero flows)."""
    if target_pv <= 0 or not len(ecf) or float(np.sum(ecf)) <= 0:
        return None
    lo, hi = -0.90, 5.0
    if not (_pv(ecf, yrs, lo) >= target_pv >= _pv(ecf, yrs, hi)):
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if _pv(ecf, yrs, mid) >= target_pv:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def run_stress_matrix(deals: pd.DataFrame, as_of: str,
                      base_curve: pd.Series | None = None) -> dict:
    """Scenario matrix over the enriched deal frame (post score/attrs/lt_weight)."""
    if base_curve is None:
        base_curve = bdc_cashflow.build_forward_sofr_curve(as_of)
    if base_curve is None:
        raise RuntimeError("no forward SOFR curve — fred_rates.csv missing?")

    loans = deals[deals["credit_mode"] != "equity"].copy()
    # floor overlay: floating loans with no per-loan floor get their BDC's
    # disclosed weighted-average floor (see module docstring for provenance)
    _spread_na = pd.to_numeric(loans["spread"], errors="coerce").notna()
    _floor_na = pd.to_numeric(loans["rate_floor"], errors="coerce").isna()
    _ov = loans["bdc"].map(FLOOR_OVERLAY)
    loans.loc[_spread_na & _floor_na & _ov.notna(), "rate_floor"] = _ov
    n_floor_overlay = int((_spread_na & _floor_na & _ov.notna()).sum())
    n_floor_perloan = int((_spread_na & ~_floor_na).sum())

    eq = deals[deals["credit_mode"] == "equity"]
    equity_fv = float(pd.to_numeric(eq["fair_value"], errors="coerce").sum())
    eq_fv_bdc = pd.to_numeric(eq["fair_value"], errors="coerce").groupby(eq["bdc"]).sum()

    fv = pd.to_numeric(loans["fair_value"], errors="coerce").fillna(0.0).to_numpy()
    par = pd.to_numeric(loans["principal"], errors="coerce").fillna(0.0).to_numpy()
    ead = np.where(par > 0, par, fv)
    pd_base = pd.to_numeric(loans["default_prob"], errors="coerce").fillna(0.03).to_numpy()
    rec_base = pd.to_numeric(loans["recovery_rate"], errors="coerce").fillna(0.40).to_numpy()
    mult = pd.to_numeric(loans["stress_multiplier"], errors="coerce").fillna(1.0).to_numpy()
    spread = pd.to_numeric(loans["spread"], errors="coerce").to_numpy()
    is_float = ~np.isnan(spread)
    bdc_arr = loans["bdc"].to_numpy()
    fv_total = float(fv.sum())
    lt_w = pd.to_numeric(loans.get("lt_weight"), errors="coerce").fillna(0.0)
    gross_cash_irr = None
    if "cf_irr" in loans.columns:
        _v = pd.to_numeric(loans["cf_irr"], errors="coerce")
        _m = _v.notna() & (lt_w > 0)
        gross_cash_irr = float((_v[_m] * lt_w[_m]).sum() / lt_w[_m].sum()) if _m.any() else None

    # ── schedule cache: floating loans per DISTINCT curve; fixed loans once ──
    shifts = sorted({s["shift"] for s in SCENARIOS.values()})
    curves = {sh: _shifted_curve(base_curve, sh) for sh in shifts}
    fwd_avg = {sh: float(curves[sh].iloc[:20].mean()) for sh in shifts}
    t0 = pd.Timestamp(as_of)
    horizon_1y = t0 + pd.DateOffset(years=1)
    rows = list(loans.itertuples(index=False))
    cols = {c: i for i, c in enumerate(loans.columns)}

    def _row_dict(t):
        return {c: t[i] for c, i in cols.items()}

    sched_fixed: list = [None] * len(rows)
    sched_float: dict[int, list] = {sh: [None] * len(rows) for sh in shifts}
    for i, t in enumerate(rows):
        r = _row_dict(t)
        if is_float[i]:
            for sh in shifts:
                sched_float[sh][i] = _loan_arrays(r, as_of, curves[sh], t0)
        else:
            sched_fixed[i] = _loan_arrays(r, as_of, curves[0], t0)

    def _arrays(i, shift):
        return sched_float[shift][i] if is_float[i] else sched_fixed[i]

    # ── implied-margin calibration on the BASE scenario (anchor PV to marks) ──
    y_star = np.full(len(rows), np.nan)
    for i in range(len(rows)):
        yrs, cf, cash_int, out0, yf, _, _ = _arrays(i, 0)
        ecf, _ = _expected_flows(cf, cash_int, out0, yf,
                                 float(np.clip(pd_base[i], _PD_FLOOR, _PD_CAP)),
                                 float(np.clip(rec_base[i], _REC_FLOOR, _REC_CAP)))
        ys = _implied_yield(ecf, yrs, fv[i])
        y_star[i] = ys if ys is not None else np.nan
    uncal = np.isnan(y_star)
    n_uncal = int(uncal.sum())
    # degenerate rows fall back to a market-convention yield so they still move
    # with the scenario; their base PV won't equal FV (counted + reported)
    y_star[uncal] = np.where(is_float[uncal],
                             fwd_avg[0] + np.nan_to_num(spread[uncal], nan=0.05),
                             0.10)

    out_scen: dict[str, dict] = {}
    ev_base_total: float | None = None
    ev_base_bdc: dict[str, float] = {}

    for name, sc in SCENARIOS.items():
        pd_s_v = np.clip(pd_base * (1.0 + (sc["pd_f"] - 1.0) * mult), _PD_FLOOR, _PD_CAP)
        rec_s_v = np.clip(rec_base - sc["rec_hc"], _REC_FLOOR, _REC_CAP)
        d_fwd = fwd_avg[sc["shift"]] - fwd_avg[0]

        agg: dict = {}
        nii_1y = 0.0
        pv_bdc: dict[str, float] = {}
        floor_fv = 0.0
        for i in range(len(rows)):
            yrs, cf, cash_int, out0, yf, floored, dates = _arrays(i, sc["shift"])
            ecf, eint = _expected_flows(cf, cash_int, out0, yf, pd_s_v[i], rec_s_v[i])
            pv = _pv(ecf, yrs, y_star[i] + d_fwd + sc["widen"])
            pv_bdc[bdc_arr[i]] = pv_bdc.get(bdc_arr[i], 0.0) + pv
            nii_1y += float(eint[dates <= np.datetime64(horizon_1y)].sum())
            if floored:
                floor_fv += fv[i]
            for d, v in zip(dates, ecf):
                agg[d] = agg.get(d, 0.0) + float(v)

        irr_flows = [(t0, -fv_total)] + [(pd.Timestamp(d), v) for d, v in sorted(agg.items())]
        net_irr = calculate_xirr(irr_flows, initial_guess=0.10)
        el_1y = float(np.sum(pd_s_v * (1.0 - rec_s_v) * ead))

        ev_bdc = {b: pv_bdc.get(b, 0.0) + float(eq_fv_bdc.get(b, 0.0)) * (1.0 - sc["eq_hc"])
                  for b in set(list(pv_bdc) + list(eq_fv_bdc.index))}
        ev = sum(ev_bdc.values())
        if name == "base":
            ev_base_total, ev_base_bdc = ev, ev_bdc
        out_scen[name] = {
            "net_irr": round(net_irr, 6),
            "nii_1y": round(nii_1y, 0),
            "el_1y_rate": round(el_1y / fv_total, 6) if fv_total else None,
            "el_1y_usd": round(el_1y, 0),
            "_ev": ev, "_ev_bdc": ev_bdc,
            "floor_bound_share": round(floor_fv / fv_total, 4) if fv_total else None,
            "pd_wavg": round(float(np.average(pd_s_v, weights=fv)), 5),
            "recovery_wavg": round(float(np.average(rec_s_v, weights=fv)), 4),
        }

    anchor = ev_base_total or 1.0
    for name, m in out_scen.items():
        m["delta_ev_pct"] = round((m.pop("_ev") - anchor) / anchor, 5)
        m["delta_ev_by_bdc"] = {b: round((v - ev_base_bdc.get(b, 0.0)) /
                                         (ev_base_bdc.get(b) or 1.0), 4)
                                for b, v in m.pop("_ev_bdc").items()}

    worst = min((n for n in out_scen if n != "base"),
                key=lambda n: out_scen[n]["delta_ev_pct"])
    base_m = out_scen["base"]
    up100 = out_scen.get("rates_+100", {}).get("nii_1y")
    return {
        "as_of": as_of,
        "method": ("survival-weighted expected cashflows; mark entry (−FV); "
                   "PD_s = PD×(1+(f−1)×score_stress_multiplier); terminal PIK balloon "
                   "repaid at maturity; mark-anchored implied-margin discounting "
                   "(base ΔEV≡0 by construction); equity haircut in macro scenarios. "
                   "Floors: BXSL per-loan (SOI footnotes), GBDC 0.76%/OBDC 0.80% "
                   "disclosed wavg overlay, ARCC/TSLX share disclosed + 1.00% ASSUMED "
                   "level, only on floating loans lacking a per-loan floor"),
        "loan_fv": round(fv_total, 0), "equity_fv": round(equity_fv, 0),
        "floors": {"per_loan": n_floor_perloan, "overlay": n_floor_overlay},
        "calibration": {"uncalibrated_rows": n_uncal,
                        "y_star_wavg": round(float(np.average(y_star, weights=np.maximum(fv, 1.0))), 5)},
        "bridge": {   # base ↔ headline reconciliation
            "gross_cash_irr_par_entry": (round(gross_cash_irr, 6)
                                         if gross_cash_irr is not None else None),
            "base_net_irr_mark_entry": base_m["net_irr"],
            "base_el_1y_rate": base_m["el_1y_rate"],
            "avg_entry_mark": round(float(fv[par > 0].sum() / par[par > 0].sum()), 4)
                              if (par > 0).any() else None,
            "note": ("gross par-entry cash IRR (daily weighted.irr) minus expected "
                     "credit losses, PIK balloon timing and mark entry ≈ stress base "
                     "net IRR — the two headline numbers are different bases by design"),
        },
        "scenarios": out_scen,
        "worst": {"name": worst, **{k: out_scen[worst][k]
                                    for k in ("net_irr", "delta_ev_pct", "el_1y_rate")}},
        "nii_dv100": (round(up100 - base_m["nii_1y"], 0)
                      if (up100 is not None and base_m["nii_1y"] is not None) else None),
    }
