// server/tools/statisticsTool.ts
// Pure JS financial statistics (no external dependency)

import type { AgentTool } from './index.js'

export const statisticsTool: AgentTool = {
  definition: {
    name: 'calculate_statistics',
    description: 'Calculate financial statistics on a numeric series (daily returns, PnL values). Returns sharpe, sortino, max_drawdown, calmar, win_rate, var_95, skewness, kurtosis, etc.',
    input_schema: {
      type: 'object',
      properties: {
        values: { type: 'array', items: { type: 'number' }, description: 'Array of numbers (e.g. daily returns)' },
        risk_free_rate: { type: 'number', description: 'Annual risk-free rate (default 0.05)' },
        periods_per_year: { type: 'number', description: 'Periods/year (default 252 for daily)' }
      },
      required: ['values']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ values, risk_free_rate = 0.05, periods_per_year = 252 }) {
    if (!Array.isArray(values) || values.length === 0) throw new Error('values must be non-empty array')
    const n = values.length
    const sorted = [...values].sort((a, b) => a - b)
    const mean = values.reduce((a, b) => a + b, 0) / n
    const variance = values.reduce((a, b) => a + (b - mean) ** 2, 0) / Math.max(n - 1, 1)
    const std = Math.sqrt(variance)
    const median = n % 2 === 0 ? (sorted[n / 2 - 1] + sorted[n / 2]) / 2 : sorted[Math.floor(n / 2)]
    const dailyRf = risk_free_rate / periods_per_year
    const sharpe = std > 0 ? ((mean - dailyRf) / std) * Math.sqrt(periods_per_year) : 0
    const downside = values.filter(v => v < 0)
    const downsideStd = downside.length > 0 ? Math.sqrt(downside.reduce((a, b) => a + b ** 2, 0) / downside.length) : 0
    const sortino = downsideStd > 0 ? ((mean - dailyRf) / downsideStd) * Math.sqrt(periods_per_year) : 0
    let peak = -Infinity, maxDd = 0, equity = 1
    for (const r of values) { equity *= (1 + r); if (equity > peak) peak = equity; maxDd = Math.max(maxDd, (peak - equity) / peak) }
    const totalReturn = values.reduce((acc, r) => acc * (1 + r), 1) - 1
    const annReturn = Math.pow(1 + totalReturn, periods_per_year / n) - 1
    const calmar = maxDd > 0 ? annReturn / maxDd : 0
    const winRate = values.filter(v => v > 0).length / n
    const skewness = n >= 3 ? values.reduce((a, b) => a + ((b - mean) / (std || 1)) ** 3, 0) / n : 0
    const kurtosis = n >= 4 ? values.reduce((a, b) => a + ((b - mean) / (std || 1)) ** 4, 0) / n - 3 : 0
    const idx95 = Math.max(Math.floor(n * 0.05), 0)
    const var95 = -sorted[idx95]
    const cvar95 = idx95 >= 0 ? -sorted.slice(0, idx95 + 1).reduce((a, b) => a + b, 0) / (idx95 + 1) : 0

    return {
      n, mean: +mean.toFixed(6), median: +median.toFixed(6), std: +std.toFixed(6),
      sharpe: +sharpe.toFixed(4), sortino: +sortino.toFixed(4),
      max_drawdown_pct: +(maxDd * 100).toFixed(2), calmar: +calmar.toFixed(4),
      win_rate_pct: +(winRate * 100).toFixed(1), total_return_pct: +(totalReturn * 100).toFixed(2),
      ann_return_pct: +(annReturn * 100).toFixed(2), skewness: +skewness.toFixed(4),
      kurtosis: +kurtosis.toFixed(4), var_95_pct: +(var95 * 100).toFixed(2),
      cvar_95_pct: +(cvar95 * 100).toFixed(2),
      p25: +sorted[Math.floor(n * 0.25)].toFixed(6),
      p75: +sorted[Math.floor(n * 0.75)].toFixed(6),
    }
  }
}
