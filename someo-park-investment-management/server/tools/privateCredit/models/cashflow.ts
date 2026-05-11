import { CashflowRow } from './types.js'

/**
 * Build a cashflow table from headers and row data.
 * Each row in `rows` corresponds to a period (0, 1, 2, ...).
 * Headers define the column names for each value in the row.
 */
export function buildCashflowTable(headers: string[], rows: number[][]): CashflowRow[] {
  return rows.map((row, idx) => {
    const values: Record<string, number> = {}
    for (let j = 0; j < headers.length; j++) {
      values[headers[j]] = row[j] ?? 0
    }
    return {
      period: idx,
      label: `Year ${idx}`,
      values,
    }
  })
}

/**
 * Round a number to a given number of decimal places.
 */
export function roundTo(value: number, decimals: number): number {
  const factor = Math.pow(10, decimals)
  return Math.round(value * factor) / factor
}

/**
 * Format a number as a percentage string (e.g., 0.1234 -> "12.34%").
 */
export function fmtPct(value: number, decimals = 2): string {
  if (isNaN(value)) return 'N/A'
  return `${(value * 100).toFixed(decimals)}%`
}

/**
 * Format a number as MOIC (e.g., 1.5 -> "1.50x").
 */
export function fmtMOIC(value: number, decimals = 2): string {
  if (isNaN(value)) return 'N/A'
  return `${value.toFixed(decimals)}x`
}
