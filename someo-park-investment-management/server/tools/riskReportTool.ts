// server/tools/riskReportTool.ts
// Data source: server/routes/riskReport.ts — keep RISK_SOURCES in sync with that route.

import fs from 'fs'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

// Per-strategy risk report dirs (mirror of routes/riskReport.ts RISK_SOURCES).
// mrpt/mtfs share the root pairs dir (YYYYMMDD_HHMMSS naming);
// ssrs/aiss/aeus use portfolio_ledger dirs (daily YYYYMMDD naming).
const RISK_SOURCES: Record<string, { dir: string; re: RegExp }> = {
  mrpt: { dir: 'trading_signals/risk_management',
          re: /^risk_report_(\d{8})_(\d{6})\.pdf$/ },
  mtfs: { dir: 'trading_signals/risk_management',
          re: /^risk_report_(\d{8})_(\d{6})\.pdf$/ },
  ssrs: { dir: 'qlib-main/sector_rotation/trading_signals/risk_management',
          re: /^risk_report_(\d{8})\.pdf$/ },
  aiss: { dir: 'qlib-main/semiconductor_strategy/trading_signals/risk_management',
          re: /^risk_report_(\d{8})\.pdf$/ },
  aeus: { dir: 'qlib-main/electric_utilities_strategy/trading_signals/risk_management',
          re: /^risk_report_(\d{8})\.pdf$/ },
}

export const riskReportsTool: AgentTool = {
  definition: {
    name: 'get_risk_reports',
    description: 'List available Risk Management report PDFs for a strategy (institutional risk pack: exposure, leverage, VaR/CVaR, concentration, factor/beta, stress, limits, plus balance/income/capital/cash-flow statements and theory diagnostics — risk contribution, FF5+UMD attribution, fat-tail, PSR/DSR, CDaR, Kelly). mrpt/mtfs share the joint pairs pack (trading_signals/risk_management, YYYYMMDD_HHMMSS naming); ssrs/aiss/aeus each have their own dir (qlib-main/<strategy dir>/trading_signals/risk_management, daily YYYYMMDD naming). BDC has no risk pack. The machine-readable JSON (risk_report_<ts>.json) sits next to each PDF — read it with read_file for exact numbers.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: '"mrpt" | "mtfs" (joint pairs pack), "ssrs", "aiss", or "aeus". Default "mrpt".',
          enum: ['mrpt', 'mtfs', 'ssrs', 'aiss', 'aeus']
        }
      },
      required: []
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy }: { strategy?: string } = {}) {
    const src = RISK_SOURCES[strategy || 'mrpt'] || RISK_SOURCES.mrpt
    const dir = getBackendPath(src.dir)
    if (!fs.existsSync(dir)) return { strategy: strategy || 'mrpt', dir: src.dir, reports: [], total: 0 }

    const reports = fs.readdirSync(dir)
      .map(f => {
        const m = f.match(src.re)
        if (!m) return null
        return { date: m[1], timestamp: m[2] ? `${m[1]}_${m[2]}` : m[1] }
      })
      .filter(Boolean)
      .sort((a: any, b: any) => (a.timestamp < b.timestamp ? 1 : -1))  // newest first

    return { strategy: strategy || 'mrpt', dir: src.dir, reports, total: reports.length }
  }
}
