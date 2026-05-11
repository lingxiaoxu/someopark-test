import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'vcop_secondary_nav',
  sheetName: 'VCOP Secondary + NAV Loan',
  description:
    'Buyer acquires LP stake at price P and finances with NAV loan. Cash from distributions sweeps loan first.',
  fund: 'VCOP',

  inputs: [
    { name: 'purchase_price', label: 'Purchase Price', type: 'number', default: 80, description: 'Price paid for the LP stake' },
    { name: 'nav_loan_principal', label: 'NAV Loan Principal', type: 'number', default: 30, description: 'Loan amount drawn' },
    { name: 'loan_coupon', label: 'Loan Coupon', type: 'percentage', default: 0.10, description: 'Annual interest rate on the NAV loan' },
    { name: 'oid_rate', label: 'OID Rate', type: 'percentage', default: 0.01, description: 'Original issue discount as fraction of principal' },
    { name: 'cash_sweep', label: 'Cash Sweep', type: 'boolean', default: true, description: 'Whether to sweep excess cash to repay loan principal' },
    { name: 'distributions', label: 'Distributions', type: 'array', default: [0, 20, 35, 40, 15], description: 'Periodic distributions from the LP stake' },
  ],

  outputs: [
    { name: 'loan_irr', label: 'Loan IRR', type: 'percentage' },
    { name: 'loan_moic', label: 'Loan MOIC', type: 'number' },
    { name: 'equity_irr', label: 'Equity IRR', type: 'percentage' },
    { name: 'equity_moic', label: 'Equity MOIC', type: 'number' },
    { name: 'asset_irr', label: 'Asset IRR', type: 'percentage' },
    { name: 'asset_moic', label: 'Asset MOIC', type: 'number' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const purchasePrice: number = inputs.purchase_price ?? 80
    const principal: number = inputs.nav_loan_principal ?? 30
    const coupon: number = inputs.loan_coupon ?? 0.10
    const oidRate: number = inputs.oid_rate ?? 0.01
    const sweep: boolean = inputs.cash_sweep ?? true
    const distributions: number[] = inputs.distributions ?? [0, 20, 35, 40, 15]

    const netAdvance = principal * (1 - oidRate)
    const equityAtClose = purchasePrice - netAdvance
    const n = distributions.length

    // Period-by-period loan amortization
    let loanOutstanding = principal
    const loanCFs: number[] = [-netAdvance] // lender perspective: negative = cash out
    const equityCFs: number[] = [-equityAtClose] // equity investor perspective
    const assetCFs: number[] = [-purchasePrice] // total asset perspective

    const rows: number[][] = []
    const headers = [
      'Distribution', 'Interest', 'Principal_Paydown', 'Loan_Outstanding',
      'Loan_CF', 'Equity_CF', 'Asset_CF',
    ]

    // Period 0 row
    rows.push([distributions[0], 0, 0, loanOutstanding, -netAdvance, -equityAtClose, -purchasePrice])

    for (let t = 1; t < n; t++) {
      const dist = distributions[t]
      const interest = loanOutstanding * coupon
      let principalPaydown = 0
      let equityCF = 0

      if (loanOutstanding <= 0) {
        // Loan fully repaid
        equityCF = dist
      } else if (sweep) {
        // Pay interest first, sweep remainder to principal
        const afterInterest = dist - interest
        if (afterInterest >= 0) {
          principalPaydown = Math.min(afterInterest, loanOutstanding)
          equityCF = afterInterest - principalPaydown
        } else {
          // Not enough to cover interest; PIK the shortfall
          principalPaydown = afterInterest // negative = added to principal
          equityCF = 0
        }
      } else {
        // No sweep: interest only, equity gets remainder
        equityCF = Math.max(0, dist - interest)
      }

      loanOutstanding = Math.max(0, loanOutstanding - principalPaydown)

      // At final period, repay remaining loan balance
      if (t === n - 1 && loanOutstanding > 0) {
        equityCF -= loanOutstanding
        principalPaydown += loanOutstanding
        loanOutstanding = 0
      }

      const loanCF = interest + principalPaydown
      loanCFs.push(loanCF)
      equityCFs.push(equityCF)
      assetCFs.push(dist)

      rows.push([dist, interest, principalPaydown, loanOutstanding, loanCF, equityCF, dist])
    }

    const loanIRR = solveIRR(loanCFs)
    const loanMOIC = calcMOIC(loanCFs)
    const equityIRR = solveIRR(equityCFs)
    const equityMOIC = calcMOIC(equityCFs)
    const assetIRR = solveIRR(assetCFs)
    const assetMOIC = calcMOIC(assetCFs)

    return {
      model: model.name,
      inputs: { purchase_price: purchasePrice, nav_loan_principal: principal, loan_coupon: coupon, oid_rate: oidRate, cash_sweep: sweep, distributions },
      outputs: {
        loan_irr: loanIRR,
        loan_moic: loanMOIC,
        equity_irr: equityIRR,
        equity_moic: equityMOIC,
        asset_irr: assetIRR,
        asset_moic: assetMOIC,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `Secondary purchase at ${purchasePrice} financed with ${principal} NAV loan (${(coupon * 100).toFixed(1)}% coupon, ${(oidRate * 100).toFixed(1)}% OID). Sweep=${sweep}.`,
    }
  },
}

export default model
