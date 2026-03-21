import fs from 'fs';
import path from 'path';
import { glob } from 'glob';

/**
 * List files matching a glob pattern in a directory, sorted by filename descending
 * (timestamp in filename means newest first).
 */
export async function listFiles(dir: string, pattern: string): Promise<string[]> {
  const fullPattern = path.join(dir, pattern).replace(/\\/g, '/');
  const files = await glob(fullPattern);
  return files.sort((a, b) => b.localeCompare(a));
}

/**
 * Find the latest file matching a pattern (by filename sort, newest timestamp first).
 */
export async function findLatestFile(dir: string, pattern: string): Promise<string> {
  const files = await listFiles(dir, pattern);
  if (files.length === 0) {
    throw new Error(`No files matching ${pattern} in ${dir}`);
  }
  return files[0];
}

/**
 * Read and parse a JSON file.
 */
export async function readJsonFile(filePath: string): Promise<any> {
  const content = await fs.promises.readFile(filePath, 'utf-8');
  return JSON.parse(content);
}

/**
 * Read a text file.
 */
export async function readTextFile(filePath: string): Promise<string> {
  return fs.promises.readFile(filePath, 'utf-8');
}

/**
 * Extract timestamp (YYYYMMDD_HHMMSS) from a filename.
 */
export function extractTimestamp(filename: string): string {
  const match = path.basename(filename).match(/(\d{8}_\d{6})/);
  return match ? match[1] : '';
}

/**
 * Get file stats (size, mtime).
 */
export async function getFileStats(filePath: string) {
  const stat = await fs.promises.stat(filePath);
  return { size: stat.size, mtime: stat.mtime.toISOString() };
}

/**
 * Deduplicate files that share the same "base name" (filename minus the trailing
 * _YYYYMMDD_HHMMSS timestamp and extension).  For each group with the same base
 * name, only the one with the latest (lexicographically largest) timestamp is kept.
 *
 * Example:
 *   monitor_mtfs_XOM_V_20260320_162515.xlsx   → base = monitor_mtfs_XOM_V
 *   monitor_mtfs_XOM_V_20260321_123709.xlsx   → base = monitor_mtfs_XOM_V  (kept)
 *
 * Input must already be sorted descending by filename (as listFiles returns).
 */
export function deduplicateLatest(files: string[]): string[] {
  const seen = new Map<string, string>();
  for (const f of files) {
    const basename = path.basename(f);
    const withoutExt = basename.replace(/\.[^.]+$/, '');
    const baseName = withoutExt.replace(/_\d{8}_\d{6}$/, '');
    if (!seen.has(baseName)) {
      seen.set(baseName, f);
    }
  }
  return Array.from(seen.values());
}
