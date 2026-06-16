#!/usr/bin/env python3
"""
Credit Risk Integration Module for Cashflow Analytics
Provides credit-adjusted attributes for loan cashflow generation
"""

import pandas as pd
import numpy as np
from typing import Dict, Tuple, Optional
from dataclasses import dataclass


@dataclass
class CreditAdjustedAttributes:
    """Credit-adjusted loan attributes for cashflow generation"""
    current_spread: float      # Original or risk-adjusted spread
    default_prob: float       # Original or credit-score adjusted default probability
    recovery_rate: float      # Credit-score based recovery rate
    stress_multiplier: float  # Credit-score based stress testing multiplier
    expected_loss: float      # Expected loss incorporating PD, LGD, and exposure


class CreditRiskCashflowIntegrator:
    """
    Integrates credit risk modeling and risk-adjusted pricing into cashflow generation.
    
    Provides a toggle between:
    - Static mode (False): Uses original loan attributes 
    - Advanced mode (True): Uses credit-adjusted attributes based on fundamentals
    """
    
    def __init__(self, advanced_mode: bool = True):
        """
        Initialize the credit risk integrator
        
        Args:
            advanced_mode: If True, applies credit adjustments. If False, uses static values.
        """
        self.advanced_mode = advanced_mode
        
        # Recovery rate mapping based on credit scores
        self.recovery_rate_mapping = {
            'range_0_40': 0.20,    # Very poor credit
            'range_40_60': 0.35,   # Poor credit  
            'range_60_80': 0.50,   # Average credit
            'range_80_100': 0.65,  # Good credit
            'range_100_plus': 0.75 # Excellent credit
        }
        
        # Stress multiplier mapping based on credit scores
        self.stress_multiplier_mapping = {
            'range_0_40': 2.0,     # 200% stress (very vulnerable)
            'range_40_60': 1.5,    # 150% stress (vulnerable) 
            'range_60_80': 1.2,    # 120% stress (moderate)
            'range_80_100': 1.0,   # 100% stress (stable)
            'range_100_plus': 0.8  # 80% stress (resilient)
        }

    def get_credit_adjusted_attributes(self, loan_data: Dict) -> CreditAdjustedAttributes:
        """
        Get credit-adjusted attributes for cashflow generation
        
        Args:
            loan_data: Dictionary containing loan metadata including:
                - current_spread: float
                - default_prob: float  
                - credit_score: float
                - risk_adjusted_spread: float
                - principal: float
                
        Returns:
            CreditAdjustedAttributes object with adjusted or original values
        """
        
        if not self.advanced_mode:
            # Static mode: return original attributes
            return CreditAdjustedAttributes(
                current_spread=loan_data.get('current_spread', 0.0),
                default_prob=loan_data.get('default_prob', 0.03),
                recovery_rate=0.40,  # Static 40% recovery assumption
                stress_multiplier=1.0,  # No additional stress
                expected_loss=loan_data.get('principal', 0) * loan_data.get('default_prob', 0.03) * 0.60  # Static LGD=60%
            )
        
        # Advanced mode: apply credit risk adjustments
        credit_score = loan_data.get('credit_score', 50.0)
        risk_adjusted_spread = loan_data.get('risk_adjusted_spread', loan_data.get('current_spread', 0.0))
        original_default_prob = loan_data.get('default_prob', 0.03)
        principal = loan_data.get('principal', 0.0)
        
        # 1. Use risk-adjusted spread if instrument has spread
        adjusted_spread = self._get_adjusted_spread(loan_data, risk_adjusted_spread)
        
        # 2. Adjust default probability based on credit score
        adjusted_default_prob = self._get_credit_adjusted_default_prob(credit_score, original_default_prob)
        
        # 3. Calculate recovery rate based on credit score
        recovery_rate = self._get_recovery_rate_from_credit_score(credit_score)
        
        # 4. Calculate stress multiplier based on credit score
        stress_multiplier = self._get_stress_multiplier_from_credit_score(credit_score)
        
        # 5. Calculate expected loss: EL = PD * LGD * EAD
        lgd = 1 - recovery_rate  # Loss Given Default
        expected_loss = adjusted_default_prob * lgd * principal
        
        return CreditAdjustedAttributes(
            current_spread=adjusted_spread,
            default_prob=adjusted_default_prob,
            recovery_rate=recovery_rate,
            stress_multiplier=stress_multiplier,
            expected_loss=expected_loss
        )

    def _get_adjusted_spread(self, loan_data: Dict, risk_adjusted_spread: float) -> float:
        """Apply risk-adjusted spread only for instruments that have spreads"""
        instrument = loan_data.get('instrument', '').lower()
        rate_type = loan_data.get('rate_type', 'fixed')
        
        # Only apply risk-adjusted spread to floating rate instruments or those with explicit spreads
        if rate_type == 'floating' or any(term in instrument for term in ['term loan', 'facility', 'floating']):
            return risk_adjusted_spread
        else:
            # For fixed-rate notes, keep original spread (usually 0)
            return loan_data.get('current_spread', 0.0)

    def _get_credit_adjusted_default_prob(self, credit_score: float, original_default_prob: float) -> float:
        """Adjust default probability based on credit score"""
        
        # Credit score adjustment factor
        if credit_score >= 100:
            adjustment_factor = 0.5    # 50% of original PD
        elif credit_score >= 80:
            adjustment_factor = 0.7    # 70% of original PD
        elif credit_score >= 60:
            adjustment_factor = 1.0    # Original PD
        elif credit_score >= 40:
            adjustment_factor = 1.5    # 150% of original PD
        else:
            adjustment_factor = 2.0    # 200% of original PD
        
        adjusted_pd = original_default_prob * adjustment_factor
        
        # Cap at reasonable bounds
        return min(max(adjusted_pd, 0.005), 0.15)  # Between 0.5% and 15%

    def _get_recovery_rate_from_credit_score(self, credit_score: float) -> float:
        """Map credit score to recovery rate"""
        
        if credit_score >= 100:
            return self.recovery_rate_mapping['range_100_plus']
        elif credit_score >= 80:
            return self.recovery_rate_mapping['range_80_100']
        elif credit_score >= 60:
            return self.recovery_rate_mapping['range_60_80']
        elif credit_score >= 40:
            return self.recovery_rate_mapping['range_40_60']
        else:
            return self.recovery_rate_mapping['range_0_40']

    def _get_stress_multiplier_from_credit_score(self, credit_score: float) -> float:
        """Map credit score to stress testing multiplier"""
        
        if credit_score >= 100:
            return self.stress_multiplier_mapping['range_100_plus']
        elif credit_score >= 80:
            return self.stress_multiplier_mapping['range_80_100'] 
        elif credit_score >= 60:
            return self.stress_multiplier_mapping['range_60_80']
        elif credit_score >= 40:
            return self.stress_multiplier_mapping['range_40_60']
        else:
            return self.stress_multiplier_mapping['range_0_40']

    def apply_default_haircut_to_cashflows(self, cashflows: pd.DataFrame, 
                                         adjusted_attrs: CreditAdjustedAttributes) -> pd.DataFrame:
        """
        Apply default probability haircut to expected cashflows
        
        Args:
            cashflows: DataFrame with payment schedule
            adjusted_attrs: Credit-adjusted attributes
            
        Returns:
            DataFrame with expected payments adjusted for default probability
        """
        
        if not self.advanced_mode:
            # Static mode: no adjustment
            return cashflows
            
        # Create copy to avoid modifying original
        adjusted_cashflows = cashflows.copy()
        
        # Calculate survival probability 
        survival_rate = 1 - adjusted_attrs.default_prob
        
        # Apply to payment columns if they exist
        payment_columns = ['payment', 'interest_payment', 'principal_payment']
        for col in payment_columns:
            if col in adjusted_cashflows.columns:
                adjusted_cashflows[f'expected_{col}'] = adjusted_cashflows[col] * survival_rate
        
        return adjusted_cashflows

    def get_stress_testing_parameters(self, loan_data: Dict) -> Dict[str, float]:
        """
        Get stress testing parameters based on credit fundamentals
        
        Args:
            loan_data: Loan metadata dictionary
            
        Returns:
            Dictionary with stress testing parameters
        """
        
        if not self.advanced_mode:
            # Static stress parameters
            return {
                'spread_shock': 0.01,      # +100bps
                'default_prob_multiplier': 1.5,  # 150% of base PD
                'recovery_rate_shock': -0.15     # -15% recovery rate
            }
        
        adjusted_attrs = self.get_credit_adjusted_attributes(loan_data)
        credit_score = loan_data.get('credit_score', 50.0)
        
        # Credit-based stress parameters
        base_spread_shock = 0.01  # 100bps base
        spread_shock = base_spread_shock * adjusted_attrs.stress_multiplier
        
        # Higher stress for lower credit scores
        default_prob_multiplier = adjusted_attrs.stress_multiplier
        recovery_rate_shock = -0.10 if credit_score >= 80 else -0.20
        
        return {
            'spread_shock': spread_shock,
            'default_prob_multiplier': default_prob_multiplier,
            'recovery_rate_shock': recovery_rate_shock,
            'credit_adjusted_recovery': adjusted_attrs.recovery_rate
        }

    def generate_credit_summary_report(self, loan_portfolio: pd.DataFrame) -> Dict:
        """
        Generate summary report comparing static vs credit-adjusted metrics
        
        Args:
            loan_portfolio: DataFrame with loan portfolio data
            
        Returns:
            Dictionary with summary statistics
        """
        
        static_integrator = CreditRiskCashflowIntegrator(advanced_mode=False)
        
        static_metrics = []
        advanced_metrics = []
        
        for idx, loan in loan_portfolio.iterrows():
            loan_dict = loan.to_dict()
            
            # Get both sets of attributes
            static_attrs = static_integrator.get_credit_adjusted_attributes(loan_dict)
            advanced_attrs = self.get_credit_adjusted_attributes(loan_dict)
            
            static_metrics.append({
                'loan_id': loan_dict.get('loan_id', f'LOAN_{idx}'),
                'default_prob': static_attrs.default_prob,
                'recovery_rate': static_attrs.recovery_rate,
                'expected_loss': static_attrs.expected_loss,
                'current_spread': static_attrs.current_spread
            })
            
            advanced_metrics.append({
                'loan_id': loan_dict.get('loan_id', f'LOAN_{idx}'),
                'default_prob': advanced_attrs.default_prob,
                'recovery_rate': advanced_attrs.recovery_rate, 
                'expected_loss': advanced_attrs.expected_loss,
                'current_spread': advanced_attrs.current_spread
            })
        
        static_df = pd.DataFrame(static_metrics)
        advanced_df = pd.DataFrame(advanced_metrics)
        
        return {
            'mode_comparison': {
                'static_mode': {
                    'avg_default_prob': static_df['default_prob'].mean(),
                    'avg_recovery_rate': static_df['recovery_rate'].mean(),
                    'total_expected_loss': static_df['expected_loss'].sum(),
                    'avg_spread': static_df['current_spread'].mean()
                },
                'advanced_mode': {
                    'avg_default_prob': advanced_df['default_prob'].mean(),
                    'avg_recovery_rate': advanced_df['recovery_rate'].mean(),
                    'total_expected_loss': advanced_df['expected_loss'].sum(),
                    'avg_spread': advanced_df['current_spread'].mean()
                }
            },
            'impact_analysis': {
                'default_prob_change': (advanced_df['default_prob'].mean() - static_df['default_prob'].mean()) / static_df['default_prob'].mean() * 100,
                'recovery_rate_change': (advanced_df['recovery_rate'].mean() - static_df['recovery_rate'].mean()) / static_df['recovery_rate'].mean() * 100,
                'expected_loss_change': (advanced_df['expected_loss'].sum() - static_df['expected_loss'].sum()) / static_df['expected_loss'].sum() * 100,
                'spread_change': (advanced_df['current_spread'].mean() - static_df['current_spread'].mean()) / max(static_df['current_spread'].mean(), 0.001) * 100
            }
        }


# Convenience functions for easy integration
def get_cashflow_attributes(loan_data: Dict, advanced_mode: bool = True) -> CreditAdjustedAttributes:
    """
    Convenience function to get credit-adjusted attributes for a single loan
    
    Args:
        loan_data: Dictionary with loan metadata
        advanced_mode: Whether to use advanced credit adjustments
        
    Returns:
        CreditAdjustedAttributes object
    """
    integrator = CreditRiskCashflowIntegrator(advanced_mode=advanced_mode)
    return integrator.get_credit_adjusted_attributes(loan_data)


def compare_cashflow_modes(loan_portfolio: pd.DataFrame) -> Dict:
    """
    Convenience function to compare static vs advanced mode impacts
    
    Args:
        loan_portfolio: Portfolio DataFrame
        
    Returns:
        Comparison report dictionary
    """
    integrator = CreditRiskCashflowIntegrator(advanced_mode=True)
    return integrator.generate_credit_summary_report(loan_portfolio)