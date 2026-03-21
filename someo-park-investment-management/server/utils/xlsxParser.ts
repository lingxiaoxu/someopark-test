import ExcelJS from 'exceljs';

// Simple in-memory cache for workbooks (5 min TTL)
const cache = new Map<string, { workbook: ExcelJS.Workbook; expiry: number }>();

async function getCachedWorkbook(filePath: string): Promise<ExcelJS.Workbook> {
  const cached = cache.get(filePath);
  if (cached && cached.expiry > Date.now()) return cached.workbook;

  const workbook = new ExcelJS.Workbook();
  await workbook.xlsx.readFile(filePath);
  cache.set(filePath, { workbook, expiry: Date.now() + 5 * 60 * 1000 });
  return workbook;
}

/**
 * List all sheet names in an Excel file.
 */
export async function listXlsxSheets(filePath: string): Promise<{ name: string; rowCount: number; columnCount: number }[]> {
  const workbook = await getCachedWorkbook(filePath);
  return workbook.worksheets.map(ws => ({
    name: ws.name,
    rowCount: ws.rowCount,
    columnCount: ws.columnCount,
  }));
}

/**
 * Parse a specific sheet from an Excel file into { headers, rows }.
 */
export async function parseXlsxSheet(
  filePath: string,
  sheetName: string
): Promise<{ headers: string[]; rows: any[]; rowCount: number }> {
  const workbook = await getCachedWorkbook(filePath);
  const worksheet = workbook.getWorksheet(sheetName);
  if (!worksheet) {
    throw new Error(`Sheet "${sheetName}" not found`);
  }

  const headers: string[] = [];
  const rows: any[] = [];

  worksheet.eachRow((row, rowNumber) => {
    if (rowNumber === 1) {
      row.eachCell((cell, colNumber) => {
        headers[colNumber - 1] = String(cell.value ?? `col_${colNumber}`);
      });
    } else {
      const obj: Record<string, any> = {};
      row.eachCell((cell, colNumber) => {
        const key = headers[colNumber - 1] || `col_${colNumber}`;
        if (cell.value instanceof Date) {
          obj[key] = cell.value.toISOString().split('T')[0];
        } else if (typeof cell.value === 'object' && cell.value !== null && 'result' in cell.value) {
          // ExcelJS formula result
          obj[key] = (cell.value as any).result;
        } else {
          obj[key] = cell.value;
        }
      });
      rows.push(obj);
    }
  });

  return { headers, rows, rowCount: rows.length };
}
