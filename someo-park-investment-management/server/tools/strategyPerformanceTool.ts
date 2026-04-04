// server/tools/strategyPerformanceTool.ts
// Data source: public/data/strategy_performance.json

import { readJsonFile } from '../utils/fileUtils.js'
import path from 'path'
import { fileURLToPath } from 'url'
import type { AgentTool } from './index.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

export const strategyPerformanceTool: AgentTool = {
  definition: {
    name: 'get_strategy_performance',
    description: 'Get daily strategy performance time series (equity, PnL, allocations). Optionally filter by date range.',
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
    const filePath = path.resolve(__dirname, '..', '..', 'public', 'data', 'strategy_performance.json')
    let data = await readJsonFile(filePath)
    if (!Array.isArray(data)) data = data.data || data.rows || []

    if (start_date) data = data.filter((r: any) => r.date >= start_date)
    if (end_date) data = data.filter((r: any) => r.date <= end_date)
    if (tail && tail > 0) data = data.slice(-tail)

    return data
  }
}
