import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'vcop_dual_track',
  sheetName: 'VCOP Dual Track',
  description:
    'Buyer acts as both NAV lender and secondary LP buyer for the same fund.',
  fund: 'VCOP',

  inputs: [
    { name: 'fund_nav', label: 'Fund NAV', type: 'number', default: 200, description: 'Total fund net asset value' },
    { name: 'secondary_lp_pct', label: 'Secondary LP %', type: 'percentage', default: 0.10, description: 'LP stake as percentage of fund' },
    { name: 'secondary_discount', label: 'Secondary Discount', type: 'percentage', default: 0.20, description: 'Discount to NAV for the secondary purchase' },
    { name: 'loan_principal', label: 'Loan Principal', type: 'number', default: 100, description: 'NAV loan principal amount' },
    { name: 'loan_coupon', label: 'Loan Coupon', type: 'percentage', default: 0.12, description: 'Annual interest rate on NAV loan' },
    { name: 'oid_rate', label: 'OID Rate', type: 'percentage', default: 0, description: 'Original issue discount' },
    { name: 'lp_distributions', label: 'LP Distributions', type: 'array', default: [0, 44.16, 160, 0], description: 'Distributions received as LP' },
    { name: 'nav_loan_paydown', label: 'NAV Loan Paydown', type: 'array', default: [0, 80, 20, 0], description: 'Loan principal paydown schedule' },
  ],

  outputs: [
    { name: 'secondary_purchase_price', label: 'Secondary Purchase Price', type: 'number' },
    { name: 'loan_irr', label: 'Loan IRR', type: 'percentage' },
    { name: 'loan_moic', label: 'Loan MOIC', type: 'number' },
    { name: 'equity_irr', label: 'Equity IRR', type: 'percentage' },
    { name: 'equity_moic', label: 'Equity MOIC', type: 'number' },
    { name: 'combined_irr', label: 'Combined IRR', type: 'percentage' },
    { name: 'combined_moic', label: 'Combined MOIC', type: 'number' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const fundNav: number = inputs.fund_nav ?? 200
    const lpPct: number = inputs.secondary_lp_pct ?? 0.10
    const discount: number = inputs.secondary_discount ?? 0.20
    const principal: number = inputs.loan_principal ?? 100
    const coupon: number = inputs.loan_coupon ?? 0.12
    const oidRate: number = inputs.oid_rate ?? 0
    const lpDist: number[] = inputs.lp_distributions ?? [0, 44.16, 160, 0]
    const paydown: number[] = inputs.nav_loan_paydown ?? [0, 80, 20, 0]

    const purchasePrice = fundNav * lpPct * (1 - discount)
    const netAdvance = principal * (1 - oidRate)
    const n = Math.max(lpDist.length, paydown.length)

    let loanOutstanding = principal
    const loanCFs: number[] = []
    const equityCFs: number[] = []
    const combinedCFs: number[] = []

    const headers = [
      'LP_Distribution', 'Loan_Interest', 'Loan_Paydown', 'Loan_Outstanding',
      'Loan_CF', 'Equity_CF', 'Combined_CF',
    ]
    const rows: number[][] = []

    for (let t = 0; t < n; t++) {
      const dist = lpDist[t] ?? 0
      const pd = paydown[t] ?? 0

      if (t === 0) {
        // Initial investment period
        const loanCF0 = -netAdvance
        const equityCF0 = -purchasePrice
        const combinedCF0 = loanCF0 + equityCF0

        loanCFs.push(loanCF0)
        equityCFs.push(equityCF0)
        combinedCFs.push(combinedCF0)
        rows.push([dist, 0, 0, loanOutstanding, loanCF0, equityCF0, combinedCF0])
      } else {
        const interest = loanOutstanding * coupon
        const actualPaydown = Math.min(pd, loanOutstanding)
        loanOutstanding = Math.max(0, loanOutstanding - actualPaydown)

        // At final period, repay remaining loan balance
        let finalRepay = 0
        if (t === n - 1 && loanOutstanding > 0) {
          finalRepay = loanOutstanding
          loanOutstanding = 0
        }

        const loanCF = interest + actualPaydown + finalRepay
        const equityCF = dist
        const combinedCF = loanCF + equityCF

        loanCFs.push(loanCF)
        equityCFs.push(equityCF)
        combinedCFs.push(combinedCF)
        rows.push([dist, interest, actualPaydown + finalRepay, loanOutstanding, loanCF, equityCF, combinedCF])
      }
    }

    const loanIRR = solveIRR(loanCFs)
    const loanMOIC = calcMOIC(loanCFs)
    const equityIRR = solveIRR(equityCFs)
    const equityMOIC = calcMOIC(equityCFs)
    const combinedIRR = solveIRR(combinedCFs)
    const combinedMOIC = calcMOIC(combinedCFs)

    return {
      model: model.name,
      inputs: {
        fund_nav: fundNav, secondary_lp_pct: lpPct, secondary_discount: discount,
        loan_principal: principal, loan_coupon: coupon, oid_rate: oidRate,
        lp_distributions: lpDist, nav_loan_paydown: paydown,
      },
      outputs: {
        secondary_purchase_price: purchasePrice,
        loan_irr: loanIRR,
        loan_moic: loanMOIC,
        equity_irr: equityIRR,
        equity_moic: equityMOIC,
        combined_irr: combinedIRR,
        combined_moic: combinedMOIC,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `Dual track: LP stake (${(lpPct * 100).toFixed(0)}% at ${(discount * 100).toFixed(0)}% discount = ${purchasePrice.toFixed(1)}) + NAV loan (${principal} at ${(coupon * 100).toFixed(1)}%).`,
    }
  },
}

export default model
