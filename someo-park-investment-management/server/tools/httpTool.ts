// server/tools/httpTool.ts
// Reference: CC src/tools/WebFetchTool/WebFetchTool.ts

import type { AgentTool } from './index.js'
import { fetchSafeText } from './safeWebFetch.js'

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
  async execute({ url, headers, timeout = 10000 }, context) {
    const response = await fetchSafeText(url, {
      headers: headers ? JSON.parse(headers) : {}, timeout, signal: context?.signal,
    })
    // Preserve raw data for API consumers. The agent's shared result storage
    // handles large output without deleting the tail of a response.
    return formatHttpResponse(response)
  }
}

export function formatHttpResponse(response: Awaited<ReturnType<typeof fetchSafeText>>) {
  return {
    status: response.status,
    content_type: response.contentType,
    body: response.contentType.includes('json') ? JSON.parse(response.text) : response.text,
  }
}
