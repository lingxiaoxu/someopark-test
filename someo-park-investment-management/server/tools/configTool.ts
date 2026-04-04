// server/tools/configTool.ts
// Reference: CC src/tools/ConfigTool/ConfigTool.ts
// Get/set Someo Agent config (simplified for SP — no voice mode, no LSP)

import type { AgentTool } from './index.js'

// SP config (stored in process memory, could be extended to database)
const spConfig: Record<string, any> = {
  default_strategy: process.env.SP_DEFAULT_STRATEGY || 'both',
  default_model: process.env.SP_DEFAULT_MODEL || 'claude-sonnet-4-5-20250929',
  language: 'zh',
  agent_max_iterations: 10,
  tool_timeout_seconds: 30,
  risk_alert_threshold: 0.8,
}

const VALID_SETTINGS: Record<string, { type: string; options?: string[]; description: string }> = {
  default_strategy: { type: 'string', options: ['mrpt', 'mtfs', 'both'], description: 'Default strategy for queries' },
  default_model: { type: 'string', description: 'Default LLM model ID' },
  language: { type: 'string', options: ['zh', 'en'], description: 'Response language' },
  agent_max_iterations: { type: 'number', description: 'Max agent loop iterations (1-20)' },
  tool_timeout_seconds: { type: 'number', description: 'Tool execution timeout (5-120)' },
  risk_alert_threshold: { type: 'number', description: 'Risk alert threshold (0-1)' },
}

export const configTool: AgentTool = {
  definition: {
    name: 'get_set_config',
    description: `Get or set Someo Agent configuration.
Available settings: ${Object.entries(VALID_SETTINGS).map(([k, v]) => `${k} (${v.type}${v.options ? ': ' + v.options.join('/') : ''}) — ${v.description}`).join('; ')}
Omit value to read current setting.`,
    input_schema: {
      type: 'object',
      properties: {
        setting: { type: 'string', description: 'Setting name' },
        value: { type: 'string', description: 'New value (omit to read current)' },
      },
      required: ['setting']
    }
  },
  async execute({ setting, value }) {
    const config = VALID_SETTINGS[setting]
    if (!config) {
      return {
        success: false,
        error: `Unknown setting: "${setting}". Available: ${Object.keys(VALID_SETTINGS).join(', ')}`,
      }
    }

    // GET operation (CC pattern)
    if (value === undefined || value === null) {
      return {
        success: true,
        operation: 'get',
        setting,
        value: spConfig[setting],
      }
    }

    // SET operation — validate
    const previousValue = spConfig[setting]

    // Type coercion (CC pattern: boolean/number coercion)
    let finalValue: any = value
    if (config.type === 'number') {
      finalValue = parseFloat(value)
      if (isNaN(finalValue)) {
        return { success: false, operation: 'set', setting, error: `${setting} requires a number` }
      }
    }

    // Options validation (CC pattern)
    if (config.options && !config.options.includes(String(finalValue))) {
      return {
        success: false, operation: 'set', setting,
        error: `Invalid value "${value}". Options: ${config.options.join(', ')}`,
      }
    }

    spConfig[setting] = finalValue
    return {
      success: true,
      operation: 'set',
      setting,
      previousValue,
      newValue: finalValue,
    }
  }
}
