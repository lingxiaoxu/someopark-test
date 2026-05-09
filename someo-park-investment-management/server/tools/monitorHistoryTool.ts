// server/tools/monitorHistoryTool.ts
// Data source: server/routes/monitorHistory.ts

import path from 'path'
import { listFiles, extractTimestamp, deduplicateLatest } from '../utils/fileUtils.js'
import { listXlsxSheets, parseXlsxSheet } from '../utils/xlsxParser.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

export const monitorHistoryTool: AgentTool = {
  definition: {
    name: 'get_monitor_history',
    description: `Get portfolio monitoring history files. Three modes:
1. No filename → list all monitor XLSX files (deduplicated, newest first)
2. filename + no sheet → list sheets in that file
3. filename + sheet → return sheet data as JSON rows`,
    input_schema: {
      type: 'object',
      properties: {
        strategy: { type: 'string', description: 'Filter by strategy: "mrpt", "mtfs", or "ssrs"', enum: ['mrpt', 'mtfs', 'ssrs'] },
        pair: { type: 'string', description: 'Filter by pair (e.g. "AWK_FOX")' },
        filename: { type: 'string', description: 'Specific XLSX filename to read' },
        sheet: { type: 'string', description: 'Sheet name within the XLSX file' },
        limit: { type: 'number', description: 'Max files to return (default 20)' }
      },
      required: []
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy, pair, filename, sheet, limit = 20 }) {
    const dir = strategy === 'ssrs'
      ? getBackendPath('historical_runs/sector_rotation')
      : getBackendPath('trading_signals/monitor_history')

    if (filename) {
      const filePath = path.join(dir, path.basename(filename))
      if (sheet) {
        return parseXlsxSheet(filePath, sheet)
      }
      return listXlsxSheets(filePath)
    }

    let pattern = 'monitor_*.xlsx'
    if (strategy === 'ssrs') {
      pattern = 'monitor_sr_*.xlsx'
    } else if (strategy && pair) {
      pattern = `monitor_${strategy}_${pair}_*.xlsx`
    } else if (strategy) {
      pattern = `monitor_${strategy}_*.xlsx`
    }

    const files = await listFiles(dir, pattern)
    const deduped = deduplicateLatest(files)
    const sliced = deduped.slice(0, limit)

    return sliced.map((f: string) => {
      const base = path.basename(f)
      return { filename: base, timestamp: extractTimestamp(base) }
    })
  }
}
