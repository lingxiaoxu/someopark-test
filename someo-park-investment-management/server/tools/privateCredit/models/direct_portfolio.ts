import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'direct_portfolio',
  sheetName: 'Direct Portfolio Secondary',
  description:
    'Direct portfolio secondary purchase analysis: IRR, MOIC, PV-based max bid, and overpayment warning.',
  fund: 'VCOP',

  inputs: [
    { name: 'portfolio_nav', label: 'Portfolio NAV', type: 'number', default: 100, description: 'Stated portfolio net asset value' },
    { name: 'discount', label: 'Discount', type: 'percentage', default: 0.08, description: 'Discount to NAV for purchase' },
    { name: 'required_irr', label: 'Required IRR', type: 'percentage', default: 0.20, description: 'Target IRR for max bid calculation' },
    { name: 'realizations', label: 'Realizations', type: 'array', default: [0, 32, 50, 46], description: 'Expected realization cashflows by year' },
  ],

  outputs: [
    { name: 'purchase_price', label: 'Purchase Price', type: 'number' },
    { name: 'irr', label: 'IRR', type: 'percentage' },
    { name: 'moic', label: 'MOIC', type: 'number' },
    { name: 'pv', label: 'PV of Realizations', type: 'number' },
    { name: 'max_bid', label: 'Max Bid', type: 'number' },
    { name: 'overpayment_warning', label: 'Overpayment Warning', type: 'string' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const nav: number = inputs.portfolio_nav ?? 100
    const discount: number = inputs.discount ?? 0.08
    const requiredIRR: number = inputs.required_irr ?? 0.20
    const realizations: number[] = inputs.realizations ?? [0, 32, 50, 46]

    const purchasePrice = nav * (1 - discount)
    const investorCFs = [-purchasePrice, ...realizations.slice(1)]

    const irr = solveIRR(investorCFs)
    const moic = calcMOIC(investorCFs)

    // Max bid = PV of future realizations at required IRR
    let maxBid = 0
    for (let t = 1; t < realizations.length; t++) {
      maxBid += realizations[t] / Math.pow(1 + requiredIRR, t)
    }

    const overpayment = purchasePrice > maxBid
      ? `OVERPAYING by ${(purchasePrice - maxBid).toFixed(2)} — purchase ${purchasePrice.toFixed(2)} > max bid ${maxBid.toFixed(2)}`
      : 'OK — purchase price within max bid'

    const headers = ['Realization', 'Investor_CF', 'Cumulative_CF']
    const rows: number[][] = []
    let cumCF = 0
    for (let t = 0; t < realizations.length; t++) {
      const cf = t === 0 ? -purchasePrice : realizations[t]
      cumCF += cf
      rows.push([realizations[t], cf, cumCF])
    }

    return {
      model: model.name,
      inputs: { portfolio_nav: nav, discount, required_irr: requiredIRR, realizations },
      outputs: {
        purchase_price: purchasePrice,
        irr,
        moic,
        pv: maxBid,
        max_bid: maxBid,
        overpayment_warning: overpayment,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `Direct portfolio at ${(discount * 100).toFixed(0)}% discount → price ${purchasePrice.toFixed(1)}. Max bid at ${(requiredIRR * 100).toFixed(0)}% required IRR = ${maxBid.toFixed(2)}.`,
    }
  },
}

export default model
