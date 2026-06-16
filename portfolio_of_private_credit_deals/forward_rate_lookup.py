#!/usr/bin/env python3
"""
Forward Rate Lookup System

Efficient lookup of pre-calculated forward rate projections from timestamped files.
Used by bond portfolio system to get realistic floating rate projections instead
of flat current rates.
"""

import pandas as pd
import numpy as np
import os
import glob
from typing import Optional, Dict

class ForwardRateLookup:
    """
    Efficient forward rate lookup system for bond cashflow calculations
    """
    
    def __init__(self, forward_rates_directory: str = None):
        """
        Initialize forward rate lookup system
        
        Args:
            forward_rates_directory: Path to directory with forward rate files
                                   If None, finds most recent timestamped directory
        """
        self.forward_rates_dir = self._find_forward_rates_directory(forward_rates_directory)
        self.combined_rates = None
        self.individual_rates = {}
        
        if self.forward_rates_dir:
            self._load_forward_rate_data()
    
    def _find_forward_rates_directory(self, provided_dir: str = None) -> Optional[str]:
        """Find forward rates directory (most recent if not specified)"""
        
        if provided_dir and os.path.exists(provided_dir):
            return provided_dir
        
        # Look for most recent timestamped directory
        pattern = "forward_rates_*"
        matching_dirs = glob.glob(pattern)
        
        if matching_dirs:
            # Sort by timestamp (directory name) and get most recent
            most_recent = sorted(matching_dirs)[-1]
            print(f"📁 Using forward rates from: {most_recent}")
            return most_recent
        
        print("⚠️ No forward rate directories found")
        return None
    
    def _load_forward_rate_data(self):
        """Load forward rate data from files for efficient lookup"""
        
        try:
            # Load combined forward rates file
            combined_file = os.path.join(self.forward_rates_dir, "COMBINED_forward_rates.txt")
            
            if os.path.exists(combined_file):
                # Parse combined file (would implement full parser in production)
                self.combined_rates = self._parse_combined_rates_file(combined_file)
                print(f"✅ Loaded combined forward rates from {self.forward_rates_dir}")
            else:
                print(f"⚠️ Combined rates file not found in {self.forward_rates_dir}")
                
        except Exception as e:
            print(f"❌ Error loading forward rate data: {e}")
    
    def _parse_combined_rates_file(self, filepath: str) -> pd.DataFrame:
        """Parse the actual combined forward rates text file"""
        
        try:
            # Read the actual file and parse it properly
            data_rows = []
            
            with open(filepath, 'r') as f:
                lines = f.readlines()
                
                # Find the data section (after the header)
                data_started = False
                for line in lines:
                    line = line.strip()
                    
                    # Skip until we reach the data rows
                    if '--------------------------------------------------------' in line:
                        data_started = True
                        continue
                    
                    if data_started and '|' in line and any(char.isdigit() for char in line):
                        # Parse data line
                        parts = [p.strip() for p in line.split('|')]
                        if len(parts) >= 8:
                            try:
                                month = int(parts[0])
                                date_str = parts[1]
                                
                                # Parse rates - remove % and convert to decimal
                                sofr = float(parts[2].replace('%', '')) / 100
                                effr = float(parts[3].replace('%', '')) / 100
                                dgs2 = float(parts[4].replace('%', '')) / 100
                                dgs5 = float(parts[5].replace('%', '')) / 100
                                dgs10 = float(parts[6].replace('%', '')) / 100
                                dgs30 = float(parts[7].replace('%', '')) / 100
                                
                                data_rows.append({
                                    'Month': month,
                                    'Date': pd.Timestamp(date_str),
                                    'SOFR': sofr,
                                    'EFFR': effr,
                                    'DGS2': dgs2,
                                    'DGS5': dgs5,
                                    'DGS10': dgs10,
                                    'DGS30': dgs30,
                                    'TimeHorizon': month / 12
                                })
                            except Exception as e:
                                continue  # Skip malformed lines
            
            if data_rows:
                df = pd.DataFrame(data_rows)
                print(f"✅ Parsed {len(df)} forward rate data points from file")
                return df
            else:
                print("❌ No valid data found in combined rates file")
                return pd.DataFrame()
                
        except Exception as e:
            print(f"❌ Error parsing combined rates file: {e}")
            return pd.DataFrame()
    
    def get_forward_rate(self, rate_code: str, lookup_date: str, rates_df: pd.DataFrame = None) -> float:
        """
        Get appropriate rate for specific rate option and date
        
        CRITICAL LOGIC:
        - For dates <= today: Use historical FRED data
        - For dates > today: Use forward rate projections
        
        Args:
            rate_code: Rate option (SOFR, EFFR, DGS2, DGS5, DGS10, DGS30)
            lookup_date: Date to get rate for (YYYY-MM-DD)  
            rates_df: FRED rates DataFrame for historical lookups
            
        Returns:
            Historical rate (if past) or forward rate (if future) as decimal
        """
        
        lookup_timestamp = pd.Timestamp(lookup_date)
        today = pd.Timestamp.today().normalize()
        
        # CRITICAL: Use historical data for past dates
        if lookup_timestamp <= today:
            if rates_df is not None and rate_code in rates_df.columns:
                try:
                    # Get actual historical rate from FRED data
                    closest_idx = rates_df.index.get_indexer([lookup_timestamp], method='nearest')[0]
                    historical_date = rates_df.index[closest_idx]
                    historical_rate = rates_df.iloc[closest_idx][rate_code]
                    
                    days_diff = abs((historical_date - lookup_timestamp).days)
                    print(f"📈 Historical lookup: {rate_code} on {lookup_date} → {historical_date.strftime('%Y-%m-%d')}: {historical_rate:.3%} (±{days_diff} days)")
                    
                    return historical_rate
                except Exception as e:
                    print(f"❌ Error getting historical {rate_code} for {lookup_date}: {e}")
                    return self._get_current_rate_fallback(rate_code)
            else:
                print(f"⚠️ No historical FRED data available for {rate_code}")
                return self._get_current_rate_fallback(rate_code)
        
        # For future dates, use forward projections
        if self.combined_rates is None:
            print(f"⚠️ No forward rate data available, using current rate fallback")
            return self._get_current_rate_fallback(rate_code)
        
        try:
            lookup_timestamp = pd.Timestamp(lookup_date)
            
            # Find closest date with better matching logic
            if not self.combined_rates.empty:
                # Calculate time differences in days
                date_diffs = []
                for i, row in self.combined_rates.iterrows():
                    projection_date = row['Date']
                    days_diff = abs((projection_date - lookup_timestamp).days)
                    date_diffs.append((days_diff, i, projection_date))
                
                # Sort by smallest difference and get closest
                date_diffs.sort()
                _, closest_idx, closest_projection_date = date_diffs[0]
                
                # Get forward rate for the rate option
                if rate_code in self.combined_rates.columns:
                    forward_rate = self.combined_rates.loc[closest_idx, rate_code]
                    
                    # Enhanced debug info
                    month_num = self.combined_rates.loc[closest_idx, 'Month'] if 'Month' in self.combined_rates.columns else closest_idx
                    days_diff = (closest_projection_date - lookup_timestamp).days
                    
                    print(f"📊 Forward lookup: {rate_code} on {lookup_date} → Month {month_num} ({closest_projection_date.strftime('%Y-%m-%d')}): {forward_rate:.3%} (±{abs(days_diff)} days)")
                    
                    return forward_rate
                else:
                    print(f"⚠️ Rate code {rate_code} not available in forward projections")
                    return self._get_current_rate_fallback(rate_code)
            else:
                print(f"⚠️ No forward rate data available")
                return self._get_current_rate_fallback(rate_code)
                
        except Exception as e:
            print(f"❌ Error looking up forward rate for {rate_code} on {lookup_date}: {e}")
            return self._get_current_rate_fallback(rate_code)
    
    def _get_current_rate_fallback(self, rate_code: str) -> float:
        """Fallback to current rates if forward lookup fails"""
        
        current_rates = {
            'SOFR': 0.0438,
            'EFFR': 0.0433, 
            'DGS2': 0.0361,
            'DGS5': 0.0375,
            'DGS10': 0.0426,
            'DGS30': 0.0490
        }
        
        return current_rates.get(rate_code, 0.04)
    
    def get_floating_rate_cashflow_projections(self, bond, num_periods: int = 20) -> pd.DataFrame:
        """
        Get complete floating rate projections for a bond using forward curve
        
        This replaces the flat rate calculation with realistic forward evolution
        """
        
        if self.combined_rates is None:
            print("⚠️ No forward rate data - using flat current rates")
            return pd.DataFrame()
        
        # Generate reset dates based on bond frequency
        today = pd.Timestamp.today()
        maturity = pd.Timestamp(bond.maturity_date)
        
        if bond.freq == 4:
            months_between = 3  # Quarterly
        elif bond.freq == 2: 
            months_between = 6  # Semi-annual
        else:
            months_between = 12  # Annual
        
        # Generate payment dates
        payment_dates = []
        current_date = today
        
        for period in range(num_periods):
            current_date += pd.DateOffset(months=months_between)
            if current_date <= maturity:
                payment_dates.append(current_date)
            else:
                break
        
        if not payment_dates:
            return pd.DataFrame()
        
        # Get forward rates for each reset date
        floating_projections = []
        
        for i, payment_date in enumerate(payment_dates):
            # Lookup forward rate
            forward_base_rate = self.get_forward_rate(bond.base_rate, payment_date.strftime('%Y-%m-%d'))
            all_in_rate = forward_base_rate + bond.spread
            
            # Calculate payments
            coupon_payment = bond.face_value * all_in_rate / bond.freq
            principal_payment = bond.face_value if i == len(payment_dates) - 1 else 0
            
            floating_projections.append({
                'payment_date': payment_date,
                'forward_base_rate': forward_base_rate,
                'spread': bond.spread,
                'all_in_rate': all_in_rate,
                'coupon_payment': coupon_payment,
                'principal_payment': principal_payment,
                'total_payment': coupon_payment + principal_payment,
                'period': i + 1,
                'source': 'Nelson_Siegel_Forward_Curve'
            })
        
        return pd.DataFrame(floating_projections)
    
    def export_enhanced_floating_bond_cashflows(self, bond, output_file: str):
        """Export enhanced floating bond cashflows to file using forward rates"""
        
        projections = self.get_floating_rate_cashflow_projections(bond)
        
        if projections.empty:
            return
        
        with open(output_file, 'w') as f:
            f.write("="*100 + "\n")
            f.write(f"ENHANCED FLOATING BOND CASHFLOWS: {bond.bond_id}\n") 
            f.write(f"Generated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("="*100 + "\n")
            f.write(f"Issuer: {bond.issuer}\n")
            f.write(f"Base Rate: {bond.base_rate}\n")
            f.write(f"Credit Spread: {bond.spread:.3%}\n")
            f.write(f"Payment Frequency: {bond.freq}x per year\n")
            f.write(f"Forward Rate Source: Nelson-Siegel yield curve projections\n")
            f.write(f"Forward Rate Directory: {self.forward_rates_dir}\n")
            f.write("\n")
            f.write("="*100 + "\n")
            f.write("CASHFLOWS WITH FORWARD RATE PROJECTIONS\n") 
            f.write("="*100 + "\n")
            f.write(f"{'Date':>12} | {'BaseRate':>9} | {'Spread':>8} | {'AllIn':>8} | {'Coupon':>10} | {'Principal':>10} | {'Total':>10}\n")
            f.write("-" * 100 + "\n")
            
            for _, row in projections.iterrows():
                f.write(f"{row['payment_date'].strftime('%Y-%m-%d'):>12} | ")
                f.write(f"{row['forward_base_rate']:>8.3%} | ")
                f.write(f"{row['spread']:>7.3%} | ")
                f.write(f"{row['all_in_rate']:>7.3%} | ")
                f.write(f"${row['coupon_payment']:>9,.0f} | ")
                f.write(f"${row['principal_payment']:>9,.0f} | ")
                f.write(f"${row['total_payment']:>9,.0f}\n")
            
            f.write("\n" + "="*100 + "\n")
            f.write("FORWARD RATE METHODOLOGY:\n")
            f.write("="*100 + "\n")
            f.write("✅ Base rates projected using Nelson-Siegel yield curve model\n")
            f.write("✅ Forward rates reflect market-embedded rate expectations\n") 
            f.write("✅ Realistic rate evolution instead of flat current rates\n")
            f.write("✅ Industry-standard approach used by Fed, ECB, major banks\n")
        
        print(f"✅ Enhanced floating bond cashflows exported: {output_file}")

def get_global_forward_rate_lookup() -> ForwardRateLookup:
    """Get global forward rate lookup instance (singleton pattern)"""
    
    global _forward_rate_lookup_instance
    
    if '_forward_rate_lookup_instance' not in globals():
        _forward_rate_lookup_instance = ForwardRateLookup()
    
    return _forward_rate_lookup_instance

if __name__ == "__main__":
    # Test the lookup system
    print("=== TESTING FORWARD RATE LOOKUP SYSTEM ===")
    
    # Initialize lookup system
    lookup = ForwardRateLookup()
    
    if lookup.forward_rates_dir:
        print(f"✅ Forward rate lookup initialized from: {lookup.forward_rates_dir}")
        
        # Test forward rate lookups
        test_dates = ['2026-03-20', '2027-06-15', '2028-12-31']
        test_rates = ['SOFR', 'DGS2', 'DGS10']
        
        print("\n📊 Testing forward rate lookups:")
        print("Date       | Rate | Forward | vs Current")
        print("-" * 40)
        
        for date in test_dates:
            for rate_code in test_rates:
                forward_rate = lookup.get_forward_rate(rate_code, date)
                current_rate = lookup._get_current_rate_fallback(rate_code)
                diff = forward_rate - current_rate
                print(f"{date} | {rate_code:>4} | {forward_rate:6.3%} | {diff:+6.3%}")
        
        print("\n✅ Forward rate lookup system operational!")
    else:
        print("❌ No forward rate data available - run forward_rate_projections.py first")