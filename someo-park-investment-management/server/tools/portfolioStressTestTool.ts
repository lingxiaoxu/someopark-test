// server/tools/portfolioStressTestTool.ts
// Stress tests a loan by comparing base-case vs stressed cashflows and credit metrics
// Data source: portfolio_of_private_credit_deals/bond_utilities.py — LoanSpec, generate_loan_schedule
//              portfolio_of_private_credit_deals/credit_risk_module.py — get_cashflow_attributes

import { runPython } from './privateCredit/pythonBridge.js'
import type { AgentTool } from './index.js'

export const portfolioStressTestTool: AgentTool = {
  definition: {
    name: 'portfolio_stress_test',
    description: `Run a stress test on a private credit loan. Compares base-case vs stressed scenarios: applies spread shock (bps), default probability multiplier, and recovery rate haircut. Returns side-by-side IRR, MOIC, expected loss, and cashflow impact.`,
    input_schema: {
      type: 'object',
      properties: {
        principal: {
          type: 'number',
          description: 'Loan principal amount'
        },
        annual_rate: {
          type: 'number',
          description: 'Annual interest rate as decimal (e.g. 0.08)'
        },
        origination_date: {
          type: 'string',
          description: 'Origination date YYYY-MM-DD'
        },
        term_months: {
          type: 'number',
          description: 'Loan term in months'
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
          description: 'Amortization style (default annuity)'
        },
        day_count: {
          type: 'string',
          enum: ['30/360', 'ACT/365', 'ACT/ACT'],
          description: 'Day count convention (default ACT/365)'
        },
        spread_shock_bps: {
          type: 'number',
          description: 'Spread shock in basis points (default 200 = +2%)'
        },
        pd_multiplier: {
          type: 'number',
          description: 'Default probability multiplier for stress (default 2.0 = double PD)'
        },
        recovery_haircut: {
          type: 'number',
          description: 'Recovery rate reduction as decimal (default 0.15 = -15%)'
        },
        credit_score: {
          type: 'number',
          description: 'Credit score for advanced mode (default 70)'
        },
        base_default_prob: {
          type: 'number',
          description: 'Base default probability as decimal (default 0.03)'
        },
      },
      required: ['principal', 'annual_rate', 'origination_date', 'term_months']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute(input: any) {
    const {
      principal,
      annual_rate,
      origination_date,
      term_months,
      io_months = 0,
      frequency = 'QE',
      amort_style = 'annuity',
      day_count = 'ACT/365',
      spread_shock_bps = 200,
      pd_multiplier = 2.0,
      recovery_haircut = 0.15,
      credit_score = 70,
      base_default_prob = 0.03,
    } = input

    const script = `
import json
from bond_utilities import LoanSpec, generate_loan_schedule, calculate_loan_irr_and_moic
from credit_risk_module import CreditRiskCashflowIntegrator

try:
    # --- Base case ---
    spec_base = LoanSpec(
        principal=${principal},
        annual_interest_rate=${annual_rate},
        origination_date='${origination_date}',
        term_months=${term_months},
        interest_only_months=${io_months},
        payment_frequency='${frequency}',
        amortization_style='${amort_style}',
        day_count='${day_count}'
    )
    schedule_base = generate_loan_schedule(spec_base)
    irr_base, moic_base = calculate_loan_irr_and_moic(schedule_base)
    total_interest_base = float(schedule_base['interest_payment'].sum())
    total_cf_base = float(schedule_base['total_cashflow'].sum())

    # --- Stressed case (rate shock) ---
    stressed_rate = ${annual_rate} + ${spread_shock_bps} / 10000.0
    spec_stressed = LoanSpec(
        principal=${principal},
        annual_interest_rate=stressed_rate,
        origination_date='${origination_date}',
        term_months=${term_months},
        interest_only_months=${io_months},
        payment_frequency='${frequency}',
        amortization_style='${amort_style}',
        day_count='${day_count}'
    )
    schedule_stressed = generate_loan_schedule(spec_stressed)
    irr_stressed, moic_stressed = calculate_loan_irr_and_moic(schedule_stressed)
    total_interest_stressed = float(schedule_stressed['interest_payment'].sum())
    total_cf_stressed = float(schedule_stressed['total_cashflow'].sum())

    # --- Credit risk: base vs stressed ---
    integrator = CreditRiskCashflowIntegrator(advanced_mode=True)

    loan_data_base = {
        'credit_score': ${credit_score},
        'default_prob': ${base_default_prob},
        'principal': ${principal},
        'current_spread': ${annual_rate},
    }
    attrs_base = integrator.get_credit_adjusted_attributes(loan_data_base)

    # Stressed credit: multiply PD, reduce recovery
    stressed_pd = min(attrs_base.default_prob * ${pd_multiplier}, 0.15)
    stressed_recovery = max(attrs_base.recovery_rate - ${recovery_haircut}, 0.05)
    stressed_lgd = 1 - stressed_recovery
    stressed_el = stressed_pd * stressed_lgd * ${principal}

    print(json.dumps({
        'success': True,
        'base_case': {
            'irr': round(irr_base, 6),
            'moic': round(moic_base, 4),
            'annual_rate': ${annual_rate},
            'total_interest': round(total_interest_base, 2),
            'total_cashflow': round(total_cf_base, 2),
            'default_prob': round(attrs_base.default_prob, 4),
            'recovery_rate': round(attrs_base.recovery_rate, 4),
            'expected_loss': round(attrs_base.expected_loss, 2),
        },
        'stressed_case': {
            'irr': round(irr_stressed, 6),
            'moic': round(moic_stressed, 4),
            'annual_rate': round(stressed_rate, 6),
            'total_interest': round(total_interest_stressed, 2),
            'total_cashflow': round(total_cf_stressed, 2),
            'default_prob': round(stressed_pd, 4),
            'recovery_rate': round(stressed_recovery, 4),
            'expected_loss': round(stressed_el, 2),
        },
        'impact': {
            'irr_change_bps': round((irr_stressed - irr_base) * 10000, 1),
            'moic_change': round(moic_stressed - moic_base, 4),
            'interest_change': round(total_interest_stressed - total_interest_base, 2),
            'expected_loss_change': round(stressed_el - attrs_base.expected_loss, 2),
            'spread_shock_bps': ${spread_shock_bps},
            'pd_multiplier': ${pd_multiplier},
            'recovery_haircut': ${recovery_haircut},
        }
    }))
except Exception as e:
    import traceback
    print(json.dumps({'success': False, 'error': str(e)[:500], 'traceback': traceback.format_exc()[-300:]}))
`

    return runPython(script)
  }
}
