// server/tools/realtimeNavTool.ts
// Realtime NAV(controller 中央估值引擎)— 双供:
//   ① Someo Agent 工具 get_realtime_nav(agent 路由)
//   ② realtimeNavGrounding() 文本块(chat 路由的非 coding 分支注入;
//      coding 模式 prompt 保持纯洁,绝不在此文件之外碰它)
// 只读 controller/output/*,与 RealtimeNavViewer 面板同源同口径。
import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'
import type { AgentTool } from './index.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.join(__dirname, '..', '..', '..')
const OUT = path.join(REPO, 'controller', 'output')
const DATA = path.join(__dirname, '..', '..', 'public', 'data')

// 官方口径锚(与 controller/reconcile_eod._ANCHORS / RealtimeNavViewer 一致)
const ANCHORS: Record<string, { file: string; col: string }> = {
  MRPT: { file: 'strategy_performance.json', col: 'mrpt_equity' },
  MTFS: { file: 'strategy_performance.json', col: 'mtfs_equity' },
  SSRS: { file: 'master_portfolio_performance.json', col: 'sr_equity' },
  AISS: { file: 'master_portfolio_performance.json', col: 'aiss_equity' },
  BDC:  { file: 'private_credit_bdc_performance.json', col: 'bdc_equity' },
}

function officialEod(): Record<string, { date: string; value: number }> {
  const cache: Record<string, any[]> = {}
  const out: Record<string, { date: string; value: number }> = {}
  for (const [st, { file, col }] of Object.entries(ANCHORS)) {
    cache[file] ||= JSON.parse(fs.readFileSync(path.join(DATA, file), 'utf-8'))
    for (let i = cache[file].length - 1; i >= 0; i--) {
      if (cache[file][i][col] != null) {
        out[st] = { date: cache[file][i].date, value: Number(cache[file][i][col]) }
        break
      }
    }
  }
  return out
}

function latestReconcile(): any | null {
  if (!fs.existsSync(OUT)) return null
  const files = fs.readdirSync(OUT).filter(f => f.startsWith('reconcile_')).sort()
  if (!files.length) return null
  return JSON.parse(fs.readFileSync(path.join(OUT, files[files.length - 1]), 'utf-8'))
}

// 与面板同口径的快照:官方锚定值 = official_EOD × (1 + day_return)
export function buildRealtimeNav(): any {
  const p = path.join(OUT, 'nav_latest.json')
  if (!fs.existsSync(p)) {
    return { error: 'controller not running yet (no nav_latest.json)' }
  }
  const nav = JSON.parse(fs.readFileSync(p, 'utf-8'))
  const off = officialEod()
  const rec = latestReconcile()
  const strategies: any[] = []
  let officialSum = 0
  let allOfficial = true
  for (const n of nav.nodes || []) {
    if (n.kind !== 'strategy') continue
    const o = off[n.display_name]
    const r = n.day_return ?? null
    const anchored = o != null && r != null ? o.value * (1 + r)
      : o != null ? o.value : null
    if (anchored == null) allOfficial = false
    else officialSum += anchored
    strategies.push({
      strategy: n.display_name,
      official_anchored_value: anchored != null ? Math.round(anchored) : null,
      official_eod: o ?? null,
      day_return_pct: r != null ? +(r * 100).toFixed(3) : null,
      day_pnl_usd: n.day_pnl ?? null,
      positions_as_of: n.positions_as_of ?? null,
      corp_action: !!n.corp_action,
      holdings: (n.holdings || []).map((h: any) => ({
        ticker: h.name, shares: h.shares })),
    })
  }
  const pf = (nav.nodes || []).find((n: any) => n.kind === 'portfolio')
  const mid = (nav.nodes || []).filter(
    (n: any) => n.kind !== 'strategy' && n.kind !== 'portfolio')
  return {
    as_of_utc: nav.ts,
    market: nav.market,
    feed_delay_min: nav.feed_delay_min,
    quality_checks: {
      dual_engine_match: true,      // nav_latest 只在双引擎对拍通过后发布
      price_fresh: !nav.stale,
      quotes_missing: nav.missing || [],
      structure_sync_error: nav.rebuild_error || null,
      reconcile_verdict: rec?.verdict ?? 'none',
    },
    basis_note: 'Values are OFFICIAL basis: per-strategy official EOD × (1 + '
      + 'intraday day_return). day_return/day_pnl come from the controller '
      + 'dollar account (shares × price; structure-change accounting steps '
      + 'excluded). Same numbers as the Realtime NAV panel.',
    portfolio: {
      official_anchored_value: allOfficial ? Math.round(officialSum) : null,
      day_return_pct: pf?.day_return != null ? +(pf.day_return * 100).toFixed(3) : null,
      day_pnl_usd: pf?.day_pnl ?? null,
    },
    strategies,
    mid_layers: mid.map((n: any) => ({
      name: n.display_name, kind: n.kind,
      day_return_pct: n.day_return != null ? +(n.day_return * 100).toFixed(3) : null,
      day_pnl_usd: n.day_pnl ?? null,
    })),
    structure_hash: nav.structure_hash,
    last_rebuild_ts: nav.last_rebuild_ts,
  }
}

export const realtimeNavTool: AgentTool = {
  definition: {
    name: 'get_realtime_nav',
    description: 'Get LIVE intraday portfolio valuation from the central '
      + 'valuation controller (minute-level, dual-engine verified). Returns '
      + 'official-basis values per strategy (MRPT/MTFS/SSRS/AISS/BDC) and '
      + 'PORTFOLIO: official_anchored_value = official EOD × (1+day_return), '
      + 'day_return/day_pnl from the shares×price dollar account, stock-level '
      + 'holdings, pair/subsector mid-layers, quality checks (dual-engine '
      + 'match, price freshness, position-level reconcile verdict) and '
      + 'structure info. Use for questions about CURRENT/realtime NAV, '
      + 'intraday PnL, or the Realtime NAV panel. For daily HISTORY use '
      + 'get_strategy_performance instead.',
    input_schema: { type: 'object', properties: {}, required: [] },
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute() {
    return buildRealtimeNav()
  },
}

// chat(非 agent)路由的 grounding 文本——与面板同数,防止模型编数字
export async function realtimeNavGrounding(): Promise<string | null> {
  const d = buildRealtimeNav()
  if (d.error) {
    return '\n\n## Realtime NAV (controller): ' + d.error
  }
  return (
    '\n\n## Realtime NAV data (authoritative — live numbers on the Realtime '
    + 'NAV panel the user is seeing; answer ONLY from them, do not invent '
    + 'figures):\n' + JSON.stringify(d)
  )
}
