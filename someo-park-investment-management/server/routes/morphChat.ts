import { Router, Request, Response } from 'express'
import { generateObject, LanguageModel, ModelMessage } from 'ai'
import { getModelClient, LLMModel, LLMModelConfig } from '../utils/models.js'
import { applyPatch } from '../utils/morph.js'
import { StanseAgentSchema, morphEditSchema } from '../../src/lib/schema.js'

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

  const { apiKey: modelApiKey, ...modelParams } = config
  const modelClient = getModelClient(model, config)

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
