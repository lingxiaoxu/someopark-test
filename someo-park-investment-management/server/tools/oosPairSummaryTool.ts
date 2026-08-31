// server/tools/oosPairSummaryTool.ts
// Data source: server/routes/walkForward.ts — GET /api/wf/pair-summary/:strategy

import { findLatestFile, readJsonFile } from '../utils/fileUtils.js'
import { parseCsvFile } from '../utils/csvParser.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

function getWfDir(strategy: string): string {
  return strategy === 'mtfs'
    ? getBackendPath('historical_runs/walk_forward_mtfs')
    : getBackendPath('historical_runs/walk_forward')
}

export const oosPairSummaryTool: AgentTool = {
  definition: {
    name: 'get_oos_pair_summary',
    description: 'MRPT/MTFS: per-pair OOS stats. SSRS: per-param OOS performance by regime. Returns [{pair, pnl, sharpe, max_dd_pct, win_rate, n_trades, n_days}] for MRPT/MTFS.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: 'Strategy: "mrpt", "mtfs", "ssrs", "aiss", or "aeus"',
          enum: ['mrpt', 'mtfs', 'ssrs', 'aiss', 'aeus']
        }
      },
      required: ['strategy']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy }) {
    if (strategy === 'ssrs') {
      const filePath = getBackendPath('qlib-main/sector_rotation/backtest_results/param_oos_by_regime.json')
      return readJsonFile(filePath)
    }
    if (strategy === 'aiss') {
      const filePath = getBackendPath('qlib-main/semiconductor_strategy/backtest_results/param_oos_by_regime.json')
      return readJsonFile(filePath)
    }
    if (strategy === 'aeus') {
      const filePath = getBackendPath('qlib-main/electric_utilities_strategy/backtest_results/param_oos_by_regime.json')
      return readJsonFile(filePath)
    }
    const dir = getWfDir(strategy)
    const filePath = await findLatestFile(dir, 'oos_pair_summary_*.csv')
    return parseCsvFile(filePath)
  }
}
