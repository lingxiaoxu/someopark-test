export interface InputField {
  name: string
  label: string
  type: 'number' | 'percentage' | 'boolean' | 'array' | 'string'
  default: number | number[] | boolean | string
  description: string
}

export interface OutputField {
  name: string
  label: string
  type: 'number' | 'percentage' | 'string'
}

export interface CashflowRow {
  period: number
  label: string
  values: Record<string, number>
}

export interface ModelResult {
  model: string
  inputs: Record<string, number | number[] | boolean | string>
  outputs: Record<string, number | string>
  cashflows?: CashflowRow[]
  description?: string
}

export interface SensitivityResult {
  model: string
  vary_param: string
  results: Array<{ param_value: number; outputs: Record<string, number> }>
}

export interface ModelDefinition {
  name: string
  sheetName: string
  description: string
  fund: 'VCOP' | 'IGPC' | 'UBP' | 'General'
  inputs: InputField[]
  outputs: OutputField[]
  compute(inputs: Record<string, any>): ModelResult
}
