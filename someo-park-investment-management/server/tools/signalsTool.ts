// server/tools/signalsTool.ts
// Data source: server/routes/signals.ts — GET /api/signals/latest/:strategy

import { readJsonFile, findLatestFile } from '../utils/fileUtils.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

export const signalsTool: AgentTool = {
  definition: {
    name: 'get_signals',
    description: 'Get latest trading signals for a strategy. Returns active_signals (pairs with entry/exit signals), flat_signals (no signal), excluded_pairs, and signal_date.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: '"mrpt", "mtfs", or "combined" for merged signals',
          enum: ['mrpt', 'mtfs', 'combined']
        }
      },
      required: ['strategy']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy }) {
    const dir = getBackendPath('trading_signals')
    const pattern = strategy === 'combined'
      ? 'combined_signals_*.json'
      : `${strategy}_signals_*.json`
    const filePath = await findLatestFile(dir, pattern)
    return readJsonFile(filePath)
  }
}
