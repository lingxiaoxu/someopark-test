// server/tools/calculatorTool.ts
// Safe math evaluation via mathjs (no eval)

import * as math from 'mathjs'
import type { AgentTool } from './index.js'

export const calculatorTool: AgentTool = {
  definition: {
    name: 'calculate',
    description: 'Evaluate a math expression safely. Supports arithmetic, trig, log, sqrt, abs, etc. Example: "sqrt(2) * 100 / 3"',
    input_schema: {
      type: 'object',
      properties: {
        expression: { type: 'string', description: 'Math expression to evaluate' }
      },
      required: ['expression']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ expression }) {
    const result = math.evaluate(expression)
    return { expression, result: result?.toString?.() ?? String(result) }
  }
}
