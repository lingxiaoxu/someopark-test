// server/tools/webSearchTool.ts
// Reference: CC src/tools/WebSearchTool/WebSearchTool.ts
// Uses Anthropic's native web_search tool (GA, no beta flag needed).

import Anthropic from '@anthropic-ai/sdk'
import type { AgentTool } from './index.js'

type SearchHit = { title: string; url: string }
type SearchError = { error_code: string; tool_use_id?: string }
type SearchOutput = {
  query: string
  results: SearchHit[]
  summary: string
  result_count: number
  duration_seconds: number
  status: 'success' | 'partial'
  errors?: SearchError[]
}

type SearchRequest = (
  request: Record<string, unknown>,
  options?: { signal?: AbortSignal },
) => Promise<unknown>

const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)

// Extract explicit API fields, as in the reference implementation. In particular,
// encrypted_content is an opaque provider field, never readable source evidence.
// Do not guess from a string's alphabet whether readable text is encrypted.
export function normalizeWebSearchResponse(
  response: unknown,
  query: string,
  durationSeconds: number,
): SearchOutput {
  if (!isRecord(response) || !Array.isArray(response.content)) {
    throw new Error('Web search failed: invalid_response')
  }

  const results: SearchHit[] = []
  const textBlocks: string[] = []
  const errors: SearchError[] = []
  let successfulSearches = 0

  for (const block of response.content) {
    if (!isRecord(block)) {
      errors.push({ error_code: 'invalid_content_block' })
      continue
    }
    if (block.type === 'text') {
      if (typeof block.text === 'string') textBlocks.push(block.text)
      else errors.push({ error_code: 'invalid_text_block' })
      continue
    }
    if (block.type !== 'web_search_tool_result') continue

    const toolId = typeof block.tool_use_id === 'string' ? { tool_use_id: block.tool_use_id } : {}
    if (!Array.isArray(block.content)) {
      // Expose only the API error code, never stringify the raw provider payload.
      const code = isRecord(block.content) && typeof block.content.error_code === 'string'
        ? block.content.error_code : 'invalid_search_result_block'
      errors.push({ ...toolId, error_code: code })
      continue
    }

    let validHits = 0
    for (const item of block.content) {
      if (!isRecord(item) || item.type !== 'web_search_result'
        || typeof item.title !== 'string' || typeof item.url !== 'string') {
        errors.push({ ...toolId, error_code: 'invalid_search_result' })
        continue
      }
      // Deliberate whitelist: no snippet, page_content or encrypted_content fallback.
      results.push({ title: item.title, url: item.url })
      validHits++
    }
    // An actual successful search may return no matches. Malformed hits alone
    // must not be represented as a successful empty search.
    if (block.content.length === 0 || validHits > 0) successfulSearches++
  }

  if (successfulSearches === 0) {
    const codes = errors.length ? [...new Set(errors.map(error => error.error_code))] : ['missing_search_results']
    throw new Error(`Web search failed: ${codes.join(', ')}`)
  }

  return {
    query,
    results,
    // Preserve readable commentary both before and after search blocks, in order.
    summary: textBlocks.join('\n\n'),
    result_count: results.length,
    duration_seconds: durationSeconds,
    status: errors.length ? 'partial' : 'success',
    ...(errors.length ? { errors } : {}),
  }
}

function validateDomains(value: unknown, name: string): string[] | undefined {
  if (value === undefined) return undefined
  if (!Array.isArray(value) || value.some(domain => typeof domain !== 'string' || !domain.trim())) {
    throw new Error(`${name} must be an array of non-empty domain strings`)
  }
  return value
}

let anthropic: Anthropic | undefined
const requestSearch: SearchRequest = async (request, options) => {
  anthropic ??= new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY })
  return anthropic.messages.create(request as any, options)
}

export function createWebSearchTool(createMessage: SearchRequest = requestSearch): AgentTool {
  return {
    definition: {
      name: 'web_search',
      description: 'Search the web for current information using Anthropic web search. Returns source titles and URLs, readable summary text, and explicit partial-search errors when present. Cite source URLs using Markdown links.',
      input_schema: {
        type: 'object',
        properties: {
          query: { type: 'string', description: 'The search query (min 2 chars)' },
          allowed_domains: { type: 'array', items: { type: 'string' }, description: 'Only include results from these domains' },
          blocked_domains: { type: 'array', items: { type: 'string' }, description: 'Exclude results from these domains' },
        },
        required: ['query'],
      },
    },
    maxResultSizeChars: 100_000,
    isConcurrencySafe: () => true,
    isReadOnly: () => true,
    async execute(input, context) {
      if (!isRecord(input) || typeof input.query !== 'string' || input.query.trim().length < 2) {
        throw new Error('Query must be at least 2 characters')
      }
      const { query } = input
      const allowedDomains = validateDomains(input.allowed_domains, 'allowed_domains')
      const blockedDomains = validateDomains(input.blocked_domains, 'blocked_domains')
      if (allowedDomains?.length && blockedDomains?.length) {
        throw new Error('Cannot specify both allowed_domains and blocked_domains')
      }
      context?.signal?.throwIfAborted()
      const startTime = Date.now()
      const searchTool: Record<string, unknown> = {
        type: 'web_search_20250305',
        name: 'web_search',
        max_uses: 5,
        ...(allowedDomains?.length ? { allowed_domains: allowedDomains } : {}),
        ...(blockedDomains?.length ? { blocked_domains: blockedDomains } : {}),
      }
      const response = await createMessage({
        model: 'claude-sonnet-4-5-20250929',
        max_tokens: 4096,
        tools: [searchTool],
        messages: [{ role: 'user', content: `Search the web for: ${query}` }],
      }, { signal: context?.signal })
      const durationSeconds = +(((Date.now() - startTime) / 1000).toFixed(2))
      return normalizeWebSearchResponse(response, query, durationSeconds)
    },
  }
}

export const webSearchTool = createWebSearchTool()
