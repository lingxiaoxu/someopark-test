// server/utils/taskManager.ts
// Reference: CC src/Task.ts — complete port of TaskType, TaskStatus, generateTaskId,
// createTaskStateBase, TaskHandle, isTerminalTaskStatus

import { randomBytes } from 'crypto'

// === CC Task.ts lines 6-13: TaskType enum ===
export type TaskType =
  | 'python'        // E2B sandbox execution
  | 'agent'         // sub-agent execution
  | 'shell'         // local shell command
  | 'workflow'      // multi-step workflow

// === CC Task.ts lines 15-20: TaskStatus ===
export type TaskStatus = 'pending' | 'running' | 'completed' | 'failed' | 'killed'

// === CC Task.ts lines 27-29: isTerminalTaskStatus ===
export function isTerminalTaskStatus(status: TaskStatus): boolean {
  return status === 'completed' || status === 'failed' || status === 'killed'
}

// === CC Task.ts lines 31-34: TaskHandle with cleanup ===
export type TaskHandle = {
  taskId: string
  cleanup?: () => void
}

// === CC Task.ts lines 79-92: type-aware ID prefixes ===
const TASK_ID_PREFIXES: Record<TaskType, string> = {
  python: 'p',
  agent: 'a',
  shell: 's',
  workflow: 'w',
}

// === CC Task.ts lines 94-106: generateTaskId with type-aware prefix ===
const TASK_ID_ALPHABET = '0123456789abcdefghijklmnopqrstuvwxyz'

export function generateTaskId(type: TaskType = 'python'): string {
  const prefix = TASK_ID_PREFIXES[type] || 'p'
  const bytes = randomBytes(8)
  let id = prefix
  for (let i = 0; i < 8; i++) {
    id += TASK_ID_ALPHABET[bytes[i]! % TASK_ID_ALPHABET.length]
  }
  return id
}

// === CC Task.ts lines 45-57: TaskStateBase — full field set ===
export interface SPTaskState {
  id: string
  type: TaskType
  status: TaskStatus
  description: string
  toolUseId?: string         // CC: links task to tool_use block
  startTime: number
  endTime?: number
  totalPausedMs?: number     // CC: tracks cumulative pause time
  stdout: string
  stderr: string
  exitCode?: number | null
  notified: boolean          // CC: lifecycle notification flag
  sandbox?: any              // E2B sandbox reference for stop_task
}

// === CC Task.ts lines 108-125: createTaskStateBase ===
// Process-level store (tasks persist across requests within process)
const taskStore = new Map<string, SPTaskState>()

export function createTask(
  type: TaskType,
  description: string,
  toolUseId?: string
): SPTaskState {
  const id = generateTaskId(type)
  const state: SPTaskState = {
    id,
    type,
    status: 'pending',
    description,
    toolUseId,
    startTime: Date.now(),
    stdout: '',
    stderr: '',
    notified: false,
  }
  taskStore.set(id, state)
  return state
}

export function getTask(id: string): SPTaskState | undefined {
  return taskStore.get(id)
}

export function getAllTasks(): SPTaskState[] {
  return Array.from(taskStore.values())
}

export function updateTaskStatus(
  id: string,
  status: TaskStatus,
  output?: { stdout?: string; stderr?: string; exitCode?: number }
): void {
  const task = taskStore.get(id)
  if (!task) return
  task.status = status
  if (output?.stdout !== undefined) task.stdout += output.stdout
  if (output?.stderr !== undefined) task.stderr += output.stderr
  if (output?.exitCode !== undefined) task.exitCode = output.exitCode
  if (isTerminalTaskStatus(status)) {
    task.endTime = Date.now()
    // Auto-cleanup after 1 hour (CC uses AppState lifecycle; SP uses timeout)
    setTimeout(() => taskStore.delete(id), 3600_000)
  }
}

// CC pattern: mark task as notified after TaskOutputTool reads it
export function markTaskNotified(id: string): void {
  const task = taskStore.get(id)
  if (task) task.notified = true
}
