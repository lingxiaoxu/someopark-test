// server/tools/listFilesTool.ts
// Reference: CC src/tools/GlobTool/GlobTool.ts

import { glob } from 'glob'
import path from 'path'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

const ALLOWED_ROOT = path.resolve(getBackendPath('.'))

export const listFilesTool: AgentTool = {
  definition: {
    name: 'list_files',
    description: 'List files matching a glob pattern. Supports ** for recursive matching. Path restricted to project directory.',
    input_schema: {
      type: 'object',
      properties: {
        pattern: { type: 'string', description: 'Glob pattern, e.g. "trading_signals/*.json" or "**/*.csv"' },
        directory: { type: 'string', description: 'Base directory (relative to project root, default: project root)' },
        limit: { type: 'number', description: 'Max results (default 50)' }
      },
      required: ['pattern']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ pattern, directory, limit = 50 }) {
    const base = directory
      ? path.resolve(getBackendPath(directory))
      : ALLOWED_ROOT

    if (!base.startsWith(ALLOWED_ROOT)) {
      throw new Error('Directory must be within project root')
    }

    const files = await glob(pattern, { cwd: base, absolute: true })
    const sorted = files
      .filter(f => f.startsWith(ALLOWED_ROOT))
      .sort()
      .reverse()
      .slice(0, limit)

    return sorted.map(f => ({
      path: path.relative(ALLOWED_ROOT, f),
      filename: path.basename(f),
    }))
  }
}
