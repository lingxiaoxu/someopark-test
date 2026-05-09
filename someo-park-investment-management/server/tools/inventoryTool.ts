// server/tools/inventoryTool.ts
// Data source: server/routes/inventory.ts — GET /api/inventory/:strategy

import { readJsonFile } from '../utils/fileUtils.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

export const inventoryTool: AgentTool = {
  definition: {
    name: 'get_inventory',
    description: 'Get current open positions (inventory) for a strategy. MRPT/MTFS: pair names, entry dates, prices, hedge ratios, shares. SSRS: sector ETF holdings with weights, shares, cost basis, rebalance history.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: '"mrpt" (Mean Reversion), "mtfs" (Momentum), or "ssrs" (Sector Rotation)',
          enum: ['mrpt', 'mtfs', 'ssrs']
        }
      },
      required: ['strategy']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy }) {
    const filePath = strategy === 'ssrs'
      ? getBackendPath('qlib-main/sector_rotation/inventory_sector_rotation.json')
      : getBackendPath(`inventory_${strategy}.json`)
    return readJsonFile(filePath)
  }
}
