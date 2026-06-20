import { Router, Request, Response } from 'express'
import { streamObject, generateText, LanguageModel, ModelMessage } from 'ai'
import { getModelClient, LLMModel, LLMModelConfig } from '../utils/models.js'
import { toPrompt, toChatPrompt } from '../utils/prompt.js'
import { stanseAgentSchema as schema } from '../../src/lib/schema.js'
import { detectArtifacts } from '../utils/artifactDetector.js'
import { predictionContextForArtifacts } from '../tools/predictionMarketTool.js'
import templates from '../../src/lib/templates.js'

const router = Router()

// Regex patterns for code generation intent
const CODE_INTENT_PATTERNS = [
  // English: action verb + (me/us/a/an/me a/...) + thing
  /\b(build|create|make|generate|write|program|develop|code|implement)\s+(me\s+)?(a|an|the|some|us\s+a)?\s*\w/i,
  // "show me code", "give me code"
  /\b(show|give)\s+(me\s+)?(some\s+)?code\b/i,
  // Tech stack names
  /\b(streamlit|nextjs|next\.js|react\s+app|vue\s+app|gradio|flask|fastapi)\b/i,
  /\b(python\s+script|javascript|typescript|html\s+page|pandas|numpy|plotly)\b/i,
  // App type nouns when used as build targets
  /\b(web\s+app|dashboard\s+app|chatbot|calculator|game|widget|component)\b/i,
  // Fix/debug existing code
  /\b(fix|debug|refactor)\s+(this|the|my)\s+(code|bug|error|script|app)\b/i,
  // Chinese
  /写代码|编写代码|帮我写|生成代码|写一个|做一个|开发一个|创建一个|制作一个|修复这段|帮我做/,
]

function isCodeRequest(text: string): boolean {
  if (!text || text.length === 0) return false
  return CODE_INTENT_PATTERNS.some(pattern => pattern.test(text))
}

// Clean reasoning-model leakage from a normal chat reply (e.g. nemotron emits <think>…</think>
// and, when told about views, sometimes prints a bare {"view": …} tool-call blob instead of prose).
// Safety net only — the prompt already forbids these; if stripping would empty the reply we keep
// the original so the user still sees something.
// Native Ollama /api/chat — the ONLY endpoint that honours the `think` flag (the OpenAI-
// compat /v1 path the AI SDK uses ignores it). Used solely when the user turns thinking OFF
// for a local (ollama) model; the default (thinking ON) keeps the existing /v1 path untouched,
// so default behaviour is byte-identical to before.
async function ollamaNativeChat(opts: {
  baseURL?: string; model: string; system: string; messages: ModelMessage[]
  think: boolean; maxTokens: number
}): Promise<string> {
  const host = (opts.baseURL || process.env.OLLAMA_BASE_URL || 'http://localhost:11434/v1').replace(/\/v1\/?$/, '')
  const flat = (c: any): string =>
    typeof c === 'string' ? c
      : Array.isArray(c) ? c.filter((p: any) => p?.type === 'text' && p.text).map((p: any) => p.text).join('\n') : ''
  const msgs = [
    { role: 'system', content: opts.system },
    ...opts.messages.map((m: any) => ({ role: m.role, content: flat(m.content) })),
  ]
  const r = await fetch(`${host}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model: opts.model, messages: msgs, think: opts.think, stream: false,
      options: { num_predict: opts.maxTokens },
    }),
  })
  if (!r.ok) throw new Error(`Ollama /api/chat ${r.status}: ${(await r.text().catch(() => '')).slice(0, 200)}`)
  const j: any = await r.json()
  return j?.message?.content || ''
}

function sanitizeChatText(s: string): string {
  if (!s) return s
  let t = s
  t = t.replace(/<think>[\s\S]*?<\/think>/gi, '')        // paired think blocks
  const close = t.lastIndexOf('</think>')                 // orphan opening (reasoning leaked, no close shown)
  if (close !== -1) t = t.slice(close + '</think>'.length)
  t = t.replace(/^\s*(\{\s*"view"\s*:[\s\S]*?\}\s*)+/i, '') // leading bare {"view":…} tool-call blob(s)
  t = t.trim()
  return t || s.trim()
}

router.post('/', async (req: Request, res: Response) => {
  const {
    messages,
    userID,
    model,
    config,
    selectedTemplate,
    appMode,
    think,
  }: {
    messages: ModelMessage[]
    userID: string | undefined
    model: LLMModel
    config: LLMModelConfig
    selectedTemplate?: string
    appMode?: 'stock' | 'prediction'
    think?: boolean   // local (ollama) reasoning toggle; undefined/true = thinking ON (default)
  } = req.body

  console.log('Chat request:', { userID, model: model?.id })

  // Extract only valid generation parameters from config (exclude model, apiKey, baseURL)
  const { apiKey: _ak, model: _m, baseURL: _bu, ...modelParams } = config

  const lastMessage = messages[messages.length - 1]
  // content can be a string or an array of { type, text } parts
  let lastContent = ''
  if (lastMessage) {
    if (typeof lastMessage.content === 'string') {
      lastContent = lastMessage.content.toLowerCase().trim()
    } else if (Array.isArray(lastMessage.content)) {
      lastContent = (lastMessage.content as any[])
        .filter((p: any) => p.type === 'text' && p.text)
        .map((p: any) => p.text)
        .join(' ')
        .toLowerCase()
        .trim()
    }
  }

  // Detect artifacts in the message (mode-scoped: prediction mode → wc_* views only).
  const detectedArtifacts = detectArtifacts(lastContent, appMode)

  // Resolve the model client up front. A bad/missing provider must NOT crash the whole
  // process (it used to throw uncaught here, taking the server down for every user) —
  // return a 400 instead.
  let modelClient
  try {
    modelClient = getModelClient(model, config)
  } catch (e: any) {
    return res.status(400).json({ error: e?.message || 'Invalid model/provider' })
  }
  const isHaikuModel = (model?.id || '').includes('haiku')

  // Decide: code generation (streamObject) vs normal chat (generateText)
  if (isCodeRequest(lastContent)) {
    // === Code generation path: structured output via streamObject ===
    console.log('Code request detected:', lastContent.substring(0, 80))
    const defaultMaxTokens = isHaikuModel ? 8192 : 16000

    try {
      const stream = await streamObject({
        model: modelClient as LanguageModel,
        schema,
        system: toPrompt(templates, selectedTemplate),
        messages,
        maxRetries: 0,
        maxTokens: modelParams.maxTokens || defaultMaxTokens,
        ...modelParams,
      })

      res.setHeader('Content-Type', 'text/plain; charset=utf-8')
      res.setHeader('Cache-Control', 'no-cache')
      res.setHeader('Connection', 'keep-alive')

      for await (const chunk of stream.textStream) {
        res.write(chunk)
      }

      if (detectedArtifacts.length > 0) {
        res.write('\n__ARTIFACTS__' + JSON.stringify(detectedArtifacts))
      }

      // Forced role: when the user explicitly picks a role (non-"auto"), pin the
      // template to that choice — never trust the model's `template` field (weaker
      // local models often pick the wrong one). The frontend applies this marker.
      if (selectedTemplate && selectedTemplate !== 'auto') {
        res.write('\n__TEMPLATE__' + selectedTemplate)
      }

      res.end()
    } catch (error: any) {
      console.error('Chat error (code):', error)
      const statusCode = error.status || 500
      const message = error.message || 'Internal server error'
      if (!res.headersSent) {
        res.status(statusCode).json({ error: message })
      }
    }
  } else {
    // === Normal chat path: call LLM with generateText ===
    console.log('Normal chat:', lastContent.substring(0, 80))
    const defaultMaxTokens = isHaikuModel ? 4096 : 8192

    // Grounding: if a World Cup view was detected, hand the model the SAME real data the
    // panel shows so its prose matches the numbers (the non-agent chat has no real
    // tool-calling). Only the chat prompt is augmented — the coding path is untouched.
    let chatSystem = toChatPrompt()
    const wcTypes = detectedArtifacts.map(a => a.type).filter(t => t.startsWith('wc_'))
    if (wcTypes.length > 0) {
      try {
        const ctx = await predictionContextForArtifacts(wcTypes)
        if (ctx) {
          chatSystem +=
            '\n\n## World Cup 2026 prediction-market data (authoritative — these are the ' +
            'live numbers on the panel the user is seeing; answer ONLY from them, do not ' +
            'invent figures, and say so if a value is absent):\n' + ctx
        }
      } catch (e) {
        console.error('prediction grounding failed (continuing without it):', e)
      }
    }

    try {
      // Thinking toggle: only a LOCAL (ollama) model with think===false takes the native
      // /api/chat path (which truly disables reasoning → faster, no empty/timeout). Everything
      // else (Claude, or thinking ON/default) keeps the existing /v1 generateText path verbatim.
      const maxTokens = modelParams.maxTokens || defaultMaxTokens
      const text = (model?.providerId === 'ollama' && think === false)
        ? await ollamaNativeChat({ baseURL: config.baseURL, model: model.id, system: chatSystem,
                                   messages, think: false, maxTokens })
        : (await generateText({
            model: modelClient as LanguageModel,
            system: chatSystem,
            messages,
            maxTokens,
            ...modelParams,
          })).text

      const chatResponse = {
        commentary: sanitizeChatText(text),
        template: 'chat-response',
        title: 'Chat',
        description: 'Chat response',
        additional_dependencies: [],
        has_additional_dependencies: false,
        install_dependencies_command: '',
        port: null,
        file_path: '',
        code: '',
        _artifacts: detectedArtifacts,
      }

      res.setHeader('Content-Type', 'text/plain; charset=utf-8')
      res.write(JSON.stringify(chatResponse))
      res.end()
    } catch (error: any) {
      console.error('Chat error (normal):', error)
      const statusCode = error.status || 500
      const message = error.message || 'Internal server error'
      if (!res.headersSent) {
        res.status(statusCode).json({ error: message })
      }
    }
  }
})

export default router
