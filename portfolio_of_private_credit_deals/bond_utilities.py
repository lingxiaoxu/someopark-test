#!/usr/bin/env python3
"""
Bond and Loan Mathematical Utilities
Contains core calculation functions for fixed income instruments
"""

import pandas as pd
import numpy as np
import math
from dataclasses import dataclass
from typing import List, Tuple, Dict

# ============================================================================
# Day Count Conventions
# ============================================================================

def calculate_day_count_30_360_us(start_date: pd.Timestamp, end_date: pd.Timestamp) -> float:
    """
    Calculate 30/360 US day count year fraction
    
    Args:
        start_date: Period start date
        end_date: Period end date
        
    Returns:
        Year fraction using 30/360 US convention
    """
    d1 = pd.Timestamp(start_date)
    d2 = pd.Timestamp(end_date)
    d1_day = min(d1.day, 30)
    d2_day = d2.day if not (d1.day == 31 and d2.day == 31) else 30
    d2_day = 30 if (d1_day == 30 and d2.day == 31) else d2_day
    years = d2.year - d1.year
    months = d2.month - d1.month
    days = d2_day - d1_day
    return (360 * years + 30 * months + days) / 360.0

def calculate_day_count_actual_365f(start_date: pd.Timestamp, end_date: pd.Timestamp) -> float:
    """
    Calculate ACT/365F day count year fraction
    
    Args:
        start_date: Period start date
        end_date: Period end date
        
    Returns:
        Year fraction using ACT/365F convention
    """
    return (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days / 365.0

def calculate_day_count_actual_actual(start_date: pd.Timestamp, end_date: pd.Timestamp) -> float:
    """
    Calculate ACT/ACT day count year fraction (simplified implementation)
    
    Args:
        start_date: Period start date
        end_date: Period end date
        
    Returns:
        Year fraction using ACT/ACT convention
    """
    d1 = pd.Timestamp(start_date)
    d2 = pd.Timestamp(end_date)
    days = (d2 - d1).days
    year_days = 366 if pd.Timestamp(d1.year, 12, 31).is_leap_year else 365
    return days / year_days

def calculate_year_fraction(start_date, end_date, convention="30/360") -> float:
    """
    Calculate year fraction using specified day count convention
    
    Args:
        start_date: Period start date
        end_date: Period end date
        convention: Day count convention ("30/360", "ACT/365", "ACT/ACT")
        
    Returns:
        Year fraction for interest calculations
    """
    if convention.upper() in ("30/360", "30E/360", "30/360 US"):
        return calculate_day_count_30_360_us(start_date, end_date)
    elif convention.upper() in ("ACT/365", "ACT/365F"):
        return calculate_day_count_actual_365f(start_date, end_date)
    elif convention.upper() in ("ACT/ACT", "ACTUAL/ACTUAL"):
        return calculate_day_count_actual_actual(start_date, end_date)
    else:
        raise ValueError(f"Unknown day count convention: {convention}")

# ============================================================================
# Bond Specifications and Calculations
# ============================================================================

@dataclass
class BulletBondSpec:
    """Corporate bond specification for pricing and risk calculations"""
    face_value: float           # Face value (e.g., 1000)
    coupon_rate: float         # Annual coupon rate as decimal (e.g., 0.05 = 5%)
    payment_frequency: int     # Payments per year (1=annual, 2=semi, 4=quarterly)
    maturity_date: str        # Maturity date "YYYY-MM-DD"
    day_count: str = "30/360" # Day count convention
    settlement_date: str = None  # Settlement date (defaults to today)
    clean_price: bool = True  # Return clean price vs dirty price
    
    # Legacy attributes for backwards compatibility
    @property
    def face(self):
        return self.face_value
    
    @property 
    def freq(self):
        return self.payment_frequency
        
    @property
    def maturity(self):
        return self.maturity_date
        
    @property
    def settle(self):
        return self.settlement_date

@dataclass  
class LoanSpec:
    """Private credit loan specification"""
    principal: float
    annual_interest_rate: float    # Annual interest rate as decimal
    origination_date: str         # Start date
    term_months: int             # Total loan term in months
    interest_only_months: int = 0 # Interest-only period
    payment_frequency: str = "ME" # Payment frequency
    amortization_style: str = "annuity"  # "annuity" or "straight_line"
    upfront_fee_percent: float = 0.0
    exit_fee_percent: float = 0.0
    pik_rate: float = 0.0        # Payment-in-kind rate
    prepayments: Dict = None     # Prepayment schedule
    day_count: str = "ACT/365"   # Day count convention
    benchmark_rate: float = 0.0  # Benchmark rate for spread calculations
    
    # Legacy attributes for backwards compatibility
    @property
    def annual_rate(self):
        return self.annual_interest_rate
        
    @property
    def start_date(self):
        return self.origination_date
        
    @property
    def io_months(self):
        return self.interest_only_months
        
    @property
    def freq(self):
        return self.payment_frequency
        
    @property
    def amort_style(self):
        return self.amortization_style
        
    @property
    def upfront_fee_pct(self):
        return self.upfront_fee_percent
        
    @property
    def exit_fee_pct(self):
        return self.exit_fee_percent

def generate_coupon_schedule(maturity_date: str, frequency: int, max_periods: int = 40) -> List[pd.Timestamp]:
    """Generate coupon payment dates working backwards from maturity"""
    maturity = pd.Timestamp(maturity_date)
    months_between = int(12 / frequency)
    dates = [maturity]
    
    for _ in range(max_periods - 1):
        maturity = maturity - pd.DateOffset(months=months_between)
        if maturity.year < 1900:
            break
        dates.append(maturity)
    
    return sorted(dates)

def find_last_next_coupon(settlement_date, coupon_dates: List[pd.Timestamp]) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """Find the last coupon date before and next coupon date after settlement"""
    settlement = pd.Timestamp(settlement_date)
    past_coupons = [d for d in coupon_dates if d <= settlement]
    future_coupons = [d for d in coupon_dates if d > settlement]
    
    last_coupon = past_coupons[-1] if past_coupons else None
    next_coupon = future_coupons[0] if future_coupons else None
    
    return last_coupon, next_coupon

def calculate_bond_cashflows(bond_spec: BulletBondSpec) -> Dict:
    """Calculate bond cashflows and accrued interest"""
    settlement = pd.Timestamp(bond_spec.settlement_date) if bond_spec.settlement_date else pd.Timestamp.today().normalize()
    coupon_schedule = generate_coupon_schedule(bond_spec.maturity_date, bond_spec.payment_frequency)
    last_coupon, next_coupon = find_last_next_coupon(settlement, coupon_schedule)
    
    if next_coupon is None:
        raise ValueError("Bond has already matured")
    
    # Calculate coupon amount per payment
    period_fraction = 1.0 / bond_spec.payment_frequency
    coupon_amount = bond_spec.face_value * bond_spec.coupon_rate * period_fraction
    
    # Calculate accrued interest
    if last_coupon is None:
        # If no previous coupon, estimate from next coupon date
        last_coupon = next_coupon - pd.DateOffset(months=int(12/bond_spec.payment_frequency))
    
    accrual_fraction = calculate_year_fraction(last_coupon, settlement, bond_spec.day_count)
    accrued_interest = coupon_amount * accrual_fraction / period_fraction
    
    # Generate future cashflows
    future_dates = [d for d in coupon_schedule if d > settlement]
    cashflows = []
    
    for i, date in enumerate(future_dates):
        if i == len(future_dates) - 1:  # Final payment includes principal
            cashflows.append((date, coupon_amount + bond_spec.face_value))
        else:
            cashflows.append((date, coupon_amount))
    
    return {
        "settlement_date": settlement,
        "last_coupon": last_coupon,
        "next_coupon": next_coupon,
        "accrual_fraction": accrual_fraction,
        "accrued_interest": accrued_interest,
        "coupon_amount": coupon_amount,
        "cashflows": cashflows
    }

def calculate_bond_price_from_ytm(bond_spec: BulletBondSpec, ytm: float) -> Tuple[float, float]:
    """Calculate bond clean and dirty price given yield-to-maturity"""
    cf_info = calculate_bond_cashflows(bond_spec)
    settlement = cf_info["settlement_date"]
    
    present_value = 0.0
    for date, cashflow in cf_info["cashflows"]:
        time_to_payment = calculate_year_fraction(settlement, date, bond_spec.day_count)
        discount_factor = (1.0 + ytm / bond_spec.payment_frequency) ** (-time_to_payment * bond_spec.payment_frequency)
        present_value += cashflow * discount_factor
    
    dirty_price = present_value
    clean_price = dirty_price - cf_info["accrued_interest"]
    
    return clean_price, dirty_price

def calculate_bond_ytm_from_price(bond_spec: BulletBondSpec, clean_price: float, tolerance: float = 1e-8, max_iterations: int = 100) -> float:
    """Calculate yield-to-maturity from bond clean price using Newton-Raphson method"""
    cf_info = calculate_bond_cashflows(bond_spec)
    target_dirty_price = clean_price + cf_info["accrued_interest"]
    
    def price_function(ytm):
        return calculate_bond_price_from_ytm(bond_spec, ytm)[1] - target_dirty_price
    
    # Initial guess
    ytm = 0.05
    
    for _ in range(max_iterations):
        # Numerical derivative
        epsilon = 1e-6
        f0 = price_function(ytm)
        f1 = price_function(ytm + epsilon)
        derivative = (f1 - f0) / epsilon
        
        if abs(derivative) < 1e-12:
            break
            
        ytm_new = ytm - f0 / derivative
        if abs(ytm_new - ytm) < tolerance:
            return ytm_new
        ytm = ytm_new
    
    # Fallback: bisection method
    low, high = -0.99, 1.0
    for _ in range(200):
        mid = (low + high) / 2
        value = price_function(mid)
        if abs(value) < tolerance:
            return mid
        
        if price_function(low) * value <= 0:
            high = mid
        else:
            low = mid
    
    return ytm

def calculate_bond_duration_convexity(bond_spec: BulletBondSpec, ytm: float) -> Tuple[float, float, float]:
    """Calculate modified duration, Macaulay duration, and convexity"""
    cf_info = calculate_bond_cashflows(bond_spec)
    settlement = cf_info["settlement_date"]
    
    pv_total = 0.0
    weighted_time = 0.0
    weighted_time_squared = 0.0
    
    for date, cashflow in cf_info["cashflows"]:
        time_to_payment = calculate_year_fraction(settlement, date, bond_spec.day_count)
        discount_factor = (1.0 + ytm / bond_spec.payment_frequency) ** (-time_to_payment * bond_spec.payment_frequency)
        present_value = cashflow * discount_factor
        
        pv_total += present_value
        weighted_time += time_to_payment * present_value
        weighted_time_squared += time_to_payment * (time_to_payment + 1/bond_spec.payment_frequency) * present_value
    
    if pv_total == 0:
        return float("nan"), float("nan"), float("nan")
    
    macaulay_duration = weighted_time / pv_total
    modified_duration = macaulay_duration / (1 + ytm / bond_spec.payment_frequency)
    convexity = weighted_time_squared / pv_total / (1 + ytm / bond_spec.payment_frequency) ** 2
    
    return modified_duration, macaulay_duration, convexity

# ============================================================================
# Loan Specifications and Calculations  
# ============================================================================

def generate_loan_schedule(loan_spec: LoanSpec) -> pd.DataFrame:
    """Generate loan payment schedule with principal and interest breakdown"""
    
    # Standardize frequency
    freq_str = "ME" if loan_spec.payment_frequency in ("M", "ME") else loan_spec.payment_frequency
    payment_dates = pd.date_range(
        pd.Timestamp(loan_spec.origination_date),
        periods=loan_spec.term_months + 1,
        freq=freq_str
    )
    
    principal_outstanding = float(loan_spec.principal)
    schedule_rows = []
    
    # Initial funding
    upfront_fees = loan_spec.principal * loan_spec.upfront_fee_percent
    schedule_rows.append({
        "payment_date": payment_dates[0],
        "outstanding_start": 0.0,
        "interest_payment": 0.0,
        "principal_payment": -loan_spec.principal,
        "fees": upfront_fees,
        "total_cashflow": -loan_spec.principal + upfront_fees,
        "outstanding_end": loan_spec.principal,
        "period_year_fraction": 0.0,
        "interest_rate": loan_spec.annual_interest_rate,
        "benchmark_rate": loan_spec.benchmark_rate,
        "notional_amount": loan_spec.principal,
    })
    
    # Generate payment schedule
    for period in range(1, len(payment_dates)):
        previous_date = payment_dates[period - 1]
        current_date = payment_dates[period]
        year_fraction = calculate_year_fraction(previous_date, current_date, loan_spec.day_count)
        outstanding_start = principal_outstanding
        
        # Calculate interest payment
        interest_payment = outstanding_start * loan_spec.annual_interest_rate * year_fraction
        
        # Add PIK interest to principal if applicable
        if loan_spec.pik_rate > 0:
            principal_outstanding += outstanding_start * loan_spec.pik_rate * year_fraction
        
        # Calculate principal payment
        principal_payment = 0.0
        if period > loan_spec.interest_only_months:
            remaining_periods = len(payment_dates) - 1 - period + 1
            
            if loan_spec.amortization_style == "annuity":
                monthly_rate = loan_spec.annual_interest_rate * year_fraction
                if monthly_rate != 0:
                    total_payment = outstanding_start * monthly_rate / (1 - (1 + monthly_rate) ** (-remaining_periods))
                    principal_payment = max(0.0, total_payment - interest_payment)
                else:
                    principal_payment = outstanding_start / remaining_periods
            else:  # straight_line
                principal_payment = outstanding_start / remaining_periods
        
        # Apply prepayments
        prepayment = 0.0
        if loan_spec.prepayments:
            date_key = pd.Timestamp(current_date).strftime("%Y-%m-%d")
            if date_key in loan_spec.prepayments and principal_outstanding > 0:
                prepayment = principal_outstanding * float(loan_spec.prepayments[date_key])
        
        total_principal_payment = min(principal_payment + prepayment, principal_outstanding)
        principal_outstanding -= total_principal_payment
        
        schedule_rows.append({
            "payment_date": current_date,
            "outstanding_start": outstanding_start,
            "interest_payment": interest_payment,
            "principal_payment": total_principal_payment,
            "fees": 0.0,
            "total_cashflow": interest_payment + total_principal_payment,
            "outstanding_end": principal_outstanding,
            "period_year_fraction": year_fraction,
            "interest_rate": loan_spec.annual_interest_rate,
            "benchmark_rate": loan_spec.benchmark_rate,
            "notional_amount": loan_spec.principal,
        })
    
    # Add exit fees
    if loan_spec.exit_fee_percent > 0 and schedule_rows:
        exit_fee = schedule_rows[-1]["outstanding_end"] * loan_spec.exit_fee_percent
        schedule_rows[-1]["fees"] += exit_fee
        schedule_rows[-1]["total_cashflow"] += exit_fee
    
    # Create DataFrame
    df = pd.DataFrame(schedule_rows)
    df.set_index("payment_date", inplace=True)
    
    # Add convenience column
    df["outstanding_balance"] = df["outstanding_end"]
    
    return df

# ============================================================================
# IRR and XNPV Calculations
# ============================================================================

def calculate_xnpv(rate: float, cashflows: List[Tuple[pd.Timestamp, float]]) -> float:
    """Calculate extended net present value for irregular cashflows"""
    cashflows = [(pd.Timestamp(date), float(amount)) for date, amount in cashflows]
    base_date = cashflows[0][0]
    total = 0.0
    
    for date, amount in cashflows:
        time_years = (date - base_date).days / 365.0
        total += amount / ((1 + rate) ** time_years)
    
    return total

def calculate_xirr(cashflows: List[Tuple[pd.Timestamp, float]], initial_guess: float = 0.1) -> float:
    """Calculate internal rate of return for irregular cashflows"""
    
    # Newton-Raphson method
    rate = initial_guess
    for _ in range(50):
        f0 = calculate_xnpv(rate, cashflows)
        epsilon = 1e-6
        f1 = calculate_xnpv(rate + epsilon, cashflows)
        derivative = (f1 - f0) / epsilon
        
        if abs(derivative) < 1e-12:
            break
            
        rate_new = rate - f0 / derivative
        if abs(rate_new - rate) < 1e-8:
            return rate_new
        rate = rate_new
    
    # Fallback: bisection method
    low, high = -0.999, 2.0
    for _ in range(200):
        mid = (low + high) / 2
        value = calculate_xnpv(mid, cashflows)
        if abs(value) < 1e-8:
            return mid
        
        if calculate_xnpv(low, cashflows) * value <= 0:
            high = mid
        else:
            low = mid
    
    return rate

def extract_loan_cashflows_for_irr(loan_schedule: pd.DataFrame) -> List[Tuple[pd.Timestamp, float]]:
    """Extract cashflows from loan schedule for IRR calculation"""
    return list(zip(
        loan_schedule.index.to_pydatetime(),
        loan_schedule["total_cashflow"].astype(float)
    ))

def calculate_loan_irr_and_moic(loan_schedule: pd.DataFrame) -> Tuple[float, float]:
    """Calculate loan IRR and multiple of invested capital (MOIC)"""
    cashflows = extract_loan_cashflows_for_irr(loan_schedule)
    irr = calculate_xirr(cashflows, initial_guess=0.12)
    
    # Calculate MOIC
    negative_cashflows = sum(min(0.0, amount) for _, amount in cashflows)
    positive_cashflows = sum(max(0.0, amount) for _, amount in cashflows)
    moic = abs(positive_cashflows / negative_cashflows) if negative_cashflows != 0 else float("inf")
    
    return irr, moic

# ============================================================================
# Portfolio Optimization Utilities
# ============================================================================

def calculate_portfolio_metrics(weights, returns_matrix: pd.DataFrame, risk_free_rate: float = 0.0, frequency: int = 252) -> Dict[str, float]:
    """Calculate portfolio expected return, volatility, and Sharpe ratio"""
    weights_array = np.asarray(weights, dtype=float)
    if weights_array.ndim != 1 or len(weights_array) != returns_matrix.shape[1]:
        raise ValueError("Weights length must equal number of assets in returns matrix")
    
    # Calculate annualized metrics
    mean_returns = returns_matrix.mean().values * frequency
    covariance_matrix = returns_matrix.cov().values * frequency
    
    portfolio_return = float(weights_array @ mean_returns)
    portfolio_variance = float(weights_array @ covariance_matrix @ weights_array)
    portfolio_volatility = float(np.sqrt(portfolio_variance))
    
    sharpe_ratio = (portfolio_return - risk_free_rate) / portfolio_volatility if portfolio_volatility > 0 else float("nan")
    
    return {
        "expected_return": portfolio_return,
        "volatility": portfolio_volatility,
        "sharpe_ratio": sharpe_ratio
    }

def simulate_random_portfolios_from_returns(returns_matrix: pd.DataFrame,
                                          num_portfolios: int = 20000,
                                          risk_free_rate: float = 0.0,
                                          allow_short: bool = False,
                                          random_seed: int = 42) -> pd.DataFrame:
    """Generate random portfolio weights and calculate metrics"""
    returns_clean = returns_matrix.dropna(how="all")
    asset_names = list(returns_clean.columns)
    
    rng = np.random.default_rng(random_seed)
    portfolio_results = []
    
    for _ in range(int(num_portfolios)):
        if allow_short:
            weights = rng.normal(0, 1, size=len(asset_names))
            if np.allclose(weights, 0):
                continue
            weights = weights / np.sum(np.abs(weights))
        else:
            weights = rng.random(len(asset_names))
            total_weight = weights.sum()
            if total_weight == 0:
                continue
            weights = weights / total_weight
        
        metrics = calculate_portfolio_metrics(weights, returns_clean, risk_free_rate)
        portfolio_results.append([
            metrics["expected_return"],
            metrics["volatility"], 
            metrics["sharpe_ratio"],
            *weights
        ])
    
    columns = ["Return", "Volatility", "Sharpe"] + asset_names
    return pd.DataFrame(portfolio_results, columns=columns)

def find_optimal_portfolios(portfolios_df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
    """Find maximum Sharpe ratio and minimum volatility portfolios"""
    if portfolios_df.empty:
        raise ValueError("No portfolios provided")
    
    max_sharpe_portfolio = portfolios_df.iloc[portfolios_df["Sharpe"].idxmax()]
    min_vol_portfolio = portfolios_df.iloc[portfolios_df["Volatility"].idxmin()]
    
    return max_sharpe_portfolio, min_vol_portfolio

def display_portfolio_weights(portfolio_series: pd.Series):
    """Display portfolio weights in a formatted table"""
    weights = {
        asset: float(portfolio_series[asset]) 
        for asset in portfolio_series.index 
        if asset not in {"Return", "Volatility", "Sharpe"}
    }
    
    total_weight = sum(weights.values())
    
    print("Portfolio Weights:")
    for asset, weight in weights.items():
        print(f"  {asset:>8s}: {weight:6.2%}")
    print(f"  {'Total':>8s}: {total_weight:6.2%}")

# ============================================================================
# Visualization Utilities
# ============================================================================

def plot_efficient_frontier(portfolios_df: pd.DataFrame,
                          max_sharpe_portfolio: pd.Series = None,
                          min_vol_portfolio: pd.Series = None,
                          risk_free_rate: float = 0.0,
                          rf_annual: float = None,  # Legacy parameter for compatibility
                          title: str = "Efficient Frontier"):
    """Plot efficient frontier with optimal portfolio markers"""
    import matplotlib.pyplot as plt
    
    # Use legacy parameter if provided
    if rf_annual is not None:
        risk_free_rate = rf_annual
    
    if portfolios_df.empty:
        raise ValueError("No portfolio data to plot")
    
    x_values = portfolios_df["Volatility"]
    y_values = portfolios_df["Return"]
    colors = portfolios_df["Sharpe"]
    
    plt.figure(figsize=(10, 6))
    scatter = plt.scatter(x_values, y_values, c=colors, cmap="viridis", s=10, alpha=0.6)
    colorbar = plt.colorbar(scatter)
    colorbar.set_label("Sharpe Ratio")
    
    # Mark optimal portfolios
    if max_sharpe_portfolio is not None:
        plt.scatter(max_sharpe_portfolio["Volatility"], max_sharpe_portfolio["Return"],
                   marker="*", s=300, c="red", edgecolor="black", label="Max Sharpe")
        
        # Capital market line
        slope = max_sharpe_portfolio["Sharpe"]
        x_line = np.linspace(0, float(x_values.max()) * 1.05, 100)
        y_line = risk_free_rate + slope * x_line
        plt.plot(x_line, y_line, color="red", linewidth=2)
    
    if min_vol_portfolio is not None:
        plt.scatter(min_vol_portfolio["Volatility"], min_vol_portfolio["Return"],
                   marker="D", s=140, c="white", edgecolor="black", label="Min Vol")
    
    plt.title(title)
    plt.xlabel("Volatility (annualized)")
    plt.ylabel("Expected Return (annualized)")
    plt.legend(loc="best")
    plt.tight_layout()
    plt.show()

def plot_loan_cashflow_waterfall(loan_schedule: pd.DataFrame, title: str = "Loan Cashflow Analysis"):
    """Plot loan cashflow waterfall chart"""
    import matplotlib.pyplot as plt
    
    # Extract cashflow components
    cashflow_data = loan_schedule[["interest_payment", "principal_payment", "fees"]].copy()
    
    # Auto-scale for readability
    max_value = np.nanmax(np.abs(cashflow_data.values))
    if max_value >= 1e9:
        cashflow_data = cashflow_data / 1e9
        scale_label = " (Billions)"
    elif max_value >= 1e6:
        cashflow_data = cashflow_data / 1e6  
        scale_label = " (Millions)"
    elif max_value >= 1e3:
        cashflow_data = cashflow_data / 1e3
        scale_label = " (Thousands)"
    else:
        scale_label = ""
    
    plt.figure(figsize=(14, 4))
    ax = cashflow_data.plot(kind="bar", stacked=True, 
                           color=["#4C78A8", "#F58518", "#54A24B"],
                           edgecolor="none")
    ax.set_title(title)
    ax.set_ylabel(f"Cashflow{scale_label}")
    ax.set_xlabel("Payment Date")
    plt.tight_layout()
    plt.show()

def plot_outstanding_balance_and_rates(loan_schedule: pd.DataFrame, title: str = "Outstanding Balance and Interest Rates"):
    """Plot outstanding balance and interest rates over time"""
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mtick
    
    fig, ax1 = plt.subplots(figsize=(12, 4.2))
    
    # Plot outstanding balance
    outstanding = loan_schedule["outstanding_balance"] if "outstanding_balance" in loan_schedule.columns else loan_schedule["outstanding_end"]
    
    # Auto-scale outstanding amounts
    max_outstanding = np.nanmax(outstanding.values)
    if max_outstanding >= 1e9:
        outstanding_scaled = outstanding / 1e9
        scale_label = " (Billions)"
    elif max_outstanding >= 1e6:
        outstanding_scaled = outstanding / 1e6
        scale_label = " (Millions)"  
    elif max_outstanding >= 1e3:
        outstanding_scaled = outstanding / 1e3
        scale_label = " (Thousands)"
    else:
        outstanding_scaled = outstanding
        scale_label = ""
    
    ax1.fill_between(loan_schedule.index, outstanding_scaled, alpha=0.3, label="Outstanding")
    ax1.plot(loan_schedule.index, outstanding_scaled, linewidth=1.8)
    ax1.set_ylabel(f"Outstanding{scale_label}")
    ax1.set_xlabel("Date")
    ax1.grid(True, axis="y", alpha=0.25)
    
    # Plot interest rates on secondary axis
    ax2 = ax1.twinx()
    if "interest_rate" in loan_schedule.columns:
        ax2.plot(loan_schedule.index, loan_schedule["interest_rate"], 
                linewidth=1.6, label="Interest Rate", color="orange")
    
    if "benchmark_rate" in loan_schedule.columns:
        ax2.plot(loan_schedule.index, loan_schedule["benchmark_rate"],
                linewidth=1.2, alpha=0.7, label="Benchmark", color="gray")
    
    ax2.yaxis.set_major_formatter(mtick.PercentFormatter(xmax=1.0))
    ax2.set_ylabel("Interest Rate")
    
    # Combine legends
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="best")
    
    plt.title(title)
    plt.tight_layout()
    plt.show()

# ============================================================================
# Utility Functions for Backwards Compatibility
# ============================================================================

# Legacy function names for compatibility with existing code
def bond_cashflows(spec):
    """Legacy wrapper for calculate_bond_cashflows"""
    # Handle both old and new spec formats
    if hasattr(spec, 'face'):
        # Old format
        new_spec = BulletBondSpec(
            face_value=spec.face,
            coupon_rate=spec.coupon_rate,
            payment_frequency=spec.freq,
            maturity_date=spec.maturity,
            day_count=spec.day_count,
            settlement_date=spec.settle
        )
    else:
        # New format - pass through
        new_spec = spec
    return calculate_bond_cashflows(new_spec)

def bond_price_from_ytm(spec, ytm):
    """Legacy wrapper for calculate_bond_price_from_ytm"""
    # Handle both old and new spec formats
    if hasattr(spec, 'face'):
        # Old format
        new_spec = BulletBondSpec(
            face_value=spec.face,
            coupon_rate=spec.coupon_rate,
            payment_frequency=spec.freq,
            maturity_date=spec.maturity,
            day_count=spec.day_count,
            settlement_date=spec.settle
        )
    else:
        # New format - pass through
        new_spec = spec
    return calculate_bond_price_from_ytm(new_spec, ytm)

def bond_ytm_from_price(spec, clean_price):
    """Legacy wrapper for calculate_bond_ytm_from_price"""
    # Handle both old and new spec formats
    if hasattr(spec, 'face'):
        # Old format
        new_spec = BulletBondSpec(
            face_value=spec.face,
            coupon_rate=spec.coupon_rate,
            payment_frequency=spec.freq,
            maturity_date=spec.maturity,
            day_count=spec.day_count,
            settlement_date=spec.settle
        )
    else:
        # New format - pass through
        new_spec = spec
    return calculate_bond_ytm_from_price(new_spec, clean_price)

def bond_duration_convexity(spec, ytm):
    """Legacy wrapper for calculate_bond_duration_convexity"""
    # Handle both old and new spec formats
    if hasattr(spec, 'face'):
        # Old format
        new_spec = BulletBondSpec(
            face_value=spec.face,
            coupon_rate=spec.coupon_rate,
            payment_frequency=spec.freq,
            maturity_date=spec.maturity,
            day_count=spec.day_count,
            settlement_date=spec.settle
        )
    else:
        # New format - pass through
        new_spec = spec
    return calculate_bond_duration_convexity(new_spec, ytm)

def loan_schedule(spec):
    """Legacy wrapper for generate_loan_schedule"""
    # Handle both old and new spec formats
    if hasattr(spec, 'annual_rate'):
        # Old format
        new_spec = LoanSpec(
            principal=spec.principal,
            annual_interest_rate=spec.annual_rate,
            origination_date=spec.start_date,
            term_months=spec.term_months,
            interest_only_months=spec.io_months,
            payment_frequency=spec.freq,
            amortization_style=spec.amort_style,
            upfront_fee_percent=spec.upfront_fee_pct,
            exit_fee_percent=spec.exit_fee_pct,
            pik_rate=spec.pik_rate,
            prepayments=spec.prepayments,
            day_count=spec.day_count,
            benchmark_rate=spec.benchmark_rate
        )
    else:
        # New format - pass through
        new_spec = spec
    
    schedule = generate_loan_schedule(new_spec)
    
    # Rename columns for backwards compatibility
    column_mapping = {
        'outstanding_start': 'OutstandingStart',
        'interest_payment': 'Interest', 
        'principal_payment': 'Principal',
        'fees': 'Fees',  # Add fees mapping
        'total_cashflow': 'Total',
        'outstanding_end': 'OutstandingEnd',
        'period_year_fraction': 'PeriodYF',
        'interest_rate': 'Rate',
        'benchmark_rate': 'Benchmark',
        'notional_amount': 'Notional',
        'outstanding_balance': 'Outstanding'
    }
    
    return schedule.rename(columns=column_mapping)

def loan_IRR_and_MOIC(df):
    """Legacy wrapper for calculate_loan_irr_and_moic"""
    # Convert column names back
    df_converted = df.rename(columns={
        'OutstandingStart': 'outstanding_start',
        'Interest': 'interest_payment',
        'Principal': 'principal_payment', 
        'Total': 'total_cashflow',
        'OutstandingEnd': 'outstanding_end'
    })
    return calculate_loan_irr_and_moic(df_converted)

# Legacy function aliases for backwards compatibility
best_portfolios = find_optimal_portfolios
print_weights = display_portfolio_weights
plot_cashflow_waterfall = plot_loan_cashflow_waterfall
plot_outstanding_enhanced = plot_outstanding_balance_and_rates

# Import year_fraction for compatibility
year_fraction = calculate_year_fraction