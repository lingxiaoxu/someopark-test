export interface ArtifactTrigger {
  type: string
  title: string
  params?: Record<string, any>
}

// Keywords mapping for all 14 artifact types, supporting EN + ZH
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
  {
    type: 'strategy_performance',
    title: 'Strategy Performance',
    keywords: ['strategy performance', 'overall performance', 'performance curve', 'inception', '策略表现', '策略曲线', '整体表现', '净值曲线总览'],
  },
  {
    type: 'risk_report',
    title: 'Risk Report',
    keywords: ['risk report', 'risk management report', 'risk pack', 'var report', 'leverage report', 'exposure report', '风险报告', '风控报告', '风险管理报告'],
  },
  {
    type: 'inventory',
    title: 'AISS Holdings',
    keywords: ['aiss', 'ai semiconductor', 'semiconductor strategy', 'ai chip', 'ai-chips', '半导体策略', 'ai半导体', '芯片策略'],
    params: { strategy: 'aiss' },
  },

  // ── Prediction Market (World Cup 2026) artifacts — open the rich wc_* viewer on the
  //    right when the user asks about these in Prediction Market mode. Keywords kept
  //    specific to avoid colliding with the stock-strategy patterns above.
  { type: 'wc_champion',      title: 'Champion Odds',       keywords: ['champion', 'championship odds', 'win the cup', 'world cup winner', '夺冠', '冠军概率', '夺冠概率', '谁夺冠'] },
  { type: 'wc_golden_boot',   title: 'Golden Boot',         keywords: ['golden boot', 'top scorer', 'top goalscorer', '金靴', '最佳射手'] },
  { type: 'wc_match_pricing', title: 'Match Pricing',       keywords: ['match pricing', 'match odds', '单场定价', '比赛定价', '胜平负'] },
  { type: 'wc_reach_round',   title: 'Reach Round',         keywords: ['reach round', 'advance to', 'reach the final', 'reach semifinal', '晋级', '晋级盘', '进决赛', '进四强'] },
  { type: 'wc_predictions',   title: "Today's Predictions", keywords: ["today's predictions", 'upcoming matches', '今日预测', '近期比赛', '即将开始'] },
  { type: 'wc_inplay',        title: 'In-Play Arbitrage',   keywords: ['in-play', 'in play', 'inplay', 'live match', 'live arbitrage', '盘中套利', '盘中', '实时比赛'] },
  { type: 'wc_divergence',    title: 'Model vs Market',     keywords: ['model vs market', 'divergence', 'vs book', '模型vs市场', '模型 vs 市场', '散度'] },
  { type: 'wc_squad',         title: 'Squad Strength',      keywords: ['squad strength', '阵容强度'] },
  { type: 'wc_form',          title: 'Recent Form',         keywords: ['recent form', '近期状态', '球队状态'] },
  { type: 'wc_styles',        title: 'Team Styles',         keywords: ['team styles', 'playing style', '球队风格', '打法'] },
  { type: 'wc_performance',   title: 'Accuracy & PnL',      keywords: ['accuracy & pnl', 'accuracy and pnl', 'prediction accuracy', 'brier', 'track record', '准确度', '盈亏', '战绩'] },
  { type: 'wc_calibration',   title: 'Calibration',         keywords: ['calibration', 'reliability', '校准'] },
  { type: 'wc_schedule',      title: 'Schedule',            keywords: ['world cup schedule', 'kickoff time', '赛程', '开赛时间'] },
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
