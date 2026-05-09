// server/tools/signalsTool.ts
// Data source: server/routes/signals.ts — GET /api/signals/latest/:strategy

import { readJsonFile, findLatestFile } from '../utils/fileUtils.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

export const signalsTool: AgentTool = {
  definition: {
    name: 'get_signals',
    description: 'Get latest trading signals for a strategy. MRPT/MTFS: active_signals, flat_signals, excluded_pairs. SSRS: sector composite scores, weights, regime, rebalance actions.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: '"mrpt", "mtfs", "combined", or "ssrs" (Sector Rotation)',
          enum: ['mrpt', 'mtfs', 'combined', 'ssrs']
        }
      },
      required: ['strategy']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy }) {
    if (strategy === 'ssrs') {
      const dir = getBackendPath('qlib-main/sector_rotation/trading_signals')
      const filePath = await findLatestFile(dir, 'sr_daily_report_*.json')
      return readJsonFile(filePath)
    }
    const dir = getBackendPath('trading_signals')
    const pattern = strategy === 'combined'
      ? 'combined_signals_*.json'
      : `${strategy}_signals_*.json`
    const filePath = await findLatestFile(dir, pattern)
    return readJsonFile(filePath)
  }
}
