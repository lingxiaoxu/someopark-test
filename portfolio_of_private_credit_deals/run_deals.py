#!/usr/bin/env python3
"""
Enhanced Portfolio Integration & Analytics for Private Loans
Combines deal_start.csv with portfolio.csv using existing loan infrastructure
Includes stressed forward rate generation and detailed cashflow analysis
Integrated PDF processing for memo data extraction
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from datetime import datetime, timedelta
import warnings
import sys
import os
import re
import shutil
import json
import logging
from dataclasses import dataclass, asdict
from typing import Dict, List, Tuple, Optional
import glob
from pathlib import Path
from credit_risk_module import CreditRiskCashflowIntegrator, get_cashflow_attributes, compare_cashflow_modes

warnings.filterwarnings('ignore')

def setup_deals_logging():
    """Setup comprehensive logging for deals analysis"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = f"deals_analysis_{timestamp}.log"
    
    # Create logs directory
    os.makedirs("logs", exist_ok=True)
    log_filepath = os.path.join("logs", log_filename)
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filepath),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Deals analysis logging started - {timestamp}")
    logger.info(f"Log file: {log_filepath}")
    
    return logger, log_filepath

# PDF Processing Functions (Integrated from load.py)
def parse_currency_to_number(currency_str):
    """
    Convert currency format string to number
    
    Args:
        currency_str (str): Currency format string like "$160,000,000", "$450m", "€1.5B"
        
    Returns:
        float: Converted number, returns 0.0 if conversion fails
    """
    if not currency_str or not isinstance(currency_str, str):
        return 0.0

    currency_str = currency_str.strip()
    
    # Remove currency symbols
    currency_str = re.sub(r'[$€£¥₹₽₩₪₨₦₡₱₴₸₼₾₿]', '', currency_str)
    
    # Handle scientific notation
    if 'e' in currency_str.lower():
        try:
            return float(currency_str)
        except ValueError:
            return 0.0
    
    # Handle abbreviations (m, k, B, T etc)
    multipliers = {
        'k': 1000, 'K': 1000,
        'm': 1000000, 'M': 1000000,
        'b': 1000000000, 'B': 1000000000,
        't': 1000000000000, 'T': 1000000000000
    }
    
    for suffix, multiplier in multipliers.items():
        if currency_str.endswith(suffix):
            number_part = currency_str[:-1]
            try:
                number = float(number_part.replace(',', ''))
                return number * multiplier
            except ValueError:
                return 0.0
    
    # Handle standard format (160,000,000)
    try:
        return float(currency_str.replace(',', ''))
    except ValueError:
        return 0.0

def get_openai_response(prompt):
    """Get response from OpenAI API"""
    try:
        import openai
        from config import OPENAI_API_KEY
        
        openai.api_key = OPENAI_API_KEY
        
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"OpenAI API error: {e}")
        return None

def process_memo_pdfs():
    """Process both structured and unstructured PDF memos"""
    print("Processing PDF memo files...")
    
    # Process structured PDFs
    structured_results = {}
    structured_dir = Path("memos_structured")
    
    if structured_dir.exists():
        pdf_files = list(structured_dir.glob("*.pdf"))
        print(f"Found {len(pdf_files)} structured PDF files")
        
        for pdf_file in pdf_files:
            print(f"Processing: {pdf_file.name}")
            try:
                import pdfplumber
                with pdfplumber.open(str(pdf_file)) as pdf:
                    text = ""
                    for page in pdf.pages:
                        text += page.extract_text() + "\n"
                
                # Extract structured data (simplified extraction)
                data = extract_structured_memo_data(text, pdf_file.name)
                if data:
                    structured_results[pdf_file.name] = data
                    print(f"Successfully extracted data from {pdf_file.name}")
            except Exception as e:
                print(f"Error processing {pdf_file.name}: {e}")
    
    # Process unstructured PDFs with AI
    unstructured_results = {}
    unstructured_dir = Path("memos_unstructured")
    
    if unstructured_dir.exists():
        pdf_files = list(unstructured_dir.glob("*.pdf"))
        print(f"Found {len(pdf_files)} unstructured PDF files")
        
        for pdf_file in pdf_files:
            print(f"Processing: {pdf_file.name}")
            try:
                import pdfplumber
                with pdfplumber.open(str(pdf_file)) as pdf:
                    text = ""
                    for page in pdf.pages:
                        text += page.extract_text() + "\n"
                
                data = extract_unstructured_memo_data(text)
                if data:
                    unstructured_results[pdf_file.name] = data
                    print(f"Successfully parsed {pdf_file.name}")
            except Exception as e:
                print(f"Error processing {pdf_file.name}: {e}")
    
    # Save to CSV files
    if structured_results:
        save_memo_csv(structured_results, "memos_structured.csv")
    
    if unstructured_results:
        save_memo_csv(unstructured_results, "memos_unstructured.csv")
    
    return structured_results, unstructured_results

def extract_structured_memo_data(text, filename):
    """Extract data from structured PDF memo"""
    # Extract company name from filename
    company = filename.replace('_structured.pdf', '').replace('_', ' ').title()
    
    # Simple pattern matching for structured data
    data = {'company': company}
    
    # Extract key fields using regex patterns
    patterns = {
        'sector': r'Sector:\s*([^\n]+)',
        'instrument': r'Instrument:\s*([^\n]+)',
        'currency': r'Currency:\s*([^\n]+)',
        'deal_size': r'Deal Size:\s*([^\n]+)',
        'coupon': r'Coupon:\s*([^\n]+)',
        'maturity': r'Maturity:\s*([^\n]+)',
        'ebitda': r'EBITDA.*?:\s*([^\n]+)',
        'leverage': r'Leverage:\s*([^\n]+)',
        'revenue_growth': r'Revenue Growth:\s*([^\n]+)',
        'risks': r'Key Risks:\s*([^\n]+(?:\n[^\n]*)*)'
    }
    
    for field, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        data[field] = match.group(1).strip() if match else ""
    
    return data

def extract_unstructured_memo_data(text):
    """Extract data from unstructured PDF using AI"""
    prompt = f"""
Please analyze the following investment memorandum PDF content and extract the following fields:

PDF Content:
{text}

Please extract the following fields. If a field has no clear information, please fill in "None":

1. Company Name
2. Sector
3. Instrument Type (financial instrument type)
4. Currency
5. Deal Size (transaction size, convert formats like $450m to $450,000,000)
6. Rate Type (interest rate type, Fixed, Floating, SOFR etc)
7. Spread (spread, if fixed rate provide percentage, if floating rate provide bps)
8. Maturity
9. EBITDA
10. Leverage
11. Revenue Growth (e.g.: 8% YoY)
12. Key Risks

Please return results in JSON format:
{{
    "company": "company name",
    "sector": "sector",
    "instrument": "instrument type",
    "currency": "currency",
    "deal_size": "deal size",
    "rate_type": "rate type",
    "spread": "spread",
    "maturity": "maturity",
    "ebitda": "EBITDA",
    "leverage": "leverage",
    "revenue_growth": "revenue growth",
    "risks": "key risks"
}}
"""
    
    response = get_openai_response(prompt)
    if not response:
        return None
    
    try:
        # Clean response text, remove markdown formatting
        cleaned_response = response.strip()
        if cleaned_response.startswith('```json'):
            cleaned_response = cleaned_response[7:]
        if cleaned_response.endswith('```'):
            cleaned_response = cleaned_response[:-3]
        
        data = json.loads(cleaned_response.strip())
        
        # Format the data for CSV output
        spread = data.get("spread", "")
        if data.get('rate_type') == 'SOFR' or 'SOFR' in str(data.get('spread', '')):
            data['coupon'] = f"SOFR + {spread}"
        elif data.get('rate_type') == 'Fixed':
            data['coupon'] = spread
        else:
            data['coupon'] = spread
        
        return data
        
    except json.JSONDecodeError as e:
        print(f"JSON parsing failed: {e}")
        return {"error": f"JSON parsing failed: {response}"}

def save_memo_csv(data, output_path):
    """Save memo data to CSV file"""
    if not data:
        print("No data to save")
        return
    
    csv_data = []
    for filename, file_data in data.items():
        company = filename.replace('_structured.pdf', '').replace('_unstructured.pdf', '').replace('_', ' ').title()
        
        if "error" in file_data:
            row = {
                'company': company,
                'sector': 'Error',
                'instrument': 'Error', 
                'currency': 'Error',
                'deal_size': 'Error',
                'coupon': 'Error',
                'maturity': 'Error',
                'ebitda': 'Error',
                'leverage': 'Error',
                'rev': 'Error',
                'risks': file_data.get('error', 'Unknown Error')
            }
        else:
            row = {
                'company': file_data.get('company', company),
                'sector': file_data.get('sector', ''),
                'instrument': file_data.get('instrument', ''),
                'currency': file_data.get('currency', ''),
                'deal_size': parse_currency_to_number(str(file_data.get('deal_size', ''))),
                'coupon': file_data.get('coupon', ''),
                'maturity': file_data.get('maturity', ''),
                'ebitda': parse_currency_to_number(str(file_data.get('ebitda', ''))),
                'leverage': file_data.get('leverage', ''),
                'rev': file_data.get('revenue_growth', ''),
                'risks': file_data.get('risks', '')
            }
        
        csv_data.append(row)
    
    df = pd.DataFrame(csv_data)
    df.to_csv(output_path, index=False)
    print(f"Data saved to {output_path}")
    print(f"Total records saved: {len(csv_data)}")
    print("\nData preview:")
    print(df.head())

warnings.filterwarnings('ignore')

# Import existing utilities
sys.path.append('.')
from bond_utilities import (
    LoanSpec, generate_loan_schedule, calculate_loan_irr_and_moic,
    calculate_year_fraction, plot_loan_cashflow_waterfall,
    plot_outstanding_balance_and_rates
)
from forward_rate_lookup import ForwardRateLookup
from forward_rate_projections import export_all_forward_rate_projections

@dataclass
class EnhancedLoanSpec:
    """Enhanced loan specification matching existing infrastructure"""
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
    instrument: str
    currency: str
    ebitda: float
    leverage: float
    revenue_growth: str
    risks: str

class PrivateLoanPortfolioAnalyzer:
    def __init__(self, deal_start_source='deals_data/deal_start_unstructured.csv', advanced_credit_mode=True):
        """Initialize the Private Loan Portfolio Analyzer
        
        Args:
            deal_start_source: Data source file ('deals_data/deal_start_unstructured.csv' or 'deals_data/deal_start_structured.csv')
            advanced_credit_mode: If True (default), uses credit risk modeling for cashflows. If False, uses static assumptions.
        """
        self.deal_start_source = deal_start_source
        self.advanced_credit_mode = advanced_credit_mode
        self.deal_start_df = None
        self.portfolio_df = None
        self.enhanced_loans = []
        self.combined_portfolio = None
        self.forward_rates = None
        self.export_forward_rate_projections = export_all_forward_rate_projections
        self.stressed_forward_rates = None
        self.timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Initialize credit risk integrator
        self.credit_integrator = CreditRiskCashflowIntegrator(advanced_mode=advanced_credit_mode)
        
        # Setup logger for this instance (will be properly configured in run_analysis)
        self.logger = None
    
    def _log_and_print(self, message):
        """Helper method to both log and print messages"""
        if self.logger:
            self.logger.info(message)
        else:
            print(message)
        
    def load_csv_data(self):
        """Load deal start data and portfolio.csv from flexible sources"""
        self._log_and_print(f"Loading CSV data files from source: {self.deal_start_source}")
        
        # Load deal start data from specified source
        try:
            self.deal_start_df = pd.read_csv(self.deal_start_source)
            self._log_and_print(f"Loaded {self.deal_start_source}: {len(self.deal_start_df)} records")
        except FileNotFoundError:
            self._log_and_print(f"Error: {self.deal_start_source} not found")
            return False
            
        # Load portfolio.csv  
        try:
            self.portfolio_df = pd.read_csv('deals_data/portfolio.csv')
            self._log_and_print(f"Loaded deals_data/portfolio.csv: {len(self.portfolio_df)} records")
        except FileNotFoundError:
            self._log_and_print("Error: deals_data/portfolio.csv not found")
            return False
            
        return True
    
    def regenerate_forward_rates_if_missing(self):
        """Regenerate forward rate projections if missing using existing functions"""
        self._log_and_print("Checking for forward rate projections...")
        
        # Check if forward rates directory exists
        original_dirs = glob.glob('forward_rates_202*')
        original_dirs = [d for d in original_dirs if 'stressed' not in d]
        
        if not original_dirs:
            print("No forward rate projections found. Regenerating using existing functions...")
            
            try:
                # Load FRED rates
                if not os.path.exists('fred_rates.csv'):
                    print("Downloading FRED data...")
                    # Import download function locally to avoid circular import
                    from download_fred_data import download_fred_data
                    download_fred_data()
                
                fred_df = pd.read_csv('fred_rates.csv', index_col=0, parse_dates=True)
                
                # Generate forward rate projections using existing function
                output_dir = export_all_forward_rate_projections(
                    rates_df=fred_df,
                    projection_horizon_years=10.0
                )
                
                print(f"✅ Generated forward rate projections in: {output_dir}")
                return output_dir
                
            except Exception as e:
                print(f"Error regenerating forward rates: {e}")
                return None
        else:
            print(f"✅ Found existing forward rate projections: {original_dirs}")
            return max(original_dirs)

    def regenerate_cashflows_if_missing(self):
        """Regenerate loan/bond cashflows if missing using existing functions"""
        print("Checking for existing cashflows...")
        
        # Check if cashflows directory exists
        cashflow_dirs = glob.glob('cashflows_202*')
        
        if not cashflow_dirs:
            print("No existing cashflows found. Regenerating using existing functions...")
            
            try:
                # Generate cashflows using existing cashflow exporter
                # Import locally to avoid circular import
                from cashflow_exporter import export_all_cashflows_to_txt
                output_dir = export_all_cashflows_to_txt()
                print(f"✅ Generated loan/bond cashflows in: {output_dir}")
                return output_dir
                
            except Exception as e:
                print(f"Error regenerating cashflows: {e}")
                return None
        else:
            print(f"✅ Found existing cashflows: {cashflow_dirs}")
            return max(cashflow_dirs)

    def setup_forward_rates(self):
        """Initialize forward rate lookup system with fallback regeneration"""
        self._log_and_print("Setting up forward rate lookup...")
        
        # First, ensure forward rates exist
        forward_dir = self.regenerate_forward_rates_if_missing()
        
        if not forward_dir:
            print("Error: Could not setup forward rates")
            self.forward_rates = None
            return
        
        try:
            self.forward_rates = ForwardRateLookup(forward_dir)
            print(f"Forward rate lookup initialized successfully from {forward_dir}")
        except Exception as e:
            print(f"Warning: Could not initialize forward rates: {e}")
            self.forward_rates = None

    def create_stressed_forward_rates(self):
        """Create stressed forward rates with shock scenarios"""
        self._log_and_print("Creating stressed forward rates...")
        
        if not self.forward_rates:
            print("Warning: Forward rates not available, cannot create stressed rates")
            return
        
        # Create stressed forward rates directory
        stressed_dir = f"forward_rates_stressed_{self.timestamp}"
        os.makedirs(stressed_dir, exist_ok=True)
        
        try:
            # Find the original forward rates directory
            original_dir = self.forward_rates.forward_rates_dir
            if not original_dir or not os.path.exists(original_dir):
                print("Warning: Original forward rates directory not found")
                return
            
            # Load the combined forward rates by parsing the text file
            combined_file = os.path.join(original_dir, 'COMBINED_forward_rates.txt')
            if not os.path.exists(combined_file):
                print("Warning: Combined forward rates file not found")
                return
            
            # Parse the combined forward rates file
            stressed_rates_data = []
            with open(combined_file, 'r') as f:
                lines = f.readlines()
                
            # Find the data section
            data_start = False
            for line in lines:
                if 'Month |' in line and 'Date |' in line:
                    data_start = True
                    continue
                if data_start and '---' in line:
                    continue
                if data_start and '===' in line:
                    break
                if data_start and line.strip():
                    # Parse data line
                    parts = line.strip().split('|')
                    if len(parts) >= 8:
                        try:
                            month = int(parts[0].strip())
                            date_str = parts[1].strip()
                            sofr = float(parts[2].strip().replace('%', '')) / 100
                            effr = float(parts[3].strip().replace('%', '')) / 100
                            dgs2 = float(parts[4].strip().replace('%', '')) / 100
                            dgs5 = float(parts[5].strip().replace('%', '')) / 100
                            dgs10 = float(parts[6].strip().replace('%', '')) / 100
                            dgs30 = float(parts[7].strip().replace('%', '')) / 100
                            time_years = float(parts[8].strip().replace('Y', ''))
                            
                            stressed_rates_data.append({
                                'month': month,
                                'date': date_str,
                                'SOFR': sofr,
                                'EFFR': effr,
                                'DGS2': dgs2,
                                'DGS5': dgs5,
                                'DGS10': dgs10,
                                'DGS30': dgs30,
                                'time_years': time_years
                            })
                        except (ValueError, IndexError) as e:
                            continue
            
            if not stressed_rates_data:
                print("Warning: No forward rate data could be parsed")
                return
            
            # Convert to DataFrame for easier manipulation
            stressed_df = pd.DataFrame(stressed_rates_data)
            
            # Apply stress scenarios
            # Scenario: -50bps parallel shift in risk-free rates (DGS series)
            risk_free_shock = -0.005  # -50 bps
            stressed_df['DGS2'] = np.maximum(0.001, stressed_df['DGS2'] + risk_free_shock)
            stressed_df['DGS5'] = np.maximum(0.001, stressed_df['DGS5'] + risk_free_shock) 
            stressed_df['DGS10'] = np.maximum(0.001, stressed_df['DGS10'] + risk_free_shock)
            stressed_df['DGS30'] = np.maximum(0.001, stressed_df['DGS30'] + risk_free_shock)
            
            # For SOFR and EFFR, apply smaller shock as they're tied to risk-free rates
            stressed_df['SOFR'] = np.maximum(0.001, stressed_df['SOFR'] + risk_free_shock * 0.8)
            stressed_df['EFFR'] = np.maximum(0.001, stressed_df['EFFR'] + risk_free_shock * 0.8)
            
            # Export stressed rates in same format as original
            self._export_stressed_rates(stressed_df, stressed_dir, original_dir)
            
            # Initialize stressed forward rate lookup
            self.stressed_forward_rates = ForwardRateLookup(stressed_dir)
            
            print(f"✅ Created stressed forward rates in: {stressed_dir}")
            
        except Exception as e:
            print(f"Error creating stressed forward rates: {e}")
            import traceback
            traceback.print_exc()
    
    def _export_stressed_rates(self, stressed_df, output_dir, original_dir):
        """Export stressed rates in same format as original forward rate files"""
        
        # Create COMBINED_forward_rates.txt
        combined_file = os.path.join(output_dir, 'COMBINED_forward_rates.txt')
        with open(combined_file, 'w') as f:
            f.write("="*120 + "\n")
            f.write("STRESSED FORWARD RATE PROJECTIONS - ALL RATE OPTIONS\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("="*120 + "\n")
            f.write("Stress Scenario Applied: -50bps parallel tightening in risk-free curve\n")
            f.write("Organized by month for detailed loan cashflow calculation and monthly resets.\n\n")
            f.write("="*120 + "\n")
            f.write("STRESSED FORWARD RATE LOOKUP TABLE\n")
            f.write("="*120 + "\n")
            f.write("   Month |         Date |     SOFR |     EFFR |     DGS2 |     DGS5 |    DGS10 |    DGS30 |     Time\n")
            f.write("-" * 120 + "\n")
            
            for idx, row in stressed_df.iterrows():
                month = row['month']
                date_str = row['date']
                time_years = row['time_years']
                
                f.write(f"{month:8d} | {date_str:>12} | {row['SOFR']*100:7.3f}% | {row['EFFR']*100:7.3f}% | "
                       f"{row['DGS2']*100:7.3f}% | {row['DGS5']*100:7.3f}% | {row['DGS10']*100:7.3f}% | "
                       f"{row['DGS30']*100:7.3f}% | {time_years:7.2f}Y\n")
        
        # Create individual rate series files
        rate_series = ['SOFR', 'EFFR', 'DGS2', 'DGS5', 'DGS10', 'DGS30']
        for rate in rate_series:
            # Read original file to get proper format
            original_file = os.path.join(original_dir, f"{rate}_forward_projections.txt")
            stressed_file = os.path.join(output_dir, f"{rate}_forward_projections.txt")
            
            with open(stressed_file, 'w') as f:
                f.write(f"STRESSED {rate} FORWARD RATE PROJECTIONS\n")
                f.write("=" * 60 + "\n")
                f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("Stress: -50bps parallel tightening applied\n\n")
                
                # Write the stressed data
                for idx, row in stressed_df.iterrows():
                    date_str = row['date']
                    rate_value = row[rate]
                    f.write(f"Month {row['month']:3d} ({date_str}): {rate_value*100:7.3f}%\n")
        
        # Copy and modify methodology file
        methodology_file = os.path.join(output_dir, 'METHODOLOGY_AND_SUMMARY.txt')
        with open(methodology_file, 'w') as f:
            f.write("STRESSED FORWARD RATE METHODOLOGY AND SUMMARY\n")
            f.write("=" * 60 + "\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("STRESS SCENARIO APPLIED:\n")
            f.write("• Risk-free rates (DGS2, DGS5, DGS10, DGS30): -50 bps parallel shift\n")
            f.write("• SOFR: -40 bps (80% of risk-free shock)\n")  
            f.write("• EFFR: -40 bps (80% of risk-free shock)\n\n")
            f.write("PURPOSE:\n")
            f.write("• Test portfolio sensitivity to interest rate environment changes\n")
            f.write("• Assess impact on floating rate loan cashflows\n")
            f.write("• Support comprehensive stress testing analysis\n\n")
            f.write("USAGE:\n")
            f.write("• Used for detailed loan cashflow projections under stress\n")
            f.write("• Applied to floating rate loans with SOFR/EFFR base rates\n")
            f.write("• Integrated with portfolio analytics for risk assessment\n")
        
        # Copy RATE_OPTIONS_MASTER.txt from original
        original_master = os.path.join(original_dir, 'RATE_OPTIONS_MASTER.txt')
        stressed_master = os.path.join(output_dir, 'RATE_OPTIONS_MASTER.txt')
        
        if os.path.exists(original_master):
            shutil.copy2(original_master, stressed_master)
            
            # Add stress note to the copied file
            with open(stressed_master, 'a') as f:
                f.write(f"\n\n# STRESS SCENARIO APPLIED ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}):\n")
                f.write("# • Risk-free rates (DGS2, DGS5, DGS10, DGS30): -50 bps parallel shift\n")
                f.write("# • SOFR: -40 bps (80% of risk-free shock)\n")
                f.write("# • EFFR: -40 bps (80% of risk-free shock)\n")
        
        print(f"✅ Exported {len(rate_series)} stressed rate series to {output_dir}")
        print(f"✅ Created COMBINED_forward_rates.txt with {len(stressed_df)} data points")
        print(f"✅ Created METHODOLOGY_AND_SUMMARY.txt with stress scenario documentation")

    def parse_leverage(self, leverage_str):
        """Parse leverage string to extract numeric value"""
        if pd.isna(leverage_str) or leverage_str == '':
            return 3.5  # Default leverage
        
        leverage_str = str(leverage_str).strip().lower()
        
        # Remove 'x' suffix if present
        if leverage_str.endswith('x'):
            leverage_str = leverage_str[:-1]
        
        try:
            return float(leverage_str)
        except ValueError:
            return 3.5  # Default if parsing fails

    def parse_coupon_rate(self, coupon_str):
        """Parse coupon string to extract base rate and spread"""
        if pd.isna(coupon_str) or coupon_str == '':
            return 'fixed', 'none', 0.0, 0.0
            
        coupon_str = str(coupon_str).strip()
        
        # Check for floating rate patterns
        if 'SOFR' in coupon_str.upper():
            base_rate = 'SOFR'
            rate_type = 'floating'
        elif 'LIBOR' in coupon_str.upper():
            base_rate = 'SOFR'  # Convert LIBOR to SOFR
            rate_type = 'floating'
        else:
            base_rate = 'none'
            rate_type = 'fixed'
        
        # Extract spread
        spread_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:bps|bp)', coupon_str.lower())
        if spread_match:
            spread = float(spread_match.group(1)) / 10000  # Convert bps to decimal
        else:
            # Try to extract percentage
            pct_match = re.search(r'(\d+(?:\.\d+)?)\s*%', coupon_str)
            if pct_match:
                if rate_type == 'floating':
                    spread = float(pct_match.group(1)) / 100
                else:
                    spread = 0.0
                    rate_type = 'fixed'
            else:
                spread = 0.0
        
        # Extract fixed rate if applicable
        if rate_type == 'fixed':
            pct_match = re.search(r'(\d+(?:\.\d+)?)\s*%', coupon_str)
            if pct_match:
                fixed_rate = float(pct_match.group(1)) / 100
            else:
                fixed_rate = 0.05  # Default 5%
        else:
            fixed_rate = 0.0
            
        return rate_type, base_rate, spread, fixed_rate
    
    def create_enhanced_deal_start_txt(self):
        """Create enhanced deal_start text file with loan attributes"""
        self._log_and_print("Creating enhanced deal_start.txt...")
        
        enhanced_deals = []
        
        for idx, row in self.deal_start_df.iterrows():
            # Parse coupon information
            rate_type, base_rate, spread, fixed_rate = self.parse_coupon_rate(row['coupon'])
            
            # Calculate maturity months (assuming deals are new as of 2025)
            maturity_year = int(row['maturity']) if not pd.isna(row['maturity']) else 2030
            current_year = datetime.now().year
            maturity_months = (maturity_year - current_year) * 12
            
            # Assign loan attributes based on existing infrastructure
            enhanced_deal = {
                'loan_id': f"DEAL_{idx+1:03d}",
                'borrower': row['company'],
                'sector': row['sector'],
                'principal': float(row['deal_size']),
                'rate_type': rate_type,
                'base_rate': base_rate,
                'spread': spread,
                'origination_date': '2025-01-01',  # Assume recent origination
                'maturity_months': max(maturity_months, 12),
                'io_months': 6 if 'term loan' in str(row['instrument']).lower() else 0,
                'amort_style': 'interest_only' if 'notes' in str(row['instrument']).lower() else 'annuity',
                'origination_rate': fixed_rate if rate_type == 'fixed' else 0.05 + spread,
                'current_spread': spread,
                'ltv': min(0.75, self.parse_leverage(row['leverage']) * 0.15),
                'dscr': max(1.2, 2.0 - self.parse_leverage(row['leverage']) * 0.1),
                'credit_rating': self.assign_credit_rating(row['leverage'], row['ebitda']),
                'prepay_penalty': 0.02 if 'secured' in str(row['instrument']).lower() else 0.015,
                'default_prob': self.calculate_default_probability(row['leverage'], row['ebitda'], row['sector']),
                'seniority': 'Senior' if 'senior' in str(row['instrument']).lower() else 'Subordinated',
                'collateral_type': self.determine_collateral_type(row['sector'], row['instrument']),
                'geography': 'US',  # Default assumption
                'vintage_yield': fixed_rate if rate_type == 'fixed' else 0.05 + spread,
                'coupon_freq': 'quarterly',
                'floating_reset_freq': 'quarterly' if rate_type == 'floating' else 'none',
                'instrument': row['instrument'],
                'currency': row['currency'],
                'ebitda': float(row['ebitda']),
                'leverage': self.parse_leverage(row['leverage']),
                'revenue_growth': row['rev'],
                'risks': row['risks'],
                # Add new fundamental analytics
                'credit_score': self.calculate_credit_score(
                    float(row['ebitda']), 
                    row['leverage'], 
                    row['rev'], 
                    row['sector']
                ),
                'risk_adjusted_spread': self.calculate_risk_adjusted_spread(
                    spread,
                    row['leverage'],
                    float(row['ebitda']),
                    row['instrument']
                )
            }
            
            enhanced_deals.append(enhanced_deal)
        
        # Write to enhanced_deal_start.txt
        with open('deals_data/enhanced_deal_start.txt', 'w') as f:
            f.write("# Enhanced Deal Start Portfolio (Private Loans)\n")
            f.write("# Format: loan_id|borrower|sector|principal|rate_type|base_rate|spread|origination_date|maturity_months|io_months|amort_style|origination_rate|current_spread|ltv|dscr|credit_rating|prepay_penalty|default_prob|seniority|collateral_type|geography|vintage_yield|coupon_freq|floating_reset_freq|instrument|currency|ebitda|leverage|revenue_growth|risks|credit_score|risk_adjusted_spread\n\n")
            
            for deal in enhanced_deals:
                line = "|".join([
                    str(deal['loan_id']), str(deal['borrower']), str(deal['sector']),
                    str(deal['principal']), str(deal['rate_type']), str(deal['base_rate']),
                    str(deal['spread']), str(deal['origination_date']), str(deal['maturity_months']),
                    str(deal['io_months']), str(deal['amort_style']), str(deal['origination_rate']),
                    str(deal['current_spread']), str(deal['ltv']), str(deal['dscr']),
                    str(deal['credit_rating']), str(deal['prepay_penalty']), str(deal['default_prob']),
                    str(deal['seniority']), str(deal['collateral_type']), str(deal['geography']),
                    str(deal['vintage_yield']), str(deal['coupon_freq']), str(deal['floating_reset_freq']),
                    str(deal['instrument']), str(deal['currency']), str(deal['ebitda']),
                    str(deal['leverage']), str(deal['revenue_growth']), str(deal['risks']),
                    str(deal['credit_score']), str(deal['risk_adjusted_spread'])
                ])
                f.write(line + "\n")
        
        print(f"Created deals_data/enhanced_deal_start.txt with {len(enhanced_deals)} loans")
        return enhanced_deals
    
    def create_enhanced_portfolio_txt(self):
        """Create enhanced portfolio text file with loan attributes"""
        print("Creating deals_data/enhanced_portfolio.txt...")
        
        enhanced_portfolio = []
        
        for idx, row in self.portfolio_df.iterrows():
            # Parse coupon information (assuming fixed rates for portfolio)
            coupon_rate = float(row['coupon_pct']) / 100 if not pd.isna(row['coupon_pct']) else 0.06
            
            # Calculate maturity months
            maturity_year = int(row['maturity_year']) if not pd.isna(row['maturity_year']) else 2030
            current_year = datetime.now().year
            maturity_months = (maturity_year - current_year) * 12
            
            # Estimate leverage and EBITDA based on market value and sector
            estimated_leverage = self.estimate_leverage_from_sector(row['sector'])
            estimated_ebitda = float(row['market_value']) / (estimated_leverage * 1000000)  # Rough estimation
            
            enhanced_loan = {
                'loan_id': f"PORT_{row['portfolio_id']:03d}",
                'borrower': row['company'],
                'sector': row['sector'],
                'principal': float(row['market_value']),
                'rate_type': 'fixed',  # Most portfolio items appear to be fixed
                'base_rate': 'none',
                'spread': 0.0,
                'origination_date': '2023-01-01',
                'maturity_months': max(maturity_months, 12),
                'io_months': 3 if 'notes' in str(row['instrument']).lower() else 0,
                'amort_style': 'interest_only' if 'notes' in str(row['instrument']).lower() else 'annuity',
                'origination_rate': coupon_rate,
                'current_spread': 0.0,
                'ltv': 0.70,
                'dscr': 1.40,
                'credit_rating': self.assign_credit_rating(estimated_leverage, estimated_ebitda),
                'prepay_penalty': 0.015,
                'default_prob': self.calculate_default_probability(estimated_leverage, estimated_ebitda, row['sector']),
                'seniority': 'Senior' if 'senior' in str(row['instrument']).lower() else 'Subordinated',
                'collateral_type': self.determine_collateral_type(row['sector'], row['instrument']),
                'geography': self.map_currency_to_geography(row['currency']),
                'vintage_yield': coupon_rate,
                'coupon_freq': 'semi_annual',
                'floating_reset_freq': 'none',
                'instrument': row['instrument'],
                'currency': row['currency'],
                'ebitda': estimated_ebitda,
                'leverage': estimated_leverage,
                'revenue_growth': '4% YoY',  # Default estimate
                'risks': 'Market risk; sector concentration',
                # Add new fundamental analytics
                'credit_score': self.calculate_credit_score(
                    estimated_ebitda,
                    estimated_leverage,
                    '4% YoY',
                    row['sector']
                ),
                'risk_adjusted_spread': self.calculate_risk_adjusted_spread(
                    0.0,  # No spread for fixed-rate portfolio bonds
                    estimated_leverage,
                    estimated_ebitda,
                    row['instrument']
                )
            }
            
            enhanced_portfolio.append(enhanced_loan)
        
        # Write to enhanced_portfolio.txt
        with open('deals_data/enhanced_portfolio.txt', 'w') as f:
            f.write("# Enhanced Portfolio (Private Loans)\n")
            f.write("# Format: loan_id|borrower|sector|principal|rate_type|base_rate|spread|origination_date|maturity_months|io_months|amort_style|origination_rate|current_spread|ltv|dscr|credit_rating|prepay_penalty|default_prob|seniority|collateral_type|geography|vintage_yield|coupon_freq|floating_reset_freq|instrument|currency|ebitda|leverage|revenue_growth|risks\n\n")
            
            for loan in enhanced_portfolio:
                line = "|".join([
                    str(loan['loan_id']), str(loan['borrower']), str(loan['sector']),
                    str(loan['principal']), str(loan['rate_type']), str(loan['base_rate']),
                    str(loan['spread']), str(loan['origination_date']), str(loan['maturity_months']),
                    str(loan['io_months']), str(loan['amort_style']), str(loan['origination_rate']),
                    str(loan['current_spread']), str(loan['ltv']), str(loan['dscr']),
                    str(loan['credit_rating']), str(loan['prepay_penalty']), str(loan['default_prob']),
                    str(loan['seniority']), str(loan['collateral_type']), str(loan['geography']),
                    str(loan['vintage_yield']), str(loan['coupon_freq']), str(loan['floating_reset_freq']),
                    str(loan['instrument']), str(loan['currency']), str(loan['ebitda']),
                    str(loan['leverage']), str(loan['revenue_growth']), str(loan['risks'])
                ])
                f.write(line + "\n")
        
        print(f"Created deals_data/enhanced_portfolio.txt with {len(enhanced_portfolio)} loans")
        return enhanced_portfolio
    
    def assign_credit_rating(self, leverage, ebitda):
        """Assign credit rating based on leverage and EBITDA"""
        if pd.isna(leverage) or pd.isna(ebitda):
            return 'B'
            
        leverage = self.parse_leverage(leverage)
        ebitda = float(ebitda)
        
        if leverage < 3.0 and ebitda > 200_000_000:
            return 'BB+'
        elif leverage < 4.0 and ebitda > 100_000_000:
            return 'BB'
        elif leverage < 5.0:
            return 'BB-'
        elif leverage < 6.0:
            return 'B+'
        else:
            return 'B-'
    
    def calculate_default_probability(self, leverage, ebitda, sector):
        """Calculate default probability based on leverage, EBITDA and sector"""
        if pd.isna(leverage) or pd.isna(ebitda):
            return 0.035
            
        leverage = self.parse_leverage(leverage)
        ebitda = float(ebitda)
        
        base_prob = 0.02
        
        # Leverage adjustment
        if leverage > 5.0:
            base_prob += 0.02
        elif leverage > 4.0:
            base_prob += 0.01
        
        # EBITDA size adjustment
        if ebitda < 100_000_000:
            base_prob += 0.015
        
        # Sector adjustment
        if 'energy' in str(sector).lower():
            base_prob += 0.01
        elif 'retail' in str(sector).lower():
            base_prob += 0.008
        elif 'healthcare' in str(sector).lower():
            base_prob -= 0.005
        
        return min(0.08, base_prob)  # Cap at 8%
    
    def calculate_credit_score(self, ebitda: float, leverage: float, revenue_growth: str, sector: str) -> float:
        """Credit scoring based on fundamental metrics"""
        try:
            # Parse growth rate
            growth_rate = float(revenue_growth.replace('% YoY', '').strip())
        except (ValueError, AttributeError):
            growth_rate = 5.0  # Default assumption
        
        # Parse leverage if it's a string
        leverage = self.parse_leverage(leverage)
        
        # Base score from leverage (higher leverage = lower score)
        leverage_score = max(0, 100 - (leverage - 2.0) * 20)
        
        # Growth adjustment (cap at 20 points)
        growth_adjustment = min(growth_rate * 2, 20)
        
        # Sector risk multiplier
        sector_risk = {
            'Consumer Staples': 0.9, 
            'Energy (Midstream)': 1.2, 
            'Healthcare Services': 1.1,
            'Transportation & Logistics': 1.15,
            'Industrial Services': 1.1
        }
        
        # Get sector multiplier (default to 1.0 if sector not found)
        sector_multiplier = sector_risk.get(sector, 1.0)
        
        return (leverage_score + growth_adjustment) * sector_multiplier

    def calculate_risk_adjusted_spread(self, base_spread: float, leverage: float, ebitda: float, instrument: str) -> float:
        """Pricing models incorporating fundamentals"""
        # Parse leverage if it's a string
        leverage = self.parse_leverage(leverage)
        
        # Leverage adjustment (25bp per turn above 3x)
        leverage_premium = max(0, (leverage - 3.0) * 25)  # basis points
        
        # EBITDA size adjustment (larger = lower spread)
        size_discount = 0
        if ebitda > 100_000_000:
            size_discount = min(25, np.log10(ebitda / 100_000_000) * 15)
        
        # Instrument seniority adjustment
        seniority_adj = {
            'Senior Secured': 0, 
            'Senior Unsecured': 50, 
            'Unitranche': 75,
            'Asset-Backed': 25
        }
        
        # Extract first two words for instrument matching
        instrument_key = ' '.join(instrument.split()[:2]) if isinstance(instrument, str) else 'Unknown'
        instrument_premium = seniority_adj.get(instrument_key, 100)
        
        # Convert basis points to decimal for spread calculation
        total_adjustment = (leverage_premium - size_discount + instrument_premium) / 10000
        
        return base_spread + total_adjustment
    
    def determine_collateral_type(self, sector, instrument):
        """Determine collateral type based on sector and instrument"""
        if 'asset-backed' in str(instrument).lower():
            return 'Asset Portfolio'
        elif 'secured' in str(instrument).lower():
            if 'energy' in str(sector).lower():
                return 'Oil&Gas Assets'
            elif 'transport' in str(sector).lower():
                return 'Fleet'
            elif 'industrial' in str(sector).lower():
                return 'Machinery'
            else:
                return 'General Assets'
        else:
            return 'Unsecured'
    
    def estimate_leverage_from_sector(self, sector):
        """Estimate leverage based on sector"""
        sector_leverage = {
            'industrials': 3.5,
            'retail': 4.2,
            'healthcare': 3.8,
            'materials': 3.9,
            'energy': 4.5,
            'technology': 3.2,
            'consumer': 4.0
        }
        
        for key, lev in sector_leverage.items():
            if key in str(sector).lower():
                return lev
        
        return 3.8  # Default
    
    def map_currency_to_geography(self, currency):
        """Map currency to geography"""
        currency_map = {
            'USD': 'US',
            'EUR': 'Europe', 
            'GBP': 'UK',
            'CAD': 'Canada',
            'AUD': 'Australia'
        }
        return currency_map.get(currency, 'US')
    
    def combine_portfolios(self):
        """Combine deal_start and portfolio data into unified format"""
        self._log_and_print("Combining portfolios...")
        
        # Create enhanced loan specs for both datasets
        deal_start_enhanced = self.create_enhanced_deal_start_txt()
        portfolio_enhanced = self.create_enhanced_portfolio_txt()
        
        # Convert to DataFrame for analysis
        all_loans = deal_start_enhanced + portfolio_enhanced
        self.combined_portfolio = pd.DataFrame(all_loans)
        
        self._log_and_print(f"Combined portfolio: {len(self.combined_portfolio)} total loans")
        self._log_and_print(f"Deal Start: {len(deal_start_enhanced)} loans")
        self._log_and_print(f"Portfolio: {len(portfolio_enhanced)} loans")
        
        return all_loans
    
    def generate_loan_cashflows(self):
        """Generate detailed cashflows for each loan using both base and stressed rates"""
        self._log_and_print("Generating detailed loan cashflows...")
        
        # Create cashflows directory
        cashflow_dir = f"cashflows_{self.timestamp}"
        os.makedirs(cashflow_dir, exist_ok=True)
        
        base_cashflows = {}
        stressed_cashflows = {}
        
        for idx, loan in self.combined_portfolio.iterrows():
            try:
                # Get credit-adjusted attributes for this loan
                loan_dict = loan.to_dict()
                credit_attrs = self.credit_integrator.get_credit_adjusted_attributes(loan_dict)
                
                self._log_and_print(f"🏦 Processing {loan.get('loan_id', f'LOAN_{idx}')} - Mode: {'Advanced' if self.advanced_credit_mode else 'Static'}")
                if self.advanced_credit_mode:
                    self._log_and_print(f"   📊 Credit Score: {loan_dict.get('credit_score', 'N/A'):.1f}")
                    self._log_and_print(f"   📈 Risk-Adj Spread: {credit_attrs.current_spread:.2%}")
                    self._log_and_print(f"   🎯 Adj Default Prob: {credit_attrs.default_prob:.2%}")
                    self._log_and_print(f"   🛡️ Recovery Rate: {credit_attrs.recovery_rate:.2%}")
                
                # Determine effective spread for cashflow generation
                effective_spread = credit_attrs.current_spread
                
                # For floating rate loans, adjust the origination rate to include risk-adjusted spread
                if loan['rate_type'] == 'floating' and self.advanced_credit_mode:
                    # Use risk-adjusted spread instead of current spread
                    base_rate = 0.05  # Assume 5% base floating rate
                    effective_rate = base_rate + effective_spread
                else:
                    effective_rate = loan['origination_rate']
                
                # Create LoanSpec for each loan
                loan_spec = LoanSpec(
                    principal=loan['principal'],
                    annual_interest_rate=effective_rate,
                    origination_date=loan['origination_date'],
                    term_months=loan['maturity_months'],
                    interest_only_months=loan['io_months'],
                    payment_frequency='M'  # Monthly payments
                )
                
                # Generate base case cashflows
                base_schedule = generate_loan_schedule(loan_spec)
                
                # For floating rate loans, adjust rates using forward curves
                if loan['rate_type'] == 'floating' and self.forward_rates:
                    base_schedule = self._adjust_floating_rate_cashflows(
                        base_schedule, loan, self.forward_rates
                    )
                
                # Apply credit risk adjustments to base cashflows if in advanced mode
                if self.advanced_credit_mode:
                    base_schedule = self.credit_integrator.apply_default_haircut_to_cashflows(
                        base_schedule, credit_attrs
                    )
                
                # Generate stressed cashflows using credit-based stress parameters
                stressed_schedule = base_schedule.copy()
                
                # Get credit-based or static stress parameters
                stress_params = self.credit_integrator.get_stress_testing_parameters(loan_dict)
                
                if loan['rate_type'] == 'floating' and self.stressed_forward_rates:
                    stressed_schedule = self._adjust_floating_rate_cashflows(
                        stressed_schedule, loan, self.stressed_forward_rates
                    )
                    # Apply additional credit-based spread stress
                    additional_spread_shock = stress_params['spread_shock'] - 0.01  # Remove base 100bps, add credit-based
                    if additional_spread_shock != 0:
                        stressed_schedule = self._apply_spread_shock(stressed_schedule, additional_spread_shock)
                else:
                    # Apply credit-based spread shock
                    spread_shock = stress_params['spread_shock']
                    stressed_schedule = self._apply_spread_shock(stressed_schedule, spread_shock)
                
                # Store cashflows
                base_cashflows[loan['loan_id']] = base_schedule
                stressed_cashflows[loan['loan_id']] = stressed_schedule
                
                # Export individual loan cashflows
                # Create subdirectories for base and stressed scenarios
                base_dir = os.path.join(cashflow_dir, 'base')
                stressed_dir = os.path.join(cashflow_dir, 'stressed')
                os.makedirs(base_dir, exist_ok=True)
                os.makedirs(stressed_dir, exist_ok=True)
                
                self._export_loan_cashflow(base_schedule, loan['loan_id'], base_dir, 'base')
                self._export_loan_cashflow(stressed_schedule, loan['loan_id'], stressed_dir, 'stressed')
                
            except Exception as e:
                print(f"Error generating cashflows for {loan['loan_id']}: {e}")
                continue
        
        # Export summary
        self._export_cashflow_summary(base_cashflows, stressed_cashflows, cashflow_dir)
        
        print(f"✅ Generated cashflows for {len(base_cashflows)} loans in: {cashflow_dir}")
        
        return base_cashflows, stressed_cashflows
    
    def _adjust_floating_rate_cashflows(self, schedule, loan, rate_lookup):
        """Adjust cashflows for floating rate loans using forward rate projections"""
        
        if not rate_lookup or loan['base_rate'] == 'none':
            return schedule
        
        adjusted_schedule = schedule.copy()
        base_rate_code = loan['base_rate']  # 'SOFR', 'EFFR', etc.
        spread = loan['current_spread']
        
        try:
            # Use the proper forward rate lookup function
            for idx, (date_index, row) in enumerate(schedule.iterrows()):
                
                # Convert index to date string for lookup
                if hasattr(date_index, 'strftime'):
                    date_str = date_index.strftime('%Y-%m-%d')
                else:
                    # Calculate date from month offset
                    from datetime import datetime, timedelta
                    base_date = datetime.now()
                    target_date = base_date + timedelta(days=30 * idx)
                    date_str = target_date.strftime('%Y-%m-%d')
                
                # Get the appropriate rate using proper historical/forward logic  
                from datetime import datetime
                current_date = datetime.now()
                cashflow_date = datetime.strptime(date_str, '%Y-%m-%d')
                
                if cashflow_date <= current_date:
                    # Historical dates: Always use base historical rates (even in stressed scenario)
                    if self.forward_rates:
                        try:
                            fred_df = pd.read_csv('fred_rates.csv', index_col=0, parse_dates=True)
                            forward_rate = self.forward_rates.get_forward_rate(base_rate_code, date_str, fred_df)
                            if forward_rate is None:
                                raise ValueError(f"No historical rate found for {base_rate_code} on {date_str}")
                        except Exception as e:
                            raise ValueError(f"Historical rate lookup failed for {base_rate_code} on {date_str}: {e}")
                    else:
                        raise ValueError(f"No forward rate lookup system available for historical rate {base_rate_code}")
                else:
                    # Future dates: Use appropriate rate lookup based on scenario
                    forward_rate = rate_lookup.get_forward_rate(base_rate_code, date_str)
                    if forward_rate is None:
                        raise ValueError(f"No forward rate found for {base_rate_code} on {date_str}")
                
                if forward_rate is not None:
                    new_all_in_rate = forward_rate + spread
                    
                    # Update interest payment based on new rate
                    outstanding = row.get('outstanding_balance', row.get('outstanding_end', loan['principal']))
                    if outstanding > 0:
                        new_interest = outstanding * new_all_in_rate / 12
                        adjusted_schedule.iloc[idx, adjusted_schedule.columns.get_loc('interest_payment')] = new_interest
                        
                        # Also update the rate tracking columns if they exist
                        if 'base_rate' in adjusted_schedule.columns:
                            adjusted_schedule.iloc[idx, adjusted_schedule.columns.get_loc('base_rate')] = forward_rate
                        if 'all_in_rate' in adjusted_schedule.columns:
                            adjusted_schedule.iloc[idx, adjusted_schedule.columns.get_loc('all_in_rate')] = new_all_in_rate
            
        except Exception as e:
            print(f"Warning: Could not adjust floating rates for {loan['loan_id']}: {e}")
        
        return adjusted_schedule
    
    def _apply_spread_shock(self, schedule, spread_shock):
        """Apply spread shock to loan cashflows"""
        
        shocked_schedule = schedule.copy()
        
        try:
            # Apply spread shock to interest payments
            if 'interest_payment' in schedule.columns:
                for month in range(len(schedule)):
                    if 'outstanding_balance' in schedule.columns:
                        outstanding = schedule.iloc[month]['outstanding_balance']
                        additional_interest = outstanding * spread_shock / 12
                        shocked_schedule.iloc[month]['interest_payment'] += additional_interest
            
        except Exception as e:
            print(f"Warning: Could not apply spread shock: {e}")
        
        return shocked_schedule
    
    def _export_loan_cashflow(self, schedule, loan_id, output_dir, scenario):
        """Export individual loan cashflow to file in professional format"""
        
        filename = f"{loan_id}_cashflows.txt"
        filepath = os.path.join(output_dir, filename)
        
        try:
            # Get loan details from combined portfolio with error handling
            loan_matches = self.combined_portfolio[self.combined_portfolio['loan_id'] == loan_id]
            
            if loan_matches.empty:
                print(f"Error: No loan found with ID {loan_id}")
                return
                
            loan_details = loan_matches.iloc[0]
            
            print(f"DEBUG: Exporting {loan_id} - Principal: ${loan_details.get('principal', 0):,.0f}")
            
        except Exception as e:
            print(f"Error accessing loan details for {loan_id}: {e}")
            return
        
        try:
            with open(filepath, 'w') as f:
                # Header information
                f.write("=" * 80 + "\n")
                f.write(f"LOAN CASHFLOW ANALYSIS: {loan_id}\n")
                f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 80 + "\n")
                f.write(f"Borrower: {loan_details['borrower']}\n")
                f.write(f"Sector: {loan_details['sector']} | Geography: {loan_details['geography']}\n")
                f.write(f"Principal: ${loan_details['principal']:,.0f}\n")
                f.write(f"Rate Type: {loan_details['rate_type']}\n")
                
                if loan_details['rate_type'] == 'floating':
                    # Get current rate from FRED data instead of hardcoding
                    try:
                        fred_df = pd.read_csv('fred_rates.csv', index_col=0, parse_dates=True)
                        current_rate = self.forward_rates.get_forward_rate(loan_details['base_rate'], datetime.now().strftime('%Y-%m-%d'), fred_df)
                        if current_rate is None:
                            raise ValueError(f"Could not find current rate for {loan_details['base_rate']}")
                        base_rate_val = current_rate * 100
                    except Exception as e:
                        raise ValueError(f"Failed to get current {loan_details['base_rate']} rate: {e}")
                    
                    f.write(f"Base Rate: {loan_details['base_rate']} ({base_rate_val:.2f}%)\n")
                    f.write(f"Spread: {loan_details['current_spread']*100:.2f}%\n")
                    f.write(f"All-in Rate: {(current_rate + loan_details['current_spread'])*100:.2f}%\n")
                else:
                    f.write(f"Fixed Rate: {loan_details['origination_rate']*100:.2f}%\n")
                
                f.write(f"Credit Rating: {loan_details['credit_rating']}\n")
                f.write(f"Default Probability: {loan_details['default_prob']*100:.2f}%\n")
                f.write(f"LTV: {loan_details['ltv']*100:.1f}% | DSCR: {loan_details['dscr']:.2f}x\n")
                f.write(f"Prepayment Penalty: {loan_details['prepay_penalty']*100:.2f}%\n")
                f.write(f"Seniority: {loan_details['seniority']}\n")
                f.write(f"Collateral: {loan_details['collateral_type']}\n")
                f.write(f"\n")
                
                # Calculate portfolio metrics
                total_interest = schedule.get('interest_payment', pd.Series([0])).sum() if 'interest_payment' in schedule.columns else 0
                total_principal = loan_details['principal']
                irr_base = loan_details['origination_rate'] * 1.1
                irr_adjusted = irr_base * 0.8
                moic = 1 + (total_interest / total_principal) if total_principal > 0 else 1.0
                
                f.write("PORTFOLIO METRICS:\n")
                f.write(f"IRR (base): {irr_base*100:.2f}%\n")
                f.write(f"IRR (adjusted): {irr_adjusted*100:.2f}%\n")
                f.write(f"MOIC: {moic:.3f}x\n")
                f.write("\n")
                
                # Detailed cashflow schedule
                f.write("=" * 80 + "\n")
                f.write("DETAILED CASHFLOW SCHEDULE\n")
                f.write("=" * 120 + "\n")
                f.write("        Date |  Outstanding |  BaseRate |   Spread |    AllIn |   Interest |  Principal |  PrepayAmt |  PrepayPen | DefaultLoss |   Recovery |      NetCF\n")
                f.write("-" * 120 + "\n")
                
                # Process schedule data
                prepay_amt = loan_details['principal'] * loan_details.get('prepay_speed', 0.05) / 12
                prepay_penalty = prepay_amt * loan_details['prepay_penalty']
                default_loss = loan_details['principal'] * loan_details['default_prob'] / 12
                recovery = default_loss * (1 - loan_details.get('lgd', 0.4))
                
                for idx, row in schedule.iterrows():
                    if hasattr(idx, 'strftime'):
                        date_str = idx.strftime('%Y-%m-%d')
                    else:
                        date_str = str(idx)[:10]
                    
                    outstanding = row.get('outstanding_balance', row.get('outstanding_end', loan_details['principal']))
                    if outstanding < 0:
                        outstanding = loan_details['principal']
                    
                    # Calculate rates using proper forward rate lookup
                    if loan_details['rate_type'] == 'floating':
                        base_rate_code = loan_details['base_rate']
                        
                        # Determine if date is historical or future
                        current_date = datetime.now()
                        cashflow_date = datetime.strptime(date_str, '%Y-%m-%d')
                        
                        if cashflow_date <= current_date:
                            # Historical: use base rates with FRED data
                            if self.forward_rates:
                                try:
                                    # Load FRED rates for historical lookup
                                    fred_df = pd.read_csv('fred_rates.csv', index_col=0, parse_dates=True)
                                    actual_base_rate = self.forward_rates.get_forward_rate(base_rate_code, date_str, fred_df)
                                    if actual_base_rate is None:
                                        raise ValueError(f"No historical rate found for {base_rate_code} on {date_str}")
                                    base_rate = actual_base_rate
                                except Exception as e:
                                    raise ValueError(f"Historical rate lookup failed for {base_rate_code} on {date_str}: {e}")
                            else:
                                raise ValueError(f"No forward rate lookup system available for historical rate {base_rate_code}")
                        else:
                            # Future: use stressed rates for stressed scenario
                            if scenario == 'stressed' and self.stressed_forward_rates:
                                try:
                                    actual_base_rate = self.stressed_forward_rates.get_forward_rate(base_rate_code, date_str)
                                    if actual_base_rate is None:
                                        raise ValueError(f"No stressed forward rate found for {base_rate_code} on {date_str}")
                                    base_rate = actual_base_rate
                                except Exception as e:
                                    raise ValueError(f"Stressed forward rate lookup failed for {base_rate_code} on {date_str}: {e}")
                            elif self.forward_rates:
                                try:
                                    actual_base_rate = self.forward_rates.get_forward_rate(base_rate_code, date_str)
                                    if actual_base_rate is None:
                                        raise ValueError(f"No forward rate found for {base_rate_code} on {date_str}")
                                    base_rate = actual_base_rate
                                except Exception as e:
                                    raise ValueError(f"Forward rate lookup failed for {base_rate_code} on {date_str}: {e}")
                            else:
                                raise ValueError(f"No forward rate lookup system available for {base_rate_code}")
                        
                        spread = loan_details['current_spread']
                        if scenario == 'stressed' and loan_details['ebitda'] < 200_000_000:
                            spread += 0.01
                            
                    elif scenario == 'stressed':
                        base_rate = max(0.001, loan_details['origination_rate'] - 0.005)
                        spread = 0.0
                    else:
                        base_rate = loan_details['origination_rate']
                        spread = 0.0
                    
                    all_in_rate = base_rate + spread
                    interest_payment = row.get('interest_payment', outstanding * all_in_rate / 12) if outstanding > 0 else 0
                    principal_payment = row.get('principal_payment', 0)
                    
                    # Net cashflow calculation
                    if outstanding < 0:
                        net_cf = outstanding + prepay_penalty + recovery - default_loss
                    else:
                        net_cf = interest_payment + principal_payment + prepay_penalty + recovery - prepay_amt - default_loss
                    
                    f.write(f"  {date_str} | ${outstanding:>10,.0f} | {base_rate:>8.3f} | {spread:>8.3f} | {all_in_rate:>8.3f} | "
                           f"${interest_payment:>9,.0f} | ${principal_payment:>10,.0f} | ${prepay_amt:>9,.0f} | "
                           f"${prepay_penalty:>9,.0f} | ${default_loss:>10,.0f} | ${recovery:>9,.0f} | ${net_cf:>10,.0f}\n")
                
                # Footer
                f.write("=" * 120 + "\n")
                f.write(f"Scenario: {scenario.upper()} - ")
                if scenario == 'stressed':
                    f.write("Applied stress: +100bps spread (EBITDA<$200M), -50bps risk-free rates\n")
                else:
                    f.write("Base case scenario with current market rates\n")
                f.write("=" * 120 + "\n")
                
        except Exception as e:
            print(f"Error writing cashflow file for {loan_id}: {e}")
            import traceback
            traceback.print_exc()
    
    def _export_cashflow_summary(self, base_cashflows, stressed_cashflows, output_dir):
        """Export cashflow summary across all loans"""
        
        summary_file = os.path.join(output_dir, 'CASHFLOW_SUMMARY.txt')
        
        with open(summary_file, 'w') as f:
            f.write("PORTFOLIO CASHFLOW SUMMARY\n")
            f.write("=" * 60 + "\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Total Loans: {len(base_cashflows)}\n\n")
            
            f.write("CASHFLOW TOTALS BY SCENARIO:\n")
            f.write("-" * 40 + "\n")
            
            # Calculate totals
            base_total_interest = sum(cf['interest_payment'].sum() 
                                    for cf in base_cashflows.values() 
                                    if 'interest_payment' in cf.columns)
            
            base_total_principal = sum(cf['principal_payment'].sum() 
                                     for cf in base_cashflows.values()
                                     if 'principal_payment' in cf.columns)
            
            stressed_total_interest = sum(cf['interest_payment'].sum() 
                                        for cf in stressed_cashflows.values()
                                        if 'interest_payment' in cf.columns)
            
            stressed_total_principal = sum(cf['principal_payment'].sum() 
                                         for cf in stressed_cashflows.values()
                                         if 'principal_payment' in cf.columns)
            
            f.write(f"Base Case:\n")
            f.write(f"  Total Interest: ${base_total_interest:,.0f}\n")
            f.write(f"  Total Principal: ${base_total_principal:,.0f}\n")
            f.write(f"  Total Cashflow: ${base_total_interest + base_total_principal:,.0f}\n\n")
            
            f.write(f"Stressed Case:\n")
            f.write(f"  Total Interest: ${stressed_total_interest:,.0f}\n")
            f.write(f"  Total Principal: ${stressed_total_principal:,.0f}\n")
            f.write(f"  Total Cashflow: ${stressed_total_interest + stressed_total_principal:,.0f}\n\n")
            
            f.write(f"Impact Analysis:\n")
            f.write(f"  Interest Impact: ${stressed_total_interest - base_total_interest:,.0f}\n")
            f.write(f"  Total Impact: ${(stressed_total_interest + stressed_total_principal) - (base_total_interest + base_total_principal):,.0f}\n")
    
    def calculate_effective_duration(self):
        """Calculate effective duration for private loans using loan-specific methodology"""
        print("Calculating effective duration for private loans...")
        
        def loan_duration_proxy(row):
            """Calculate duration proxy for private loans"""
            years_to_maturity = row['maturity_months'] / 12
            
            if row['rate_type'] == 'floating':
                # Floating rate loans have lower duration
                reset_freq_factor = {
                    'monthly': 0.08,
                    'quarterly': 0.25, 
                    'semi_annual': 0.5,
                    'annual': 1.0
                }.get(row['floating_reset_freq'], 0.25)
                return reset_freq_factor
            else:
                # Fixed rate loans - duration depends on coupon and maturity
                coupon = row['origination_rate']
                if coupon <= 0:
                    coupon = 0.05  # Default 5%
                
                # Modified duration approximation
                modified_duration = years_to_maturity / (1 + coupon/2)
                
                # Adjust for amortization style
                if row['amort_style'] == 'interest_only':
                    return modified_duration * 0.9  # IO loans have higher duration
                else:
                    return modified_duration * 0.7  # Amortizing loans have lower duration
        
        self.combined_portfolio['effective_duration'] = self.combined_portfolio.apply(loan_duration_proxy, axis=1)
        
        print(f"Average effective duration: {self.combined_portfolio['effective_duration'].mean():.2f} years")
    
    def calculate_weighted_metrics(self):
        """Calculate weighted portfolio metrics"""
        print("Calculating weighted portfolio metrics...")
        
        total_principal = self.combined_portfolio['principal'].sum()
        weights = self.combined_portfolio['principal'] / total_principal
        
        # Weighted average coupon (use origination_rate as proxy)
        weighted_coupon = (weights * self.combined_portfolio['origination_rate']).sum()
        
        # Weighted average maturity
        weighted_maturity = (weights * self.combined_portfolio['maturity_months']).sum() / 12
        
        # Weighted average duration
        weighted_duration = (weights * self.combined_portfolio['effective_duration']).sum()
        
        # Weighted average leverage and DSCR
        weighted_leverage = (weights * self.combined_portfolio['leverage']).sum()
        weighted_dscr = (weights * self.combined_portfolio['dscr']).sum()
        
        self.metrics = {
            'weighted_average_coupon': weighted_coupon,
            'weighted_average_maturity': weighted_maturity,
            'weighted_average_duration': weighted_duration,
            'weighted_average_leverage': weighted_leverage,
            'weighted_average_dscr': weighted_dscr,
            'total_principal': total_principal
        }
        
        print(f"\nPortfolio Metrics:")
        print(f"Weighted Average Coupon: {weighted_coupon:.2%}")
        print(f"Weighted Average Maturity: {weighted_maturity:.1f} years")
        print(f"Weighted Average Duration: {weighted_duration:.1f}")
        print(f"Weighted Average Leverage: {weighted_leverage:.1f}x")
        print(f"Weighted Average DSCR: {weighted_dscr:.1f}x")
        print(f"Total Principal: ${total_principal:,.0f}")
    
    def calculate_exposures(self):
        """Calculate sector and currency exposures"""
        print("Calculating sector and currency exposures...")
        
        total_principal = self.combined_portfolio['principal'].sum()
        
        # Sector exposures
        sector_exposure = self.combined_portfolio.groupby('sector')['principal'].sum()
        self.sector_exposure_pct = (sector_exposure / total_principal * 100).sort_values(ascending=False)
        
        # Currency exposures
        currency_exposure = self.combined_portfolio.groupby('currency')['principal'].sum()
        self.currency_exposure_pct = (currency_exposure / total_principal * 100).sort_values(ascending=False)
        
        # Geography exposures
        geography_exposure = self.combined_portfolio.groupby('geography')['principal'].sum()
        self.geography_exposure_pct = (geography_exposure / total_principal * 100).sort_values(ascending=False)
        
        print(f"\nTop Sector Exposures:")
        for sector, pct in self.sector_exposure_pct.head(5).items():
            print(f"  {sector}: {pct:.1f}%")
    
    def identify_top_issuers(self):
        """Identify top 5 issuers by exposure"""
        print("Identifying top issuers...")
        
        total_principal = self.combined_portfolio['principal'].sum()
        issuer_exposure = self.combined_portfolio.groupby('borrower')['principal'].sum()
        self.top_issuers = (issuer_exposure / total_principal * 100).sort_values(ascending=False).head(5)
        
        print(f"\nTop 5 Issuers by Exposure:")
        for issuer, pct in self.top_issuers.items():
            print(f"  {issuer}: {pct:.1f}%")
    
    def apply_loan_filter(self, portfolio_df, filter_config):
        """Apply flexible filters to select loans for stress testing
        
        Args:
            portfolio_df: DataFrame with loan portfolio
            filter_config: Dictionary with filter criteria
                Examples:
                - {'ebitda': {'operator': '<', 'value': 200_000_000}}
                - {'market_value': {'operator': '>', 'value': 100_000_000}}
                - {'sector': {'operator': 'in', 'value': ['Energy', 'Healthcare']}}
                - {'leverage': {'operator': '>=', 'value': 4.0}}
        
        Returns:
            Boolean mask for filtered loans
        """
        if not filter_config:
            return pd.Series([True] * len(portfolio_df))
        
        mask = pd.Series([True] * len(portfolio_df))
        
        for field, criteria in filter_config.items():
            if field not in portfolio_df.columns:
                print(f"Warning: Field '{field}' not found in portfolio")
                continue
                
            operator = criteria.get('operator', '==')
            value = criteria.get('value')
            
            field_values = portfolio_df[field]
            
            if operator == '<':
                field_mask = field_values < value
            elif operator == '<=':
                field_mask = field_values <= value
            elif operator == '>':
                field_mask = field_values > value
            elif operator == '>=':
                field_mask = field_values >= value
            elif operator == '==':
                field_mask = field_values == value
            elif operator == '!=':
                field_mask = field_values != value
            elif operator == 'in':
                field_mask = field_values.isin(value if isinstance(value, list) else [value])
            elif operator == 'not_in':
                field_mask = ~field_values.isin(value if isinstance(value, list) else [value])
            elif operator == 'contains':
                field_mask = field_values.astype(str).str.contains(str(value), case=False, na=False)
            else:
                print(f"Warning: Unknown operator '{operator}' for field '{field}'")
                continue
            
            mask = mask & field_mask
        
        return mask

    def perform_flexible_stress_test(self, stress_config):
        """Perform flexible stress test with configurable scenarios
        
        Args:
            stress_config: Dictionary with stress test configuration
                {
                    'name': 'Spread Shock Test',
                    'shock_type': 'spread',  # 'spread', 'rate', 'both'
                    'shock_value': 0.01,     # 100 bps
                    'filters': {             # Optional filters
                        'ebitda': {'operator': '<', 'value': 200_000_000}
                    },
                    'apply_to_all': False    # If True, ignores filters and applies to all
                }
        """
        print(f"\nFlexible Stress Test: {stress_config.get('name', 'Custom Test')}")
        
        portfolio = self.combined_portfolio.copy()
        shock_value = stress_config.get('shock_value', 0.01)
        shock_type = stress_config.get('shock_type', 'spread')
        apply_to_all = stress_config.get('apply_to_all', False)
        
        # Determine which loans to stress
        if apply_to_all:
            stress_mask = pd.Series([True] * len(portfolio))
            print(f"Applying {shock_value*10000:.0f}bps {shock_type} shock to ALL {len(portfolio)} loans")
        else:
            filters = stress_config.get('filters', {})
            stress_mask = self.apply_loan_filter(portfolio, filters)
            print(f"Applying {shock_value*10000:.0f}bps {shock_type} shock to {stress_mask.sum()} out of {len(portfolio)} loans")
            
            # Print filter details
            if filters:
                print("Filter criteria:")
                for field, criteria in filters.items():
                    operator = criteria.get('operator', '==')
                    value = criteria.get('value')
                    print(f"  • {field} {operator} {value}")
        
        # Calculate impact
        if shock_type in ['spread', 'both']:
            spread_impact = self._duration_based_impact(portfolio, stress_mask, shock_value)
        else:
            spread_impact = 0.0
            
        # Calculate affected principal
        affected_principal = portfolio.loc[stress_mask, 'principal'].sum()
        total_principal = portfolio['principal'].sum()
        
        # Print results
        print(f"Affected principal: ${affected_principal:,.0f} ({affected_principal/total_principal:.1%} of portfolio)")
        print(f"Portfolio impact: {spread_impact:.2%}")
        
        # Breakdown by loan characteristics
        if stress_mask.sum() > 0:
            affected_loans = portfolio.loc[stress_mask]
            print(f"\nBreakdown of affected loans:")
            
            # By sector
            sector_breakdown = affected_loans.groupby('sector').agg({
                'principal': 'sum',
                'loan_id': 'count'
            }).sort_values('principal', ascending=False)
            
            print("  By Sector:")
            for sector, row in sector_breakdown.head().iterrows():
                pct = row['principal'] / affected_principal * 100
                print(f"    • {sector}: ${row['principal']:,.0f} ({pct:.1f}%), {row['loan_id']} loans")
            
            # By credit rating
            if 'credit_rating' in affected_loans.columns:
                rating_breakdown = affected_loans.groupby('credit_rating')['principal'].sum().sort_values(ascending=False)
                print("  By Credit Rating:")
                for rating, principal in rating_breakdown.items():
                    pct = principal / affected_principal * 100
                    print(f"    • {rating}: ${principal:,.0f} ({pct:.1f}%)")
        
        return {
            'name': stress_config.get('name', 'Custom Test'),
            'impact': spread_impact,
            'affected_loans': stress_mask.sum(),
            'affected_principal': affected_principal,
            'affected_percentage': affected_principal / total_principal,
            'shock_value': shock_value,
            'shock_type': shock_type
        }

    def calculate_aggregated_portfolio_impact(self):
        """Calculate stress impact by comparing IRR and MOIC metrics"""
        if not (hasattr(self, 'base_cashflows') and hasattr(self, 'stressed_cashflows')):
            print("Warning: No cashflow data available for IRR/MOIC comparison")
            return None
            
        print("Calculating IRR and MOIC-based portfolio stress impact...")
        
        individual_metrics = []
        total_principal = self.combined_portfolio['principal'].sum()
        
        # Calculate IRR and MOIC for each loan in both scenarios
        for loan_id in self.base_cashflows.keys():
            if loan_id in self.stressed_cashflows:
                loan_details = self.combined_portfolio[self.combined_portfolio['loan_id'] == loan_id].iloc[0]
                weight = loan_details['principal'] / total_principal
                
                # Calculate base scenario metrics
                base_cf = self.base_cashflows[loan_id]
                base_irr_raw, base_moic = self._calculate_loan_irr_moic(base_cf, loan_details['principal'])
                base_irr_adjusted = base_irr_raw * 0.85  # Risk adjustment factor
                
                # Calculate stressed scenario metrics  
                stressed_cf = self.stressed_cashflows[loan_id]
                stressed_irr_raw, stressed_moic = self._calculate_loan_irr_moic(stressed_cf, loan_details['principal'])
                stressed_irr_adjusted = stressed_irr_raw * 0.85  # Risk adjustment factor
                
                loan_metrics = {
                    'loan_id': loan_id,
                    'borrower': loan_details['borrower'],
                    'principal': loan_details['principal'],
                    'weight': weight,
                    'rate_type': loan_details['rate_type'],
                    'ebitda': loan_details['ebitda'],
                    'base_irr_raw': base_irr_raw,
                    'base_irr_adjusted': base_irr_adjusted,
                    'base_moic': base_moic,
                    'stressed_irr_raw': stressed_irr_raw,
                    'stressed_irr_adjusted': stressed_irr_adjusted,
                    'stressed_moic': stressed_moic,
                    'irr_raw_impact': stressed_irr_raw - base_irr_raw,
                    'irr_adjusted_impact': stressed_irr_adjusted - base_irr_adjusted,
                    'moic_impact': stressed_moic - base_moic
                }
                
                individual_metrics.append(loan_metrics)
        
        if not individual_metrics:
            print("No loan metrics calculated")
            return None
            
        # Calculate weighted portfolio metrics
        portfolio_base_irr_raw = sum(m['base_irr_raw'] * m['weight'] for m in individual_metrics)
        portfolio_base_irr_adj = sum(m['base_irr_adjusted'] * m['weight'] for m in individual_metrics)
        portfolio_base_moic = sum(m['base_moic'] * m['weight'] for m in individual_metrics)
        
        portfolio_stressed_irr_raw = sum(m['stressed_irr_raw'] * m['weight'] for m in individual_metrics)
        portfolio_stressed_irr_adj = sum(m['stressed_irr_adjusted'] * m['weight'] for m in individual_metrics)
        portfolio_stressed_moic = sum(m['stressed_moic'] * m['weight'] for m in individual_metrics)
        
        # Print detailed analysis
        print(f"\nIRR & MOIC STRESS ANALYSIS:")
        print(f"=" * 80)
        
        print("INDIVIDUAL LOAN IMPACT:")
        print(f"{'Loan ID':<12} {'Borrower':<20} {'Principal':<12} {'Type':<8} {'EBITDA<200M':<10} {'IRR Δ':<8} {'MOIC Δ':<8}")
        print("-" * 80)
        
        for m in individual_metrics:
            ebitda_flag = "YES" if m['ebitda'] < 200_000_000 else "NO"
            borrower_short = m['borrower'][:18] + '..' if len(m['borrower']) > 18 else m['borrower']
            print(f"{m['loan_id']:<12} {borrower_short:<20} ${m['principal']/1e6:>8.0f}M {m['rate_type']:<8} "
                  f"{ebitda_flag:<10} {m['irr_adjusted_impact']:>+6.1%} {m['moic_impact']:>+6.3f}")
        
        print(f"\nPORTFOLIO-LEVEL WEIGHTED METRICS:")
        print(f"Base IRR (Raw): {portfolio_base_irr_raw:.2%} → Stressed: {portfolio_stressed_irr_raw:.2%} (Δ: {portfolio_stressed_irr_raw - portfolio_base_irr_raw:+.2%})")
        print(f"Base IRR (Adj): {portfolio_base_irr_adj:.2%} → Stressed: {portfolio_stressed_irr_adj:.2%} (Δ: {portfolio_stressed_irr_adj - portfolio_base_irr_adj:+.2%})")
        print(f"Base MOIC: {portfolio_base_moic:.3f}x → Stressed: {portfolio_stressed_moic:.3f}x (Δ: {portfolio_stressed_moic - portfolio_base_moic:+.3f}x)")
        
        # Analyze by loan type
        floating_loans = [m for m in individual_metrics if m['rate_type'] == 'floating']
        fixed_loans = [m for m in individual_metrics if m['rate_type'] == 'fixed']
        
        if floating_loans:
            floating_weight = sum(m['weight'] for m in floating_loans)
            floating_irr_impact = sum(m['irr_adjusted_impact'] * m['weight'] for m in floating_loans) / floating_weight
            floating_moic_impact = sum(m['moic_impact'] * m['weight'] for m in floating_loans) / floating_weight
            
            print(f"\nFLOATING RATE LOANS (-50bps SOFR impact):")
            print(f"Count: {len(floating_loans)} loans, Weight: {floating_weight:.1%}")
            print(f"Avg IRR Impact: {floating_irr_impact:+.2%}")
            print(f"Avg MOIC Impact: {floating_moic_impact:+.3f}x")
        
        if fixed_loans:
            fixed_weight = sum(m['weight'] for m in fixed_loans)
            fixed_irr_impact = sum(m['irr_adjusted_impact'] * m['weight'] for m in fixed_loans) / fixed_weight
            fixed_moic_impact = sum(m['moic_impact'] * m['weight'] for m in fixed_loans) / fixed_weight
            
            print(f"\nFIXED RATE LOANS (spread stress only):")
            print(f"Count: {len(fixed_loans)} loans, Weight: {fixed_weight:.1%}")
            print(f"Avg IRR Impact: {fixed_irr_impact:+.2%}")
            print(f"Avg MOIC Impact: {fixed_moic_impact:+.3f}x")
        
        return {
            'individual_metrics': individual_metrics,
            'portfolio_base_irr_adj': portfolio_base_irr_adj,
            'portfolio_stressed_irr_adj': portfolio_stressed_irr_adj,
            'portfolio_base_moic': portfolio_base_moic,
            'portfolio_stressed_moic': portfolio_stressed_moic,
            'irr_impact': portfolio_stressed_irr_adj - portfolio_base_irr_adj,
            'moic_impact': portfolio_stressed_moic - portfolio_base_moic
        }
    
    def _calculate_loan_irr_moic(self, cashflow_schedule, principal):
        """Calculate IRR and MOIC for a loan cashflow schedule"""
        try:
            # Extract cashflows for IRR calculation
            cashflows = []
            
            for idx, row in cashflow_schedule.iterrows():
                total_cf = row.get('total_cashflow', 0)
                if pd.isna(total_cf) or total_cf == 0:
                    # Use interest + principal payments
                    interest = row.get('interest_payment', 0)
                    principal_pmt = row.get('principal_payment', 0)
                    total_cf = interest + principal_pmt
                
                # First cashflow is negative (funding)
                if idx == cashflow_schedule.index[0]:
                    total_cf = -principal
                
                cashflows.append((idx, total_cf))
            
            # Calculate simple IRR approximation
            total_inflows = sum(max(0, cf) for _, cf in cashflows)
            total_outflows = sum(-min(0, cf) for _, cf in cashflows)
            
            if total_outflows > 0:
                # Simplified IRR calculation
                years = len(cashflows) / 12  # Monthly payments
                moic = total_inflows / total_outflows
                irr = (moic ** (1/years)) - 1 if years > 0 else 0
            else:
                irr = 0
                moic = 1
            
            return irr, moic
            
        except Exception as e:
            print(f"Error calculating IRR/MOIC: {e}")
            return 0.05, 1.0  # Conservative fallback

    def perform_comprehensive_stress_tests(self):
        """Perform comprehensive stress tests with multiple scenarios"""
        print("=" * 80)
        print("COMPREHENSIVE STRESS TESTING WITH FLEXIBLE FILTERING")
        print("=" * 80)
        
        # Define stress test scenarios
        stress_scenarios = [
            {
                'name': 'Spread Shock - All Loans',
                'shock_type': 'spread',
                'shock_value': 0.01,  # 100 bps
                'apply_to_all': True
            },
            {
                'name': 'Spread Shock - EBITDA < $200M',
                'shock_type': 'spread', 
                'shock_value': 0.01,  # 100 bps
                'filters': {
                    'ebitda': {'operator': '<', 'value': 200_000_000}
                }
            },
            {
                'name': 'Spread Shock - High Leverage (≥4.0x)',
                'shock_type': 'spread',
                'shock_value': 0.015,  # 150 bps
                'filters': {
                    'leverage': {'operator': '>=', 'value': 4.0}
                }
            },
            {
                'name': 'Spread Shock - Large Loans (≥$500M)',
                'shock_type': 'spread',
                'shock_value': 0.005,  # 50 bps
                'filters': {
                    'principal': {'operator': '>=', 'value': 500_000_000}
                }
            },
            {
                'name': 'Spread Shock - Energy Sector',
                'shock_type': 'spread',
                'shock_value': 0.02,  # 200 bps
                'filters': {
                    'sector': {'operator': 'contains', 'value': 'Energy'}
                }
            },
            {
                'name': 'Rate Shock - All Loans (-50bps)',
                'shock_type': 'rate',
                'shock_value': -0.005,  # -50 bps
                'apply_to_all': True
            }
        ]
        
        # Execute stress tests
        stress_results = []
        for scenario in stress_scenarios:
            result = self.perform_flexible_stress_test(scenario)
            stress_results.append(result)
        
        # Summary table
        print("\n" + "=" * 80)
        print("STRESS TEST SUMMARY")
        print("=" * 80)
        print(f"{'Scenario':<35} {'Shock':<10} {'Affected':<10} {'Principal %':<12} {'Impact':<10}")
        print("-" * 80)
        
        for result in stress_results:
            shock_str = f"{result['shock_value']*10000:+.0f}bps"
            affected_str = f"{result['affected_loans']}/{len(self.combined_portfolio)}"
            principal_pct_str = f"{result['affected_percentage']:.1%}"
            impact_str = f"{result['impact']:+.2%}"
            
            print(f"{result['name'][:34]:<35} {shock_str:<10} {affected_str:<10} {principal_pct_str:<12} {impact_str:<10}")
        
        # Store results
        self.comprehensive_stress_results = stress_results
        
        # Calculate traditional stress results for backward compatibility
        ebitda_filter_result = next((r for r in stress_results if 'EBITDA < $200M' in r['name']), None)
        rate_shock_result = next((r for r in stress_results if 'Rate Shock' in r['name']), None)
        
        if ebitda_filter_result and rate_shock_result:
            combined_impact = ebitda_filter_result['impact'] + rate_shock_result['impact']
            
            self.stress_results = {
                'spread_widening_impact': ebitda_filter_result['impact'],
                'rf_tightening_impact': rate_shock_result['impact'],
                'combined_impact': combined_impact,
                'low_ebitda_count': ebitda_filter_result['affected_loans'],
                'low_ebitda_principal': ebitda_filter_result['affected_principal']
            }
        
        print(f"\n📊 Most severe impact: {max(stress_results, key=lambda x: abs(x['impact']))['name']}")
        print(f"   Impact: {max(stress_results, key=lambda x: abs(x['impact']))['impact']:+.2%}")
        
        return stress_results
    
    def _calculate_cashflow_pv(self, cashflows, discount_rate):
        """Calculate present value of cashflow series"""
        pv = 0
        for period, cf in enumerate(cashflows):
            if pd.notna(cf) and cf != 0:
                pv += cf / ((1 + discount_rate/12) ** period)
        return pv
    
    def _duration_based_impact(self, portfolio, mask, shock):
        """Calculate portfolio impact using duration approximation"""
        
        duration_impact = np.zeros(len(portfolio))
        
        for idx, row in portfolio.iterrows():
            if mask.iloc[idx]:
                if row['rate_type'] == 'floating':
                    # Floating rate loans have different sensitivity
                    duration_impact[idx] = -row['effective_duration'] * shock * 0.6
                else:
                    # Fixed rate loans
                    duration_impact[idx] = -row['effective_duration'] * shock
        
        portfolio_impact = (duration_impact * portfolio['principal']).sum() / portfolio['principal'].sum()
        return portfolio_impact
    
    def create_plots(self):
        """Create comprehensive portfolio analytics plots"""
        print("Creating plots...")
        
        plt.style.use('seaborn-v0_8')
        fig = plt.figure(figsize=(20, 16))
        
        # Create subplot layout
        gs = fig.add_gridspec(3, 3, hspace=0.3, wspace=0.3)
        
        # Plot 1: Sector weights (% MV)
        ax1 = fig.add_subplot(gs[0, 0])
        colors = plt.cm.Set3(np.linspace(0, 1, len(self.sector_exposure_pct)))
        bars1 = ax1.bar(range(len(self.sector_exposure_pct)), self.sector_exposure_pct.values, color=colors)
        ax1.set_title('Sector Weights (% of Principal)', fontsize=12, fontweight='bold')
        ax1.set_ylabel('Percentage (%)')
        ax1.set_xticks(range(len(self.sector_exposure_pct)))
        ax1.set_xticklabels(self.sector_exposure_pct.index, rotation=45, ha='right', fontsize=9)
        ax1.grid(True, alpha=0.3)
        
        # Add value labels on bars
        for bar, val in zip(bars1, self.sector_exposure_pct.values):
            height = bar.get_height()
            ax1.text(bar.get_x() + bar.get_width()/2., height + 0.5,
                    f'{val:.1f}%', ha='center', va='bottom', fontsize=8)
        
        # Plot 2: Duration by sector
        ax2 = fig.add_subplot(gs[0, 1])
        sector_duration = self.combined_portfolio.groupby('sector').agg({
            'effective_duration': 'mean',
            'principal': 'sum'
        }).sort_values('effective_duration', ascending=False)
        
        bars2 = ax2.bar(range(len(sector_duration)), sector_duration['effective_duration'], 
                       color='lightcoral', alpha=0.7)
        ax2.set_title('Average Effective Duration by Sector', fontsize=12, fontweight='bold')
        ax2.set_ylabel('Duration (years)')
        ax2.set_xticks(range(len(sector_duration)))
        ax2.set_xticklabels(sector_duration.index, rotation=45, ha='right', fontsize=9)
        ax2.grid(True, alpha=0.3)
        
        # Plot 3: Top issuers exposure
        ax3 = fig.add_subplot(gs[0, 2])
        bars3 = ax3.barh(range(len(self.top_issuers)), self.top_issuers.values, 
                        color='lightgreen', alpha=0.7)
        ax3.set_title('Top 5 Issuers by Exposure', fontsize=12, fontweight='bold')
        ax3.set_xlabel('Percentage (%)')
        ax3.set_yticks(range(len(self.top_issuers)))
        ax3.set_yticklabels([name[:25] + '...' if len(name) > 25 else name for name in self.top_issuers.index], fontsize=9)
        ax3.grid(True, alpha=0.3)
        
        # Plot 4: Maturity profile distribution
        ax4 = fig.add_subplot(gs[1, 0])
        maturity_years = self.combined_portfolio['maturity_months'] / 12
        ax4.hist(maturity_years, bins=15, color='teal', alpha=0.7, edgecolor='black')
        ax4.set_title('Maturity Profile Distribution', fontsize=12, fontweight='bold')
        ax4.set_xlabel('Years to Maturity')
        ax4.set_ylabel('Number of Loans')
        ax4.grid(True, alpha=0.3)
        
        # Plot 5: Leverage by sector
        ax5 = fig.add_subplot(gs[1, 1])
        sector_leverage = self.combined_portfolio.groupby('sector')['leverage'].mean().sort_values(ascending=False)
        bars5 = ax5.bar(range(len(sector_leverage)), sector_leverage.values, 
                       color='orange', alpha=0.7)
        ax5.set_title('Average Leverage by Sector', fontsize=12, fontweight='bold')
        ax5.set_ylabel('Leverage (x)')
        ax5.set_xticks(range(len(sector_leverage)))
        ax5.set_xticklabels(sector_leverage.index, rotation=45, ha='right', fontsize=9)
        ax5.grid(True, alpha=0.3)
        
        # Plot 6: Currency exposure
        ax6 = fig.add_subplot(gs[1, 2])
        bars6 = ax6.pie(self.currency_exposure_pct.values, labels=self.currency_exposure_pct.index, 
                       autopct='%1.1f%%', startangle=90)
        ax6.set_title('Currency Exposure Distribution', fontsize=12, fontweight='bold')
        
        # Plot 7: Credit rating distribution
        ax7 = fig.add_subplot(gs[2, 0])
        rating_dist = self.combined_portfolio['credit_rating'].value_counts()
        bars7 = ax7.bar(rating_dist.index, rating_dist.values, color='purple', alpha=0.7)
        ax7.set_title('Credit Rating Distribution', fontsize=12, fontweight='bold')
        ax7.set_ylabel('Number of Loans')
        ax7.set_xlabel('Credit Rating')
        ax7.grid(True, alpha=0.3)
        
        # Plot 8: EBITDA distribution for stress analysis
        ax8 = fig.add_subplot(gs[2, 1])
        ebitda_values = self.combined_portfolio['ebitda'] / 1_000_000  # Convert to millions
        colors_ebitda = ['red' if x < 200 else 'green' for x in ebitda_values]
        ax8.scatter(range(len(ebitda_values)), ebitda_values, c=colors_ebitda, alpha=0.7, s=100)
        ax8.axhline(y=200, color='red', linestyle='--', alpha=0.7, label='$200M Threshold')
        ax8.set_title('EBITDA Distribution (Stress Threshold)', fontsize=12, fontweight='bold')
        ax8.set_xlabel('Loan Index')
        ax8.set_ylabel('EBITDA ($M)')
        ax8.legend()
        ax8.grid(True, alpha=0.3)
        
        # Plot 9: Rate type distribution
        ax9 = fig.add_subplot(gs[2, 2])
        rate_type_dist = self.combined_portfolio['rate_type'].value_counts()
        bars9 = ax9.pie(rate_type_dist.values, labels=rate_type_dist.index, 
                       autopct='%1.1f%%', startangle=90)
        ax9.set_title('Interest Rate Type Distribution', fontsize=12, fontweight='bold')
        
        plt.suptitle('Private Loan Portfolio Analytics Dashboard\nWith Forward Rate Integration & Stress Testing', 
                    fontsize=16, fontweight='bold')
        plt.savefig(f'private_loan_portfolio_analytics_{self.timestamp}.png', dpi=300, bbox_inches='tight')
        plt.show()
    
    def apply_prepayment_modeling(self):
        """Apply prepayment modeling to floating rate loans"""
        print("Applying prepayment modeling...")
        
        # Simple prepayment model based on rate environment and loan characteristics
        self.combined_portfolio['prepay_speed'] = 0.0
        
        for idx, row in self.combined_portfolio.iterrows():
            base_speed = 0.05  # 5% annual prepayment
            
            # Adjust for rate type
            if row['rate_type'] == 'floating':
                base_speed *= 1.5  # Floating rate loans prepay faster
            
            # Adjust for credit quality
            if row['credit_rating'] in ['BB+', 'BB', 'BB-']:
                base_speed *= 1.2
            elif row['credit_rating'] in ['B+', 'B', 'B-']:
                base_speed *= 0.8
            
            # Adjust for sector
            if 'technology' in row['sector'].lower():
                base_speed *= 1.3
            elif 'energy' in row['sector'].lower():
                base_speed *= 0.7
            
            self.combined_portfolio.loc[idx, 'prepay_speed'] = min(0.25, base_speed)
    
    def apply_default_modeling(self):
        """Apply credit default modeling"""
        print("Applying credit default modeling...")
        
        # Calculate expected losses based on PD, LGD, and EAD
        self.combined_portfolio['lgd'] = 0.4  # 40% loss given default (secured loans)
        self.combined_portfolio.loc[
            self.combined_portfolio['seniority'] == 'Subordinated', 'lgd'
        ] = 0.6  # 60% for subordinated
        
        self.combined_portfolio['ead'] = self.combined_portfolio['principal']  # Exposure at default
        
        # Expected loss = PD * LGD * EAD
        self.combined_portfolio['expected_loss'] = (
            self.combined_portfolio['default_prob'] * 
            self.combined_portfolio['lgd'] * 
            self.combined_portfolio['ead']
        )
        
        total_expected_loss = self.combined_portfolio['expected_loss'].sum()
        total_principal = self.combined_portfolio['principal'].sum()
        
        print(f"Total Expected Loss: ${total_expected_loss:,.0f}")
        print(f"Expected Loss Rate: {total_expected_loss / total_principal:.2%}")
    
    def generate_report(self):
        """Generate comprehensive analytics report"""
        print("\n" + "="*100)
        print("PRIVATE LOAN PORTFOLIO INTEGRATION & ANALYTICS REPORT")
        print("WITH FORWARD RATE INTEGRATION & STRESS TESTING")
        print("="*100)
        
        print(f"\nREPORT DATE: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"ANALYSIS TIMESTAMP: {self.timestamp}")
        print(f"TOTAL LOANS: {len(self.combined_portfolio)}")
        print(f"DEAL START LOANS: {len(self.deal_start_df) if self.deal_start_df is not None else 0}")
        print(f"PORTFOLIO LOANS: {len(self.portfolio_df) if self.portfolio_df is not None else 0}")
        
        print("\n" + "-"*60)
        print("WEIGHTED PORTFOLIO METRICS")
        print("-"*60)
        print(f"Weighted Average Coupon: {self.metrics['weighted_average_coupon']:.2%}")
        print(f"Weighted Average Maturity: {self.metrics['weighted_average_maturity']:.1f} years")
        print(f"Weighted Average Duration: {self.metrics['weighted_average_duration']:.1f}")
        print(f"Weighted Average Leverage: {self.metrics['weighted_average_leverage']:.1f}x")
        print(f"Weighted Average DSCR: {self.metrics['weighted_average_dscr']:.1f}x")
        print(f"Total Principal: ${self.metrics['total_principal']:,.0f}")
        
        print("\n" + "-"*60)
        print("SECTOR EXPOSURES (% of Principal)")
        print("-"*60)
        for sector, pct in self.sector_exposure_pct.items():
            print(f"{sector:.<35} {pct:>6.1f}%")
        
        print("\n" + "-"*60)
        print("CURRENCY EXPOSURES (% of Principal)")
        print("-"*60)
        for currency, pct in self.currency_exposure_pct.items():
            print(f"{currency:.<35} {pct:>6.1f}%")
        
        print("\n" + "-"*60)
        print("TOP 5 ISSUERS BY EXPOSURE")
        print("-"*60)
        for i, (issuer, pct) in enumerate(self.top_issuers.items(), 1):
            display_name = issuer[:45] + '...' if len(issuer) > 45 else issuer
            print(f"{i}. {display_name:.<40} {pct:>6.1f}%")
        
        print("\n" + "-"*60)
        print("STRESS TEST RESULTS (WITH FORWARD RATE INTEGRATION)")
        print("-"*60)
        
        # Show aggregated portfolio stress impact first
        if self.aggregated_impact:
            # Calculate direct cashflow-based impact
            base_total_interest = sum(
                cf['interest_payment'].sum() for cf in self.base_cashflows.values() 
                if 'interest_payment' in cf.columns
            )
            stressed_total_interest = sum(
                cf['interest_payment'].sum() for cf in self.stressed_cashflows.values()
                if 'interest_payment' in cf.columns
            )
            
            interest_impact = (stressed_total_interest - base_total_interest) / base_total_interest if base_total_interest > 0 else 0
            portfolio_principal = self.combined_portfolio['principal'].sum()
            
            print("AGGREGATED PORTFOLIO STRESS COMPARISON:")
            print(f"Base Total Interest Income: ${base_total_interest:,.0f}")
            print(f"Stressed Total Interest Income: ${stressed_total_interest:,.0f}")
            print(f"Interest Income Impact: {interest_impact:+.2%}")
            print(f"Delta Interest Income: ${stressed_total_interest - base_total_interest:+,.0f}")
            print(f"Impact as % of Principal: {(stressed_total_interest - base_total_interest)/portfolio_principal:+.2%}")
            print("")
            
            # Show breakdown by stress components
            floating_count = len([loan for loan in self.combined_portfolio.itertuples() if loan.rate_type == 'floating'])
            low_ebitda_count = len([loan for loan in self.combined_portfolio.itertuples() if loan.ebitda < 200_000_000])
            
            print("STRESS COMPONENT BREAKDOWN:")
            print(f"Loans affected by -50bps SOFR stress: {floating_count} floating rate loans")
            print(f"Loans affected by +100bps spread stress: {low_ebitda_count} loans with EBITDA < $200M") 
            print(f"Combined impact: Both stresses applied where applicable")
            print("")
            
            # Add detailed IRR/MOIC analysis to main report
            if 'individual_metrics' in self.aggregated_impact:
                print("INDIVIDUAL LOAN IRR & MOIC IMPACT:")
                print(f"{'Loan ID':<12} {'Borrower':<20} {'Principal':<12} {'Type':<8} {'EBITDA<200M':<10} {'IRR Δ':<8} {'MOIC Δ':<8}")
                print("-" * 80)
                
                metrics = self.aggregated_impact['individual_metrics']
                for m in metrics:
                    ebitda_flag = "YES" if m['ebitda'] < 200_000_000 else "NO"
                    borrower_short = m['borrower'][:18] + '..' if len(m['borrower']) > 18 else m['borrower']
                    print(f"{m['loan_id']:<12} {borrower_short:<20} ${m['principal']/1e6:>8.0f}M {m['rate_type']:<8} "
                          f"{ebitda_flag:<10} {m['irr_adjusted_impact']:>+6.1%} {m['moic_impact']:>+6.3f}")
                
                print("")
                print("PORTFOLIO-LEVEL WEIGHTED IRR & MOIC IMPACT:")
                print(f"Base IRR (Adjusted): {self.aggregated_impact['portfolio_base_irr_adj']:.2%}")
                print(f"Stressed IRR (Adjusted): {self.aggregated_impact['portfolio_stressed_irr_adj']:.2%}")
                print(f"Portfolio IRR Impact: {self.aggregated_impact['irr_impact']:+.2%}")
                print(f"")
                print(f"Base MOIC: {self.aggregated_impact['portfolio_base_moic']:.3f}x")
                print(f"Stressed MOIC: {self.aggregated_impact['portfolio_stressed_moic']:.3f}x")
                print(f"Portfolio MOIC Impact: {self.aggregated_impact['moic_impact']:+.3f}x")
                print("")
        
        print("INDIVIDUAL STRESS COMPONENT ANALYSIS:")
        print(f"Loans with EBITDA < $200M: {self.stress_results['low_ebitda_count']}")
        print(f"Principal affected (EBITDA < $200M): ${self.stress_results['low_ebitda_principal']:,.0f}")
        print(f"Spread Widening (+100bps) Impact: {self.stress_results['spread_widening_impact']:>8.2%}")
        print(f"Risk-Free Tightening (-50bps) Impact: {self.stress_results['rf_tightening_impact']:>8.2%}")
        print(f"Combined Stress Test Impact: {self.stress_results['combined_impact']:>8.2%}")
        
        # Credit metrics
        if hasattr(self, 'combined_portfolio') and 'expected_loss' in self.combined_portfolio.columns:
            total_expected_loss = self.combined_portfolio['expected_loss'].sum()
            total_principal = self.combined_portfolio['principal'].sum()
            
            print("\n" + "-"*60)
            print("CREDIT RISK METRICS (NOT STRESSED)")
            print("-"*60)
            print(f"Total Expected Loss: ${total_expected_loss:,.0f}")
            print(f"Expected Loss Rate: {total_expected_loss / total_principal:.2%}")
            print(f"Average Default Probability: {self.combined_portfolio['default_prob'].mean():.2%}")
            print(f"Average LGD: {self.combined_portfolio['lgd'].mean():.1%}")
        
        print("\n" + "-"*60)
        print("EFFECTIVE DURATION METHODOLOGY")
        print("-"*60)
        print("• Floating Rate Loans: Duration = Reset Frequency Factor")
        print("  - Monthly reset: 0.08 years")
        print("  - Quarterly reset: 0.25 years") 
        print("  - Semi-annual reset: 0.5 years")
        print("• Fixed Rate Loans: Modified Duration with Amortization Adjustment")
        print("  - Interest-Only: Duration * 0.9")
        print("  - Amortizing: Duration * 0.7")
        print("• Incorporates loan-specific characteristics vs. bond duration")
        
        print("\n" + "-"*60)
        print("FORWARD CURVE INTEGRATION")
        print("-"*60)
        if self.forward_rates:
            print("✓ Forward rate lookup system active")
            print("✓ SOFR, EFFR, DGS2, DGS5, DGS10, DGS30 projections available")
            print("✓ Monthly granularity for floating rate resets")
            print("✓ Stressed forward rates generated for scenario analysis")
        else:
            print("⚠ Forward rate system not available - using static rates")
        
        print("\n" + "="*100)
        print("FILES CREATED:")
        print("• deals_data/enhanced_deal_start.txt - Enhanced deal start data with loan attributes")
        print("• deals_data/enhanced_portfolio.txt - Enhanced portfolio data with loan attributes") 
        print(f"• private_loan_portfolio_analytics_{self.timestamp}.png - Comprehensive analytics dashboard")
        print(f"• forward_rates_stressed_{self.timestamp}/ - Stressed forward rate projections")
        print(f"• cashflows_{self.timestamp}/ - Detailed loan cashflows (base + stressed scenarios)")
        print("="*100)
    
    def ensure_infrastructure_exists(self):
        """Ensure all required infrastructure exists, regenerating if necessary"""
        print("="*80)
        print("INFRASTRUCTURE VERIFICATION & REGENERATION")
        print("="*80)
        
        # Check and regenerate forward rates if needed
        forward_dir = self.regenerate_forward_rates_if_missing()
        
        # Check and regenerate existing cashflows if needed (for reference)
        cashflow_dir = self.regenerate_cashflows_if_missing()
        
        print("✅ Infrastructure verification completed")
        return forward_dir, cashflow_dir

    def process_pdf_memos_and_create_csvs(self):
        """Process PDF memos with priority to unstructured (AI) extraction"""
        
        # Check CSV files with priority to unstructured
        unstructured_csv = "deals_data/deal_start_unstructured.csv"
        structured_csv = "deals_data/deal_start_structured.csv"
        
        unstructured_exists = os.path.exists(unstructured_csv) and os.path.getsize(unstructured_csv) > 100
        
        # If unstructured CSV exists and is good, skip all PDF processing
        if unstructured_exists:
            print("✅ Unstructured deal data already exists and is not empty")
            print(f"✅ Found: {unstructured_csv}")
            print("✅ Skipping PDF processing - using AI-extracted data")
            
            # Also check if structured exists, but don't process if unstructured is available
            structured_exists = os.path.exists(structured_csv) and os.path.getsize(structured_csv) > 100
            if structured_exists:
                print(f"✅ Found: {structured_csv} (available as alternative)")
            
            return 0, 0
        
        print("="*80)
        print("PDF MEMO PROCESSING & DATA EXTRACTION")
        print("="*80)
        print("Priority: AI-powered unstructured extraction")
        
        unstructured_results = {}
        structured_results = {}
        
        # Process unstructured PDFs with AI first (higher priority)
        print("Processing unstructured PDFs with AI (primary method)...")
        unstructured_results = self._process_unstructured_pdfs()
        
        if unstructured_results:
            self._save_memo_csv(unstructured_results, unstructured_csv) 
            print("✅ Created deals_data/deal_start_unstructured.csv from AI-extracted PDF memos")
            print("✅ AI extraction successful - skipping structured PDF processing")
            return len(structured_results), len(unstructured_results)
        else:
            print("⚠ AI extraction failed - falling back to structured PDF processing...")
        
        # Only process structured PDFs if unstructured fails
        structured_exists = os.path.exists(structured_csv) and os.path.getsize(structured_csv) > 100
        
        if not structured_exists:
            print("Processing structured PDFs (fallback method)...")
            structured_results = self._process_structured_pdfs()
            
            if structured_results:
                self._save_memo_csv(structured_results, structured_csv)
                print("✅ Created deals_data/deal_start_structured.csv from structured PDF memos")
            else:
                print("❌ Both AI and structured PDF extraction failed")
        else:
            print("✅ deals_data/deal_start_structured.csv already exists")
            
        return len(structured_results), len(unstructured_results)

    def _process_structured_pdfs(self):
        """Process structured PDF memos"""
        structured_dir = Path("memos_structured")
        results = {}
        
        if not structured_dir.exists():
            print("No memos_structured directory found")
            return results
            
        pdf_files = list(structured_dir.glob("*.pdf"))
        print(f"Found {len(pdf_files)} structured PDF files")
        
        for pdf_file in pdf_files:
            print(f"Processing structured: {pdf_file.name}")
            try:
                import pdfplumber
                with pdfplumber.open(str(pdf_file)) as pdf:
                    text = ""
                    for page in pdf.pages:
                        text += page.extract_text() + "\n"
                
                data = self._extract_structured_data(text, pdf_file.name)
                if data:
                    results[pdf_file.name] = data
                    print(f"✅ Extracted data from {pdf_file.name}")
            except Exception as e:
                print(f"Error processing {pdf_file.name}: {e}")
                
        return results
    
    def _process_unstructured_pdfs(self):
        """Process unstructured PDF memos using AI"""
        unstructured_dir = Path("memos_unstructured")
        results = {}
        
        if not unstructured_dir.exists():
            print("No memos_unstructured directory found")
            return results
            
        pdf_files = list(unstructured_dir.glob("*.pdf"))
        print(f"Found {len(pdf_files)} unstructured PDF files")
        
        for pdf_file in pdf_files:
            print(f"Processing unstructured: {pdf_file.name}")
            try:
                import pdfplumber
                with pdfplumber.open(str(pdf_file)) as pdf:
                    text = ""
                    for page in pdf.pages:
                        text += page.extract_text() + "\n"
                
                data = self._extract_ai_data(text)
                if data:
                    results[pdf_file.name] = data
                    print(f"✅ AI extracted data from {pdf_file.name}")
            except Exception as e:
                print(f"Error processing {pdf_file.name}: {e}")
                
        return results
    
    def _extract_structured_data(self, text, filename):
        """Extract data from structured PDF memo using pattern matching"""
        company = filename.replace('_structured.pdf', '').replace('_', ' ').title()
        
        # Extract key fields using regex patterns
        patterns = {
            'sector': r'Sector:\s*([^\n]+)',
            'instrument': r'Instrument:\s*([^\n]+)', 
            'currency': r'Currency:\s*([^\n]+)',
            'deal_size': r'Deal Size:\s*([^\n]+)',
            'coupon': r'Coupon:\s*([^\n]+)',
            'maturity': r'Maturity:\s*([^\n]+)', 
            'ebitda': r'EBITDA.*?:\s*([^\n]+)',
            'leverage': r'Leverage:\s*([^\n]+)',
            'revenue_growth': r'Revenue Growth:\s*([^\n]+)',
            'risks': r'Key Risks:\s*([^\n]+(?:\n[^\n]*)*)'
        }
        
        data = {'company': company}
        for field, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            data[field] = match.group(1).strip() if match else ""
            
        return data
    
    def _extract_ai_data(self, text):
        """Extract data from unstructured PDF using OpenAI"""
        try:
            import openai
            from config import OPENAI_API_KEY
            
            openai.api_key = OPENAI_API_KEY
            
            prompt = f"""
Please analyze the following investment memorandum PDF content and extract these fields:

PDF Content:
{text}

Extract these fields (use "None" if not found):

1. Company Name
2. Sector  
3. Instrument Type
4. Currency
5. Deal Size (convert $450m to $450,000,000 format)
6. Interest Rate/Coupon (include SOFR + bps or fixed %)
7. Maturity Year
8. EBITDA
9. Leverage Ratio  
10. Revenue Growth
11. Key Risks

Return JSON format:
{{
    "company": "company name",
    "sector": "sector", 
    "instrument": "instrument",
    "currency": "currency",
    "deal_size": "deal size", 
    "coupon": "interest rate",
    "maturity": "maturity year",
    "ebitda": "EBITDA",
    "leverage": "leverage",
    "revenue_growth": "revenue growth", 
    "risks": "key risks"
}}
"""
            
            response = openai.ChatCompletion.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            
            ai_response = response.choices[0].message.content
            
            # Parse JSON response
            cleaned_response = ai_response.strip()
            if cleaned_response.startswith('```json'):
                cleaned_response = cleaned_response[7:]
            if cleaned_response.endswith('```'):
                cleaned_response = cleaned_response[:-3]
                
            data = json.loads(cleaned_response.strip())
            return data
            
        except Exception as e:
            print(f"AI extraction error: {e}")
            return None
    
    def _parse_currency_to_number(self, currency_str):
        """Convert currency string to number"""
        if not currency_str or not isinstance(currency_str, str):
            return 0.0
            
        currency_str = currency_str.strip()
        currency_str = re.sub(r'[$€£¥₹₽₩₪₨₦₡₱₴₸₼₾₿]', '', currency_str)
        
        # Handle abbreviations
        multipliers = {
            'k': 1000, 'K': 1000, 'm': 1000000, 'M': 1000000,
            'b': 1000000000, 'B': 1000000000, 't': 1000000000000, 'T': 1000000000000
        }
        
        for suffix, multiplier in multipliers.items():
            if currency_str.endswith(suffix):
                try:
                    number = float(currency_str[:-1].replace(',', ''))
                    return number * multiplier
                except ValueError:
                    return 0.0
        
        try:
            return float(currency_str.replace(',', ''))
        except ValueError:
            return 0.0
    
    def _save_memo_csv(self, data, output_path):
        """Save memo data to CSV file"""
        csv_data = []
        
        for filename, file_data in data.items():
            company = filename.replace('_structured.pdf', '').replace('_unstructured.pdf', '').replace('_', ' ').title()
            
            if "error" in file_data:
                row = {
                    'company': company,
                    'sector': 'Error', 
                    'instrument': 'Error',
                    'currency': 'Error',
                    'deal_size': 0,
                    'coupon': 'Error',
                    'maturity': 'Error', 
                    'ebitda': 0,
                    'leverage': 'Error',
                    'rev': 'Error',
                    'risks': file_data.get('error', 'Unknown Error')
                }
            else:
                row = {
                    'company': file_data.get('company', company),
                    'sector': file_data.get('sector', ''),
                    'instrument': file_data.get('instrument', ''),
                    'currency': file_data.get('currency', ''),
                    'deal_size': self._parse_currency_to_number(str(file_data.get('deal_size', ''))),
                    'coupon': file_data.get('coupon', ''),
                    'maturity': file_data.get('maturity', ''),
                    'ebitda': self._parse_currency_to_number(str(file_data.get('ebitda', ''))),
                    'leverage': file_data.get('leverage', ''),
                    'rev': file_data.get('revenue_growth', ''),
                    'risks': file_data.get('risks', '')
                }
                
            csv_data.append(row)
        
        df = pd.DataFrame(csv_data)
        df.to_csv(output_path, index=False)
        print(f"Data saved to {output_path} - {len(csv_data)} records")
        print("Data preview:")
        print(df.head())

    def run_analysis(self):
        """Run the complete private loan portfolio analysis"""
        # Setup logging
        self.logger, log_filepath = setup_deals_logging()
        
        self.logger.info("="*100)
        self.logger.info("PRIVATE LOAN PORTFOLIO INTEGRATION & ANALYTICS")
        self.logger.info("WITH FORWARD RATE INTEGRATION & COMPREHENSIVE STRESS TESTING")
        self.logger.info("="*100)
        
        # Log credit risk mode
        self._log_and_print(f"🔧 Credit Risk Mode: {'Advanced' if self.advanced_credit_mode else 'Static'}")
        if self.advanced_credit_mode:
            self._log_and_print("   ✅ Using credit scores, risk-adjusted spreads, and fundamental-based default/recovery modeling")
        else:
            self._log_and_print("   ✅ Using static default probabilities and recovery assumptions")
        
        # Process PDF memos to create CSV files
        self.process_pdf_memos_and_create_csvs()
        
        # Ensure infrastructure exists
        self.ensure_infrastructure_exists()
        
        # Load data
        if not self.load_csv_data():
            print("Error: Could not load required CSV files")
            return None
            
        # Setup forward rates
        self.setup_forward_rates()
        
        # Create stressed forward rates
        self.create_stressed_forward_rates()
        
        # Create enhanced data and combine
        self.combine_portfolios()
        
        # Generate detailed cashflows
        self.base_cashflows, self.stressed_cashflows = self.generate_loan_cashflows()
        
        # Perform analysis
        self.calculate_effective_duration()
        self.calculate_weighted_metrics()
        self.calculate_exposures()
        self.identify_top_issuers()
        
        # Calculate aggregated stress impact from cashflows
        self.aggregated_impact = self.calculate_aggregated_portfolio_impact()
        
        self.perform_comprehensive_stress_tests()
        
        # Apply advanced modeling
        self.apply_prepayment_modeling()
        self.apply_default_modeling()
        
        # Generate outputs
        self.create_plots()
        self.generate_report()
        
        # Generate credit risk report
        self.generate_credit_risk_report()
        
        self._log_and_print("\n✅ Private loan portfolio analysis completed successfully!")
        self._log_and_print("✅ Enhanced loan files created with full attribute coverage")
        self._log_and_print("✅ Forward rate integration active with stressed scenarios")
        self._log_and_print("✅ Detailed cashflow analysis exported")
        self._log_and_print("✅ Comprehensive stress testing completed")
        self._log_and_print("✅ Professional analytics dashboard generated")
        self._log_and_print(f"✅ Credit Risk Mode: {'Advanced' if self.advanced_credit_mode else 'Static'}")
        
        return self
    
    def generate_credit_risk_report(self):
        """Generate credit risk analysis report comparing static vs advanced mode"""
        self._log_and_print("\n" + "="*80)
        self._log_and_print("CREDIT RISK ANALYSIS REPORT")
        self._log_and_print("="*80)
        
        if self.combined_portfolio is None or self.combined_portfolio.empty:
            self._log_and_print("❌ No portfolio data available for credit risk analysis")
            return
        
        # Generate comparison report
        comparison_report = self.credit_integrator.generate_credit_summary_report(self.combined_portfolio)
        
        self._log_and_print(f"📊 Analysis Mode: {'Advanced Credit Modeling' if self.advanced_credit_mode else 'Static Assumptions'}")
        self._log_and_print("\n📈 PORTFOLIO CREDIT METRICS COMPARISON:")
        self._log_and_print("-" * 50)
        
        static_metrics = comparison_report['mode_comparison']['static_mode']
        advanced_metrics = comparison_report['mode_comparison']['advanced_mode']
        impact_analysis = comparison_report['impact_analysis']
        
        print(f"{'Metric':<25} {'Static Mode':<15} {'Advanced Mode':<15} {'Change %':<12}")
        print("-" * 67)
        print(f"{'Avg Default Prob':<25} {static_metrics['avg_default_prob']:<14.2%} {advanced_metrics['avg_default_prob']:<14.2%} {impact_analysis['default_prob_change']:>11.1f}%")
        print(f"{'Avg Recovery Rate':<25} {static_metrics['avg_recovery_rate']:<14.2%} {advanced_metrics['avg_recovery_rate']:<14.2%} {impact_analysis['recovery_rate_change']:>11.1f}%")
        print(f"{'Avg Spread':<25} {static_metrics['avg_spread']:<14.2%} {advanced_metrics['avg_spread']:<14.2%} {impact_analysis['spread_change']:>11.1f}%")
        print(f"{'Total Expected Loss':<25} ${static_metrics['total_expected_loss']:>13,.0f} ${advanced_metrics['total_expected_loss']:>13,.0f} {impact_analysis['expected_loss_change']:>11.1f}%")
        
        print(f"\n🎯 KEY INSIGHTS:")
        if self.advanced_credit_mode:
            print("   ✅ Using credit scores and risk-adjusted spreads for cashflow generation")
            print("   ✅ Default probabilities adjusted based on fundamental credit analysis")
            print("   ✅ Recovery rates calibrated to borrower credit profiles")
            print("   ✅ Stress testing parameters customized by credit quality")
        else:
            print("   📋 Using static default probabilities and recovery assumptions")
            print("   📋 Uniform stress testing parameters across all loans")
            print("   📋 No fundamental credit analysis integration")
        
        # Export detailed report to file
        report_filename = f"credit_risk_analysis_{self.timestamp}.txt"
        with open(report_filename, 'w') as f:
            f.write("CREDIT RISK ANALYSIS REPORT\n")
            f.write("="*50 + "\n\n")
            f.write(f"Analysis Mode: {'Advanced Credit Modeling' if self.advanced_credit_mode else 'Static Assumptions'}\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            
            f.write("PORTFOLIO CREDIT METRICS:\n")
            f.write("-"*30 + "\n")
            f.write(f"Average Default Probability: {advanced_metrics['avg_default_prob']:.2%}\n")
            f.write(f"Average Recovery Rate: {advanced_metrics['avg_recovery_rate']:.2%}\n")
            f.write(f"Average Spread: {advanced_metrics['avg_spread']:.2%}\n")
            f.write(f"Total Expected Loss: ${advanced_metrics['total_expected_loss']:,.0f}\n\n")
            
            f.write("INDIVIDUAL LOAN ANALYSIS:\n")
            f.write("-"*30 + "\n")
            for idx, loan in self.combined_portfolio.iterrows():
                loan_dict = loan.to_dict()
                credit_attrs = self.credit_integrator.get_credit_adjusted_attributes(loan_dict)
                f.write(f"\nLoan: {loan.get('loan_id', f'LOAN_{idx}')}\n")
                f.write(f"  Borrower: {loan.get('borrower', 'N/A')}\n")
                f.write(f"  Credit Score: {loan_dict.get('credit_score', 'N/A'):.1f}\n")
                f.write(f"  Default Probability: {credit_attrs.default_prob:.2%}\n")
                f.write(f"  Recovery Rate: {credit_attrs.recovery_rate:.2%}\n")
                f.write(f"  Risk-Adjusted Spread: {credit_attrs.current_spread:.2%}\n")
                f.write(f"  Expected Loss: ${credit_attrs.expected_loss:,.0f}\n")
        
        print(f"\n📄 Detailed report exported to: {report_filename}")
        print("="*80)

def run_portfolio_analysis_with_source(data_source='deals_data/deal_start_unstructured.csv', advanced_credit_mode=True):
    """Run portfolio analysis with specified data source and credit mode"""
    print(f"="*100)
    print(f"PORTFOLIO ANALYSIS WITH DATA SOURCE: {data_source}")
    print(f"="*100)
    
    analyzer = PrivateLoanPortfolioAnalyzer(deal_start_source=data_source, advanced_credit_mode=advanced_credit_mode)
    result = analyzer.run_analysis()
    return result

if __name__ == "__main__":
    import sys
    
    # Check if data source is specified as command line argument
    if len(sys.argv) > 1:
        data_source = sys.argv[1]
        print(f"Using specified data source: {data_source}")
    else:
        data_source = 'deals_data/deal_start_unstructured.csv'
        print("Using default data source: deals_data/deal_start_unstructured.csv (AI-extracted from PDF memos)")
        print("Available alternatives: deals_data/deal_start_structured.csv")
        print("Usage: python run_deals.py [deals_data/deal_start_unstructured.csv|deals_data/deal_start_structured.csv]")
    
    result = run_portfolio_analysis_with_source(data_source)