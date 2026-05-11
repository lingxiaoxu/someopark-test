import { ModelDefinition, ModelResult } from './types.js'
import { solveIRR, calcMOIC } from './irr.js'
import { buildCashflowTable } from './cashflow.js'

const model: ModelDefinition = {
  name: 'vcop_raroc',
  sheetName: 'VCOP RAROC + Euro Waterfall',
  description:
    'Part 1: RAROC (risk-adjusted return on capital). Part 2: Euro-style waterfall with preferred return and GP catch-up.',
  fund: 'VCOP',

  inputs: [
    // RAROC inputs
    { name: 'ead', label: 'EAD (Exposure at Default)', type: 'number', default: 100, description: 'Exposure at default' },
    { name: 'rate', label: 'Rate', type: 'percentage', default: 0.10, description: 'Loan rate' },
    { name: 'fees', label: 'Fee Income', type: 'percentage', default: 0.01, description: 'Upfront/annual fees as % of EAD' },
    { name: 'pd', label: 'PD (Probability of Default)', type: 'percentage', default: 0.03, description: 'Annual probability of default' },
    { name: 'lgd', label: 'LGD (Loss Given Default)', type: 'percentage', default: 0.40, description: 'Loss given default' },
    { name: 'ec_pct', label: 'Economic Capital %', type: 'percentage', default: 0.25, description: 'Economic capital as % of EAD' },
    // Waterfall inputs
    { name: 'committed', label: 'Committed Capital', type: 'number', default: 100, description: 'Total committed capital' },
    { name: 'invested', label: 'Invested Capital', type: 'number', default: 80, description: 'Actually invested capital' },
    { name: 'mgmt_fee', label: 'Management Fee', type: 'percentage', default: 0.0125, description: 'Annual management fee on committed' },
    { name: 'pref', label: 'Preferred Return', type: 'percentage', default: 0.08, description: 'LP preferred return hurdle' },
    { name: 'carry', label: 'Carry %', type: 'percentage', default: 0.20, description: 'GP carried interest' },
    { name: 'distributions', label: 'Distributions', type: 'array', default: [0, 0, 30, 50, 50, 40, 0], description: 'Total gross distributions by year' },
  ],

  outputs: [
    { name: 'raroc', label: 'RAROC', type: 'percentage' },
    { name: 'gross_income', label: 'Gross Income', type: 'number' },
    { name: 'expected_loss', label: 'Expected Loss', type: 'number' },
    { name: 'economic_capital', label: 'Economic Capital', type: 'number' },
    { name: 'lp_irr', label: 'LP IRR', type: 'percentage' },
    { name: 'lp_moic', label: 'LP MOIC', type: 'number' },
    { name: 'gp_carry_total', label: 'GP Total Carry', type: 'number' },
  ],

  compute(inputs: Record<string, any>): ModelResult {
    // --- RAROC ---
    const ead: number = inputs.ead ?? 100
    const rate: number = inputs.rate ?? 0.10
    const fees: number = inputs.fees ?? 0.01
    const pd: number = inputs.pd ?? 0.03
    const lgd: number = inputs.lgd ?? 0.40
    const ecPct: number = inputs.ec_pct ?? 0.25

    const grossIncome = (rate + fees) * ead
    const expectedLoss = pd * lgd * ead
    const economicCapital = ecPct * ead
    const raroc = economicCapital > 0 ? (grossIncome - expectedLoss) / economicCapital : 0

    // --- Euro Waterfall ---
    const committed: number = inputs.committed ?? 100
    const invested: number = inputs.invested ?? 80
    const mgmtFeePct: number = inputs.mgmt_fee ?? 0.0125
    const prefRate: number = inputs.pref ?? 0.08
    const carryPct: number = inputs.carry ?? 0.20
    const distributions: number[] = inputs.distributions ?? [0, 0, 30, 50, 50, 40, 0]

    const n = distributions.length

    // Euro waterfall: period by period
    let capitalReturned = 0
    let cumPrefOwed = 0 // cumulative pref owed (compounds)
    let cumPrefPaid = 0
    let gpCarryTotal = 0

    const lpCFs: number[] = [-invested]
    const headers = [
      'Gross_Dist', 'Mgmt_Fee', 'Net_Dist', 'Capital_Return', 'Pref_Payment',
      'Catchup', 'Carry_Split', 'LP_CF', 'GP_CF',
    ]
    const rows: number[][] = []
    rows.push([0, 0, 0, 0, 0, 0, 0, -invested, 0])

    for (let t = 1; t < n; t++) {
      const grossDist = distributions[t]
      const mgmtFeeAmt = committed * mgmtFeePct
      const netDist = Math.max(0, grossDist - mgmtFeeAmt)

      // Accrue pref on unreturned capital
      const unreturnedCapital = Math.max(0, invested - capitalReturned)
      cumPrefOwed += unreturnedCapital * prefRate

      let remaining = netDist
      let periodCapReturn = 0
      let periodPref = 0
      let periodCatchup = 0
      let periodCarrySplit = 0

      // Step 1: Return capital
      if (capitalReturned < invested && remaining > 0) {
        periodCapReturn = Math.min(remaining, invested - capitalReturned)
        capitalReturned += periodCapReturn
        remaining -= periodCapReturn
      }

      // Step 2: Pay accrued preferred return
      const prefUnpaid = cumPrefOwed - cumPrefPaid
      if (prefUnpaid > 0 && remaining > 0) {
        periodPref = Math.min(remaining, prefUnpaid)
        cumPrefPaid += periodPref
        remaining -= periodPref
      }

      // Step 3: GP catch-up — GP gets carry/(1-carry) of cumulative pref paid, less what GP already received
      if (remaining > 0) {
        const gpTarget = (carryPct / (1 - carryPct)) * cumPrefPaid
        const gpNeeded = Math.max(0, gpTarget - gpCarryTotal)
        periodCatchup = Math.min(remaining, gpNeeded)
        gpCarryTotal += periodCatchup
        remaining -= periodCatchup
      }

      // Step 4: Residual split (1-carry) to LP, carry to GP
      if (remaining > 0) {
        periodCarrySplit = remaining * carryPct
        gpCarryTotal += periodCarrySplit
        remaining -= periodCarrySplit // remaining after GP take = LP share
      }

      const lpCF = periodCapReturn + periodPref + (remaining) // LP gets cap return + pref + LP share of residual
      const gpCF = periodCatchup + periodCarrySplit

      lpCFs.push(lpCF)
      rows.push([grossDist, mgmtFeeAmt, netDist, periodCapReturn, periodPref, periodCatchup, periodCarrySplit, lpCF, gpCF])
    }

    const lpIRR = solveIRR(lpCFs)
    const lpMOIC = calcMOIC(lpCFs)

    return {
      model: model.name,
      inputs: {
        ead, rate, fees, pd, lgd, ec_pct: ecPct,
        committed, invested, mgmt_fee: mgmtFeePct, pref: prefRate, carry: carryPct, distributions,
      },
      outputs: {
        raroc,
        gross_income: grossIncome,
        expected_loss: expectedLoss,
        economic_capital: economicCapital,
        lp_irr: lpIRR,
        lp_moic: lpMOIC,
        gp_carry_total: gpCarryTotal,
      },
      cashflows: buildCashflowTable(headers, rows),
      description: `RAROC = ${(raroc * 100).toFixed(1)}%. Euro waterfall: LP IRR ${isNaN(lpIRR) ? 'N/A' : (lpIRR * 100).toFixed(1) + '%'}, GP carry ${gpCarryTotal.toFixed(2)}.`,
    }
  },
}

export default model
