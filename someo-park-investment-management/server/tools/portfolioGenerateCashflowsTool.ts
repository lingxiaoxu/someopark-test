// server/tools/portfolioGenerateCashflowsTool.ts
// Generates detailed loan cashflow schedule using bond_utilities.py
// Data source: portfolio_of_private_credit_deals/bond_utilities.py — LoanSpec + generate_loan_schedule

import { runPython } from './privateCredit/pythonBridge.js'
import type { AgentTool } from './index.js'

export const portfolioGenerateCashflowsTool: AgentTool = {
  definition: {
    name: 'portfolio_generate_cashflows',
    description: `Generate a detailed loan cashflow schedule with IRR and MOIC. Outputs period-by-period breakdown of interest, principal, fees, and outstanding balance. Supports annuity/bullet/straight-line amortization, interest-only periods, PIK, upfront/exit fees, and multiple day-count conventions.`,
    input_schema: {
      type: 'object',
      properties: {
        principal: {
          type: 'number',
          description: 'Loan principal amount (e.g. 10000000 for $10M)'
        },
        annual_rate: {
          type: 'number',
          description: 'Annual interest rate as decimal (e.g. 0.08 for 8%)'
        },
        origination_date: {
          type: 'string',
          description: 'Loan origination date in YYYY-MM-DD format'
        },
        term_months: {
          type: 'number',
          description: 'Total loan term in months'
        },
        io_months: {
          type: 'number',
          description: 'Interest-only period in months (default 0)'
        },
        frequency: {
          type: 'string',
          enum: ['ME', 'QE', 'SE', 'YE'],
          description: 'Payment frequency: ME=monthly, QE=quarterly, SE=semi-annual, YE=annual (default QE)'
        },
        amort_style: {
          type: 'string',
          enum: ['annuity', 'bullet', 'straight_line'],
          description: 'Amortization style (default annuity)'
        },
        upfront_fee: {
          type: 'number',
          description: 'Upfront fee as decimal (e.g. 0.02 for 2%, default 0)'
        },
        exit_fee: {
          type: 'number',
          description: 'Exit fee as decimal (e.g. 0.01 for 1%, default 0)'
        },
        pik_rate: {
          type: 'number',
          description: 'Payment-in-kind rate as decimal (default 0)'
        },
        day_count: {
          type: 'string',
          enum: ['30/360', 'ACT/365', 'ACT/ACT'],
          description: 'Day count convention (default ACT/365)'
        },
        benchmark_rate: {
          type: 'number',
          description: 'Benchmark rate for spread calculations (default 0)'
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
      upfront_fee = 0,
      exit_fee = 0,
      pik_rate = 0,
      day_count = 'ACT/365',
      benchmark_rate = 0,
    } = input

    const script = `
import json
from bond_utilities import LoanSpec, generate_loan_schedule, calculate_loan_irr_and_moic

try:
    spec = LoanSpec(
        principal=${principal},
        annual_interest_rate=${annual_rate},
        origination_date='${origination_date}',
        term_months=${term_months},
        interest_only_months=${io_months},
        payment_frequency='${frequency}',
        amortization_style='${amort_style}',
        upfront_fee_percent=${upfront_fee},
        exit_fee_percent=${exit_fee},
        pik_rate=${pik_rate},
        day_count='${day_count}',
        benchmark_rate=${benchmark_rate}
    )
    schedule = generate_loan_schedule(spec)
    irr, moic = calculate_loan_irr_and_moic(schedule)

    rows = []
    for idx, row in schedule.iterrows():
        rows.append({
            'date': str(idx.date()),
            'outstanding_start': round(row['outstanding_start'], 2),
            'interest': round(row['interest_payment'], 2),
            'principal': round(row['principal_payment'], 2),
            'fees': round(row['fees'], 2),
            'total_cf': round(row['total_cashflow'], 2),
            'outstanding_end': round(row['outstanding_end'], 2),
            'rate': round(row['interest_rate'], 6)
        })

    print(json.dumps({
        'success': True,
        'irr': round(irr, 6),
        'moic': round(moic, 4),
        'total_periods': len(schedule),
        'principal': ${principal},
        'annual_rate': ${annual_rate},
        'term_months': ${term_months},
        'cashflows': rows
    }))
except Exception as e:
    print(json.dumps({'success': False, 'error': str(e)[:500]}))
`

    return runPython(script)
  }
}
