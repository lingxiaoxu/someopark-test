// server/tools/stopTaskTool.ts
// Reference: CC src/tools/TaskStopTool/TaskStopTool.ts

import { getTask, updateTaskStatus } from '../utils/taskManager.js'
import type { AgentTool } from './index.js'

export const stopTaskTool: AgentTool = {
  definition: {
    name: 'stop_task',
    description: 'Stop/kill a running background task (e.g. from run_python background mode). Closes the E2B sandbox.',
    input_schema: {
      type: 'object',
      properties: {
        task_id: { type: 'string', description: 'Task ID to stop' },
      },
      required: ['task_id']
    }
  },
  async execute({ task_id }) {
    const task = getTask(task_id)
    if (!task) throw new Error(`No task found with ID: ${task_id}`)
    if (task.status !== 'running' && task.status !== 'pending') {
      throw new Error(`Task ${task_id} is not running (status: ${task.status})`)
    }

    // Kill E2B sandbox if exists (CC pattern: shellCommand.kill())
    if (task.sandbox) {
      await task.sandbox.kill().catch(() => {})
      task.sandbox = null
    }

    updateTaskStatus(task_id, 'killed')
    return {
      message: `Successfully stopped task: ${task_id}`,
      task_id,
      description: task.description,
    }
  }
}
