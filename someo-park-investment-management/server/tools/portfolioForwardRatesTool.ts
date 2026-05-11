// server/tools/portfolioForwardRatesTool.ts
// Looks up historical FRED rates and forward rate projections
// Data source: portfolio_of_private_credit_deals/forward_rate_lookup.py — ForwardRateLookup
//              portfolio_of_private_credit_deals/fred_rates.csv — historical FRED data

import { runPython } from './privateCredit/pythonBridge.js'
import type { AgentTool } from './index.js'

export const portfolioForwardRatesTool: AgentTool = {
  definition: {
    name: 'portfolio_forward_rates',
    description: `Look up interest rates (historical from FRED or forward projections). Supports SOFR, EFFR, DGS2, DGS5, DGS10, DGS30. Can look up specific dates or project monthly rates forward from today.`,
    input_schema: {
      type: 'object',
      properties: {
        rate_code: {
          type: 'string',
          enum: ['SOFR', 'EFFR', 'DGS2', 'DGS5', 'DGS10', 'DGS30'],
          description: 'Rate code: SOFR, EFFR, DGS2, DGS5, DGS10, DGS30'
        },
        dates: {
          type: 'array',
          items: { type: 'string' },
          description: 'Specific dates to look up (YYYY-MM-DD format). If omitted, generates monthly projection.'
        },
        projection_months: {
          type: 'number',
          description: 'Number of months to project forward from today (default 12, used only when dates not provided)'
        },
      },
      required: ['rate_code']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute(input: any) {
    const {
      rate_code,
      dates,
      projection_months = 12,
    } = input

    let script: string

    if (dates && Array.isArray(dates) && dates.length > 0) {
      // Specific dates mode
      const datesJson = JSON.stringify(dates)
      script = `
import json, pandas as pd
from forward_rate_lookup import ForwardRateLookup
import os

try:
    rates_csv = os.path.join(os.getcwd(), 'fred_rates.csv')
    rates_df = pd.read_csv(rates_csv, index_col=0, parse_dates=True) if os.path.exists(rates_csv) else None
    lookup = ForwardRateLookup()

    dates = ${datesJson}
    rate_code = '${rate_code}'
    today_str = str(pd.Timestamp.now().date())

    results = []
    for d in dates:
        try:
            rate = lookup.get_forward_rate(rate_code, d, rates_df)
            source = 'historical' if d <= today_str else 'forward'
            results.append({'date': d, 'rate': round(rate, 6), 'source': source})
        except Exception as e:
            results.append({'date': d, 'rate': None, 'error': str(e)[:200]})

    print(json.dumps({
        'success': True,
        'rate_code': rate_code,
        'count': len(results),
        'rates': results
    }))
except Exception as e:
    print(json.dumps({'success': False, 'error': str(e)[:500]}))
`
    } else {
      // Projection mode — generate monthly dates from today
      script = `
import json, pandas as pd
from forward_rate_lookup import ForwardRateLookup
from datetime import datetime, timedelta
import os

try:
    rates_csv = os.path.join(os.getcwd(), 'fred_rates.csv')
    rates_df = pd.read_csv(rates_csv, index_col=0, parse_dates=True) if os.path.exists(rates_csv) else None
    lookup = ForwardRateLookup()

    rate_code = '${rate_code}'
    months = ${projection_months}
    today_str = str(pd.Timestamp.now().date())

    # Generate monthly dates
    dates = []
    base = datetime.now()
    for m in range(months + 1):
        d = base + timedelta(days=30 * m)
        dates.append(d.strftime('%Y-%m-%d'))

    results = []
    for d in dates:
        try:
            rate = lookup.get_forward_rate(rate_code, d, rates_df)
            source = 'historical' if d <= today_str else 'forward'
            results.append({'date': d, 'rate': round(rate, 6), 'source': source})
        except Exception as e:
            results.append({'date': d, 'rate': None, 'error': str(e)[:200]})

    print(json.dumps({
        'success': True,
        'rate_code': rate_code,
        'projection_months': months,
        'count': len(results),
        'rates': results
    }))
except Exception as e:
    print(json.dumps({'success': False, 'error': str(e)[:500]}))
`
    }

    return runPython(script)
  }
}
