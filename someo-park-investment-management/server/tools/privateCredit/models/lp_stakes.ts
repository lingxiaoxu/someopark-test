import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'lp_stakes',
  sheetName: 'LP Stakes Secondary',
  description:
    'LP Stakes secondary purchase analysis: compute IRR, MOIC, max bid at required IRR, and overpayment warning.',
  fund: 'VCOP',

  inputs: [
    { name: 'stated_nav', label: 'Stated NAV', type: 'number', default: 100, description: 'Stated net asset value of the LP stake' },
    { name: 'discount', label: 'Discount', type: 'percentage', default: 0.15, description: 'Discount to NAV for purchase' },
    { name: 'required_irr', label: 'Required IRR', type: 'percentage', default: 0.18, description: 'Target IRR for max bid calculation' },
    { name: 'distributions', label: 'Distributions', type: 'array', default: [0, 20, 40, 60, 0], description: 'Expected distributions by year' },
  ],

  outputs: [
    { name: 'purchase_price', label: 'Purchase Price', type: 'number' },
    { name: 'irr', label: 'IRR', type: 'percentage' },
    { name: 'moic', label: 'MOIC', type: 'number' },
    { name: 'max_bid', label: 'Max Bid', type: 'number' },
    { name: 'implied_discount', label: 'Implied Discount at Max Bid', type: 'percentage' },
    { name: 'overpayment_warning', label: 'Overpayment Warning', type: 'string' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const nav: number = inputs.stated_nav ?? 100
    const discount: number = inputs.discount ?? 0.15
    const requiredIRR: number = inputs.required_irr ?? 0.18
    const distributions: number[] = inputs.distributions ?? [0, 20, 40, 60, 0]

    const purchasePrice = nav * (1 - discount)
    const investorCFs = [-purchasePrice, ...distributions.slice(1)]
    const n = investorCFs.length

    const irr = solveIRR(investorCFs)
    const moic = calcMOIC(investorCFs)

    // Max bid = PV of future distributions at required IRR
    let maxBid = 0
    for (let t = 1; t < distributions.length; t++) {
      maxBid += distributions[t] / Math.pow(1 + requiredIRR, t)
    }

    const impliedDiscount = nav > 0 ? 1 - maxBid / nav : 0
    const overpayment = purchasePrice > maxBid
      ? `OVERPAYING by ${(purchasePrice - maxBid).toFixed(2)} — purchase ${purchasePrice.toFixed(2)} > max bid ${maxBid.toFixed(2)}`
      : 'OK — purchase price within max bid'

    // Cashflow table
    const headers = ['Distribution', 'Investor_CF', 'Cumulative_CF']
    const rows: number[][] = []
    let cumCF = 0
    for (let t = 0; t < distributions.length; t++) {
      const cf = t === 0 ? -purchasePrice : distributions[t]
      cumCF += cf
      rows.push([distributions[t], cf, cumCF])
    }

    return {
      model: model.name,
      inputs: { stated_nav: nav, discount, required_irr: requiredIRR, distributions },
      outputs: {
        purchase_price: purchasePrice,
        irr,
        moic,
        max_bid: maxBid,
        implied_discount: impliedDiscount,
        overpayment_warning: overpayment,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `LP stake at ${(discount * 100).toFixed(0)}% discount → price ${purchasePrice.toFixed(1)}. Max bid at ${(requiredIRR * 100).toFixed(0)}% required IRR = ${maxBid.toFixed(2)}.`,
    }
  },
}

export default model
