// server/tools/runPythonTool.ts
// Reference: CC src/tools/BashTool/BashTool.tsx — execute, timeout, background mode
// Uses E2B Code Interpreter SDK v2: Sandbox.create() + sandbox.runCode() + sandbox.kill()

import { createTask, updateTaskStatus, getTask } from '../utils/taskManager.js'
import type { AgentTool } from './index.js'

export const runPythonTool: AgentTool = {
  definition: {
    name: 'run_python',
    description: `Execute Python code in an isolated E2B sandbox. Two modes:
- background=false (default): Wait for result, return stdout (max timeout seconds)
- background=true: Submit async, return task_id; use get_task_output to poll result.
Requires E2B_API_KEY environment variable.`,
    input_schema: {
      type: 'object',
      properties: {
        code: { type: 'string', description: 'Python code to execute' },
        background: { type: 'boolean', description: 'Run in background (default false)' },
        timeout: { type: 'number', description: 'Timeout in seconds (default 30, max 120)' },
      },
      required: ['code']
    }
  },
  async execute({ code, background = false, timeout = 30 }) {
    if (!process.env.E2B_API_KEY) throw new Error('E2B_API_KEY not configured')

    const { Sandbox } = await import('@e2b/code-interpreter')
    const task = createTask('python', `run_python: ${code.slice(0, 60)}...`)
    updateTaskStatus(task.id, 'running')

    const timeoutMs = Math.min(timeout, 120) * 1000

    // Common packages needed by agent code — pre-install once per sandbox
    const PREINSTALL = 'pip install -q yfinance pandas numpy requests 2>/dev/null'

    if (!background) {
      // Synchronous mode
      let sandbox: any
      try {
        sandbox = await Sandbox.create({ timeoutMs })
        // Pre-install deps so first run doesn't fail with "No module named X"
        await sandbox.runCode(`import subprocess; subprocess.run("${PREINSTALL}", shell=True, capture_output=True)`)
        const result = await sandbox.runCode(code)
        const stdout = (result.logs?.stdout ?? []).join('\n')
        const stderr = (result.logs?.stderr ?? []).join('\n')
        updateTaskStatus(task.id, 'completed', { stdout, stderr })
        await sandbox.kill().catch(() => {})
        return {
          task_id: task.id,
          status: 'completed',
          stdout,
          stderr,
          error: result.error?.value || null,
        }
      } catch (err: any) {
        updateTaskStatus(task.id, 'failed', { stderr: err.message })
        if (sandbox) await sandbox.kill().catch(() => {})
        throw err
      }
    } else {
      // Background mode — return task_id immediately
      ;(async () => {
        let sandbox: any
        try {
          sandbox = await Sandbox.create({ timeoutMs: 120_000 })
          const state = getTask(task.id)
          if (state) state.sandbox = sandbox
          await sandbox.runCode(`import subprocess; subprocess.run("${PREINSTALL}", shell=True, capture_output=True)`)
          const result = await sandbox.runCode(code)
          const stdout = (result.logs?.stdout ?? []).join('\n')
          const stderr = (result.logs?.stderr ?? []).join('\n')
          updateTaskStatus(task.id, 'completed', { stdout, stderr })
        } catch (err: any) {
          updateTaskStatus(task.id, 'failed', { stderr: err.message })
        } finally {
          if (sandbox) await sandbox.kill().catch(() => {})
        }
      })().catch(() => {})

      return {
        task_id: task.id,
        status: 'pending',
        message: 'Task submitted. Use get_task_output to check status.',
      }
    }
  }
}
