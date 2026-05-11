// server/tools/kbReadTool.ts
// Knowledge Base read tool — reads specific document by ID or filename
import type { AgentTool } from './index.js'
import * as path from 'path'
import * as fs from 'fs'
import { getBackendPath } from '../config.js'

const KB_PATH = getBackendPath('public/knowledge_base')

export const kbReadTool: AgentTool = {
  definition: {
    name: 'kb_read',
    description: 'Read a specific knowledge base document by ID (e.g. "KB-01") or filename. Returns the full markdown content or a specific section. Use after kb_search to get full context from a relevant document.',
    input_schema: {
      type: 'object' as const,
      properties: {
        doc_id: { type: 'string', description: 'Document ID (e.g. "KB-01", "KB-26") or full filename' },
        section: { type: 'string', description: 'Optional: specific section heading to extract (e.g. "Introduction", "Model 1")' },
        offset: { type: 'number', description: 'Start line (0-based, default: 0)' },
        limit: { type: 'number', description: 'Max lines to return (default: 300)' },
      },
      required: ['doc_id']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ doc_id, section, offset = 0, limit = 300 }) {
    if (!fs.existsSync(KB_PATH)) {
      return { error: 'Knowledge base not found. Run docx→md conversion first.' }
    }

    // Find file by ID or filename
    const files = fs.readdirSync(KB_PATH).filter(f => f.endsWith('.md'))
    let targetFile: string | undefined

    if (doc_id.endsWith('.md')) {
      targetFile = files.find(f => f === doc_id)
    } else {
      // Match by KB-XX prefix
      const id = doc_id.toUpperCase().replace(/^KB-?/i, 'KB-')
      targetFile = files.find(f => f.toUpperCase().startsWith(id))
    }

    if (!targetFile) {
      return {
        error: `Document "${doc_id}" not found.`,
        available: files.map(f => f.match(/^(KB-\d+)/)?.[1]).filter(Boolean).join(', ')
      }
    }

    const filePath = path.join(KB_PATH, targetFile)
    const content = fs.readFileSync(filePath, 'utf8')

    // If section requested, extract it
    if (section) {
      const lines = content.split('\n')
      let sectionStart = -1
      let sectionEnd = lines.length
      const sectionLower = section.toLowerCase()

      for (let i = 0; i < lines.length; i++) {
        const headingMatch = lines[i].match(/^(#{1,4})\s+(.+)$/)
        if (headingMatch) {
          if (headingMatch[2].toLowerCase().includes(sectionLower)) {
            sectionStart = i
            const level = headingMatch[1].length
            // Find end: next heading of same or higher level
            for (let j = i + 1; j < lines.length; j++) {
              const nextHeading = lines[j].match(/^(#{1,4})\s+/)
              if (nextHeading && nextHeading[1].length <= level) {
                sectionEnd = j
                break
              }
            }
            break
          }
        }
      }

      if (sectionStart === -1) {
        return {
          file: targetFile,
          error: `Section "${section}" not found in document.`,
          available_sections: lines
            .filter(l => /^#{1,3}\s+/.test(l))
            .map(l => l.replace(/^#+\s+/, ''))
            .slice(0, 20)
        }
      }

      const sectionLines = lines.slice(sectionStart, sectionEnd)
      return {
        file: targetFile,
        doc_id: targetFile.match(/^(KB-\d+)/)?.[1] || doc_id,
        section,
        total_lines: sectionLines.length,
        content: sectionLines.join('\n')
      }
    }

    // Return full document with offset/limit
    const lines = content.split('\n')
    const sliced = lines.slice(offset, offset + limit)

    return {
      file: targetFile,
      doc_id: targetFile.match(/^(KB-\d+)/)?.[1] || doc_id,
      total_lines: lines.length,
      showing: `${offset + 1}-${Math.min(offset + limit, lines.length)}`,
      content: sliced.join('\n')
    }
  }
}
