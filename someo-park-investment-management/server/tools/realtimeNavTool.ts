// Someo Agent NAV tool + ordinary-chat grounding. Coding prompts/routes stay untouched.
// Read the same data adapters and use the same display rules as RealtimeNavViewer.
import type { AgentTool } from './index.js'
import {
  buildRealtimeNavPanel, roundedNavMoney, type NavPanelInput,
} from '../../shared/realtimeNav.js'
import { readOfficialEquity } from '../utils/officialEquity.js'
import {
  etToday, readNavLatest, readNavMirror, readNavPrevClose, readNavReconcile, readNavStream,
} from '../utils/controllerNavData.js'

function readPanelInput(nowMs: number): NavPanelInput & { input_errors: Record<string, string> } {
  const latest = readNavLatest()
  const input_errors: Record<string, string> = {}
  const optional = <T>(name: string, read: () => T, fallback: T): T => {
    try { return read() } catch (e) {
      input_errors[name] = String(e)
      return fallback
    }
  }
  const now = new Date(nowMs)
  return {
    latest,
    official: optional('official', readOfficialEquity, null),
    mirror: optional('scalars', readNavMirror, null),
    prevClose: optional('prev-close', () => readNavPrevClose(now), null),
    reconcile: optional('reconcile', () => readNavReconcile(now), null),
    streamRows: optional('stream', () => readNavStream(etToday(now)).rows, []),
    nowMs, input_errors,
  }
}

// Optional input is for a deterministic replay of one panel snapshot, without I/O.
export function buildRealtimeNav(snapshot?: NavPanelInput): any {
  let input: ReturnType<typeof readPanelInput> | NavPanelInput
  try {
    input = snapshot ?? readPanelInput(Date.now())
  } catch (e) {
    return { error: `controller NAV unavailable: ${String(e)}` }
  }
  const panel = buildRealtimeNavPanel(input)
  const { latest: nav, reconcile: rec } = input
  const pf = nav.nodes.find(n => n.kind === 'portfolio')
  const ledgerReturn = (n: typeof pf) => n?.day_return != null
    ? +(n.day_return * 100).toFixed(3) : null
  const amount = (value: number | null) => value === null ? null : roundedNavMoney(value)
  return {
    as_of_utc: nav.ts,
    market: nav.market,
    feed_delay_min: nav.feed_delay_min,
    price_as_of_utc: panel.feed.priceTs,
    price_time_et: panel.feed.priceTimeEt,
    price_label_mode: panel.feed.dead ? 'frozen' : panel.feed.delayMin >= 1 ? 'delayed' : 'live',
    heartbeat_time_et: panel.feed.timeEt,
    heartbeat_age_seconds: panel.feed.ageSeconds,
    heartbeat_state: panel.quality.states.heartbeat,
    quality_checks: {
      dual_engine_match: panel.quality.states.dual_engine_match === 'pass',
      price_fresh: !nav.stale,
      quotes_missing: nav.missing || [],
      structure_sync_error: nav.rebuild_error || null,
      reconcile_verdict: rec?.verdict ?? 'none',
      reconcile_date: rec?.date ?? null,
      reconcile_age_bdays: rec?.age_bdays ?? null,
      reconcile_stale: rec?.stale === true,
      states: panel.quality.states,
      status: panel.quality.status,
      all_pass: panel.quality.allPass,
    },
    basis_note: 'Panel display values and percentages use the same functions as '
      + 'RealtimeNavViewer. MRPT/MTFS: live ledger value minus frozen capital base C; '
      + 'AISS/SSRS/AEUS/BDC: live ledger value times frozen strategy scalar k. '
      + 'No constants are recalculated. If official conversion is unavailable, '
      + 'value falls back to the ledger exactly as the panel does; basis marks this '
      + 'and official_anchored_value is null. Display percentages use the panel EOD '
      + 'baseline; comparison_date is the panel label, while official_eod records '
      + 'the actual baseline date. day_pnl_usd and ledger_day_return_pct are ledger '
      + 'diagnostics, not the panel percentage. rolloff is the historical frozen K '
      + 'memo, not a fresh QC reconciliation and not an input to live NAV. '
      + 'Compare the same as_of_utc snapshot; the panel polls every 45 seconds.',
    portfolio: {
      ...panel.portfolio,
      value: roundedNavMoney(panel.portfolio.value),
      official_anchored_value: amount(panel.portfolio.official_anchored_value),
      day_return_pct: panel.portfolio.day_return_pct === null ? null
        : +panel.portfolio.day_return_pct.toFixed(2),
      ledger_day_return_pct: ledgerReturn(pf),
      day_pnl_usd: pf?.day_pnl ?? null,
      day_pnl_basis: 'ledger',
    },
    strategies: panel.strategies.map(s => {
      const n = nav.nodes.find(n => n.node_id === s.node_id)
      return {
        ...s,
        value: roundedNavMoney(s.value),
        official_anchored_value: amount(s.official_anchored_value),
        day_return_pct: s.day_return_pct === null ? null : +s.day_return_pct.toFixed(2),
        ledger_day_return_pct: ledgerReturn(n),
        day_pnl_usd: n?.day_pnl ?? null,
        day_pnl_basis: 'ledger',
      }
    }),
    mid_layers: nav.nodes.filter(n => n.kind !== 'strategy' && n.kind !== 'portfolio').map(n => {
      const strategy = panel.strategies.find(s => s.children.some(c => c.node_id === n.node_id))
      const display = strategy?.children.find(c => c.node_id === n.node_id)
      return {
        ...display, name: n.display_name, kind: n.kind, strategy: strategy?.strategy ?? null,
        day_return_pct: ledgerReturn(n), ledger_day_return_pct: ledgerReturn(n),
        day_pnl_usd: n.day_pnl ?? null, day_pnl_basis: 'ledger',
      }
    }),
    rolloff: panel.rolloff,
    display_conversion: input.mirror ? {
      scalars: input.mirror.scalars, capital_base: input.mirror.capital_base ?? null,
      frozen_at: input.mirror.frozen_at ?? null,
      scaled_frozen: input.mirror.scaled_frozen ?? null,
    } : null,
    structure_hash: nav.structure_hash,
    last_rebuild_ts: nav.last_rebuild_ts,
    structure_diff: nav.structure_diff || [],
    corp_actions: nav.corp_actions || {},
    input_errors: 'input_errors' in input ? input.input_errors : {},
  }
}

export const realtimeNavTool: AgentTool = {
  definition: {
    name: 'get_realtime_nav',
    description: 'Get CURRENT/realtime NAV using the same data and display rules '
      + 'as the Realtime NAV panel. Returns portfolio and all six strategy card '
      + 'values, card percentages and baseline dates, displayed holdings and QC '
      + 'mirror shares, price timestamps/delay, all panel quality states and the '
      + 'historical frozen K memo. Use value/display_value and display_return '
      + 'when describing the panel; basis identifies official vs ledger fallback. '
      + 'ledger_day_return_pct/day_pnl_usd are separately labelled ledger diagnostics. '
      + 'No re-anchoring or changes to frozen constants. For daily HISTORY use '
      + 'get_strategy_performance instead.',
    input_schema: { type: 'object', properties: {}, required: [] },
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute() {
    return buildRealtimeNav()
  },
}

export async function realtimeNavGrounding(): Promise<string | null> {
  const d = buildRealtimeNav()
  if (d.error) return '\n\n## Realtime NAV (controller): ' + d.error
  return '\n\n## Realtime NAV panel snapshot (authoritative): Use value/display_value '
    + 'and display_return to describe the panel. Respect basis, quality states and '
    + 'as_of_utc; do not substitute ledger diagnostics or historical rolloff values '
    + 'for current panel values. Do not invent missing data.\n' + JSON.stringify(d)
}
