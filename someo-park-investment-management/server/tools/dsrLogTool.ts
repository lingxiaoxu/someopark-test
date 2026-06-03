// server/tools/dsrLogTool.ts
// Data source: server/routes/walkForward.ts — GET /api/wf/dsr-log/:strategy

import { findLatestFile, readJsonFile } from '../utils/fileUtils.js'
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
    description: 'MRPT/MTFS: DSR selection log. SSRS: WF fold selection detail (73 folds, per-fold selected param and method). AISS: WF fold selection detail (per-fold selected param, MCPS score, DSR p-value, method).',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: 'Strategy: "mrpt", "mtfs", "ssrs", or "aiss"',
          enum: ['mrpt', 'mtfs', 'ssrs', 'aiss']
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
    if (strategy === 'ssrs') {
      const filePath = getBackendPath('qlib-main/sector_rotation/backtest_results/wf_fold_detail.json')
      const data = await readJsonFile(filePath)
      return { folds: data.folds, n_folds: data.n_folds, n_param_sets: data.n_param_sets }
    }
    if (strategy === 'aiss') {
      const filePath = getBackendPath('qlib-main/semiconductor_strategy/backtest_results/wf_fold_detail.json')
      const data = await readJsonFile(filePath)
      return { folds: data.folds, n_folds: data.n_folds, n_param_sets: data.n_param_sets, selection_log: data.selection_log }
    }
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
