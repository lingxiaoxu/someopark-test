// server/tools/portfolioAnalyzeDealTool.ts
// Full deal analysis: cashflows + credit risk + stress test in one call
// Combines bond_utilities.py and credit_risk_module.py for a comprehensive single-deal report

import { runPython } from './privateCredit/pythonBridge.js'
import type { AgentTool } from './index.js'

export const portfolioAnalyzeDealTool: AgentTool = {
  definition: {
    name: 'portfolio_analyze_deal',
    description: `Run a comprehensive analysis on a single private credit deal: generates cashflow schedule, calculates credit risk metrics (credit score, PD, recovery, expected loss), runs base vs stressed scenarios, and returns a full deal summary. This is the all-in-one tool for deal evaluation.`,
    input_schema: {
      type: 'object',
      properties: {
        company: {
          type: 'string',
          description: 'Borrower company name'
        },
        principal: {
          type: 'number',
          description: 'Deal size / principal amount'
        },
        annual_rate: {
          type: 'number',
          description: 'Annual interest rate as decimal (e.g. 0.0725 for 7.25%)'
        },
        origination_date: {
          type: 'string',
          description: 'Origination date YYYY-MM-DD'
        },
        term_months: {
          type: 'number',
          description: 'Loan term in months'
        },
        ebitda: {
          type: 'number',
          description: 'Borrower EBITDA'
        },
        leverage: {
          type: 'number',
          description: 'Leverage ratio (e.g. 4.5)'
        },
        revenue_growth: {
          type: 'string',
          description: 'Revenue growth (e.g. "8% YoY")'
        },
        sector: {
          type: 'string',
          description: 'Industry sector'
        },
        instrument: {
          type: 'string',
          description: 'Instrument type (e.g. "Senior Secured Notes")'
        },
        io_months: {
          type: 'number',
          description: 'Interest-only months (default 0)'
        },
        frequency: {
          type: 'string',
          enum: ['ME', 'QE', 'SE', 'YE'],
          description: 'Payment frequency (default QE)'
        },
        amort_style: {
          type: 'string',
          enum: ['annuity', 'bullet', 'straight_line'],
          description: 'Amortization style (default bullet for bonds, annuity for loans)'
        },
        day_count: {
          type: 'string',
          enum: ['30/360', 'ACT/365', 'ACT/ACT'],
          description: 'Day count convention (default ACT/365)'
        },
        upfront_fee: {
          type: 'number',
          description: 'Upfront fee as decimal (default 0)'
        },
        exit_fee: {
          type: 'number',
          description: 'Exit fee as decimal (default 0)'
        },
        base_default_prob: {
          type: 'number',
          description: 'Base default probability (default 0.03)'
        },
        spread_shock_bps: {
          type: 'number',
          description: 'Stress spread shock in bps (default 200)'
        },
        pd_multiplier: {
          type: 'number',
          description: 'Stress PD multiplier (default 2.0)'
        },
      },
      required: ['company', 'principal', 'annual_rate', 'origination_date', 'term_months',
                 'ebitda', 'leverage', 'revenue_growth', 'sector', 'instrument']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute(input: any) {
    const {
      company,
      principal,
      annual_rate,
      origination_date,
      term_months,
      ebitda,
      leverage,
      revenue_growth,
      sector,
      instrument,
      io_months = 0,
      frequency = 'QE',
      amort_style = 'annuity',
      day_count = 'ACT/365',
      upfront_fee = 0,
      exit_fee = 0,
      base_default_prob = 0.03,
      spread_shock_bps = 200,
      pd_multiplier = 2.0,
    } = input

    const safeCompany = String(company).replace(/'/g, "\\'")
    const safeGrowth = String(revenue_growth).replace(/'/g, "\\'")
    const safeSector = String(sector).replace(/'/g, "\\'")
    const safeInstrument = String(instrument).replace(/'/g, "\\'")

    const script = `
import json, numpy as np
from bond_utilities import LoanSpec, generate_loan_schedule, calculate_loan_irr_and_moic
from credit_risk_module import CreditRiskCashflowIntegrator

try:
    # === 1. CREDIT SCORING ===
    growth_str = '${safeGrowth}'
    try:
        growth_rate = float(growth_str.replace('% YoY', '').replace('%', '').strip())
    except (ValueError, AttributeError):
        growth_rate = 5.0

    lev = float(${leverage})
    leverage_score = max(0, 100 - (lev - 2.0) * 20)
    growth_adj = min(growth_rate * 2, 20)
    sector_risk = {
        'Consumer Staples': 0.9, 'Energy (Midstream)': 1.2,
        'Healthcare Services': 1.1, 'Transportation & Logistics': 1.15,
        'Industrial Services': 1.1,
    }
    sector_mult = sector_risk.get('${safeSector}', 1.0)
    credit_score = (leverage_score + growth_adj) * sector_mult

    # Risk-adjusted spread
    lev_premium = max(0, (lev - 3.0) * 25)
    size_disc = 0
    if float(${ebitda}) > 100_000_000:
        size_disc = min(25, np.log10(float(${ebitda}) / 100_000_000) * 15)
    sen_adj = {'Senior Secured': 0, 'Senior Unsecured': 50, 'Unitranche': 75, 'Asset-Backed': 25}
    inst_key = ' '.join('${safeInstrument}'.split()[:2])
    inst_prem = sen_adj.get(inst_key, 100)
    ras = float(${annual_rate}) + (lev_premium - size_disc + inst_prem) / 10000

    # === 2. CREDIT RISK ATTRIBUTES ===
    integrator = CreditRiskCashflowIntegrator(advanced_mode=True)
    loan_data = {
        'credit_score': credit_score, 'default_prob': ${base_default_prob},
        'principal': ${principal}, 'current_spread': ${annual_rate},
        'risk_adjusted_spread': ras, 'instrument': '${safeInstrument}',
    }
    attrs = integrator.get_credit_adjusted_attributes(loan_data)

    # === 3. BASE CASHFLOWS ===
    spec_base = LoanSpec(
        principal=${principal}, annual_interest_rate=${annual_rate},
        origination_date='${origination_date}', term_months=${term_months},
        interest_only_months=${io_months}, payment_frequency='${frequency}',
        amortization_style='${amort_style}', day_count='${day_count}',
        upfront_fee_percent=${upfront_fee}, exit_fee_percent=${exit_fee}
    )
    sched_base = generate_loan_schedule(spec_base)
    irr_base, moic_base = calculate_loan_irr_and_moic(sched_base)

    # Summarize cashflows (first 5 + last 3 periods)
    cf_rows = []
    for idx, row in sched_base.iterrows():
        cf_rows.append({
            'date': str(idx.date()),
            'outstanding_start': round(row['outstanding_start'], 2),
            'interest': round(row['interest_payment'], 2),
            'principal': round(row['principal_payment'], 2),
            'fees': round(row['fees'], 2),
            'total_cf': round(row['total_cashflow'], 2),
            'outstanding_end': round(row['outstanding_end'], 2),
        })
    total_interest_base = float(sched_base['interest_payment'].sum())

    # === 4. STRESSED CASHFLOWS ===
    stressed_rate = ${annual_rate} + ${spread_shock_bps} / 10000.0
    spec_stressed = LoanSpec(
        principal=${principal}, annual_interest_rate=stressed_rate,
        origination_date='${origination_date}', term_months=${term_months},
        interest_only_months=${io_months}, payment_frequency='${frequency}',
        amortization_style='${amort_style}', day_count='${day_count}',
        upfront_fee_percent=${upfront_fee}, exit_fee_percent=${exit_fee}
    )
    sched_stressed = generate_loan_schedule(spec_stressed)
    irr_stressed, moic_stressed = calculate_loan_irr_and_moic(sched_stressed)
    total_interest_stressed = float(sched_stressed['interest_payment'].sum())

    # Stressed credit
    stressed_pd = min(attrs.default_prob * ${pd_multiplier}, 0.15)
    stressed_recovery = max(attrs.recovery_rate - 0.15, 0.05)
    stressed_el = stressed_pd * (1 - stressed_recovery) * ${principal}

    # === 5. ASSEMBLE RESULT ===
    print(json.dumps({
        'success': True,
        'company': '${safeCompany}',
        'deal_summary': {
            'principal': ${principal},
            'annual_rate': ${annual_rate},
            'term_months': ${term_months},
            'sector': '${safeSector}',
            'instrument': '${safeInstrument}',
        },
        'credit_analysis': {
            'credit_score': round(credit_score, 2),
            'default_prob': round(attrs.default_prob, 4),
            'recovery_rate': round(attrs.recovery_rate, 4),
            'expected_loss': round(attrs.expected_loss, 2),
            'risk_adjusted_spread': round(ras, 4),
            'stress_multiplier': round(attrs.stress_multiplier, 2),
        },
        'base_case': {
            'irr': round(irr_base, 6),
            'moic': round(moic_base, 4),
            'total_interest': round(total_interest_base, 2),
            'total_periods': len(sched_base),
        },
        'stressed_case': {
            'irr': round(irr_stressed, 6),
            'moic': round(moic_stressed, 4),
            'total_interest': round(total_interest_stressed, 2),
            'stressed_rate': round(stressed_rate, 6),
            'stressed_pd': round(stressed_pd, 4),
            'stressed_recovery': round(stressed_recovery, 4),
            'stressed_expected_loss': round(stressed_el, 2),
        },
        'impact': {
            'irr_change_bps': round((irr_stressed - irr_base) * 10000, 1),
            'moic_change': round(moic_stressed - moic_base, 4),
            'expected_loss_change': round(stressed_el - attrs.expected_loss, 2),
        },
        'cashflows': cf_rows,
    }))
except Exception as e:
    import traceback
    print(json.dumps({'success': False, 'error': str(e)[:500], 'traceback': traceback.format_exc()[-300:]}))
`

    // Full analysis may take longer — allow 90s timeout
    return runPython(script, 90000)
  }
}
