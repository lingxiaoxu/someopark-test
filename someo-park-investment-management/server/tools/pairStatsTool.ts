// server/tools/pairStatsTool.ts
// Aggregates OOS performance + DSR + inventory + signal for a single pair

import { readJsonFile, findLatestFile } from '../utils/fileUtils.js'
import { parseCsvFile } from '../utils/csvParser.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

function normPair(p: string) { return p.replace('/', '_') }
function getWfDir(s: string) {
  return s === 'mtfs'
    ? getBackendPath('historical_runs/walk_forward_mtfs')
    : getBackendPath('historical_runs/walk_forward')
}

export const pairStatsTool: AgentTool = {
  definition: {
    name: 'get_pair_stats',
    description: `MRPT/MTFS: comprehensive pair stats. SSRS: sector ETF stats from inventory and daily report. AISS: individual stock stats (pass a ticker like "NVDA") from stock_holdings + daily report.
- OOS performance (pnl, sharpe, max_dd, n_trades from walk-forward)
- DSR selection history (how many WF windows this pair was selected)
- Current inventory (in position, days held, entry prices)
- Current signal (direction, z_score)`,
    input_schema: {
      type: 'object',
      properties: {
        pair: { type: 'string', description: 'e.g. "DG_MOS" / "DG/MOS" (MRPT/MTFS), a sector ETF (SSRS), or a stock ticker like "NVDA" (AISS)' },
        strategy: { type: 'string', enum: ['mrpt', 'mtfs', 'ssrs', 'aiss'] }
      },
      required: ['pair', 'strategy']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ pair, strategy }) {
    if (strategy === 'aiss') {
      const inv = await readJsonFile(getBackendPath('qlib-main/semiconductor_strategy/inventory_aiss.json'))
      const stock = inv?.stock_holdings?.[pair] || null
      const signalDir = getBackendPath('qlib-main/semiconductor_strategy/trading_signals')
      let latestReport = null
      try {
        const reportFile = await findLatestFile(signalDir, 'aiss_daily_report_*.json')
        latestReport = await readJsonFile(reportFile)
      } catch {}
      const subsector = stock?.subsectors?.[0] ?? null
      return {
        ticker: pair,
        subsector,
        holding: stock,                                   // shares, weight, last_price, target_value
        subsector_weight: subsector ? (inv?.holdings?.[subsector]?.weight ?? null) : null,
        stock_breakdown: latestReport?.stock_breakdown?.[pair] ?? null,
        regime: latestReport?.regime?.label ?? null,
        as_of: inv?.as_of,
      }
    }
    if (strategy === 'ssrs') {
      const inv = await readJsonFile(getBackendPath('qlib-main/sector_rotation/inventory_sector_rotation.json'))
      const holding = inv?.holdings?.[pair] || null
      const signalDir = getBackendPath('qlib-main/sector_rotation/trading_signals')
      let latestReport = null
      try {
        const reportFile = await findLatestFile(signalDir, 'sr_daily_report_*.json')
        latestReport = await readJsonFile(reportFile)
      } catch {}
      return {
        sector: pair,
        holding,
        composite_score: latestReport?.signals?.find((s: any) => s.sector === pair)?.composite_score ?? null,
        regime: latestReport?.regime?.label ?? null,
        as_of: inv?.as_of,
      }
    }
    const pairKey = normPair(pair)
    const dir = getWfDir(strategy)
    const result: Record<string, any> = { pair: pairKey, strategy }

    // 1. OOS pair summary
    try {
      const f = await findLatestFile(dir, 'oos_pair_summary_*.csv')
      const rows = await parseCsvFile(f)
      result.oos_performance = rows.find((r: any) => normPair(r.pair || `${r.s1}_${r.s2}` || '') === pairKey) ?? null
    } catch { result.oos_performance = null }

    // 2. DSR selection history
    try {
      const f = await findLatestFile(dir, 'dsr_selection_log_*.csv')
      const rows = await parseCsvFile(f)
      const found = rows.filter((r: any) => normPair(r.pair || `${r.s1}_${r.s2}` || '') === pairKey)
      result.dsr_selected_windows = found.length
      result.dsr_last_selected = found[0]?.window_id ?? null
    } catch { result.dsr_selected_windows = null }

    // 3. Inventory
    try {
      const inv = await readJsonFile(getBackendPath(`inventory_${strategy}.json`))
      const p = inv.pairs?.[pairKey] ?? inv.pairs?.[pair] ?? null
      result.inventory = p
        ? { direction: p.direction, entry_date: p.entry_date, days_held: p.days_held, entry_price_s1: p.entry_price_s1, entry_price_s2: p.entry_price_s2 }
        : { direction: null, status: 'flat' }
    } catch { result.inventory = null }

    // 4. Current signal
    try {
      const sigDir = getBackendPath('trading_signals')
      const f = await findLatestFile(sigDir, `${strategy}_signals_*.json`)
      const sigs = await readJsonFile(f)
      const all = [...(sigs.active_signals || []), ...(sigs.flat_signals || []), ...(sigs.excluded_pairs || [])]
      result.current_signal = all.find((s: any) => normPair(s.pair || `${s.s1}_${s.s2}` || '') === pairKey) ?? null
    } catch { result.current_signal = null }

    return result
  }
}
