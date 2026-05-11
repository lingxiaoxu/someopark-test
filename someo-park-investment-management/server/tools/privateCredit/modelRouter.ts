// server/tools/privateCredit/modelRouter.ts
// Routes natural language queries to the best matching model(s)
import { getModel, listModels, matchModel } from './index.js'

export interface RouteResult {
  model: string
  confidence: number
  reason: string
  route: 'excel' | 'python' | 'knowledge_base' | 'mixed'
  suggestedTools: string[]
}

/** Route a user query to the best model and tool combination */
export function routeQuery(query: string): RouteResult[] {
  const q = query.toLowerCase()
  const results: RouteResult[] = []

  // 1. Check for KB-only queries (conceptual, no calculation needed)
  const kbKeywords = ['什么是', 'what is', '解释', 'explain', '定义', 'define', '策略是什么',
    '区别', 'difference', '比较概念', 'oaktree', 'goldman', 'blackstone', 'ares',
    '市场趋势', 'market trend', '发展历史', 'history']
  if (kbKeywords.some(kw => q.includes(kw))) {
    results.push({
      model: 'knowledge_base',
      confidence: 0.7,
      reason: 'Conceptual/factual question — search knowledge base first',
      route: 'knowledge_base',
      suggestedTools: ['kb_search', 'kb_read']
    })
  }

  // 2. Check for Python-specific queries
  const pythonKeywords = ['sofr', 'forward rate', '远期利率', 'fred', 'nelson-siegel',
    'credit score', '信用评分', 'stress test', '压力测试', 'floating rate', '浮动利率',
    'portfolio analysis', '组合分析', 'amortization schedule', '摊销表',
    'default probability', '违约概率', 'recovery rate', 'expected loss',
    'yield curve', '收益率曲线', 'pik', 'prepayment', 'day count']
  const pythonMatch = pythonKeywords.filter(kw => q.includes(kw))
  if (pythonMatch.length > 0) {
    const tools: string[] = []
    if (pythonMatch.some(kw => ['sofr', 'forward rate', '远期利率', 'fred', 'yield curve', '收益率曲线'].includes(kw)))
      tools.push('portfolio_forward_rates')
    if (pythonMatch.some(kw => ['credit score', '信用评分', 'default probability', '违约概率', 'recovery rate', 'expected loss'].includes(kw)))
      tools.push('portfolio_credit_risk')
    if (pythonMatch.some(kw => ['stress test', '压力测试'].includes(kw)))
      tools.push('portfolio_stress_test')
    if (pythonMatch.some(kw => ['amortization', '摊销', 'pik', 'prepayment', 'day count', 'floating rate', '浮动利率'].includes(kw)))
      tools.push('portfolio_generate_cashflows')
    if (pythonMatch.some(kw => ['portfolio analysis', '组合分析'].includes(kw)))
      tools.push('portfolio_run_existing')
    if (tools.length === 0) tools.push('portfolio_analyze_deal')

    results.push({
      model: 'python_engine',
      confidence: 0.6 + pythonMatch.length * 0.1,
      reason: `Matched Python keywords: ${pythonMatch.join(', ')}`,
      route: 'python',
      suggestedTools: tools
    })
  }

  // 3. Check for Excel model matches
  const modelMatches = matchModel(query)
  for (const m of modelMatches.slice(0, 3)) {
    const model = getModel(m.model)
    if (!model) continue

    const tools = ['pc_compute']
    if (q.includes('sensitivity') || q.includes('敏感性') || q.includes('what if') || q.includes('如果'))
      tools.push('pc_sensitivity')
    if (q.includes('compare') || q.includes('比较') || q.includes('vs'))
      tools.push('pc_compare_scenarios')

    results.push({
      model: m.model,
      confidence: m.confidence,
      reason: m.reason,
      route: 'excel',
      suggestedTools: tools
    })
  }

  // Sort by confidence
  results.sort((a, b) => b.confidence - a.confidence)
  return results
}
