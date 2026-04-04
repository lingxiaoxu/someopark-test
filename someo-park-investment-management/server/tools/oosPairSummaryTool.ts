// server/tools/oosPairSummaryTool.ts
// Data source: server/routes/walkForward.ts — GET /api/wf/pair-summary/:strategy

import { findLatestFile } from '../utils/fileUtils.js'
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
    description: 'Get per-pair OOS performance summary. Returns [{pair, pnl, sharpe, max_dd_pct, win_rate, n_trades, n_days}] for all pairs in the walk-forward.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: 'Strategy: "mrpt" or "mtfs"',
          enum: ['mrpt', 'mtfs']
        }
      },
      required: ['strategy']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy }) {
    const dir = getWfDir(strategy)
    const filePath = await findLatestFile(dir, 'oos_pair_summary_*.csv')
    return parseCsvFile(filePath)
  }
}
