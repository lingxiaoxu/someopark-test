// server/tools/sendMessageTool.ts
// Reference: CC src/tools/BriefTool/BriefTool.ts
// Agent proactively sends a message to the user mid-analysis (before final answer)
// Adapted to SSE: sends { type: 'brief' } event to frontend

import type { AgentTool } from './index.js'

// Factory: needs sendSSE function from per-request context
export function createSendMessageTool(sendSSE: (evt: any) => void): AgentTool {
  return {
    definition: {
      name: 'send_message',
      description: `Send a message to the user during analysis — for interim updates, progress notes, or proactive alerts without waiting for final answer. Use status="proactive" for unsolicited updates.`,
      input_schema: {
        type: 'object',
        properties: {
          message: { type: 'string', description: 'Markdown message to display to user' },
          status: { type: 'string', enum: ['normal', 'proactive'], description: 'normal = reply to user, proactive = unsolicited update' },
        },
        required: ['message']
      }
    },
    async execute({ message, status = 'normal' }) {
      sendSSE({ type: 'brief', message, status })
      return { delivered: true, status, sentAt: new Date().toISOString() }
    }
  }
}
