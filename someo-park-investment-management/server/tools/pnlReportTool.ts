// server/tools/pnlReportTool.ts
// Data source: server/routes/pnlReport.ts

import fs from 'fs'
import path from 'path'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

export const pnlReportsTool: AgentTool = {
  definition: {
    name: 'get_pnl_reports',
    description: 'List available PnL report PDFs. Returns date list (YYYYMMDD format). PDFs themselves are binary; this tool only lists available dates.',
    input_schema: {
      type: 'object',
      properties: {},
      required: []
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute() {
    const dir = getBackendPath('trading_signals/pnl_reports')
    if (!fs.existsSync(dir)) return { reports: [], total: 0 }

    const files = fs.readdirSync(dir)
    const pattern = /pnl_report_(\d{8})\.pdf/
    const dates = files
      .map(f => { const m = f.match(pattern); return m ? m[1] : null })
      .filter(Boolean)
      .sort()
      .reverse()

    return { reports: dates, total: dates.length }
  }
}
