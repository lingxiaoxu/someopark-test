// server/tools/searchContentTool.ts
// Reference: CC src/tools/GrepTool/GrepTool.ts — ripgrep wrapper
// Searches file contents within the project directory

import { execSync } from 'child_process'
import path from 'path'
import { getBackendPath } from '../config.js'
import type { AgentTool } from './index.js'

const ALLOWED_ROOT = path.resolve(getBackendPath('.'))

export const searchContentTool: AgentTool = {
  definition: {
    name: 'search_content',
    description: `Search file contents using ripgrep (rg). Supports regex patterns, file type filters, context lines, and 3 output modes. Path restricted to project directory.`,
    input_schema: {
      type: 'object',
      properties: {
        pattern: { type: 'string', description: 'Regex pattern to search for' },
        path: { type: 'string', description: 'File or directory to search (relative to project root)' },
        glob: { type: 'string', description: 'File glob filter, e.g. "*.json" or "*.{py,ts}"' },
        output_mode: { type: 'string', enum: ['content', 'files_with_matches', 'count'], description: 'Output format (default: files_with_matches)' },
        context: { type: 'number', description: 'Lines of context around matches (for content mode)' },
        case_insensitive: { type: 'boolean', description: 'Case insensitive search (default false)' },
        type: { type: 'string', description: 'File type filter: js, py, ts, json, etc.' },
        head_limit: { type: 'number', description: 'Max results (default 50)' },
        multiline: { type: 'boolean', description: 'Enable multiline matching (default false)' },
      },
      required: ['pattern']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ pattern, path: searchPath, glob: globPattern, output_mode = 'files_with_matches', context, case_insensitive, type: fileType, head_limit = 50, multiline }) {
    // Build rg args (CC GrepTool pattern)
    const args: string[] = ['--hidden']

    // Exclude VCS dirs
    for (const dir of ['.git', 'node_modules', '.venv', '__pycache__', '.mypy_cache']) {
      args.push('--glob', `!${dir}`)
    }

    args.push('--max-columns', '500')

    if (multiline) args.push('-U', '--multiline-dotall')
    if (case_insensitive) args.push('-i')

    // Output mode
    if (output_mode === 'files_with_matches') args.push('-l')
    else if (output_mode === 'count') args.push('-c')

    // Context (content mode only)
    if (output_mode === 'content' && context) {
      args.push('-C', String(context))
    }

    // Line numbers for content mode
    if (output_mode === 'content') args.push('-n')

    // Pattern (escape leading dash)
    if (pattern.startsWith('-')) args.push('-e', pattern)
    else args.push(pattern)

    // Type filter
    if (fileType) args.push('--type', fileType)

    // Glob filter
    if (globPattern) {
      for (const g of globPattern.split(/\s+/)) {
        args.push('--glob', g)
      }
    }

    // Search path
    const absolutePath = searchPath
      ? path.resolve(getBackendPath(searchPath))
      : ALLOWED_ROOT

    if (!absolutePath.startsWith(ALLOWED_ROOT)) {
      throw new Error('Search path must be within project directory')
    }

    args.push(absolutePath)

    // Resolve rg binary: prefer system rg, fallback to Claude Code vendored binary
    const arch = process.arch === 'arm64' ? 'arm64' : 'x64'
    const platform = process.platform === 'darwin' ? 'darwin' : 'linux'
    const vendoredRg = `/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/vendor/ripgrep/${arch}-${platform}/rg`
    const rgBin = (() => {
      try { execSync('which rg', { stdio: 'pipe' }); return 'rg' } catch { return vendoredRg }
    })()

    // Execute ripgrep
    try {
      const stdout = execSync(`"${rgBin}" ${args.map(a => `'${a.replace(/'/g, "'\\''")}'`).join(' ')}`, {
        encoding: 'utf8',
        maxBuffer: 10 * 1024 * 1024,
        timeout: 30000,
      })

      const lines = stdout.split('\n').filter(Boolean)

      // Apply head_limit (CC applyHeadLimit pattern)
      const limited = lines.slice(0, head_limit)

      // Relativize paths
      const result = limited.map(line => {
        if (line.startsWith(ALLOWED_ROOT)) {
          return line.slice(ALLOWED_ROOT.length + 1)
        }
        return line
      })

      if (output_mode === 'count') {
        let totalMatches = 0
        for (const line of result) {
          const count = parseInt(line.split(':').pop() || '0', 10)
          if (!isNaN(count)) totalMatches += count
        }
        return { mode: 'count', content: result.join('\n'), numMatches: totalMatches, numFiles: result.length }
      }

      if (output_mode === 'content') {
        return { mode: 'content', content: result.join('\n'), numLines: result.length }
      }

      // files_with_matches
      return { mode: 'files_with_matches', filenames: result, numFiles: result.length }

    } catch (err: any) {
      // rg exits with code 1 when no matches (not an error)
      if (err.status === 1) {
        return { mode: output_mode, filenames: [], numFiles: 0, numMatches: 0, content: '' }
      }
      throw new Error(`ripgrep error: ${err.message}`)
    }
  }
}
