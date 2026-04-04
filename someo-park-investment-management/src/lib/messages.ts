import { StanseAgentSchema } from './schema'
import { ExecutionResult } from './types'
import { DeepPartial } from 'ai'

export type MessageText = {
  type: 'text'
  text: string
}

export type MessageCode = {
  type: 'code'
  text: string
}

export type MessageImage = {
  type: 'image'
  image: string
}

export type Message = {
  role: 'assistant' | 'user'
  content: Array<MessageText | MessageCode | MessageImage>
  object?: DeepPartial<StanseAgentSchema>
  result?: ExecutionResult
  // Someo Park artifact triggers
  artifacts?: ArtifactTrigger[]
  // Someo Agent fields
  isAgentMessage?: boolean
  agentSteps?: AgentStep[]
  agentFinalText?: string
  agentUsage?: Extract<AgentStep, { type: 'usage' }>
}

export type ArtifactTrigger = {
  type: string
  title: string
  params?: Record<string, any>
}

// === Someo Agent types ===

export type AgentStep =
  | { type: 'thinking'; text: string }
  | { type: 'tool_call'; toolName: string; toolInput: Record<string, any>; status: 'pending' | 'completed' | 'error'; toolUseId?: string }
  | { type: 'tool_result'; toolName: string; toolResult: string; isError: boolean; toolUseId?: string }
  | { type: 'text'; text: string }
  | { type: 'task_update'; tasks: TaskItem[] }
  | { type: 'ask_user'; question: string; options?: string[] }
  | { type: 'usage'; input_tokens: number; output_tokens: number; cache_read_tokens: number; cache_write_tokens: number; cost_usd: number; iterations: number }

export interface TaskItem {
  id: string
  title: string
  status: 'pending' | 'in_progress' | 'completed' | 'failed'
  activeForm?: string
}

export interface AgentSSEEvent {
  type: string
  [key: string]: any
}

export function toAISDKMessages(messages: Message[]) {
  return messages.map((message) => ({
    role: message.role,
    content: message.content.map((content) => {
      if (content.type === 'code') {
        return {
          type: 'text' as const,
          text: content.text,
        }
      }
      return content
    }),
  }))
}

export async function toMessageImage(files: File[]) {
  if (files.length === 0) {
    return []
  }

  return Promise.all(
    files.map(async (file) => {
      const base64 = Buffer.from(await file.arrayBuffer()).toString('base64')
      return `data:${file.type};base64,${base64}`
    }),
  )
}
