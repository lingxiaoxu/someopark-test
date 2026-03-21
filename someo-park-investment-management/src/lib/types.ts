import { ExecutionError, Result } from '@e2b/code-interpreter'

type ExecutionResultBase = {
  sbxId: string
}

export type ExecutionResultInterpreter = ExecutionResultBase & {
  template: string
  stdout: string[]
  stderr: string[]
  runtimeError?: ExecutionError
  cellResults: Result[]
}

export type ExecutionResultWeb = ExecutionResultBase & {
  template: string
  url: string
}

export type ExecutionResult = ExecutionResultInterpreter | ExecutionResultWeb

// Someo Park specific types
export interface Conversation {
  id: string
  title: string
  created_at: string
  updated_at: string
  user_id: string
}
