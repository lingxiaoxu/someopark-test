import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'vcop_fair_nav',
  sheetName: 'VCOP Fair NAV & Bidding',
  description:
    'Compute fair NAV based on PV of expected distributions, derive bid range, and IRR at each bid level.',
  fund: 'VCOP',

  inputs: [
    { name: 'stated_nav', label: 'Stated NAV', type: 'number', default: 100, description: 'GP-reported NAV' },
    { name: 'required_irr', label: 'Required IRR', type: 'percentage', default: 0.12, description: 'Discount rate for fair NAV' },
    { name: 'tight_discount', label: 'Tight Discount', type: 'percentage', default: 0.08, description: 'Low end of bid discount range' },
    { name: 'wide_discount', label: 'Wide Discount', type: 'percentage', default: 0.10, description: 'High end of bid discount range' },
    { name: 'distributions', label: 'Distributions', type: 'array', default: [0, 25, 30, 28, 20, 0], description: 'Expected distributions by year' },
  ],

  outputs: [
    { name: 'fair_nav', label: 'Fair NAV', type: 'number' },
    { name: 'implied_discount', label: 'Implied Discount', type: 'percentage' },
    { name: 'bid_tight', label: 'Bid (Tight)', type: 'number' },
    { name: 'bid_wide', label: 'Bid (Wide)', type: 'number' },
    { name: 'irr_at_tight', label: 'IRR at Tight Bid', type: 'percentage' },
    { name: 'irr_at_wide', label: 'IRR at Wide Bid', type: 'percentage' },
    { name: 'irr_at_stated', label: 'IRR at Stated NAV', type: 'percentage' },
    { name: 'warning', label: 'Warning', type: 'string' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const statedNav: number = inputs.stated_nav ?? 100
    const requiredIRR: number = inputs.required_irr ?? 0.12
    const tightDiscount: number = inputs.tight_discount ?? 0.08
    const wideDiscount: number = inputs.wide_discount ?? 0.10
    const distributions: number[] = inputs.distributions ?? [0, 25, 30, 28, 20, 0]

    // Fair NAV = PV of all future distributions at required IRR
    let fairNav = 0
    for (let t = 1; t < distributions.length; t++) {
      fairNav += distributions[t] / Math.pow(1 + requiredIRR, t)
    }

    const impliedDiscount = statedNav > 0 ? 1 - fairNav / statedNav : 0

    // Bid range
    const bidTight = statedNav * (1 - tightDiscount)
    const bidWide = statedNav * (1 - wideDiscount)

    // IRR at each bid level
    const futureDist = distributions.slice(1)

    const irrAtTight = solveIRR([-bidTight, ...futureDist])
    const irrAtWide = solveIRR([-bidWide, ...futureDist])
    const irrAtStated = solveIRR([-statedNav, ...futureDist])

    // Warnings
    const warnings: string[] = []
    if (fairNav < bidWide) {
      warnings.push(`Fair NAV (${fairNav.toFixed(2)}) < Wide bid (${bidWide.toFixed(2)}) — both bids overpriced`)
    } else if (fairNav < bidTight) {
      warnings.push(`Fair NAV (${fairNav.toFixed(2)}) < Tight bid (${bidTight.toFixed(2)}) — tight bid overpriced`)
    }
    if (impliedDiscount < 0) {
      warnings.push(`Fair NAV exceeds stated NAV — potential undervaluation by GP`)
    }

    // Cashflow table: show PV breakdown
    const headers = ['Distribution', 'PV_Factor', 'PV_of_Distribution']
    const rows: number[][] = []
    rows.push([0, 1, 0])
    for (let t = 1; t < distributions.length; t++) {
      const pvFactor = 1 / Math.pow(1 + requiredIRR, t)
      rows.push([distributions[t], pvFactor, distributions[t] * pvFactor])
    }

    return {
      model: model.name,
      inputs: { stated_nav: statedNav, required_irr: requiredIRR, tight_discount: tightDiscount, wide_discount: wideDiscount, distributions },
      outputs: {
        fair_nav: fairNav,
        implied_discount: impliedDiscount,
        bid_tight: bidTight,
        bid_wide: bidWide,
        irr_at_tight: irrAtTight,
        irr_at_wide: irrAtWide,
        irr_at_stated: irrAtStated,
        warning: warnings.length > 0 ? warnings.join('; ') : 'None',
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `Fair NAV = ${fairNav.toFixed(2)} vs stated ${statedNav}. Bid range: ${bidWide.toFixed(1)} (wide) to ${bidTight.toFixed(1)} (tight).`,
    }
  },
}

export default model
