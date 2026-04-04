// server/tools/walkForwardSummaryTool.ts
// Data source: server/routes/walkForward.ts — GET /api/wf/summary/:strategy

import { readJsonFile, findLatestFile } from '../utils/fileUtils.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

function getWfDir(strategy: string): string {
  return strategy === 'mtfs'
    ? getBackendPath('historical_runs/walk_forward_mtfs')
    : getBackendPath('historical_runs/walk_forward')
}

export const wfSummaryTool: AgentTool = {
  definition: {
    name: 'get_wf_summary',
    description: 'Get walk-forward summary: OOS stats (total_pnl, sharpe, max_dd_pct) and window details for a strategy.',
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
    const filePath = await findLatestFile(dir, 'walk_forward_summary_*.json')
    return readJsonFile(filePath)
  }
}
