// server/tools/pcListModelsTool.ts
// Lists all Private Credit models from the Excel template
import type { AgentTool } from './index.js'

interface ModelInfo {
  name: string
  fund: string
  description: string
  key_inputs: string[]
  key_outputs: string[]
}

const ALL_MODELS: ModelInfo[] = [
  { name: 'VCOP_Secondary+NAV', fund: 'VCOP', description: 'Secondary purchase + NAV loan with cash sweep. PIK capitalization.', key_inputs: ['purchase_price', 'nav_loan_principal', 'loan_coupon', 'oid_rate', 'cash_sweep', 'distributions[]'], key_outputs: ['Loan IRR', 'Equity IRR', 'Asset IRR', 'MOIC'] },
  { name: 'VCOP_DualTrack', fund: 'VCOP', description: 'Dual track: NAV loan to fund GP + secondary LP stake in same fund.', key_inputs: ['fund_nav', 'secondary_lp_pct', 'secondary_discount', 'loan_principal', 'loan_coupon'], key_outputs: ['Loan IRR', 'Equity IRR', 'Combined IRR', 'MOIC'] },
  { name: 'VCOP_FairNAV_Pricing', fund: 'VCOP', description: 'Fair NAV valuation & bidding range. PV of forecast distributions.', key_inputs: ['stated_nav', 'required_irr', 'tight_discount', 'wide_discount', 'distributions[]'], key_outputs: ['Fair NAV', 'Implied Discount', 'Bid range', 'IRR at each bid'] },
  { name: 'VCOP_LTV_Trigger', fund: 'VCOP', description: 'NAV facility covenant monitoring: LTV, borrowing base, DSCR, cure calculator.', key_inputs: ['portfolio_nav', 'eligible_nav', 'advance_rate', 'debt', 'coupon', 'stress_nav'], key_outputs: ['LTV', 'DSCR', 'Cure Amount', 'Breach flags'] },
  { name: 'VCOP_RAROC_NAVLoan', fund: 'VCOP', description: 'RAROC for NAV loan + Euro-style fund waterfall (pref→catch-up→80/20).', key_inputs: ['ead', 'rate', 'pd', 'lgd', 'ec_pct', 'committed', 'invested', 'carry_pct'], key_outputs: ['RAROC', 'LP IRR', 'GP Carry'] },
  { name: 'IGPC_BBB_Call', fund: 'IGPC', description: 'BBB-rated private placement, callable. YTW = MIN(YTC, YTM).', key_inputs: ['par', 'coupon', 'call_year', 'call_price', 'maturity', 'purchase_price'], key_outputs: ['Bond IRR', 'YTW', 'YTM', 'MOIC'] },
  { name: 'IGPC_Infra_Amort', fund: 'IGPC', description: 'Infrastructure debt: interest-only then amortization (straight or mortgage).', key_inputs: ['par', 'coupon', 'io_years', 'amort_years', 'amort_profile'], key_outputs: ['Bond IRR', 'MOIC'] },
  { name: 'IGPC_Call_Generic', fund: 'IGPC', description: 'Generic callable bond. Parametric call year/price. YTW analysis.', key_inputs: ['par', 'coupon', 'call_year', 'call_price', 'maturity'], key_outputs: ['YTW', 'YTC', 'YTM', 'MOIC'] },
  { name: 'UBP_Structured', fund: 'UBP', description: 'Structured mezzanine: cash coupon + PIK + OID + call. Euro-style carry.', key_inputs: ['face', 'cash_coupon', 'pik_rate', 'oid', 'call_year', 'call_price', 'carry_pct', 'hurdle'], key_outputs: ['Gross IRR', 'Net IRR', 'MOIC'] },
  { name: 'UBP_Secondary_Loan', fund: 'UBP', description: 'Secondary 1st lien loan at discount. Coupon on face value.', key_inputs: ['face', 'purchase_price', 'coupon', 'years_to_exit', 'exit_price'], key_outputs: ['IRR', 'MOIC', 'Cash-on-Cash'] },
  { name: 'UBP_Warrant', fund: 'UBP', description: 'Mezzanine + penny warrant kicker on borrower equity. Carry calculation.', key_inputs: ['bond_face', 'coupon', 'oid', 'years_to_exit', 'equity_value', 'warrant_pct', 'carry_pct'], key_outputs: ['Gross IRR', 'Net IRR', 'Bond-only IRR'] },
  { name: 'LP_Stakes', fund: 'General', description: 'Secondary LP purchase at NAV discount. PV-based max bid calculation.', key_inputs: ['stated_nav', 'discount', 'required_irr', 'distributions[]'], key_outputs: ['IRR', 'MOIC', 'Max Bid', 'Overpayment Warning'] },
  { name: 'CVs', fund: 'General', description: 'Continuation vehicles: secondary buy with reset management fees and carry.', key_inputs: ['nav', 'discount', 'mgmt_fee', 'carry_pct', 'hurdle', 'distributions[]'], key_outputs: ['Net IRR', 'Gross IRR', 'MOIC'] },
  { name: 'Direct_Portfolio', fund: 'General', description: 'Direct asset secondary at discount. PV-based max bid.', key_inputs: ['portfolio_nav', 'discount', 'required_irr', 'realizations[]'], key_outputs: ['IRR', 'MOIC', 'Max Bid'] },
  { name: 'NAV_Loan', fund: 'General', description: 'NAV loan with cash sweep and LTV covenant monitoring.', key_inputs: ['loan_face', 'coupon', 'oid', 'ltv_limit', 'sweep_pct', 'fund_nav[]', 'distributions[]'], key_outputs: ['Loan IRR', 'MOIC', 'LTV per period'] },
  { name: 'Hybrid_Facility', fund: 'General', description: 'Subscription line (low rate) then NAV line (high rate) conversion.', key_inputs: ['equity', 'sub_draw', 'sub_rate', 'io_years', 'nav_rate', 'distributions[]'], key_outputs: ['Equity IRR', 'Debt IRR', 'MOIC'] },
  { name: 'Preferred_Equity', fund: 'General', description: 'Cumulative pref + participation + step-up if not redeemed at maturity.', key_inputs: ['pref_investment', 'pref_coupon', 'maturity', 'step_up_rate', 'participation_pct', 'equity_proceeds'], key_outputs: ['Pref IRR', 'MOIC'] },
  { name: 'Portfolio_HHI', fund: 'General', description: 'Herfindahl-Hirschman Index by position, GP, sector, vintage. Concentration flags.', key_inputs: ['positions[]{name, gp, sector, vintage, exposure}'], key_outputs: ['HHI by dimension', 'Effective N', 'Concentration flags'] },
  { name: 'Waterfall_Euro', fund: 'General', description: 'Euro-style waterfall: capital return → pref → catch-up → 80/20 split.', key_inputs: ['committed', 'invested', 'mgmt_fee_pct', 'pref_rate', 'carry_pct', 'distributions[]'], key_outputs: ['LP IRR', 'LP MOIC', 'GP Carry'] },
]

export const pcListModelsTool: AgentTool = {
  definition: {
    name: 'pc_list_models',
    description: 'List all available Private Credit calculation models from the Excel template (19 models across VCOP/IGPC/UBP/General fund types). Shows model name, fund type, description, required inputs, and expected outputs.',
    input_schema: {
      type: 'object' as const,
      properties: {
        fund_filter: { type: 'string', enum: ['VCOP', 'IGPC', 'UBP', 'General', 'all'], description: 'Filter by fund type (default: all)' },
        category: { type: 'string', description: 'Filter by category keyword, e.g. "waterfall", "NAV", "callable", "IRR"' },
      },
      required: []
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ fund_filter = 'all', category } = {}) {
    let models = ALL_MODELS

    if (fund_filter && fund_filter !== 'all') {
      models = models.filter(m => m.fund === fund_filter)
    }

    if (category) {
      const kw = category.toLowerCase()
      models = models.filter(m =>
        m.name.toLowerCase().includes(kw) ||
        m.description.toLowerCase().includes(kw) ||
        m.key_outputs.some(o => o.toLowerCase().includes(kw))
      )
    }

    return {
      total: models.length,
      models: models.map(m => ({
        name: m.name,
        fund: m.fund,
        description: m.description,
        inputs: m.key_inputs,
        outputs: m.key_outputs
      }))
    }
  }
}
