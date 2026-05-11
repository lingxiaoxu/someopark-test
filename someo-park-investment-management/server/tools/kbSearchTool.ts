// server/tools/kbSearchTool.ts
// Knowledge Base search tool — pure Node.js, no external commands
import type { AgentTool } from './index.js'
import * as path from 'path'
import * as fs from 'fs'
import { getBackendPath } from '../config.js'

const KB_PATH = getBackendPath('public/knowledge_base')

interface SearchResult {
  doc_id: string
  file: string
  title: string
  section: string
  excerpt: string
  line: number
}

function extractDocId(filename: string): string {
  const match = filename.match(/^(KB-\d+)/)
  return match ? match[1] : filename
}

function extractTitle(content: string, filename: string): string {
  const titleMatch = content.match(/^title:\s*"?(.+?)"?\s*$/m)
  if (titleMatch) return titleMatch[1]
  const headingMatch = content.match(/^#\s+(.+)$/m)
  if (headingMatch) return headingMatch[1]
  return path.basename(filename, '.md')
}

function findNearestSection(lines: string[], lineNum: number): string {
  for (let i = lineNum - 1; i >= 0; i--) {
    const match = lines[i]?.match(/^#{1,4}\s+(.+)$/)
    if (match) return match[1]
  }
  return '(top-level)'
}

export const kbSearchTool: AgentTool = {
  definition: {
    name: 'kb_search',
    description: 'Search the Private Credit & Fixed Income knowledge base (42 markdown documents). Supports keyword search across titles, sections, and content in Chinese and English. Returns relevant excerpts with source references. Topics include: private credit markets, 44 regression models, ABF, NAV funds, covenants, convexity, fixed-income arbitrage, convertible bonds, systematic investing, Oaktree/Goldman/Blackstone/Ares strategies, VCOP/IGPC/UBP fund analysis, macro perspectives, RWA blockchain, and more.',
    input_schema: {
      type: 'object' as const,
      properties: {
        query: { type: 'string', description: 'Search query (Chinese or English)' },
        top_k: { type: 'number', description: 'Number of results to return (default: 5)' },
        language: { type: 'string', enum: ['all', 'en', 'zh'], description: 'Filter by language (default: all)' },
      },
      required: ['query']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ query, top_k = 5, language = 'all' }) {
    if (!fs.existsSync(KB_PATH)) {
      return { error: 'Knowledge base not found. Run docx→md conversion first.', results: [] }
    }

    // Get all MD files
    let files = fs.readdirSync(KB_PATH).filter(f => f.endsWith('.md') && f.startsWith('KB-')).sort()

    // Language filter
    if (language === 'en') {
      files = files.filter(f => {
        const content = fs.readFileSync(path.join(KB_PATH, f), 'utf8')
        return /^language:\s*en/m.test(content)
      })
    } else if (language === 'zh') {
      files = files.filter(f => {
        const content = fs.readFileSync(path.join(KB_PATH, f), 'utf8')
        return /^language:\s*zh/m.test(content)
      })
    }

    // Split query into keywords
    const keywords = query.split(/[\s,;]+/).filter(k => k.length > 0)
    if (keywords.length === 0) {
      return { query, results: [], message: 'Empty query' }
    }

    // Search each file
    const allResults: Array<SearchResult & { score: number }> = []

    for (const file of files) {
      const filePath = path.join(KB_PATH, file)
      const content = fs.readFileSync(filePath, 'utf8')
      const lines = content.split('\n')
      const title = extractTitle(content, file)
      const docId = extractDocId(file)
      const contentLower = content.toLowerCase()

      // Score: count keyword matches
      let score = 0
      let bestLine = -1
      let bestExcerpt = ''

      for (const kw of keywords) {
        const kwLower = kw.toLowerCase()
        // Count occurrences in content
        let idx = 0
        let count = 0
        while ((idx = contentLower.indexOf(kwLower, idx)) !== -1) {
          count++
          idx += kwLower.length
        }
        if (count > 0) {
          score += Math.min(count, 10) // cap per keyword

          // Find best matching line for excerpt
          if (bestLine === -1) {
            const firstIdx = contentLower.indexOf(kwLower)
            // Count which line this is on
            const beforeMatch = content.substring(0, firstIdx)
            const lineNum = beforeMatch.split('\n').length - 1
            bestLine = lineNum

            // Extract excerpt: the matching line + 1 before + 1 after
            const excerptLines = lines.slice(Math.max(0, lineNum - 1), lineNum + 2)
            bestExcerpt = excerptLines.join(' ').trim().substring(0, 300)
          }
        }

        // Bonus for title match
        if (title.toLowerCase().includes(kwLower)) {
          score += 5
        }
      }

      if (score > 0) {
        const section = bestLine >= 0 ? findNearestSection(lines, bestLine) : '(top-level)'
        allResults.push({
          doc_id: docId,
          file,
          title,
          section,
          excerpt: bestExcerpt,
          line: bestLine + 1,
          score
        })
      }
    }

    // Sort by score descending, take top_k
    allResults.sort((a, b) => b.score - a.score)
    const results = allResults.slice(0, top_k).map(({ score, ...rest }) => rest)

    return {
      query,
      results,
      total_docs_searched: files.length
    }
  }
}
