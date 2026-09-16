// Reference: Claude Code Test/utils/toolResultStorage.ts.
// Keep the full result; replace only the model-visible content with a preview.
// This web app uses a per-request reader instead of exposing arbitrary disk paths.
import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import type { AgentTool } from '../tools/index.js'

export const DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000
export const PREVIEW_SIZE_CHARS = 2_000
export const MAX_READ_SIZE_CHARS = 12_000

export function getPersistenceThreshold(declared = DEFAULT_MAX_RESULT_SIZE_CHARS): number {
  // Paged readers bound their own output; persisting their pages would be circular.
  if (declared === Infinity) return Infinity
  return Number.isFinite(declared) && declared > 0
    ? Math.min(declared, DEFAULT_MAX_RESULT_SIZE_CHARS)
    : DEFAULT_MAX_RESULT_SIZE_CHARS
}

function safeEnd(text: string, end: number): number {
  // Offsets are JS characters (UTF-16 code units), never advertised as bytes/tokens.
  const capped = Math.min(end, text.length)
  const previous = text.charCodeAt(capped - 1)
  const next = text.charCodeAt(capped)
  return previous >= 0xd800 && previous <= 0xdbff && next >= 0xdc00 && next <= 0xdfff
    ? capped - 1 : capped
}

export function generatePreview(content: string): string {
  let end = safeEnd(content, PREVIEW_SIZE_CHARS)
  const newline = content.lastIndexOf('\n', end - 1)
  if (newline > PREVIEW_SIZE_CHARS * 0.8) end = newline
  return content.slice(0, end)
}

type StoredResult = { filepath: string; digest: string; toolName: string; chars: number }

export function createToolResultStore(options: {
  rootDir?: string
  onPersistenceError?: (error: unknown) => void
} = {}) {
  const rootDir = options.rootDir ?? process.env.SP_AGENT_TOOL_RESULTS_DIR
    ?? path.join(os.tmpdir(), 'someo-agent-tool-results')
  const entries = new Map<string, StoredResult>()
  let directory: Promise<string> | undefined

  async function getDirectory() {
    // Server-generated random directories: caller-supplied chat/session IDs are
    // never paths, and the reader below can only access this request's entries.
    if (!directory) directory = (async () => {
      await mkdir(rootDir, { recursive: true, mode: 0o700 })
      return mkdtemp(path.join(rootDir, 'run-'))
    })()
    try { return await directory }
    catch (error) { directory = undefined; throw error }
  }

  async function prepare(content: string, toolUseId: string, toolName: string, declaredMax?: number): Promise<string> {
    if (content.length <= getPersistenceThreshold(declaredMax)) return content
    // Preserve the existing image behavior; inline chart Markdown is extracted
    // by agent.ts before this helper. Non-text payloads must not become text files.
    if (/data:image\/(?:png|jpeg);base64,/.test(content)) return content
    try {
      const digest = createHash('sha256').update(content).digest('hex')
      let entry = entries.get(toolUseId)
      if (entry && entry.digest !== digest) throw new Error('Tool-use ID reused for different content')
      if (!entry) {
        const dir = await getDirectory()
        const filename = createHash('sha256').update(toolUseId).digest('hex') + '.txt'
        const filepath = path.join(dir, filename)
        await writeFile(filepath, content, { encoding: 'utf8', flag: 'wx', mode: 0o600 })
        entry = { filepath, digest, toolName, chars: content.length }
        entries.set(toolUseId, entry)
      }
      const preview = generatePreview(content)
      return [
        '<persisted-output>',
        `Output too large (${content.length} characters). Full output saved to: ${entry.filepath}`,
        `Read the complete result with read_tool_result: ${JSON.stringify({ result_id: toolUseId, offset: 0, limit: MAX_READ_SIZE_CHARS })}.`,
        'Use next_offset to continue. The preview is not the complete result; check the full source before drawing conclusions.',
        '',
        `Preview (first ${preview.length} characters):`,
        preview,
        '...',
        '</persisted-output>',
      ].join('\n')
    } catch (error) {
      // Reference behavior: a disk failure must not silently discard the tail.
      if (options.onPersistenceError) options.onPersistenceError(error)
      else console.warn('[Agent] Could not persist tool result; preserving complete output')
      return content
    }
  }

  const readTool: AgentTool = {
    definition: {
      name: 'read_tool_result',
      description: 'Read the full text of a tool result saved during this request. Use the result_id from <persisted-output>, then continue with next_offset until done. Offsets and limits are characters, not lines. Only saved results from this request are accessible.',
      input_schema: {
        type: 'object',
        properties: {
          result_id: { type: 'string', description: 'Tool-use ID returned in the persisted-output notice' },
          offset: { type: 'integer', minimum: 0, description: '0-based character offset, default 0' },
          limit: { type: 'integer', minimum: 1, maximum: MAX_READ_SIZE_CHARS, description: `Characters per page, default/max ${MAX_READ_SIZE_CHARS}` },
        },
        required: ['result_id'],
      },
    },
    maxResultSizeChars: Infinity,
    isConcurrencySafe: () => true,
    isReadOnly: () => true,
    async execute({ result_id, offset = 0, limit = MAX_READ_SIZE_CHARS }) {
      if (typeof result_id !== 'string' || !entries.has(result_id)) {
        throw new Error('Saved tool result not found in this request')
      }
      if (!Number.isSafeInteger(offset) || offset < 0) throw new Error('offset must be a non-negative integer')
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > MAX_READ_SIZE_CHARS) {
        throw new Error(`limit must be an integer between 1 and ${MAX_READ_SIZE_CHARS}`)
      }
      const entry = entries.get(result_id)!
      const content = await readFile(entry.filepath, 'utf8')
      // A changed or damaged file must never be presented as the original result.
      if (createHash('sha256').update(content).digest('hex') !== entry.digest) {
        throw new Error('Saved tool result integrity check failed')
      }
      if (offset > content.length) throw new Error('offset exceeds the saved result length')
      if (offset > 0 && safeEnd(content, offset) !== offset) {
        throw new Error('offset splits a Unicode character; use the returned next_offset')
      }
      // With a one-character limit allow a complete surrogate pair to advance.
      let end = safeEnd(content, offset + limit)
      if (end === offset && offset < content.length) end = Math.min(offset + 2, content.length)
      return {
        result_id, tool: entry.toolName, total_chars: content.length,
        offset, returned_chars: end - offset, next_offset: end < content.length ? end : null,
        done: end >= content.length, content: content.slice(offset, end),
      }
    },
  }
  return { prepare, readTool }
}
