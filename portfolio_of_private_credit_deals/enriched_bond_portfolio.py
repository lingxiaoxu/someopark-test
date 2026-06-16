#!/usr/bin/env python3
"""
Enhanced Bond Portfolio with Real FRED Data and Detailed Specifications
"""
import pandas as pd
import numpy as np
import datetime as dt
from dataclasses import dataclass, asdict
from typing import Dict, List, Tuple
import sys
import os

# Import existing bond/loan classes and functions from test.py
sys.path.append('.')
from bond_utilities import (
    LoanSpec, BulletBondSpec, loan_schedule, loan_IRR_and_MOIC,
    bond_price_from_ytm, bond_ytm_from_price, bond_duration_convexity,
    simulate_random_portfolios_from_returns, best_portfolios, 
    plot_efficient_frontier, print_weights,
    plot_cashflow_waterfall, plot_outstanding_enhanced,
    calculate_year_fraction as year_fraction
)

@dataclass
class EnhancedLoanSpec:
    """Enhanced loan specification with all attributes"""
    loan_id: str
    borrower: str
    sector: str
    principal: float
    rate_type: str  # 'fixed' or 'floating'
    base_rate: str  # 'SOFR', 'EFFR', 'DGS2', 'DGS10', or 'none'
    spread: float
    origination_date: str
    maturity_months: int
    io_months: int
    amort_style: str
    origination_rate: float
    current_spread: float
    ltv: float  # loan-to-value
    dscr: float  # debt service coverage ratio
    credit_rating: str
    prepay_penalty: float
    default_prob: float
    seniority: str
    collateral_type: str
    geography: str
    vintage_yield: float
    coupon_freq: str
    floating_reset_freq: str

@dataclass  
class EnhancedBondSpec:
    """Enhanced bond specification with all attributes"""
    bond_id: str
    issuer: str
    sector: str
    face_value: float
    coupon_rate: float
    rate_type: str  # 'fixed' or 'floating'
    base_rate: str  # 'SOFR', 'EFFR', 'DGS2', 'DGS5', 'DGS10', or 'none'
    spread: float
    issue_date: str
    maturity_date: str
    freq: int  # payment frequency
    day_count: str
    call_protection: int  # years
    credit_rating: str
    duration: float
    convexity: float
    oas_spread: float  # option-adjusted spread
    liquidity_score: float
    seniority: str
    collateral: str
    geography: str
    vintage_yield: float
    callable: str  # 'yes' or 'no'
    puttable: str  # 'yes' or 'no'

def load_fred_rates():
    """Load FRED interest rate data"""
    try:
        rates_df = pd.read_csv('fred_rates.csv', index_col=0, parse_dates=True)
        print(f"Loaded FRED rates: {rates_df.shape[0]} days, {rates_df.shape[1]} series")
        return rates_df
    except FileNotFoundError:
        print("FRED rates file not found. Please run download_fred_data.py first.")
        return None

def load_loans():
    """Load enhanced loan specifications from file"""
    loans = []
    with open('synthetic_data/synthetic_loans.csv', 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                parts = line.split('|')
                if len(parts) >= 23:  # Ensure we have all fields
                    loan = EnhancedLoanSpec(
                        loan_id=parts[0],
                        borrower=parts[1],
                        sector=parts[2], 
                        principal=float(parts[3]),
                        rate_type=parts[4],
                        base_rate=parts[5],
                        spread=float(parts[6]),
                        origination_date=parts[7],
                        maturity_months=int(parts[8]),
                        io_months=int(parts[9]),
                        amort_style=parts[10],
                        origination_rate=float(parts[11]),
                        current_spread=float(parts[12]),
                        ltv=float(parts[13]),
                        dscr=float(parts[14]),
                        credit_rating=parts[15],
                        prepay_penalty=float(parts[16]),
                        default_prob=float(parts[17]),
                        seniority=parts[18],
                        collateral_type=parts[19],
                        geography=parts[20],
                        vintage_yield=float(parts[21]),
                        coupon_freq=parts[22],
                        floating_reset_freq=parts[23] if len(parts) > 23 else 'none'
                    )
                    loans.append(loan)
    
    print(f"Loaded {len(loans)} loan specifications")
    return loans

def load_bonds():
    """Load enhanced bond specifications from file"""
    bonds = []
    with open('synthetic_data/synthetic_bonds.csv', 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                parts = line.split('|')
                if len(parts) >= 23:  # Ensure we have all fields
                    bond = EnhancedBondSpec(
                        bond_id=parts[0],
                        issuer=parts[1],
                        sector=parts[2],
                        face_value=float(parts[3]),
                        coupon_rate=float(parts[4]),
                        rate_type=parts[5],
                        base_rate=parts[6],
                        spread=float(parts[7]),
                        issue_date=parts[8],
                        maturity_date=parts[9],
                        freq=int(parts[10]),
                        day_count=parts[11],
                        call_protection=int(parts[12]),
                        credit_rating=parts[13],
                        duration=float(parts[14]),
                        convexity=float(parts[15]),
                        oas_spread=float(parts[16]),
                        liquidity_score=float(parts[17]),
                        seniority=parts[18],
                        collateral=parts[19],
                        geography=parts[20],
                        vintage_yield=float(parts[21]),
                        callable=parts[22],
                        puttable=parts[23] if len(parts) > 23 else 'no'
                    )
                    bonds.append(bond)
    
    print(f"Loaded {len(bonds)} bond specifications")
    return bonds

def get_current_rate(base_rate: str, rates_df: pd.DataFrame, date: str = None):
    """Get current floating rate from FRED data"""
    if rates_df is None or base_rate == 'none':
        return 0.0
        
    if date is None:
        date = rates_df.index[-1]  # Most recent
    else:
        date = pd.Timestamp(date)
        
    # Map rate names to FRED columns
    rate_mapping = {
        'SOFR': 'SOFR',
        'EFFR': 'EFFR', 
        'DGS2': 'DGS2',
        'DGS5': 'DGS5',
        'DGS10': 'DGS10',
        'DGS30': 'DGS30'
    }
    
    if base_rate in rate_mapping:
        col = rate_mapping[base_rate]
        if col in rates_df.columns:
            # Get rate closest to date
            idx = rates_df.index.get_indexer([date], method='nearest')[0]
            return rates_df.iloc[idx][col]
    
    # Use last known rate or interpolate instead of arbitrary default
    if rates_df is not None and not rates_df.empty:
        # Use most recent available rate for this series
        available_cols = [c for c in ['SOFR', 'EFFR', 'DGS2', 'DGS5', 'DGS10'] if c in rates_df.columns]
        if available_cols:
            recent_rate = rates_df[available_cols[0]].dropna().iloc[-1]
            return recent_rate
    
    # This should never happen if FRED data is properly loaded
    raise ValueError(f"No FRED rate data available for {base_rate}. Check FRED data loading.")

def calculate_loan_current_rate(loan: EnhancedLoanSpec, rates_df: pd.DataFrame):
    """Calculate current all-in rate for a loan"""
    if loan.rate_type == 'fixed':
        return loan.origination_rate
    else:
        # Floating rate: base_rate + spread
        base_rate = get_current_rate(loan.base_rate, rates_df)
        return base_rate + loan.current_spread

def get_historical_rate(rate_type: str, date: str, rates_df: pd.DataFrame) -> float:
    """Get historical rate from FRED data for a specific date"""
    try:
        target_date = pd.Timestamp(date)
        
        # Find closest available date in FRED data
        available_dates = rates_df.index
        closest_idx = available_dates.get_indexer([target_date], method='nearest')[0]
        closest_date = available_dates[closest_idx]
        
        # Map rate types to FRED columns
        rate_mapping = {
            'DGS1': 'DGS2',   # Use 2Y as proxy for 1Y
            'DGS2': 'DGS2',
            'DGS5': 'DGS5', 
            'DGS10': 'DGS10',
            'DGS30': 'DGS30',
            'SOFR': 'SOFR',
            'EFFR': 'EFFR'
        }
        
        col = rate_mapping.get(rate_type, 'DGS10')
        if col in rates_df.columns:
            rate = rates_df.loc[closest_date, col]
            print(f"Historical {rate_type} on {target_date.strftime('%Y-%m-%d')}: {rate:.3%}")
            return rate
            
    except Exception as e:
        print(f"Error getting historical rate for {rate_type} on {date}: {e}")
    
    # Fallback using current rate if historical lookup fails
    return get_current_rate(rate_type, rates_df)

def calculate_floating_rate_for_period(bond_or_loan, period_date: pd.Timestamp, rates_df: pd.DataFrame) -> float:
    """
    Calculate floating rate for a specific period using forward rate projections when available
    Falls back to current rates if forward projections not available
    """
    
    # Try to use forward rate lookup first
    try:
        from forward_rate_lookup import get_global_forward_rate_lookup
        forward_lookup = get_global_forward_rate_lookup()
        
        if forward_lookup.forward_rates_dir and hasattr(bond_or_loan, 'base_rate'):
            # Use historical for past dates, forward for future dates
            forward_base_rate = forward_lookup.get_forward_rate(
                bond_or_loan.base_rate, 
                period_date.strftime('%Y-%m-%d'),
                rates_df  # Pass FRED data for historical lookups
            )
            spread = bond_or_loan.current_spread if hasattr(bond_or_loan, 'current_spread') else bond_or_loan.spread
            return forward_base_rate + spread
    except Exception as e:
        print(f"Forward rate lookup failed, using current rate fallback: {e}")
    
    # Fallback to current rate approach
    # Determine reset date based on frequency
    if hasattr(bond_or_loan, 'floating_reset_freq'):
        reset_freq = bond_or_loan.floating_reset_freq
    else:
        reset_freq = 'quarterly'  # Default
    
    # Calculate most recent reset date
    if reset_freq == 'monthly':
        reset_date = period_date.replace(day=1)  # First of month
    elif reset_freq == 'quarterly':
        quarter_starts = [1, 4, 7, 10]  # Jan, Apr, Jul, Oct
        current_quarter = ((period_date.month - 1) // 3) * 3 + 1
        reset_date = period_date.replace(month=current_quarter, day=1)
    elif reset_freq == 'semi_annual':
        reset_date = period_date.replace(month=1 if period_date.month <= 6 else 7, day=1)
    elif reset_freq == 'annual':
        reset_date = period_date.replace(month=1, day=1)
    else:
        reset_date = period_date  # Default to period date
    
    # Get base rate at reset date using current rate approach
    if hasattr(bond_or_loan, 'base_rate'):
        base_rate = get_current_rate(bond_or_loan.base_rate, rates_df, reset_date.strftime('%Y-%m-%d'))
        spread = bond_or_loan.current_spread if hasattr(bond_or_loan, 'current_spread') else bond_or_loan.spread
        return base_rate + spread
    
    return 0.05  # Should not reach here

def calculate_bond_current_ytm(bond: EnhancedBondSpec, rates_df: pd.DataFrame):
    """Calculate current YTM for a bond with convexity adjustments using historical data"""
    if bond.rate_type == 'fixed':
        # Get actual historical treasury rate when bond was issued
        issue_date = bond.issue_date
        
        # Determine appropriate benchmark based on bond duration
        if bond.duration <= 3:
            benchmark_rate = 'DGS2'
        elif bond.duration <= 7:
            benchmark_rate = 'DGS5'
        elif bond.duration <= 20:
            benchmark_rate = 'DGS10'
        else:
            benchmark_rate = 'DGS30'
        
        # Get historical rate when issued (vintage treasury)
        vintage_treasury = get_historical_rate(benchmark_rate, issue_date, rates_df)
        
        # Get current treasury rate
        current_treasury = get_current_rate(benchmark_rate, rates_df)
        
        # Interest rate change since issuance
        rate_change = current_treasury - vintage_treasury
        
        # Duration adjustment (first-order price sensitivity)
        duration_adjustment = -rate_change * bond.duration / 100
        
        # Convexity adjustment (second-order) - reduces duration impact for rate increases
        convexity_adjustment = 0.5 * (rate_change ** 2) * bond.convexity / 10000
        
        # Credit spread adjustment based on current market conditions
        current_ig_spread = get_current_rate('IG_CREDIT', rates_df) if 'IG_CREDIT' in rates_df.columns else 0.015
        vintage_ig_spread = get_historical_rate('IG_CREDIT', issue_date, rates_df) if 'IG_CREDIT' in rates_df.columns else 0.012
        credit_spread_change = current_ig_spread - vintage_ig_spread
        
        # Liquidity adjustment
        liquidity_adjustment = bond.oas_spread * (10 - bond.liquidity_score) / 10
        
        # Total yield adjustment
        total_adjustment = (duration_adjustment + convexity_adjustment + 
                          credit_spread_change + liquidity_adjustment)
        
        return bond.vintage_yield + total_adjustment
    else:
        # Floating rate bond - calculate current rate based on most recent reset
        today = pd.Timestamp.today()
        current_rate = calculate_floating_rate_for_period(bond, today, rates_df)
        return current_rate

def calculate_prepayment_speed(loan: EnhancedLoanSpec) -> float:
    """Calculate expected prepayment speed based on loan characteristics"""
    
    # Base prepayment speed (annual CPR - Conditional Prepayment Rate)
    base_speed = 0.05  # 5% annual base rate
    
    # Adjust based on loan-to-value (higher LTV = lower prepayment ability)
    if loan.ltv > 0.8:
        ltv_adjustment = -0.02  # High LTV reduces prepayment
    elif loan.ltv < 0.6:
        ltv_adjustment = 0.015  # Low LTV increases prepayment ability
    else:
        ltv_adjustment = 0
    
    # Adjust based on debt service coverage ratio (higher DSCR = more ability to prepay)
    if loan.dscr > 1.5:
        dscr_adjustment = 0.01  # Strong cash flow increases prepayment
    elif loan.dscr < 1.2:
        dscr_adjustment = -0.015  # Weak cash flow reduces prepayment
    else:
        dscr_adjustment = 0
    
    # Adjust based on credit rating (better credit = more refinancing options)
    rating_adjustments = {
        'A': 0.02, 'A-': 0.015, 'BBB+': 0.01, 'BBB': 0.005, 'BBB-': 0,
        'BB+': -0.005, 'BB': -0.01, 'BB-': -0.015, 
        'B+': -0.02, 'B': -0.025, 'B-': -0.03
    }
    rating_adjustment = rating_adjustments.get(loan.credit_rating, -0.01)
    
    # Sector-based adjustments (some sectors prepay more than others)
    sector_adjustments = {
        'Technology': 0.01,  # Tech companies often have more cash
        'Healthcare': 0.005,
        'Real Estate': -0.005,  # Real estate is illiquid
        'Energy': -0.01,  # Cyclical, less predictable cash flows
        'Transportation': -0.015  # Capital intensive, less cash
    }
    sector_adjustment = sector_adjustments.get(loan.sector, 0)
    
    # Geography adjustments (some markets more active)
    if loan.geography in ['California', 'New York', 'Texas']:
        geo_adjustment = 0.005  # Active markets
    else:
        geo_adjustment = 0
    
    # Calculate final prepayment speed
    total_speed = (base_speed + ltv_adjustment + dscr_adjustment + 
                  rating_adjustment + sector_adjustment + geo_adjustment)
    
    # Ensure reasonable bounds
    return max(0.01, min(0.25, total_speed))  # Between 1% and 25% annual

def apply_prepayment_and_default_adjustments(returns: dict, loans: list, bonds: list):
    """Apply comprehensive prepayment speeds and credit losses to expected returns"""
    adjusted_returns = returns.copy()
    
    # Adjust loan returns for prepayments and defaults
    for loan in loans:
        loan_id = loan.loan_id
        if loan_id in adjusted_returns:
            # Enhanced prepayment modeling based on loan characteristics
            base_prepay_speed = calculate_prepayment_speed(loan)
            
            # Prepayment penalty impact on returns
            if loan.prepay_penalty > 0:
                # Higher penalties discourage prepayment, extending duration
                penalty_effect = loan.prepay_penalty * 0.5  # Penalty provides some income
                # But reduces expected prepayment speed
                adjusted_prepay_speed = base_prepay_speed * (1 - loan.prepay_penalty)
            else:
                penalty_effect = 0
                adjusted_prepay_speed = base_prepay_speed
            
            # Duration impact of prepayment
            # Faster prepayment = shorter duration = less interest rate risk but less total interest
            duration_impact = -adjusted_prepay_speed * 0.3  # Negative impact on yield
            
            # Net prepayment adjustment
            prepay_adjustment = penalty_effect + duration_impact
            
            # Default probability reduces expected return
            default_adjustment = -loan.default_prob * 0.6  # Recovery assumption ~40%
            
            # Combine adjustments
            total_adjustment = prepay_adjustment + default_adjustment
            adjusted_returns[loan_id] = max(0.01, adjusted_returns[loan_id] + total_adjustment)
    
    # Adjust bond returns for credit risk
    for bond in bonds:
        bond_id = bond.bond_id
        if bond_id in adjusted_returns:
            # Credit adjustment based on rating and liquidity
            credit_risk_map = {
                'AAA': 0.0001, 'AA+': 0.0002, 'AA': 0.0003, 'AA-': 0.0005,
                'A+': 0.0008, 'A': 0.0012, 'A-': 0.0018,
                'BBB+': 0.0025, 'BBB': 0.0035, 'BBB-': 0.0050,
                'BB+': 0.0080, 'BB': 0.0120, 'BB-': 0.0180,
                'B+': 0.0250, 'B': 0.0350, 'B-': 0.0500,
                'CCC+': 0.0800, 'CCC': 0.1200, 'CCC-': 0.1800
            }
            
            default_prob = credit_risk_map.get(bond.credit_rating, 0.02)
            liquidity_adjustment = (10 - bond.liquidity_score) * 0.0005
            
            total_adjustment = -default_prob * 0.5 - liquidity_adjustment
            adjusted_returns[bond_id] = max(0.01, adjusted_returns[bond_id] + total_adjustment)
    
    return adjusted_returns

# ============================================================================
# Advanced OU Modeling and MTM Return Calculations
# ============================================================================

@dataclass
class OUParameters:
    """Ornstein-Uhlenbeck process parameters for yield modeling"""
    mu: float      # Long-term mean level (%)
    kappa: float   # Mean reversion strength (higher = faster reversion)
    sigma: float   # Volatility (%)
    
# Market-differentiated OU parameters
OU_PARAMS = {
    'IG': OUParameters(mu=0.035, kappa=0.15, sigma=0.008),    # Investment Grade: lower vol, moderate reversion
    'HY': OUParameters(mu=0.065, kappa=0.25, sigma=0.015),   # High Yield: higher vol, faster reversion  
    'TREASURY': OUParameters(mu=0.040, kappa=0.12, sigma=0.006)  # Treasury: lowest vol, slow reversion
}

def classify_credit_quality(rating: str) -> str:
    """Classify bond into IG, HY, or TREASURY based on credit rating"""
    ig_ratings = ['AAA', 'AA+', 'AA', 'AA-', 'A+', 'A', 'A-', 'BBB+', 'BBB']
    if rating in ig_ratings:
        return 'IG'
    else:
        return 'HY'

def simulate_ou_yield_path(y0: float, params: OUParameters, n_periods: int = 252, dt: float = 1/252, random_seed: int = 42) -> np.ndarray:
    """
    Simulate Ornstein-Uhlenbeck mean-reverting yield path
    dy = kappa * (mu - y) * dt + sigma * sqrt(dt) * dW
    
    Args:
        y0: Initial yield level
        params: OU parameters (mu, kappa, sigma)
        n_periods: Number of periods to simulate
        dt: Time step (e.g., 1/252 for daily, 1/12 for monthly)
        random_seed: Random seed for reproducibility
    """
    np.random.seed(random_seed)
    
    yields = np.zeros(n_periods + 1)
    yields[0] = y0
    
    for t in range(n_periods):
        # OU process increment
        drift = params.kappa * (params.mu - yields[t]) * dt
        diffusion = params.sigma * np.sqrt(dt) * np.random.normal()
        
        yields[t + 1] = yields[t] + drift + diffusion
        
        # Ensure non-negative yields
        yields[t + 1] = max(0.0001, yields[t + 1])
    
    return yields[1:]  # Return path without initial value

def calculate_bond_monthly_returns_with_rolldown(bond: EnhancedBondSpec, 
                                               rates_df: pd.DataFrame,
                                               n_months: int = 36) -> pd.DataFrame:
    """
    Calculate monthly bond returns using OU yield simulation with proper MTM valuation
    Trading/Portfolio Context: MTM valuation changes ARE returns (capital gains/losses)
    Total Return = Coupon Income + Capital Appreciation/Depreciation from yield changes
    """
    
    # Classify bond for OU parameters
    credit_class = classify_credit_quality(bond.credit_rating)
    ou_params = OU_PARAMS[credit_class]
    
    # Determine benchmark yield based on duration
    if bond.duration <= 3:
        benchmark = 'DGS2'
    elif bond.duration <= 7:
        benchmark = 'DGS5'  
    elif bond.duration <= 20:
        benchmark = 'DGS10'
    else:
        benchmark = 'DGS30'
    
    initial_treasury = get_historical_rate(benchmark, bond.issue_date, rates_df)
    initial_yield = initial_treasury + bond.oas_spread
    
    # Simulate yield path
    yield_path = simulate_ou_yield_path(initial_yield, ou_params, n_months, dt=1/12, random_seed=42)
    
    # Calculate monthly returns with MTM valuation changes
    monthly_returns = []
    monthly_data = []
    
    base_settle_date = pd.Timestamp(bond.issue_date)
    previous_price = None
    
    for month in range(n_months):
        # Current settlement date (rolls forward each month)
        current_settle = base_settle_date + pd.DateOffset(months=month)
        
        # Skip if past maturity
        maturity_date = pd.Timestamp(bond.maturity_date)
        if current_settle >= maturity_date:
            break
            
        # Current period yield
        y_current = yield_path[month] if month < len(yield_path) else yield_path[-1]
        
        try:
            # Create bond spec for current period (with roll-down)
            current_spec = BulletBondSpec(
                face_value=bond.face_value,
                coupon_rate=bond.coupon_rate,
                payment_frequency=bond.freq,
                maturity_date=bond.maturity_date,
                settlement_date=current_settle.strftime('%Y-%m-%d')
            )
            
            # Calculate current market price and metrics
            current_clean_price, current_dirty_price = bond_price_from_ytm(current_spec, y_current)
            mod_dur_current, _, conv_current = bond_duration_convexity(current_spec, y_current)
            
            # Calculate return components
            
            # 1. Coupon Income (if coupon payment occurs this month)
            months_from_issue = (current_settle.year - base_settle_date.year) * 12 + (current_settle.month - base_settle_date.month)
            is_coupon_month = (months_from_issue % (12 // bond.freq)) == 0
            monthly_coupon_income = (bond.coupon_rate / bond.freq) if is_coupon_month else 0.0
            
            # 2. Capital Appreciation/Depreciation (MTM valuation change)
            if previous_price is not None:
                price_return = (current_clean_price - previous_price) / previous_price
            else:
                price_return = 0.0  # First month
            
            # 3. Roll-down effect (separate from yield change effect)
            # Time decay benefit as bond approaches maturity
            time_to_maturity = (maturity_date - current_settle).days / 365.25
            previous_ttm = (maturity_date - (current_settle - pd.DateOffset(months=1))).days / 365.25 if month > 0 else time_to_maturity
            rolldown_return = 0.0001 * (previous_ttm - time_to_maturity) if month > 0 else 0.0  # Small roll-down benefit
            
            # Total return = Coupon Income + MTM Price Change + Roll-down
            total_return = monthly_coupon_income + price_return + rolldown_return
            
            # Store detailed information
            monthly_data.append({
                'Month': month + 1,
                'SettleDate': current_settle,
                'Yield': y_current,
                'YieldChange': yield_path[month + 1] - y_current if month + 1 < len(yield_path) else 0,
                'ModifiedDuration': mod_dur_current,
                'Convexity': conv_current,
                'CleanPrice': current_clean_price,
                'DirtyPrice': current_dirty_price,
                'CouponIncome': monthly_coupon_income,
                'PriceReturn': price_return,
                'RolldownReturn': rolldown_return,
                'TotalReturn': total_return,
                'TimeToMaturity': time_to_maturity,
                'IsCouponMonth': is_coupon_month
            })
            
            monthly_returns.append(total_return)
            previous_price = current_clean_price  # Store for next period's price return calculation
            
        except Exception as e:
            print(f"Error calculating MTM return for month {month + 1}: {e}")
            monthly_returns.append(0.0)
    
    return pd.DataFrame(monthly_data)

def calculate_loan_monthly_returns_income_based(loan: EnhancedLoanSpec,
                                              rates_df: pd.DataFrame,
                                              n_months: int = 36) -> pd.DataFrame:
    """
    Calculate private credit loan returns based on interest and fee income only
    Principal repayments are treated as return of capital, not return
    """
    
    # Calculate current rate
    if loan.rate_type == 'floating':
        base_rate = get_current_rate(loan.base_rate, rates_df)
        current_rate = base_rate + loan.current_spread
    else:
        current_rate = loan.origination_rate
        
    loan_spec = LoanSpec(
        principal=loan.principal,
        annual_interest_rate=current_rate,
        origination_date=loan.origination_date,
        term_months=loan.maturity_months,
        interest_only_months=loan.io_months,
        amortization_style=loan.amort_style,
        upfront_fee_percent=0.01,
        exit_fee_percent=loan.prepay_penalty / 2
    )
    
    # Generate loan schedule
    schedule = loan_schedule(loan_spec)
    
    # Calculate monthly returns based on interest income only
    monthly_returns_data = []
    
    for i, (date, row) in enumerate(schedule.iterrows()):
        if i == 0:  # Skip initial funding
            continue
            
        outstanding_start = row['OutstandingStart']
        interest_payment = row['Interest']
        fee_payment = row['Fees']
        principal_payment = row['Principal']  # This is return of capital, not return
        
        # Return calculation: (Interest + Fees) / Outstanding_Start
        # Principal repayments do not contribute to return calculation
        if outstanding_start > 0:
            interest_return = interest_payment / outstanding_start
            fee_return = fee_payment / outstanding_start
            total_income_return = interest_return + fee_return
            
            # For floating rate loans, update rate for this period
            if loan.rate_type == 'floating':
                period_rate = calculate_floating_rate_for_period(loan, date, rates_df)
                period_base_rate = get_current_rate(loan.base_rate, rates_df, date.strftime('%Y-%m-%d'))
                period_spread = loan.current_spread
            else:
                period_rate = loan_spec.annual_interest_rate  # Use the loan_spec rate
                period_base_rate = loan_spec.annual_interest_rate
                period_spread = 0.0
        else:
            total_income_return = 0.0
            period_rate = current_rate
            period_base_rate = 0.0
            period_spread = 0.0
        
        monthly_returns_data.append({
            'Month': i,
            'Date': date,
            'OutstandingStart': outstanding_start,
            'InterestPayment': interest_payment,
            'FeePayment': fee_payment,
            'PrincipalPayment': principal_payment,  # Return of capital
            'InterestReturn': interest_return if outstanding_start > 0 else 0,
            'FeeReturn': fee_return if outstanding_start > 0 else 0,
            'TotalIncomeReturn': total_income_return,
            'PeriodRate': period_rate,
            'PeriodBaseRate': period_base_rate,
            'PeriodSpread': period_spread,
            'CreditRating': loan.credit_rating,
            'DefaultProb': loan.default_prob,
            'LTV': loan.ltv,
            'DSCR': loan.dscr
        })
    
    return pd.DataFrame(monthly_returns_data)

def generate_ou_based_daily_returns_for_all_bonds(bonds: list, rates_df: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """
    Generate OU-based daily returns for ALL bonds using sophisticated mean-reverting yield modeling
    
    Args:
        bonds: List of all bond specifications
        rates_df: FRED rate data
        dates: Date index for return series
        
    Returns:
        DataFrame with OU-generated daily returns for all bonds
    """
    print(f"Generating OU-based returns for {len(bonds)} bonds...")
    
    bond_returns_matrix = pd.DataFrame(index=dates)
    
    for i, bond in enumerate(bonds):
        try:
            # Skip matured bonds
            maturity_date = pd.Timestamp(bond.maturity_date)
            if maturity_date <= pd.Timestamp.today():
                continue
                
            # Classify bond for OU parameters
            credit_class = classify_credit_quality(bond.credit_rating)
            ou_params = OU_PARAMS[credit_class]
            
            # Get initial yield from historical data
            if bond.duration <= 3:
                benchmark = 'DGS2'
            elif bond.duration <= 7:
                benchmark = 'DGS5'
            elif bond.duration <= 20:
                benchmark = 'DGS10'
            else:
                benchmark = 'DGS30'
            
            initial_treasury = get_historical_rate(benchmark, bond.issue_date, rates_df)
            initial_yield = initial_treasury + bond.oas_spread
            
            # Generate OU yield path for the entire date range
            n_periods = len(dates)
            # Use unique random seed for each bond based on bond ID
            bond_seed = 42 + abs(hash(bond.bond_id)) % 1000
            yield_path = simulate_ou_yield_path(
                y0=initial_yield, 
                params=ou_params, 
                n_periods=n_periods, 
                dt=1/252,
                random_seed=bond_seed
            )
            
            # Convert yield changes to bond returns using duration-convexity approximation
            daily_returns = []
            
            for j in range(len(dates)):
                if j == 0:
                    # First day - baseline
                    daily_returns.append(bond.coupon_rate / 252)  # Daily coupon accrual
                else:
                    # Calculate return from yield change
                    y_prev = yield_path[j-1] if j-1 < len(yield_path) else yield_path[-1]
                    y_curr = yield_path[j] if j < len(yield_path) else yield_path[-1]
                    delta_y = y_curr - y_prev
                    
                    # Duration-convexity return approximation
                    coupon_return = bond.coupon_rate / 252  # Daily coupon accrual
                    duration_return = -bond.duration * delta_y / 100  # Duration effect
                    convexity_return = 0.5 * bond.convexity * (delta_y ** 2) / 10000  # Convexity benefit
                    
                    total_return = coupon_return + duration_return + convexity_return
                    daily_returns.append(total_return)
            
            bond_returns_matrix[bond.bond_id] = daily_returns
            
            if (i + 1) % 20 == 0:  # Progress update every 20 bonds
                print(f"  Generated OU returns for {i+1} bonds...")
                
        except Exception as e:
            print(f"Error generating OU returns for {bond.bond_id}: {e}")
            # Fallback to simple daily return
            bond_returns_matrix[bond.bond_id] = bond.vintage_yield / 252
    
    print(f"✅ OU modeling complete for {len(bond_returns_matrix.columns)} active bonds")
    return bond_returns_matrix

def generate_treasury_factor_daily_returns_for_bonds(bonds: list, rates_df: pd.DataFrame, dates: pd.DatetimeIndex, adjusted_returns: dict) -> pd.DataFrame:
    """
    Generate treasury factor model daily returns for bonds (legacy approach)
    Uses real Treasury yield changes as systematic factors
    """
    print(f"Generating treasury factor returns for {len(bonds)} bonds...")
    
    bond_returns_matrix = pd.DataFrame(index=dates)
    
    # Use multiple real market factors for systematic risk
    treasury_2y_changes = rates_df['DGS2'].pct_change().reindex(dates, method='ffill').fillna(0)
    treasury_10y_changes = rates_df['DGS10'].pct_change().reindex(dates, method='ffill').fillna(0)
    sofr_changes = rates_df['SOFR'].pct_change().reindex(dates, method='ffill').fillna(0)
    
    for i, bond in enumerate(bonds):
        bond_id = bond.bond_id
        if bond_id not in adjusted_returns:
            continue
            
        annual_return = adjusted_returns[bond_id]
        daily_return = annual_return / 252
        
        # SCIENTIFIC BOND VOLATILITY MODEL
        duration_vol = bond.duration * 0.004  # ~40bp vol per year of duration
        
        # Credit spread volatility
        credit_vol_map = {
            'AAA': 0.02, 'AA+': 0.025, 'AA': 0.03, 'AA-': 0.035, 'A+': 0.04, 'A': 0.045, 'A-': 0.05,
            'BBB+': 0.06, 'BBB': 0.07, 'BBB-': 0.08, 'BB+': 0.12, 'BB': 0.15, 'BB-': 0.18,
            'B+': 0.22, 'B': 0.25, 'B-': 0.30, 'CCC+': 0.40, 'CCC': 0.50
        }
        credit_vol = credit_vol_map.get(bond.credit_rating, 0.10)
        
        # Liquidity volatility
        liquidity_vol = (10 - bond.liquidity_score) * 0.005
        
        # Convexity reduces volatility for large rate moves
        convexity_vol_reduction = bond.convexity * 0.0001
        
        volatility = duration_vol + credit_vol + liquidity_vol - convexity_vol_reduction
        
        # SCIENTIFIC BETA MODEL FOR BONDS
        if bond.rate_type == 'floating':
            treasury_2y_beta = 0.05
            treasury_10y_beta = 0.02
            sofr_beta = 0.1
        else:
            if bond.duration <= 3:
                treasury_2y_beta = 0.8
                treasury_10y_beta = 0.3
            elif bond.duration <= 10:
                treasury_2y_beta = 0.4
                treasury_10y_beta = 0.7
            else:
                treasury_2y_beta = 0.2
                treasury_10y_beta = 0.9
            sofr_beta = 0.1
        
        # Credit quality affects correlation
        if bond.credit_rating in ['AAA', 'AA+', 'AA', 'AA-', 'A+', 'A', 'A-']:
            credit_correlation_factor = 1.0  # Investment grade
        else:
            credit_correlation_factor = 1.3  # High yield more correlated
        
        # Apply multi-factor model
        systematic_component = (treasury_2y_beta * treasury_2y_changes + 
                              treasury_10y_beta * treasury_10y_changes +
                              sofr_beta * sofr_changes) * credit_correlation_factor
        
        daily_vol = volatility / np.sqrt(252)
        
        # Create deterministic idiosyncratic component
        asset_hash = abs(hash(bond_id)) % 1000
        time_component = np.sin(np.arange(len(dates)) * 2 * np.pi / 252 + asset_hash)
        sector_component = np.cos(np.arange(len(dates)) * 2 * np.pi / (252 * 3) + asset_hash * 2)
        
        # Scale by idiosyncratic volatility
        total_systematic_vol = np.std(systematic_component) if len(systematic_component) > 1 else 0
        idiosyncratic_vol = np.sqrt(max(0, daily_vol**2 - total_systematic_vol**2))
        
        idiosyncratic_component = (time_component + sector_component) * idiosyncratic_vol * 0.5
        
        # Combine all components
        daily_returns = daily_return + systematic_component + idiosyncratic_component
        
        # Add mean reversion
        mean_reversion = 0.001
        daily_returns = pd.Series(daily_returns, index=dates)
        for j in range(1, len(daily_returns)):
            daily_returns.iloc[j] = (1 - mean_reversion) * daily_returns.iloc[j] + mean_reversion * daily_return
        
        bond_returns_matrix[bond_id] = daily_returns
    
    print(f"✅ Treasury factor modeling complete for {len(bond_returns_matrix.columns)} bonds")
    return bond_returns_matrix

def enhance_floating_bonds_with_nelson_siegel(bonds: list, rates_df: pd.DataFrame) -> Dict:
    """
    Enhance floating rate bonds using Nelson-Siegel forward curve projections
    Fixes the flat rate issue by using market-embedded rate expectations
    """
    from yield_curve_modeling import enhance_floating_bond_cashflows_with_nelson_siegel
    
    enhanced_floating_bonds = {}
    floating_bonds = [b for b in bonds if b.rate_type == 'floating']
    
    print(f"🏦 Enhancing {len(floating_bonds)} floating rate bonds with Nelson-Siegel...")
    
    for bond in floating_bonds:
        try:
            enhanced_cf = enhance_floating_bond_cashflows_with_nelson_siegel(bond, rates_df)
            if not enhanced_cf.empty:
                enhanced_floating_bonds[bond.bond_id] = enhanced_cf
                
                # Show rate evolution for first few bonds
                if len(enhanced_floating_bonds) <= 3:
                    rate_range = f"{enhanced_cf['projected_base_rate'].min():.3%} to {enhanced_cf['projected_base_rate'].max():.3%}"
                    print(f"  ✅ {bond.bond_id} ({bond.issuer}): {rate_range}")
                    
        except Exception as e:
            print(f"  ❌ Error enhancing {bond.bond_id}: {e}")
    
    print(f"✅ Enhanced {len(enhanced_floating_bonds)} floating bonds with Nelson-Siegel forward curves")
    return enhanced_floating_bonds

def export_cashflows_during_run(loans: list, bonds: list, rates_df: pd.DataFrame, adjusted_returns: dict):
    """Export individual cashflows to txt files during portfolio run"""
    import os
    
    # Create timestamped directory
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = f"cashflows_{timestamp}"
    os.makedirs(output_dir, exist_ok=True)
    
    print(f"\n--- Exporting Cashflows to {output_dir}/ ---")
    
    # Export loan cashflows
    loan_count = 0
    for loan in loans:  # Export ALL loans
        try:
            # Calculate current rate
            if loan.rate_type == 'floating':
                base_rate = get_current_rate(loan.base_rate, rates_df)
                current_rate = base_rate + loan.current_spread
            else:
                current_rate = loan.origination_rate
            
            # Create loan schedule
            loan_spec = LoanSpec(
                principal=loan.principal,
                annual_interest_rate=current_rate,
                origination_date=loan.origination_date,
                term_months=loan.maturity_months,
                interest_only_months=loan.io_months,
                amortization_style=loan.amort_style,
                upfront_fee_percent=0.01,
                exit_fee_percent=loan.prepay_penalty / 2,
                benchmark_rate=get_current_rate('DGS2', rates_df, loan.origination_date) if rates_df is not None else 0.03
            )
            
            schedule = loan_schedule(loan_spec)
            irr, moic = loan_IRR_and_MOIC(schedule)
            
            # Enhanced cashflow breakdown with detailed columns
            enhanced_schedule = schedule.copy()
            
            # Add period-specific floating rate calculations
            if loan.rate_type == 'floating':
                period_rates = []
                period_base_rates = []
                period_spreads = []
                
                for period_date in enhanced_schedule.index:
                    if period_date > pd.Timestamp(loan.origination_date):
                        period_rate = calculate_floating_rate_for_period(loan, period_date, rates_df)
                        
                        # Use same historical/forward logic for base rate display
                        try:
                            from forward_rate_lookup import get_global_forward_rate_lookup
                            forward_lookup = get_global_forward_rate_lookup()
                            base_rate = forward_lookup.get_forward_rate(loan.base_rate, period_date.strftime('%Y-%m-%d'), rates_df)
                        except:
                            # Fallback to old method
                            base_rate = get_current_rate(loan.base_rate, rates_df, period_date.strftime('%Y-%m-%d'))
                        
                        period_rates.append(period_rate)
                        period_base_rates.append(base_rate)
                        period_spreads.append(loan.current_spread)
                    else:
                        period_rates.append(current_rate)
                        period_base_rates.append(0)
                        period_spreads.append(0)
                
                enhanced_schedule['PeriodBaseRate'] = period_base_rates
                enhanced_schedule['PeriodSpread'] = period_spreads  
                enhanced_schedule['PeriodAllInRate'] = period_rates
            else:
                enhanced_schedule['PeriodBaseRate'] = current_rate
                enhanced_schedule['PeriodSpread'] = 0.0
                enhanced_schedule['PeriodAllInRate'] = current_rate
            
            # Detailed prepayment modeling
            prepay_speed = calculate_prepayment_speed(loan)
            enhanced_schedule['PrepaySpeed'] = prepay_speed
            enhanced_schedule['PrepayAmount'] = enhanced_schedule['OutstandingStart'] * prepay_speed / 12  # Monthly
            enhanced_schedule['PrepayPenaltyRate'] = loan.prepay_penalty
            enhanced_schedule['PrepayPenaltyAmount'] = enhanced_schedule['PrepayAmount'] * loan.prepay_penalty
            enhanced_schedule['NetPrepayment'] = enhanced_schedule['PrepayAmount'] - enhanced_schedule['PrepayPenaltyAmount']
            
            # Detailed credit default modeling
            enhanced_schedule['DefaultProb'] = loan.default_prob / 12  # Monthly default probability
            # Calculate survival probability properly
            default_prob_monthly = loan.default_prob / 12
            survival_probs = []
            cumulative_survival = 1.0
            for i in range(len(enhanced_schedule)):
                cumulative_survival *= (1 - default_prob_monthly)
                survival_probs.append(cumulative_survival)
            enhanced_schedule['SurvivalProb'] = survival_probs
            enhanced_schedule['ExpectedLossRate'] = loan.default_prob * 0.6  # 60% loss given default
            enhanced_schedule['ExpectedLossAmount'] = enhanced_schedule['OutstandingStart'] * enhanced_schedule['ExpectedLossRate'] / 12
            enhanced_schedule['CreditAdjustment'] = -enhanced_schedule['ExpectedLossAmount']
            
            # Recovery modeling
            enhanced_schedule['RecoveryRate'] = 0.4  # 40% recovery assumption
            enhanced_schedule['RecoveryAmount'] = enhanced_schedule['ExpectedLossAmount'] * (-enhanced_schedule['RecoveryRate'] / -0.6)  # Partial recovery
            
            # Final cashflow calculation
            enhanced_schedule['GrossCashflow'] = enhanced_schedule['Total']
            enhanced_schedule['PrepaymentAdjustment'] = enhanced_schedule['NetPrepayment']
            enhanced_schedule['CreditAdjustment'] = enhanced_schedule['CreditAdjustment']
            enhanced_schedule['RecoveryAdjustment'] = enhanced_schedule['RecoveryAmount']
            enhanced_schedule['NetCashflow'] = (enhanced_schedule['GrossCashflow'] + 
                                              enhanced_schedule['PrepaymentAdjustment'] +
                                              enhanced_schedule['CreditAdjustment'] + 
                                              enhanced_schedule['RecoveryAdjustment'])
            
            schedule = enhanced_schedule
            
            # Export to file
            filename = f"{loan.loan_id}_{loan.borrower.replace(' ', '_').replace('&', 'and')}_cashflows.txt"
            filepath = os.path.join(output_dir, filename)
            
            with open(filepath, 'w') as f:
                f.write("="*80 + "\n")
                f.write(f"LOAN CASHFLOW ANALYSIS: {loan.loan_id}\n")
                f.write(f"Generated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("="*80 + "\n")
                f.write(f"Borrower: {loan.borrower}\n")
                f.write(f"Sector: {loan.sector} | Geography: {loan.geography}\n")
                f.write(f"Principal: ${loan.principal:,.0f}\n")
                f.write(f"Rate Type: {loan.rate_type}\n")
                if loan.rate_type == 'floating':
                    f.write(f"Base Rate: {loan.base_rate} ({base_rate:.2%})\n")
                    f.write(f"Spread: {loan.current_spread:.2%}\n")
                    f.write(f"All-in Rate: {current_rate:.2%}\n")
                else:
                    f.write(f"Fixed Rate: {current_rate:.2%}\n")
                f.write(f"Credit Rating: {loan.credit_rating}\n")
                f.write(f"Default Probability: {loan.default_prob:.2%}\n")
                f.write(f"LTV: {loan.ltv:.1%} | DSCR: {loan.dscr:.2f}x\n")
                f.write(f"Prepayment Penalty: {loan.prepay_penalty:.2%}\n")
                f.write(f"Seniority: {loan.seniority}\n")
                f.write(f"Collateral: {loan.collateral_type}\n")
                f.write(f"\nPORTFOLIO METRICS:\n")
                f.write(f"IRR (base): {irr:.2%}\n")
                f.write(f"IRR (adjusted): {adjusted_returns.get(loan.loan_id, irr):.2%}\n")
                f.write(f"MOIC: {moic:.3f}x\n")
                f.write("\n" + "="*80 + "\n")
                f.write("DETAILED CASHFLOW SCHEDULE\n")
                f.write("="*120 + "\n")
                
                # Write comprehensive header
                f.write(f"{'Date':>12} | {'Outstanding':>12} | {'BaseRate':>9} | {'Spread':>8} | {'AllIn':>8} | ")
                f.write(f"{'Interest':>10} | {'Principal':>10} | {'PrepayAmt':>10} | {'PrepayPen':>10} | ")
                f.write(f"{'DefaultLoss':>10} | {'Recovery':>10} | {'NetCF':>10}\n")
                f.write("-" * 120 + "\n")
                
                # Write detailed cashflow data
                for idx, row in schedule.iterrows():
                    f.write(f"{idx.strftime('%Y-%m-%d'):>12} | ")
                    f.write(f"${row['OutstandingStart']:>11,.0f} | ")
                    f.write(f"{row.get('PeriodBaseRate', current_rate):>8.3f} | ")
                    f.write(f"{row.get('PeriodSpread', 0):>7.3f} | ")
                    f.write(f"{row.get('PeriodAllInRate', current_rate):>7.3f} | ")
                    f.write(f"${row['Interest']:>9,.0f} | ")
                    f.write(f"${row['Principal']:>9,.0f} | ")
                    f.write(f"${row.get('PrepayAmount', 0):>9,.0f} | ")
                    f.write(f"${row.get('PrepayPenaltyAmount', 0):>9,.0f} | ")
                    f.write(f"${abs(row.get('CreditAdjustment', 0)):>9,.0f} | ")
                    f.write(f"${row.get('RecoveryAmount', 0):>9,.0f} | ")
                    f.write(f"${row['NetCashflow']:>9,.0f}\n")
                
                f.write("\n" + "="*120 + "\n")
                f.write("COLUMN DEFINITIONS:\n")
                f.write("="*120 + "\n")
                f.write("Outstanding: Principal balance at period start\n")
                f.write("BaseRate: Historical base rate (SOFR/Treasury) for floating loans\n")
                f.write("Spread: Credit spread over base rate\n") 
                f.write("AllIn: Total interest rate (BaseRate + Spread)\n")
                f.write("Interest: Interest payment for period\n")
                f.write("Principal: Scheduled principal payment\n")
                f.write("PrepayAmt: Expected prepayment amount based on borrower characteristics\n")
                f.write("PrepayPen: Penalty fee on prepayments\n")
                f.write("DefaultLoss: Expected credit loss for the period\n")
                f.write("Recovery: Expected recovery value in case of default\n")
                f.write("NetCF: Final net cashflow after all adjustments\n")
            
            loan_count += 1
            
        except Exception as e:
            print(f"Error exporting {loan.loan_id}: {e}")
    
    # Export bond cashflows
    bond_count = 0
    for bond in bonds:  # Export ALL bonds
        try:
            current_ytm = adjusted_returns.get(bond.bond_id, bond.vintage_yield)
            
            # Generate coupon dates (simplified)
            maturity = pd.Timestamp(bond.maturity_date)
            settle = pd.Timestamp.today()
            
            if maturity <= settle:
                continue  # Skip matured bonds
                
            # Generate payment dates
            months_between = 12 // bond.freq
            payment_dates = []
            current_date = maturity
            
            while current_date > settle:
                payment_dates.append(current_date)
                current_date = current_date - pd.DateOffset(months=months_between)
                
            payment_dates = sorted([d for d in payment_dates if d > settle])
            
            if not payment_dates:
                continue
            
            # Create cashflows
            coupon_amount = bond.face_value * bond.coupon_rate / bond.freq
            
            # Credit adjustments
            credit_default_map = {
                'AAA': 0.0001, 'AA+': 0.0002, 'AA': 0.0003, 'AA-': 0.0005,
                'A+': 0.0008, 'A': 0.0012, 'A-': 0.0018,
                'BBB+': 0.0025, 'BBB': 0.0035, 'BBB-': 0.0050,
                'BB+': 0.0080, 'BB': 0.0120, 'BB-': 0.0180,
                'B+': 0.0250, 'B': 0.0350, 'B-': 0.0500,
                'CCC+': 0.0800, 'CCC': 0.1200, 'CCC-': 0.1800
            }
            
            default_prob = credit_default_map.get(bond.credit_rating, 0.02)
            recovery_rate = 0.4
            
            # Export to file
            filename = f"{bond.bond_id}_{bond.issuer.replace(' ', '_').replace('.', '').replace('&', 'and')}_cashflows.txt"
            filepath = os.path.join(output_dir, filename)
            
            with open(filepath, 'w') as f:
                f.write("="*80 + "\n")
                f.write(f"BOND CASHFLOW ANALYSIS: {bond.bond_id}\n")
                f.write(f"Generated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("="*80 + "\n")
                f.write(f"Issuer: {bond.issuer}\n")
                f.write(f"Sector: {bond.sector} | Geography: {bond.geography}\n")
                f.write(f"Face Value: ${bond.face_value:,.0f}\n")
                f.write(f"Coupon Rate: {bond.coupon_rate:.2%}\n")
                f.write(f"Rate Type: {bond.rate_type}\n")
                if bond.rate_type == 'floating':
                    f.write(f"Base Rate: {bond.base_rate}\n")
                    f.write(f"Spread: {bond.spread:.2%}\n")
                f.write(f"Payment Frequency: {bond.freq}x per year\n")
                f.write(f"Credit Rating: {bond.credit_rating}\n")
                f.write(f"Duration: {bond.duration:.2f} | Convexity: {bond.convexity:.2f}\n")
                f.write(f"Liquidity Score: {bond.liquidity_score:.1f}/10\n")
                f.write(f"Callable: {bond.callable} | Seniority: {bond.seniority}\n")
                f.write(f"\nPORTFOLIO METRICS:\n")
                f.write(f"Current YTM: {current_ytm:.2%}\n")
                f.write(f"Default Probability: {default_prob:.3%}\n")
                f.write(f"Expected Recovery: {recovery_rate:.1%}\n")
                f.write("\n" + "="*80 + "\n")
                f.write("DETAILED BOND CASHFLOW SCHEDULE\n")
                f.write("="*120 + "\n")
                
                # Write comprehensive header for bonds
                f.write(f"{'Date':>12} | {'CouponRate':>10} | {'BaseRate':>9} | {'Spread':>8} | ")
                f.write(f"{'Coupon':>10} | {'Principal':>10} | {'DefaultLoss':>10} | {'Recovery':>10} | {'NetCF':>10}\n")
                f.write("-" * 120 + "\n")
                
                # Write detailed bond cashflow data
                for i, date in enumerate(payment_dates):
                    # Calculate period-specific rates for floating rate bonds
                    if bond.rate_type == 'floating':
                        # Use forward rate lookup if available, otherwise current rate
                        try:
                            from forward_rate_lookup import get_global_forward_rate_lookup
                            forward_lookup = get_global_forward_rate_lookup()
                            
                            if forward_lookup.forward_rates_dir:
                                # Use historical for past dates, forward for future dates
                                period_base_rate = forward_lookup.get_forward_rate(
                                    bond.base_rate, 
                                    date.strftime('%Y-%m-%d'),
                                    rates_df  # Pass FRED data for historical lookups
                                )
                            else:
                                # Fallback to current rate
                                period_base_rate = get_current_rate(bond.base_rate, rates_df, date.strftime('%Y-%m-%d'))
                        except:
                            # Fallback if forward rate system not available
                            period_base_rate = get_current_rate(bond.base_rate, rates_df, date.strftime('%Y-%m-%d'))
                        
                        period_coupon_rate = period_base_rate + bond.spread
                        period_coupon = bond.face_value * period_coupon_rate / bond.freq
                    else:
                        period_base_rate = get_historical_rate('DGS10', bond.issue_date, rates_df)
                        period_coupon_rate = bond.coupon_rate
                        period_coupon = coupon_amount
                    
                    if i == len(payment_dates) - 1:  # Final payment
                        coupon = period_coupon
                        principal = bond.face_value
                        total = coupon + principal
                    else:  # Coupon payments only
                        coupon = period_coupon
                        principal = 0
                        total = coupon
                    
                    # Enhanced credit modeling
                    periods_from_now = i + 1
                    period_default_prob = default_prob * periods_from_now / len(payment_dates)
                    survival_prob = (1 - default_prob / len(payment_dates)) ** periods_from_now
                    
                    expected_loss = total * period_default_prob
                    recovery_value = expected_loss * recovery_rate
                    net_credit_adjustment = recovery_value - expected_loss
                    
                    adjusted_total = total * survival_prob + recovery_value
                    
                    f.write(f"{date.strftime('%Y-%m-%d'):>12} | ")
                    f.write(f"{period_coupon_rate:>9.3%} | ")
                    f.write(f"{period_base_rate:>8.3%} | ")
                    f.write(f"{bond.spread if bond.rate_type=='floating' else 0:>7.3%} | ")
                    f.write(f"${coupon:>9,.0f} | ")
                    f.write(f"${principal:>9,.0f} | ")
                    f.write(f"${expected_loss:>9,.0f} | ")
                    f.write(f"${recovery_value:>9,.0f} | ")
                    f.write(f"${adjusted_total:>9,.0f}\n")
                
                f.write("\n" + "="*120 + "\n")
                f.write("COLUMN DEFINITIONS:\n")
                f.write("="*120 + "\n")
                f.write("CouponRate: Period-specific coupon rate (fixed or floating)\n")
                f.write("BaseRate: Historical base rate from FRED data\n")
                f.write("Spread: Credit spread over base rate (floating bonds only)\n")
                f.write("Coupon: Coupon payment for the period\n")
                f.write("Principal: Principal repayment (final period only)\n")
                f.write("DefaultLoss: Expected credit loss for the period\n")
                f.write("Recovery: Expected recovery value in case of default\n")
                f.write("NetCF: Final net cashflow after credit adjustments\n")
            
            bond_count += 1
            
        except Exception as e:
            print(f"Error exporting {bond.bond_id}: {e}")
    
    # Create summary file
    summary_path = os.path.join(output_dir, "EXPORT_SUMMARY.txt")
    with open(summary_path, 'w') as f:
        f.write("="*80 + "\n")
        f.write("CASHFLOW EXPORT SUMMARY\n")
        f.write(f"Generated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("="*80 + "\n")
        f.write(f"Exported Loan Cashflows: {loan_count}\n")
        f.write(f"Exported Bond Cashflows: {bond_count}\n")
        f.write(f"Total Files: {loan_count + bond_count + 1}\n")
        f.write(f"\nDirectory: {os.path.abspath(output_dir)}\n")
        f.write("\nAll cashflows include:\n")
        f.write("- Real-time floating rate calculations\n")
        f.write("- Credit default probability adjustments\n")
        f.write("- Prepayment penalty impacts (loans)\n")
        f.write("- Expected loss provisions\n")
        f.write("- Recovery value estimates\n")
    
    print(f"Exported {loan_count} loan and {bond_count} bond cashflow files to {output_dir}/")
    return output_dir

def create_enhanced_bond_portfolio(use_ou_modeling: bool = True, 
                                  generate_forward_rates: bool = True):
    """
    Create bond portfolio using enriched specifications and real market data
    
    Args:
        use_ou_modeling: If True, use OU process for all bonds. If False, use treasury factor model.
        generate_forward_rates: If True, generate Nelson-Siegel forward rate projections.
    """
    
    print("=== Enhanced Bond Portfolio with Real Market Data ===")
    print(f"Bond modeling approach: {'OU Mean-Reverting Process' if use_ou_modeling else 'Treasury Factor Model'}")
    print(f"Forward rate projections: {'Nelson-Siegel' if generate_forward_rates else 'Current rates only'}")
    
    # Load data
    rates_df = load_fred_rates()
    loans = load_loans()
    bonds = load_bonds()
    
    if not loans or not bonds:
        print("Failed to load loan or bond data")
        return
    
    print(f"\\nLoaded {len(loans)} loans and {len(bonds)} bonds")
    print(f"Rate data available: {rates_df is not None}")
    
    # Generate forward rate projections using Nelson-Siegel
    forward_rates_dir = None
    if generate_forward_rates:
        print("\\n🏦 GENERATING NELSON-SIEGEL FORWARD RATE PROJECTIONS")
        print("="*60)
        try:
            from forward_rate_projections import export_all_forward_rate_projections
            
            forward_rates_dir = export_all_forward_rate_projections(
                rates_df=rates_df,
                projection_horizon_years=10.0
            )
            
            print(f"✅ Forward rate projections exported to: {forward_rates_dir}")
            
        except Exception as e:
            print(f"❌ Error generating forward rates: {e}")
            print("Continuing with current rate approach...")
            forward_rates_dir = None
    
    # Calculate current returns for all instruments
    returns = {}
    
    print("\\nCalculating loan returns with current market rates...")
    for loan in loans:
        try:
            current_rate = calculate_loan_current_rate(loan, rates_df)
            
            # Create LoanSpec for cashflow calculation
            loan_spec = LoanSpec(
                principal=loan.principal,
                annual_interest_rate=current_rate,
                origination_date=loan.origination_date,
                term_months=loan.maturity_months,
                interest_only_months=loan.io_months,
                amortization_style=loan.amort_style,
                upfront_fee_percent=0.01,  # Standard assumption
                exit_fee_percent=loan.prepay_penalty / 2,
                benchmark_rate=get_current_rate('DGS2', rates_df, loan.origination_date)
            )
            
            schedule = loan_schedule(loan_spec)
            irr, _ = loan_IRR_and_MOIC(schedule)
            returns[loan.loan_id] = irr
            
        except Exception as e:
            print(f"Error calculating return for {loan.loan_id}: {e}")
            # Use FRED data for structured fallback instead of hardcoded values
            if loan.rate_type == 'floating':
                # For floating rate loans, use current FRED rate + spread
                current_base_rate = get_current_rate(loan.base_rate, rates_df)
                returns[loan.loan_id] = current_base_rate + loan.current_spread
            else:
                # For fixed rate loans, use the original fixed rate
                returns[loan.loan_id] = loan.origination_rate
    
    print("Calculating bond returns with current market rates...")
    for bond in bonds:
        try:
            current_ytm = calculate_bond_current_ytm(bond, rates_df)
            returns[bond.bond_id] = current_ytm
            
        except Exception as e:
            print(f"Error calculating return for {bond.bond_id}: {e}")
            # Use FRED data for structured fallback based on bond characteristics
            if bond.rate_type == 'floating':
                # For floating rate bonds, use current FRED rate + spread
                current_base_rate = get_current_rate(bond.base_rate, rates_df)
                returns[bond.bond_id] = current_base_rate + bond.spread
            else:
                # For fixed rate bonds, use historical vs current FRED rates
                base_treasury = get_current_rate('DGS10', rates_df)  # Current 10Y from FRED
                vintage_treasury = get_historical_rate('DGS10', bond.issue_date, rates_df)  # Historical from FRED
                duration_adj = -(base_treasury - vintage_treasury) * bond.duration / 100
                credit_adj = bond.oas_spread * (10 - bond.liquidity_score) / 10
                returns[bond.bond_id] = bond.vintage_yield + duration_adj + credit_adj
    
    # Apply credit and prepayment adjustments
    print("Applying prepayment and credit adjustments...")
    adjusted_returns = apply_prepayment_and_default_adjustments(returns, loans, bonds)
    
    # Enhance floating rate bonds with Nelson-Siegel forward curves
    print("Enhancing floating rate bonds with Nelson-Siegel...")
    enhanced_floating_bonds = enhance_floating_bonds_with_nelson_siegel(bonds, rates_df)
    
    # Export individual cashflows with timestamp
    print("Exporting individual cashflows...")
    export_dir = export_cashflows_during_run(loans, bonds, rates_df, adjusted_returns)
    
    # Create daily returns matrix for portfolio optimization
    print("Creating daily returns matrix...")
    start_date = '2022-01-01'
    end_date = dt.date.today().strftime('%Y-%m-%d')
    dates = pd.date_range(start_date, end_date, freq='D')
    
    # Generate loan returns (always use scientific factor model for loans)
    print("Generating loan returns using scientific factor model...")
    loan_returns_matrix = pd.DataFrame(index=dates)
    
    # Use Treasury factors for loan returns (not OU - loans don't have yield curves)
    treasury_10y_changes = rates_df['DGS10'].pct_change().reindex(dates, method='ffill').fillna(0)
    sofr_changes = rates_df['SOFR'].pct_change().reindex(dates, method='ffill').fillna(0)
    
    for loan in loans:
        loan_id = loan.loan_id
        if loan_id not in adjusted_returns:
            continue
            
        annual_return = adjusted_returns[loan_id]
        daily_return = annual_return / 252
        
        # SCIENTIFIC LOAN VOLATILITY MODEL
        credit_vol_map = {
            'A': 0.08, 'A-': 0.09, 'BBB+': 0.10, 'BBB': 0.12, 'BBB-': 0.14,
            'BB+': 0.16, 'BB': 0.18, 'BB-': 0.20, 'B+': 0.22, 'B': 0.25, 'B-': 0.28
        }
        base_vol = credit_vol_map.get(loan.credit_rating, 0.20)
        ltv_vol = loan.ltv * 0.15
        dscr_vol = max(0, (1.5 - loan.dscr) * 0.08)
        
        sector_vol_map = {
            'Technology': 0.12, 'Healthcare': 0.08, 'Financial Services': 0.15,
            'Energy': 0.25, 'Real Estate': 0.18, 'Consumer Discretionary': 0.14,
            'Consumer Staples': 0.07, 'Industrials': 0.11, 'Materials': 0.16,
            'Transportation': 0.20, 'Utilities': 0.09, 'Telecommunications': 0.10
        }
        sector_vol = sector_vol_map.get(loan.sector, 0.12)
        volatility = 0.4 * base_vol + 0.3 * sector_vol + 0.2 * ltv_vol + 0.1 * dscr_vol
        
        # Loan beta model
        if loan.rate_type == 'floating':
            sofr_beta = 0.1 + loan.default_prob * 0.3
            treasury_beta = 0.05
        else:
            sofr_beta = 0.3 + loan.default_prob * 0.5
            treasury_beta = 0.2 + loan.default_prob * 0.4
        
        systematic_component = sofr_beta * sofr_changes + treasury_beta * treasury_10y_changes
        
        daily_vol = volatility / np.sqrt(252)
        asset_hash = abs(hash(loan_id)) % 1000
        time_component = np.sin(np.arange(len(dates)) * 2 * np.pi / 252 + asset_hash)
        sector_component = np.cos(np.arange(len(dates)) * 2 * np.pi / (252 * 3) + asset_hash * 2)
        
        total_systematic_vol = np.std(systematic_component) if len(systematic_component) > 1 else 0
        idiosyncratic_vol = np.sqrt(max(0, daily_vol**2 - total_systematic_vol**2))
        idiosyncratic_component = (time_component + sector_component) * idiosyncratic_vol * 0.5
        
        daily_returns = daily_return + systematic_component + idiosyncratic_component
        
        # Add mean reversion for loans
        mean_reversion = 0.002
        daily_returns = pd.Series(daily_returns, index=dates)
        for j in range(1, len(daily_returns)):
            daily_returns.iloc[j] = (1 - mean_reversion) * daily_returns.iloc[j] + mean_reversion * daily_return
        
        loan_returns_matrix[loan_id] = daily_returns
    
    # Generate bond returns using selected approach
    if use_ou_modeling:
        print("Generating bond returns using OU mean-reverting process...")
        bond_returns_matrix = generate_ou_based_daily_returns_for_all_bonds(bonds, rates_df, dates)
    else:
        print("Generating bond returns using treasury factor model...")  
        bond_returns_matrix = generate_treasury_factor_daily_returns_for_bonds(bonds, rates_df, dates, adjusted_returns)
    
    # Combine loan and bond returns
    returns_matrix = pd.concat([loan_returns_matrix, bond_returns_matrix], axis=1)
    
    # Advanced OU modeling for sample bonds and loans
    print("\\nRunning advanced OU modeling for sample assets...")
    try:
        # Run OU simulation for first 3 bonds
        sample_bonds = bonds[:3]
        for bond in sample_bonds:
            try:
                bond_ou_results = calculate_bond_monthly_returns_with_rolldown(bond, rates_df, n_months=12)
                print(f"OU simulation complete for {bond.bond_id}")
            except Exception as e:
                print(f"Error in OU simulation for {bond.bond_id}: {e}")
        
        # Run income-based simulation for first 3 loans  
        sample_loans = loans[:3]
        for loan in sample_loans:
            try:
                loan_income_results = calculate_loan_monthly_returns_income_based(loan, rates_df, n_months=12)
                print(f"Income simulation complete for {loan.loan_id}")
            except Exception as e:
                print(f"Error in income simulation for {loan.loan_id}: {e}")
                
    except Exception as e:
        print(f"Error in advanced modeling: {e}")
    
    # Portfolio optimization
    print("\\nOptimizing portfolio...")
    portfolios = simulate_random_portfolios_from_returns(
        returns_matrix, num_portfolios=10000, risk_free_rate=0.035, allow_short=False, random_seed=123
    )
    
    max_sharpe, min_vol = best_portfolios(portfolios)
    
    # Results
    print("\\n" + "="*60)
    print("ENHANCED BOND PORTFOLIO RESULTS")
    print("="*60)
    
    print("\\n=== Max-Sharpe Portfolio (Enhanced) ===")
    print(f"Expected Return: {max_sharpe['Return']:.2%}")
    print(f"Volatility: {max_sharpe['Volatility']:.2%}")
    print(f"Sharpe Ratio: {max_sharpe['Sharpe']:.2f}")
    
    print("\\n=== Min-Vol Portfolio (Enhanced) ===")
    print(f"Expected Return: {min_vol['Return']:.2%}")
    print(f"Volatility: {min_vol['Volatility']:.2%}")
    print(f"Sharpe Ratio: {min_vol['Sharpe']:.2f}")
    
    # Show top holdings in each portfolio
    print("\\n=== Top 10 Holdings - Max Sharpe ===")
    weights = {k: v for k, v in max_sharpe.items() if k not in ['Return', 'Volatility', 'Sharpe']}
    top_holdings = sorted(weights.items(), key=lambda x: x[1], reverse=True)[:10]
    for asset, weight in top_holdings:
        asset_type = "Loan" if asset.startswith('LOAN_') else "Bond"
        print(f"{asset:12s}: {weight:6.2%} ({asset_type})")
    
    # Plot efficient frontier
    plot_efficient_frontier(
        portfolios, max_sharpe, min_vol, risk_free_rate=0.035,
        title="Enhanced Bond Portfolio - Efficient Frontier\\n(50 Loans + 100 Corporate Bonds with Real Market Data)"
    )
    
    # Sample analytics
    print("\\n=== Sample Asset Analytics ===")
    sample_loan = loans[0]
    sample_bond = bonds[0]
    
    print(f"\\nSample Loan: {sample_loan.loan_id}")
    print(f"  Borrower: {sample_loan.borrower}")
    print(f"  Sector: {sample_loan.sector}")
    print(f"  Rate Type: {sample_loan.rate_type}")
    if sample_loan.rate_type == 'floating':
        print(f"  Base Rate: {sample_loan.base_rate}")
        print(f"  Current Spread: {sample_loan.current_spread:.2%}")
    print(f"  Current Return: {adjusted_returns[sample_loan.loan_id]:.2%}")
    print(f"  Credit Rating: {sample_loan.credit_rating}")
    print(f"  Default Probability: {sample_loan.default_prob:.1%}")
    
    print(f"\\nSample Bond: {sample_bond.bond_id}")
    print(f"  Issuer: {sample_bond.issuer}")
    print(f"  Sector: {sample_bond.sector}")
    print(f"  Rate Type: {sample_bond.rate_type}")
    print(f"  Current YTM: {adjusted_returns[sample_bond.bond_id]:.2%}")
    print(f"  Credit Rating: {sample_bond.credit_rating}")
    print(f"  Duration: {sample_bond.duration:.1f}")
    print(f"  Liquidity Score: {sample_bond.liquidity_score:.1f}/10")
    
    return portfolios, max_sharpe, min_vol, loans, bonds, adjusted_returns

if __name__ == "__main__":
    create_enhanced_bond_portfolio()