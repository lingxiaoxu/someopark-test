// server/tools/dailyReportTool.ts
// Data source: server/routes/dailyReport.ts

import { readJsonFile, readTextFile, findLatestFile } from '../utils/fileUtils.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

export const dailyReportTool: AgentTool = {
  definition: {
    name: 'get_daily_report',
    description: 'Get latest daily report (JSON). Contains regime analysis, signal summary, position monitoring, and PnL breakdown for both strategies.',
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
    return readJsonFile(filePath)
  }
}

export const dailyReportTextTool: AgentTool = {
  definition: {
    name: 'get_daily_report_text',
    description: 'Get latest daily report as human-readable text. Useful when JSON format is too verbose.',
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
    const filePath = await findLatestFile(dir, 'daily_report_[0-9]*.txt')
    return readTextFile(filePath)
  }
}
