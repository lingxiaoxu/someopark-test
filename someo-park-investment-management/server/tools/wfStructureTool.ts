// server/tools/wfStructureTool.ts
// Data source: server/routes/walkForward.ts — GET /api/wf/xlsx/list

import fs from 'fs/promises'
import path from 'path'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

async function findXlsx(base: string): Promise<string[]> {
  const results: string[] = []
  async function walk(dir: string) {
    const entries = await fs.readdir(dir, { withFileTypes: true })
    for (const e of entries) {
      const full = path.join(dir, e.name)
      if (e.isDirectory()) await walk(full)
      else if (e.name.endsWith('.xlsx')) results.push(full)
    }
  }
  await walk(base)
  return results
}

export const wfStructureTool: AgentTool = {
  definition: {
    name: 'get_wf_structure',
    description: 'List all XLSX files in the walk-forward directory tree. Useful to discover available diagnostic data.',
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: 'Strategy: "mrpt" or "mtfs"',
          enum: ['mrpt', 'mtfs']
        }
      },
      required: ['strategy']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy }) {
    const dir = strategy === 'mtfs'
      ? getBackendPath('historical_runs/walk_forward_mtfs')
      : getBackendPath('historical_runs/walk_forward')
    const files = await findXlsx(dir)
    return files.map(f => ({
      path: path.relative(dir, f),
      filename: path.basename(f),
    }))
  }
}
