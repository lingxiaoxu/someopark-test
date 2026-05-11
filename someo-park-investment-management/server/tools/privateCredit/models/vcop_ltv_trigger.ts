import { ModelDefinition, ModelResult } from './types.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'vcop_ltv_trigger',
  sheetName: 'VCOP LTV/OC Trigger',
  description:
    'LTV/OC trigger and cure calculator: borrowing base, headroom, DSCR, stress test, and cure amount.',
  fund: 'VCOP',

  inputs: [
    { name: 'portfolio_nav', label: 'Portfolio NAV', type: 'number', default: 100, description: 'Current portfolio NAV' },
    { name: 'eligible_nav', label: 'Eligible NAV', type: 'number', default: 80, description: 'Eligible (borrowing base) NAV' },
    { name: 'advance_rate', label: 'Advance Rate', type: 'percentage', default: 0.30, description: 'Maximum advance rate on eligible NAV' },
    { name: 'debt', label: 'Outstanding Debt', type: 'number', default: 24, description: 'Current outstanding debt' },
    { name: 'coupon', label: 'Coupon', type: 'percentage', default: 0.10, description: 'Annual coupon rate' },
    { name: 'annual_cash', label: 'Annual Cash Available', type: 'number', default: 15, description: 'Annual cash available for debt service' },
    { name: 'stress_nav', label: 'Stress NAV', type: 'number', default: 75, description: 'NAV under stress scenario' },
    { name: 'ltv_limit', label: 'LTV Limit', type: 'percentage', default: 0.30, description: 'Maximum LTV covenant' },
    { name: 'stress_eligible_nav', label: 'Stress Eligible NAV', type: 'number', default: 60, description: 'Eligible NAV under stress' },
  ],

  outputs: [
    { name: 'borrowing_base', label: 'Borrowing Base', type: 'number' },
    { name: 'headroom', label: 'Headroom', type: 'number' },
    { name: 'current_ltv', label: 'Current LTV', type: 'percentage' },
    { name: 'dscr', label: 'DSCR', type: 'number' },
    { name: 'stress_ltv', label: 'Stress LTV', type: 'percentage' },
    { name: 'stress_borrowing_base', label: 'Stress Borrowing Base', type: 'number' },
    { name: 'cure_amount', label: 'Cure Amount', type: 'number' },
    { name: 'ltv_breach', label: 'LTV Breach', type: 'string' },
    { name: 'stress_breach', label: 'Stress Breach', type: 'string' },
    { name: 'dscr_flag', label: 'DSCR Flag', type: 'string' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const portfolioNav: number = inputs.portfolio_nav ?? 100
    const eligibleNav: number = inputs.eligible_nav ?? 80
    const advanceRate: number = inputs.advance_rate ?? 0.30
    const debt: number = inputs.debt ?? 24
    const coupon: number = inputs.coupon ?? 0.10
    const annualCash: number = inputs.annual_cash ?? 15
    const stressNav: number = inputs.stress_nav ?? 75
    const ltvLimit: number = inputs.ltv_limit ?? 0.30
    const stressEligibleNav: number = inputs.stress_eligible_nav ?? 60

    // Current state
    const borrowingBase = eligibleNav * advanceRate
    const headroom = borrowingBase - debt
    const currentLTV = portfolioNav > 0 ? debt / portfolioNav : 999
    const debtService = debt * coupon
    const dscr = debtService > 0 ? annualCash / debtService : 999

    // Stress state
    const stressLTV = stressNav > 0 ? debt / stressNav : 999
    const stressBorrowingBase = stressEligibleNav * advanceRate
    const maxAllowableDebt = stressNav * ltvLimit
    const cureAmount = Math.max(0, debt - maxAllowableDebt)

    // Breach flags
    const ltvBreach = currentLTV > ltvLimit ? `BREACH: ${(currentLTV * 100).toFixed(1)}% > ${(ltvLimit * 100).toFixed(0)}%` : 'OK'
    const stressBreach = stressLTV > ltvLimit ? `BREACH: ${(stressLTV * 100).toFixed(1)}% > ${(ltvLimit * 100).toFixed(0)}%` : 'OK'
    const dscrFlag = dscr < 1.2 ? `WARNING: DSCR ${dscr.toFixed(2)}x < 1.20x` : `OK: ${dscr.toFixed(2)}x`

    // Summary table
    const headers = ['Metric', 'Current', 'Stress', 'Limit']
    const rows: number[][] = [
      [1, portfolioNav, stressNav, 0],         // NAV
      [2, eligibleNav, stressEligibleNav, 0],   // Eligible NAV
      [3, borrowingBase, stressBorrowingBase, 0], // Borrowing Base
      [4, currentLTV, stressLTV, ltvLimit],     // LTV
      [5, headroom, stressBorrowingBase - debt, 0], // Headroom
      [6, dscr, 0, 1.2],                        // DSCR
      [7, 0, cureAmount, 0],                    // Cure Amount
    ]

    return {
      model: model.name,
      inputs: {
        portfolio_nav: portfolioNav, eligible_nav: eligibleNav, advance_rate: advanceRate,
        debt, coupon, annual_cash: annualCash, stress_nav: stressNav,
        ltv_limit: ltvLimit, stress_eligible_nav: stressEligibleNav,
      },
      outputs: {
        borrowing_base: borrowingBase,
        headroom,
        current_ltv: currentLTV,
        dscr,
        stress_ltv: stressLTV,
        stress_borrowing_base: stressBorrowingBase,
        cure_amount: cureAmount,
        ltv_breach: ltvBreach,
        stress_breach: stressBreach,
        dscr_flag: dscrFlag,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `LTV trigger: current ${(currentLTV * 100).toFixed(1)}% (limit ${(ltvLimit * 100).toFixed(0)}%), stress ${(stressLTV * 100).toFixed(1)}%. Cure = ${cureAmount.toFixed(1)}. DSCR = ${dscr.toFixed(2)}x.`,
    }
  },
}

export default model
