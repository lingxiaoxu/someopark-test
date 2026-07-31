// macroMarketDataTool — SomeoAgent skill for the Kalshi macro paper-trading system.
// Parallel to the five predictionMarket* (World Cup) tools; reads the same authoritative
// public/data/macro_*.json exports the macro views render. READ-ONLY; no prompts changed.
import { macroContextForArtifacts, MACRO_ARTIFACT_TYPES } from './macroMarketTool.js'
import type { AgentTool } from './index.js'

export const macroMarketDataTool: AgentTool = {
  definition: {
    name: 'macro_market_data',
    description:
      'Authoritative data from the Someo Park macro prediction-market system (Kalshi paper trading): ' +
      'FOMC decision probabilities (model vs market), CPI/PCE/jobs/claims forecasts with ladders, ' +
      'open paper positions & PnL, model-vs-market divergence, OOS calibration, ops coverage & health. ' +
      'Use for any question about Fed decisions, inflation prints, jobless claims, macro bets or the ' +
      'macro system itself. `view` selects the data slice.',
    input_schema: {
      type: 'object',
      properties: {
        view: {
          type: 'string',
          enum: [...MACRO_ARTIFACT_TYPES],
          description:
            'Data slice: macro_board (all series + next releases), macro_fed (FOMC), ' +
            'macro_inflation, macro_labor, macro_divergence, macro_decisions (positions), ' +
            'macro_performance, macro_calibration (OOS), macro_coverage (ops), macro_risk.',
        },
      },
      required: ['view'],
    },
  },
  execute: async (input: { view?: string }) => {
    const view = (MACRO_ARTIFACT_TYPES as readonly string[]).includes(input?.view ?? '')
      ? (input!.view as (typeof MACRO_ARTIFACT_TYPES)[number])
      : 'macro_board'
    const ctx = await macroContextForArtifacts([view])
    return ctx || 'No macro data available for this view yet.'
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
}
