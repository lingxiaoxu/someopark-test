import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'ubp_warrant',
  sheetName: 'UBP Mezz + Warrant',
  description:
    'Mezzanine bond plus warrant equity kicker. Bond CF + warrant payoff at exit. Euro-style carry on total return.',
  fund: 'UBP',

  inputs: [
    { name: 'bond_face', label: 'Bond Face', type: 'number', default: 100, description: 'Mezzanine bond face value' },
    { name: 'cash_coupon', label: 'Cash Coupon', type: 'percentage', default: 0.10, description: 'Annual cash coupon rate on bond' },
    { name: 'oid', label: 'OID', type: 'percentage', default: 0.01, description: 'Original issue discount' },
    { name: 'years_to_exit', label: 'Years to Exit', type: 'number', default: 4, description: 'Holding period' },
    { name: 'exit_price', label: 'Exit Price', type: 'number', default: 1.02, description: 'Bond exit price as fraction of face' },
    { name: 'equity_value', label: 'Equity Value at Exit', type: 'number', default: 800, description: 'Total company equity value at exit' },
    { name: 'warrant_pct', label: 'Warrant %', type: 'percentage', default: 0.05, description: 'Warrant coverage as % of equity' },
    { name: 'mgmt_fee', label: 'Management Fee', type: 'percentage', default: 0.01, description: 'Annual management fee' },
    { name: 'carry_pct', label: 'Carry %', type: 'percentage', default: 0.10, description: 'GP carried interest' },
    { name: 'hurdle', label: 'Hurdle Rate', type: 'percentage', default: 0.08, description: 'Preferred return hurdle' },
  ],

  outputs: [
    { name: 'gross_irr', label: 'Gross IRR', type: 'percentage' },
    { name: 'gross_moic', label: 'Gross MOIC', type: 'number' },
    { name: 'net_irr', label: 'Net IRR', type: 'percentage' },
    { name: 'net_moic', label: 'Net MOIC', type: 'number' },
    { name: 'bond_only_irr', label: 'Bond-Only IRR', type: 'percentage' },
    { name: 'warrant_value', label: 'Warrant Value', type: 'number' },
    { name: 'total_carry', label: 'Total GP Carry', type: 'number' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const bondFace: number = inputs.bond_face ?? 100
    const cashCoupon: number = inputs.cash_coupon ?? 0.10
    const oidRate: number = inputs.oid ?? 0.01
    const yearsToExit: number = inputs.years_to_exit ?? 4
    const exitPrice: number = inputs.exit_price ?? 1.02
    const equityValue: number = inputs.equity_value ?? 800
    const warrantPct: number = inputs.warrant_pct ?? 0.05
    const mgmtFee: number = inputs.mgmt_fee ?? 0.01
    const carryPct: number = inputs.carry_pct ?? 0.10
    const hurdle: number = inputs.hurdle ?? 0.08

    const netAdvance = bondFace * (1 - oidRate)
    const couponAmt = bondFace * cashCoupon
    const warrantValue = equityValue * warrantPct
    const bondExit = bondFace * exitPrice

    // Bond-only cashflows
    const bondOnlyCFs: number[] = [-netAdvance]
    for (let t = 1; t < yearsToExit; t++) bondOnlyCFs.push(couponAmt)
    bondOnlyCFs.push(couponAmt + bondExit)

    // Gross cashflows (bond + warrant)
    const grossCFs: number[] = [-netAdvance]
    for (let t = 1; t < yearsToExit; t++) grossCFs.push(couponAmt)
    grossCFs.push(couponAmt + bondExit + warrantValue) // warrant payoff at exit

    // Net cashflows: subtract mgmt fees and carry
    const netCFs: number[] = [-netAdvance]
    let cumGrossIncome = 0
    for (let t = 1; t <= yearsToExit; t++) {
      const fee = bondFace * mgmtFee
      const isExit = t === yearsToExit

      let grossCF = couponAmt
      if (isExit) grossCF += bondExit + warrantValue
      cumGrossIncome += grossCF

      let carry = 0
      if (isExit) {
        // Euro-style carry at exit
        const totalGrossProfit = cumGrossIncome - netAdvance
        const hurdleAmt = netAdvance * (Math.pow(1 + hurdle, yearsToExit) - 1)
        carry = Math.max(0, (totalGrossProfit - hurdleAmt) * carryPct)
      }

      netCFs.push(grossCF - fee - carry)
    }

    const grossIRR = solveIRR(grossCFs)
    const grossMOIC = calcMOIC(grossCFs)
    const netIRR = solveIRR(netCFs)
    const netMOIC = calcMOIC(netCFs)
    const bondOnlyIRR = solveIRR(bondOnlyCFs)

    // Compute total carry for output
    const totalGrossProfit = cumGrossIncome - netAdvance
    const hurdleAmt = netAdvance * (Math.pow(1 + hurdle, yearsToExit) - 1)
    const totalCarry = Math.max(0, (totalGrossProfit - hurdleAmt) * carryPct)

    const headers = ['Coupon', 'Bond_Exit', 'Warrant', 'Mgmt_Fee', 'Gross_CF', 'Net_CF']
    const rows: number[][] = []
    rows.push([0, 0, 0, 0, -netAdvance, -netAdvance])
    cumGrossIncome = 0
    for (let t = 1; t <= yearsToExit; t++) {
      const fee = bondFace * mgmtFee
      const isExit = t === yearsToExit
      const bExit = isExit ? bondExit : 0
      const wVal = isExit ? warrantValue : 0
      const grossCF = couponAmt + bExit + wVal
      cumGrossIncome += grossCF

      let carry = 0
      if (isExit) {
        const profit = cumGrossIncome - netAdvance
        const hAmt = netAdvance * (Math.pow(1 + hurdle, yearsToExit) - 1)
        carry = Math.max(0, (profit - hAmt) * carryPct)
      }

      rows.push([couponAmt, bExit, wVal, fee, grossCF, grossCF - fee - carry])
    }

    return {
      model: model.name,
      inputs: {
        bond_face: bondFace, cash_coupon: cashCoupon, oid: oidRate, years_to_exit: yearsToExit,
        exit_price: exitPrice, equity_value: equityValue, warrant_pct: warrantPct,
        mgmt_fee: mgmtFee, carry_pct: carryPct, hurdle,
      },
      outputs: {
        gross_irr: grossIRR,
        gross_moic: grossMOIC,
        net_irr: netIRR,
        net_moic: netMOIC,
        bond_only_irr: bondOnlyIRR,
        warrant_value: warrantValue,
        total_carry: totalCarry,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `Mezz + warrant: ${bondFace} face, ${(cashCoupon * 100).toFixed(0)}% coupon, ${(warrantPct * 100).toFixed(0)}% warrant on ${equityValue} equity, ${yearsToExit}yr exit.`,
    }
  },
}

export default model
