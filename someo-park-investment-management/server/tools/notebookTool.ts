// server/tools/notebookTool.ts
// Reference: CC src/tools/FileReadTool/FileReadTool.ts — .ipynb reading branch
// Reads Jupyter notebook cells: code, markdown, outputs

import fs from 'fs'
import path from 'path'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

const ALLOWED_ROOT = path.resolve(getBackendPath('.'))

function validatePath(filePath: string): string {
  const resolved = path.resolve(filePath)
  if (!resolved.startsWith(ALLOWED_ROOT)) {
    throw new Error(`Path not allowed. Must be within ${ALLOWED_ROOT}`)
  }
  return resolved
}

interface NotebookCell {
  cell_type: string
  source: string
  outputs?: any[]
  execution_count?: number | null
}

function readNotebook(filePath: string): NotebookCell[] {
  const raw = fs.readFileSync(filePath, 'utf8')
  const nb = JSON.parse(raw)

  if (!nb.cells || !Array.isArray(nb.cells)) {
    throw new Error('Invalid notebook format: no cells array')
  }

  return nb.cells.map((cell: any, idx: number) => {
    const source = Array.isArray(cell.source) ? cell.source.join('') : (cell.source || '')

    const result: NotebookCell = {
      cell_type: cell.cell_type || 'unknown',
      source,
      execution_count: cell.execution_count ?? null,
    }

    // Extract outputs for code cells (CC pattern: text, error, image outputs)
    if (cell.cell_type === 'code' && cell.outputs) {
      result.outputs = cell.outputs.map((output: any) => {
        if (output.output_type === 'stream') {
          return {
            type: 'stream',
            name: output.name,
            text: Array.isArray(output.text) ? output.text.join('') : (output.text || ''),
          }
        }
        if (output.output_type === 'execute_result' || output.output_type === 'display_data') {
          const data: any = { type: output.output_type }
          if (output.data?.['text/plain']) {
            data.text = Array.isArray(output.data['text/plain'])
              ? output.data['text/plain'].join('')
              : output.data['text/plain']
          }
          if (output.data?.['image/png']) {
            data.image = '(base64 image omitted)'
          }
          return data
        }
        if (output.output_type === 'error') {
          return {
            type: 'error',
            ename: output.ename,
            evalue: output.evalue,
            traceback: (output.traceback || []).join('\n').slice(0, 500),
          }
        }
        return { type: output.output_type }
      })
    }

    return result
  })
}

export const readNotebookTool: AgentTool = {
  definition: {
    name: 'read_notebook',
    description: 'Read a Jupyter notebook (.ipynb) file. Returns all cells (code/markdown) with their outputs (text, errors, image placeholders). Path must be within project directory.',
    input_schema: {
      type: 'object',
      properties: {
        file_path: { type: 'string', description: 'Path to .ipynb file (absolute or relative to project root)' },
      },
      required: ['file_path']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ file_path }) {
    const resolved = file_path.startsWith('/')
      ? validatePath(file_path)
      : validatePath(getBackendPath(file_path))

    if (!resolved.endsWith('.ipynb')) throw new Error('File must be a .ipynb notebook')
    if (!fs.existsSync(resolved)) throw new Error(`Notebook not found: ${resolved}`)

    const cells = readNotebook(resolved)
    return {
      file: resolved,
      cell_count: cells.length,
      cells,
    }
  }
}
