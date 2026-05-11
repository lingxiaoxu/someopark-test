import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'ubp_structured',
  sheetName: 'UBP Structured Mezz',
  description:
    'UBP structured mezzanine with cash coupon + PIK + OID + call option. Euro-style carry at exit.',
  fund: 'UBP',

  inputs: [
    { name: 'face', label: 'Face Value', type: 'number', default: 100, description: 'Initial face value' },
    { name: 'cash_coupon', label: 'Cash Coupon', type: 'percentage', default: 0.13, description: 'Annual cash coupon rate' },
    { name: 'pik_rate', label: 'PIK Rate', type: 'percentage', default: 0.02, description: 'Annual PIK accretion rate' },
    { name: 'oid', label: 'OID', type: 'percentage', default: 0.015, description: 'Original issue discount' },
    { name: 'call_year', label: 'Call Year', type: 'number', default: 2, description: 'Year at which the note is called (0 = hold to maturity)' },
    { name: 'call_price', label: 'Call Price', type: 'number', default: 1.05, description: 'Call price as fraction of accreted face' },
    { name: 'mgmt_fee', label: 'Management Fee', type: 'percentage', default: 0.01, description: 'Annual management fee on committed capital' },
    { name: 'carry_pct', label: 'Carry %', type: 'percentage', default: 0.10, description: 'GP carried interest percentage' },
    { name: 'hurdle', label: 'Hurdle Rate', type: 'percentage', default: 0.08, description: 'Preferred return hurdle for carry calculation' },
  ],

  outputs: [
    { name: 'gross_irr', label: 'Gross IRR', type: 'percentage' },
    { name: 'gross_moic', label: 'Gross MOIC', type: 'number' },
    { name: 'net_irr', label: 'Net IRR', type: 'percentage' },
    { name: 'net_moic', label: 'Net MOIC', type: 'number' },
    { name: 'total_carry', label: 'Total GP Carry', type: 'number' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const face: number = inputs.face ?? 100
    const cashCoupon: number = inputs.cash_coupon ?? 0.13
    const pikRate: number = inputs.pik_rate ?? 0.02
    const oid: number = inputs.oid ?? 0.015
    const callYear: number = inputs.call_year ?? 2
    const callPrice: number = inputs.call_price ?? 1.05
    const mgmtFee: number = inputs.mgmt_fee ?? 0.01
    const carryPct: number = inputs.carry_pct ?? 0.10
    const hurdle: number = inputs.hurdle ?? 0.08

    const netAdvance = face * (1 - oid)
    const horizon = callYear > 0 ? callYear : 5 // default 5yr if no call

    // Period-by-period
    let accretedFace = face
    const grossCFs: number[] = [-netAdvance]
    const netCFs: number[] = [-netAdvance]
    let cumGrossIncome = 0

    const headers = [
      'Accreted_Face', 'Cash_Coupon', 'PIK_Accretion', 'Mgmt_Fee',
      'Gross_CF', 'Net_CF',
    ]
    const rows: number[][] = []
    rows.push([face, 0, 0, 0, -netAdvance, -netAdvance])

    for (let t = 1; t <= horizon; t++) {
      const cashCpn = accretedFace * cashCoupon
      const pikAccretion = accretedFace * pikRate
      accretedFace += pikAccretion

      const fee = face * mgmtFee // fee on original committed capital
      let grossCF = cashCpn
      let netCF = cashCpn - fee

      // At call/exit year: receive call price * accreted face
      if (t === horizon) {
        const redemption = accretedFace * callPrice
        grossCF += redemption
        cumGrossIncome += cashCpn + redemption

        // Euro-style carry: compute at exit
        const totalGrossProfit = cumGrossIncome - netAdvance
        const hurdleAmt = netAdvance * (Math.pow(1 + hurdle, horizon) - 1)
        const excessProfit = Math.max(0, totalGrossProfit - hurdleAmt)
        const carry = excessProfit * carryPct

        netCF = grossCF - fee - carry

        grossCFs.push(grossCF)
        netCFs.push(netCF)
        rows.push([accretedFace, cashCpn, pikAccretion, fee, grossCF, netCF])
      } else {
        cumGrossIncome += cashCpn
        grossCFs.push(grossCF)
        netCFs.push(netCF)
        rows.push([accretedFace, cashCpn, pikAccretion, fee, grossCF, netCF])
      }
    }

    const grossIRR = solveIRR(grossCFs)
    const grossMOIC = calcMOIC(grossCFs)
    const netIRR = solveIRR(netCFs)
    const netMOIC = calcMOIC(netCFs)

    // Recompute carry for output
    const totalGrossProfit = cumGrossIncome - netAdvance
    const hurdleAmt = netAdvance * (Math.pow(1 + hurdle, horizon) - 1)
    const totalCarry = Math.max(0, totalGrossProfit - hurdleAmt) * carryPct

    return {
      model: model.name,
      inputs: {
        face, cash_coupon: cashCoupon, pik_rate: pikRate, oid, call_year: callYear,
        call_price: callPrice, mgmt_fee: mgmtFee, carry_pct: carryPct, hurdle,
      },
      outputs: {
        gross_irr: grossIRR,
        gross_moic: grossMOIC,
        net_irr: netIRR,
        net_moic: netMOIC,
        total_carry: totalCarry,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `Structured mezz: ${face} face, ${(cashCoupon * 100).toFixed(0)}% cash + ${(pikRate * 100).toFixed(0)}% PIK, ${(oid * 100).toFixed(1)}% OID, callable yr ${callYear} @ ${(callPrice * 100).toFixed(0)}%.`,
    }
  },
}

export default model
