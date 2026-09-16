import assert from 'node:assert/strict'
import fs from 'node:fs'
import childProcess from 'node:child_process'
import { syncBuiltinESMExports } from 'node:module'
import { createServer, type Server } from 'node:http'
import test from 'node:test'
import express from 'express'
import chatRouter from '../routes/chat.js'
import morphChatRouter from '../routes/morphChat.js'
import { toChatPrompt, toPrompt } from '../utils/prompt.js'
import { withConversationStructure } from '../utils/conversationPrompt.js'
import { cryptoChatGrounding } from '../utils/cryptoPrompt.js'
import { detectArtifacts } from '../utils/artifactDetector.js'
import templates from '../../src/lib/templates.js'
import { SnapshotSchema } from '../../src/crypto-markets/types.js'

const AS_OF = new Date().toISOString()
const performance = (net: number) => ({
  status: 'verified', source_as_of: AS_OF, scope: 'request-test fixture',
  net_pnl_usd: net, fees_usd: 0.12, settled_count: 3, open_count: 0,
  curve: [{ at: AS_OF, net_usd: net, cumulative_usd: net, drawdown_usd: 0 }],
  sample_windows: 3, unresolved_count: 0, verdict: 'fixture', note: 'fixture data only',
})
const execution = {
  label: 'fixture', since: AS_OF, source_as_of: AS_OF, scope: 'fixture', note: '',
  signals: 3, requested_contracts: 75, accepted: 2, filled_orders: 1,
  full_fills: 1, partial_fills: 0, zero_fills: 1, filled_contracts: 25,
  empty_side: 1, unavailable: 0, other_skips: 0,
}
const strategy = (id: 'fave' | 'pfme', paper: number, demo: number) => ({
  id, acronym: id.toUpperCase(), name: `${id} fixture`, english_name: `${id} fixture`,
  description: 'fixture strategy', version: 'fixture-v1', parameters: [], runtime: [],
  paper: performance(paper), demo: performance(demo),
  execution: { all: execution, recent: execution, exits: { all: execution, recent: execution } },
  orders: [], positions: [], settlements: [], markets: [], issues: [],
})
const snapshot = SnapshotSchema.parse({
  schema_version: 1, snapshot_id: 'crypto-chat-request-fixture', generated_at: AS_OF,
  execution_environment: 'demo', market_data_environment: 'prod', prod_execution_enabled: false,
  issues: [], strategies: {
    fave: strategy('fave', 7123.45, 234.56),
    pfme: strategy('pfme', -123.45, -2.34),
  },
})
const MODEL = {
  id: 'request-test-local-model', name: 'Request Test Local Model',
  provider: 'Ollama', providerId: 'ollama',
}
const codeArtifact = {
  commentary: 'Fixture code', template: 'code-interpreter-v1', title: 'Fixture',
  description: 'Fixture output', additional_dependencies: [], has_additional_dependencies: false,
  install_dependencies_command: '', port: null, file_path: 'main.py', code: 'print(1)',
}

function assertConversationStructure(system: string, activeMode?: string): void {
  assert.match(system, /Someo Park Local Model 120B/)
  assert.match(system, /## Application modules and strategy ownership/)
  assert.match(system, /answer with exactly this user-facing name: Someo Park Local Model 120B/)
  if (activeMode) assert.match(system, new RegExp(`Active application module: ${activeMode} —`))
  const stockSection = system.split('### 1.')[1]?.split('### 2.')[0] ?? ''
  const cryptoSection = system.split('### 5.')[1]?.split('## Current module')[0] ?? ''
  assert.match(stockSection, /Strategies: MRPT, MTFS, SSRS, AISS, AEUS, BDC\./)
  assert.doesNotMatch(stockSection, /FAVE|PFME/)
  assert.match(cryptoSection, /Strategies: FAVE/)
  assert.match(cryptoSection, /PFME/)
  for (const strategy of ['MRPT', 'MTFS', 'SSRS', 'AISS', 'AEUS', 'BDC', 'FAVE', 'PFME']) {
    assert.match(system, new RegExp(`\\b${strategy}\\b`), `${strategy} is represented in the module hierarchy`)
  }
  for (const module of ['stock', 'macro', 'prediction', 'soccer', 'crypto']) {
    assert.match(system, new RegExp(`\\b${module}\\b`, 'i'), `${module} is represented in the module hierarchy`)
  }
  assert.match(system, /World\s?Cup|世界杯/)
  assert.doesNotMatch(system, /request-test-local-model|Request Test Local Model|nemotron-3-super:120b|claude-sonnet-4-6|Routed model ID|Provider adapter/)
}

async function listen(server: Server): Promise<string> {
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  assert.ok(address && typeof address === 'object')
  return `http://127.0.0.1:${address.port}`
}

async function close(server: Server): Promise<void> {
  server.closeAllConnections()
  await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
}

// Exercise the real route branches and AI SDK serialization. No production server,
// user history, model credentials, live snapshot, or external network is used.
test('ordinary crypto QA reaches both model transports; coding prompts remain isolated', async t => {
  type Captured = { url: string; body: any }
  const captured: Captured[] = []
  const externalAttempts: string[] = []
  let responseKind: 'chat' | 'code' | 'edit' | 'agent-tool' | 'agent-text' = 'chat'
  let patchCalls = 0
  let snapshotReads = 0
  let sourceUnavailable = false
  let agentFixtureOnly = false
  const llm = createServer(async (req, res) => {
    const chunks: Buffer[] = []
    for await (const chunk of req) chunks.push(Buffer.from(chunk))
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
    captured.push({ url: req.url!, body })
    if (req.url === '/v1/messages') {
      assert.equal(body.stream, true)
      const hasResult = body.messages.some((message: any) => Array.isArray(message.content)
        && message.content.some((block: any) => block.type === 'tool_result'))
      const useTool = responseKind === 'agent-tool' && !hasResult
      const events: any[] = [
        { type: 'message_start', message: { id: 'agent-fixture', type: 'message', role: 'assistant',
          model: body.model, content: [], stop_reason: null, stop_sequence: null,
          usage: { input_tokens: 1, output_tokens: 0 } } },
        { type: 'content_block_start', index: 0, content_block: useTool
          ? { type: 'tool_use', id: 'crypto-fixture-tool', name: 'get_crypto_market', input: {} }
          : { type: 'text', text: '' } },
        { type: 'content_block_delta', index: 0, delta: useTool
          ? { type: 'input_json_delta', partial_json: JSON.stringify({ view: 'performance', strategy: 'all' }) }
          : { type: 'text_delta', text: 'Fixture agent answer' } },
        { type: 'content_block_stop', index: 0 },
        { type: 'message_delta', delta: { stop_reason: useTool ? 'tool_use' : 'end_turn', stop_sequence: null },
          usage: { output_tokens: 1 } },
        { type: 'message_stop' },
      ]
      res.setHeader('Content-Type', 'text/event-stream')
      res.end(events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(''))
      return
    }
    const content = responseKind === 'code' ? JSON.stringify(codeArtifact)
      : responseKind === 'edit' ? JSON.stringify({ edit: 'print(2)', commentary: 'Fixture edit', instruction: 'Change fixture' })
        : 'Fixture answer'
    if (req.url === '/api/chat') {
      res.setHeader('Content-Type', 'application/json')
      res.end(JSON.stringify({ model: MODEL.id, message: { role: 'assistant', content }, done: true }))
      return
    }
    assert.equal(req.url, '/v1/chat/completions')
    if (body.stream) {
      res.setHeader('Content-Type', 'text/event-stream')
      const base = { id: 'fixture-chat', object: 'chat.completion.chunk', created: 1, model: MODEL.id }
      res.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: { role: 'assistant', content }, finish_reason: null }] })}\n\n`)
      res.write(`data: ${JSON.stringify({ ...base, choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] })}\n\n`)
      res.end('data: [DONE]\n\n')
      return
    }
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify({
      id: 'fixture-chat', object: 'chat.completion', created: 1, model: MODEL.id,
      choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
    }))
  })
  const app = express()
  app.use(express.json())
  app.use('/chat', chatRouter)
  app.use('/morph-chat', morphChatRouter)
  const api = createServer(app)
  const llmURL = await listen(llm)
  const apiURL = await listen(api)
  const originalFetch = globalThis.fetch
  const originalRead = fs.promises.readFile
  const originalMorphKey = process.env.MORPH_API_KEY
  process.env.MORPH_API_KEY = 'request-test-not-a-real-key'
  t.after(async () => {
    if (originalMorphKey === undefined) delete process.env.MORPH_API_KEY
    else process.env.MORPH_API_KEY = originalMorphKey
    await Promise.all([close(api), close(llm)])
  })
  const snapshotPath = (p: unknown) => String(p).replaceAll('\\', '/').endsWith('/public/data/crypto_prediction/snapshot.json')
  t.mock.method(Date, 'now', () => Date.parse(AS_OF))
  t.mock.method(fs.promises, 'readFile', async (p: any, options: any) => {
    if (!snapshotPath(p)) {
      if (agentFixtureOnly && /\/(?:trading_signals|qlib-main)\//.test(String(p)))
        throw new Error('Non-crypto live state is unavailable in the request fixture')
      return originalRead(p, options)
    }
    snapshotReads++
    if (sourceUnavailable) throw new Error('fixture source unavailable')
    return JSON.stringify(snapshot)
  })
  t.mock.method(fs, 'writeFileSync', () => { throw new Error('Chat route tests must not write files') })
  t.mock.method(fs.promises, 'writeFile', async () => { throw new Error('Chat route tests must not write files') })
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : String(input)
    if (url.startsWith(`${apiURL}/`) || url.startsWith(`${llmURL}/`)) return originalFetch(input, init)
    if (url === 'https://www.someopark.com/blog-feed.xml') return new Response('<rss><channel/></rss>')
    if (url === 'https://www.someopark.com/blog-posts-sitemap.xml') return new Response('<urlset/>')
    if (url === 'https://api.morphllm.com/v1/chat/completions') {
      patchCalls++
      return Response.json({ choices: [{ message: { content: 'print(2)' } }] })
    }
    externalAttempts.push(url)
    throw new Error('External network is disabled for chat request tests')
  })

  async function request(route: '/chat' | '/morph-chat', appMode: string, question: string,
    options: { think?: boolean; activeCode?: boolean; kind?: typeof responseKind } = {}) {
    responseKind = options.kind ?? 'chat'
    const before = captured.length
    const response = await fetch(`${apiURL}${route}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: [{ role: 'user', content: question }], model: MODEL,
        config: { baseURL: `${llmURL}/v1`, temperature: 0, maxTokens: 1000 },
        appMode, think: options.think, selectedTemplate: 'code-interpreter-v1',
        currentStanseAgent: { ...codeArtifact, code: options.activeCode ? 'print(1)' : '' },
      }),
    })
    const body = await response.text()
    assert.equal(response.status, 200, body)
    assert.equal(captured.length, before + 1, 'one model request per route call')
    const outbound = captured.at(-1)!
    const systems = outbound.body.messages.filter((message: any) => message.role === 'system')
    assert.equal(systems.length, 1)
    return { system: systems[0].content as string, outbound, body }
  }

  await t.test('thinking ON and OFF carry the same crypto data, branded identity and module hierarchy', async () => {
    const question = 'FAVE 和 PFME 的策略收益、Demo 盈亏与当前模型是什么？'
    const on = await request('/chat', 'crypto', question, { think: true })
    const off = await request('/chat', 'crypto', question, { think: false })
    assert.equal(on.outbound.url, '/v1/chat/completions')
    assert.equal(off.outbound.url, '/api/chat')
    assert.equal(off.outbound.body.think, false)
    for (const result of [on, off]) {
      assert.match(result.system, /FAVE/)
      assert.match(result.system, /PFME/)
      assertConversationStructure(result.system, 'crypto')
      assert.match(result.system, /7123\.45/)
      assert.match(result.system, /234\.56/)
      assert.match(result.system, /-123\.45/)
      assert.match(result.system, /-2\.34/)
      assert.match(result.system, /paper/)
      assert.match(result.system, /demo/)
      assert.equal(JSON.parse(result.body).template, 'chat-response')
    }
    assert.equal(on.system, off.system)
  })

  await t.test('Morph plain QA retains its entire previous system, including previous model metadata', async () => {
    const question = 'FAVE 和 PFME 收益怎么样？'
    const result = await request('/morph-chat', 'crypto', question)
    assert.equal(result.system, toChatPrompt()
      + await cryptoChatGrounding(question, detectArtifacts(question, 'crypto'), MODEL))
    assert.match(result.system, /FAVE/)
    assert.match(result.system, /PFME/)
    assert.match(result.system, /7123\.45/)
    assert.match(result.system, /-123\.45/)
    assert.match(result.system, /request-test-local-model/)
    assert.equal(JSON.parse(result.body).template, 'chat-response')
  })

  await t.test('ordinary chat structures all modules while Morph keeps its original prompts and neither reads crypto data', async () => {
    const before = snapshotReads
    for (const mode of ['stock', 'prediction', 'macro', 'soccer']) {
      for (const think of [true, false]) {
        const result = await request('/chat', mode, '你好', { think })
        assert.equal(result.system, withConversationStructure(toChatPrompt(), mode), `/chat / ${mode} / thinking=${think}`)
        assertConversationStructure(result.system, mode)
        assert.equal(result.outbound.url, think ? '/v1/chat/completions' : '/api/chat')
        assert.doesNotMatch(result.system, /7123\.45|request-test-local-model/)
      }
      const morph = await request('/morph-chat', mode, '你好')
      assert.equal(morph.system, toChatPrompt(), `/morph-chat / ${mode}`)
      assert.doesNotMatch(morph.system, /7123\.45|request-test-local-model/)
    }
    assert.equal(snapshotReads, before, 'other modules never read crypto data')
  })

  await t.test('crypto code generation sends the original toPrompt system unchanged', async () => {
    const before = snapshotReads
    const baseline = await request('/chat', 'stock', 'Write a Python script', { kind: 'code' })
    const crypto = await request('/chat', 'crypto', 'Write a Python script', { kind: 'code' })
    assert.ok(crypto.system.includes(toPrompt(templates, 'code-interpreter-v1')))
    assert.equal(crypto.system, baseline.system)
    assert.doesNotMatch(crypto.system, /FAVE|PFME|7123\.45|request-test-local-model/)
    assert.equal(JSON.parse(crypto.body.split('\n__TEMPLATE__')[0]).code, 'print(1)')
    assert.equal(snapshotReads, before, 'code generation never reads crypto data')
  })

  await t.test('active Morph code keeps its edit-only system even for a crypto question', async () => {
    const before = snapshotReads
    const baseline = await request('/morph-chat', 'stock', '收益怎么样？', { activeCode: true, kind: 'edit' })
    const crypto = await request('/morph-chat', 'crypto', 'FAVE 收益怎么样？', { activeCode: true, kind: 'edit' })
    assert.equal(crypto.system, baseline.system)
    assert.match(crypto.system, /^You are modifying code\./)
    assert.match(crypto.system, /CURRENT CODE:\n```\nprint\(1\)/)
    assert.doesNotMatch(crypto.system, /FAVE|PFME|7123\.45|request-test-local-model/)
    assert.equal(JSON.parse(crypto.body).code, 'print(2)')
    assert.equal(patchCalls, 2)
    assert.equal(snapshotReads, before, 'code editing never reads crypto data')
  })
  await t.test('unavailable crypto source reaches the model as unavailable, with no fabricated figures', async () => {
    sourceUnavailable = true
    try {
      for (const route of ['/chat', '/morph-chat'] as const) {
        const result = await request(route, 'crypto', 'FAVE 和 PFME 收益怎么样？')
        assert.match(result.system, /SOURCE_UNAVAILABLE/)
        if (route === '/chat') assertConversationStructure(result.system, 'crypto')
        else assert.match(result.system, /request-test-local-model/)
        assert.doesNotMatch(result.system, /7123\.45|234\.56|-123\.45|-2\.34/)
        assert.equal(JSON.parse(result.body).template, 'chat-response')
      }
    } finally {
      sourceUnavailable = false
    }
  })
  await t.test('SomeoAgent executes the same crypto skill with branded-only identity and unchanged actual routing', async () => {
    const environment = {
      ANTHROPIC_API_KEY: 'agent-request-test-not-a-real-key', ANTHROPIC_AUTH_TOKEN: '',
      ANTHROPIC_BASE_URL: llmURL, FRED_API_KEY: 'agent-request-test-not-a-real-key',
      SUPABASE_URL: '', VITE_SUPABASE_URL: '', SUPABASE_SECRET_KEY: '', SP_DISABLED_TOOLS: '',
    }
    const savedEnvironment = new Map(Object.keys(environment).map(key => [key, process.env[key]]))
    const originalSyncRead = fs.readFileSync
    const syncReadMock = t.mock.method(fs, 'readFileSync', (p: any, options: any) => {
      if (/\/\.env(?:\.|$)/.test(String(p))) throw new Error('Tests must never read real .env files')
      return originalSyncRead(p, options)
    })
    const execMock = t.mock.method(childProcess, 'execSync', () => {
      throw new Error('Agent request tests must not invoke git or other shell processes')
    })
    // agentPrompt imports the named builtin export; synchronize only inside this
    // isolated test process and restore both forms afterwards.
    syncBuiltinESMExports()
    Object.assign(process.env, environment)
    agentFixtureOnly = true
    try {
      const { supabaseAdmin } = await import('../utils/supabaseAdmin.js')
      assert.equal(supabaseAdmin, null, 'test must never contact the user quota database')
      const { registerAllTools } = await import('../tools/index.js')
      registerAllTools()
      const { default: agentRouter } = await import('../routes/agent.js')
      const { getSomeoAgentSystemPrompt, clearContextCaches } = await import('../utils/agentPrompt.js')
      clearContextCaches()
      app.use('/agent', agentRouter)

      async function agentRequest(mode: string, useTool: boolean) {
        responseKind = useTool ? 'agent-tool' : 'agent-text'
        const before = captured.length
        const response = await fetch(`${apiURL}/agent`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messages: [{ role: 'user', content: 'FAVE 和 PFME 收益怎么样？' }],
            model: { ...MODEL, id: 'nemotron-3-super:120b' }, appMode: mode, sessionId: 'agent-request-test' }),
        })
        const output = await response.text()
        assert.equal(response.status, 200)
        const events = output.split('\n').filter(line => line.startsWith('data: '))
          .map(line => JSON.parse(line.slice(6)))
        assert.equal(events.some(event => event.type === 'error'), false, output)
        assert.equal(events.at(-1)?.type, 'done')
        const requests = captured.slice(before)
        assert.equal(requests.length, useTool ? 2 : 1)
        assert.ok(requests.every(request => request.url === '/v1/messages'))
        return { requests, events }
      }

      const crypto = await agentRequest('crypto', true)
      const first = crypto.requests[0].body
      assert.deepEqual(first.tools.map((tool: any) => tool.name).filter((name: string) => name.startsWith('get_crypto_')).sort(),
        ['get_crypto_contract', 'get_crypto_market', 'get_crypto_track_record'])
      assert.equal(first.model, 'claude-sonnet-4-6')
      assertConversationStructure(first.system, 'crypto')
      assert.match(first.system, /Crypto prediction-market knowledge/)
      const toolEvent = crypto.events.find(event => event.type === 'tool_result')
      assert.equal(toolEvent?.toolName, 'get_crypto_market')
      assert.equal(toolEvent?.isError, false)
      const toolData = JSON.parse(toolEvent.toolResult)
      assert.equal(toolData.snapshot_id, snapshot.snapshot_id)
      assert.deepEqual(toolData.strategies.map((s: any) => s.acronym), ['FAVE', 'PFME'])
      assert.deepEqual(toolData.strategies.map((s: any) => [s.paper.net_pnl_usd, s.demo.net_pnl_usd]),
        [[7123.45, 234.56], [-123.45, -2.34]])
      const second = crypto.requests[1].body
      const toolResult = second.messages.flatMap((message: any) => Array.isArray(message.content) ? message.content : [])
        .find((block: any) => block.type === 'tool_result')
      assert.equal(toolResult.tool_use_id, 'crypto-fixture-tool')
      assert.equal(toolResult.is_error, false)
      assert.deepEqual(JSON.parse(toolResult.content), toolData, 'real tool data returns to the model on its next iteration')

      const baseSystem = await getSomeoAgentSystemPrompt()
      const before = snapshotReads
      for (const mode of ['stock', 'prediction', 'macro', 'soccer']) {
        const { requests } = await agentRequest(mode, false)
        const body = requests[0].body
        assert.equal(body.system, withConversationStructure(baseSystem, mode), `${mode} retains its base Agent prompt within the conversational structure`)
        assertConversationStructure(body.system, mode)
        assert.equal(body.model, first.model, 'module hierarchy must not change model routing')
        assert.equal(body.tools.some((tool: any) => tool.name.startsWith('get_crypto_')), false)
        assert.deepEqual(body.tools, first.tools.filter((tool: any) => !tool.name.startsWith('get_crypto_')),
          `${mode} retains the same definitions for all original tools`)
        assert.doesNotMatch(body.system, /Crypto prediction-market knowledge|7123\.45/)
      }
      assert.equal(snapshotReads, before, 'other Agent modes never read crypto data')
    } finally {
      agentFixtureOnly = false
      for (const [key, value] of savedEnvironment) {
        if (value === undefined) delete process.env[key]
        else process.env[key] = value
      }
      syncReadMock.mock.restore()
      execMock.mock.restore()
      syncBuiltinESMExports()
    }
  })
  assert.deepEqual(externalAttempts, [], 'no unexpected external dependency was contacted')
})
