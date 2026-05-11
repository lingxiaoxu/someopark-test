// server/tools/pcCustomCashflowTool.ts
// Custom cashflow model — arbitrary sequences with IRR/MOIC/PV/waterfall
import type { AgentTool } from './index.js'

export const pcCustomCashflowTool: AgentTool = {
  definition: {
    name: 'pc_custom_cashflow',
    description: 'Build a custom cash flow model with arbitrary sequences. Calculates IRR, MOIC, NPV. Optionally applies Euro-style waterfall (capital return → pref → catch-up → LP/GP split) and fee layering (mgmt fee + carry). Use this when no existing Excel template model fits the deal structure.',
    input_schema: {
      type: 'object' as const,
      properties: {
        cashflows: { type: 'array', items: { type: 'number' }, description: 'Array of cash flows [t0, t1, t2, ...]. t0 is typically negative (investment).' },
        discount_rate: { type: 'number', description: 'Discount rate for NPV calculation (e.g. 0.08 for 8%)' },
        waterfall: {
          type: 'object',
          properties: {
            committed_capital: { type: 'number' },
            invested_capital: { type: 'number' },
            pref_rate: { type: 'number', description: 'Preferred return rate (e.g. 0.08)' },
            carry_pct: { type: 'number', description: 'Carried interest % (e.g. 0.20)' },
            mgmt_fee_pct: { type: 'number', description: 'Management fee % on committed (e.g. 0.0125)' },
          },
          description: 'Optional waterfall parameters for Euro-style distribution'
        },
      },
      required: ['cashflows']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ cashflows, discount_rate, waterfall }) {
    const { solveIRR, calcNPV, calcMOIC } = await import('./privateCredit/models/irr.js')

    const irr = solveIRR(cashflows)
    const moic = calcMOIC(cashflows)
    const npv = discount_rate != null ? calcNPV(discount_rate, cashflows) : undefined

    const result: any = {
      cashflows,
      irr: Math.round(irr * 1e6) / 1e6,
      moic: Math.round(moic * 1e4) / 1e4,
    }
    if (npv != null) {
      result.npv = Math.round(npv * 100) / 100
      result.discount_rate = discount_rate
    }

    // Apply waterfall if provided
    if (waterfall) {
      const { committed_capital = 100, invested_capital, pref_rate = 0.08, carry_pct = 0.20, mgmt_fee_pct = 0 } = waterfall
      const ic = invested_capital ?? committed_capital
      const distributions = cashflows.slice(1).map(v => Math.max(0, v)) // positive CFs are distributions

      let unreturned = ic
      let prefOwed = 0
      let capitalReturned = 0
      let gpCum = 0
      const schedule: any[] = []

      for (let t = 0; t < distributions.length; t++) {
        const gross = distributions[t]
        const mgmtFee = mgmt_fee_pct * committed_capital
        const available = Math.max(0, gross - mgmtFee)

        // Capital return
        const toCapital = Math.min(available, unreturned)
        unreturned -= toCapital
        capitalReturned += toCapital

        // Pref accrual
        const prefAccrual = unreturned * pref_rate
        prefOwed += prefAccrual

        // Pay pref
        const afterCapital = available - toCapital
        const toPref = Math.min(afterCapital, prefOwed)
        prefOwed -= toPref

        // Post-pref profit for carry
        const postPref = afterCapital - toPref
        const cumPostPref = postPref // simplified: period post-pref
        const gpTarget = (carry_pct / (1 - carry_pct)) * toPref
        const catchupNeeded = Math.max(0, gpTarget - gpCum)
        const catchup = Math.min(postPref, catchupNeeded)
        gpCum += catchup

        const remaining = postPref - catchup
        const gpSplit = remaining * carry_pct
        const lpSplit = remaining * (1 - carry_pct)
        gpCum += gpSplit

        const lpDist = toCapital + toPref + lpSplit
        schedule.push({
          period: t + 1,
          gross_dist: Math.round(gross * 100) / 100,
          mgmt_fee: Math.round(mgmtFee * 100) / 100,
          capital_return: Math.round(toCapital * 100) / 100,
          pref_paid: Math.round(toPref * 100) / 100,
          catchup: Math.round(catchup * 100) / 100,
          gp_carry: Math.round(gpSplit * 100) / 100,
          lp_distribution: Math.round(lpDist * 100) / 100,
        })
      }

      const lpCashflows = [-ic, ...schedule.map(s => s.lp_distribution)]
      const lpIrr = solveIRR(lpCashflows)
      const lpMoic = calcMOIC(lpCashflows)

      result.waterfall = {
        lp_irr: Math.round(lpIrr * 1e6) / 1e6,
        lp_moic: Math.round(lpMoic * 1e4) / 1e4,
        gp_total_carry: Math.round(gpCum * 100) / 100,
        schedule,
      }
    }

    return result
  }
}
