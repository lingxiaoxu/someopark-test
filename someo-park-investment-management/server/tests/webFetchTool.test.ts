import assert from 'node:assert/strict'
import test from 'node:test'
import { gzipSync } from 'node:zlib'
import { createWebFetchTool, prepareWebContent, MAX_MARKDOWN_LENGTH, TRUNCATION_MARKER } from '../tools/webFetchTool.js'
import { fetchSafeText, isPublicAddress, MAX_HTTP_BYTES, type SafeFetchDependencies } from '../tools/safeWebFetch.js'
import { formatHttpResponse } from '../tools/httpTool.js'

const sourceURL = 'https://research.example.com/report'
const page = (text: string, contentType = 'text/html') => ({ status: 200, url: sourceURL, contentType, text })
const hop = (body = 'public page') => ({ status: 200, headers: { 'content-type': 'text/plain' }, body: Buffer.from(body) })
const safeDependencies = (request: SafeFetchDependencies['request'] = async () => hop()): SafeFetchDependencies => ({
  lookup: async () => [{ address: '8.8.8.8', family: 4 }], request,
})

test('web_fetch converts HTML, excludes scripts/styles, and retains source and readable facts', async () => {
  let modelContent = ''
  const tool = createWebFetchTool({
    fetch: async () => page('<html><head><style>invisible-style</style></head><body><h1>Revenue</h1><p>Revenue was <strong>$42 million</strong>.</p><script>secret-script</script><a href="https://example.com/filing">Filing</a></body></html>'),
    summarize: async input => { modelContent = input.content; assert.equal(input.url, sourceURL); return { text: 'Revenue was $42 million.' } },
  })
  const result: any = await tool.execute({ url: sourceURL, prompt: 'What was revenue?' })
  assert.match(modelContent, /Revenue\n=+/)
  assert.match(modelContent, /\*\*\$42 million\*\*/)
  assert.match(modelContent, /\[Filing\]\(https:\/\/example.com\/filing\)/)
  assert.doesNotMatch(modelContent, /invisible-style|secret-script/)
  assert.equal(result.source_url, sourceURL)
  assert.equal(result.summary, 'Revenue was $42 million.')
  assert.equal(result.content_truncated, false)
})

test('100,000-character boundary caps webpage input with an explicit marker and metadata', async () => {
  const exact = prepareWebContent('a'.repeat(MAX_MARKDOWN_LENGTH), 'text/plain')
  assert.equal(exact.truncated, false)
  assert.equal(exact.content.length, MAX_MARKDOWN_LENGTH)
  let modelContent = ''
  const tool = createWebFetchTool({
    fetch: async () => page('a'.repeat(MAX_MARKDOWN_LENGTH) + 'TAIL', 'text/plain'),
    summarize: async input => { modelContent = input.content; return { text: 'Partial summary', stopReason: 'max_tokens' } },
  })
  const result: any = await tool.execute({ url: sourceURL, prompt: 'Summarize' })
  assert.equal(modelContent, 'a'.repeat(MAX_MARKDOWN_LENGTH) + TRUNCATION_MARKER)
  assert.equal(result.source_chars, 100_004)
  assert.equal(result.included_chars, 100_000)
  assert.equal(result.omitted_chars, 4)
  assert.equal(result.content_truncated, true)
  assert.equal(result.summary_truncated, true)
})

test('http_request formatting preserves raw JSON and long text instead of summarizing/truncating', () => {
  const data = { data: 'a'.repeat(12_000), tail: 'retained' }
  assert.deepEqual(formatHttpResponse(page(JSON.stringify(data), 'application/json')).body, data)
  const raw = 'a'.repeat(12_000) + 'THE TAIL'
  assert.equal(formatHttpResponse(page(raw, 'text/plain')).body, raw)
  assert.equal(formatHttpResponse({ ...page('server error', 'text/plain'), status: 503 }).status, 503)
})

test('webpage input cap preserves a surrogate pair crossing the 100k boundary', async () => {
  const prefix = 'a'.repeat(MAX_MARKDOWN_LENGTH - 1)
  const input = prefix + '📈TAIL'
  const prepared = prepareWebContent(input, 'text/plain')
  assert.equal(prepared.content, prefix + TRUNCATION_MARKER)
  assert.equal(prepared.included_chars, MAX_MARKDOWN_LENGTH - 1)
  assert.equal(prepared.source_chars, MAX_MARKDOWN_LENGTH + 5)
  const tool = createWebFetchTool({
    fetch: async () => page(input, 'text/plain'),
    summarize: async () => ({ text: 'Summary' }),
  })
  const result: any = await tool.execute({ url: sourceURL, prompt: 'Summarize' })
  assert.equal(result.included_chars, 99_999)
  assert.equal(result.omitted_chars, 6)
  assert.equal(result.content_truncated, true)
  // A pair wholly inside the boundary is retained without reducing the cap.
  assert.equal(prepareWebContent('a'.repeat(MAX_MARKDOWN_LENGTH - 2) + '📈TAIL', 'text/plain').included_chars, MAX_MARKDOWN_LENGTH)
})

test('web_fetch propagates HTTP, model, empty result and unsupported media errors', async () => {
  let modelCalls = 0
  const unavailable = createWebFetchTool({ fetch: async () => ({ ...page('error'), status: 503 }), summarize: async () => { modelCalls++; return { text: 'fake' } } })
  await assert.rejects(unavailable.execute({ url: sourceURL, prompt: 'Summarize' }), /HTTP 503/)
  assert.equal(modelCalls, 0)
  const failedModel = createWebFetchTool({ fetch: async () => page('<p>Facts</p>'), summarize: async () => { throw new Error('model unavailable') } })
  await assert.rejects(failedModel.execute({ url: sourceURL, prompt: 'Summarize' }), /model unavailable/)
  const emptyModel = createWebFetchTool({ fetch: async () => page('<p>Facts</p>'), summarize: async () => ({ text: '' }) })
  await assert.rejects(emptyModel.execute({ url: sourceURL, prompt: 'Summarize' }), /no readable content/)
  const binary = createWebFetchTool({ fetch: async () => page('binary', 'application/pdf'), summarize: async () => ({ text: '' }) })
  await assert.rejects(binary.execute({ url: sourceURL, prompt: 'Summarize' }), /Unsupported webpage content type/)
})

test('public-address validation excludes loopback/private/reserved IPv4 and IPv6 forms', () => {
  for (const address of ['127.0.0.1', '10.1.2.3', '172.16.2.3', '192.168.1.1', '169.254.169.254', '100.100.100.200', '0.0.0.0', '224.0.0.1', '::1', '::', 'fc00::1', 'fe80::1', '::ffff:127.0.0.1', '::ffff:8.8.8.8', '2001:db8::1', '2002:7f00:1::']) {
    assert.equal(isPublicAddress(address), false, address)
  }
  assert.equal(isPublicAddress('8.8.8.8'), true)
  assert.equal(isPublicAddress('2001:4860:4860::8888'), true)
})

test('unsafe URL forms and private DNS never reach the transport', async () => {
  let requestCalls = 0
  const dependencies = safeDependencies(async () => { requestCalls++; return hop() })
  for (const url of ['file:///etc/passwd', 'ftp://example.com', 'https://u:p@example.com', 'http://localhost', 'http://sub.localhost/', 'http://host.local/', 'http://127.1/', 'http://2130706433/', 'http://[::1]/', 'http://[::ffff:127.0.0.1]/']) {
    await assert.rejects(fetchSafeText(url, {}, dependencies), /Only HTTP|credentials|private|localhost/)
  }
  await assert.rejects(fetchSafeText(sourceURL, {}, {
    ...dependencies, lookup: async () => [{ address: '8.8.8.8', family: 4 }, { address: '10.0.0.1', family: 4 }],
  }), /private/)
  assert.equal(requestCalls, 0)
})

test('DNS address is pinned for the request; private redirects are rejected before the next hop', async () => {
  let requests = 0
  const dependencies = safeDependencies(async (url, address) => {
    requests++
    assert.equal(url.href, sourceURL)
    assert.equal(address.address, '8.8.8.8')
    return { status: 302, headers: { location: 'http://169.254.169.254/latest/meta-data/' }, body: Buffer.alloc(0) }
  })
  await assert.rejects(fetchSafeText(sourceURL, {}, dependencies), /private/)
  assert.equal(requests, 1)
})

test('redirect DNS is validated and caller credentials do not cross origins', async () => {
  const visited: string[] = []
  const dependencies = safeDependencies(async (url, _address, options) => {
    visited.push(url.hostname)
    if (visited.length === 1) {
      assert.equal(options.headers.authorization, 'Bearer fixture')
      assert.equal(options.headers.Host, undefined)
      return { status: 302, headers: { location: 'https://second.example.com/page' }, body: Buffer.alloc(0) }
    }
    assert.deepEqual(options.headers, {})
    return hop('second page')
  })
  const result = await fetchSafeText(sourceURL, { headers: { authorization: 'Bearer fixture', Host: 'internal' } }, dependencies)
  assert.equal(result.url, 'https://second.example.com/page')
  assert.deepEqual(visited, ['research.example.com', 'second.example.com'])
  const privateRedirect = { ...dependencies, lookup: async (host: string) => [{ address: host.startsWith('second') ? '192.168.1.1' : '8.8.8.8', family: 4 }] }
  visited.length = 0
  await assert.rejects(fetchSafeText(sourceURL, { headers: { authorization: 'Bearer fixture' } }, privateRedirect), /private/)
  assert.equal(visited.length, 1)
})

test('response size is bounded before and after decompression', async () => {
  await assert.rejects(fetchSafeText(sourceURL, {}, safeDependencies(async () => hop('x'.repeat(MAX_HTTP_BYTES + 1)))), /too large/)
  const result = await fetchSafeText(sourceURL, {}, safeDependencies(async () => ({ status: 200, headers: { 'content-encoding': 'gzip' }, body: gzipSync('compressed page') })))
  assert.equal(result.text, 'compressed page')
  const bomb = gzipSync('x'.repeat(MAX_HTTP_BYTES + 1))
  await assert.rejects(fetchSafeText(sourceURL, {}, safeDependencies(async () => ({ status: 200, headers: { 'content-encoding': 'gzip' }, body: bomb }))), /larger|length|size/i)
})

test('abort interrupts DNS, fetch and model processing instead of yielding a fake success', async () => {
  const controller = new AbortController()
  const pending = fetchSafeText(sourceURL, { signal: controller.signal }, { ...safeDependencies(), lookup: async () => new Promise(() => {}) })
  controller.abort(new Error('client disconnected'))
  await assert.rejects(pending, /client disconnected/)
  const transportAbort = new AbortController()
  const fetching = fetchSafeText(sourceURL, { signal: transportAbort.signal }, safeDependencies(async () => {
    transportAbort.abort(new Error('transport disconnected'))
    return new Promise(() => {})
  }))
  await assert.rejects(fetching, /transport disconnected/)
  const modelAbort = new AbortController()
  const tool = createWebFetchTool({
    fetch: async () => page('<p>Facts</p>'),
    summarize: async input => {
      assert.equal(input.signal, modelAbort.signal)
      modelAbort.abort(new Error('model disconnected'))
      return { text: 'must not appear as success' }
    },
  })
  await assert.rejects(tool.execute({ url: sourceURL, prompt: 'Summarize' }, { signal: modelAbort.signal }), /model disconnected/)
})
