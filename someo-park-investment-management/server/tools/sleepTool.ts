// server/tools/sleepTool.ts
// Reference: CC src/tools/SleepTool/prompt.ts
// Simple wait tool — for rate limiting, polling delays between tool calls

import type { AgentTool } from './index.js'

export const sleepTool: AgentTool = {
  definition: {
    name: 'sleep',
    description: 'Wait for a specified duration (seconds). Use for rate limiting between API calls or waiting for background tasks. Max 60 seconds.',
    input_schema: {
      type: 'object',
      properties: {
        duration: { type: 'number', description: 'Duration to sleep in seconds (max 60)' },
      },
      required: ['duration']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ duration }) {
    const clamped = Math.min(Math.max(duration, 0.1), 60)
    await new Promise(resolve => setTimeout(resolve, clamped * 1000))
    return { slept_seconds: clamped }
  }
}
