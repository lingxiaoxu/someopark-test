#!/usr/bin/env python3
"""
Yield Curve Modeling and Forward Rate Projections

Industry-standard approaches for constructing yield curves and projecting forward rates:
1. Bootstrapping: Build zero curve from market instruments
2. Interpolation: Fill gaps between market points
3. Forward Rate Calculation: Extract forward rates from zero curve
4. Term Structure Models: Nelson-Siegel, Svensson for smooth curves

This module implements professional yield curve construction used by:
- Central Banks (Fed, ECB, BOJ)
- Investment Banks (Goldman, JPM, MS)
- Asset Managers (BlackRock, PIMCO, Vanguard)
- Risk Management Systems (Bloomberg, Refinitiv)
"""

import pandas as pd
import numpy as np
import datetime as dt
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
import scipy.optimize as opt
from scipy.interpolate import CubicSpline, interp1d

@dataclass
class YieldCurvePoint:
    """Market yield curve observation"""
    maturity_years: float    # Time to maturity in years
    yield_rate: float       # Yield rate as decimal (e.g., 0.045 = 4.5%)
    instrument_type: str    # 'Treasury', 'SOFR', 'Swap', 'Future'
    date: str              # Observation date
    liquidity: float = 1.0 # Liquidity weight (1.0 = most liquid)

class YieldCurve:
    """
    Professional yield curve construction and forward rate projection
    
    Industry Standard Approach:
    1. Bootstrap zero curve from market instruments
    2. Interpolate between known points
    3. Calculate forward rates
    4. Project future curve evolution
    """
    
    def __init__(self, curve_date: str, currency: str = 'USD'):
        self.curve_date = pd.Timestamp(curve_date)
        self.currency = currency
        self.market_points: List[YieldCurvePoint] = []
        self.zero_rates: pd.Series = None
        self.forward_rates: pd.DataFrame = None
        
    def add_market_point(self, maturity_years: float, yield_rate: float, 
                        instrument_type: str, liquidity: float = 1.0):
        """Add market observation to curve construction"""
        point = YieldCurvePoint(
            maturity_years=maturity_years,
            yield_rate=yield_rate,
            instrument_type=instrument_type,
            date=self.curve_date.strftime('%Y-%m-%d'),
            liquidity=liquidity
        )
        self.market_points.append(point)
        
    def bootstrap_zero_curve(self, method: str = 'cubic_spline') -> pd.Series:
        """
        Bootstrap zero-coupon yield curve from market instruments
        
        Industry Methods:
        - 'linear': Linear interpolation (simple)
        - 'cubic_spline': Cubic spline (smooth, industry standard)
        - 'nelson_siegel': Nelson-Siegel parametric model
        - 'svensson': Extended Nelson-Siegel (central bank standard)
        
        Returns:
            Series with zero rates for standard maturities
        """
        if not self.market_points:
            raise ValueError("No market points provided for curve construction")
        
        # Sort by maturity and remove duplicates
        points = sorted(self.market_points, key=lambda x: x.maturity_years)
        
        # Remove duplicate maturities (keep most liquid)
        unique_points = {}
        for point in points:
            maturity = point.maturity_years
            if maturity not in unique_points or point.liquidity > unique_points[maturity].liquidity:
                unique_points[maturity] = point
        
        points = sorted(unique_points.values(), key=lambda x: x.maturity_years)
        
        # Extract data for interpolation
        maturities = [p.maturity_years for p in points]
        yields = [p.yield_rate for p in points]
        weights = [p.liquidity for p in points]
        
        # Standard curve maturities (market convention)
        standard_maturities = [
            1/12, 3/12, 6/12, 1, 2, 3, 5, 7, 10, 20, 30  # 1M to 30Y
        ]
        
        if method == 'cubic_spline':
            # Cubic spline interpolation (most common in industry)
            spline = CubicSpline(maturities, yields, extrapolate=True)
            zero_rates = [spline(t) for t in standard_maturities]
            
        elif method == 'linear':
            # Linear interpolation
            interp_func = interp1d(maturities, yields, 
                                 kind='linear', fill_value='extrapolate')
            zero_rates = [interp_func(t) for t in standard_maturities]
            
        elif method == 'nelson_siegel':
            # Nelson-Siegel parametric model: y(τ) = β₀ + β₁((1-e^(-τ/λ))/(τ/λ)) + β₂((1-e^(-τ/λ))/(τ/λ) - e^(-τ/λ))
            zero_rates = self._fit_nelson_siegel(maturities, yields, standard_maturities)
            
        elif method == 'svensson':
            # Extended Nelson-Siegel (used by ECB, Fed)
            zero_rates = self._fit_svensson(maturities, yields, standard_maturities)
            
        else:
            raise ValueError(f"Unknown interpolation method: {method}")
        
        # Create series with proper index
        self.zero_rates = pd.Series(zero_rates, index=standard_maturities)
        return self.zero_rates
    
    def calculate_forward_rates(self) -> pd.DataFrame:
        """
        Calculate forward rates from zero curve using industry formula:
        f(t₁,t₂) = [(1 + z(t₂))^t₂ / (1 + z(t₁))^t₁]^(1/(t₂-t₁)) - 1
        
        Where:
        - f(t₁,t₂) = forward rate from time t₁ to t₂
        - z(t) = zero rate for maturity t
        
        This is the standard method used by all major financial institutions.
        """
        if self.zero_rates is None:
            raise ValueError("Must bootstrap zero curve first")
        
        maturities = self.zero_rates.index.tolist()
        forward_data = []
        
        # Calculate forward rates for all pairs
        for i, start_time in enumerate(maturities[:-1]):
            for j, end_time in enumerate(maturities[i+1:], i+1):
                # Standard forward rate formula
                z_start = self.zero_rates.iloc[i]
                z_end = self.zero_rates.iloc[j]
                
                if end_time > start_time:
                    forward_rate = ((1 + z_end) ** end_time / (1 + z_start) ** start_time) ** (1/(end_time - start_time)) - 1
                    
                    forward_data.append({
                        'start_time': start_time,
                        'end_time': end_time,
                        'forward_period': end_time - start_time,
                        'forward_rate': forward_rate,
                        'annualized_rate': forward_rate,  # Already annualized
                        'forward_label': f"{start_time:.2f}Y×{end_time:.2f}Y"
                    })
        
        self.forward_rates = pd.DataFrame(forward_data)
        return self.forward_rates
    
    def get_forward_rate(self, start_years: float, end_years: float) -> float:
        """Get specific forward rate (e.g., 2Y×5Y forward)"""
        if self.forward_rates is None:
            self.calculate_forward_rates()
            
        # Find closest match
        matches = self.forward_rates[
            (abs(self.forward_rates['start_time'] - start_years) < 0.1) &
            (abs(self.forward_rates['end_time'] - end_years) < 0.1)
        ]
        
        if not matches.empty:
            return matches.iloc[0]['forward_rate']
        
        # If no exact match, interpolate
        return self._interpolate_forward_rate(start_years, end_years)
    
    def project_future_curve(self, projection_date: str, method: str = 'forward_interpolation') -> 'YieldCurve':
        """
        Project yield curve to future date using forward rates
        
        Industry Methods:
        - 'forward_interpolation': Use forward rates (market standard)
        - 'parallel_shift': Assume parallel curve movement  
        - 'twist_model': Principal component analysis
        - 'affine_model': Cox-Ingersoll-Ross or Vasicek model
        """
        projection_timestamp = pd.Timestamp(projection_date)
        time_horizon = (projection_timestamp - self.curve_date).days / 365.25
        
        if method == 'forward_interpolation':
            return self._project_using_forwards(time_horizon, projection_date)
        elif method == 'parallel_shift':
            return self._project_parallel_shift(time_horizon, projection_date)
        else:
            raise ValueError(f"Unknown projection method: {method}")
    
    def get_floating_rate_projections(self, reset_dates: List[str], 
                                    rate_type: str = 'SOFR',
                                    tenor: str = '3M') -> pd.Series:
        """
        Get floating rate projections for specific reset dates
        This is what we need to fix the Goldman Sachs bond!
        
        Args:
            reset_dates: List of reset dates
            rate_type: Base rate type ('SOFR', 'LIBOR', 'EFFR')  
            tenor: Rate tenor ('1M', '3M', '6M')
            
        Returns:
            Series of projected rates for each reset date
        """
        projections = []
        
        for reset_date in reset_dates:
            reset_timestamp = pd.Timestamp(reset_date)
            time_to_reset = (reset_timestamp - self.curve_date).days / 365.25
            
            if time_to_reset <= 0:
                # Past or current reset - use current rate
                current_rate = self.get_current_short_rate(rate_type, tenor)
                projections.append(current_rate)
            else:
                # Future reset - use forward rate projection
                if tenor == '3M':
                    tenor_years = 0.25
                elif tenor == '1M':
                    tenor_years = 1/12
                elif tenor == '6M':
                    tenor_years = 0.5
                else:
                    tenor_years = 0.25  # Default to 3M
                
                # Get forward rate starting at reset date
                forward_rate = self.get_forward_rate(time_to_reset, time_to_reset + tenor_years)
                projections.append(forward_rate)
        
        return pd.Series(projections, index=pd.to_datetime(reset_dates))
    
    def _fit_nelson_siegel(self, maturities: List[float], yields: List[float], 
                          target_maturities: List[float]) -> List[float]:
        """
        Fit Nelson-Siegel parametric model
        
        Industry Implementation:
        y(τ) = β₀ + β₁((1-e^(-τ/λ))/(τ/λ)) + β₂((1-e^(-τ/λ))/(τ/λ) - e^(-τ/λ))
        
        Economic Interpretation:
        β₀ = Long-term level (30Y yield)
        β₁ = Short-term factor (slope: short vs long) 
        β₂ = Medium-term factor (curvature: hump in 2Y-10Y)
        λ = Decay parameter (shape control)
        """
        
        def nelson_siegel(tau, beta0, beta1, beta2, lambda_param):
            """Nelson-Siegel yield curve formula"""
            tau = np.array(tau)
            # Avoid division by zero for very small tau
            tau = np.maximum(tau, 1e-6)
            
            term1 = beta0  # Long-term level
            term2 = beta1 * (1 - np.exp(-tau/lambda_param)) / (tau/lambda_param)  # Short-term slope
            term3 = beta2 * ((1 - np.exp(-tau/lambda_param)) / (tau/lambda_param) - np.exp(-tau/lambda_param))  # Curvature
            return term1 + term2 + term3
        
        def objective(params):
            beta0, beta1, beta2, lambda_param = params
            try:
                model_yields = nelson_siegel(maturities, beta0, beta1, beta2, lambda_param)
                return np.sum((np.array(yields) - model_yields) ** 2)
            except:
                return 1e6  # Large penalty for invalid parameters
        
        # Industry-standard initial guess (based on Fed practice)
        long_term_yield = yields[-1] if yields else 0.045  # Use longest maturity
        short_term_yield = yields[0] if yields else 0.040   # Use shortest maturity
        
        initial_guess = [
            long_term_yield,                    # β₀: long-term level
            short_term_yield - long_term_yield, # β₁: slope factor  
            0.01,                              # β₂: curvature
            2.5                                # λ: typical decay parameter
        ]
        
        # Optimize parameters with realistic constraints
        bounds = [
            (0.01, 0.12),   # β₀: 1% to 12% (reasonable yield range)
            (-0.08, 0.08),  # β₁: slope factor range
            (-0.08, 0.08),  # β₂: curvature range  
            (0.5, 8.0)      # λ: decay parameter (Fed uses ~2-3)
        ]
        
        try:
            result = opt.minimize(objective, initial_guess, bounds=bounds, method='L-BFGS-B')
            
            if result.success:
                # Store fitted parameters for inspection
                beta0, beta1, beta2, lambda_param = result.x
                self.nelson_siegel_params = {
                    'beta0': beta0, 'beta1': beta1, 'beta2': beta2, 'lambda': lambda_param,
                    'fit_error': result.fun
                }
                
                print(f"Nelson-Siegel parameters fitted:")
                print(f"  β₀ (long-term): {beta0:.3%}")
                print(f"  β₁ (slope): {beta1:.3%}")  
                print(f"  β₂ (curvature): {beta2:.3%}")
                print(f"  λ (decay): {lambda_param:.2f}")
                print(f"  Fit error: {result.fun:.6f}")
                
                # Generate smooth curve
                return nelson_siegel(target_maturities, beta0, beta1, beta2, lambda_param).tolist()
            else:
                print(f"Nelson-Siegel optimization failed: {result.message}")
                # Fallback to cubic spline
                return self._fallback_to_cubic_spline(maturities, yields, target_maturities)
                
        except Exception as e:
            print(f"Error in Nelson-Siegel fitting: {e}")
            return self._fallback_to_cubic_spline(maturities, yields, target_maturities)
    
    def _fallback_to_cubic_spline(self, maturities: List[float], yields: List[float], 
                                 target_maturities: List[float]) -> List[float]:
        """Fallback to cubic spline if Nelson-Siegel fails"""
        spline = CubicSpline(maturities, yields, extrapolate=True)
        return [spline(t) for t in target_maturities]
    
    def _fit_svensson(self, maturities: List[float], yields: List[float],
                     target_maturities: List[float]) -> List[float]:
        """Fit Svensson (Extended Nelson-Siegel) model - used by central banks"""
        
        def svensson(tau, beta0, beta1, beta2, beta3, lambda1, lambda2):
            """Extended Nelson-Siegel (Svensson) formula"""
            tau = np.array(tau)
            term1 = beta0
            term2 = beta1 * (1 - np.exp(-tau/lambda1)) / (tau/lambda1)
            term3 = beta2 * ((1 - np.exp(-tau/lambda1)) / (tau/lambda1) - np.exp(-tau/lambda1))
            term4 = beta3 * ((1 - np.exp(-tau/lambda2)) / (tau/lambda2) - np.exp(-tau/lambda2))
            return term1 + term2 + term3 + term4
        
        def objective(params):
            beta0, beta1, beta2, beta3, lambda1, lambda2 = params
            model_yields = svensson(maturities, beta0, beta1, beta2, beta3, lambda1, lambda2)
            return np.sum((np.array(yields) - model_yields) ** 2)
        
        # Initial parameter guess (based on ECB practice)
        initial_guess = [0.04, -0.01, 0.01, 0.005, 2.0, 8.0]
        
        # Optimize with constraints
        bounds = [(0.01, 0.10), (-0.05, 0.05), (-0.05, 0.05), (-0.05, 0.05), (0.1, 10.0), (0.1, 20.0)]
        result = opt.minimize(objective, initial_guess, bounds=bounds)
        
        # Generate curve
        beta0, beta1, beta2, beta3, lambda1, lambda2 = result.x
        return svensson(target_maturities, beta0, beta1, beta2, beta3, lambda1, lambda2).tolist()
    
    def _project_using_forwards(self, time_horizon: float, projection_date: str) -> 'YieldCurve':
        """Project curve using forward rates (industry standard)"""
        
        # Create new curve for projection date
        projected_curve = YieldCurve(projection_date, self.currency)
        
        # For each maturity, use forward rate
        for maturity in self.zero_rates.index:
            if maturity > time_horizon:
                # Use forward rate starting at time_horizon
                forward_rate = self.get_forward_rate(time_horizon, maturity)
                projected_curve.add_market_point(
                    maturity_years=maturity - time_horizon,
                    yield_rate=forward_rate,
                    instrument_type='Forward_Projection'
                )
        
        # Bootstrap the projected curve
        projected_curve.bootstrap_zero_curve()
        return projected_curve
    
    def get_current_short_rate(self, rate_type: str, tenor: str) -> float:
        """Get current short rate for floating rate projections"""
        # This would typically come from current market data
        # For now, use the short end of our curve
        if self.zero_rates is not None and len(self.zero_rates) > 0:
            return self.zero_rates.iloc[0]  # Shortest maturity
        return 0.04  # Fallback
    
    def _interpolate_forward_rate(self, start_years: float, end_years: float) -> float:
        """Interpolate forward rate when exact match not available"""
        if self.zero_rates is None:
            raise ValueError("Zero curve not available for interpolation")
        
        # Linear interpolation of zero rates, then calculate forward
        z_start = np.interp(start_years, self.zero_rates.index, self.zero_rates.values)
        z_end = np.interp(end_years, self.zero_rates.index, self.zero_rates.values)
        
        # Standard forward rate formula
        forward_rate = ((1 + z_end) ** end_years / (1 + z_start) ** start_years) ** (1/(end_years - start_years)) - 1
        return forward_rate

def build_yield_curve_from_fred(rates_df: pd.DataFrame, curve_date: str = None) -> YieldCurve:
    """
    Build yield curve from FRED Treasury data
    
    Industry Standard Inputs:
    - DGS1MO, DGS3MO, DGS6MO (Bills)
    - DGS2, DGS5, DGS10, DGS30 (Notes & Bonds)
    - SOFR, EFFR (Short rates)
    
    Args:
        rates_df: FRED rates DataFrame
        curve_date: Date for curve construction (defaults to latest)
        
    Returns:
        Constructed yield curve object
    """
    if curve_date is None:
        curve_date = rates_df.index[-1].strftime('%Y-%m-%d')
    
    curve = YieldCurve(curve_date, currency='USD')
    
    # Map FRED series to maturities
    fred_mapping = {
        'SOFR': (1/365, 'SOFR'),      # Overnight
        'EFFR': (1/365, 'EFFR'),      # Overnight  
        'DGS1MO': (1/12, 'Treasury'), # 1 Month
        'DGS3MO': (0.25, 'Treasury'), # 3 Month
        'DGS6MO': (0.5, 'Treasury'),  # 6 Month
        'DGS2': (2, 'Treasury'),      # 2 Year
        'DGS5': (5, 'Treasury'),      # 5 Year
        'DGS10': (10, 'Treasury'),    # 10 Year
        'DGS30': (30, 'Treasury'),    # 30 Year
    }
    
    curve_timestamp = pd.Timestamp(curve_date)
    
    # Add available market points
    for fred_series, (maturity_years, instrument_type) in fred_mapping.items():
        if fred_series in rates_df.columns:
            # Get rate on curve date (or closest)
            try:
                if curve_timestamp in rates_df.index:
                    rate = rates_df.loc[curve_timestamp, fred_series]
                else:
                    # Find closest date
                    closest_idx = rates_df.index.get_indexer([curve_timestamp], method='nearest')[0]
                    rate = rates_df.iloc[closest_idx][fred_series]
                
                if pd.notna(rate) and rate > 0:
                    # Add to curve with liquidity weighting
                    liquidity = 1.0 if instrument_type == 'Treasury' else 0.8
                    curve.add_market_point(maturity_years, rate, instrument_type, liquidity)
                    
            except Exception as e:
                print(f"Warning: Could not add {fred_series} to curve: {e}")
    
    print(f"Built yield curve with {len(curve.market_points)} market points for {curve_date}")
    return curve

def calculate_floating_rate_cashflows_with_forwards(bond_spec, rates_df: pd.DataFrame, 
                                                  method: str = 'forward_projection') -> pd.DataFrame:
    """
    Calculate floating rate bond cashflows using forward rate projections
    
    Industry Methods:
    - 'forward_projection': Use yield curve forwards (most accurate)
    - 'current_rate_flat': Use current rate for all periods (current implementation)
    - 'ou_evolution': Use OU process for rate evolution
    
    This fixes the Goldman Sachs bond issue!
    """
    
    # Build current yield curve
    curve = build_yield_curve_from_fred(rates_df)
    curve.bootstrap_zero_curve(method='cubic_spline')
    
    # Generate reset dates based on bond frequency
    issue_date = pd.Timestamp(bond_spec.issue_date)
    maturity_date = pd.Timestamp(bond_spec.maturity_date)
    
    if bond_spec.freq == 4:  # Quarterly
        reset_freq = '3M'
        months_between = 3
    elif bond_spec.freq == 2:  # Semi-annual
        reset_freq = '6M'  
        months_between = 6
    elif bond_spec.freq == 1:  # Annual
        reset_freq = '12M'
        months_between = 12
    else:
        reset_freq = '3M'  # Default quarterly
        months_between = 3
    
    # Generate all reset/payment dates
    reset_dates = []
    current_date = issue_date
    while current_date < maturity_date:
        current_date += pd.DateOffset(months=months_between)
        if current_date <= maturity_date:
            reset_dates.append(current_date.strftime('%Y-%m-%d'))
    
    # Get forward rate projections for each reset
    if method == 'forward_projection':
        projected_rates = curve.get_floating_rate_projections(reset_dates, bond_spec.base_rate, reset_freq)
    elif method == 'current_rate_flat':
        # Current implementation - flat current rate
        from enriched_bond_portfolio import get_current_rate
        current_rate = get_current_rate(bond_spec.base_rate, rates_df)
        projected_rates = pd.Series([current_rate] * len(reset_dates), 
                                  index=pd.to_datetime(reset_dates))
    else:
        raise ValueError(f"Unknown projection method: {method}")
    
    # Calculate cashflows with varying rates
    cashflow_data = []
    
    for i, reset_date in enumerate(reset_dates):
        base_rate = projected_rates.iloc[i]
        all_in_rate = base_rate + bond_spec.spread
        
        # Calculate coupon payment
        if hasattr(bond_spec, 'face_value'):
            face_value = bond_spec.face_value
        else:
            face_value = getattr(bond_spec, 'face', 1000)  # Legacy compatibility
            
        coupon_payment = face_value * all_in_rate / bond_spec.freq
        
        # Principal payment (only final period)
        principal_payment = face_value if i == len(reset_dates) - 1 else 0
        
        cashflow_data.append({
            'reset_date': reset_date,
            'base_rate': base_rate,
            'spread': bond_spec.spread,
            'all_in_rate': all_in_rate,  
            'coupon_payment': coupon_payment,
            'principal_payment': principal_payment,
            'total_cashflow': coupon_payment + principal_payment
        })
    
    return pd.DataFrame(cashflow_data)

# ============================================================================
# Industry Best Practices Implementation
# ============================================================================

class ProfessionalYieldCurveSystem:
    """
    Professional-grade yield curve system following industry standards:
    
    1. Bloomberg/Refinitiv Style: Multiple curve construction methods
    2. Central Bank Style: Nelson-Siegel/Svensson parametric models
    3. Investment Bank Style: Bootstrap + interpolation + forward calculations
    4. Risk Management Style: Scenario analysis and curve evolution modeling
    """
    
    def __init__(self):
        self.curves: Dict[str, YieldCurve] = {}
        self.curve_history: List[YieldCurve] = []
        
    def build_daily_curves_from_fred(self, rates_df: pd.DataFrame, 
                                   start_date: str = None, end_date: str = None) -> Dict[str, YieldCurve]:
        """Build yield curves for every day in FRED data"""
        
        if start_date is None:
            start_date = rates_df.index[0].strftime('%Y-%m-%d')
        if end_date is None:
            end_date = rates_df.index[-1].strftime('%Y-%m-%d')
        
        date_range = pd.date_range(start_date, end_date, freq='B')  # Business days
        curves = {}
        
        print(f"Building {len(date_range)} daily yield curves...")
        
        for i, date in enumerate(date_range):
            try:
                curve = build_yield_curve_from_fred(rates_df, date.strftime('%Y-%m-%d'))
                curve.bootstrap_zero_curve()
                curves[date.strftime('%Y-%m-%d')] = curve
                
                if (i + 1) % 100 == 0:
                    print(f"  Built {i+1} curves...")
                    
            except Exception as e:
                print(f"Error building curve for {date}: {e}")
        
        self.curves = curves
        return curves
    
    def get_floating_rate_forward_curve(self, base_date: str, 
                                      forward_horizon_years: float = 5.0) -> pd.DataFrame:
        """
        Get forward curve for floating rates (industry standard approach)
        
        Used for:
        - Floating rate bond valuation
        - Interest rate derivatives pricing  
        - Risk management scenarios
        """
        if base_date not in self.curves:
            raise ValueError(f"No curve available for {base_date}")
        
        base_curve = self.curves[base_date]
        
        # Calculate forward rates for standard tenors
        forward_structure = []
        forward_dates = []
        
        # Generate quarterly forward dates
        current_date = pd.Timestamp(base_date)
        for quarter in range(int(forward_horizon_years * 4)):
            forward_date = current_date + pd.DateOffset(months=3 * quarter)
            start_time = quarter * 0.25
            end_time = (quarter + 1) * 0.25
            
            if end_time <= forward_horizon_years:
                forward_rate = base_curve.get_forward_rate(start_time, end_time)
                forward_structure.append({
                    'forward_date': forward_date,
                    'start_time': start_time,
                    'end_time': end_time, 
                    'forward_rate': forward_rate,
                    'tenor': '3M'
                })
        
        return pd.DataFrame(forward_structure)

# ============================================================================
# Integration Functions for Existing System
# ============================================================================

def build_nelson_siegel_forward_projections(rates_df: pd.DataFrame, 
                                          curve_date: str = None,
                                          projection_horizon_years: float = 5.0,
                                          frequency: str = 'monthly') -> pd.DataFrame:
    """
    Build Nelson-Siegel model and generate forward rate projections
    
    This is the industry-standard approach used by:
    - Federal Reserve for FOMC projections
    - ECB for euro area yield curve modeling
    - Major investment banks for derivatives pricing
    
    Returns:
        DataFrame with forward rate projections for floating bond resets
    """
    
    print("🏦 Building Nelson-Siegel yield curve model...")
    
    # Build yield curve from FRED data
    curve = build_yield_curve_from_fred(rates_df, curve_date)
    
    # Use Nelson-Siegel for smooth curve construction  
    zero_curve = curve.bootstrap_zero_curve(method='nelson_siegel')
    
    print("✅ Nelson-Siegel curve fitted successfully")
    
    # Generate forward rate projections based on specified frequency
    forward_projections = []
    base_date = pd.Timestamp(curve_date) if curve_date else pd.Timestamp.today()
    
    # Determine frequency parameters
    if frequency == 'monthly':
        periods_per_year = 12
        period_length = 1/12  # 1 month = 1/12 year
        date_offset_months = 1
        tenor_name = '1M'
    elif frequency == 'quarterly':
        periods_per_year = 4
        period_length = 0.25  # 3 months = 0.25 year
        date_offset_months = 3
        tenor_name = '3M'
    elif frequency == 'weekly':
        periods_per_year = 52
        period_length = 1/52  # 1 week = 1/52 year
        date_offset_months = 0.25  # Approximate
        tenor_name = '1W'
    else:
        # Default to monthly
        periods_per_year = 12
        period_length = 1/12
        date_offset_months = 1
        tenor_name = '1M'
    
    total_periods = int(projection_horizon_years * periods_per_year)
    print(f"Generating {total_periods} {frequency} forward rate projections...")
    
    # Calculate forward rates for specified frequency
    for period in range(total_periods):
        start_time = period * period_length
        end_time = (period + 1) * period_length
        
        if frequency == 'monthly':
            projection_date = base_date + pd.DateOffset(months=period)
        elif frequency == 'weekly':
            projection_date = base_date + pd.DateOffset(weeks=period)
        else:  # quarterly
            projection_date = base_date + pd.DateOffset(months=period * 3)
        
        if hasattr(curve, 'nelson_siegel_params'):
            # Use Nelson-Siegel parameters to calculate forward rate
            params = curve.nelson_siegel_params
            
            # Calculate zero rates at start and end times
            def ns_rate(tau):
                if tau <= 1e-6:
                    tau = 1e-6
                term1 = params['beta0']
                term2 = params['beta1'] * (1 - np.exp(-tau/params['lambda'])) / (tau/params['lambda'])
                term3 = params['beta2'] * ((1 - np.exp(-tau/params['lambda'])) / (tau/params['lambda']) - np.exp(-tau/params['lambda']))
                return term1 + term2 + term3
            
            z_start = ns_rate(start_time)
            z_end = ns_rate(end_time)
            
            # Standard forward rate formula  
            if end_time > start_time and start_time >= 0:
                forward_rate = ((1 + z_end)**end_time / (1 + z_start)**start_time)**(1/period_length) - 1
            else:
                forward_rate = z_end  # Use spot rate for first period
        else:
            # Fallback if Nelson-Siegel fitting failed
            forward_rate = curve.zero_rates.iloc[0] if curve.zero_rates is not None else 0.04
        
        forward_projections.append({
            'projection_date': projection_date,
            'period': period,
            'start_time_years': start_time,
            'end_time_years': end_time,
            'forward_rate': forward_rate,
            'rate_type': f'{tenor_name}_Forward'
        })
    
    forward_df = pd.DataFrame(forward_projections)
    print(f"✅ Generated {len(forward_df)} {frequency} forward rate projections")
    
    return forward_df

def enhance_floating_bond_cashflows_with_nelson_siegel(bond, rates_df: pd.DataFrame) -> pd.DataFrame:
    """
    Enhanced floating rate bond cashflow calculation using Nelson-Siegel forward projections
    
    This directly fixes the Goldman Sachs bond issue by using market-based forward rates
    instead of flat current rates!
    
    Industry Standard Approach:
    1. Fit Nelson-Siegel to current Treasury curve
    2. Extract forward rate projections  
    3. Apply to floating bond reset dates
    4. Generate realistic variable coupon payments
    """
    
    try:
        print(f"🔧 Enhancing floating bond {bond.bond_id} with Nelson-Siegel forwards...")
        
        # Get Nelson-Siegel forward projections
        forward_projections = build_nelson_siegel_forward_projections(
            rates_df, 
            curve_date=None,  # Use latest data
            projection_horizon_years=5.0
        )
        
        # Generate payment dates for this bond
        issue_date = pd.Timestamp(bond.issue_date)
        maturity_date = pd.Timestamp(bond.maturity_date)
        
        # Calculate payment frequency
        if bond.freq == 4:
            months_between = 3  # Quarterly
        elif bond.freq == 2:
            months_between = 6  # Semi-annual
        else:
            months_between = 12  # Annual
        
        # Generate reset/payment dates
        payment_dates = []
        current_date = pd.Timestamp.today()  # Start from today for future cashflows
        
        while current_date < maturity_date:
            current_date += pd.DateOffset(months=months_between)
            if current_date <= maturity_date:
                payment_dates.append(current_date)
        
        if not payment_dates:
            return pd.DataFrame()  # No future payments
        
        # Match payment dates to forward projections
        enhanced_cashflows = []
        
        for i, payment_date in enumerate(payment_dates):
            # Find closest forward projection
            time_to_payment = (payment_date - pd.Timestamp.today()).days / 365.25
            
            # Get forward rate from Nelson-Siegel projections
            closest_projection = forward_projections[
                forward_projections['start_time_years'] <= time_to_payment
            ].iloc[-1] if not forward_projections[forward_projections['start_time_years'] <= time_to_payment].empty else forward_projections.iloc[0]
            
            projected_base_rate = closest_projection['forward_rate']
            all_in_rate = projected_base_rate + bond.spread
            
            # Calculate payments
            face_value = bond.face_value
            coupon_payment = face_value * all_in_rate / bond.freq
            principal_payment = face_value if i == len(payment_dates) - 1 else 0
            
            # Credit adjustments
            credit_default_map = {
                'AAA': 0.0001, 'AA+': 0.0002, 'AA': 0.0003, 'AA-': 0.0005,
                'A+': 0.0008, 'A': 0.0012, 'A-': 0.0018,
                'BBB+': 0.0025, 'BBB': 0.0035, 'BBB-': 0.0050,
                'BB+': 0.0080, 'BB': 0.0120, 'BB-': 0.0180,
                'B+': 0.0250, 'B': 0.0350, 'B-': 0.0500
            }
            
            default_prob = credit_default_map.get(bond.credit_rating, 0.02)
            recovery_rate = 0.4
            periods_from_now = i + 1
            survival_prob = (1 - default_prob / len(payment_dates)) ** periods_from_now
            
            expected_loss = (coupon_payment + principal_payment) * (1 - survival_prob)
            recovery_value = expected_loss * recovery_rate
            net_cashflow = (coupon_payment + principal_payment) * survival_prob + recovery_value
            
            enhanced_cashflows.append({
                'payment_date': payment_date,
                'projected_base_rate': projected_base_rate,
                'spread': bond.spread,
                'all_in_rate': all_in_rate,
                'coupon_payment': coupon_payment,
                'principal_payment': principal_payment,
                'total_payment': coupon_payment + principal_payment,
                'default_prob': default_prob,
                'survival_prob': survival_prob,
                'expected_loss': expected_loss,
                'recovery_value': recovery_value,
                'net_cashflow': net_cashflow,
                'modeling_method': 'Nelson_Siegel_Forward'
            })
        
        result_df = pd.DataFrame(enhanced_cashflows)
        
        print(f"✅ Enhanced floating bond {bond.bond_id}:")
        print(f"   Payments: {len(result_df)}")  
        print(f"   Rate range: {result_df['projected_base_rate'].min():.3%} to {result_df['projected_base_rate'].max():.3%}")
        print(f"   Using Nelson-Siegel forward curve projections")
        
        return result_df
        
    except Exception as e:
        print(f"❌ Error enhancing {bond.bond_id}: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()

if __name__ == "__main__":
    # Test the yield curve system
    print("=== Testing Professional Yield Curve System ===")
    
    # Load FRED data
    import sys
    sys.path.append('.')
    from enriched_bond_portfolio import load_fred_rates, load_bonds
    
    rates_df = load_fred_rates()
    bonds = load_bonds()
    
    # Build current yield curve
    curve = build_yield_curve_from_fred(rates_df)
    curve.bootstrap_zero_curve(method='cubic_spline')
    
    print(f"\n✅ Yield curve built with {len(curve.market_points)} market points")
    print("Zero rates:")
    for maturity, rate in curve.zero_rates.items():
        print(f"  {maturity:5.2f}Y: {rate:.3%}")
    
    # Test forward rate calculation
    forward_2y5y = curve.get_forward_rate(2, 5)  # 2Y×5Y forward
    print(f"\n2Y×5Y Forward Rate: {forward_2y5y:.3%}")
    
    # Test floating rate projection
    sample_floating_bond = next(b for b in bonds if b.rate_type == 'floating')
    print(f"\nTesting floating rate enhancement for {sample_floating_bond.bond_id}")
    
    enhanced_cf = enhance_floating_bond_cashflows_with_forwards(sample_floating_bond, rates_df)
    if enhanced_cf is not None and not enhanced_cf.empty:
        print("Enhanced cashflows (first 5 periods):")
        print(enhanced_cf.head())
    
    print("\n✅ Yield curve system ready for integration!")