// server/tools/dsrLogTool.ts
// Data source: server/routes/walkForward.ts — GET /api/wf/dsr-log/:strategy

import { findLatestFile } from '../utils/fileUtils.js'
import { parseCsvFile } from '../utils/csvParser.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

function getWfDir(strategy: string): string {
  return strategy === 'mtfs'
    ? getBackendPath('historical_runs/walk_forward_mtfs')
    : getBackendPath('historical_runs/walk_forward')
}

export const dsrLogTool: AgentTool = {
  definition: {
    name: 'get_dsr_log',
    description: 'Get DSR (Dynamic Selection Ratio) parameter selection log. Shows which parameters were selected for each walk-forward window and pair.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: 'Strategy: "mrpt" or "mtfs"',
          enum: ['mrpt', 'mtfs']
        },
        pair: {
          type: 'string',
          description: 'Filter by pair name (e.g. "DG_MOS"). If omitted, returns all pairs.'
        }
      },
      required: ['strategy']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy, pair }) {
    const dir = getWfDir(strategy)
    const filePath = await findLatestFile(dir, 'dsr_selection_log_*.csv')
    const rows = await parseCsvFile(filePath)
    if (pair) {
      const norm = pair.replace('/', '_')
      return rows.filter((r: any) => {
        const rPair = (r.pair || `${r.s1}_${r.s2}` || '').replace('/', '_')
        return rPair === norm
      })
    }
    return rows
  }
}
