// server/tools/pnlReportTool.ts
// Data source: server/routes/pnlReport.ts — keep PNL_DIRS in sync with that route.

import fs from 'fs'
import path from 'path'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

// Per-strategy PnL report dirs (mirror of routes/pnlReport.ts PNL_DIRS).
// mrpt/mtfs share the root pairs dir; ssrs/aiss/aeus use portfolio_ledger dirs.
const PNL_DIRS: Record<string, string> = {
  mrpt: 'trading_signals/pnl_reports',
  mtfs: 'trading_signals/pnl_reports',
  ssrs: 'qlib-main/sector_rotation/trading_signals/pnl_reports',
  aiss: 'qlib-main/semiconductor_strategy/trading_signals/pnl_reports',
  aeus: 'qlib-main/electric_utilities_strategy/trading_signals/pnl_reports',
}

export const pnlReportsTool: AgentTool = {
  definition: {
    name: 'get_pnl_reports',
    description: 'List available PnL report PDFs for a strategy. mrpt/mtfs share the joint pairs report (trading_signals/pnl_reports); ssrs/aiss/aeus each have their own portfolio_ledger report dir (qlib-main/<strategy dir>/trading_signals/pnl_reports). BDC has no PnL report PDFs (use get_strategy_performance / portfolio_bdc_holdings instead). Returns date list (YYYYMMDD) plus the directory the PDFs live in.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: '"mrpt" | "mtfs" (joint pairs report), "ssrs", "aiss", or "aeus". Default "mrpt".',
          enum: ['mrpt', 'mtfs', 'ssrs', 'aiss', 'aeus']
        }
      },
      required: []
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy }: { strategy?: string } = {}) {
    const rel = PNL_DIRS[strategy || 'mrpt'] || PNL_DIRS.mrpt
    const dir = getBackendPath(rel)
    if (!fs.existsSync(dir)) return { strategy: strategy || 'mrpt', dir: rel, reports: [], total: 0 }

    const files = fs.readdirSync(dir)
    const pattern = /pnl_report_(\d{8})\.pdf/
    const dates = files
      .map(f => { const m = f.match(pattern); return m ? m[1] : null })
      .filter(Boolean)
      .sort()
      .reverse()

    return { strategy: strategy || 'mrpt', dir: rel, reports: dates, total: dates.length }
  }
}
