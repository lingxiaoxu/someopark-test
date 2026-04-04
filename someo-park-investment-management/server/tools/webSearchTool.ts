// server/tools/webSearchTool.ts
// Reference: CC src/tools/WebSearchTool/WebSearchTool.ts
// Uses Anthropic's native web_search tool (GA, no beta flag needed)

import Anthropic from '@anthropic-ai/sdk'
import type { AgentTool } from './index.js'

const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY })

export const webSearchTool: AgentTool = {
  definition: {
    name: 'web_search',
    description: 'Search the web for current information using Anthropic web search. Returns search results with titles, URLs, and snippets.',
    input_schema: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'The search query (min 2 chars)' },
        allowed_domains: { type: 'array', items: { type: 'string' }, description: 'Only include results from these domains' },
        blocked_domains: { type: 'array', items: { type: 'string' }, description: 'Exclude results from these domains' },
      },
      required: ['query']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ query, allowed_domains, blocked_domains }) {
    if (!query || query.length < 2) throw new Error('Query must be at least 2 characters')
    if (allowed_domains && blocked_domains) throw new Error('Cannot specify both allowed_domains and blocked_domains')

    const startTime = Date.now()

    // CC pattern: web_search_20250305 tool type (GA, no betas needed)
    const searchTool: any = {
      type: 'web_search_20250305',
      name: 'web_search',
      max_uses: 5,
    }
    if (allowed_domains?.length) searchTool.allowed_domains = allowed_domains
    if (blocked_domains?.length) searchTool.blocked_domains = blocked_domains

    const response = await anthropic.messages.create({
      model: 'claude-sonnet-4-5-20250929',
      max_tokens: 4096,
      tools: [searchTool],
      messages: [{ role: 'user', content: `Search the web for: ${query}` }],
    } as any)

    // CC pattern: extract from server_tool_use + web_search_tool_result + text blocks
    const results: any[] = []
    let textContent = ''

    for (const block of response.content) {
      const b = block as any
      if (b.type === 'web_search_tool_result') {
        if (Array.isArray(b.content)) {
          for (const item of b.content) {
            if (item.type === 'web_search_result') {
              results.push({
                title: item.title,
                url: item.url,
                snippet: item.page_content?.slice(0, 300) || item.encrypted_content || '',
              })
            }
          }
        }
      } else if (b.type === 'text') {
        textContent += b.text
      }
      // server_tool_use blocks contain the search query — skip (already have it)
    }

    const durationSeconds = +(((Date.now() - startTime) / 1000).toFixed(2))

    return {
      query,
      results,
      summary: textContent,
      result_count: results.length,
      duration_seconds: durationSeconds,
    }
  }
}
