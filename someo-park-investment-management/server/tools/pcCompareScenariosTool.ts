// server/tools/pcCompareScenariosTool.ts
// Compare multiple scenarios of a PC model side by side
import type { AgentTool } from './index.js'

export const pcCompareScenariosTool: AgentTool = {
  definition: {
    name: 'pc_compare_scenarios',
    description: 'Compare multiple scenarios of a Private Credit model side by side. Each scenario has different input parameters. Returns a comparison table of all key outputs (IRR, MOIC, etc.) for easy analysis.',
    input_schema: {
      type: 'object' as const,
      properties: {
        model: { type: 'string', description: 'Model name' },
        scenarios: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              name: { type: 'string', description: 'Scenario label (e.g. "Base Case", "Bull", "Bear")' },
              inputs: { type: 'object', description: 'Input overrides for this scenario' },
            }
          },
          description: 'Array of scenarios to compare'
        },
      },
      required: ['model', 'scenarios']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ model: modelName, scenarios }) {
    const { getModel } = await import('./privateCredit/index.js')
    const model = getModel(modelName)
    if (!model) return { error: `Model "${modelName}" not found.` }

    const results = scenarios.map((scenario: any) => {
      try {
        const result = model.compute(scenario.inputs || {})
        return {
          scenario: scenario.name || 'Unnamed',
          inputs: scenario.inputs || {},
          outputs: result.outputs,
        }
      } catch (e: any) {
        return {
          scenario: scenario.name || 'Unnamed',
          error: e.message,
        }
      }
    })

    return {
      model: modelName,
      comparison: results
    }
  }
}
