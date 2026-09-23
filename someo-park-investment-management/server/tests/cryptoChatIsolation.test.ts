import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { detectArtifacts } from '../utils/artifactDetector.js'
import { detectCryptoArtifacts, resolveCryptoScope } from '../tools/cryptoTriggers.js'

test('crypto questions resolve to the existing eight panels, without stock or World Cup data', () => {
  const cases = [
    ['FAVE PFME 战绩', 'crypto_performance'],
    ['FAVE 订单', 'crypto_orders'],
    ['W8 当前持仓', 'crypto_positions'],
    ['PFME 结算', 'crypto_settlements'],
    ['FAVE 执行质量', 'crypto_execution'],
    ['BTC 当前报价', 'crypto_markets'],
    ['W7 入场条件', 'crypto_rules'],
    ['PFME 运行健康度', 'crypto_health'],
  ]
  for (const [message, type] of cases) {
    const actual = detectArtifacts(message, 'crypto')
    assert.equal(actual[0]?.type, type, message)
    assert.ok(actual.every(item => item.type.startsWith('crypto_')), message)
  }
  assert.deepEqual(detectArtifacts('W8 当前持仓', 'crypto'), [
    { type: 'crypto_positions', title: '当前持仓', params: { strategyId: 'pfme' } },
  ])
})

test('scope distinguishes each strategy and comparisons; PFME two legs are not two strategies', () => {
  for (const name of ['FAVE', 'w7', '优势侧价值入场']) {
    assert.deepEqual(resolveCryptoScope(name), { strategy: 'fave' })
  }
  for (const name of ['PFME', 'W8', '被动优势侧入场', 'W8 两个 leg', 'PFME both legs']) {
    assert.deepEqual(resolveCryptoScope(name), { strategy: 'pfme' })
  }
  for (const query of ['FAVE PFME', 'W7和W8', 'w7w8', 'both', '比较两个策略', '当前收益', '']) {
    assert.deepEqual(resolveCryptoScope(query), { strategy: 'all' })
  }
  assert.deepEqual(resolveCryptoScope('compare FAVE with both strategies'), { strategy: 'all' })
  assert.ok(detectCryptoArtifacts('FAVE PFME 战绩').every(a => !a.params?.strategyId))
})

test('exact settlement-window tickers use the existing artifact params without guessing from a coin', () => {
  const ticker = 'KXBTC15M-26SEP151245-00'
  assert.deepEqual(resolveCryptoScope(`pfme ${ticker.toLowerCase()}`), { strategy: 'pfme', ticker })
  assert.deepEqual(detectCryptoArtifacts(`W8 ${ticker}`), [
    { type: 'crypto_markets', title: '二元市场', params: { strategyId: 'pfme', ticker } },
  ])
  assert.deepEqual(resolveCryptoScope('FAVE BTC KXBTC15M-26SEP15'), { strategy: 'fave' })
  assert.deepEqual(resolveCryptoScope(`${ticker}-EXTRA`), { strategy: 'all' })
  assert.deepEqual(resolveCryptoScope(`${ticker} KXETH15M-26SEP151245-00`), { strategy: 'all' })
  assert.deepEqual(resolveCryptoScope(`${ticker} ${ticker}`), { strategy: 'all', ticker })
})

test('strategy introductions use rules, and unrelated chat does not auto-open a market', () => {
  assert.deepEqual(detectCryptoArtifacts('介绍一下 PFME'), [
    { type: 'crypto_rules', title: '策略规则与验证', params: { strategyId: 'pfme' } },
  ])
  assert.deepEqual(detectCryptoArtifacts('介绍一下w7w8'), [
    { type: 'crypto_rules', title: '策略规则与验证' },
  ])
  for (const message of ['你好', '你底层用的什么模型', 'what model do you use?', '']) {
    assert.deepEqual(detectArtifacts(message, 'crypto'), [], message)
  }
})

// Golden outputs captured from the pre-crypto detector on 2026-09-15. Keep its
// existing behavior, including broad/no-mode matches, unchanged for other modules.
const EXISTING_CASES: Array<{ q: string; mode?: 'stock' | 'prediction' | 'macro' | 'soccer'; expected: unknown }> = [
  { q: '交易信号 当前持仓', mode: 'stock', expected: [
    { type: 'table', title: 'Trading Signals', params: { strategy: 'mrpt' } },
    { type: 'inventory', title: 'Current Inventory', params: { strategy: 'mrpt' } },
  ] },
  { q: 'MRPT 战绩 风险', mode: 'stock', expected: [] },
  { q: 'AISS 当前持仓', mode: 'stock', expected: [
    { type: 'inventory', title: 'Current Inventory', params: { strategy: 'mrpt' } },
    { type: 'inventory', title: 'AISS Holdings', params: { strategy: 'aiss' } },
  ] },
  { q: '战绩 风险 总览', mode: 'prediction', expected: [
    { type: 'wc_performance', title: 'Accuracy & PnL', params: undefined },
    { type: 'wc_venues', title: 'Venues & API', params: undefined },
    { type: 'wc_overview', title: 'System & Model Notes', params: undefined },
  ] },
  { q: '世界杯冠军概率', mode: 'prediction', expected: [{ type: 'wc_champion', title: 'Champion Odds', params: undefined }] },
  { q: 'CPI 通胀 战绩', mode: 'macro', expected: [
    { type: 'macro_inflation', title: 'Inflation' }, { type: 'macro_performance', title: 'Performance' },
  ] },
  { q: '宏观告警', mode: 'macro', expected: [{ type: 'macro_coverage', title: 'Coverage' }] },
  { q: '英超积分榜', mode: 'soccer', expected: [{ type: 'soccer_league_table', title: 'League Table', params: { league: 'epl' } }] },
  { q: '英超近期比赛', mode: 'soccer', expected: [{ type: 'soccer_predictions', title: "Today's Predictions", params: { league: 'epl' } }] },
  { q: '持仓 战绩', expected: [
    { type: 'inventory', title: 'Current Inventory', params: { strategy: 'mrpt' } },
    { type: 'wc_performance', title: 'Accuracy & PnL', params: undefined },
  ] },
  { q: '你好', mode: 'stock', expected: [] },
]

test('stock, World Cup, Macro, Soccer and legacy no-mode artifact outputs stay unchanged', () => {
  for (const { q, mode, expected } of EXISTING_CASES) {
    assert.deepEqual(detectArtifacts(q, mode), expected, `${mode ?? 'legacy'}: ${q}`)
  }
})

function sourceNodeHash(file: string, prefix: string, thenOnly = false): string {
  const source = fs.readFileSync(new URL(file, import.meta.url), 'utf8')
  const parsed = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true)
  let found: ts.Node | undefined
  const scan = (node: ts.Node) => {
    if (node !== parsed && node.getText(parsed).startsWith(prefix)) {
      found = thenOnly ? (node as ts.IfStatement).thenStatement : node
      return
    }
    ts.forEachChild(node, scan)
  }
  scan(parsed)
  assert.ok(found, `protected code block missing: ${prefix}`)
  return createHash('sha256').update(found.getText(parsed)).digest('hex')
}

test('coding system prompt and both code-generation/edit branches retain their original bytes', () => {
  // Hashes captured from the original AST source spans, not from a /tmp file at
  // test time. The guard permits independent QA imports/branches around them.
  assert.equal(sourceNodeHash('../utils/prompt.ts', 'export function toPrompt('),
    'ecb091e84b8bdc358636c42e0ce961012608d8e8a46b83956dd0ef5a479617da')
  assert.equal(sourceNodeHash('../routes/chat.ts', 'if (isCodeRequest(lastContent)) {', true),
    '54a3538e27bf55a2ca711a339e2a1c2ff800cf10f890c49c317b12017957b72d')
  assert.equal(sourceNodeHash('../routes/morphChat.ts', 'try {\n    const contextualSystemPrompt ='),
    '59ff0e759b754829338506f361ddf89e841bc6367efa110791c55d3169f07219')
})

test('the complete Morph route and its shared prompt builders remain byte-identical', () => {
  // Includes Morph's ordinary-QA fallback, not only the edit prompt. New
  // conversational policy must be opted into by /chat and /agent separately.
  const protectedFiles = {
    '../routes/morphChat.ts': 'a24b6159a2d57bdae63688a31dbbd199b0e89a8f59b5ded90a2475cbcccdaa34',
    '../utils/prompt.ts': '9e2d5ffc9c1f8f1b8a7cc8b2219c31915e63eadb7c6f807dec509c2a6c468f34',
    '../utils/cryptoPrompt.ts': '2d57a1da750601a081ed975e81cc971d27ec8e1e2f9e53ca7f7f19fce064695f',
    '../utils/agentPrompt.ts': '47f3639053cade5434c8c531669ec916ad1c9509cea89aec9a142c82ddca81d2',
  }
  for (const [file, expected] of Object.entries(protectedFiles)) {
    assert.equal(createHash('sha256').update(fs.readFileSync(new URL(file, import.meta.url))).digest('hex'),
      expected, file)
  }
})
