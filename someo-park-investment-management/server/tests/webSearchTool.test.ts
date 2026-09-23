import assert from 'node:assert/strict'
import test from 'node:test'
import { createWebSearchTool, normalizeWebSearchResponse, webSearchTool } from '../tools/webSearchTool.js'

const hit = (overrides: Record<string, unknown> = {}) => ({
  type: 'web_search_result', title: 'Rocket Lab news', url: 'https://example.com/rklb', ...overrides,
})
const search = (content: unknown = [hit()], toolId = 'search-1') => ({
  type: 'web_search_tool_result', tool_use_id: toolId, content,
})
const text = (value: string) => ({ type: 'text', text: value })
const normalize = (content: unknown[]) => normalizeWebSearchResponse({ content }, 'RKLB research', 1.25)

test('search uses a title/url whitelist; opaque prefixes and suffixes never become snippets', () => {
  const opaque = 'EswOCioIExgCIiRkZTE3MjM1NS1mMTNiLTRkZGUtYjAzNy0xMjFlNGZkOTI0ZTES'.repeat(40)
  const response = normalize([
    text('Before: sources for RKLB.'),
    { type: 'server_tool_use', name: 'web_search', input: { query: 'RKLB' } },
    search([hit({ encrypted_content: opaque, page_content: 'Unselected page field', trailing: opaque })]),
    text('After: a readable conclusion with a source [link](https://example.com/rklb).'),
  ])
  assert.deepEqual(response.results, [{ title: 'Rocket Lab news', url: 'https://example.com/rklb' }])
  assert.equal(response.summary, 'Before: sources for RKLB.\n\nAfter: a readable conclusion with a source [link](https://example.com/rklb).')
  assert.equal(response.status, 'success')
  assert.equal(response.result_count, 1)
  assert.equal(response.duration_seconds, 1.25)
  assert.doesNotMatch(JSON.stringify(response), /encrypted_content|page_content|snippet|EswOCio/)
})

test('all readable text remains intact, including non-ASCII and Base64-looking prose', () => {
  const before = '研究：火箭实验室。\nEncoded example SGVsbG8gd29ybGQ= is part of this explanation.'
  const after = 'Résumé — 株価、売上、风险；末尾总结。'
  const result = normalize([text(before), search(), text(after)])
  assert.equal(result.summary, `${before}\n\n${after}`)
})

test('multiple searches preserve all links and leading/intermediate/trailing text in sequence', () => {
  const result = normalize([
    text('First.'), search([hit()], 'one'), text('Middle.'),
    { type: 'server_tool_use', name: 'web_search' },
    search([hit({ title: 'Another source', url: 'https://example.org/news' })], 'two'),
    text('Last.'),
  ])
  assert.equal(result.summary, 'First.\n\nMiddle.\n\nLast.')
  assert.equal(result.result_count, 2)
  assert.equal(result.results[1].url, 'https://example.org/news')
})

test('large opaque search fields cannot push a useful final summary outside an 8k budget', () => {
  const results = Array.from({ length: 9 }, (_, index) => hit({
    title: `RKLB source ${index + 1}`, url: `https://example.com/${index + 1}`,
    encrypted_content: 'aBcDeF0123456789+/'.repeat(150),
  }))
  const source = { content: [search(results), text('Readable final summary. '.repeat(60))] }
  assert.ok(JSON.stringify(source).length > 22_000)
  const output = JSON.stringify(normalizeWebSearchResponse(source, 'RKLB', 2), null, 2)
  assert.ok(output.length < 8_000)
  assert.ok(output.includes('Readable final summary. '.repeat(60)))
  assert.doesNotMatch(output, /aBcDeF0123456789/)
})

test('an upstream tool error alongside results is explicit partial success', () => {
  const result = normalize([
    search([hit()], 'success'),
    search({ type: 'web_search_tool_result_error', error_code: 'rate_limit_error', encrypted_content: 'DO_NOT_COPY' }, 'failed'),
    text('One source is available; another search failed.'),
  ])
  assert.equal(result.status, 'partial')
  assert.deepEqual(result.errors, [{ error_code: 'rate_limit_error', tool_use_id: 'failed' }])
  assert.equal(result.result_count, 1)
  assert.match(result.summary, /another search failed/)
  assert.doesNotMatch(JSON.stringify(result), /DO_NOT_COPY/)
})

test('total API tool failure throws instead of reporting an empty successful search', () => {
  assert.throws(() => normalize([
    text('I will search.'),
    search({ type: 'web_search_tool_result_error', error_code: 'unavailable' }),
  ]), /Web search failed: unavailable/)
})

test('successful searches with zero matches remain valid empty results', () => {
  const result = normalize([search([]), text('No matching sources found.')])
  assert.equal(result.status, 'success')
  assert.equal(result.result_count, 0)
  assert.deepEqual(result.results, [])
  assert.equal(result.summary, 'No matching sources found.')
})

test('malformed response envelopes and missing search blocks are not fake successful searches', () => {
  for (const value of [null, undefined, [], {}, { content: null }, { content: 'bad' }]) {
    assert.throws(() => normalizeWebSearchResponse(value, 'RKLB', 0), /invalid_response/)
  }
  assert.throws(() => normalize([]), /missing_search_results/)
  assert.throws(() => normalize([text('No search was performed.')]), /missing_search_results/)
  assert.throws(() => normalize([search(null)]), /invalid_search_result_block/)
  assert.throws(() => normalize([null, search([null, hit({ title: null })])]), /invalid_content_block.*invalid_search_result/)
})

test('malformed individual entries are reported while valid sources and text survive', () => {
  const result = normalize([
    null, { type: 'text', text: null }, search([null, hit(), hit({ url: 5 })]), text('Readable.'),
  ])
  assert.equal(result.status, 'partial')
  assert.equal(result.result_count, 1)
  assert.deepEqual(result.errors?.map(error => error.error_code), [
    'invalid_content_block', 'invalid_text_block', 'invalid_search_result', 'invalid_search_result',
  ])
  assert.equal(result.summary, 'Readable.')
})

test('tool forwards the existing search model and filters plus request cancellation', async () => {
  const controller = new AbortController()
  const calls: unknown[] = []
  const tool = createWebSearchTool(async (request, options) => {
    calls.push({ request, options })
    return { content: [search(), text('Summary')] }
  })
  const result = await tool.execute({ query: 'RKLB analysis', allowed_domains: ['example.com'], blocked_domains: [] }, { signal: controller.signal })
  assert.equal((result as any).status, 'success')
  assert.deepEqual(calls, [{
    request: {
      model: 'claude-sonnet-4-5-20250929', max_tokens: 4096,
      tools: [{ type: 'web_search_20250305', name: 'web_search', max_uses: 5, allowed_domains: ['example.com'] }],
      messages: [{ role: 'user', content: 'Search the web for: RKLB analysis' }],
    },
    options: { signal: controller.signal },
  }])
  assert.equal(webSearchTool.maxResultSizeChars, 100_000)
  assert.equal(webSearchTool.isReadOnly?.(), true)
  assert.equal(webSearchTool.isConcurrencySafe?.(), true)
})

test('invalid queries/domain filters are rejected before any upstream request', async () => {
  let calls = 0
  const tool = createWebSearchTool(async () => { calls++; return { content: [search()] } })
  for (const input of [undefined, null, {}, { query: 22 }, { query: ' ' }, { query: 'r' }]) {
    await assert.rejects(tool.execute(input), /Query must be/)
  }
  for (const input of [
    { query: 'RKLB', allowed_domains: 'example.com' },
    { query: 'RKLB', blocked_domains: [null] },
    { query: 'RKLB', allowed_domains: [''] },
    { query: 'RKLB', blocked_domains: ['   '] },
  ]) {
    await assert.rejects(tool.execute(input), /array of non-empty domain strings/)
  }
  await assert.rejects(tool.execute({ query: 'RKLB', allowed_domains: ['example.com'], blocked_domains: ['example.org'] }), /Cannot specify both/)
  assert.equal(calls, 0)
})

test('nonempty blocked filter works with an empty allowed filter', async () => {
  let options: any
  const tool = createWebSearchTool(async request => { options = request; return { content: [search([])] } })
  await tool.execute({ query: 'RKLB', allowed_domains: [], blocked_domains: ['example.com'] })
  assert.deepEqual(options.tools[0].blocked_domains, ['example.com'])
  assert.equal(options.tools[0].allowed_domains, undefined)
})

test('upstream request rejection is not converted to a successful empty result', async () => {
  const failure = new Error('Upstream unavailable')
  const tool = createWebSearchTool(async () => { throw failure })
  await assert.rejects(tool.execute({ query: 'RKLB' }), error => error === failure)
})

test('already cancelled searches never start an upstream request', async () => {
  const controller = new AbortController()
  controller.abort()
  let called = false
  const tool = createWebSearchTool(async () => { called = true; return { content: [search()] } })
  await assert.rejects(tool.execute({ query: 'RKLB' }, { signal: controller.signal }), /aborted/i)
  assert.equal(called, false)
})
