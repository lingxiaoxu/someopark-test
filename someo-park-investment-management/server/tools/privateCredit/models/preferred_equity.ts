import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'preferred_equity',
  sheetName: 'Preferred Equity',
  description:
    'Cumulative preferred equity with participation and step-up. Pref balance compounds at coupon rate; at maturity, participation on excess; if extended, step-up rate applies.',
  fund: 'VCOP',

  inputs: [
    { name: 'pref_investment', label: 'Pref Investment', type: 'number', default: 25, description: 'Preferred equity investment amount' },
    { name: 'pref_coupon', label: 'Pref Coupon', type: 'percentage', default: 0.12, description: 'Annual preferred coupon rate (cumulative)' },
    { name: 'maturity', label: 'Maturity (years)', type: 'number', default: 3, description: 'Scheduled maturity in years' },
    { name: 'step_up_rate', label: 'Step-up Rate', type: 'percentage', default: 0.15, description: 'Coupon rate after maturity if extended' },
    { name: 'participation_pct', label: 'Participation %', type: 'percentage', default: 0.20, description: 'Participation in excess equity value' },
    { name: 'equity_yr3', label: 'Equity at Maturity', type: 'number', default: 45, description: 'Total equity value at maturity' },
    { name: 'equity_yr4', label: 'Equity at Yr 4', type: 'number', default: 0, description: 'Total equity value if extended to year 4 (0 = no extension)' },
  ],

  outputs: [
    { name: 'pref_irr', label: 'Pref IRR', type: 'percentage' },
    { name: 'pref_moic', label: 'Pref MOIC', type: 'number' },
    { name: 'accrued_pref', label: 'Accrued Pref at Maturity', type: 'number' },
    { name: 'participation_value', label: 'Participation Value', type: 'number' },
    { name: 'total_payout', label: 'Total Payout', type: 'number' },
    { name: 'extended_irr', label: 'Extended IRR (if applicable)', type: 'percentage' },
    { name: 'extended_moic', label: 'Extended MOIC (if applicable)', type: 'number' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const investment: number = inputs.pref_investment ?? 25
    const coupon: number = inputs.pref_coupon ?? 0.12
    const maturity: number = inputs.maturity ?? 3
    const stepUp: number = inputs.step_up_rate ?? 0.15
    const participationPct: number = inputs.participation_pct ?? 0.20
    const equityYr3: number = inputs.equity_yr3 ?? 45
    const equityYr4: number = inputs.equity_yr4 ?? 0

    // --- Base case: maturity at scheduled date ---
    // Pref balance compounds
    const prefAtMaturity = investment * Math.pow(1 + coupon, maturity)
    const accruedPref = prefAtMaturity - investment

    // Participation on excess above pref claim
    const excessEquity = Math.max(0, equityYr3 - prefAtMaturity)
    const participation = excessEquity * participationPct

    const totalPayout = prefAtMaturity + participation

    // Cashflows: invest at t0, nothing until maturity, total payout at maturity
    const baseCFs: number[] = [-investment]
    for (let t = 1; t < maturity; t++) baseCFs.push(0)
    baseCFs.push(totalPayout)

    const prefIRR = solveIRR(baseCFs)
    const prefMOIC = calcMOIC(baseCFs)

    // --- Extension case: if equity_yr4 > 0, compute extended scenario ---
    let extIRR = NaN
    let extMOIC = NaN
    if (equityYr4 > 0) {
      // Year 4 balance: compound at step-up rate for one more year
      const prefAtYr4 = prefAtMaturity * (1 + stepUp)
      const excessYr4 = Math.max(0, equityYr4 - prefAtYr4)
      const participationYr4 = excessYr4 * participationPct
      const totalPayoutYr4 = prefAtYr4 + participationYr4

      const extCFs: number[] = [-investment]
      for (let t = 1; t <= maturity; t++) extCFs.push(0)
      extCFs.push(totalPayoutYr4)

      extIRR = solveIRR(extCFs)
      extMOIC = calcMOIC(extCFs)
    }

    // Cashflow table
    const headers = ['Pref_Balance', 'Coupon_Accrual', 'Participation', 'CF']
    const rows: number[][] = []
    let balance = investment
    for (let t = 0; t <= maturity; t++) {
      if (t === 0) {
        rows.push([balance, 0, 0, -investment])
      } else {
        const accrual = balance * coupon
        balance = balance + accrual
        const part = t === maturity ? participation : 0
        const cf = t === maturity ? totalPayout : 0
        rows.push([balance, accrual, part, cf])
      }
    }

    return {
      model: model.name,
      inputs: {
        pref_investment: investment, pref_coupon: coupon, maturity, step_up_rate: stepUp,
        participation_pct: participationPct, equity_yr3: equityYr3, equity_yr4: equityYr4,
      },
      outputs: {
        pref_irr: prefIRR,
        pref_moic: prefMOIC,
        accrued_pref: accruedPref,
        participation_value: participation,
        total_payout: totalPayout,
        extended_irr: extIRR,
        extended_moic: extMOIC,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `Pref equity ${investment} at ${(coupon * 100).toFixed(0)}% cumulative, ${maturity}yr maturity, ${(participationPct * 100).toFixed(0)}% participation.`,
    }
  },
}

export default model
