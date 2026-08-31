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
    description: 'Get walk-forward summary. MRPT/MTFS: OOS stats (total_pnl, sharpe, max_dd_pct) and window details. SSRS: 73-fold WF stats, synthetic OOS metrics, param OOS performance, WFE. AISS/AEUS: anchored WF folds, synthetic OOS metrics, MCPS/DSR per fold, param OOS performance.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: '"mrpt", "mtfs", "ssrs" (Sector Rotation), "aiss" (AI Semiconductor), or "aeus" (AI Electric Utilities)',
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
      const filePath = getBackendPath('qlib-main/sector_rotation/backtest_results/wf_fold_detail.json')
      return readJsonFile(filePath)
    }
    if (strategy === 'aiss') {
      const filePath = getBackendPath('qlib-main/semiconductor_strategy/backtest_results/wf_fold_detail.json')
      return readJsonFile(filePath)
    }
    if (strategy === 'aeus') {
      const filePath = getBackendPath('qlib-main/electric_utilities_strategy/backtest_results/wf_fold_detail.json')
      return readJsonFile(filePath)
    }
    const dir = getWfDir(strategy)
    const filePath = await findLatestFile(dir, 'walk_forward_summary_*.json')
    return readJsonFile(filePath)
  }
}
