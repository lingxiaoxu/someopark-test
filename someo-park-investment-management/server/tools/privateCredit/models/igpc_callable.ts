import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'igpc_callable',
  sheetName: 'IGPC Callable Bond',
  description:
    'BBB callable bond analysis: YTM, YTC, YTW. Computes IRR for both call and no-call scenarios.',
  fund: 'IGPC',

  inputs: [
    { name: 'par', label: 'Par Value', type: 'number', default: 100, description: 'Par/face value of the bond' },
    { name: 'coupon', label: 'Coupon Rate', type: 'percentage', default: 0.06, description: 'Annual coupon rate' },
    { name: 'call_year', label: 'Call Year', type: 'number', default: 4, description: 'Year at which the bond is callable (0 = non-callable)' },
    { name: 'call_price', label: 'Call Price', type: 'number', default: 1.03, description: 'Call price as fraction of par (e.g. 1.03 = 103%)' },
    { name: 'maturity', label: 'Maturity (years)', type: 'number', default: 7, description: 'Bond maturity in years' },
    { name: 'purchase_price', label: 'Purchase Price', type: 'number', default: 1.0, description: 'Purchase price as fraction of par (e.g. 1.0 = par)' },
  ],

  outputs: [
    { name: 'ytm', label: 'Yield to Maturity', type: 'percentage' },
    { name: 'ytc', label: 'Yield to Call', type: 'percentage' },
    { name: 'ytw', label: 'Yield to Worst', type: 'percentage' },
    { name: 'bond_moic', label: 'Bond MOIC (no-call)', type: 'number' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const par: number = inputs.par ?? 100
    const couponRate: number = inputs.coupon ?? 0.06
    const callYear: number = inputs.call_year ?? 4
    const callPrice: number = inputs.call_price ?? 1.03
    const maturity: number = inputs.maturity ?? 7
    const purchasePriceFrac: number = inputs.purchase_price ?? 1.0

    const buyPrice = par * purchasePriceFrac
    const couponAmt = par * couponRate

    // --- No-call (YTM) scenario ---
    const ytmCFs: number[] = [-buyPrice]
    for (let t = 1; t < maturity; t++) {
      ytmCFs.push(couponAmt)
    }
    ytmCFs.push(couponAmt + par) // final coupon + par redemption

    const ytm = solveIRR(ytmCFs)
    const bondMOIC = calcMOIC(ytmCFs)

    // --- Call scenario (YTC) ---
    let ytc = NaN
    if (callYear > 0 && callYear < maturity) {
      const ytcCFs: number[] = [-buyPrice]
      for (let t = 1; t < callYear; t++) {
        ytcCFs.push(couponAmt)
      }
      ytcCFs.push(couponAmt + par * callPrice) // final coupon + call redemption

      ytc = solveIRR(ytcCFs)
    } else {
      // Non-callable or call at maturity
      ytc = ytm
    }

    const ytw = Math.min(
      isNaN(ytm) ? Infinity : ytm,
      isNaN(ytc) ? Infinity : ytc,
    )

    // Cashflow table for the no-call scenario
    const headers = ['Coupon', 'Principal', 'Total_CF']
    const rows: number[][] = []
    rows.push([0, 0, -buyPrice])
    for (let t = 1; t <= maturity; t++) {
      const princ = t === maturity ? par : 0
      rows.push([couponAmt, princ, couponAmt + princ])
    }

    return {
      model: model.name,
      inputs: { par, coupon: couponRate, call_year: callYear, call_price: callPrice, maturity, purchase_price: purchasePriceFrac },
      outputs: {
        ytm,
        ytc,
        ytw: ytw === Infinity ? NaN : ytw,
        bond_moic: bondMOIC,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `Callable bond: par ${par}, ${(couponRate * 100).toFixed(1)}% coupon, ${maturity}yr maturity, callable at yr ${callYear} @ ${(callPrice * 100).toFixed(0)}%.`,
    }
  },
}

export default model
