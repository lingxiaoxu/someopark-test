import { Router, Request, Response } from 'express'
import { streamObject, generateText, LanguageModel, ModelMessage } from 'ai'
import { getModelClient, LLMModel, LLMModelConfig } from '../utils/models.js'
import { toPrompt, toChatPrompt } from '../utils/prompt.js'
import { stanseAgentSchema as schema } from '../../src/lib/schema.js'
import { detectArtifacts } from '../utils/artifactDetector.js'
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

router.post('/', async (req: Request, res: Response) => {
  const {
    messages,
    userID,
    model,
    config,
  }: {
    messages: ModelMessage[]
    userID: string | undefined
    model: LLMModel
    config: LLMModelConfig
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

  // Detect artifacts in the message
  const detectedArtifacts = detectArtifacts(lastContent)

  const modelClient = getModelClient(model, config)
  const isHaikuModel = model.id.includes('haiku')

  // Decide: code generation (streamObject) vs normal chat (generateText)
  if (isCodeRequest(lastContent)) {
    // === Code generation path: structured output via streamObject ===
    console.log('Code request detected:', lastContent.substring(0, 80))
    const defaultMaxTokens = isHaikuModel ? 8192 : 16000

    try {
      const stream = await streamObject({
        model: modelClient as LanguageModel,
        schema,
        system: toPrompt(templates),
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

    try {
      const result = await generateText({
        model: modelClient as LanguageModel,
        system: toChatPrompt(),
        messages,
        maxTokens: modelParams.maxTokens || defaultMaxTokens,
        ...modelParams,
      })

      const chatResponse = {
        commentary: result.text,
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
