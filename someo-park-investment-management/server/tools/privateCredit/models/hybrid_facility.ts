import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'hybrid_facility',
  sheetName: 'Hybrid Sub + NAV Facility',
  description:
    'Subscription line transitions to NAV line. Interest-only during sub period, then NAV rate applies. Bullet repayment at horizon end.',
  fund: 'VCOP',

  inputs: [
    { name: 'equity', label: 'Equity Investment', type: 'number', default: 30, description: 'Equity contribution by buyer' },
    { name: 'sub_draw', label: 'Sub Line Draw', type: 'number', default: 20, description: 'Subscription line amount drawn' },
    { name: 'sub_rate', label: 'Sub Line Rate', type: 'percentage', default: 0.07, description: 'Interest rate during subscription line period' },
    { name: 'io_years', label: 'IO Years', type: 'number', default: 1, description: 'Number of interest-only years (sub line period)' },
    { name: 'nav_rate', label: 'NAV Line Rate', type: 'percentage', default: 0.10, description: 'Interest rate after conversion to NAV line' },
    { name: 'sweep_in_io', label: 'Sweep in IO', type: 'boolean', default: false, description: 'Whether to sweep excess cash during IO period' },
    { name: 'distributions', label: 'Distributions', type: 'array', default: [0, 10, 30, 30], description: 'Distributions per period' },
  ],

  outputs: [
    { name: 'total_investment', label: 'Total Investment', type: 'number' },
    { name: 'equity_irr', label: 'Equity IRR', type: 'percentage' },
    { name: 'equity_moic', label: 'Equity MOIC', type: 'number' },
    { name: 'debt_irr', label: 'Debt IRR', type: 'percentage' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const equity: number = inputs.equity ?? 30
    const subDraw: number = inputs.sub_draw ?? 20
    const subRate: number = inputs.sub_rate ?? 0.07
    const ioYears: number = inputs.io_years ?? 1
    const navRate: number = inputs.nav_rate ?? 0.10
    const sweepInIO: boolean = inputs.sweep_in_io ?? false
    const distributions: number[] = inputs.distributions ?? [0, 10, 30, 30]

    const totalInvestment = equity + subDraw
    const n = distributions.length

    let debtOutstanding = subDraw
    const equityCFs: number[] = [-equity]
    const debtCFs: number[] = [-subDraw]

    const headers = [
      'Distribution', 'Rate', 'Interest', 'Principal_Paydown', 'Debt_Outstanding',
      'Equity_CF', 'Debt_CF',
    ]
    const rows: number[][] = []
    rows.push([0, 0, 0, 0, debtOutstanding, -equity, -subDraw])

    for (let t = 1; t < n; t++) {
      const dist = distributions[t]
      const isIO = t <= ioYears
      const rate = isIO ? subRate : navRate
      const interest = debtOutstanding * rate
      let principalPay = 0

      if (t === n - 1) {
        // Bullet repayment at horizon end
        principalPay = debtOutstanding
      } else if (!isIO || sweepInIO) {
        // Sweep excess after interest to principal
        const excess = Math.max(0, dist - interest)
        principalPay = Math.min(excess, debtOutstanding)
      }

      debtOutstanding = Math.max(0, debtOutstanding - principalPay)

      const debtCF = interest + principalPay
      const equityCF = dist - debtCF

      equityCFs.push(equityCF)
      debtCFs.push(debtCF)

      rows.push([dist, rate, interest, principalPay, debtOutstanding, equityCF, debtCF])
    }

    const equityIRR = solveIRR(equityCFs)
    const equityMOIC = calcMOIC(equityCFs)
    const debtIRR = solveIRR(debtCFs)

    return {
      model: model.name,
      inputs: { equity, sub_draw: subDraw, sub_rate: subRate, io_years: ioYears, nav_rate: navRate, sweep_in_io: sweepInIO, distributions },
      outputs: {
        total_investment: totalInvestment,
        equity_irr: equityIRR,
        equity_moic: equityMOIC,
        debt_irr: debtIRR,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `Hybrid facility: ${subDraw} sub line at ${(subRate * 100).toFixed(0)}% (IO ${ioYears}yr) → NAV at ${(navRate * 100).toFixed(0)}%. Equity = ${equity}.`,
    }
  },
}

export default model
