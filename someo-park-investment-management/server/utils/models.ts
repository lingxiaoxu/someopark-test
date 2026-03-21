import { createAnthropic } from '@ai-sdk/anthropic'
import { createGoogleGenerativeAI } from '@ai-sdk/google'
import { createOpenAI } from '@ai-sdk/openai'
import { createOllama } from 'ollama-ai-provider'

export type LLMModel = {
  id: string
  name: string
  provider: string
  providerId: string
}

export type LLMModelConfig = {
  model?: string
  apiKey?: string
  baseURL?: string
  temperature?: number
  topP?: number
  topK?: number
  frequencyPenalty?: number
  presencePenalty?: number
  maxTokens?: number
}

export function getModelClient(model: LLMModel, config: LLMModelConfig) {
  const { id: modelNameString, providerId } = model
  // Ensure empty string is treated as undefined (fallback to env var)
  const apiKey = config.apiKey || undefined
  const baseURL = config.baseURL || undefined

  const anthropicKey = apiKey || process.env.ANTHROPIC_API_KEY
  const openaiKey = apiKey || process.env.OPENAI_API_KEY
  const googleKey = apiKey || process.env.GOOGLE_AI_API_KEY

  const providerConfigs = {
    anthropic: () =>
      createAnthropic({
        apiKey: anthropicKey,
        ...(baseURL ? { baseURL } : {}),
      })(modelNameString),
    openai: () =>
      createOpenAI({
        apiKey: openaiKey,
        ...(baseURL ? { baseURL } : {}),
      })(modelNameString),
    google: () =>
      createGoogleGenerativeAI({
        apiKey: googleKey,
        ...(baseURL ? { baseURL } : {}),
      })(modelNameString),
    ollama: () => createOllama({ baseURL })(modelNameString),
  }

  const createClient =
    providerConfigs[providerId as keyof typeof providerConfigs]

  if (!createClient) {
    throw new Error(`Unsupported provider: ${providerId}`)
  }

  return createClient()
}
