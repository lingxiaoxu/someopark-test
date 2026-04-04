// server/tools/regimeTool.ts
// Data source: server/routes/regime.ts — GET /api/regime/latest

import { readJsonFile, findLatestFile } from '../utils/fileUtils.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

export const regimeTool: AgentTool = {
  definition: {
    name: 'get_regime',
    description: 'Get current macro regime status. Returns regime_label (Risk-On/Off), regime_score, component_scores, indicators, mrpt_weight, mtfs_weight for capital allocation.',
    input_schema: {
      type: 'object',
      properties: {},
      required: []
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute() {
    const dir = getBackendPath('trading_signals')
    const filePath = await findLatestFile(dir, 'daily_report_[0-9]*.json')
    const report = await readJsonFile(filePath)
    if (!report?.regime) throw new Error('No regime data found in latest daily report')
    return { ...report.regime, signal_date: report.signal_date }
  }
}
