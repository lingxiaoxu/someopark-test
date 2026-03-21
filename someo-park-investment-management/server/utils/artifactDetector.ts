export interface ArtifactTrigger {
  type: string
  title: string
  params?: Record<string, any>
}

// Keywords mapping for all 13 artifact types, supporting EN + ZH
const ARTIFACT_PATTERNS: Array<{
  type: string
  title: string
  keywords: string[]
  params?: Record<string, any>
}> = [
  {
    type: 'pair_universe',
    title: 'Pair Universe',
    keywords: ['pair universe', 'pairs', 'pair selection', '交易对', '配对'],
    params: { strategy: 'mrpt' },
  },
  {
    type: 'wf_summary',
    title: 'Walk-Forward Summary',
    keywords: ['walk-forward summary', 'walk forward summary', 'wf summary', '走前摘要'],
    params: { strategy: 'mrpt' },
  },
  {
    type: 'chart',
    title: 'OOS Equity Curve',
    keywords: ['equity curve', 'oos curve', 'oos equity', '权益曲线', '净值曲线'],
    params: { strategy: 'mrpt' },
  },
  {
    type: 'oos_pair_summary',
    title: 'OOS Pair Summary',
    keywords: ['oos pair summary', 'pair summary', 'pair performance', '样本外配对'],
    params: { strategy: 'mrpt' },
  },
  {
    type: 'wf_grid',
    title: 'DSR Selection Grid',
    keywords: ['dsr', 'dsr grid', 'dsr selection', 'dsr log', '参数选择'],
    params: { strategy: 'mrpt' },
  },
  {
    type: 'table',
    title: 'Trading Signals',
    keywords: ['trading signal', 'signals', 'signal table', '交易信号', '信号'],
    params: { strategy: 'mrpt' },
  },
  {
    type: 'daily_report',
    title: 'Daily Report',
    keywords: ['daily report', 'report', '日报', '每日报告'],
  },
  {
    type: 'inventory',
    title: 'Current Inventory',
    keywords: ['inventory', 'current inventory', 'positions', '库存', '持仓', '当前持仓'],
    params: { strategy: 'mrpt' },
  },
  {
    type: 'inventory_history',
    title: 'Inventory History',
    keywords: ['inventory history', 'position history', '历史持仓', '库存历史'],
    params: { strategy: 'mrpt' },
  },
  {
    type: 'wf_diagnostic',
    title: 'WF Diagnostic',
    keywords: ['diagnostic', 'wf diagnostic', '诊断'],
  },
  {
    type: 'dashboard',
    title: 'Macro Regime Status',
    keywords: ['regime', 'macro regime', 'market regime', '宏观', '市场状态'],
  },
  {
    type: 'portfolio_history',
    title: 'Monitor History',
    keywords: ['monitor history', 'portfolio history', '监控历史'],
  },
  {
    type: 'wf_structure',
    title: 'Walk-Forward File Structure',
    keywords: ['wf structure', 'file structure', 'walk-forward structure', '文件结构'],
  },
]

export function detectArtifacts(message: string): ArtifactTrigger[] {
  if (!message) return []

  const lowerMessage = message.toLowerCase()
  const detected: ArtifactTrigger[] = []

  for (const pattern of ARTIFACT_PATTERNS) {
    for (const keyword of pattern.keywords) {
      if (lowerMessage.includes(keyword.toLowerCase())) {
        // Check MTFS strategy mentions
        const params = { ...pattern.params }
        if (params.strategy && (lowerMessage.includes('mtfs') || lowerMessage.includes('momentum'))) {
          params.strategy = 'mtfs'
        }
        detected.push({
          type: pattern.type,
          title: pattern.title,
          params: Object.keys(params).length > 0 ? params : undefined,
        })
        break // one match per artifact type is enough
      }
    }
  }

  return detected
}
