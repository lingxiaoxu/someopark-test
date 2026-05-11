// server/tools/privateCredit/pythonBridge.ts
// Python bridge for private credit portfolio tools
// Executes Python scripts in the portfolio_of_private_credit_deals module
// using conda someopark_run env with FRED_API_KEY from project root .env

import { execSync } from 'child_process'
import * as path from 'path'
import * as fs from 'fs'
import { getBackendPath } from '../../config.js'

// Resolve via getBackendPath (ESM-safe, no __dirname)
const PYTHON_MODULE_PATH = getBackendPath('portfolio_of_private_credit_deals')

/**
 * Load a single key from the project root .env file
 */
function loadEnvKey(key: string): string {
  // First check process.env (set by `source .env` before server start)
  if (process.env[key]) return process.env[key]!
  try {
    const envPath = getBackendPath('.env')
    const content = fs.readFileSync(envPath, 'utf8')
    const match = content.match(new RegExp(`^${key}=(.+)$`, 'm'))
    return match ? match[1].trim() : ''
  } catch {
    return ''
  }
}

const FRED_API_KEY = loadEnvKey('FRED_API_KEY')

export interface PythonResult {
  success: boolean
  [key: string]: any
}

/**
 * Run a Python script in the private credit module directory.
 *
 * - Writes script to a temp file (avoids shell quoting issues)
 * - Injects FRED_API_KEY into os.environ
 * - Uses `conda run -n someopark_run` (verified env with all deps)
 * - CWD = portfolio_of_private_credit_deals/ (required for relative imports)
 * - stdout may contain non-JSON info lines (ForwardRateLookup prints emoji lines);
 *   we extract the last line starting with '{' as the JSON result
 * - Cleans up temp file on success or failure
 */
export function runPython(script: string, timeoutMs = 60000): PythonResult {
  const tmpFile = path.join(PYTHON_MODULE_PATH, '.tmp_agent_script.py')
  const fullScript = [
    'import os, sys, warnings',
    'warnings.filterwarnings("ignore")',
    `os.environ['FRED_API_KEY'] = '${FRED_API_KEY}'`,
    script,
  ].join('\n')

  try {
    fs.writeFileSync(tmpFile, fullScript, 'utf8')
    const stdout = execSync(
      `conda run -n someopark_run --no-capture-output python "${tmpFile}"`,
      { encoding: 'utf8', timeout: timeoutMs, cwd: PYTHON_MODULE_PATH }
    )
    // Clean up temp file
    try { fs.unlinkSync(tmpFile) } catch {}
    // Extract last JSON line (Python modules may print info/emoji before JSON)
    const lines = stdout.trim().split('\n')
    const jsonLine = [...lines].reverse().find(l => l.trimStart().startsWith('{'))
    if (!jsonLine) {
      return { success: false, error: 'No JSON output from Python', raw_stdout: stdout.slice(-500) }
    }
    return JSON.parse(jsonLine)
  } catch (err: any) {
    // Clean up temp file on error
    try { fs.unlinkSync(tmpFile) } catch {}
    return {
      success: false,
      error: (err.message || 'Unknown error').substring(0, 500),
    }
  }
}

/**
 * Get the resolved path to the Python module directory.
 * Useful for tools that need to reference data files.
 */
export function getPythonModulePath(): string {
  return PYTHON_MODULE_PATH
}
