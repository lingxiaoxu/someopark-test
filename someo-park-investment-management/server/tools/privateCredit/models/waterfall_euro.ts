import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'waterfall_euro',
  sheetName: 'Euro-Style Waterfall',
  description:
    'Full Euro-style waterfall: capital return, preferred return accrual/payment, GP catch-up, and 80/20 residual split.',
  fund: 'General',

  inputs: [
    { name: 'committed', label: 'Committed Capital', type: 'number', default: 100, description: 'Total committed capital' },
    { name: 'invested', label: 'Invested Capital', type: 'number', default: 80, description: 'Actually invested capital' },
    { name: 'mgmt_fee_pct', label: 'Management Fee %', type: 'percentage', default: 0.0125, description: 'Annual management fee on committed capital' },
    { name: 'pref_rate', label: 'Preferred Return', type: 'percentage', default: 0.08, description: 'LP preferred return (annual)' },
    { name: 'carry_pct', label: 'Carry %', type: 'percentage', default: 0.20, description: 'GP carried interest percentage' },
    { name: 'distributions', label: 'Distributions', type: 'array', default: [0, 0, 30, 50, 50, 40, 0], description: 'Total gross distributions by year' },
  ],

  outputs: [
    { name: 'lp_irr', label: 'LP IRR', type: 'percentage' },
    { name: 'lp_moic', label: 'LP MOIC', type: 'number' },
    { name: 'gp_carry_total', label: 'GP Total Carry', type: 'number' },
    { name: 'total_mgmt_fees', label: 'Total Management Fees', type: 'number' },
    { name: 'total_to_lp', label: 'Total to LP', type: 'number' },
    { name: 'total_to_gp', label: 'Total to GP', type: 'number' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    const committed: number = inputs.committed ?? 100
    const invested: number = inputs.invested ?? 80
    const mgmtFeePct: number = inputs.mgmt_fee_pct ?? 0.0125
    const prefRate: number = inputs.pref_rate ?? 0.08
    const carryPct: number = inputs.carry_pct ?? 0.20
    const distributions: number[] = inputs.distributions ?? [0, 0, 30, 50, 50, 40, 0]

    const n = distributions.length

    let capitalReturned = 0
    let cumPrefOwed = 0
    let cumPrefPaid = 0
    let gpCarryTotal = 0
    let totalMgmtFees = 0
    let totalToLP = 0
    let totalToGP = 0

    const lpCFs: number[] = [-invested]
    const headers = [
      'Year', 'Gross_Dist', 'Mgmt_Fee', 'Net_Available', 'Capital_Return',
      'Pref_Accrued', 'Pref_Paid', 'Catchup', 'Carry_Split', 'LP_Residual',
      'LP_CF', 'GP_CF',
    ]
    const rows: number[][] = []
    rows.push([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -invested, 0])

    for (let t = 1; t < n; t++) {
      const grossDist = distributions[t]
      const mgmtFeeAmt = committed * mgmtFeePct
      totalMgmtFees += mgmtFeeAmt
      const netAvailable = Math.max(0, grossDist - mgmtFeeAmt)

      // Accrue pref on unreturned capital
      const unreturnedCapital = Math.max(0, invested - capitalReturned)
      const prefAccrual = unreturnedCapital * prefRate
      cumPrefOwed += prefAccrual

      let remaining = netAvailable
      let periodCapReturn = 0
      let periodPrefPaid = 0
      let periodCatchup = 0
      let periodCarrySplit = 0
      let lpResidual = 0

      // Step 1: Return capital
      if (capitalReturned < invested && remaining > 0) {
        periodCapReturn = Math.min(remaining, invested - capitalReturned)
        capitalReturned += periodCapReturn
        remaining -= periodCapReturn
      }

      // Step 2: Pay accrued preferred return
      const prefUnpaid = cumPrefOwed - cumPrefPaid
      if (prefUnpaid > 0 && remaining > 0) {
        periodPrefPaid = Math.min(remaining, prefUnpaid)
        cumPrefPaid += periodPrefPaid
        remaining -= periodPrefPaid
      }

      // Step 3: GP catch-up
      // GP target = carry/(1-carry) * cumulative pref paid
      if (remaining > 0) {
        const gpTarget = (carryPct / (1 - carryPct)) * cumPrefPaid
        const gpNeeded = Math.max(0, gpTarget - gpCarryTotal)
        periodCatchup = Math.min(remaining, gpNeeded)
        gpCarryTotal += periodCatchup
        remaining -= periodCatchup
      }

      // Step 4: Residual split — (1-carry) to LP, carry to GP
      if (remaining > 0) {
        periodCarrySplit = remaining * carryPct
        lpResidual = remaining * (1 - carryPct)
        gpCarryTotal += periodCarrySplit
        remaining = 0
      }

      const lpCF = periodCapReturn + periodPrefPaid + lpResidual
      const gpCF = periodCatchup + periodCarrySplit + mgmtFeeAmt

      totalToLP += lpCF
      totalToGP += gpCF

      lpCFs.push(lpCF)
      rows.push([
        t, grossDist, mgmtFeeAmt, netAvailable, periodCapReturn,
        prefAccrual, periodPrefPaid, periodCatchup, periodCarrySplit,
        lpResidual, lpCF, gpCF,
      ])
    }

    const lpIRR = solveIRR(lpCFs)
    const lpMOIC = calcMOIC(lpCFs)

    return {
      model: model.name,
      inputs: {
        committed, invested, mgmt_fee_pct: mgmtFeePct, pref_rate: prefRate,
        carry_pct: carryPct, distributions,
      },
      outputs: {
        lp_irr: lpIRR,
        lp_moic: lpMOIC,
        gp_carry_total: gpCarryTotal,
        total_mgmt_fees: totalMgmtFees,
        total_to_lp: totalToLP,
        total_to_gp: totalToGP,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `Euro waterfall: ${invested} invested of ${committed} committed. ${(prefRate * 100).toFixed(0)}% pref, ${(carryPct * 100).toFixed(0)}% carry. LP IRR = ${isNaN(lpIRR) ? 'N/A' : (lpIRR * 100).toFixed(1) + '%'}, GP carry = ${gpCarryTotal.toFixed(2)}.`,
    }
  },
}

export default model
