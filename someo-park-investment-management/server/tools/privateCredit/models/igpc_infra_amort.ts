import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'igpc_infra_amort',
  sheetName: 'IGPC Infrastructure Debt',
  description:
    'Infrastructure debt with interest-only period followed by amortization (straight-line or mortgage-style).',
  fund: 'IGPC',

  inputs: [
    { name: 'par', label: 'Par Value', type: 'number', default: 100, description: 'Par/face value' },
    { name: 'coupon', label: 'Coupon Rate', type: 'percentage', default: 0.065, description: 'Annual coupon rate' },
    { name: 'io_years', label: 'IO Years', type: 'number', default: 5, description: 'Interest-only period in years' },
    { name: 'amort_years', label: 'Amort Years', type: 'number', default: 5, description: 'Amortization period in years' },
    { name: 'amort_profile', label: 'Amort Profile', type: 'string', default: 'straight', description: 'Amortization style: "straight" or "mortgage"' },
  ],

  outputs: [
    { name: 'bond_irr', label: 'Bond IRR', type: 'percentage' },
    { name: 'bond_moic', label: 'Bond MOIC', type: 'number' },
    { name: 'total_interest', label: 'Total Interest', type: 'number' },
    { name: 'wal', label: 'Weighted Avg Life (years)', type: 'number' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const par: number = inputs.par ?? 100
    const couponRate: number = inputs.coupon ?? 0.065
    const ioYears: number = inputs.io_years ?? 5
    const amortYears: number = inputs.amort_years ?? 5
    const amortProfile: string = inputs.amort_profile ?? 'straight'

    const totalYears = ioYears + amortYears

    // Pre-compute amortization schedule
    let outstandingBalance = par
    const cashflows: number[] = [-par]

    const headers = ['Interest', 'Principal', 'Total_CF', 'Outstanding']
    const rows: number[][] = []
    rows.push([0, 0, -par, par])

    let totalInterest = 0
    let walNumerator = 0 // sum of (t * principal_t)

    if (amortProfile === 'mortgage') {
      // Mortgage-style: constant total payment during amort period
      // PMT = P * r / (1 - (1+r)^-n)
      const r = couponRate
      const pmt = par * r / (1 - Math.pow(1 + r, -amortYears))

      for (let t = 1; t <= totalYears; t++) {
        const interest = outstandingBalance * couponRate
        let principal = 0
        let cf: number

        if (t <= ioYears) {
          // IO period
          cf = interest
        } else {
          // Amort period - mortgage style
          principal = pmt - interest
          // Guard against floating point overshoot on last payment
          if (t === totalYears) {
            principal = outstandingBalance
          }
          cf = interest + principal
          outstandingBalance = Math.max(0, outstandingBalance - principal)
          walNumerator += t * principal
        }

        totalInterest += interest
        cashflows.push(cf)
        rows.push([interest, principal, cf, outstandingBalance])
      }
    } else {
      // Straight-line amortization
      const principalPerPeriod = par / amortYears

      for (let t = 1; t <= totalYears; t++) {
        const interest = outstandingBalance * couponRate
        let principal = 0
        let cf: number

        if (t <= ioYears) {
          cf = interest
        } else {
          principal = t === totalYears ? outstandingBalance : Math.min(principalPerPeriod, outstandingBalance)
          cf = interest + principal
          outstandingBalance = Math.max(0, outstandingBalance - principal)
          walNumerator += t * principal
        }

        totalInterest += interest
        cashflows.push(cf)
        rows.push([interest, principal, cf, outstandingBalance])
      }
    }

    const wal = par > 0 ? walNumerator / par : 0
    const bondIRR = solveIRR(cashflows)
    const bondMOIC = calcMOIC(cashflows)

    return {
      model: model.name,
      inputs: { par, coupon: couponRate, io_years: ioYears, amort_years: amortYears, amort_profile: amortProfile },
      outputs: {
        bond_irr: bondIRR,
        bond_moic: bondMOIC,
        total_interest: totalInterest,
        wal,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `Infra debt: ${par} par, ${(couponRate * 100).toFixed(1)}% coupon, ${ioYears}yr IO + ${amortYears}yr ${amortProfile} amort. WAL = ${wal.toFixed(1)}yr.`,
    }
  },
}

export default model
