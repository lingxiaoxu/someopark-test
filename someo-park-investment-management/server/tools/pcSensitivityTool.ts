// server/tools/pcSensitivityTool.ts
// Sensitivity analysis — vary 1-2 parameters across a range
import type { AgentTool } from './index.js'

export const pcSensitivityTool: AgentTool = {
  definition: {
    name: 'pc_sensitivity',
    description: 'Run sensitivity analysis on a Private Credit model. Varies one or two input parameters across a range and shows how outputs (IRR, MOIC, etc.) change. Useful for "what-if" scenarios, finding break-even points, and risk analysis.',
    input_schema: {
      type: 'object' as const,
      properties: {
        model: { type: 'string', description: 'Model name' },
        base_inputs: { type: 'object', description: 'Base case input overrides' },
        vary_param: { type: 'string', description: 'Parameter name to vary (e.g. "purchase_price", "coupon")' },
        range: {
          type: 'object',
          properties: {
            min: { type: 'number' },
            max: { type: 'number' },
            steps: { type: 'number' }
          },
          description: 'Range to sweep: {min, max, steps}'
        },
        vary_param_2: { type: 'string', description: 'Optional second parameter for 2D sensitivity table' },
        range_2: {
          type: 'object',
          properties: {
            min: { type: 'number' },
            max: { type: 'number' },
            steps: { type: 'number' }
          }
        },
      },
      required: ['model', 'vary_param', 'range']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ model: modelName, base_inputs = {}, vary_param, range, vary_param_2, range_2 }) {
    const { getModel } = await import('./privateCredit/index.js')
    const model = getModel(modelName)
    if (!model) return { error: `Model "${modelName}" not found.` }

    const steps = range.steps || 5
    const stepSize = (range.max - range.min) / steps

    if (vary_param_2 && range_2) {
      // 2D sensitivity
      const steps2 = range_2.steps || 5
      const stepSize2 = (range_2.max - range_2.min) / steps2
      const table: any[] = []

      for (let i = 0; i <= steps; i++) {
        const v1 = range.min + i * stepSize
        for (let j = 0; j <= steps2; j++) {
          const v2 = range_2.min + j * stepSize2
          const inputs = { ...base_inputs, [vary_param]: v1, [vary_param_2]: v2 }
          try {
            const result = model.compute(inputs)
            table.push({
              [vary_param]: Math.round(v1 * 1e6) / 1e6,
              [vary_param_2]: Math.round(v2 * 1e6) / 1e6,
              ...result.outputs
            })
          } catch (e: any) {
            table.push({
              [vary_param]: v1,
              [vary_param_2]: v2,
              error: e.message
            })
          }
        }
      }

      return {
        model: modelName,
        type: '2D',
        vary: [vary_param, vary_param_2],
        results: table
      }
    }

    // 1D sensitivity
    const results: any[] = []
    for (let i = 0; i <= steps; i++) {
      const v = range.min + i * stepSize
      const inputs = { ...base_inputs, [vary_param]: v }
      try {
        const result = model.compute(inputs)
        results.push({
          [vary_param]: Math.round(v * 1e6) / 1e6,
          ...result.outputs
        })
      } catch (e: any) {
        results.push({ [vary_param]: v, error: e.message })
      }
    }

    return {
      model: modelName,
      type: '1D',
      vary: vary_param,
      results
    }
  }
}
