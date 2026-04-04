// server/tools/inventoryTool.ts
// Data source: server/routes/inventory.ts — GET /api/inventory/:strategy

import { readJsonFile } from '../utils/fileUtils.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

export const inventoryTool: AgentTool = {
  definition: {
    name: 'get_inventory',
    description: 'Get current open positions (inventory) for MRPT or MTFS strategy. Returns pair names, entry dates, entry prices, hedge ratios, shares, days held, allocated capital.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: 'Strategy: "mrpt" (Mean Reversion Pair Trading) or "mtfs" (Multi-Timeframe Strategy)',
          enum: ['mrpt', 'mtfs']
        }
      },
      required: ['strategy']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy }) {
    const filePath = getBackendPath(`inventory_${strategy}.json`)
    return readJsonFile(filePath)
  }
}
