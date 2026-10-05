// server/tools/fileTool.ts
// Reference: CC src/tools/FileReadTool/FileReadTool.ts

import fs from 'fs'
import path from 'path'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'
import { resolveAllowed } from './pathGuard.js'

const ALLOWED_ROOT = path.resolve(getBackendPath('.'))

// Shared guard (symlinks, separator-safe root check, secret deny-list).
function validatePath(filePath: string): string {
  return resolveAllowed(filePath)
}

export const readFileTool: AgentTool = {
  definition: {
    name: 'read_file',
    description: 'Read a text file from the project directory. Supports offset/limit for large files. Path must be within the someopark-test directory.',
    input_schema: {
      type: 'object',
      properties: {
        file_path: { type: 'string', description: 'Absolute or relative path (relative to project root)' },
        offset: { type: 'number', description: 'Start reading from this line (0-based, default 0)' },
        limit: { type: 'number', description: 'Max lines to read (default 200)' }
      },
      required: ['file_path']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ file_path, offset = 0, limit = 200 }) {
    // Handle relative paths
    const resolved = file_path.startsWith('/')
      ? validatePath(file_path)
      : validatePath(getBackendPath(file_path))

    if (!fs.existsSync(resolved)) throw new Error(`File not found: ${resolved}`)

    const stat = fs.statSync(resolved)
    if (stat.isDirectory()) throw new Error(`Path is a directory: ${resolved}`)

    const content = fs.readFileSync(resolved, 'utf8')
    const lines = content.split('\n')
    const total = lines.length

    const sliced = lines.slice(offset, offset + limit)
    const numbered = sliced.map((line, i) => `${offset + i + 1}\t${line}`).join('\n')

    return {
      file: resolved,
      total_lines: total,
      showing: `${offset + 1}-${offset + sliced.length}`,
      content: numbered,
    }
  }
}
