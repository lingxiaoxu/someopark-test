import { Router, Request, Response } from 'express'
import { generateObject, generateText, LanguageModel, ModelMessage } from 'ai'
import { getModelClient, LLMModel, LLMModelConfig } from '../utils/models.js'
import { applyPatch } from '../utils/morph.js'
import { StanseAgentSchema, morphEditSchema } from '../../src/lib/schema.js'
import { detectArtifacts } from '../utils/artifactDetector.js'
import { toChatPrompt } from '../utils/prompt.js'

// Same code-intent patterns as chat.ts
const CODE_INTENT_PATTERNS = [
  /\b(build|create|make|generate|write|program|develop|code|implement)\s+(me\s+)?(a|an|the|some|us\s+a)?\s*\w/i,
  /\b(show|give)\s+(me\s+)?(some\s+)?code\b/i,
  /\b(change|update|modify|add|remove|rename|replace|refactor|fix)\s+.{0,50}(color|font|text|style|class|function|variable|line|button|layout|size|margin|padding|border)/i,
  /\b(streamlit|nextjs|next\.js|react\s+app|vue\s+app|gradio|flask|fastapi)\b/i,
  /\b(python\s+script|javascript|typescript|html\s+page|pandas|numpy|plotly)\b/i,
  /\b(web\s+app|dashboard\s+app|chatbot|calculator|game|widget|component)\b/i,
  /\b(fix|debug|refactor)\s+(this|the|my)\s+(code|bug|error|script|app)\b/i,
  /写代码|编写代码|帮我写|生成代码|写一个|做一个|开发一个|创建一个|制作一个|修复这段|帮我做/,
  /改成|修改|更改|把.*改|将.*改|换成|变成/,
]

function isCodeEditRequest(text: string): boolean {
  if (!text || text.length === 0) return false
  return CODE_INTENT_PATTERNS.some(pattern => pattern.test(text))
}

const router = Router()

router.post('/', async (req: Request, res: Response) => {
  const {
    messages,
    model,
    config,
    currentStanseAgent,
  }: {
    messages: ModelMessage[]
    model: LLMModel
    config: LLMModelConfig
    currentStanseAgent: StanseAgentSchema
  } = req.body

  const { apiKey: _ak, model: _m, baseURL: _bu, ...modelParams } = config
  const modelClient = getModelClient(model, config)

  // Extract last message text
  const lastMessage = messages[messages.length - 1]
  let lastContent = ''
  if (lastMessage) {
    if (typeof lastMessage.content === 'string') {
      lastContent = lastMessage.content.trim()
    } else if (Array.isArray(lastMessage.content)) {
      lastContent = (lastMessage.content as any[])
        .filter((p: any) => p.type === 'text' && p.text)
        .map((p: any) => p.text)
        .join(' ')
        .trim()
    }
  }

  // If currentStanseAgent has code, assume user wants to modify it — skip intent check.
  // Only fall back to chat if there's no active code AND it doesn't look like a code request.
  const hasActiveCode = currentStanseAgent?.code && currentStanseAgent.code.length > 0
  if (!hasActiveCode && !isCodeEditRequest(lastContent)) {
    const detectedArtifacts = detectArtifacts(lastContent)
    try {
      const result = await generateText({
        model: modelClient as LanguageModel,
        system: toChatPrompt(),
        messages,
        maxTokens: 8192,
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
      return
    } catch (error: any) {
      res.status(500).json({ error: error.message || 'Internal server error' })
      return
    }
  }

  try {
    const contextualSystemPrompt = `You are modifying code. Output JSON with the "edit" field containing the modified code.

FILE: ${currentStanseAgent.file_path}

CURRENT CODE:
\`\`\`
${currentStanseAgent.code}
\`\`\`

OUTPUT JSON FORMAT:
{
  "edit": "<your modified code here>",
  "commentary": "what you changed",
  "instruction": "one line summary",
  "new_dependencies": ["new_package"]
}

The "edit" field MUST contain the modified code. Use "# ... existing code ..." to mark unchanged sections.`

    const result = await generateObject({
      model: modelClient as LanguageModel,
      system: contextualSystemPrompt,
      messages,
      schema: morphEditSchema,
      maxRetries: 2,
      maxTokens: 16384,
      ...modelParams,
    })

    const editInstructions = result.object

    if (!editInstructions.edit) {
      throw new Error('AI did not return the required "edit" field with code changes')
    }

    const morphResult = await applyPatch({
      targetFile: currentStanseAgent.file_path,
      instructions: editInstructions.instruction || 'Apply changes',
      initialCode: currentStanseAgent.code,
      codeEdit: editInstructions.edit,
    })

    const existingDeps = currentStanseAgent.additional_dependencies || []
    const newDeps = editInstructions.new_dependencies || []
    const mergedDeps = Array.from(new Set([...existingDeps, ...newDeps]))

    const updatedStanseAgent: StanseAgentSchema = {
      ...currentStanseAgent,
      code: morphResult.code,
      commentary: editInstructions.commentary || 'Code modified',
      additional_dependencies: mergedDeps,
      has_additional_dependencies: mergedDeps.length > 0,
      install_dependencies_command: mergedDeps.length > 0
        ? `pip install ${mergedDeps.join(' ')}`
        : currentStanseAgent.install_dependencies_command,
    }

    res.setHeader('Content-Type', 'text/plain; charset=utf-8')
    res.write(JSON.stringify(updatedStanseAgent))
    res.end()
  } catch (error: any) {
    console.error('Morph chat error:', error)
    const statusCode = error.status || 500
    res.status(statusCode).json({ error: error.message || 'Internal server error' })
  }
})

export default router
