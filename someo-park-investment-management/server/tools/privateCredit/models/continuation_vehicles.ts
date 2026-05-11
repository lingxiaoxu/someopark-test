import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'continuation_vehicles',
  sheetName: 'Continuation Vehicles',
  description:
    'Continuation vehicle analysis with management fees and carried interest (hurdle-based).',
  fund: 'VCOP',

  inputs: [
    { name: 'nav', label: 'NAV', type: 'number', default: 10, description: 'Initial NAV of the continuation vehicle' },
    { name: 'discount', label: 'Discount', type: 'percentage', default: 0.05, description: 'Discount to NAV' },
    { name: 'mgmt_fee', label: 'Management Fee', type: 'percentage', default: 0.01, description: 'Annual management fee on NAV' },
    { name: 'carry_pct', label: 'Carry %', type: 'percentage', default: 0.10, description: 'Carried interest percentage' },
    { name: 'hurdle', label: 'Hurdle Rate', type: 'percentage', default: 0.07, description: 'Preferred return hurdle' },
    { name: 'gross_distributions', label: 'Gross Distributions', type: 'array', default: [0, 3, 8, 0], description: 'Gross distributions per period' },
  ],

  outputs: [
    { name: 'purchase_price', label: 'Purchase Price', type: 'number' },
    { name: 'gross_irr', label: 'Gross IRR', type: 'percentage' },
    { name: 'gross_moic', label: 'Gross MOIC', type: 'number' },
    { name: 'net_irr', label: 'Net IRR', type: 'percentage' },
    { name: 'net_moic', label: 'Net MOIC', type: 'number' },
    { name: 'total_carry', label: 'Total Carry', type: 'number' },
    { name: 'total_mgmt_fees', label: 'Total Mgmt Fees', type: 'number' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const nav: number = inputs.nav ?? 10
    const discount: number = inputs.discount ?? 0.05
    const mgmtFee: number = inputs.mgmt_fee ?? 0.01
    const carryPct: number = inputs.carry_pct ?? 0.10
    const hurdle: number = inputs.hurdle ?? 0.07
    const grossDist: number[] = inputs.gross_distributions ?? [0, 3, 8, 0]

    const purchasePrice = nav * (1 - discount)
    const n = grossDist.length

    const grossCFs: number[] = [-purchasePrice]
    const netCFs: number[] = [-purchasePrice]

    const headers = [
      'Gross_Distribution', 'Mgmt_Fee', 'Cumulative_Gross', 'Hurdle_Amount',
      'Excess', 'Carry', 'Net_Distribution', 'Net_CF',
    ]
    const rows: number[][] = []
    rows.push([0, 0, 0, 0, 0, 0, 0, -purchasePrice])

    let cumGross = 0
    let totalCarry = 0
    let totalMgmtFees = 0

    for (let t = 1; t < n; t++) {
      const gross = grossDist[t]
      cumGross += gross

      // Management fee on initial NAV
      const fee = nav * mgmtFee

      // Hurdle: compound hurdle amount on purchase price
      const hurdleAmt = purchasePrice * (Math.pow(1 + hurdle, t) - 1)

      // Profit above purchase price
      const cumProfit = cumGross - purchasePrice
      const excess = Math.max(0, cumProfit - hurdleAmt)

      // Carry on incremental excess
      const cumCarryTarget = excess * carryPct
      const periodCarry = Math.max(0, cumCarryTarget - totalCarry)
      totalCarry += periodCarry
      totalMgmtFees += fee

      const netDist = gross - fee - periodCarry
      grossCFs.push(gross)
      netCFs.push(netDist)

      rows.push([gross, fee, cumGross, hurdleAmt, excess, periodCarry, netDist, netDist])
    }

    const grossIRR = solveIRR(grossCFs)
    const grossMOIC = calcMOIC(grossCFs)
    const netIRR = solveIRR(netCFs)
    const netMOIC = calcMOIC(netCFs)

    return {
      model: model.name,
      inputs: { nav, discount, mgmt_fee: mgmtFee, carry_pct: carryPct, hurdle, gross_distributions: grossDist },
      outputs: {
        purchase_price: purchasePrice,
        gross_irr: grossIRR,
        gross_moic: grossMOIC,
        net_irr: netIRR,
        net_moic: netMOIC,
        total_carry: totalCarry,
        total_mgmt_fees: totalMgmtFees,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `CV at ${(discount * 100).toFixed(0)}% discount (price ${purchasePrice.toFixed(2)}). Mgmt fee ${(mgmtFee * 100).toFixed(1)}%, carry ${(carryPct * 100).toFixed(0)}% over ${(hurdle * 100).toFixed(0)}% hurdle.`,
    }
  },
}

export default model
