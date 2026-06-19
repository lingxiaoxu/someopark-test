// server/tools/strategyPerformanceTool.ts
// Data source: public/data/master_portfolio_performance.json
// (the full multi-strategy series; the legacy strategy_performance.json only had MRPT/MTFS)

import { readJsonFile } from '../utils/fileUtils.js'
import path from 'path'
import { fileURLToPath } from 'url'
import type { AgentTool } from './index.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

export const strategyPerformanceTool: AgentTool = {
  definition: {
    name: 'get_strategy_performance',
    description: 'Get daily performance time series across ALL strategies. Each row is keyed by `date` with per-strategy fields *_equity / *_pnl / *_dd for: mrpt (MRPT), mtfs (MTFS), sr (SSRS sector rotation), aiss (AISS semiconductor), bdc (BDC private-credit look-through). Also combined_* (= MRPT+MTFS+SSRS+AISS) and master_* (= all incl. BDC), plus benchmark equity spy_equity / smh_equity / soxx_equity / mags_equity. Report on all strategies present, not just MRPT/MTFS. Optionally filter by date range or tail.',
    input_schema: {
      type: 'object',
      properties: {
        start_date: { type: 'string', description: 'Start date filter (YYYY-MM-DD)' },
        end_date: { type: 'string', description: 'End date filter (YYYY-MM-DD)' },
        tail: { type: 'number', description: 'Only return last N entries' }
      },
      required: []
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ start_date, end_date, tail }) {
    const filePath = path.resolve(__dirname, '..', '..', 'public', 'data', 'master_portfolio_performance.json')
    let data = await readJsonFile(filePath)
    if (!Array.isArray(data)) data = data.data || data.rows || []

    if (start_date) data = data.filter((r: any) => r.date >= start_date)
    if (end_date) data = data.filter((r: any) => r.date <= end_date)
    if (tail && tail > 0) data = data.slice(-tail)

    return data
  }
}
