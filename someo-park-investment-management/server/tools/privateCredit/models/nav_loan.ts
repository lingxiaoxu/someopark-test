import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'nav_loan',
  sheetName: 'NAV Loan + LTV Covenant',
  description:
    'NAV loan with cash sweep and LTV covenant monitoring. Tracks loan outstanding, interest, principal paydown, and covenant compliance.',
  fund: 'VCOP',

  inputs: [
    { name: 'loan_face', label: 'Loan Face', type: 'number', default: 40, description: 'Loan face amount' },
    { name: 'coupon', label: 'Coupon', type: 'percentage', default: 0.12, description: 'Annual coupon rate' },
    { name: 'oid', label: 'OID', type: 'percentage', default: 0.01, description: 'Original issue discount' },
    { name: 'ltv_limit', label: 'LTV Limit', type: 'percentage', default: 0.30, description: 'Maximum loan-to-value ratio' },
    { name: 'sweep_pct', label: 'Sweep %', type: 'percentage', default: 1.0, description: 'Fraction of excess cash swept to principal (0-1)' },
    { name: 'fund_nav', label: 'Fund NAV', type: 'array', default: [200, 200, 180, 100], description: 'Fund NAV per period' },
    { name: 'distributions', label: 'Distributions', type: 'array', default: [0, 30, 60, 100], description: 'Cash distributions available per period' },
  ],

  outputs: [
    { name: 'net_advance', label: 'Net Advance', type: 'number' },
    { name: 'loan_irr', label: 'Loan IRR', type: 'percentage' },
    { name: 'loan_moic', label: 'Loan MOIC', type: 'number' },
    { name: 'covenant_breaches', label: 'Covenant Breaches', type: 'string' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const loanFace: number = inputs.loan_face ?? 40
    const coupon: number = inputs.coupon ?? 0.12
    const oid: number = inputs.oid ?? 0.01
    const ltvLimit: number = inputs.ltv_limit ?? 0.30
    const sweepPct: number = inputs.sweep_pct ?? 1.0
    const fundNav: number[] = inputs.fund_nav ?? [200, 200, 180, 100]
    const distributions: number[] = inputs.distributions ?? [0, 30, 60, 100]

    const netAdvance = loanFace * (1 - oid)
    const n = Math.max(fundNav.length, distributions.length)

    let outstanding = loanFace
    const loanCFs: number[] = [-netAdvance]

    const headers = [
      'NAV', 'Distribution', 'Interest', 'Principal_Sweep', 'Outstanding',
      'LTV', 'LTV_Limit', 'Covenant_OK', 'Loan_CF',
    ]
    const rows: number[][] = []

    const breaches: string[] = []

    // Period 0
    const ltv0 = fundNav[0] > 0 ? outstanding / fundNav[0] : 999
    const cov0 = ltv0 <= ltvLimit ? 1 : 0
    if (!cov0) breaches.push(`Year 0: LTV ${(ltv0 * 100).toFixed(1)}% > ${(ltvLimit * 100).toFixed(0)}%`)
    rows.push([fundNav[0] ?? 0, 0, 0, 0, outstanding, ltv0, ltvLimit, cov0, -netAdvance])

    for (let t = 1; t < n; t++) {
      const nav = fundNav[t] ?? fundNav[fundNav.length - 1]
      const dist = distributions[t] ?? 0

      if (outstanding <= 0) {
        loanCFs.push(0)
        rows.push([nav, dist, 0, 0, 0, 0, ltvLimit, 1, 0])
        continue
      }

      const interest = outstanding * coupon

      // Cash available for sweep after interest
      const cashAfterInterest = Math.max(0, dist - interest)
      const principalSweep = Math.min(cashAfterInterest * sweepPct, outstanding)
      outstanding = Math.max(0, outstanding - principalSweep)

      // At final period, repay remaining
      let finalRepay = 0
      if (t === n - 1 && outstanding > 0) {
        finalRepay = outstanding
        outstanding = 0
      }

      const totalPaid = interest + principalSweep + finalRepay
      const ltv = nav > 0 ? outstanding / nav : 999
      const covOK = ltv <= ltvLimit ? 1 : 0
      if (!covOK) breaches.push(`Year ${t}: LTV ${(ltv * 100).toFixed(1)}% > ${(ltvLimit * 100).toFixed(0)}%`)

      loanCFs.push(totalPaid)
      rows.push([nav, dist, interest, principalSweep + finalRepay, outstanding, ltv, ltvLimit, covOK, totalPaid])
    }

    const loanIRR = solveIRR(loanCFs)
    const loanMOIC = calcMOIC(loanCFs)

    return {
      model: model.name,
      inputs: { loan_face: loanFace, coupon, oid, ltv_limit: ltvLimit, sweep_pct: sweepPct, fund_nav: fundNav, distributions },
      outputs: {
        net_advance: netAdvance,
        loan_irr: loanIRR,
        loan_moic: loanMOIC,
        covenant_breaches: breaches.length > 0 ? breaches.join('; ') : 'None',
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `NAV loan face ${loanFace} (advance ${netAdvance.toFixed(1)}), ${(coupon * 100).toFixed(0)}% coupon, ${(sweepPct * 100).toFixed(0)}% sweep, LTV limit ${(ltvLimit * 100).toFixed(0)}%.`,
    }
  },
}

export default model
