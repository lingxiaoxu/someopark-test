// server/tools/inventoryTool.ts
// Data source: server/routes/inventory.ts — GET /api/inventory/:strategy

import { readJsonFile } from '../utils/fileUtils.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

export const inventoryTool: AgentTool = {
  definition: {
    name: 'get_inventory',
    description: 'Get current open positions (inventory) for a strategy. MRPT/MTFS: pair names, entry dates, prices, hedge ratios, shares. SSRS: sector ETF holdings with weights, shares, cost basis, rebalance history. AISS/AEUS: stock-level book — use the stock_holdings field (tradable individual stocks: ticker, shares, weight, subsector tag); the subsector-level holdings are only a grouping, NOT tradable.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: '"mrpt" (Mean Reversion), "mtfs" (Momentum), "ssrs" (Sector Rotation), "aiss" (AI Semiconductor), or "aeus" (AI Electric Utilities)',
          enum: ['mrpt', 'mtfs', 'ssrs', 'aiss', 'aeus']
        }
      },
      required: ['strategy']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy }) {
    if (strategy === 'ssrs')
      return readJsonFile(getBackendPath('qlib-main/sector_rotation/inventory_sector_rotation.json'))
    if (strategy === 'aiss')
      return readJsonFile(getBackendPath('qlib-main/semiconductor_strategy/inventory_aiss.json'))
    if (strategy === 'aeus')
      return readJsonFile(getBackendPath('qlib-main/electric_utilities_strategy/inventory_aeus.json'))
    return readJsonFile(getBackendPath(`inventory_${strategy}.json`))
  }
}
