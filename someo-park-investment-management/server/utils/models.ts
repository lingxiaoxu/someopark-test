import { createAnthropic } from '@ai-sdk/anthropic'
import { createOpenAI } from '@ai-sdk/openai'

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
  // Open-source models are served by a (possibly remote) Ollama host through its
  // OpenAI-compatible endpoint (.../v1). The native ollama-ai-provider package only
  // implements AI SDK provider spec v1, which AI SDK v6 rejects — so we drive Ollama
  // via @ai-sdk/openai instead (spec v2). UI override > env (remote Tailscale host) >
  // localhost default. apiKey is required by the SDK but ignored by Ollama.
  const ollamaBaseURL = baseURL || process.env.OLLAMA_BASE_URL || 'http://localhost:11434/v1'

  const providerConfigs = {
    // Claude (Anthropic) — hosted API.
    anthropic: () =>
      createAnthropic({
        apiKey: anthropicKey,
        ...(baseURL ? { baseURL } : {}),
      })(modelNameString),
    // Open-source model on an Ollama host (OpenAI-compatible /v1 endpoint).
    ollama: () =>
      createOpenAI({
        baseURL: ollamaBaseURL,
        apiKey: apiKey || 'ollama',
      }).chat(modelNameString),
  }

  const createClient =
    providerConfigs[providerId as keyof typeof providerConfigs]

  if (!createClient) {
    throw new Error(`Unsupported provider: ${providerId} (supported: 'anthropic', 'ollama')`)
  }

  return createClient()
}
