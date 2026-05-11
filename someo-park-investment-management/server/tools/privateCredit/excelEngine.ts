// server/tools/privateCredit/excelEngine.ts
// Excel read/write engine for Private Credit template using exceljs
import * as path from 'path'
import * as fs from 'fs'
import { getBackendPath } from '../../config.js'

const EXCEL_PATH = getBackendPath('public/Private_Credit_Templates_IGPC_UBP_VCOP base template.xlsx')

export interface ExcelModelData {
  sheetName: string
  inputs: Record<string, any>
  formulas: Record<string, string>
  outputs: Record<string, any>
  allCells: Array<{ row: number; col: number; label: string; value: any; formula?: string }>
}

export interface ModelSummary {
  sheetName: string
  rows: number
  cols: number
}

/** List all sheets in the Excel template */
export async function listSheets(): Promise<ModelSummary[]> {
  const ExcelJS = await import('exceljs')
  const wb = new ExcelJS.Workbook()
  await wb.xlsx.readFile(EXCEL_PATH)
  const sheets: ModelSummary[] = []
  wb.eachSheet((ws, _id) => {
    sheets.push({ sheetName: ws.name, rows: ws.rowCount, cols: ws.columnCount })
  })
  return sheets
}

/** Read a specific sheet, returning all cells with values and formulas */
export async function readExcelModel(sheetName: string): Promise<ExcelModelData> {
  const ExcelJS = await import('exceljs')
  const wb = new ExcelJS.Workbook()
  await wb.xlsx.readFile(EXCEL_PATH)
  const ws = wb.getWorksheet(sheetName)
  if (!ws) throw new Error(`Sheet "${sheetName}" not found`)

  const allCells: ExcelModelData['allCells'] = []
  const inputs: Record<string, any> = {}
  const formulas: Record<string, string> = {}
  const outputs: Record<string, any> = {}

  ws.eachRow({ includeEmpty: false }, (row, rowNum) => {
    const label = row.getCell(1).value?.toString() || ''
    row.eachCell({ includeEmpty: false }, (cell, colNum) => {
      const entry: any = {
        row: rowNum,
        col: colNum,
        label: colNum === 1 ? String(cell.value || '') : label,
        value: cell.value,
      }
      if (cell.formula) entry.formula = cell.formula

      allCells.push(entry)

      // Heuristic: column A is labels, column B+ is data
      if (colNum === 2 && label) {
        if (cell.formula) {
          formulas[label] = cell.formula
          outputs[label] = cell.value
        } else {
          inputs[label] = cell.value
        }
      }
    })
  })

  return { sheetName, inputs, formulas, outputs, allCells }
}

/** Read a specific cell range from a sheet */
export async function readRange(sheetName: string, range: string): Promise<any[][]> {
  const ExcelJS = await import('exceljs')
  const wb = new ExcelJS.Workbook()
  await wb.xlsx.readFile(EXCEL_PATH)
  const ws = wb.getWorksheet(sheetName)
  if (!ws) throw new Error(`Sheet "${sheetName}" not found`)

  const [start, end] = range.split(':')
  const startCell = ws.getCell(start)
  const endCell = ws.getCell(end)
  const rows: any[][] = []

  for (let r = Number(startCell.row); r <= Number(endCell.row); r++) {
    const row: any[] = []
    for (let c = Number(startCell.col); c <= Number(endCell.col); c++) {
      row.push(ws.getCell(r, c).value)
    }
    rows.push(row)
  }
  return rows
}

/** Write modified values to a copy of the workbook */
export async function writeExcelResult(
  modifications: Record<string, any>,
  outputPath?: string
): Promise<string> {
  const ExcelJS = await import('exceljs')
  const wb = new ExcelJS.Workbook()
  await wb.xlsx.readFile(EXCEL_PATH)

  for (const [key, value] of Object.entries(modifications)) {
    // key format: "SheetName:CellAddress" e.g. "VCOP_Secondary+NAV:B5"
    const [sheetName, cellAddr] = key.split(':')
    const ws = wb.getWorksheet(sheetName)
    if (ws && cellAddr) {
      ws.getCell(cellAddr).value = value
    }
  }

  const outPath = outputPath || EXCEL_PATH.replace('.xlsx', '_modified.xlsx')
  await wb.xlsx.writeFile(outPath)
  return outPath
}

/** Check if the Excel template exists */
export function templateExists(): boolean {
  return fs.existsSync(EXCEL_PATH)
}
