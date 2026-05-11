import { ModelDefinition, ModelResult, SensitivityResult } from './models/types.js'

// Import all models
import vcop_secondary_nav from './models/vcop_secondary_nav.js'
import vcop_dual_track from './models/vcop_dual_track.js'
import lp_stakes from './models/lp_stakes.js'
import continuation_vehicles from './models/continuation_vehicles.js'
import direct_portfolio from './models/direct_portfolio.js'
import nav_loan from './models/nav_loan.js'
import hybrid_facility from './models/hybrid_facility.js'
import preferred_equity from './models/preferred_equity.js'
import igpc_callable from './models/igpc_callable.js'
import igpc_infra_amort from './models/igpc_infra_amort.js'
import ubp_structured from './models/ubp_structured.js'
import ubp_secondary_loan from './models/ubp_secondary_loan.js'
import ubp_warrant from './models/ubp_warrant.js'
import vcop_fair_nav from './models/vcop_fair_nav.js'
import vcop_ltv_trigger from './models/vcop_ltv_trigger.js'
import vcop_raroc from './models/vcop_raroc.js'
import portfolio_hhi from './models/portfolio_hhi.js'
import waterfall_euro from './models/waterfall_euro.js'

// Re-export types
export type { ModelDefinition, ModelResult, SensitivityResult } from './models/types.js'
export type { InputField, OutputField, CashflowRow } from './models/types.js'
export { solveIRR, calcMOIC, calcNPV } from './models/irr.js'

// ---------- Model Registry ----------

const modelRegistry: Record<string, ModelDefinition> = {}

function registerModel(model: ModelDefinition): void {
  modelRegistry[model.name] = model
}

// Register all 18 models (vcop_raroc includes the euro waterfall logic, waterfall_euro is standalone)
const allModels: ModelDefinition[] = [
  vcop_secondary_nav,
  vcop_dual_track,
  lp_stakes,
  continuation_vehicles,
  direct_portfolio,
  nav_loan,
  hybrid_facility,
  preferred_equity,
  igpc_callable,
  igpc_infra_amort,
  ubp_structured,
  ubp_secondary_loan,
  ubp_warrant,
  vcop_fair_nav,
  vcop_ltv_trigger,
  vcop_raroc,
  portfolio_hhi,
  waterfall_euro,
]

for (const m of allModels) {
  registerModel(m)
}

// ---------- Public API ----------

/**
 * Get a model by exact name.
 */
export function getModel(name: string): ModelDefinition | undefined {
  return modelRegistry[name]
}

/**
 * List all models, optionally filtered by fund.
 */
export function listModels(filter?: { fund?: string; category?: string }): ModelDefinition[] {
  let models = Object.values(modelRegistry)
  if (filter?.fund) {
    models = models.filter(m => m.fund === filter.fund)
  }
  if (filter?.category) {
    const cat = filter.category.toLowerCase()
    models = models.filter(m =>
      m.name.toLowerCase().includes(cat) ||
      m.description.toLowerCase().includes(cat) ||
      m.sheetName.toLowerCase().includes(cat),
    )
  }
  return models
}

/**
 * Fuzzy-match models against a natural-language query.
 * Returns ranked candidates with confidence scores and reasons.
 */
export function matchModel(query: string): Array<{ model: string; confidence: number; reason: string }> {
  const q = query.toLowerCase()
  const results: Array<{ model: string; confidence: number; reason: string }> = []

  // Keywords -> model mapping for common queries
  const keywordMap: Record<string, string[]> = {
    vcop_secondary_nav: ['secondary', 'nav loan', 'cash sweep', 'lp stake', 'vcop secondary'],
    vcop_dual_track: ['dual track', 'lender and buyer', 'both lender'],
    lp_stakes: ['lp stake', 'secondary purchase', 'max bid'],
    continuation_vehicles: ['continuation', 'cv', 'continuation vehicle'],
    direct_portfolio: ['direct portfolio', 'portfolio secondary', 'realization'],
    nav_loan: ['nav loan', 'ltv covenant', 'cash sweep', 'loan'],
    hybrid_facility: ['hybrid', 'subscription line', 'sub line', 'nav line'],
    preferred_equity: ['preferred', 'pref equity', 'cumulative', 'step-up', 'participation'],
    igpc_callable: ['callable', 'ytm', 'ytc', 'ytw', 'bond', 'igpc callable', 'bbb'],
    igpc_infra_amort: ['infrastructure', 'infra', 'amortiz', 'io period', 'igpc infra'],
    ubp_structured: ['structured', 'mezz', 'pik', 'ubp structured'],
    ubp_secondary_loan: ['secondary loan', '1st lien', 'first lien', 'ubp secondary'],
    ubp_warrant: ['warrant', 'equity kicker', 'mezz warrant', 'ubp warrant'],
    vcop_fair_nav: ['fair nav', 'bidding', 'bid range', 'fair value'],
    vcop_ltv_trigger: ['ltv trigger', 'covenant', 'cure', 'oc trigger', 'dscr', 'borrowing base'],
    vcop_raroc: ['raroc', 'risk-adjusted', 'economic capital', 'raroc waterfall'],
    portfolio_hhi: ['hhi', 'concentration', 'herfindahl', 'diversif'],
    waterfall_euro: ['waterfall', 'euro waterfall', 'carry', 'catch-up', 'preferred return'],
  }

  for (const [modelName, keywords] of Object.entries(keywordMap)) {
    const model = modelRegistry[modelName]
    if (!model) continue

    let maxScore = 0
    let bestKeyword = ''

    for (const kw of keywords) {
      if (q.includes(kw)) {
        const score = kw.length / q.length // longer keyword match = higher confidence
        if (score > maxScore) {
          maxScore = score
          bestKeyword = kw
        }
      }
    }

    // Also check model name and description
    if (q.includes(modelName.replace(/_/g, ' '))) {
      maxScore = Math.max(maxScore, 0.8)
      bestKeyword = modelName
    }

    // Check fund name
    if (q.includes(model.fund.toLowerCase())) {
      maxScore = Math.max(maxScore, 0.2)
      if (!bestKeyword) bestKeyword = model.fund
    }

    if (maxScore > 0) {
      results.push({
        model: modelName,
        confidence: Math.min(1.0, maxScore * 1.5), // scale up
        reason: `Matched keyword "${bestKeyword}" in query. ${model.description.slice(0, 80)}...`,
      })
    }
  }

  // Sort by confidence descending
  results.sort((a, b) => b.confidence - a.confidence)
  return results
}

/**
 * Run a model with given inputs.
 */
export function runModel(modelName: string, inputs: Record<string, any>): ModelResult {
  const model = getModel(modelName)
  if (!model) {
    throw new Error(`Model "${modelName}" not found. Available: ${Object.keys(modelRegistry).join(', ')}`)
  }
  return model.compute(inputs)
}

/**
 * Run sensitivity analysis: vary one parameter across a range.
 */
export function runSensitivity(
  modelName: string,
  baseInputs: Record<string, any>,
  varyParam: string,
  values: number[],
): SensitivityResult {
  const model = getModel(modelName)
  if (!model) {
    throw new Error(`Model "${modelName}" not found.`)
  }

  const results: Array<{ param_value: number; outputs: Record<string, number> }> = []

  for (const val of values) {
    const inputs = { ...baseInputs, [varyParam]: val }
    const result = model.compute(inputs)

    // Extract numeric outputs only
    const numOutputs: Record<string, number> = {}
    for (const [k, v] of Object.entries(result.outputs)) {
      if (typeof v === 'number') numOutputs[k] = v
    }
    results.push({ param_value: val, outputs: numOutputs })
  }

  return { model: modelName, vary_param: varyParam, results }
}

/**
 * Get the count of registered models.
 */
export function modelCount(): number {
  return Object.keys(modelRegistry).length
}
