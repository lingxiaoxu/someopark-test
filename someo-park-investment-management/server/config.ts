import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BACKEND_ROOT = path.resolve(__dirname, '..', process.env.BACKEND_ROOT || '..');

export const API_PORT = parseInt(process.env.API_PORT || '3001', 10);
export const MONGO_URI = process.env.MONGO_URI || '';

/**
 * Resolve a relative path under the backend root, with path traversal protection.
 */
export function getBackendPath(relativePath: string): string {
  const resolved = path.resolve(BACKEND_ROOT, relativePath);
  if (!resolved.startsWith(BACKEND_ROOT)) {
    throw new Error('Path traversal attempt detected');
  }
  return resolved;
}
