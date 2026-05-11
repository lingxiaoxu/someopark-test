// server/tools/pcComputeTool.ts
// Runs a Private Credit model calculation with custom inputs
import type { AgentTool } from './index.js'

// Lazy import to avoid circular dependency at module load
let getModelFn: ((name: string) => any) | null = null
async function getModel(name: string) {
  if (!getModelFn) {
    const mod = await import('./privateCredit/index.js')
    getModelFn = mod.getModel
  }
  return getModelFn(name)
}

export const pcComputeTool: AgentTool = {
  definition: {
    name: 'pc_compute',
    description: 'Run a Private Credit model calculation with custom inputs. Returns full cash flow schedule, derived values, IRR, MOIC, and all outputs. Supports overriding any input parameter from the Excel template defaults. Use pc_list_models first to see available models, and pc_read_model to understand inputs.',
    input_schema: {
      type: 'object' as const,
      properties: {
        model: { type: 'string', description: 'Model name (e.g. "VCOP_Secondary+NAV", "Waterfall_Euro", "Portfolio_HHI")' },
        inputs: { type: 'object', description: 'Input overrides as key-value pairs, e.g. {"purchase_price": 85, "coupon_rate": 0.08}. Unspecified inputs use Excel template defaults.' },
        show_cashflows: { type: 'boolean', description: 'Include detailed period-by-period cash flows in output (default: true)' },
      },
      required: ['model']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ model: modelName, inputs = {}, show_cashflows = true }) {
    const model = await getModel(modelName)
    if (!model) {
      // Try fuzzy match
      const { listModels } = await import('./privateCredit/index.js')
      const all = listModels()
      const lower = modelName.toLowerCase()
      const match = all.find((m: any) =>
        m.name.toLowerCase().includes(lower) ||
        lower.includes(m.name.toLowerCase().replace(/[_+]/g, ''))
      )
      if (match) {
        const matched = await getModel(match.name)
        if (matched) {
          const result = matched.compute(inputs)
          if (!show_cashflows) delete result.cashflows
          return result
        }
      }
      return {
        error: `Model "${modelName}" not found.`,
        available: all.map((m: any) => m.name)
      }
    }

    const result = model.compute(inputs)
    if (!show_cashflows) delete result.cashflows
    return result
  }
}
