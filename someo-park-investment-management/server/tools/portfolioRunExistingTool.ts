// server/tools/portfolioRunExistingTool.ts
// Runs analysis on the existing portfolio from CSV deal files
// Data source: portfolio_of_private_credit_deals/run_deals.py — PrivateLoanPortfolioAnalyzer
//              portfolio_of_private_credit_deals/deals_data/*.csv

import { runPython } from './privateCredit/pythonBridge.js'
import type { AgentTool } from './index.js'

export const portfolioRunExistingTool: AgentTool = {
  definition: {
    name: 'portfolio_run_existing',
    description: `Run analysis on the existing private credit portfolio from CSV deal files. Loads deals from deals_data/ directory, calculates credit scores, generates cashflows for each loan, runs stress tests, and returns portfolio-level summary metrics. Choose between unstructured (AI-extracted from PDFs) or structured data source.`,
    input_schema: {
      type: 'object',
      properties: {
        data_source: {
          type: 'string',
          enum: ['unstructured', 'structured'],
          description: 'Data source: "unstructured" (AI-extracted from PDF memos, default) or "structured" (manually curated)'
        },
        advanced_credit_mode: {
          type: 'boolean',
          description: 'Use advanced credit-score-based modeling (default true)'
        },
        summary_only: {
          type: 'boolean',
          description: 'Return only portfolio summary without per-deal cashflows (default true, faster)'
        },
      },
      required: []
    }
  },
  isConcurrencySafe: () => false,  // Writes output files, not safe for concurrent runs
  isReadOnly: () => false,          // Creates output files (plots, reports)
  async execute(input: any) {
    const {
      data_source = 'unstructured',
      advanced_credit_mode = true,
      summary_only = true,
    } = input

    // The 5 legacy demo deals were relocated to tests/fixtures when the module went live
    // on real BDC underlying loans (see RunBDCLookThrough.py / portfolio_bdc_holdings).
    // These options now run on the regression fixtures; real-data queries use the
    // portfolio_bdc_holdings tool (precomputed sleeve look-through).
    const csvFile = data_source === 'structured'
      ? 'tests/fixtures/deal_start_structured.csv'
      : 'tests/fixtures/deal_start_unstructured.csv'

    // Conditionally include per-deal cashflow rows in the Python script
    const cashflowBlock = summary_only ? '' : `
            # Include cashflow rows if not summary-only
            cf_rows = []
            for cf_idx, cf_row in schedule.iterrows():
                cf_rows.append({
                    'date': str(cf_idx.date()),
                    'interest': round(cf_row['interest_payment'], 2),
                    'principal': round(cf_row['principal_payment'], 2),
                    'outstanding_end': round(cf_row['outstanding_end'], 2),
                })
            deal_info['cashflows'] = cf_rows
`

    const script = `
import json, pandas as pd, numpy as np
from bond_utilities import LoanSpec, generate_loan_schedule, calculate_loan_irr_and_moic
from credit_risk_module import CreditRiskCashflowIntegrator
import os

try:
    # Load deals CSV
    csv_path = '${csvFile}'
    if not os.path.exists(csv_path):
        print(json.dumps({'success': False, 'error': f'Deal file not found: {csv_path}'}))
        raise SystemExit(0)

    df = pd.read_csv(csv_path)

    advanced = ${advanced_credit_mode ? 'True' : 'False'}
    integrator = CreditRiskCashflowIntegrator(advanced_mode=advanced)

    deals = []
    portfolio_total_principal = 0
    portfolio_total_el = 0
    portfolio_irrs = []

    for idx, row in df.iterrows():
        try:
            company = str(row.get('company', f'Deal_{idx}'))
            sector = str(row.get('sector', 'Unknown'))
            instrument = str(row.get('instrument', 'Unknown'))
            currency = str(row.get('currency', 'USD'))

            # Parse deal size
            deal_size = float(row.get('deal_size', 0))

            # Parse coupon — handle "SOFR + Xbps" format
            coupon_str = str(row.get('coupon', '0'))
            if 'SOFR' in coupon_str.upper() or '+' in coupon_str:
                # Floating: extract spread in bps
                parts = coupon_str.replace('bps', '').replace('bp', '').split('+')
                spread_part = parts[-1].strip()
                try:
                    spread_bps = float(spread_part)
                    annual_rate = spread_bps / 10000.0 + 0.043  # SOFR ~4.3% baseline
                except ValueError:
                    annual_rate = 0.08
                rate_type = 'floating'
            elif '%' in coupon_str:
                annual_rate = float(coupon_str.replace('%', '').strip()) / 100.0
                rate_type = 'fixed'
            else:
                try:
                    annual_rate = float(coupon_str)
                    if annual_rate > 1:
                        annual_rate /= 100.0
                except ValueError:
                    annual_rate = 0.08
                rate_type = 'fixed'

            # Parse maturity year to term months (from origination ~today)
            maturity_str = str(row.get('maturity', '2030'))
            try:
                mat_year = int(maturity_str)
                term_months = max(12, (mat_year - 2025) * 12)
            except ValueError:
                term_months = 60

            # Parse fundamentals
            ebitda = float(row.get('ebitda', 100_000_000))
            leverage_str = str(row.get('leverage', '3.5'))
            try:
                leverage = float(leverage_str.replace('x', '').strip())
            except ValueError:
                leverage = 3.5

            rev_growth = str(row.get('rev', '5% YoY'))

            # Credit score calculation
            try:
                growth_rate = float(rev_growth.replace('% YoY', '').replace('%', '').strip())
            except (ValueError, AttributeError):
                growth_rate = 5.0
            leverage_score = max(0, 100 - (leverage - 2.0) * 20)
            growth_adj = min(growth_rate * 2, 20)
            sector_risk_map = {
                'Consumer Staples': 0.9, 'Energy (Midstream)': 1.2,
                'Healthcare Services': 1.1, 'Transportation & Logistics': 1.15,
                'Industrial Services': 1.1,
            }
            sector_mult = sector_risk_map.get(sector, 1.0)
            credit_score = (leverage_score + growth_adj) * sector_mult

            # Credit risk attributes
            loan_data = {
                'credit_score': credit_score,
                'default_prob': 0.03,
                'principal': deal_size,
                'current_spread': annual_rate,
                'instrument': instrument,
            }
            attrs = integrator.get_credit_adjusted_attributes(loan_data)

            # Generate cashflows
            spec = LoanSpec(
                principal=deal_size,
                annual_interest_rate=annual_rate,
                origination_date='2025-01-15',
                term_months=term_months,
                payment_frequency='QE',
                amortization_style='annuity',
                day_count='ACT/365'
            )
            schedule = generate_loan_schedule(spec)
            irr, moic = calculate_loan_irr_and_moic(schedule)

            portfolio_total_principal += deal_size
            portfolio_total_el += attrs.expected_loss
            portfolio_irrs.append(irr)

            deal_info = {
                'company': company,
                'sector': sector,
                'instrument': instrument,
                'deal_size': deal_size,
                'annual_rate': round(annual_rate, 4),
                'rate_type': rate_type,
                'term_months': term_months,
                'credit_score': round(credit_score, 2),
                'default_prob': round(attrs.default_prob, 4),
                'recovery_rate': round(attrs.recovery_rate, 4),
                'expected_loss': round(attrs.expected_loss, 2),
                'irr': round(irr, 6),
                'moic': round(moic, 4),
                'leverage': leverage,
                'ebitda': ebitda,
            }

${cashflowBlock}

            deals.append(deal_info)
        except Exception as deal_err:
            deals.append({
                'company': str(row.get('company', f'Deal_{idx}')),
                'error': str(deal_err)[:200],
            })

    # Portfolio summary
    avg_irr = float(np.mean(portfolio_irrs)) if portfolio_irrs else 0
    avg_credit_score = float(np.mean([d['credit_score'] for d in deals if 'credit_score' in d])) if deals else 0
    avg_default_prob = float(np.mean([d['default_prob'] for d in deals if 'default_prob' in d])) if deals else 0

    print(json.dumps({
        'success': True,
        'data_source': '${csvFile}',
        'credit_mode': 'advanced' if advanced else 'static',
        'portfolio_summary': {
            'num_deals': len(deals),
            'total_principal': round(portfolio_total_principal, 2),
            'total_expected_loss': round(portfolio_total_el, 2),
            'avg_irr': round(avg_irr, 6),
            'avg_credit_score': round(avg_credit_score, 2),
            'avg_default_prob': round(avg_default_prob, 4),
            'portfolio_loss_rate': round(portfolio_total_el / portfolio_total_principal * 100, 4) if portfolio_total_principal > 0 else 0,
        },
        'deals': deals,
    }))
except Exception as e:
    import traceback
    print(json.dumps({'success': False, 'error': str(e)[:500], 'traceback': traceback.format_exc()[-300:]}))
`

    // Portfolio run can take longer — allow 120s
    return runPython(script, 120000)
  }
}
