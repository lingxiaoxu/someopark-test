// server/tools/csvTool.ts
// Reuses SP existing: server/utils/xlsxParser.ts + server/utils/csvParser.ts

import path from 'path'
import { parseCsvFile } from '../utils/csvParser.js'
import { listXlsxSheets, parseXlsxSheet } from '../utils/xlsxParser.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

const ALLOWED_ROOT = path.resolve(getBackendPath('.'))

function validatePath(filePath: string): string {
  const resolved = path.resolve(filePath)
  if (!resolved.startsWith(ALLOWED_ROOT)) {
    throw new Error(`Path not allowed. Must be within ${ALLOWED_ROOT}`)
  }
  return resolved
}

export const parseXlsxTool: AgentTool = {
  definition: {
    name: 'parse_data_file',
    description: `Parse CSV or XLSX file. For CSV: returns array of objects. For XLSX: returns sheet data or sheet list.
Path must be within the someopark-test directory.`,
    input_schema: {
      type: 'object',
      properties: {
        file_path: { type: 'string', description: 'File path (absolute or relative to project root)' },
        sheet: { type: 'string', description: 'For XLSX: sheet name to read. Omit to list sheets.' }
      },
      required: ['file_path']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ file_path, sheet }) {
    const resolved = file_path.startsWith('/')
      ? validatePath(file_path)
      : validatePath(getBackendPath(file_path))

    if (resolved.endsWith('.csv')) {
      return parseCsvFile(resolved)
    }

    if (resolved.endsWith('.xlsx')) {
      if (sheet) return parseXlsxSheet(resolved, sheet)
      return listXlsxSheets(resolved)
    }

    throw new Error(`Unsupported format. Use .csv or .xlsx files.`)
  }
}
