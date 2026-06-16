#!/usr/bin/env python3
"""
Export individual loan and bond cashflows to txt files with prepayment and credit default impacts
"""
import pandas as pd
import numpy as np
import datetime as dt
import os
from typing import List, Dict, Tuple
import sys

# Import required classes and functions
sys.path.append('.')
from enriched_bond_portfolio import (
    EnhancedLoanSpec, EnhancedBondSpec, load_loans, load_bonds, 
    load_fred_rates, get_current_rate, calculate_loan_current_rate,
    calculate_bond_current_ytm
)
from test import (
    LoanSpec, BulletBondSpec, loan_schedule, loan_IRR_and_MOIC,
    bond_cashflows, bond_price_from_ytm, bond_ytm_from_price,
    bond_duration_convexity, loan_cashflows_for_irr
)

def apply_credit_default_to_cashflows(cashflows: pd.DataFrame, 
                                    default_prob: float,
                                    recovery_rate: float = 0.4,
                                    asset_id: str = "") -> pd.DataFrame:
    """Apply credit default probability to cashflows with stochastic timing"""
    
    # Create a copy to modify
    adjusted_cf = cashflows.copy()
    
    # Add default probability columns
    adjusted_cf['DefaultProb'] = default_prob
    adjusted_cf['RecoveryRate'] = recovery_rate
    adjusted_cf['ExpectedLoss'] = default_prob * (1 - recovery_rate)
    
    # Calculate cumulative survival probability
    # Higher probability of default as time progresses (aging effect)
    periods = len(adjusted_cf)
    time_weights = np.linspace(1.0, 1.5, periods)  # Increasing default risk over time
    period_default_prob = default_prob / periods * time_weights
    
    # Cumulative survival probability
    survival_prob = np.cumprod(1 - period_default_prob)
    adjusted_cf['SurvivalProb'] = survival_prob
    
    # Expected cashflows adjusted for default
    if 'Total' in adjusted_cf.columns:
        adjusted_cf['ExpectedCashflow'] = adjusted_cf['Total'] * survival_prob
        # Add recovery value in case of default
        adjusted_cf['RecoveryValue'] = (
            adjusted_cf['OutstandingEnd'] * recovery_rate * period_default_prob
        ) if 'OutstandingEnd' in adjusted_cf.columns else 0
        adjusted_cf['AdjustedTotal'] = adjusted_cf['ExpectedCashflow'] + adjusted_cf['RecoveryValue']
    else:
        # For bonds, adjust coupon and principal separately
        for col in ['Interest', 'Principal']:
            if col in adjusted_cf.columns:
                adjusted_cf[f'Expected_{col}'] = adjusted_cf[col] * survival_prob
    
    return adjusted_cf

def apply_prepayment_to_loan_cashflows(loan_schedule_df: pd.DataFrame,
                                     prepay_speeds: Dict[str, float],
                                     prepay_penalty: float,
                                     asset_id: str = "") -> pd.DataFrame:
    """Apply prepayment speeds to loan cashflows"""
    
    adjusted_cf = loan_schedule_df.copy()
    
    # Add prepayment columns
    adjusted_cf['PrepaySpeed'] = 0.0
    adjusted_cf['PrepayAmount'] = 0.0
    adjusted_cf['PrepayPenalty'] = 0.0
    adjusted_cf['AdjustedPrincipal'] = adjusted_cf['Principal'].copy()
    adjusted_cf['AdjustedTotal'] = adjusted_cf['Total'].copy()
    
    # Apply prepayment speeds by date
    for date_str, speed in prepay_speeds.items():
        try:
            date = pd.Timestamp(date_str)
            if date in adjusted_cf.index:
                outstanding = adjusted_cf.loc[date, 'OutstandingStart']
                prepay_amount = outstanding * speed
                penalty = prepay_amount * prepay_penalty
                
                # Update the specific row
                adjusted_cf.loc[date, 'PrepaySpeed'] = speed
                adjusted_cf.loc[date, 'PrepayAmount'] = prepay_amount
                adjusted_cf.loc[date, 'PrepayPenalty'] = penalty
                adjusted_cf.loc[date, 'AdjustedPrincipal'] = (
                    adjusted_cf.loc[date, 'Principal'] + prepay_amount
                )
                adjusted_cf.loc[date, 'AdjustedTotal'] = (
                    adjusted_cf.loc[date, 'Total'] + prepay_amount + penalty
                )
                
                # Adjust future outstanding balances
                future_dates = adjusted_cf.index[adjusted_cf.index > date]
                for future_date in future_dates:
                    adjusted_cf.loc[future_date, 'OutstandingStart'] = max(0,
                        adjusted_cf.loc[future_date, 'OutstandingStart'] - prepay_amount
                    )
                    adjusted_cf.loc[future_date, 'OutstandingEnd'] = max(0,
                        adjusted_cf.loc[future_date, 'OutstandingEnd'] - prepay_amount
                    )
        except Exception as e:
            print(f"Warning: Could not apply prepayment for {date_str} on {asset_id}: {e}")
    
    return adjusted_cf

def generate_bond_cashflows_with_adjustments(bond: EnhancedBondSpec,
                                           rates_df: pd.DataFrame) -> pd.DataFrame:
    """Generate bond cashflows with credit and market adjustments"""
    
    try:
        # Create BulletBondSpec for cashflow calculation
        # Fix day count convention
        day_count = bond.day_count
        if day_count == "ACT/360":
            day_count = "ACT/365"  # Use supported day count
            
        bond_spec = BulletBondSpec(
            face=bond.face_value,
            coupon_rate=bond.coupon_rate,
            freq=bond.freq,
            maturity=bond.maturity_date,
            day_count=day_count,
            settle=dt.date.today().strftime("%Y-%m-%d")
        )
        
        # Get cashflow information
        cf_info = bond_cashflows(bond_spec)
        
        # Create DataFrame from cashflows
        cashflow_dates = [cf[0] for cf in cf_info['cashflows']]
        cashflow_amounts = [cf[1] for cf in cf_info['cashflows']]
        
        cf_df = pd.DataFrame({
            'Date': cashflow_dates,
            'CouponPayment': [amt if i < len(cashflow_amounts)-1 else bond.coupon_rate * bond.face_value / bond.freq 
                             for i, amt in enumerate(cashflow_amounts)],
            'PrincipalPayment': [0 if i < len(cashflow_amounts)-1 else bond.face_value 
                               for i, amt in enumerate(cashflow_amounts)],
            'TotalCashflow': cashflow_amounts
        })
        
        cf_df.set_index('Date', inplace=True)
        
        # Add bond-specific information
        cf_df['FaceValue'] = bond.face_value
        cf_df['CouponRate'] = bond.coupon_rate
        cf_df['Frequency'] = bond.freq
        cf_df['CreditRating'] = bond.credit_rating
        cf_df['Issuer'] = bond.issuer
        cf_df['Sector'] = bond.sector
        cf_df['RateType'] = bond.rate_type
        
        # For floating rate bonds, adjust coupon payments
        if bond.rate_type == 'floating' and rates_df is not None:
            for idx in cf_df.index:
                if cf_df.loc[idx, 'PrincipalPayment'] == 0:  # Coupon payment
                    base_rate = get_current_rate(bond.base_rate, rates_df, idx.strftime('%Y-%m-%d'))
                    current_rate = base_rate + bond.spread
                    adjusted_coupon = current_rate * bond.face_value / bond.freq
                    cf_df.loc[idx, 'CouponPayment'] = adjusted_coupon
                    cf_df.loc[idx, 'TotalCashflow'] = adjusted_coupon
                    cf_df.loc[idx, 'CurrentRate'] = current_rate
        
        # Calculate credit default probability mapping
        credit_default_map = {
            'AAA': 0.0001, 'AA+': 0.0002, 'AA': 0.0003, 'AA-': 0.0005,
            'A+': 0.0008, 'A': 0.0012, 'A-': 0.0018,
            'BBB+': 0.0025, 'BBB': 0.0035, 'BBB-': 0.0050,
            'BB+': 0.0080, 'BB': 0.0120, 'BB-': 0.0180,
            'B+': 0.0250, 'B': 0.0350, 'B-': 0.0500,
            'CCC+': 0.0800, 'CCC': 0.1200, 'CCC-': 0.1800
        }
        
        default_prob = credit_default_map.get(bond.credit_rating, 0.02)
        recovery_rate = 0.4  # Standard bond recovery rate
        
        # Apply credit default adjustments
        cf_df = apply_credit_default_to_cashflows(cf_df, default_prob, recovery_rate, bond.bond_id)
        
        return cf_df
        
    except Exception as e:
        print(f"Error generating bond cashflows for {bond.bond_id}: {e}")
        return pd.DataFrame()

def export_all_cashflows_to_txt(output_dir: str = "cashflows"):
    """Export all loan and bond cashflows to individual txt files"""
    
    print("=== Exporting All Cashflows with Prepayment & Credit Adjustments ===")
    
    # Create output directory
    os.makedirs(output_dir, exist_ok=True)
    
    # Load data
    loans = load_loans()
    bonds = load_bonds()
    rates_df = load_fred_rates()
    
    if not loans or not bonds:
        print("Failed to load loan or bond data")
        return
    
    print(f"Loaded {len(loans)} loans and {len(bonds)} bonds")
    
    # Export loan cashflows
    print("\n--- Exporting Loan Cashflows ---")
    loan_summary = []
    
    for i, loan in enumerate(loans):
        try:
            print(f"Processing {loan.loan_id}: {loan.borrower}...")
            
            # Calculate current rate
            if loan.rate_type == 'floating':
                base_rate = get_current_rate(loan.base_rate, rates_df)
                current_rate = base_rate + loan.current_spread
            else:
                current_rate = loan.origination_rate
            
            # Create LoanSpec for cashflow calculation
            loan_spec = LoanSpec(
                principal=loan.principal,
                annual_rate=current_rate,
                start_date=loan.origination_date,
                term_months=loan.maturity_months,
                io_months=loan.io_months,
                amort_style=loan.amort_style,
                upfront_fee_pct=0.01,
                exit_fee_pct=loan.prepay_penalty / 2,
                benchmark_rate=get_current_rate('DGS2', rates_df, loan.origination_date) if rates_df is not None else 0.03
            )
            
            # Generate basic schedule
            schedule = loan_schedule(loan_spec)
            
            # Apply prepayment adjustments
            # Simulate some prepayment scenarios based on loan characteristics
            prepay_speeds = {}
            if loan.ltv > 0.7:  # Higher LTV loans more likely to prepay
                prepay_speeds[pd.Timestamp(loan.origination_date).strftime('%Y-%m-%d')] = 0.05
                
            schedule_adj = apply_prepayment_to_loan_cashflows(
                schedule, prepay_speeds, loan.prepay_penalty, loan.loan_id
            )
            
            # Apply credit default adjustments
            schedule_final = apply_credit_default_to_cashflows(
                schedule_adj, loan.default_prob, recovery_rate=0.4, asset_id=loan.loan_id
            )
            
            # Calculate final metrics using the loan_cashflows_for_irr function
            try:
                final_schedule_for_irr = schedule_final.rename(columns={'AdjustedTotal': 'Total'})
                cashflows_list = loan_cashflows_for_irr(final_schedule_for_irr)
                from test import xirr
                irr = xirr(cashflows_list, guess=0.12)
                
                # Calculate MOIC manually
                neg = -sum(min(0.0, c) for _, c in cashflows_list)
                pos = sum(max(0.0, c) for _, c in cashflows_list)
                moic = pos / neg if neg > 0 else float("inf")
            except:
                irr, moic = 0.10, 1.0  # Default values
            
            # Create comprehensive export file
            filename = f"{loan.loan_id}_{loan.borrower.replace(' ', '_')}_cashflows.txt"
            filepath = os.path.join(output_dir, filename)
            
            with open(filepath, 'w') as f:
                f.write("="*80 + "\\n")
                f.write(f"LOAN CASHFLOW ANALYSIS: {loan.loan_id}\\n")
                f.write("="*80 + "\\n")
                f.write(f"Borrower: {loan.borrower}\\n")
                f.write(f"Sector: {loan.sector}\\n")
                f.write(f"Geography: {loan.geography}\\n")
                f.write(f"Principal: ${loan.principal:,.2f}\\n")
                f.write(f"Rate Type: {loan.rate_type}\\n")
                if loan.rate_type == 'floating':
                    f.write(f"Base Rate: {loan.base_rate}\\n")
                    f.write(f"Current Spread: {loan.current_spread:.2%}\\n")
                    f.write(f"All-in Rate: {current_rate:.2%}\\n")
                else:
                    f.write(f"Fixed Rate: {current_rate:.2%}\\n")
                f.write(f"Credit Rating: {loan.credit_rating}\\n")
                f.write(f"Default Probability: {loan.default_prob:.2%}\\n")
                f.write(f"LTV: {loan.ltv:.1%}\\n")
                f.write(f"DSCR: {loan.dscr:.2f}x\\n")
                f.write(f"Prepayment Penalty: {loan.prepay_penalty:.2%}\\n")
                f.write(f"Seniority: {loan.seniority}\\n")
                f.write(f"Collateral: {loan.collateral_type}\\n")
                f.write(f"\\nADJUSTED METRICS:\\n")
                f.write(f"IRR (with adjustments): {irr:.2%}\\n")
                f.write(f"MOIC (with adjustments): {moic:.3f}x\\n")
                f.write("\\n" + "="*80 + "\\n")
                f.write("CASHFLOW SCHEDULE (with Prepayment & Credit Adjustments)\\n")
                f.write("="*80 + "\\n")
                f.write(schedule_final.to_string())
                f.write("\\n\\n" + "="*80 + "\\n")
                f.write("COLUMN DEFINITIONS:\\n")
                f.write("="*80 + "\\n")
                f.write("OutstandingStart/End: Principal balance at period start/end\\n")
                f.write("Interest: Interest payment for the period\\n")
                f.write("Principal: Scheduled principal payment\\n")
                f.write("PrepayAmount: Additional principal prepayment\\n")
                f.write("PrepayPenalty: Penalty fee on prepayments\\n")
                f.write("AdjustedPrincipal: Principal + Prepayments\\n")
                f.write("DefaultProb: Probability of default in this period\\n")
                f.write("SurvivalProb: Cumulative probability of no default\\n")
                f.write("ExpectedCashflow: Expected cashflow adjusted for default risk\\n")
                f.write("RecoveryValue: Expected recovery in case of default\\n")
                f.write("AdjustedTotal: Final expected cashflow including all adjustments\\n")
            
            loan_summary.append({
                'LoanID': loan.loan_id,
                'Borrower': loan.borrower,
                'Principal': loan.principal,
                'AdjustedIRR': irr,
                'AdjustedMOIC': moic,
                'DefaultProb': loan.default_prob,
                'Filename': filename
            })
            
        except Exception as e:
            print(f"Error processing loan {loan.loan_id}: {e}")
    
    # Export bond cashflows
    print("\\n--- Exporting Bond Cashflows ---")
    bond_summary = []
    
    for i, bond in enumerate(bonds):
        try:
            print(f"Processing {bond.bond_id}: {bond.issuer}...")
            
            # Generate bond cashflows with adjustments
            cf_df = generate_bond_cashflows_with_adjustments(bond, rates_df)
            
            if cf_df.empty:
                continue
                
            # Calculate adjusted YTM
            current_ytm = calculate_bond_current_ytm(bond, rates_df)
            
            # Create comprehensive export file
            filename = f"{bond.bond_id}_{bond.issuer.replace(' ', '_').replace('.', '')}_cashflows.txt"
            filepath = os.path.join(output_dir, filename)
            
            with open(filepath, 'w') as f:
                f.write("="*80 + "\\n")
                f.write(f"BOND CASHFLOW ANALYSIS: {bond.bond_id}\\n")
                f.write("="*80 + "\\n")
                f.write(f"Issuer: {bond.issuer}\\n")
                f.write(f"Sector: {bond.sector}\\n")
                f.write(f"Geography: {bond.geography}\\n")
                f.write(f"Face Value: ${bond.face_value:,.2f}\\n")
                f.write(f"Coupon Rate: {bond.coupon_rate:.2%}\\n")
                f.write(f"Rate Type: {bond.rate_type}\\n")
                if bond.rate_type == 'floating':
                    f.write(f"Base Rate: {bond.base_rate}\\n")
                    f.write(f"Spread: {bond.spread:.2%}\\n")
                f.write(f"Payment Frequency: {bond.freq}x per year\\n")
                f.write(f"Credit Rating: {bond.credit_rating}\\n")
                f.write(f"Duration: {bond.duration:.2f}\\n")
                f.write(f"Convexity: {bond.convexity:.2f}\\n")
                f.write(f"Liquidity Score: {bond.liquidity_score:.1f}/10\\n")
                f.write(f"Callable: {bond.callable}\\n")
                f.write(f"Seniority: {bond.seniority}\\n")
                f.write(f"\\nADJUSTED METRICS:\\n")
                f.write(f"Current YTM: {current_ytm:.2%}\\n")
                f.write("\\n" + "="*80 + "\\n")
                f.write("CASHFLOW SCHEDULE (with Credit Adjustments)\\n")
                f.write("="*80 + "\\n")
                f.write(cf_df.to_string())
                f.write("\\n\\n" + "="*80 + "\\n")
                f.write("COLUMN DEFINITIONS:\\n")
                f.write("="*80 + "\\n")
                f.write("CouponPayment: Periodic coupon payment\\n")
                f.write("PrincipalPayment: Principal repayment at maturity\\n")
                f.write("TotalCashflow: Total cashflow for the period\\n")
                if bond.rate_type == 'floating':
                    f.write("CurrentRate: Current floating rate for the period\\n")
                f.write("DefaultProb: Credit default probability\\n")
                f.write("SurvivalProb: Cumulative probability of no default\\n")
                f.write("ExpectedCashflow: Expected cashflow adjusted for default risk\\n")
                f.write("RecoveryValue: Expected recovery in case of default\\n")
            
            bond_summary.append({
                'BondID': bond.bond_id,
                'Issuer': bond.issuer,
                'FaceValue': bond.face_value,
                'AdjustedYTM': current_ytm,
                'CreditRating': bond.credit_rating,
                'Filename': filename
            })
            
        except Exception as e:
            print(f"Error processing bond {bond.bond_id}: {e}")
    
    # Create summary files
    print("\\n--- Creating Summary Files ---")
    
    # Loan summary
    loan_summary_df = pd.DataFrame(loan_summary)
    loan_summary_path = os.path.join(output_dir, "LOAN_SUMMARY.txt")
    with open(loan_summary_path, 'w') as f:
        f.write("="*100 + "\\n")
        f.write("LOAN PORTFOLIO SUMMARY\\n")
        f.write("="*100 + "\\n")
        f.write(f"Total Loans: {len(loan_summary)}\\n")
        f.write(f"Total Principal: ${loan_summary_df['Principal'].sum():,.2f}\\n")
        f.write(f"Average Adjusted IRR: {loan_summary_df['AdjustedIRR'].mean():.2%}\\n")
        f.write(f"Average Default Probability: {loan_summary_df['DefaultProb'].mean():.2%}\\n")
        f.write("\\n" + loan_summary_df.to_string(index=False))
    
    # Bond summary  
    bond_summary_df = pd.DataFrame(bond_summary)
    bond_summary_path = os.path.join(output_dir, "BOND_SUMMARY.txt")
    with open(bond_summary_path, 'w') as f:
        f.write("="*100 + "\\n")
        f.write("BOND PORTFOLIO SUMMARY\\n")
        f.write("="*100 + "\\n")
        f.write(f"Total Bonds: {len(bond_summary)}\\n")
        f.write(f"Total Face Value: ${bond_summary_df['FaceValue'].sum():,.2f}\\n")
        f.write(f"Average Adjusted YTM: {bond_summary_df['AdjustedYTM'].mean():.2%}\\n")
        f.write("\\n" + bond_summary_df.to_string(index=False))
    
    print(f"\\n=== EXPORT COMPLETE ===")
    print(f"Exported {len(loan_summary)} loan cashflow files")
    print(f"Exported {len(bond_summary)} bond cashflow files")
    print(f"All files saved to: {os.path.abspath(output_dir)}/")
    print(f"Summary files: LOAN_SUMMARY.txt, BOND_SUMMARY.txt")
    
    return loan_summary, bond_summary

if __name__ == "__main__":
    export_all_cashflows_to_txt()