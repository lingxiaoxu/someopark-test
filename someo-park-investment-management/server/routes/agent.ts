// server/routes/agent.ts
// Someo Agent Loop + SSE streaming
// Reference: CC src/query.ts, src/QueryEngine.ts, src/services/api/claude.ts,
//            src/services/tools/toolOrchestration.ts, src/services/tools/toolExecution.ts

import express from 'express'
import Anthropic from '@anthropic-ai/sdk'
import { getAgentTools, executeTool } from '../tools/index.js'
import { getSomeoAgentSystemPrompt } from '../utils/agentPrompt.js'
import { createSendMessageTool } from '../tools/sendMessageTool.js'
import type { AgentTool } from '../tools/index.js'

const router = express.Router()

// Anthropic client — API key from environment (never hardcoded)
const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY })

// === Model pricing — copied from CC src/utils/modelCost.ts ===
// Structure: { input, output, cacheRead (10% of input), cacheWrite (125% of input) } per MTok
interface ModelCosts { input: number; output: number; cacheRead: number; cacheWrite: number }
const COST_TIER_0_8_4: ModelCosts =  { input: 0.8,  output: 4,    cacheRead: 0.08,  cacheWrite: 1.0   }
const COST_TIER_1_5: ModelCosts =    { input: 1,    output: 5,    cacheRead: 0.1,   cacheWrite: 1.25  }
const COST_TIER_3_15: ModelCosts =   { input: 3,    output: 15,   cacheRead: 0.3,   cacheWrite: 3.75  }
const COST_TIER_5_25: ModelCosts =   { input: 5,    output: 25,   cacheRead: 0.5,   cacheWrite: 6.25  }
const COST_TIER_15_75: ModelCosts =  { input: 15,   output: 75,   cacheRead: 1.5,   cacheWrite: 18.75 }
const COST_TIER_30_150: ModelCosts = { input: 30,   output: 150,  cacheRead: 3.0,   cacheWrite: 37.5  }
const DEFAULT_COST = COST_TIER_5_25

// CC: MODEL_COSTS mapping — maps canonical model names to cost tiers
const MODEL_PRICES: Record<string, ModelCosts> = {
  // Opus family
  'claude-opus-4-6':            COST_TIER_15_75,
  'claude-opus-4-5-20251101':   COST_TIER_5_25,
  'claude-opus-4-20250514':     COST_TIER_15_75,
  'claude-opus-4-1-20250414':   COST_TIER_15_75,
  // Sonnet family
  'claude-sonnet-4-6':          COST_TIER_3_15,
  'claude-sonnet-4-5-20250929': COST_TIER_3_15,
  'claude-sonnet-4-5-20250514': COST_TIER_3_15,
  'claude-sonnet-4-20250514':   COST_TIER_3_15,
  'claude-3-5-sonnet-20241022': COST_TIER_3_15,
  // Haiku family
  'claude-haiku-4-5-20251001':  COST_TIER_1_5,
  'claude-3-5-haiku-20241022':  COST_TIER_0_8_4,
}

function getModelCost(modelId: string): ModelCosts {
  // CC pattern: exact match first, then prefix match, then default
  if (MODEL_PRICES[modelId]) return MODEL_PRICES[modelId]
  for (const [key, cost] of Object.entries(MODEL_PRICES)) {
    if (modelId.startsWith(key.replace(/-\d{8}$/, ''))) return cost
  }
  console.warn(`[Agent] Unknown model cost for ${modelId}, using default tier`)
  return DEFAULT_COST
}

// === SSE event types ===
interface AgentSSEEvent {
  type: 'thinking' | 'tool_call' | 'tool_result' | 'text' |
    'task_update' | 'ask_user' | 'error' | 'done' | 'usage' | 'brief'
  [key: string]: any
}

function sendSSE(res: express.Response, event: AgentSSEEvent) {
  if (!res.writableEnded) {
    res.write(`data: ${JSON.stringify(event)}\n\n`)
  }
}

// === CC src/utils/messages.js: normalizeMessagesForAPI() ===
// Ensures messages are valid for Anthropic API: no empty content, proper role alternation
function normalizeMessagesForAPI(messages: Anthropic.MessageParam[]): Anthropic.MessageParam[] {
  const result: Anthropic.MessageParam[] = []
  for (const msg of messages) {
    // Skip messages with empty content
    if (!msg.content || (Array.isArray(msg.content) && msg.content.length === 0)) continue
    // Ensure role alternation: merge consecutive same-role messages
    const last = result[result.length - 1]
    if (last && last.role === msg.role) {
      // Merge content arrays
      if (Array.isArray(last.content) && Array.isArray(msg.content)) {
        ;(last.content as any[]).push(...(msg.content as any[]))
      }
      continue
    }
    result.push({ ...msg })
  }
  // Anthropic requires first message to be 'user'
  while (result.length > 0 && result[0].role !== 'user') {
    result.shift()
  }
  return result
}

// === Tool result truncation (reference: CC src/utils/toolResultStorage.ts) ===
const MAX_TOOL_RESULT_CHARS = 8000

function truncateResult(result: string, toolName: string): string {
  if (result.length <= MAX_TOOL_RESULT_CHARS) return result
  const omitted = result.length - MAX_TOOL_RESULT_CHARS
  return result.slice(0, MAX_TOOL_RESULT_CHARS) +
    `\n\n[TRUNCATED: ${omitted} chars omitted. Tool: ${toolName}]`
}

// === ask_user pause/resume mechanism (reference: CC AskUserQuestionTool) ===
const pendingAskUser = new Map<string, (answer: string) => void>()

// Receive user answers for ask_user
router.post('/answer', (req, res) => {
  const { sessionId, answer } = req.body
  const resolve = pendingAskUser.get(sessionId)
  if (!resolve) return res.status(404).json({ error: 'No pending ask_user for this session' })
  pendingAskUser.delete(sessionId)
  resolve(answer)
  res.json({ ok: true })
})

// === Stateful tool factories (per-request) ===
interface TaskItem {
  id: string
  title: string
  status: 'pending' | 'in_progress' | 'completed' | 'failed'
  activeForm?: string
}

function createAskUserTool(sessionId: string, sendSSEFn: (evt: AgentSSEEvent) => void): AgentTool {
  return {
    definition: {
      name: 'ask_user',
      description: 'Ask the user a clarifying question and wait for their answer. Use when strategy (mrpt/mtfs), date range, or other key parameters are ambiguous.',
      input_schema: {
        type: 'object',
        properties: {
          question: { type: 'string', description: 'The question to ask' },
          options: { type: 'array', items: { type: 'string' }, description: 'Optional predefined choices' }
        },
        required: ['question']
      }
    },
    async execute({ question, options }) {
      sendSSEFn({ type: 'ask_user', question, options })
      return new Promise<string>((resolve, reject) => {
        pendingAskUser.set(sessionId, (answer) => resolve(`User answered: ${answer}`))
        setTimeout(() => {
          pendingAskUser.delete(sessionId)
          reject(new Error('ask_user timeout (2 min)'))
        }, 120_000)
      })
    }
  }
}

function createManageTasksTool(sendSSEFn: (evt: AgentSSEEvent) => void): AgentTool {
  let tasks: TaskItem[] = []

  return {
    definition: {
      name: 'manage_tasks',
      description: `Manage task list visible to user in real-time.
Actions: "write" (replace all), "create" (add one), "update" (change status), "list" (return all).
Statuses: pending | in_progress | completed | failed.
Best practice: call "write" upfront with all planned tasks, "update" as you complete each.`,
      input_schema: {
        type: 'object',
        properties: {
          action: { type: 'string', enum: ['write', 'create', 'update', 'list'] },
          todos: { type: 'array', items: { type: 'object' }, description: 'For "write": [{title, status}]' },
          title: { type: 'string', description: 'For "create": task title' },
          id: { type: 'string', description: 'For "update": task id' },
          status: { type: 'string', description: 'For "update": new status' }
        },
        required: ['action']
      }
    },
    async execute({ action, todos, title, id, status }) {
      const genId = () => Math.random().toString(36).slice(2, 10)

      if (action === 'write') {
        tasks = (todos || []).map((t: any) => ({
          id: t.id || genId(), title: t.title || t.content,
          status: t.status || 'pending', activeForm: t.activeForm
        }))
      } else if (action === 'create') {
        tasks.push({ id: genId(), title: title || '', status: 'pending' })
      } else if (action === 'update') {
        const t = tasks.find(t => t.id === id)
        if (!t) throw new Error(`Task ${id} not found`)
        if (status) t.status = status as TaskItem['status']
      } else if (action === 'list') {
        return { tasks }
      }
      sendSSEFn({ type: 'task_update', tasks: [...tasks] })
      return { action, task_count: tasks.length, tasks }
    }
  }
}

// === Max iterations ===
const MAX_ITERATIONS = 10

// === Main agent endpoint ===
router.post('/', async (req, res) => {
  const { messages, model, sessionId = crypto.randomUUID() } = req.body

  // SSE headers
  res.setHeader('Content-Type', 'text/event-stream')
  res.setHeader('Cache-Control', 'no-cache')
  res.setHeader('Connection', 'keep-alive')
  res.setHeader('X-Accel-Buffering', 'no')
  res.flushHeaders()

  const send = (evt: AgentSSEEvent) => sendSSE(res, evt)

  // Heartbeat to prevent proxy timeout (reference: CC SSE keepalive)
  const heartbeat = setInterval(() => {
    if (!res.writableEnded) res.write(': heartbeat\n\n')
  }, 20_000)

  // Abort on client disconnect (reference: CC QueryEngine abortController)
  const abortController = new AbortController()
  res.on('close', () => {
    if (!res.writableFinished) {
      console.log('[Agent] Client disconnected, aborting')
      abortController.abort()
    }
    clearInterval(heartbeat)
  })

  try {
    // Build tools: static + stateful factories
    const staticTools = getAgentTools()
    const askUser = createAskUserTool(sessionId, send)
    const manageTasks = createManageTasksTool(send)
    const sendMessage = createSendMessageTool(send)

    const allToolDefs: Anthropic.Tool[] = [
      ...staticTools.map(t => t.definition as Anthropic.Tool),
      askUser.definition as Anthropic.Tool,
      manageTasks.definition as Anthropic.Tool,
      sendMessage.definition as Anthropic.Tool,
    ]

    async function executeToolWithContext(name: string, input: any): Promise<string | object> {
      if (name === 'ask_user') return askUser.execute(input)
      if (name === 'manage_tasks') return manageTasks.execute(input)
      if (name === 'send_message') return sendMessage.execute(input)
      return executeTool(name, input)
    }

    const systemPrompt = await getSomeoAgentSystemPrompt()

    // Prepare conversation messages
    const conversationMessages: Anthropic.MessageParam[] = [...messages]
    let iterations = 0

    // Token usage tracking (reference: CC src/cost-tracker.ts)
    const modelId = model?.id || 'claude-sonnet-4-5-20250929'
    const usage = {
      input_tokens: 0, output_tokens: 0,
      cache_read_input_tokens: 0, cache_creation_input_tokens: 0,
      cost_usd: 0, iterations: 0,
    }

    // CC query.ts: MAX_OUTPUT_TOKENS_RECOVERY_LIMIT for output token exhaustion retry
    const MAX_OUTPUT_TOKENS_RECOVERY = 3
    let outputTokenRecoveryCount = 0

    while (iterations < MAX_ITERATIONS) {
      if (abortController.signal.aborted) break
      iterations++
      usage.iterations = iterations

      // === STEP 1: Normalize messages + call Anthropic API ===
      // CC src/utils/messages.js: normalizeMessagesForAPI() before each API call
      const normalizedMessages = normalizeMessagesForAPI(conversationMessages)
      console.log(`[Agent] Iteration ${iterations}, calling ${modelId} with ${normalizedMessages.length} messages, ${allToolDefs.length} tools`)

      // Use create() with stream:true for raw SSE events
      const stream = await anthropic.messages.create({
        model: modelId,
        system: systemPrompt,
        messages: normalizedMessages,
        tools: allToolDefs,
        max_tokens: 16384,
        stream: true,
      })

      // === STEP 2: Collect streaming response ===
      let currentText = ''
      let currentThinking = ''
      const toolUseBlocks: Array<{ type: 'tool_use'; id: string; name: string; input: any }> = []
      const inputAccumulators: Record<string, string> = {}
      let stopReason = ''
      let eventCount = 0

      for await (const event of stream) {
        eventCount++
        if (abortController.signal.aborted) break

        if (event.type === 'content_block_start') {
          if (event.content_block.type === 'thinking') {
            currentThinking = ''
          }
          if (event.content_block.type === 'tool_use') {
            inputAccumulators[event.content_block.id] = ''
            toolUseBlocks.push({
              type: 'tool_use',
              id: event.content_block.id,
              name: event.content_block.name,
              input: {}
            })
            send({ type: 'tool_call', toolName: event.content_block.name, toolInput: {}, toolUseId: event.content_block.id })
          }
        }

        if (event.type === 'content_block_delta') {
          const delta = event.delta as any
          if (delta.type === 'thinking_delta') {
            currentThinking += delta.thinking
          }
          if (delta.type === 'text_delta') {
            currentText += delta.text
            send({ type: 'text', text: delta.text })
          }
          if (delta.type === 'input_json_delta') {
            const block = toolUseBlocks[toolUseBlocks.length - 1]
            if (block) inputAccumulators[block.id] += delta.partial_json
          }
        }

        if (event.type === 'content_block_stop' && currentThinking) {
          send({ type: 'thinking', text: currentThinking })
          currentThinking = ''
        }

        if (event.type === 'message_start') {
          const u = (event as any).message?.usage
          if (u) {
            usage.input_tokens += u.input_tokens ?? 0
            usage.cache_read_input_tokens += u.cache_read_input_tokens ?? 0
            usage.cache_creation_input_tokens += u.cache_creation_input_tokens ?? 0
          }
        }

        if (event.type === 'message_delta') {
          stopReason = (event as any).delta?.stop_reason || ''
          const u = (event as any).usage
          if (u) usage.output_tokens += u.output_tokens ?? 0
        }
      }

      console.log(`[Agent] Iter ${iterations}: ${eventCount} events, ${currentText.length} chars, ${toolUseBlocks.length} tools, stop=${stopReason}`)

      // Parse complete tool inputs
      for (const block of toolUseBlocks) {
        try { block.input = JSON.parse(inputAccumulators[block.id] || '{}') }
        catch { block.input = {} }
      }

      // Build assistant message for conversation history
      const assistantContent: Anthropic.ContentBlockParam[] = []
      if (currentText) assistantContent.push({ type: 'text', text: currentText })
      assistantContent.push(...toolUseBlocks.map(b => ({
        type: 'tool_use' as const, id: b.id, name: b.name, input: b.input as Record<string, unknown>
      })))
      if (assistantContent.length > 0) {
        conversationMessages.push({ role: 'assistant', content: assistantContent })
      }

      // === STEP 3: Check if done ===
      if (stopReason === 'end_turn') break
      if (toolUseBlocks.length === 0) {
        // CC query.ts: output token recovery — if max_tokens hit, retry up to 3 times
        if (stopReason === 'max_tokens' && outputTokenRecoveryCount < MAX_OUTPUT_TOKENS_RECOVERY) {
          outputTokenRecoveryCount++
          console.log(`[Agent] max_tokens hit, recovery attempt ${outputTokenRecoveryCount}/${MAX_OUTPUT_TOKENS_RECOVERY}`)
          // Continue loop — model will resume from where it left off
          continue
        }
        break
      }
      outputTokenRecoveryCount = 0 // Reset on successful tool_use

      // === STEP 4: Execute tools ===
      const toolResultContent: Anthropic.ToolResultBlockParam[] = []

      for (const toolUse of toolUseBlocks) {
        if (abortController.signal.aborted) break

        // Update tool_call with parsed input
        send({ type: 'tool_call', toolName: toolUse.name, toolInput: toolUse.input, toolUseId: toolUse.id, update: true })

        let result: string
        let isError = false

        try {
          const raw = await executeToolWithContext(toolUse.name, toolUse.input)
          result = typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2)
        } catch (err: any) {
          result = `Error executing ${toolUse.name}: ${err.message}`
          isError = true
        }

        // Truncate large results
        result = truncateResult(result, toolUse.name)

        send({ type: 'tool_result', toolName: toolUse.name, toolResult: result, isError, toolUseId: toolUse.id })
        toolResultContent.push({
          type: 'tool_result',
          tool_use_id: toolUse.id,
          content: result,
          is_error: isError,
        })
      }

      // === STEP 5: Add tool results to conversation ===
      conversationMessages.push({ role: 'user', content: toolResultContent })

      // Reset for next iteration
      currentText = ''
      toolUseBlocks.length = 0
    }

    if (iterations >= MAX_ITERATIONS) {
      send({ type: 'error', text: 'Reached maximum iteration limit (10). Please ask a more specific question.' })
    }

    // Send usage stats (reference: CC cost-tracker formatTotalCost + modelCost.ts calculateCost)
    const price = getModelCost(modelId)
    usage.cost_usd = +(
      (usage.input_tokens * price.input / 1_000_000) +
      (usage.output_tokens * price.output / 1_000_000) +
      (usage.cache_read_input_tokens * price.cacheRead / 1_000_000) +
      (usage.cache_creation_input_tokens * price.cacheWrite / 1_000_000)
    ).toFixed(4)

    send({
      type: 'usage',
      input_tokens: usage.input_tokens,
      output_tokens: usage.output_tokens,
      cache_read_tokens: usage.cache_read_input_tokens,
      cache_write_tokens: usage.cache_creation_input_tokens,
      cost_usd: usage.cost_usd,
      iterations: usage.iterations,
    })

    send({ type: 'done' })
  } catch (err: any) {
    console.error('[Agent Error]', err.message, err.stack?.slice(0, 500))
    send({ type: 'error', text: err.message })
  } finally {
    clearInterval(heartbeat)
    if (!res.writableEnded) res.end()
  }
})

export default router
