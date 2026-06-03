// server/tools/riskReportTool.ts
// Data source: server/routes/riskReport.ts

import fs from 'fs'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

export const riskReportsTool: AgentTool = {
  definition: {
    name: 'get_risk_reports',
    description: 'List available Risk Management report PDFs (institutional risk pack: exposure, leverage, VaR/CVaR, concentration, factor/beta, stress, limits, plus balance/income/capital/cash-flow statements and theory diagnostics — risk contribution, FF5+UMD attribution, fat-tail, PSR/DSR, CDaR, Kelly). Returns timestamp list (YYYYMMDD_HHMMSS). The PDF/JSON/XLSX themselves are in trading_signals/risk_management/; the machine-readable JSON (risk_report_<ts>.json) can be read with read_file for exact numbers.',
    input_schema: {
      type: 'object',
      properties: {},
      required: []
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute() {
    const dir = getBackendPath('trading_signals/risk_management')
    if (!fs.existsSync(dir)) return { reports: [], total: 0 }

    const pattern = /^risk_report_(\d{8})_(\d{6})\.pdf$/
    const reports = fs.readdirSync(dir)
      .map(f => { const m = f.match(pattern); return m ? { date: m[1], timestamp: `${m[1]}_${m[2]}` } : null })
      .filter(Boolean)
      .sort((a: any, b: any) => (a.timestamp < b.timestamp ? 1 : -1))  // newest first

    return { reports, total: reports.length }
  }
}
