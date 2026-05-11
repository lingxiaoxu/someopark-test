import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'ubp_secondary_loan',
  sheetName: 'UBP Secondary 1st Lien',
  description:
    'Secondary purchase of a first lien loan at a discount. Coupon computed on face (not purchase price). Exit at par or specified exit price.',
  fund: 'UBP',

  inputs: [
    { name: 'face', label: 'Face Value', type: 'number', default: 100, description: 'Loan face/par value' },
    { name: 'purchase_price', label: 'Purchase Price', type: 'number', default: 80, description: 'Price paid (at discount)' },
    { name: 'coupon', label: 'Coupon Rate', type: 'percentage', default: 0.08, description: 'Annual coupon on face' },
    { name: 'years_to_exit', label: 'Years to Exit', type: 'number', default: 2, description: 'Holding period in years' },
    { name: 'exit_price', label: 'Exit Price', type: 'number', default: 1.0, description: 'Exit price as fraction of face (1.0 = par)' },
  ],

  outputs: [
    { name: 'irr', label: 'IRR', type: 'percentage' },
    { name: 'moic', label: 'MOIC', type: 'number' },
    { name: 'cash_on_cash', label: 'Cash-on-Cash Yield', type: 'percentage' },
    { name: 'discount_pct', label: 'Purchase Discount', type: 'percentage' },
    { name: 'total_return', label: 'Total Return', type: 'number' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const face: number = inputs.face ?? 100
    const purchasePrice: number = inputs.purchase_price ?? 80
    const couponRate: number = inputs.coupon ?? 0.08
    const yearsToExit: number = inputs.years_to_exit ?? 2
    const exitPrice: number = inputs.exit_price ?? 1.0

    const couponAmt = face * couponRate
    const exitProceeds = face * exitPrice
    const discountPct = face > 0 ? 1 - purchasePrice / face : 0

    // Cash-on-cash yield = annual coupon / purchase price
    const cashOnCash = purchasePrice > 0 ? couponAmt / purchasePrice : 0

    // Build cashflows
    const cfs: number[] = [-purchasePrice]
    for (let t = 1; t < yearsToExit; t++) {
      cfs.push(couponAmt)
    }
    cfs.push(couponAmt + exitProceeds) // last period: coupon + exit

    const irr = solveIRR(cfs)
    const moic = calcMOIC(cfs)
    const totalReturn = cfs.reduce((a, b) => a + b, 0) + purchasePrice // total received - invested

    const headers = ['Coupon', 'Exit_Proceeds', 'Total_CF', 'Cumulative']
    const rows: number[][] = []
    let cum = 0
    for (let t = 0; t <= yearsToExit; t++) {
      if (t === 0) {
        cum = -purchasePrice
        rows.push([0, 0, -purchasePrice, cum])
      } else {
        const exit = t === yearsToExit ? exitProceeds : 0
        const cf = couponAmt + exit
        cum += cf
        rows.push([couponAmt, exit, cf, cum])
      }
    }

    return {
      model: model.name,
      inputs: { face, purchase_price: purchasePrice, coupon: couponRate, years_to_exit: yearsToExit, exit_price: exitPrice },
      outputs: {
        irr,
        moic,
        cash_on_cash: cashOnCash,
        discount_pct: discountPct,
        total_return: totalReturn,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `Secondary 1st lien: face ${face}, purchased at ${purchasePrice} (${(discountPct * 100).toFixed(0)}% discount), ${(couponRate * 100).toFixed(0)}% coupon, ${yearsToExit}yr exit at ${(exitPrice * 100).toFixed(0)}%.`,
    }
  },
}

export default model
