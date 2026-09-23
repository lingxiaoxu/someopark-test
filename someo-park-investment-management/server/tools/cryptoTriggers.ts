// Parallel to soccerTriggers: a leaf vocabulary shared by ordinary chat and the
// crypto grounding loader. It neither loads data nor imports artifactDetector.
export type CryptoStrategyScope = 'fave' | 'pfme' | 'all'
export type CryptoScope = { strategy: CryptoStrategyScope; ticker?: string }
export type CryptoTrigger = {
  type: string
  title: string
  params?: { strategyId?: 'fave' | 'pfme'; ticker?: string }
}

const FAVE_NAME = /\b(?:fave|w7)\b|优势侧价值入场/i
const PFME_NAME = /\b(?:pfme|w8)\b|被动优势侧入场/i
const ALL_STRATEGIES = /\bboth\s+strategies\b|\ball\s+strategies\b|\b(?:w7w8|w8w7|favepfme|pfmefave)\b|两个策略|两种策略|全部策略|所有策略|两者/i
const EXACT_TICKER = /(?<![A-Z0-9-])KX(?:BTC|ETH|SOL|DOGE|XRP)15M-\d{2}[A-Z]{3}\d{6}-\d{2}(?![A-Z0-9-])/gi

export function resolveCryptoScope(message: string): CryptoScope {
  const fave = FAVE_NAME.test(message)
  const pfme = PFME_NAME.test(message)
  const strategy = ALL_STRATEGIES.test(message) || fave === pfme
    ? 'all' : fave ? 'fave' : 'pfme'
  // A symbol or a partial ticker does not identify a settlement window. Multiple
  // exact contracts stay unfiltered rather than silently selecting the first.
  const tickers = [...new Set((message.match(EXACT_TICKER) ?? []).map(t => t.toUpperCase()))]
  return { strategy, ...(tickers.length === 1 ? { ticker: tickers[0] } : {}) }
}

export const CRYPTO_KEYWORD_PATTERNS: ReadonlyArray<{
  type: string; title: string; pattern: RegExp
}> = [
  { type: 'crypto_performance', title: '收益与回撤', pattern: /\b(?:performance|pnl|p&l|profit|profits|profitability|drawdown|returns?|track record)\b|收益|盈亏|盈利|赚钱|亏损|转负|回撤|战绩|表现/i },
  { type: 'crypto_orders', title: '订单与成交', pattern: /\b(?:orders?|fills?|trades?|trade history)\b|订单|成交|下单|交易记录|逐笔/i },
  { type: 'crypto_positions', title: '当前持仓', pattern: /\b(?:positions?|inventory|exposure|holdings?)\b|持仓|仓位|库存|敞口/i },
  { type: 'crypto_settlements', title: '结算账本', pattern: /\b(?:settlements?|settled|payouts?|ledger)\b|结算|兑付|账本/i },
  { type: 'crypto_execution', title: '执行质量', pattern: /\b(?:execution|fill rate|slippage|liquidity|partial fill)\b|执行质量|成交率|成交比例|成交分母|流动性|滑点|部分成交|空盘口/i },
  { type: 'crypto_markets', title: '二元市场', pattern: /\b(?:markets?|contracts?|orderbooks?|quotes?|bid|ask)\b|二元市场|合约|盘口|报价|阈值|到期/i },
  { type: 'crypto_rules', title: '策略规则与验证', pattern: /\b(?:rules?|parameters?|methodology|validation|paired|tilted|complete sets?|strategy overview|how it works)\b|策略介绍|策略原理|策略规则|参数|入场条件|退出条件|验收|对照|执行腿|执行leg|怎么交易|怎么做|如何运作/i },
  { type: 'crypto_health', title: '运行与数据健康', pattern: /\b(?:health|alerts?|errors?|heartbeat|rate limit|source hash|stale|gaps?|runtime|process)\b|健康|告警|报警|错误|限流|心跳|进程|中断|缺口|源码|版本差异|数据过期|更新时间|运行状况|运行状态|正常运行/i },
]

export function detectCryptoArtifacts(message: string): CryptoTrigger[] {
  if (!message.trim()) return []
  const scope = resolveCryptoScope(message)
  const params: NonNullable<CryptoTrigger['params']> = {
    ...(scope.strategy === 'all' ? {} : { strategyId: scope.strategy }),
    ...(scope.ticker ? { ticker: scope.ticker } : {}),
  }
  const scoped = ({ type, title }: { type: string; title: string }): CryptoTrigger => ({
    type, title,
    ...(Object.keys(params).length ? { params: { ...params } } : {}),
  })
  const topics = CRYPTO_KEYWORD_PATTERNS.filter(item => item.pattern.test(message))
  if (topics.length) return topics.map(scoped)
  if (scope.ticker) return [scoped({ type: 'crypto_markets', title: '二元市场' })]
  // A bare strategy question opens its existing rules view. Unrelated questions
  // (including the chat model's identity) must not force open a market panel.
  if (FAVE_NAME.test(message) || PFME_NAME.test(message) || ALL_STRATEGIES.test(message)
      || /加密货币策略|加密货币预测市场|\bcrypto strateg(?:y|ies)\b/i.test(message)) {
    return [scoped({ type: 'crypto_rules', title: '策略规则与验证' })]
  }
  return []
}
