// server/tools/compareStrategiesTool.ts
// Reference: CC src/tools/AgentTool/AgentTool.tsx (multi-step aggregation)

import { readJsonFile, findLatestFile } from '../utils/fileUtils.js'
import { parseCsvFile } from '../utils/csvParser.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

function getWfDir(s: string) {
  return s === 'mtfs'
    ? getBackendPath('historical_runs/walk_forward_mtfs')
    : getBackendPath('historical_runs/walk_forward')
}

export const compareStrategiesTool: AgentTool = {
  definition: {
    name: 'compare_strategies',
    description: 'Compare MRPT vs MTFS vs SSRS OOS performance side by side. Returns metrics: total_pnl, sharpe, max_dd_pct, win_rate_pct, pair_count, windows.',
    input_schema: { type: 'object', properties: {}, required: [] }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute() {
    const results: Record<string, any> = {}
    for (const strategy of ['mrpt', 'mtfs', 'ssrs']) {
      if (strategy === 'ssrs') {
        try {
          const filePath = getBackendPath('qlib-main/sector_rotation/backtest_results/wf_fold_detail.json')
          const data = await readJsonFile(filePath)
          results.ssrs = {
            ...data.synthetic_metrics,
            n_folds: data.n_folds,
            mean_wfe: data.mean_wfe,
          }
        } catch (err: any) {
          results.ssrs = { error: err.message }
        }
        continue
      }
      const dir = getWfDir(strategy)
      try {
        const summaryFile = await findLatestFile(dir, 'walk_forward_summary_*.json')
        const summary = await readJsonFile(summaryFile)
        const pairFile = await findLatestFile(dir, 'oos_pair_summary_*.csv')
        const pairs = await parseCsvFile(pairFile)
        const wins = pairs.filter((p: any) => parseFloat(p.pnl ?? p.net_pnl ?? 0) > 0).length
        results[strategy] = {
          ...summary.oos_stats,
          pair_count: pairs.length,
          win_rate_pct: pairs.length > 0 ? +(wins / pairs.length * 100).toFixed(1) : 'N/A',
          windows: summary.windows?.length ?? 'N/A',
        }
      } catch (err: any) {
        results[strategy] = { error: err.message }
      }
    }
    const allKeys = Array.from(new Set([...Object.keys(results.mrpt || {}), ...Object.keys(results.mtfs || {}), ...Object.keys(results.ssrs || {})]))
    return {
      as_of: new Date().toLocaleDateString('en-CA'),
      comparison: allKeys.map(k => ({ metric: k, mrpt: results.mrpt?.[k] ?? 'N/A', mtfs: results.mtfs?.[k] ?? 'N/A', ssrs: results.ssrs?.[k] ?? 'N/A' }))
    }
  }
}
