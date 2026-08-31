// server/tools/inventoryHistoryTool.ts
// Data source: server/routes/inventory.ts — GET /api/inventory/history/:strategy

import { listFiles, readJsonFile, extractTimestamp, getFileStats } from '../utils/fileUtils.js'
import { getBackendPath } from '../config.js'
import path from 'path'
import type { AgentTool } from './index.js'

export const inventoryHistoryTool: AgentTool = {
  definition: {
    name: 'get_inventory_history',
    description: 'Get historical inventory snapshots for a strategy. Returns metadata list (filename, timestamp, size). Use with limit to control result size.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: '"mrpt", "mtfs", "ssrs" (Sector Rotation), "aiss" (AI Semiconductor), or "aeus" (AI Electric Utilities)',
          enum: ['mrpt', 'mtfs', 'ssrs', 'aiss', 'aeus']
        },
        limit: {
          type: 'number',
          description: 'Max number of snapshots to return (default 10, newest first)'
        },
        filename: {
          type: 'string',
          description: 'If provided, return the full content of this specific snapshot file'
        }
      },
      required: ['strategy']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy, limit = 10, filename }) {
    const dir = strategy === 'ssrs'
      ? getBackendPath('qlib-main/sector_rotation/inventory_history')
      : strategy === 'aiss'
      ? getBackendPath('qlib-main/semiconductor_strategy/inventory_history')
      : strategy === 'aeus'
      ? getBackendPath('qlib-main/electric_utilities_strategy/inventory_history')
      : getBackendPath('inventory_history')

    if (filename) {
      const filePath = path.join(dir, path.basename(filename))
      return readJsonFile(filePath)
    }

    const pattern = strategy === 'ssrs'
      ? 'inventory_sector_rotation_*.json'
      : strategy === 'aiss'
      ? 'inventory_aiss_*.json'
      : strategy === 'aeus'
      ? 'inventory_aeus_*.json'
      : `inventory_${strategy}_*.json`
    const files = await listFiles(dir, pattern)
    const sliced = files.slice(0, limit)

    return Promise.all(sliced.map(async (f: string) => {
      const stats = await getFileStats(f)
      return {
        filename: path.basename(f),
        timestamp: extractTimestamp(path.basename(f)),
        size: stats.size,
        mtime: stats.mtime,
      }
    }))
  }
}
