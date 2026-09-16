// Crypto prediction markets — parallel to predictionMarketTool / soccerMarketTool.
// Both ordinary-chat grounding and SomeoAgent tools read the exact dashboard export.
// No exchange client, credentials, private ledger, or trading operation is accessible here.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { z } from 'zod'
import { SnapshotSchema, type Snapshot, type Strategy, type Performance } from '../../src/crypto-markets/types.js'
import { resultDrawdown } from '../../src/crypto-markets/resultBooks.js'
import type { AgentTool } from './index.js'

const snapshotPath = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../public/data/crypto_prediction/snapshot.json')
const SOURCE = 'public/data/crypto_prediction/snapshot.json'
const MAX_AGE_MS = 180_000
const FUTURE_TOLERANCE_MS = 60_000
const VIEWS = ['summary', 'performance', 'orders', 'positions', 'settlements', 'execution', 'markets', 'rules', 'health'] as const
export const CRYPTO_ARTIFACT_TYPES = VIEWS.filter(v => v !== 'summary').map(v => `crypto_${v}`)

const strategyArg = z.enum(['fave', 'pfme', 'all', 'w7', 'w8']).transform(s => s === 'w7' ? 'fave' : s === 'w8' ? 'pfme' : s)
const commonArgs = {
  strategy: strategyArg.optional().default('all'),
  basis: z.enum(['paper', 'demo', 'prod']).optional().default('paper'),
  ticker: z.string().min(1).max(128).regex(/^[A-Z0-9][A-Z0-9._-]*$/).optional(),
  asset: z.enum(['BTC', 'ETH', 'SOL', 'DOGE', 'XRP']).optional(),
  since: z.string().datetime({ offset: true }).optional(),
  until: z.string().datetime({ offset: true }).optional(),
  page: z.number().int().min(1).max(100_000).optional().default(1),
  limit: z.number().int().min(1).max(50).optional().default(10),
}
const rangeIsOrdered = (a: { since?: string; until?: string }) => !a.since || !a.until || Date.parse(a.since) <= Date.parse(a.until)
const scopeSchema = z.object(commonArgs).strict().refine(rangeIsOrdered, 'since must be at or before until')
const marketArgsSchema = z.object({ ...commonArgs, view: z.enum(VIEWS).default('summary') }).strict().refine(rangeIsOrdered, 'since must be at or before until')
const contractArgsSchema = z.object({ ...commonArgs, ticker: commonArgs.ticker.unwrap() }).strict().refine(rangeIsOrdered, 'since must be at or before until')
type Scope = z.infer<typeof scopeSchema>
type View = typeof VIEWS[number]

export const CRYPTO_ACCOUNTING_NOTE =
  'Paper is the primary strategy evaluation; Demo is an independent execution mirror, not a second paper arm. ' +
  'Prod quotes are read-only market data: Prod execution/results are not connected. Never add paper and Demo PnL. ' +
  'PFME paper is the tilted execution candidate only; the paired control arm is not exported and cannot be inferred from Demo. ' +
  'Preserve status, scope, source_as_of, unresolved_count and verdict. A positive PnL is not proof that the strategy passed validation. ' +
  'Use each source freshness, not generated_at, to describe current results. Missing values are unknown, never zero. ' +
  'Read-only data: none of these tools place, cancel, modify orders, or change strategies.'

function unavailable(code: string, detail: string, metadata: Record<string, unknown> = {}) {
  return { status: 'unavailable' as const, source: SOURCE, code, detail, ...metadata }
}

function invalid(error: z.ZodError) {
  return unavailable('INVALID_ARGUMENT', error.issues.map(i => `${i.path.join('.') || 'input'}: ${i.message}`).join('; '))
}

/** Fixed-path, uncached read. No static build / old file / different module fallback. */
async function loadSnapshot(now: number): Promise<{ snapshot: Snapshot } | ReturnType<typeof unavailable>> {
  let raw: string
  try {
    raw = await fs.promises.readFile(snapshotPath, { encoding: 'utf8', signal: AbortSignal.timeout(5_000) })
  } catch (e: any) {
    return unavailable(e?.name === 'AbortError' || e?.name === 'TimeoutError' ? 'SOURCE_TIMEOUT' : 'SOURCE_UNAVAILABLE',
      'The crypto dashboard snapshot could not be read. Current strategies, prices and results are unavailable; no substitute source was used.')
  }
  let value: unknown
  try { value = JSON.parse(raw) } catch {
    return unavailable('SOURCE_INVALID_JSON', 'The crypto dashboard snapshot is not valid JSON; current data is unavailable.')
  }
  const parsed = SnapshotSchema.safeParse(value)
  if (!parsed.success) return unavailable('SOURCE_INVALID_SCHEMA',
    `Crypto snapshot validation failed at ${parsed.error.issues[0]?.path.join('.') || 'snapshot'}; current data is unavailable.`)
  const at = Date.parse(parsed.data.generated_at)
  if (at > now + FUTURE_TOLERANCE_MS) return unavailable('SOURCE_FUTURE',
    'The crypto snapshot timestamp is in the future; current data cannot be verified.', { generated_at: parsed.data.generated_at })
  if (now - at > MAX_AGE_MS) return unavailable('SOURCE_STALE',
    'The crypto snapshot is over 180 seconds old. Current results and prices are unavailable; this is not evidence of zero activity.',
    { generated_at: parsed.data.generated_at, age_seconds: Math.floor((now - at) / 1000) })
  return { snapshot: parsed.data }
}

function freshness(at: string | null, now: number, threshold = 300) {
  const ts = at === null ? NaN : Date.parse(at)
  const status = !Number.isFinite(ts) ? 'unavailable' : ts > now + FUTURE_TOLERANCE_MS ? 'future'
    : now - ts > threshold * 1000 ? 'stale' : 'current'
  return { status, as_of: at, age_seconds: Number.isFinite(ts) ? Math.max(0, Math.floor((now - ts) / 1000)) : null, stale_after_seconds: threshold }
}

function metadata(snapshot: Snapshot, now: number) {
  return {
    status: 'available', source: SOURCE, schema_version: snapshot.schema_version,
    snapshot_id: snapshot.snapshot_id, generated_at: snapshot.generated_at, checked_at: new Date(now).toISOString(),
    execution_environment: snapshot.execution_environment, market_data_environment: snapshot.market_data_environment,
    prod_execution_enabled: snapshot.prod_execution_enabled, accounting: CRYPTO_ACCOUNTING_NOTE,
    snapshot_issues: snapshot.issues,
  }
}

function identity(s: Strategy) {
  return { id: s.id, acronym: s.acronym, name: s.name, english_name: s.english_name, version: s.version }
}

function summary(p: Performance, now: number) {
  const { curve, ...rest } = p
  return { ...rest, max_drawdown_usd: resultDrawdown(p), curve_points: curve.length, freshness: freshness(p.source_as_of, now) }
}

function selected(snapshot: Snapshot, scope: Scope): Strategy[] {
  return scope.strategy === 'all' ? [snapshot.strategies.fave, snapshot.strategies.pfme] : [snapshot.strategies[scope.strategy]]
}

function inRange(at: string | null, scope: Scope): boolean {
  if (!scope.since && !scope.until) return true
  if (!at || !Number.isFinite(Date.parse(at))) return false
  return (!scope.since || Date.parse(at) >= Date.parse(scope.since)) && (!scope.until || Date.parse(at) <= Date.parse(scope.until))
}

function rowsPage<T>(rows: T[], scope: Scope, match: (row: T) => boolean) {
  const filtered = rows.filter(match)
  const offset = (scope.page - 1) * scope.limit
  return { source_count: rows.length, matching_count: filtered.length, page: scope.page, limit: scope.limit,
    has_more: offset + scope.limit < filtered.length, rows: filtered.slice(offset, offset + scope.limit) }
}

function scopedRow(row: { ticker?: string; asset?: string; at?: string | null; close_at?: string | null }, scope: Scope) {
  return (!scope.ticker || row.ticker === scope.ticker) && (!scope.asset || row.asset === scope.asset) && inRange(row.at ?? row.close_at ?? null, scope)
}

function withPages(s: Strategy, view: View, scope: Scope, now: number): Record<string, unknown> {
  const id = identity(s)
  const result = { ...id, paper: summary(s.paper, now), demo: summary(s.demo, now),
    prod: { status: 'not_connected', net_pnl_usd: null, source_as_of: null },
    paired_control: s.id === 'pfme' ? { status: 'unavailable', reason: 'The paired control book is not included in this export. PFME paper is tilted only.' } : null }
  if (view === 'summary') return { ...result, description: s.description, runtime: runtime(s, now), issues: s.issues }
  if (view === 'performance') return result
  if (view === 'rules') return { ...id, description: s.description, parameters: s.parameters, runtime: runtime(s, now),
    paper_scope: s.paper.scope, paper_verdict: s.paper.verdict, demo_scope: s.demo.scope, paired_control: result.paired_control }
  if (view === 'health') return { ...id, runtime: runtime(s, now), issues: s.issues,
    paper: summary(s.paper, now), demo: summary(s.demo, now) }
  if (view === 'execution') return { ...id, basis: 'demo', execution: s.execution,
    note: 'Entry and exit denominators are separate; all and recent are predefined source windows, not filtered by these row filters.',
    freshness: freshness(s.demo.source_as_of, now) }
  if (view === 'markets') return { ...id, market_data_environment: 'prod', ...rowsPage(s.markets, scope, r => scopedRow(r, scope)),
    quote_freshness: s.markets.filter(r => scopedRow(r, scope)).map(r => ({ ticker: r.ticker, ...freshness(r.quote_at, now, 120) })),
    note: 'Exact ticker identities. YES ask is derived from NO bids and vice versa. These Prod quotes are not Demo executable quotes.' }
  if (view === 'orders') return { ...id, basis: 'demo', scope: s.demo.scope, freshness: freshness(s.demo.source_as_of, now),
    ...rowsPage([...s.orders].sort((a, b) => (b.at ?? '').localeCompare(a.at ?? '')), scope, r => scopedRow(r, scope)) }
  if (view === 'positions') return { ...id, basis: 'demo', scope: s.demo.scope, freshness: freshness(s.demo.source_as_of, now),
    ...rowsPage(s.positions, scope, r => scopedRow(r, scope)) }
  return { ...id, basis: 'demo', scope: s.demo.scope, freshness: freshness(s.demo.source_as_of, now),
    ...rowsPage([...s.settlements].sort((a, b) => b.at!.localeCompare(a.at!)), scope, r => scopedRow(r, scope)),
    note: 'Rows are only this strategy’s attributed Demo settlements. Headline performance is the whole exported book, not a page sum.' }
}

function runtime(s: Strategy, now: number) {
  return s.runtime.map(r => ({ ...r, freshness: freshness(r.as_of, now, r.stale_after_seconds) }))
}

export async function cryptoMarketData(input: unknown = {}): Promise<object> {
  const parsed = marketArgsSchema.safeParse(input)
  if (!parsed.success) return invalid(parsed.error)
  const now = Date.now(), loaded = await loadSnapshot(now)
  if (!('snapshot' in loaded)) return loaded
  const scope = parsed.data
  return { ...metadata(loaded.snapshot, now), view: scope.view, filters: scope,
    strategies: selected(loaded.snapshot, scope).map(s => withPages(s, scope.view, scope, now)) }
}

async function contractData(input: unknown): Promise<object> {
  const parsed = contractArgsSchema.safeParse(input)
  if (!parsed.success) return invalid(parsed.error)
  const now = Date.now(), loaded = await loadSnapshot(now)
  if (!('snapshot' in loaded)) return loaded
  const scope = parsed.data
  return { ...metadata(loaded.snapshot, now), ticker: scope.ticker, filters: scope,
    strategies: selected(loaded.snapshot, scope).map(s => {
      const market = s.markets.find(m => m.ticker === scope.ticker) ?? null
      const orders = rowsPage(s.orders, scope, r => scopedRow(r, scope))
      const positions = rowsPage(s.positions, scope, r => scopedRow(r, scope))
      const settlements = rowsPage(s.settlements, scope, r => scopedRow(r, scope))
      return { ...identity(s), lookup: market || orders.matching_count || positions.matching_count || settlements.matching_count ? 'found' : 'not_in_snapshot',
        market, quote_freshness: freshness(market?.quote_at ?? null, now, 120),
        demo: { source_as_of: s.demo.source_as_of, freshness: freshness(s.demo.source_as_of, now), orders, positions, settlements },
        paper_contract: { status: 'unavailable', reason: 'Per-contract paper transactions are not part of this export.' },
        note: 'An exact ticker absent from the export is not proof that the contract does not exist, has no market, or has an empty book.' }
    }) }
}

async function trackRecordData(input: unknown = {}): Promise<object> {
  const parsed = scopeSchema.safeParse(input)
  if (!parsed.success) return invalid(parsed.error)
  const now = Date.now(), loaded = await loadSnapshot(now)
  if (!('snapshot' in loaded)) return loaded
  const scope = parsed.data
  if (scope.asset || scope.ticker) return unavailable('UNSUPPORTED_FILTER',
    'The exported performance curve is aggregated by strategy and cannot be filtered by asset or ticker. Use the Demo contract/settlement tools for contract records.')
  return { ...metadata(loaded.snapshot, now), basis: scope.basis, filters: scope,
    strategies: selected(loaded.snapshot, scope).map(s => {
      if (scope.basis === 'prod') return { ...identity(s), status: 'not_connected', performance: null, curve: null,
        note: 'Prod trading results are not connected; neither Demo results nor Prod quotes can supply this book.' }
      const p = s[scope.basis]
      const allFiltered = p.curve.filter(point => inRange(point.at, scope))
      const curve = rowsPage(p.curve, scope, point => inRange(point.at, scope))
      return { ...identity(s), performance: summary(p, now), curve,
        filtered_curve: { matching_count: allFiltered.length,
          net_usd: p.status === 'unavailable' || !p.curve.length ? null : allFiltered.reduce((total, point) => total + point.net_usd, 0),
          coverage: p.curve.length ? 'exported_curve_points_only' : 'unavailable',
          first_at: allFiltered[0]?.at ?? null, last_at: allFiltered.at(-1)?.at ?? null },
        note: 'performance is the source’s whole-book summary. filtered_curve sums every matching EXPORTED point, not just this page; export coverage is not guaranteed to span the complete strategy history. An absent curve is unavailable, not zero PnL. Cumulative/drawdown values retain their original whole-book origin. Curve points are time buckets, not individual trades.',
        paired_control: s.id === 'pfme' ? 'Unavailable in this export; paper uses tilted only.' : null }
    }) }
}

/** Ordinary chat gets the same validated data slices as the read-only Agent tools. */
export async function cryptoContextForArtifacts(types: string[], scope: unknown = {}): Promise<string> {
  const parsed = scopeSchema.safeParse(scope)
  if (!parsed.success) return '## Crypto prediction-market data\n' + JSON.stringify(invalid(parsed.error))
  const views = [...new Set(types.map(t => t.replace(/^crypto_/, '')))]
  if (!views.length) views.push('summary', 'rules')
  if (views.some(v => !(VIEWS as readonly string[]).includes(v))) return '## Crypto prediction-market data\n' +
    JSON.stringify(unavailable('INVALID_ARGUMENT', 'Unknown crypto artifact view.'))
  const now = Date.now(), loaded = await loadSnapshot(now)
  if (!('snapshot' in loaded)) return '## Crypto prediction-market data\n' + JSON.stringify(loaded)
  // Deduplicated views, bounded rows per view and two strategy identities maximum.
  // JSON remains valid; no string slicing can cut off an accounting qualifier.
  const payload = { ...metadata(loaded.snapshot, now), filters: parsed.data,
    views: views.map(view => ({ view, strategies: selected(loaded.snapshot, parsed.data)
      .map(s => withPages(s, view as View, parsed.data, now)) })) }
  return '## Crypto prediction-market data (authoritative dashboard export)\n' + JSON.stringify(payload)
}

const properties = {
  strategy: { type: 'string', enum: ['fave', 'pfme', 'all'], description: 'FAVE / PFME or both (default all). Legacy aliases w7/w8 are accepted as input but output names remain FAVE/PFME.' },
  page: { type: 'integer', minimum: 1, description: 'One-based page; totals describe all matching records, not this page.' },
  limit: { type: 'integer', minimum: 1, maximum: 50, description: 'Rows per strategy/view; default 10, maximum 50.' },
  since: { type: 'string', description: 'Optional inclusive ISO timestamp with timezone.' },
  until: { type: 'string', description: 'Optional inclusive ISO timestamp with timezone.' },
}
const readOnly = { isConcurrencySafe: () => true, isReadOnly: () => true }

export const cryptoMarketTool: AgentTool = {
  ...readOnly,
  definition: {
    name: 'get_crypto_market',
    description: 'Read authoritative FAVE / PFME 15-minute crypto binary-market strategy data from the same export as the dashboard. ' +
      'Paper is the primary assessment; Demo orders/positions/settlements are an independent mirror; Prod is read-only quotes with no trading results. ' +
      'Use summary/performance for PnL, rules for the current registration, health for source and process alerts. PFME paper is tilted, not paired control.',
    input_schema: { type: 'object', properties: { ...properties, view: { type: 'string', enum: [...VIEWS] },
      ticker: { type: 'string', description: 'Optional exact Kalshi ticker; never fuzzy market matching.' },
      asset: { type: 'string', enum: ['BTC', 'ETH', 'SOL', 'DOGE', 'XRP'] } }, required: ['view'] },
  },
  execute: cryptoMarketData,
}

export const cryptoContractTool: AgentTool = {
  ...readOnly,
  definition: {
    name: 'get_crypto_contract',
    description: 'Inspect one exact crypto binary Kalshi ticker: exported Prod quote and separate attributed Demo orders, positions, settlements. ' +
      'Missing ticker means absent from this export, not absent at the exchange or an empty book. Does not fetch quotes or trade.',
    input_schema: { type: 'object', properties: { ...properties,
      ticker: { type: 'string', description: 'Exact ticker returned by get_crypto_market; no guessing by coin/date.' } }, required: ['ticker'] },
  },
  execute: contractData,
}

export const cryptoTrackRecordTool: AgentTool = {
  ...readOnly,
  definition: {
    name: 'get_crypto_track_record',
    description: 'Read a strategy’s independent paper (default), Demo or unconnected Prod result book with paginated equity-curve points. ' +
      'Returns whole-book performance and every matching point’s total separately from the current page. PFME paper is tilted; paired control is not exported.',
    input_schema: { type: 'object', properties: { ...properties,
      basis: { type: 'string', enum: ['paper', 'demo', 'prod'], description: 'Independent result book. Default paper; Prod returns not_connected, never Demo PnL.' } }, required: [] },
  },
  execute: trackRecordData,
}
