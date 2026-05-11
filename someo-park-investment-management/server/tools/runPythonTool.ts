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

    // Common packages + CJK font for Chinese chart labels
    const PREINSTALL = [
      'pip install -q yfinance pandas numpy requests seaborn plotly kaleido scipy 2>/dev/null',
      'apt-get update -qq && apt-get install -y -qq fonts-noto-cjk 2>/dev/null || true',
      // Rebuild matplotlib font cache so it picks up the new font
      'python -c "import matplotlib; matplotlib.font_manager._load_fontmanager(try_read_cache=False)" 2>/dev/null || true',
    ].join(' && ')

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
        // Extract base64 images from E2B results (matplotlib plots, etc.)
        const images: string[] = []
        if (result.results) {
          for (const r of result.results) {
            if (r.png) images.push(r.png)
          }
        }
        updateTaskStatus(task.id, 'completed', { stdout, stderr })
        await sandbox.kill().catch(() => {})
        return {
          task_id: task.id,
          status: 'completed',
          stdout: images.length > 0
            ? stdout + '\n' + images.map((img, i) => `![Chart ${i+1}](data:image/png;base64,${img})`).join('\n')
            : stdout,
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
          let stdout = (result.logs?.stdout ?? []).join('\n')
          const stderr = (result.logs?.stderr ?? []).join('\n')
          // Extract base64 images from E2B results
          if (result.results) {
            for (const r of result.results) {
              if (r.png) stdout += `\n![Chart](data:image/png;base64,${r.png})`
            }
          }
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
