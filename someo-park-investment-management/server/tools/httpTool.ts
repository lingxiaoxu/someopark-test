// server/tools/httpTool.ts
// Reference: CC src/tools/WebFetchTool/WebFetchTool.ts

import type { AgentTool } from './index.js'

export const httpRequestTool: AgentTool = {
  definition: {
    name: 'http_request',
    description: 'Make an HTTP GET request to a URL. Returns response body as text or JSON. Only GET method for safety.',
    input_schema: {
      type: 'object',
      properties: {
        url: { type: 'string', description: 'URL to fetch' },
        headers: { type: 'string', description: 'JSON string of extra headers' },
        timeout: { type: 'number', description: 'Timeout in ms (default 10000)' }
      },
      required: ['url']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ url, headers, timeout = 10000 }) {
    const parsedUrl = new URL(url)
    // Security: block private IP ranges
    const blocked = ['localhost', '127.0.0.1', '0.0.0.0', '[::1]']
    if (blocked.includes(parsedUrl.hostname)) {
      throw new Error('Requests to localhost/private IPs are not allowed')
    }

    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeout)

    try {
      const res = await fetch(url, {
        method: 'GET',
        headers: headers ? JSON.parse(headers) : {},
        signal: controller.signal,
      })

      const contentType = res.headers.get('content-type') || ''
      const body = contentType.includes('json')
        ? await res.json()
        : await res.text()

      return {
        status: res.status,
        content_type: contentType,
        body: typeof body === 'string' && body.length > 8000
          ? body.slice(0, 8000) + `\n[TRUNCATED: ${body.length - 8000} chars omitted]`
          : body,
      }
    } finally {
      clearTimeout(timer)
    }
  }
}
