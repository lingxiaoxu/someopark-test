import assert from 'node:assert/strict'
import childProcess from 'node:child_process'
import fs from 'node:fs'
import { createServer, type Server, type ServerResponse } from 'node:http'
import { syncBuiltinESMExports } from 'node:module'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import express from 'express'

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

function reply(res: ServerResponse, model: string, output: string | { id: string; name: string; input: unknown }) {
  const useTool = typeof output !== 'string'
  const events = [
    { type: 'message_start', message: { id: 'research-fixture', type: 'message', role: 'assistant',
      model, content: [], stop_reason: null, stop_sequence: null,
      usage: { input_tokens: 1, output_tokens: 0 } } },
    { type: 'content_block_start', index: 0, content_block: useTool
      ? { type: 'tool_use', id: output.id, name: output.name, input: {} }
      : { type: 'text', text: '' } },
    { type: 'content_block_delta', index: 0, delta: useTool
      ? { type: 'input_json_delta', partial_json: JSON.stringify(output.input) }
      : { type: 'text_delta', text: output } },
    { type: 'content_block_stop', index: 0 },
    { type: 'message_delta', delta: { stop_reason: useTool ? 'tool_use' : 'end_turn', stop_sequence: null },
      usage: { output_tokens: 1 } },
    { type: 'message_stop' },
  ]
  res.setHeader('Content-Type', 'text/event-stream')
  res.end(events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(''))
}

// Isolated real-route test: only localhost model/API fixtures, an empty backend,
// and this test's temporary tool-result directory are used. No production .env,
// account quota, data recordings, model service, or shell process is accessed.
test('Agent research pipeline retains large-result tails, scopes readers, and reports search errors', async t => {
  const temporaryRoot = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'agent-research-test-'))
  const payload = { source: 'fixture only', observations: 'observable data\n'.repeat(4200),
    conclusion: 'TAIL_EVIDENCE: this conclusion must remain available after the preview.' }
  const fullResult = JSON.stringify(payload, null, 2)
  assert.ok(fullResult.length > 60_000)
  let scenario: 'large' | 'cross-request' | 'search-error' | 'search-success' = 'large'
  const captured: any[] = []
  const externalAttempts: string[] = []
  const upstreamErrors: unknown[] = []
  let fixtureSignal: AbortSignal | undefined
  const summary = 'Readable RKLB fixture summary with [source](https://example.com/rklb).'
  const opaque = 'Opaque_provider_payload_must_not_become_a_snippet_'.repeat(800)
  const llm = createServer(async (req, res) => {
    try {
      const chunks: Buffer[] = []
      for await (const chunk of req) chunks.push(Buffer.from(chunk))
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
      assert.equal(req.url, '/v1/messages')
      captured.push(body)
      if (!body.stream) {
        assert.equal(body.tools[0].type, 'web_search_20250305')
        const content = scenario === 'search-error'
          ? [{ type: 'web_search_tool_result', tool_use_id: 'native-search',
            content: { type: 'web_search_tool_result_error', error_code: 'max_uses_exceeded' } }]
          : [
            { type: 'text', text: 'Readable introduction.' },
            { type: 'web_search_tool_result', tool_use_id: 'native-search', content: [
              { type: 'web_search_result', title: 'RKLB fixture source', url: 'https://example.com/rklb',
                encrypted_content: opaque },
            ] },
            { type: 'text', text: summary },
          ]
        res.setHeader('Content-Type', 'application/json')
        res.end(JSON.stringify({ id: 'search-fixture', type: 'message', role: 'assistant', model: body.model,
          content, stop_reason: 'end_turn', stop_sequence: null, usage: { input_tokens: 1, output_tokens: 1 } }))
        return
      }
      assert.ok(body.tools.some((tool: any) => tool.name === 'read_tool_result'))
      const results = body.messages.flatMap((message: any) => Array.isArray(message.content) ? message.content : [])
        .filter((block: any) => block.type === 'tool_result')
      if (scenario === 'large') {
        if (results.length === 0) reply(res, body.model, { id: 'large-result', name: 'research_fixture', input: {} })
        else if (results.length === 1) {
          assert.equal(results[0].is_error, false)
          assert.match(results[0].content, /^<persisted-output>/)
          assert.match(results[0].content, /read_tool_result/)
          assert.doesNotMatch(results[0].content, /TAIL_EVIDENCE/)
          assert.ok(results[0].content.length < 4000)
          reply(res, body.model, { id: 'page-one', name: 'read_tool_result',
            input: { result_id: 'large-result', offset: 48_000, limit: 12_000 } })
        } else if (results.length === 2) {
          const page = JSON.parse(results[1].content)
          assert.equal(results[1].is_error, false)
          assert.equal(page.content, fullResult.slice(48_000, 60_000))
          assert.equal(page.next_offset, 60_000)
          assert.equal(page.done, false)
          reply(res, body.model, { id: 'page-tail', name: 'read_tool_result',
            input: { result_id: 'large-result', offset: page.next_offset, limit: 12_000 } })
        } else {
          assert.equal(results.length, 3)
          const page = JSON.parse(results[2].content)
          assert.equal(results[2].is_error, false)
          assert.equal(page.content, fullResult.slice(60_000))
          assert.match(page.content, /TAIL_EVIDENCE/)
          assert.equal(page.next_offset, null)
          assert.equal(page.done, true)
          reply(res, body.model, 'Verified the full source including TAIL_EVIDENCE.')
        }
      } else if (scenario === 'cross-request') {
        if (results.length === 0) reply(res, body.model, { id: 'foreign-read', name: 'read_tool_result',
          input: { result_id: 'large-result', offset: 60_000 } })
        else {
          assert.equal(results.length, 1)
          assert.equal(results[0].is_error, true)
          assert.match(results[0].content, /not found in this request/)
          assert.doesNotMatch(results[0].content, /TAIL_EVIDENCE/)
          reply(res, body.model, 'A different request cannot access this result.')
        }
      } else if (results.length === 0) {
        reply(res, body.model, { id: 'search-result', name: 'web_search', input: { query: 'RKLB fixture' } })
      } else if (scenario === 'search-error') {
        assert.equal(results.length, 1)
        assert.equal(results[0].is_error, true)
        assert.match(results[0].content, /Web search failed: max_uses_exceeded/)
        reply(res, body.model, 'Search failed explicitly; no research evidence was invented.')
      } else {
        const result = JSON.parse(results[0].content)
        assert.equal(results[0].is_error, false)
        assert.equal(result.summary, `Readable introduction.\n\n${summary}`)
        assert.deepEqual(result.results, [{ title: 'RKLB fixture source', url: 'https://example.com/rklb' }])
        assert.doesNotMatch(results[0].content, /Opaque_provider|encrypted_content|snippet/)
        reply(res, body.model, 'Readable source and complete summary reached the model.')
      }
    } catch (error) {
      upstreamErrors.push(error)
      res.writeHead(400, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ type: 'error', error: { type: 'invalid_request_error', message: 'Fixture assertion failed' } }))
    }
  })
  const app = express()
  app.use(express.json())
  const api = createServer(app)
  const llmURL = await listen(llm)
  const apiURL = await listen(api)
  const environment = {
    ANTHROPIC_API_KEY: 'research-fixture-not-a-real-key', ANTHROPIC_AUTH_TOKEN: '',
    ANTHROPIC_BASE_URL: llmURL, FRED_API_KEY: 'research-fixture-not-a-real-key',
    SUPABASE_URL: '', VITE_SUPABASE_URL: '', SUPABASE_SECRET_KEY: '', SP_DISABLED_TOOLS: '',
    BACKEND_ROOT: path.join(temporaryRoot, 'empty-backend'), SP_AGENT_TOOL_RESULTS_DIR: path.join(temporaryRoot, 'results'),
  }
  const savedEnvironment = new Map(Object.keys(environment).map(key => [key, process.env[key]]))
  const originalFetch = globalThis.fetch
  const originalRead = fs.readFileSync
  const readMock = t.mock.method(fs, 'readFileSync', (p: any, options: any) => {
    if (/\/\.env(?:\.|$)/.test(String(p))) throw new Error('Tests must never read real .env files')
    return originalRead(p, options)
  })
  const execMock = t.mock.method(childProcess, 'execSync', () => { throw new Error('Tests must not run shell commands') })
  syncBuiltinESMExports()
  t.mock.method(globalThis, 'fetch', async (input: string | URL | Request, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : String(input)
    if (url.startsWith(`${apiURL}/`) || url.startsWith(`${llmURL}/`)) return originalFetch(input, init)
    externalAttempts.push(url)
    throw new Error('External network is disabled for research pipeline tests')
  })
  Object.assign(process.env, environment)
  t.after(async () => {
    for (const [key, value] of savedEnvironment) {
      if (value === undefined) delete process.env[key]
      else process.env[key] = value
    }
    readMock.mock.restore()
    execMock.mock.restore()
    syncBuiltinESMExports()
    await Promise.all([close(api), close(llm)])
    await fs.promises.rm(temporaryRoot, { recursive: true, force: true })
  })
  const { supabaseAdmin } = await import('../utils/supabaseAdmin.js')
  assert.equal(supabaseAdmin, null, 'tests must never contact user quota storage')
  const { registerTool } = await import('../tools/index.js')
  const { webSearchTool } = await import('../tools/webSearchTool.js')
  registerTool(webSearchTool)
  registerTool({
    definition: { name: 'research_fixture', description: 'Isolated test fixture',
      input_schema: { type: 'object', properties: {}, required: [] } },
    async execute(_input, context) { fixtureSignal = context?.signal; return payload },
  })
  const { default: agentRouter } = await import('../routes/agent.js')
  app.use('/agent', agentRouter)

  async function request(nextScenario: typeof scenario) {
    scenario = nextScenario
    const before = captured.length
    const response = await fetch(`${apiURL}/agent`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'user', content: 'Research fixture evidence' }],
        model: { id: 'claude-sonnet-4-6' }, appMode: 'stock', sessionId: 'same-client-session' }),
    })
    assert.equal(response.status, 200)
    const text = await response.text()
    assert.deepEqual(upstreamErrors, [])
    const events = text.split('\n').filter(line => line.startsWith('data: ')).map(line => JSON.parse(line.slice(6)))
    assert.equal(events.some(event => event.type === 'error'), false, text)
    assert.equal(events.at(-1)?.type, 'done')
    return { events, requests: captured.slice(before) }
  }

  await t.test('saved full result can be paged through its tail while frontend receives original JSON', async () => {
    const { events, requests } = await request('large')
    assert.equal(requests.length, 4)
    assert.ok(fixtureSignal instanceof AbortSignal, 'route propagates request cancellation into tools')
    assert.equal(fixtureSignal.aborted, false)
    const result = events.find(event => event.type === 'tool_result' && event.toolUseId === 'large-result')
    assert.equal(result?.toolResult, fullResult)
    assert.equal(result?.isError, false)
    assert.match(events.filter(event => event.type === 'text').map(event => event.text).join(''), /TAIL_EVIDENCE/)
  })
  await t.test('reusing the client session ID cannot retrieve a prior request result', async () => {
    const { events, requests } = await request('cross-request')
    assert.equal(requests.length, 2)
    const result = events.find(event => event.type === 'tool_result')
    assert.equal(result?.isError, true)
    assert.match(result?.toolResult, /not found in this request/)
  })
  await t.test('native search failures are marked as tool errors for both model and frontend', async () => {
    const { events, requests } = await request('search-error')
    assert.equal(requests.length, 3)
    const result = events.find(event => event.type === 'tool_result')
    assert.equal(result?.isError, true)
    assert.match(result?.toolResult, /max_uses_exceeded/)
  })
  await t.test('readable search text survives end to end without opaque provider fields', async () => {
    const { events, requests } = await request('search-success')
    assert.equal(requests.length, 3)
    const result = events.find(event => event.type === 'tool_result')
    assert.equal(result?.isError, false)
    assert.equal(JSON.parse(result.toolResult).summary, `Readable introduction.\n\n${summary}`)
    assert.doesNotMatch(result.toolResult, /Opaque_provider|encrypted_content|snippet/)
  })
  assert.deepEqual(externalAttempts, [])
})
