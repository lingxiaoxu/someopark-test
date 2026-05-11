// server/tools/pcReadModelTool.ts
// Reads a specific PC model definition — inputs, outputs, logic description
import type { AgentTool } from './index.js'

export const pcReadModelTool: AgentTool = {
  definition: {
    name: 'pc_read_model',
    description: 'Read a specific Private Credit model from the Excel template. Returns all inputs (with default values), formulas/logic description, cash flow schedule structure, and outputs. Use this to understand how a model works before running pc_compute.',
    input_schema: {
      type: 'object' as const,
      properties: {
        model: { type: 'string', description: 'Model name (e.g. "VCOP_Secondary+NAV", "Waterfall_Euro")' },
      },
      required: ['model']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ model: modelName }) {
    const { getModel, listModels } = await import('./privateCredit/index.js')
    const model = getModel(modelName)
    if (!model) {
      const all = listModels()
      return {
        error: `Model "${modelName}" not found.`,
        available: all.map((m: any) => m.name)
      }
    }

    return {
      name: model.name,
      sheet: model.sheetName,
      fund: model.fund,
      description: model.description,
      inputs: model.inputs.map((inp: any) => ({
        name: inp.name,
        label: inp.label,
        type: inp.type,
        default: inp.default,
        description: inp.description
      })),
      outputs: model.outputs.map((out: any) => ({
        name: out.name,
        label: out.label,
        type: out.type
      }))
    }
  }
}
