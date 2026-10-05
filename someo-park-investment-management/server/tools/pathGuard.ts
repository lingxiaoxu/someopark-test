// server/tools/pathGuard.ts — ONE path guard for every agent tool that reads
// the filesystem (read_file, list_files, search_content, csv, notebook).
//
// 2026-10-01 security fix: the tools only checked `startsWith(repo root)`, so
// any visitor could ask the agent to read someo-park-investment-management/.env
// (SUPABASE_SECRET_KEY), prediction_market/.env (the owner's Kalshi PROD key id)
// and per-user private data. Now: symlinks are resolved, the root comparison
// requires a path separator, and secret-bearing names/dirs are refused.

import fs from 'fs'
import path from 'path'
import { getBackendPath } from '../config.js'

export const ALLOWED_ROOT = path.resolve(getBackendPath('.'))

const DENY_BASENAME = [
  /^\.env(\..*)?$/i,                       // .env, .env.local, .env.kalshi_users ...
  /\.(pem|key|p12|pfx|crt|cer|der|keystore|jks)$/i,
  /^id_(rsa|dsa|ecdsa|ed25519)/i,
  /secret/i,
  /credential/i,
  /^\.?netrc$/i, /^\.npmrc$/i, /^\.pypirc$/i,
]
// Directory segments that hold private per-user or credential material.
const DENY_SEGMENT = new Set(['prod_users', '.kalshi', '.ssh', '.aws', '.gnupg', '.git'])

export function isDeniedPath(absPath: string): boolean {
  const parts = path.resolve(absPath).split(path.sep)
  if (parts.some(p => DENY_SEGMENT.has(p))) return true
  return DENY_BASENAME.some(re => re.test(parts[parts.length - 1] || ''))
}

function inside(root: string, p: string) {
  return p === root || p.startsWith(root + path.sep)
}

/** realpath of the deepest EXISTING ancestor + the not-yet-existing remainder,
 *  so a symlink inside the repo can never smuggle a path out of it. */
function realish(p: string): string {
  let head = p
  const tail: string[] = []
  for (;;) {
    try { return path.join(fs.realpathSync(head), ...tail.reverse()) } catch { /* climb */ }
    const parent = path.dirname(head)
    if (parent === head) return p
    tail.push(path.basename(head))
    head = parent
  }
}

/** Resolve a user-supplied path; throw unless it is inside the repo and not secret. */
export function resolveAllowed(filePath: string): string {
  const candidate = path.isAbsolute(filePath) ? path.resolve(filePath) : path.resolve(getBackendPath(filePath))
  const real = realish(candidate)
  if (!inside(ALLOWED_ROOT, candidate) || !inside(ALLOWED_ROOT, real)) {
    throw new Error(`Path not allowed. Must be within ${ALLOWED_ROOT}`)
  }
  if (isDeniedPath(candidate) || isDeniedPath(real)) {
    throw new Error('Path not allowed: credential / private data files are not readable')
  }
  return real
}

/** For listings: keep only entries that are inside the repo and not secret. */
export function isListable(absPath: string): boolean {
  try {
    const real = fs.realpathSync(absPath)
    return inside(ALLOWED_ROOT, real) && !isDeniedPath(absPath) && !isDeniedPath(real)
  } catch {
    return false
  }
}

/** ripgrep exclusions mirroring the deny rules (defence in depth; output is also filtered). */
export const RG_DENY_GLOBS = [
  '!**/.env', '!**/.env.*', '!**/*.pem', '!**/*.key', '!**/*.p12', '!**/*.pfx',
  '!**/id_rsa*', '!**/id_ed25519*', '!**/*secret*', '!**/*credential*',
  '!**/prod_users/**', '!**/.kalshi/**', '!**/.ssh/**', '!**/.git/**',
]
