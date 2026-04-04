// server/tools/taskOutputTool.ts
// Reference: CC src/tools/TaskOutputTool/TaskOutputTool.tsx
// Poll-based task completion with blocking wait

import { getTask, isTerminalTaskStatus } from '../utils/taskManager.js'
import type { AgentTool } from './index.js'

export const taskOutputTool: AgentTool = {
  definition: {
    name: 'get_task_output',
    description: 'Get output of a background task (from run_python). Supports blocking wait with timeout.',
    input_schema: {
      type: 'object',
      properties: {
        task_id: { type: 'string', description: 'Task ID from run_python' },
        block: { type: 'boolean', description: 'Wait for completion (default true)' },
        timeout: { type: 'number', description: 'Wait timeout in ms (default 30000, max 600000)' },
      },
      required: ['task_id']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ task_id, block = true, timeout = 30000 }) {
    const task = getTask(task_id)
    if (!task) throw new Error(`No task found with ID: ${task_id}`)

    // Non-blocking path (CC pattern)
    if (!block) {
      return {
        retrieval_status: isTerminalTaskStatus(task.status) ? 'success' : 'not_ready',
        task_id: task.id,
        status: task.status,
        description: task.description,
        stdout: task.stdout,
        stderr: task.stderr,
        exit_code: task.exitCode ?? null,
        duration_ms: (task.endTime || Date.now()) - task.startTime,
      }
    }

    // Blocking path — poll every 100ms (CC pattern: waitForTaskCompletion)
    const clampedTimeout = Math.min(timeout, 600000)
    const deadline = Date.now() + clampedTimeout

    while (Date.now() < deadline) {
      const current = getTask(task_id)
      if (!current) throw new Error(`Task ${task_id} disappeared`)
      if (isTerminalTaskStatus(current.status)) {
        return {
          retrieval_status: 'success',
          task_id: current.id,
          status: current.status,
          description: current.description,
          stdout: current.stdout,
          stderr: current.stderr,
          exit_code: current.exitCode ?? null,
          duration_ms: (current.endTime || Date.now()) - current.startTime,
        }
      }
      await new Promise(r => setTimeout(r, 100))
    }

    // Timeout
    const final = getTask(task_id)
    return {
      retrieval_status: 'timeout',
      task_id: task_id,
      status: final?.status || 'unknown',
      description: final?.description || '',
      stdout: final?.stdout || '',
      stderr: final?.stderr || '',
      duration_ms: final ? (Date.now() - final.startTime) : 0,
    }
  }
}
