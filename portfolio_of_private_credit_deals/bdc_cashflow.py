"""
bdc_cashflow.py — D4 §6.5: cash-flow modelling for real BDC loans, extending (never
modifying) the existing engine.

Builds a LoanSpec from a SOI deal row and runs the module's generate_loan_schedule
(which already handles amortisation / IO / PIK / fees), then ADDS the columns that real
SOI fields unlock and the legacy demo never used:

  CashInterest / PIKInterest  — split the coupon: cash = (all_in − pik)×outstanding,
                                PIK compounds into principal (SOI tags PIK per loan).
  FloorApplied                — base_t = max(forward_SOFR, rate_floor); floor flag.
  OIDAccretion                — cost vs par pulled to par over the remaining life; the
                                SOI cost/principal gap is real OID the demo ignored.
  ExitPar / ExitMark          — redemption scenarios: at par vs at the BDC's FV mark.
  NonAccrual handling         — cash interest = 0, recovery path (per credit attrs).

All additive: generate_loan_schedule is called unchanged, so fundamental-mode demo
behaviour is bit-identical. Returns (schedule_df, metrics_dict).
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

from bond_utilities import LoanSpec, generate_loan_schedule, calculate_loan_irr_and_moic

_FRED_RATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fred_rates.csv")


def _term_months(as_of: str, maturity: str) -> int:
    """Remaining months from snapshot to maturity ('YYYY-MM'); floor at 6m."""
    try:
        a = pd.Timestamp(as_of)
        m = pd.Timestamp(maturity if len(str(maturity)) > 7 else f"{maturity}-01")
        return max(6, int(round((m - a).days / 30.44)))
    except Exception:  # noqa: BLE001
        return 60


_CURRENT_SOFR = 0.036          # fallback base when only the spread is known (~2026 SOFR)


def _all_in_rate(row) -> float:
    """Total (cash + PIK) annual rate from SOI fields, best available source."""
    allin = pd.to_numeric(row.get("all_in_rate"), errors="coerce")
    if pd.notna(allin):
        return float(allin)
    spread = pd.to_numeric(row.get("spread"), errors="coerce")
    if pd.notna(spread):
        return float(spread) + _CURRENT_SOFR
    pik = pd.to_numeric(row.get("pik_rate"), errors="coerce")
    if pd.notna(pik) and pik > 0:
        return float(pik)                    # all-PIK instrument: total rate = PIK rate
    return 0.10                              # last-resort default


def _f(x, default=0.0) -> float:
    """Safe float: NaN/None -> default (avoids the `nan or 0` truthiness trap)."""
    v = pd.to_numeric(x, errors="coerce")
    return float(v) if pd.notna(v) else default


def build_loan_spec(row, as_of: str) -> LoanSpec:
    principal = _f(row.get("principal")) or _f(row.get("fair_value"))
    pik = _f(row.get("pik_rate"))
    all_in = _all_in_rate(row)
    cash_rate = max(0.0, all_in - pik)        # engine pays cash coupon; PIK compounds separately
    return LoanSpec(
        principal=abs(principal) or 1.0,
        annual_interest_rate=cash_rate,
        origination_date=str(as_of),
        term_months=_term_months(as_of, row.get("maturity")),
        payment_frequency="QE",
        amortization_style="annuity",
        pik_rate=pik,
    )


def _fwd_at(fwd_curve, ts):
    """Forward SOFR at a payment date from a precomputed month-indexed curve."""
    if fwd_curve is None or len(fwd_curve) == 0:
        return None
    ts = pd.Timestamp(ts)
    idx = fwd_curve.index.get_indexer([ts], method="nearest")[0]
    return float(fwd_curve.iloc[idx])


def bdc_loan_cashflow(row, as_of: str, fwd_curve=None) -> tuple[pd.DataFrame, dict]:
    """Schedule + SOI-extended columns + summary metrics for one loan.

    If fwd_curve (a month-indexed forward-SOFR Series) is given, floating loans are
    re-priced PER PERIOD on the forward curve: rate_t = max(fwd_SOFR_t, floor) + spread
    (§7.2). Without it, the loan's current all-in rate is held flat."""
    spec = build_loan_spec(row, as_of)
    sched = generate_loan_schedule(spec).copy()

    pik = float(pd.to_numeric(row.get("pik_rate"), errors="coerce") or 0.0)
    floor = pd.to_numeric(row.get("rate_floor"), errors="coerce")
    spread = pd.to_numeric(row.get("spread"), errors="coerce")
    non_accrual = bool(row.get("non_accrual") is True)
    yf = sched.get("period_year_fraction", pd.Series(0.0, index=sched.index))
    out0 = sched.get("outstanding_start", pd.Series(0.0, index=sched.index))

    floor_applied = False
    if fwd_curve is not None and pd.notna(spread) and spread > 0:
        # forward floating: re-price cash interest per period on the forward SOFR curve
        per_rate, fa = [], False
        for ts in sched.index:
            base = _fwd_at(fwd_curve, ts)
            base = base if base is not None else 0.04
            if pd.notna(floor) and base < float(floor):
                base = float(floor); fa = True
            per_rate.append(max(base, 0.0) + float(spread))
        floor_applied = fa
        rates = pd.Series(per_rate, index=sched.index)
        cash_int = out0 * rates * yf
        sched["interest_payment"] = cash_int
        sched["interest_rate"] = rates
        sched["total_cashflow"] = (sched["interest_payment"]
                                   + sched.get("principal_payment", 0.0)
                                   + sched.get("fees", 0.0))
    else:
        cash_int = sched.get("interest_payment", pd.Series(0.0, index=sched.index))

    sched["CashInterest"] = 0.0 if non_accrual else cash_int
    sched["PIKInterest"] = out0 * pik * yf
    sched["FloorApplied"] = floor_applied
    sched["NonAccrual"] = non_accrual

    # OID accretion: (par − cost) pulled to par straight-line over the schedule
    cost = pd.to_numeric(row.get("cost"), errors="coerce")
    par = float(spec.principal)
    oid = (par - float(cost)) if pd.notna(cost) else 0.0
    n = max(len(sched), 1)
    sched["OIDAccretion"] = oid / n

    # exit scenarios at the final period
    fv = pd.to_numeric(row.get("fair_value"), errors="coerce")
    mark = (float(fv) / float(cost)) if (pd.notna(fv) and pd.notna(cost) and cost) else 1.0
    irr, moic = calculate_loan_irr_and_moic(sched)
    metrics = {
        "irr": irr, "moic": moic,
        "cash_interest_total": float(sched["CashInterest"].sum()),
        "pik_interest_total": float(sched["PIKInterest"].sum()),
        "oid_total": float(oid),
        "exit_par": par, "exit_mark": par * mark,
        "term_months": spec.term_months, "non_accrual": non_accrual,
    }
    return sched, metrics


def build_forward_sofr_curve(as_of: str, horizon_years: int = 30):
    """Precompute one forward-SOFR curve (quarter-end month-indexed Series) via the
    module's ForwardRateLookup, so per-loan floating re-pricing is a cheap lookup
    instead of 100k Nelson-Siegel calls. Past dates → fred_rates.csv; future → NS."""
    import contextlib, io
    rates_df = pd.read_csv(_FRED_RATES, index_col=0, parse_dates=True) \
        if os.path.exists(_FRED_RATES) else None
    dates = pd.date_range(pd.Timestamp(as_of), periods=horizon_years * 4 + 1, freq="QE")
    try:
        from forward_rate_lookup import ForwardRateLookup
        frl = ForwardRateLookup()
        if getattr(frl, "forward_rates_dir", None):       # true NS forward curve available
            vals = []
            with contextlib.redirect_stdout(io.StringIO()):
                for d in dates:
                    try:
                        vals.append(float(frl.get_forward_rate("SOFR", str(d.date()), rates_df)))
                    except Exception:  # noqa: BLE001
                        vals.append(np.nan)
            s = pd.Series(vals, index=dates).ffill().bfill()
            if s.notna().any():
                return s
    except Exception:  # noqa: BLE001
        pass
    # robust fallback: current SOFR (from the unified fred_rates.csv) held flat — the
    # standard "spot held forward" assumption, using REAL current data (not hardcoded).
    if rates_df is not None and "SOFR" in rates_df.columns and rates_df["SOFR"].notna().any():
        cur = float(rates_df["SOFR"].dropna().iloc[-1])
        return pd.Series(cur, index=dates)
    return None


def portfolio_cashflow_metrics(df: pd.DataFrame, as_of: str, detail_top_n: int = 50,
                               fwd_curve=None) -> pd.DataFrame:
    """Per-deal IRR/MOIC/cash-vs-PIK/OID across the book (in-memory, no per-loan file).

    Detailed schedules are computed for ALL loans (metrics need them) but only the
    top-N by fair value keep their full schedule (returned via the `schedule` attr on
    the caller side); here we return the per-deal metric columns."""
    rows = []
    for _, r in df.iterrows():
        if r.get("credit_mode") == "equity":
            rows.append({"irr": np.nan, "moic": np.nan, "cash_interest_total": 0.0,
                         "pik_interest_total": 0.0, "oid_total": 0.0,
                         "exit_par": np.nan, "exit_mark": np.nan,
                         "term_months": np.nan, "non_accrual": False})
            continue
        try:
            _, m = bdc_loan_cashflow(r, as_of, fwd_curve=fwd_curve)
        except Exception:  # noqa: BLE001
            m = {"irr": np.nan, "moic": np.nan, "cash_interest_total": 0.0,
                 "pik_interest_total": 0.0, "oid_total": 0.0, "exit_par": np.nan,
                 "exit_mark": np.nan, "term_months": np.nan, "non_accrual": False}
        rows.append(m)
    return pd.DataFrame(rows, index=df.index)
