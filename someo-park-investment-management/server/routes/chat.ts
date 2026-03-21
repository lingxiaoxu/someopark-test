import { Router, Request, Response } from 'express'
import { streamObject, generateText, LanguageModel, ModelMessage } from 'ai'
import { getModelClient, LLMModel, LLMModelConfig } from '../utils/models.js'
import { toPrompt, toChatPrompt } from '../utils/prompt.js'
import { stanseAgentSchema as schema } from '../../src/lib/schema.js'
import { detectArtifacts } from '../utils/artifactDetector.js'
import templates from '../../src/lib/templates.js'

const router = Router()

// Keywords that indicate a code/app generation request
const CODE_KEYWORDS = [
  'build', 'create', 'make', 'generate', 'program', 'code', 'develop', 'write',
  'app', 'application', 'website', 'page', 'tool', 'game', 'bot',
  'calculator', 'graph', 'form', 'server', 'database',
  'function', 'script', 'component', 'widget', 'interface', 'ui', 'frontend',
  'backend', 'deploy', 'fix', 'debug', 'refactor',
  'streamlit', 'react', 'next', 'python', 'javascript', 'typescript', 'html', 'css',
  'plotly', 'pandas', 'numpy',
  '编写', '编程', '程序', '应用', '网站', '页面',
  '修改', '修复',
]

function isCodeRequest(text: string): boolean {
  if (!text || text.length === 0) return false
  const lower = text.toLowerCase()
  return CODE_KEYWORDS.some(keyword => lower.includes(keyword))
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
