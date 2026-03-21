import fs from 'fs';
import { parse } from 'csv-parse/sync';

/**
 * Parse a CSV file and return an array of objects (first row = headers).
 */
export async function parseCsvFile(filePath: string): Promise<any[]> {
  const content = await fs.promises.readFile(filePath, 'utf-8');
  const records = parse(content, {
    columns: true,
    skip_empty_lines: true,
    trim: true,
    cast: true,
  });
  return records;
}
