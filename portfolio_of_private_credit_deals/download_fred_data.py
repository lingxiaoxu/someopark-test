#!/usr/bin/env python3
"""
Download FRED interest rate data for bond and loan modeling

DEPRECATED (2026-06): fred_rates.csv is now produced by the repo-root
``SyncPrivateCreditRates.py``, which reads the 8 rate/spread series from the
project's single FRED access point (``MacroStateStore``) and rewrites this CSV in
the SAME format. Do NOT run this script in production — it bypasses the unified
store and would double-maintain FRED. Kept only as a standalone reference / break-glass.
"""
import os
import pandas as pd
from fredapi import Fred
import datetime as dt

# FRED API key from environment variable (set via .env in project root)
fred = Fred(api_key=os.environ.get('FRED_API_KEY', ''))

def download_fred_rates():
    """Download key interest rate series from FRED"""
    
    # Define the series we need
    series = {
        'SOFR': 'SOFR',           # Secured Overnight Financing Rate
        'EFFR': 'EFFR',           # Effective Federal Funds Rate (proxy for OIS)
        'DGS2': 'DGS2',           # 2-Year Treasury Constant Maturity Rate  
        'DGS10': 'DGS10',         # 10-Year Treasury Constant Maturity Rate
        'DGS5': 'DGS5',           # 5-Year Treasury
        'DGS30': 'DGS30',         # 30-Year Treasury
        'BAMLC0A0CM': 'IG_CREDIT', # Investment Grade Corporate Bond Spread
        'BAMLH0A0HYM2': 'HY_CREDIT'# High Yield Corporate Bond Spread
    }
    
    # Download data from 2020 to present
    start_date = '2020-01-01'
    end_date = dt.date.today().strftime('%Y-%m-%d')
    
    print(f"Downloading FRED data from {start_date} to {end_date}...")
    
    rates_data = {}
    for name, series_id in series.items():
        try:
            print(f"Downloading {name} ({series_id})...")
            data = fred.get_series(series_id, start=start_date, end=end_date)
            rates_data[name] = data
        except Exception as e:
            print(f"Error downloading {name}: {e}")
            # Create dummy data if download fails
            dates = pd.date_range(start_date, end_date, freq='D')
            if 'SOFR' in name or 'EFFR' in name:
                rates_data[name] = pd.Series(5.0, index=dates)  # 5% default
            elif '2' in name:
                rates_data[name] = pd.Series(4.5, index=dates)  # 4.5% default
            elif '10' in name:
                rates_data[name] = pd.Series(4.2, index=dates)  # 4.2% default
            else:
                rates_data[name] = pd.Series(3.0, index=dates)  # 3% default
    
    # Combine into DataFrame
    rates_df = pd.DataFrame(rates_data)
    
    # Forward fill missing values (weekends/holidays)
    rates_df = rates_df.ffill()
    
    # Convert to decimal format (FRED data is in percentages)
    rates_df = rates_df / 100.0
    
    # Save to CSV
    rates_df.to_csv('fred_rates.csv')
    print(f"Saved {len(rates_df)} rows of rate data to fred_rates.csv")
    
    # Show recent data
    print("\nRecent rates (last 5 days):")
    print(rates_df.tail().round(4))
    
    return rates_df

if __name__ == "__main__":
    rates_df = download_fred_rates()