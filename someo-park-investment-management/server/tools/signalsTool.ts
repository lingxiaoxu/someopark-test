// server/tools/signalsTool.ts
// Data source: server/routes/signals.ts — GET /api/signals/latest/:strategy

import { readJsonFile, findLatestFile } from '../utils/fileUtils.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

export const signalsTool: AgentTool = {
  definition: {
    name: 'get_signals',
    description: 'Get latest trading signals for a strategy. MRPT/MTFS: active_signals, flat_signals, excluded_pairs. SSRS: sector composite scores, weights, regime, rebalance actions. AISS/AEUS: subsector composite scores + stock_holdings/stock_trades (tradable individual stocks), regime, rebalance decision.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: '"mrpt", "mtfs", "combined", "ssrs" (Sector Rotation), "aiss" (AI Semiconductor), or "aeus" (AI Electric Utilities)',
          enum: ['mrpt', 'mtfs', 'combined', 'ssrs', 'aiss', 'aeus']
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
    if (strategy === 'aiss') {
      const dir = getBackendPath('qlib-main/semiconductor_strategy/trading_signals')
      const filePath = await findLatestFile(dir, 'aiss_daily_report_*.json')
      return readJsonFile(filePath)
    }
    if (strategy === 'aeus') {
      const dir = getBackendPath('qlib-main/electric_utilities_strategy/trading_signals')
      const filePath = await findLatestFile(dir, 'aeus_daily_report_*.json')
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
