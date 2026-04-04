// server/tools/equityCurveTool.ts
// Data source: server/routes/walkForward.ts — GET /api/wf/equity-curve/:strategy

import { findLatestFile } from '../utils/fileUtils.js'
import { parseCsvFile } from '../utils/csvParser.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

function getWfDir(strategy: string): string {
  return strategy === 'mtfs'
    ? getBackendPath('historical_runs/walk_forward_mtfs')
    : getBackendPath('historical_runs/walk_forward')
}

export const equityCurveTool: AgentTool = {
  definition: {
    name: 'get_equity_curve',
    description: 'Get OOS equity curve time series for a strategy. Returns array of {date, equity, daily_return}. Can be large — use tail to get recent data.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: 'Strategy: "mrpt" or "mtfs"',
          enum: ['mrpt', 'mtfs']
        },
        tail: {
          type: 'number',
          description: 'Only return last N data points (default: all)'
        }
      },
      required: ['strategy']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy, tail }) {
    const dir = getWfDir(strategy)
    const filePath = await findLatestFile(dir, 'oos_equity_curve_*.csv')
    const rows = await parseCsvFile(filePath)
    if (tail && tail > 0) return rows.slice(-tail)
    return rows
  }
}
