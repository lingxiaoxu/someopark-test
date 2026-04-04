// server/tools/diagnosticTool.ts
// Data source: server/routes/diagnostic.ts

import { findLatestFile, listFiles, extractTimestamp, deduplicateLatest } from '../utils/fileUtils.js'
import { listXlsxSheets, parseXlsxSheet } from '../utils/xlsxParser.js'
import { getBackendPath } from '../config.js'
import path from 'path'
import type { AgentTool } from './index.js'

export const diagnosticTool: AgentTool = {
  definition: {
    name: 'get_wf_diagnostic',
    description: `Get walk-forward diagnostic XLSX data. Two modes:
1. No sheet → list all sheets in the latest diagnostic file
2. sheet name → return that sheet's data as JSON rows`,
    input_schema: {
      type: 'object',
      properties: {
        sheet: { type: 'string', description: 'Sheet name to read. Omit to list available sheets.' }
      },
      required: []
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ sheet }) {
    const dir = getBackendPath('historical_runs')
    const filePath = await findLatestFile(dir, 'wf_diagnostic_*.xlsx')

    if (sheet) {
      return parseXlsxSheet(filePath, sheet)
    }
    return listXlsxSheets(filePath)
  }
}
