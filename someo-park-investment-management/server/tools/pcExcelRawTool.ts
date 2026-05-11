// server/tools/pcExcelRawTool.ts
// Read/write raw data from Private Credit Excel template
import type { AgentTool } from './index.js'
import * as path from 'path'
import * as fs from 'fs'
import { getBackendPath } from '../config.js'

const EXCEL_PATH = getBackendPath('public/Private_Credit_Templates_IGPC_UBP_VCOP base template.xlsx')

export const pcExcelRawTool: AgentTool = {
  definition: {
    name: 'pc_excel_raw',
    description: 'Read raw data from the Private Credit Excel template (v4 audited, 20 sheets). Can list sheets, read specific cells/ranges, or read entire sheets. Also supports writing modified values to a copy for export.',
    input_schema: {
      type: 'object' as const,
      properties: {
        action: { type: 'string', enum: ['list_sheets', 'read_sheet', 'read_range', 'read_cell', 'export_modified'], description: 'Action to perform' },
        sheet: { type: 'string', description: 'Sheet name (required for read_sheet, read_range, read_cell)' },
        range: { type: 'string', description: 'Cell range for read_range, e.g. "A1:F20"' },
        cell: { type: 'string', description: 'Single cell for read_cell, e.g. "B5"' },
        modifications: { type: 'object', description: 'For export_modified: {"sheet:cell": value} pairs, e.g. {"VCOP_Secondary+NAV:B5": 85}' },
        output_path: { type: 'string', description: 'For export_modified: where to save modified workbook' },
      },
      required: ['action']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ action, sheet, range, cell, modifications, output_path }) {
    if (!fs.existsSync(EXCEL_PATH)) {
      return { error: `Excel template not found at ${EXCEL_PATH}` }
    }

    // Dynamic import exceljs
    const ExcelJS = await import('exceljs')
    const wb = new ExcelJS.Workbook()
    await wb.xlsx.readFile(EXCEL_PATH)

    if (action === 'list_sheets') {
      const sheets: any[] = []
      wb.eachSheet((ws, id) => {
        sheets.push({ id, name: ws.name, rows: ws.rowCount, cols: ws.columnCount })
      })
      return { sheets }
    }

    if (!sheet) return { error: 'Sheet name required for this action' }
    const ws = wb.getWorksheet(sheet)
    if (!ws) {
      const available: string[] = []
      wb.eachSheet((w) => available.push(w.name))
      return { error: `Sheet "${sheet}" not found`, available }
    }

    if (action === 'read_cell') {
      if (!cell) return { error: 'Cell address required (e.g. "B5")' }
      const c = ws.getCell(cell)
      return {
        sheet, cell,
        value: c.value,
        formula: c.formula || null,
        type: c.type
      }
    }

    if (action === 'read_range') {
      if (!range) return { error: 'Range required (e.g. "A1:F20")' }
      const [start, end] = range.split(':')
      const startCell = ws.getCell(start)
      const endCell = ws.getCell(end)
      const rows: any[][] = []

      for (let r = Number(startCell.row); r <= Number(endCell.row); r++) {
        const row: any[] = []
        for (let c = Number(startCell.col); c <= Number(endCell.col); c++) {
          const cell = ws.getCell(r, c)
          row.push(cell.value)
        }
        rows.push(row)
      }
      return { sheet, range, data: rows }
    }

    if (action === 'read_sheet') {
      const rows: any[] = []
      ws.eachRow({ includeEmpty: false }, (row, rowNum) => {
        if (rowNum > 100) return // safety limit
        const vals: any[] = []
        row.eachCell({ includeEmpty: true }, (cell) => {
          vals.push(cell.value)
        })
        rows.push({ row: rowNum, values: vals })
      })
      return { sheet, total_rows: ws.rowCount, data: rows }
    }

    if (action === 'export_modified') {
      if (!modifications) return { error: 'Modifications required' }
      // Apply modifications
      for (const [key, value] of Object.entries(modifications)) {
        const [sheetName, cellAddr] = key.split(':')
        const targetSheet = wb.getWorksheet(sheetName)
        if (targetSheet && cellAddr) {
          targetSheet.getCell(cellAddr).value = value as any
        }
      }
      const outPath = output_path || EXCEL_PATH.replace('.xlsx', '_modified.xlsx')
      await wb.xlsx.writeFile(outPath)
      return { message: `Modified workbook saved to ${outPath}`, modifications_applied: Object.keys(modifications).length }
    }

    return { error: `Unknown action: ${action}` }
  }
}
