import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test, { type TestContext } from 'node:test'
import { fileURLToPath } from 'node:url'
import { SnapshotSchema, type Snapshot, type Strategy } from '../../src/crypto-markets/types.js'
import { cryptoMarketTool, cryptoContractTool, cryptoTrackRecordTool,
  cryptoContextForArtifacts, CRYPTO_ARTIFACT_TYPES } from '../tools/cryptoMarketTool.js'

const NOW = Date.parse('2026-09-15T17:00:00Z')
const iso = (offset = 0) => new Date(NOW + offset).toISOString()
const SNAPSHOT = fileURLToPath(new URL('../../public/data/crypto_prediction/snapshot.json', import.meta.url))
const tick = 'KXBTC15M-26SEP151245-45'

function strategy(id: 'fave' | 'pfme'): Strategy {
  const perf = {
    status: 'verified' as const, source_as_of: iso(-10_000), scope: `${id} independent paper scope`,
    net_pnl_usd: 42, fees_usd: 2, settled_count: 30, open_count: 1,
    curve: [
      { at: iso(-120_000), net_usd: 5, cumulative_usd: 5, drawdown_usd: 0 },
      { at: iso(-90_000), net_usd: -2, cumulative_usd: 3, drawdown_usd: -2 },
      { at: iso(-60_000), net_usd: 7, cumulative_usd: 10, drawdown_usd: 0 },
    ], sample_windows: 12, unresolved_count: 0, verdict: 'Fixed validation has not passed', note: 'Paper queue / fee model only',
  }
  return {
    id, acronym: id.toUpperCase(), name: `${id} name`, english_name: `${id} English`,
    description: '15-minute binary contract strategy', version: 'test_version',
    parameters: [{ label: 'Test rule', value: 'test' }],
    runtime: [{ source: `${id}_observer`, status: 'observing', as_of: iso(-5_000), stale_after_seconds: 300, detail: 'Source heartbeat' }],
    paper: perf, demo: { ...perf, curve: [{ at: iso(-60_000), net_usd: -.25, cumulative_usd: -.25, drawdown_usd: -.25 }],
      status: 'partial', scope: `${id} independent Demo scope`, net_pnl_usd: -.25, unresolved_count: 1, note: 'Actual Demo fills and official settlement' },
    execution: {
      all: window('all'), recent: window('48h'), exits: { all: window('all exits'), recent: window('48h exits') },
    },
    orders: [1, 2, 3].map(n => ({ id: `order-${n}`, ticker: n === 1 ? 'KXETH15M-OTHER' : tick,
      asset: n === 1 ? 'ETH' : 'BTC', at: iso(-n * 10_000), close_at: iso(60_000), side: 'yes', entry: true,
      quantity: 25, filled: n, price: .8, cost_usd: n * .8, fees_usd: .01, status: 'executed', verified: n !== 3 })),
    positions: [{ ticker: tick, asset: 'BTC', close_at: iso(60_000), yes_quantity: 1, no_quantity: 0,
      paired_quantity: 0, net_quantity: 1, cost_usd: .8, fees_usd: .01, exit_status: 'open', verified: false, source_as_of: iso(-20_000) }],
    settlements: [{ ticker: tick, asset: 'BTC', at: iso(-60_000), result: 'yes', quantity: 1,
      cost_usd: .8, fees_usd: .01, payout_usd: 1, net_usd: .19 }],
    markets: [{ ticker: tick, asset: 'BTC', close_at: iso(60_000), threshold: 65000, spot: 65001,
      yes_ask: .8, no_ask: .21, yes_quantity: 25, no_quantity: 0, quote_at: iso(-15_000), quote_source: 'orderbook',
      quote_status: 'one_sided', status: 'active', result: null }],
    issues: [{ source: `${id}_observer`, code: 'SOURCE_HASH_MISMATCH', detail: 'Registered source differs from current disk' }],
  }
}

function window(label: string) {
  return { label, since: iso(-48 * 3600_000), source_as_of: iso(-10_000), scope: 'Attributable Demo entry attempts', note: 'Do not combine entry and exit denominators',
    signals: 100, requested_contracts: 2500, accepted: 20, filled_orders: 10, full_fills: 3, partial_fills: 7, zero_fills: 10,
    filled_contracts: 100, empty_side: 60, unavailable: 20, other_skips: 0 }
}

function snapshot(): Snapshot {
  return { schema_version: 1, snapshot_id: 'fixture', generated_at: iso(-1000), execution_environment: 'demo', market_data_environment: 'prod',
    prod_execution_enabled: false, issues: [], strategies: { fave: strategy('fave'), pfme: strategy('pfme') } }
}

function serve(t: TestContext, value: unknown = snapshot()): string[] {
  const reads: string[] = []
  t.mock.method(Date, 'now', () => NOW)
  t.mock.method(fs.promises, 'readFile', async (filename: any, options: any) => {
    reads.push(String(filename))
    assert.equal(path.resolve(String(filename)), SNAPSHOT)
    assert.equal(options.encoding, 'utf8')
    assert.ok(options.signal instanceof AbortSignal)
    return JSON.stringify(value)
  })
  return reads
}

function parseContext(context: string): any {
  const split = context.indexOf('\n')
  return JSON.parse(context.slice(split + 1))
}

test('all three crypto skills are read-only and declare only data selectors', () => {
  assert.deepEqual([cryptoMarketTool, cryptoContractTool, cryptoTrackRecordTool].map(tool => tool.definition.name),
    ['get_crypto_market', 'get_crypto_contract', 'get_crypto_track_record'])
  for (const tool of [cryptoMarketTool, cryptoContractTool, cryptoTrackRecordTool]) {
    assert.equal(tool.isReadOnly?.(), true)
    assert.equal(tool.isConcurrencySafe?.(), true)
    assert.doesNotMatch(JSON.stringify(tool.definition.input_schema), /api_key|password|private_key|file_path|base_url/)
  }
})

test('ordinary chat and Agent skills read one fixed export and preserve independent result books', async t => {
  const reads = serve(t)
  const result: any = await cryptoMarketTool.execute({ view: 'performance' })
  const ctx = parseContext(await cryptoContextForArtifacts(['crypto_performance']))
  assert.deepEqual(ctx.views[0].strategies, result.strategies)
  assert.equal(reads.length, 2)
  assert.equal(new Set(reads).size, 1)
  assert.deepEqual(result.strategies.map((s: any) => s.id), ['fave', 'pfme'])
  for (const s of result.strategies) {
    assert.equal(s.paper.net_pnl_usd, 42)
    assert.equal(s.demo.net_pnl_usd, -.25)
    assert.equal(s.paper.verdict, 'Fixed validation has not passed')
    assert.equal(s.demo.status, 'partial')
    assert.equal(s.demo.unresolved_count, 1)
    assert.equal(s.prod.net_pnl_usd, null)
    assert.equal(s.paper.max_drawdown_usd, -2)
  }
  assert.equal(result.strategies[1].paired_control.status, 'unavailable')
  assert.match(result.accounting, /PFME paper is the tilted/)
  assert.match(result.accounting, /Never add paper and Demo/)
})

test('unscoped crypto questions receive both strategies and current rule definitions', async t => {
  serve(t)
  const ctx = parseContext(await cryptoContextForArtifacts([]))
  assert.deepEqual(ctx.views.map((v: any) => v.view), ['summary', 'rules'])
  assert.deepEqual(ctx.views[0].strategies.map((s: any) => s.id), ['fave', 'pfme'])
  assert.equal(ctx.views[1].strategies[1].parameters[0].label, 'Test rule')
})

test('legacy aliases resolve explicitly; invalid arguments are rejected before source access', async t => {
  const reads = serve(t)
  const a: any = await cryptoMarketTool.execute({ view: 'summary', strategy: 'w8' })
  assert.deepEqual(a.strategies.map((s: any) => s.id), ['pfme'])
  const ctx = parseContext(await cryptoContextForArtifacts(['crypto_rules'], { strategy: 'w7' }))
  assert.equal(ctx.views[0].strategies[0].id, 'fave')
  const before = reads.length
  for (const input of [{ strategy: 'mrpt' }, { view: 'macro_board' }, { limit: 0 }, { page: 1.5 }, { ticker: '../../.env' },
    { since: 'yesterday' }, { since: iso(1000), until: iso() }, { path: '/tmp/secret.json' }]) {
    const r: any = await cryptoMarketTool.execute(input)
    assert.equal(r.code, 'INVALID_ARGUMENT', JSON.stringify(input))
    assert.equal(r.strategies, undefined)
  }
  assert.equal(reads.length, before)
  assert.equal(parseContext(await cryptoContextForArtifacts(['wc_performance'])).code, 'INVALID_ARGUMENT')
  assert.equal(parseContext(await cryptoContextForArtifacts([], { strategy: 'unknown' })).code, 'INVALID_ARGUMENT')
})

test('row filters run before pagination and retain unknown verification states', async t => {
  serve(t)
  const r: any = await cryptoMarketTool.execute({ view: 'orders', strategy: 'fave', asset: 'BTC', limit: 1, page: 2,
    since: iso(-35_000), until: iso(-15_000) })
  const page = r.strategies[0]
  assert.equal(page.source_count, 3)
  assert.equal(page.matching_count, 2)
  assert.equal(page.rows.length, 1)
  assert.equal(page.rows[0].id, 'order-3')
  assert.equal(page.rows[0].verified, false)
  assert.equal(page.has_more, false)
  assert.equal(page.basis, 'demo')
})

test('contract lookup is exact and does not turn an absent export row into an empty venue', async t => {
  serve(t)
  const r: any = await cryptoContractTool.execute({ ticker: tick, strategy: 'pfme', limit: 1 })
  assert.equal(r.strategies[0].lookup, 'found')
  assert.equal(r.strategies[0].market.ticker, tick)
  assert.equal(r.strategies[0].demo.orders.matching_count, 2)
  assert.equal(r.strategies[0].demo.positions.rows[0].verified, false)
  assert.equal(r.strategies[0].paper_contract.status, 'unavailable')
  const absent: any = await cryptoContractTool.execute({ ticker: tick + '-WRONG' })
  assert.ok(absent.strategies.every((s: any) => s.lookup === 'not_in_snapshot' && s.market === null))
  assert.match(absent.strategies[0].note, /not proof.*does not exist/)
})

test('track record totals sum the full filtered export, retain original cumulative origin, and never claim full history coverage', async t => {
  serve(t)
  const r: any = await cryptoTrackRecordTool.execute({ strategy: 'fave', since: iso(-100_000), page: 2, limit: 1 })
  const s = r.strategies[0]
  assert.equal(r.basis, 'paper')
  assert.equal(s.performance.net_pnl_usd, 42)
  assert.equal(s.filtered_curve.net_usd, 5)
  assert.equal(s.filtered_curve.matching_count, 2)
  assert.equal(s.filtered_curve.coverage, 'exported_curve_points_only')
  assert.equal(s.curve.rows[0].net_usd, 7)
  assert.equal(s.curve.rows[0].cumulative_usd, 10)
  assert.match(s.note, /not guaranteed to span the complete strategy history/)
  const demo: any = await cryptoTrackRecordTool.execute({ strategy: 'fave', basis: 'demo' })
  assert.equal(demo.strategies[0].performance.net_pnl_usd, -.25)
  assert.equal(demo.strategies[0].performance.unresolved_count, 1)
  const unsupported: any = await cryptoTrackRecordTool.execute({ ticker: tick })
  assert.equal(unsupported.code, 'UNSUPPORTED_FILTER')
})

test('missing curve and unconnected Prod results never turn into zero or borrowed Demo profit', async t => {
  const data = snapshot()
  data.strategies.pfme.paper.curve = []
  serve(t, data)
  const paper: any = await cryptoTrackRecordTool.execute({ strategy: 'pfme' })
  assert.equal(paper.strategies[0].performance.net_pnl_usd, 42)
  assert.equal(paper.strategies[0].filtered_curve.net_usd, null)
  assert.equal(paper.strategies[0].filtered_curve.coverage, 'unavailable')
  const prod: any = await cryptoTrackRecordTool.execute({ basis: 'prod' })
  for (const s of prod.strategies) {
    assert.equal(s.status, 'not_connected')
    assert.equal(s.performance, null)
    assert.equal(s.curve, null)
  }
  assert.doesNotMatch(JSON.stringify(prod.strategies), /42|-0\.25/)
})

test('fresh publication time does not conceal old or unavailable book, runtime, and quote timestamps', async t => {
  const data = snapshot()
  data.strategies.fave.paper.source_as_of = iso(-600_000)
  data.strategies.fave.demo.source_as_of = null
  data.strategies.fave.runtime[0].as_of = iso(-600_000)
  data.strategies.fave.markets[0].quote_at = iso(-300_000)
  data.strategies.pfme.paper.source_as_of = iso(120_000)
  serve(t, data)
  const r: any = await cryptoMarketTool.execute({ view: 'summary' })
  assert.equal(r.strategies[0].paper.freshness.status, 'stale')
  assert.equal(r.strategies[0].demo.freshness.status, 'unavailable')
  assert.equal(r.strategies[0].runtime[0].freshness.status, 'stale')
  assert.equal(r.strategies[1].paper.freshness.status, 'future')
  const market: any = await cryptoMarketTool.execute({ view: 'markets', strategy: 'fave' })
  assert.equal(market.strategies[0].quote_freshness[0].status, 'stale')
})

for (const [label, mutate, code] of [
  ['expired snapshot', (s: any) => { s.generated_at = iso(-181_000) }, 'SOURCE_STALE'],
  ['future snapshot', (s: any) => { s.generated_at = iso(61_000) }, 'SOURCE_FUTURE'],
  ['schema drift', (s: any) => { s.extra = 'unexpected' }, 'SOURCE_INVALID_SCHEMA'],
  ['wrong strategy identity', (s: any) => { s.strategies.pfme.id = 'fave' }, 'SOURCE_INVALID_SCHEMA'],
  ['unexpected Prod trading', (s: any) => { s.prod_execution_enabled = true }, 'SOURCE_INVALID_SCHEMA'],
] as const) {
  test(`${label} is explicitly unavailable in tools and ordinary chat`, async t => {
    const data = snapshot()
    mutate(data)
    serve(t, data)
    const r: any = await cryptoMarketTool.execute({ view: 'performance' })
    assert.equal(r.code, code)
    assert.equal(r.strategies, undefined)
    const ctx = parseContext(await cryptoContextForArtifacts(['crypto_performance']))
    assert.equal(ctx.code, code)
    assert.equal(ctx.views, undefined)
  })
}

test('missing source, timed-out reads and invalid JSON fail without credential paths or substitute data', async t => {
  t.mock.method(Date, 'now', () => NOW)
  const mock = t.mock.method(fs.promises, 'readFile', async () => { throw Object.assign(new Error('private path should not leak'), { code: 'ENOENT' }) })
  const missing: any = await cryptoMarketTool.execute({ view: 'summary' })
  assert.equal(missing.code, 'SOURCE_UNAVAILABLE')
  assert.doesNotMatch(JSON.stringify(missing), /private path/)
  mock.mock.mockImplementation(async () => { throw Object.assign(new Error('timeout'), { name: 'AbortError' }) })
  const timeout: any = await cryptoTrackRecordTool.execute({})
  assert.equal(timeout.code, 'SOURCE_TIMEOUT')
  mock.mock.mockImplementation(async () => '{broken' as any)
  const malformed: any = await cryptoContractTool.execute({ ticker: tick })
  assert.equal(malformed.code, 'SOURCE_INVALID_JSON')
})

test('current real publication validates and is read consistently without rewriting source data', async t => {
  const raw = await fs.promises.readFile(SNAPSHOT, 'utf8')
  const actual = SnapshotSchema.parse(JSON.parse(raw))
  // Freeze the publication being checked, so its moving live file cannot race assertions.
  serve(t, actual)
  t.mock.method(Date, 'now', () => Date.parse(actual.generated_at) + 1000)
  const r: any = await cryptoMarketTool.execute({ view: 'performance' })
  assert.equal(r.status, 'available')
  for (const id of ['fave', 'pfme'] as const) {
    const row = r.strategies.find((s: any) => s.id === id)
    assert.equal(row.paper.net_pnl_usd, actual.strategies[id].paper.net_pnl_usd)
    assert.equal(row.demo.net_pnl_usd, actual.strategies[id].demo.net_pnl_usd)
    assert.equal(row.paper.scope, actual.strategies[id].paper.scope)
  }
  const context = await cryptoContextForArtifacts([...CRYPTO_ARTIFACT_TYPES])
  const ctx = parseContext(context)
  assert.equal(ctx.views.length, 8)
  assert.ok(context.length < 100_000, `All 8 views must remain bounded, got ${context.length}`)
  assert.doesNotMatch(context, /\[truncated\]/)
})
