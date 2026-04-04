// server/tools/index.ts
// Tool registry framework for Someo Agent
// Reference: CC src/Tool.ts (Tool interface), src/tools.ts (getAllTools pattern)

// === CC src/Tool.ts: AgentTool interface ===
export interface AgentTool {
  definition: {
    name: string
    description: string
    input_schema: {
      type: 'object'
      properties: Record<string, any>
      required: string[]
    }
  }
  execute(input: any): Promise<string | object>
  isConcurrencySafe?: () => boolean   // CC buildTool default: false (conservative)
  isReadOnly?: () => boolean          // CC buildTool default: false (assume write)
}

// === CC src/Tool.ts: buildTool() — safe defaults (Section 13.4.1) ===
export function buildAgentTool(
  def: Omit<AgentTool, 'isConcurrencySafe' | 'isReadOnly'> & {
    isConcurrencySafe?: () => boolean
    isReadOnly?: () => boolean
  }
): AgentTool {
  return {
    isConcurrencySafe: () => false,  // CC default: false (conservative, assume unsafe)
    isReadOnly: () => false,         // CC default: false (assume write operation)
    ...def,
  }
}

// === CC src/tools.ts: Tool registry ===
const toolRegistry: Record<string, AgentTool> = {}

export function registerTool(tool: AgentTool) {
  toolRegistry[tool.definition.name] = tool
}

// === CC src/tools.ts: filterToolsByDenyRules() (Section 13.4.2) ===
export function getAgentTools(): AgentTool[] {
  // Deny rules from env (SP_DISABLED_TOOLS=tool1,tool2)
  const disabled = new Set(
    (process.env.SP_DISABLED_TOOLS || '').split(',').filter(Boolean)
  )
  // CC pattern: filter by deny rules then by isEnabled
  return Object.values(toolRegistry).filter(t => !disabled.has(t.definition.name))
}

export async function executeTool(name: string, input: any): Promise<string | object> {
  const tool = toolRegistry[name]
  if (!tool) throw new Error(`Unknown tool: "${name}". Available: ${Object.keys(toolRegistry).join(', ')}`)
  return tool.execute(input)
}

// === CC src/tools.ts: assembleToolPool() — stable ordering for prompt cache (Section 13.4.3) ===
// Built-in tools sorted first (stable order), then MCP tools appended (Phase 4)
// CC comment: "flat sort would cause MCP tools inserted between built-ins, breaking cache"
export function assembleToolPoolWithMCP(mcpTools: AgentTool[] = []): AgentTool[] {
  const builtIn = getAgentTools()
  const byName = (a: AgentTool, b: AgentTool) => a.definition.name.localeCompare(b.definition.name)
  // CC pattern: built-in first (stable), MCP appended, deduplicate by name (built-in wins)
  return [
    ...new Map(
      [...builtIn.sort(byName), ...mcpTools.sort(byName)]
        .map(t => [t.definition.name, t])
    ).values()
  ]
}

// === ESM imports — ordered by usage frequency for prompt cache stability ===

// Financial domain tools (16)
import { inventoryTool } from './inventoryTool.js'
import { signalsTool } from './signalsTool.js'
import { regimeTool } from './regimeTool.js'
import { dailyReportTool, dailyReportTextTool } from './dailyReportTool.js'
import { wfSummaryTool } from './walkForwardSummaryTool.js'
import { equityCurveTool } from './equityCurveTool.js'
import { oosPairSummaryTool } from './oosPairSummaryTool.js'
import { dsrLogTool } from './dsrLogTool.js'
import { pairUniverseTool } from './pairUniverseTool.js'
import { monitorHistoryTool } from './monitorHistoryTool.js'
import { diagnosticTool } from './diagnosticTool.js'
import { wfStructureTool } from './wfStructureTool.js'
import { strategyPerformanceTool } from './strategyPerformanceTool.js'
import { pnlReportsTool } from './pnlReportTool.js'
import { inventoryHistoryTool } from './inventoryHistoryTool.js'

// General-purpose data tools
import { mongodbTool } from './mongodbTool.js'
import { calculatorTool } from './calculatorTool.js'
import { statisticsTool } from './statisticsTool.js'
import { readFileTool } from './fileTool.js'
import { parseXlsxTool } from './csvTool.js'
import { listFilesTool } from './listFilesTool.js'
import { queryJsonTool } from './jsonTool.js'
import { httpRequestTool } from './httpTool.js'
import { dateTimeTool } from './dateTimeTool.js'
import { compareStrategiesTool } from './compareStrategiesTool.js'
import { pairStatsTool } from './pairStatsTool.js'

// Phase 2 tools — web search, content search, notebook, python, task mgmt, config, sleep
import { webSearchTool } from './webSearchTool.js'
import { searchContentTool } from './searchContentTool.js'
import { readNotebookTool } from './notebookTool.js'
import { runPythonTool } from './runPythonTool.js'
import { taskOutputTool } from './taskOutputTool.js'
import { stopTaskTool } from './stopTaskTool.js'
import { sleepTool } from './sleepTool.js'
import { configTool } from './configTool.js'

export function registerAllTools() {
  const allTools: AgentTool[] = [
    // Financial (16 including text variant)
    inventoryTool, signalsTool, regimeTool, dailyReportTool,
    wfSummaryTool, equityCurveTool, oosPairSummaryTool, dsrLogTool,
    pairUniverseTool, dailyReportTextTool, monitorHistoryTool, diagnosticTool,
    wfStructureTool, strategyPerformanceTool, pnlReportsTool, inventoryHistoryTool,
    // General-purpose data (11)
    mongodbTool, calculatorTool, statisticsTool, readFileTool,
    parseXlsxTool, listFilesTool, queryJsonTool, httpRequestTool,
    dateTimeTool, compareStrategiesTool, pairStatsTool,
    // Phase 2 tools (8)
    webSearchTool, searchContentTool, readNotebookTool, runPythonTool,
    taskOutputTool, stopTaskTool, sleepTool, configTool,
    // Note: ask_user, manage_tasks, send_message are stateful factories
    // created per-request in agent.ts (not registered here)
  ]
  allTools.forEach(registerTool)
  console.log(`Someo Agent: ${allTools.length} tools registered`)
}
