// server/tools/kbListTool.ts
// Knowledge Base list tool — lists all available documents with metadata
import type { AgentTool } from './index.js'
import * as path from 'path'
import * as fs from 'fs'
import { getBackendPath } from '../config.js'

const KB_PATH = getBackendPath('public/knowledge_base')

interface KBDoc {
  id: string
  file: string
  title: string
  language: string
  topics: string[]
  charCount: number
}

function parseDocMetadata(filePath: string): KBDoc {
  const filename = path.basename(filePath)
  const content = fs.readFileSync(filePath, 'utf8')
  const id = filename.match(/^(KB-\d+)/)?.[1] || filename

  // Parse YAML frontmatter
  let title = filename.replace(/\.md$/, '')
  let language = 'unknown'
  let topics: string[] = []

  const fmMatch = content.match(/^---\n([\s\S]*?)\n---/)
  if (fmMatch) {
    const fm = fmMatch[1]
    const titleMatch = fm.match(/^title:\s*"?(.+?)"?\s*$/m)
    if (titleMatch) title = titleMatch[1]
    const langMatch = fm.match(/^language:\s*(\w+)/m)
    if (langMatch) language = langMatch[1]
    const topicsMatch = fm.match(/^topics:\s*\[(.+)\]/m)
    if (topicsMatch) topics = topicsMatch[1].split(',').map(t => t.trim().replace(/['"]/g, ''))
  }

  // Count content chars (exclude frontmatter)
  const body = content.replace(/^---[\s\S]*?---\n?/, '')
  const charCount = body.length

  return { id, file: filename, title, language, topics, charCount }
}

export const kbListTool: AgentTool = {
  definition: {
    name: 'kb_list',
    description: 'List all available knowledge base documents (42 total) with IDs, titles, languages, topics, and character counts. Use to discover relevant documents before searching or reading.',
    input_schema: {
      type: 'object' as const,
      properties: {
        topic_filter: { type: 'string', description: 'Filter by topic keyword (e.g. "private-credit", "regression", "arbitrage")' },
        language: { type: 'string', enum: ['all', 'en', 'zh'], description: 'Filter by language (default: all)' },
      },
      required: []
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ topic_filter, language = 'all' } = {}) {
    if (!fs.existsSync(KB_PATH)) {
      return { error: 'Knowledge base not found. Run docx→md conversion first.', documents: [] }
    }

    // Try KB_INDEX.json first
    const indexPath = path.join(KB_PATH, 'KB_INDEX.json')
    let docs: KBDoc[]

    if (fs.existsSync(indexPath)) {
      docs = JSON.parse(fs.readFileSync(indexPath, 'utf8'))
    } else {
      // Build from files
      const files = fs.readdirSync(KB_PATH)
        .filter(f => f.endsWith('.md') && f.startsWith('KB-'))
        .sort()
      docs = files.map(f => parseDocMetadata(path.join(KB_PATH, f)))
    }

    // Apply filters
    if (language !== 'all') {
      docs = docs.filter(d => d.language === language)
    }
    if (topic_filter) {
      const filter = topic_filter.toLowerCase()
      docs = docs.filter(d =>
        d.topics.some(t => t.toLowerCase().includes(filter)) ||
        d.title.toLowerCase().includes(filter)
      )
    }

    return {
      total: docs.length,
      documents: docs.map(d => ({
        id: d.id,
        title: d.title,
        language: d.language,
        topics: d.topics,
        chars: d.charCount
      }))
    }
  }
}
