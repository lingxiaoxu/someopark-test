// server/tools/jsonTool.ts
// Reference: CC src/tools/GrepTool/GrepTool.ts (search/filter pattern)

import { JSONPath } from 'jsonpath-plus'
import { readJsonFile } from '../utils/fileUtils.js'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

const FILE_SHORTCUTS: Record<string, string> = {
  inventory_mrpt: 'inventory_mrpt.json',
  inventory_mtfs: 'inventory_mtfs.json',
  pair_universe_mrpt: 'pair_universe_mrpt.json',
  pair_universe_mtfs: 'pair_universe_mtfs.json',
}

export const queryJsonTool: AgentTool = {
  definition: {
    name: 'query_json',
    description: `Query JSON data using JSONPath expressions.
Examples: "$.pairs[?(@.days_held > 5)]" — pairs held >5 days
          "$.regime.mrpt_weight" — extract specific field
          "$..sharpe" — find all sharpe values recursively
Shortcuts for file_path: inventory_mrpt, inventory_mtfs, pair_universe_mrpt, pair_universe_mtfs`,
    input_schema: {
      type: 'object',
      properties: {
        file_path: { type: 'string', description: 'File path or shortcut name' },
        query: { type: 'string', description: 'JSONPath expression' },
        inline_data: { type: 'string', description: 'JSON string to query directly (alternative to file_path)' }
      },
      required: ['query']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ file_path, query, inline_data }) {
    let data: any
    if (inline_data) {
      data = JSON.parse(inline_data)
    } else if (file_path) {
      const resolved = getBackendPath(FILE_SHORTCUTS[file_path] || file_path)
      data = await readJsonFile(resolved)
    } else {
      throw new Error('Either file_path or inline_data is required')
    }
    const result = JSONPath({ path: query, json: data })
    return { query, result_count: Array.isArray(result) ? result.length : 1, result }
  }
}
