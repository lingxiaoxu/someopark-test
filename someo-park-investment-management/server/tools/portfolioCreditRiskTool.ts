// server/tools/portfolioCreditRiskTool.ts
// Calculates credit risk metrics: credit score, default probability, recovery rate,
// expected loss, risk-adjusted spread using credit_risk_module.py and run_deals.py
// Data source: portfolio_of_private_credit_deals/credit_risk_module.py — CreditRiskCashflowIntegrator
//              portfolio_of_private_credit_deals/run_deals.py — PrivateLoanPortfolioAnalyzer

import { runPython } from './privateCredit/pythonBridge.js'
import type { AgentTool } from './index.js'

export const portfolioCreditRiskTool: AgentTool = {
  definition: {
    name: 'portfolio_credit_risk',
    description: `Calculate credit risk metrics for a private credit deal. Returns credit score, default probability (PD), recovery rate, stress multiplier, expected loss, and risk-adjusted spread. Supports static and advanced (credit-score-based) modes.`,
    input_schema: {
      type: 'object',
      properties: {
        ebitda: {
          type: 'number',
          description: 'Borrower EBITDA in dollars (e.g. 100000000 for $100M)'
        },
        leverage: {
          type: 'number',
          description: 'Leverage ratio as number (e.g. 4.5 for 4.5x)'
        },
        revenue_growth: {
          type: 'string',
          description: 'Revenue growth rate string (e.g. "8% YoY")'
        },
        sector: {
          type: 'string',
          description: 'Industry sector (e.g. "Consumer Staples", "Healthcare Services", "Energy (Midstream)", "Transportation & Logistics", "Industrial Services")'
        },
        instrument: {
          type: 'string',
          description: 'Instrument type (e.g. "Senior Secured Notes", "Senior Unsecured Notes", "Unitranche Term Loan", "Asset-Backed Facility")'
        },
        coupon: {
          type: 'number',
          description: 'Base coupon/spread as decimal (e.g. 0.0625 for 6.25%)'
        },
        principal: {
          type: 'number',
          description: 'Loan principal for expected loss calculation (default 10000000)'
        },
        base_default_prob: {
          type: 'number',
          description: 'Base default probability as decimal (default 0.03 = 3%)'
        },
        advanced_mode: {
          type: 'boolean',
          description: 'Use advanced credit-score-based adjustments (default true)'
        },
      },
      required: ['ebitda', 'leverage', 'revenue_growth', 'sector', 'instrument', 'coupon']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute(input: any) {
    const {
      ebitda,
      leverage,
      revenue_growth,
      sector,
      instrument,
      coupon,
      principal = 10000000,
      base_default_prob = 0.03,
      advanced_mode = true,
    } = input

    // Escape single quotes in strings passed to Python
    const safeGrowth = String(revenue_growth).replace(/'/g, "\\'")
    const safeSector = String(sector).replace(/'/g, "\\'")
    const safeInstrument = String(instrument).replace(/'/g, "\\'")

    const script = `
import json
import numpy as np
from credit_risk_module import CreditRiskCashflowIntegrator

try:
    # Calculate credit score using the same formula as PrivateLoanPortfolioAnalyzer
    # (replicated here to avoid importing the full run_deals module which has heavy deps)
    growth_str = '${safeGrowth}'
    try:
        growth_rate = float(growth_str.replace('% YoY', '').replace('%', '').strip())
    except (ValueError, AttributeError):
        growth_rate = 5.0

    lev = float(${leverage})
    leverage_score = max(0, 100 - (lev - 2.0) * 20)
    growth_adjustment = min(growth_rate * 2, 20)
    sector_risk = {
        'Consumer Staples': 0.9,
        'Energy (Midstream)': 1.2,
        'Healthcare Services': 1.1,
        'Transportation & Logistics': 1.15,
        'Industrial Services': 1.1,
    }
    sector_multiplier = sector_risk.get('${safeSector}', 1.0)
    credit_score = (leverage_score + growth_adjustment) * sector_multiplier

    # Calculate risk-adjusted spread (same formula as PrivateLoanPortfolioAnalyzer)
    leverage_premium = max(0, (lev - 3.0) * 25)  # bps per turn above 3x
    size_discount = 0
    ebitda_val = float(${ebitda})
    if ebitda_val > 100_000_000:
        size_discount = min(25, np.log10(ebitda_val / 100_000_000) * 15)
    seniority_adj = {
        'Senior Secured': 0,
        'Senior Unsecured': 50,
        'Unitranche': 75,
        'Asset-Backed': 25,
    }
    instrument_key = ' '.join('${safeInstrument}'.split()[:2])
    instrument_premium = seniority_adj.get(instrument_key, 100)
    total_adjustment = (leverage_premium - size_discount + instrument_premium) / 10000
    risk_adjusted_spread = float(${coupon}) + total_adjustment

    # Get credit-adjusted attributes via CreditRiskCashflowIntegrator
    advanced = ${advanced_mode ? 'True' : 'False'}
    integrator = CreditRiskCashflowIntegrator(advanced_mode=advanced)

    loan_data = {
        'credit_score': credit_score,
        'leverage': lev,
        'ebitda': ebitda_val,
        'risk_adjusted_spread': risk_adjusted_spread,
        'instrument': '${safeInstrument}',
        'coupon_numeric': float(${coupon}),
        'default_prob': float(${base_default_prob}),
        'principal': float(${principal}),
        'current_spread': float(${coupon}),
    }
    attrs = integrator.get_credit_adjusted_attributes(loan_data)

    # Also get stress testing parameters
    stress_params = integrator.get_stress_testing_parameters(loan_data)

    print(json.dumps({
        'success': True,
        'credit_score': round(credit_score, 2),
        'default_prob': round(attrs.default_prob, 4),
        'recovery_rate': round(attrs.recovery_rate, 4),
        'stress_multiplier': round(attrs.stress_multiplier, 2),
        'expected_loss': round(attrs.expected_loss, 2),
        'risk_adjusted_spread': round(risk_adjusted_spread, 4),
        'mode': 'advanced' if advanced else 'static',
        'stress_params': {
            'spread_shock': round(stress_params['spread_shock'], 4),
            'default_prob_multiplier': round(stress_params['default_prob_multiplier'], 2),
            'recovery_rate_shock': round(stress_params.get('recovery_rate_shock', 0), 4),
        },
        'inputs': {
            'ebitda': ebitda_val,
            'leverage': lev,
            'sector': '${safeSector}',
            'instrument': '${safeInstrument}',
            'coupon': float(${coupon}),
        }
    }))
except Exception as e:
    import traceback
    print(json.dumps({'success': False, 'error': str(e)[:500], 'traceback': traceback.format_exc()[-300:]}))
`

    return runPython(script)
  }
}
