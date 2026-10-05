// server/utils/kalshiUserKeys.ts
// Per-user Kalshi PROD credentials (2026-10-01). Pure validation + storage.
//
// Storage lives OUTSIDE the (public) repo, readable only by this machine user:
//   ~/.kalshi/prod_<userId>.pem   private key, mode 0600
//   ~/.kalshi/users.env           KALSHI_PROD_API_KEY_ID_<userId>=...
//                                 KALSHI_PROD_PRIVATE_KEY_PATH_<userId>=...
//                                 written ONLY after a live signed check passes
//   ~/.kalshi/users.json          registry: status / email / validated_at
//   ~/.kalshi/pending/<userId>.json  key id awaiting verification
//   ~/.kalshi/disabled/<userId>   written by the order router on 401/403
//   ~/.kalshi/ledgers/<userId>.json  private ledger, written by the exporter
//   ~/.kalshi/audit.jsonl         append-only event log (no key material)
//   ~/.kalshi/trading/requests/<userId>.json  the user's live-trading application (this server)
//   ~/.kalshi/trading/approved/<userId>.json  the OWNER's approval + ratio (written by hand, never here)
//   ~/.kalshi/trading/stopped/<userId>.json   stop marker (user here, or the owner by hand)
// <userId> is ALWAYS Supabase auth.users.id, taken from the verified login
// and cross-checked against auth.users (id + email) before activation; the
// same id and email are recorded in every one of these places.
// The owner's own KALSHI_PROD_* variables live in other files and use the
// un-suffixed names, so a user entry can never shadow or overwrite them.

import { execFile } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const OWNER_EMAIL = 'lxu912@gmail.com'
export const KEY_DIR = process.env.KALSHI_USER_KEY_DIR || path.join(os.homedir(), '.kalshi')
export const PROD_BASE = 'https://external-api.kalshi.com'
export const API_ROOT = '/trade-api/v2'

// Same shape as the owner's KALSHI_PROD_API_KEY_ID (36-char lowercase UUID).
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
export const PEM_MIN_BYTES = 80     // an Ed25519 PKCS#8 PEM (Kalshi's default since 2026-10-01) is ~119 bytes
export const PEM_MAX_BYTES = 4096   // RSA-4096 PKCS#8 PEM is ~3.3 KB
// Kalshi's web app downloads the private key as `<key name>.txt` (PKCS#8 PEM text inside); older keys
// came as .pem. Any of these names is accepted; the CONTENT is what gets validated.
const KEY_FILE_RE = /^[^\\/\0]{1,128}\.(pem|txt|key)$/i
const PEM_RE = /^-----BEGIN (RSA )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]+-----END (RSA )?PRIVATE KEY-----\r?\n?$/

export type Check = { ok: true } | { ok: false; error: string }

export function isUserId(id: unknown): id is string {
  // Supabase auth.users.id. Also the ONLY thing that reaches a file name, so
  // the UUID shape doubles as the path-traversal guard.
  return typeof id === 'string' && UUID_RE.test(id)
}

export function checkKeyId(raw: unknown): Check {
  if (typeof raw !== 'string' || raw.length === 0) return { ok: false, error: 'API Key 不能为空' }
  if (raw !== raw.trim()) return { ok: false, error: 'API Key 前后不能有空格或换行' }
  if (!UUID_RE.test(raw)) return { ok: false, error: 'API Key 格式不正确：应为 36 位小写 UUID（xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx）' }
  return { ok: true }
}

export function checkPem(filename: unknown, content: unknown): Check {
  if (typeof filename !== 'string' || typeof content !== 'string') return { ok: false, error: '只能上传一个私钥文件（Kalshi 下载的 .txt 或 .pem）' }
  if (filename !== path.basename(filename) || !KEY_FILE_RE.test(filename)) {
    return { ok: false, error: '文件类型错误：只接受 Kalshi 下载的私钥文件（.txt 或 .pem）' }
  }
  const bytes = Buffer.byteLength(content, 'utf8')
  if (bytes < PEM_MIN_BYTES || bytes > PEM_MAX_BYTES) {
    return { ok: false, error: `文件大小异常（${bytes} 字节）：私钥文件应在 ${PEM_MIN_BYTES}–${PEM_MAX_BYTES} 字节之间` }
  }
  if (!PEM_RE.test(content)) return { ok: false, error: '文件内容不是单个 PEM 私钥（应以 -----BEGIN PRIVATE KEY----- 开头）' }
  let key: crypto.KeyObject
  try {
    key = crypto.createPrivateKey(content)
  } catch {
    return { ok: false, error: '私钥无法解析：文件损坏或被加密' }
  }
  // Kalshi issues Ed25519 keys by default since 2026-10-01 (changelog 2026-09-24) and still supports RSA.
  if (key.asymmetricKeyType === 'rsa') {
    const bits = key.asymmetricKeyDetails?.modulusLength ?? 0
    if (bits < 2048 || bits > 4096) return { ok: false, error: `RSA 位数异常（${bits}）` }
    return { ok: true }
  }
  if (key.asymmetricKeyType === 'ed25519') return { ok: true }
  return { ok: false, error: `Kalshi 只接受 RSA 或 Ed25519 私钥（这个文件是 ${key.asymmetricKeyType ?? '未知类型'}）` }
}

/** 'rsa' | 'ed25519' for a validated PEM (null if unparsable). */
export function pemKeyType(content: string): 'rsa' | 'ed25519' | null {
  try {
    const t = crypto.createPrivateKey(content).asymmetricKeyType
    return t === 'rsa' || t === 'ed25519' ? t : null
  } catch { return null }
}

// ── storage ────────────────────────────────────────────────────────────────
function ensureDirs() {
  for (const d of [KEY_DIR, path.join(KEY_DIR, 'pending'), path.join(KEY_DIR, 'disabled'),
    path.join(KEY_DIR, 'trading'), path.join(KEY_DIR, 'trading', 'requests'), path.join(KEY_DIR, 'trading', 'stopped')]) {
    fs.mkdirSync(d, { recursive: true, mode: 0o700 })
    fs.chmodSync(d, 0o700)
  }
}

function writeAtomic(file: string, data: string) {
  const tmp = `${file}.${process.pid}.${Date.now()}.tmp`
  fs.writeFileSync(tmp, data, { mode: 0o600 })
  fs.renameSync(tmp, file)
  fs.chmodSync(file, 0o600)
}

export const pemPath = (uid: string) => path.join(KEY_DIR, `prod_${uid}.pem`)
export const pendingPemPath = (uid: string) => path.join(KEY_DIR, 'pending', `${uid}.pem`)
export const LEDGER_DIR = process.env.KALSHI_USER_LEDGER_DIR || path.join(KEY_DIR, 'ledgers')
const pendingPath = (uid: string) => path.join(KEY_DIR, 'pending', `${uid}.json`)
const disabledPath = (uid: string) => path.join(KEY_DIR, 'disabled', uid)
const registryPath = () => path.join(KEY_DIR, 'users.json')
const envPath = () => path.join(KEY_DIR, 'users.env')

export type RegistryEntry = {
  user_id: string                 // == the registry key == Supabase auth.users.id
  email: string | null            // as recorded in Supabase at the last check
  email_confirmed: boolean
  provider: string | null
  status: 'active'
  validated_at: string
  first_validated_at: string
  supabase_checked_at: string
  key_id_masked: string
  account_key_hashes: string[]    // sha256 of every key id on this Kalshi account
}

export type Identity = { id: string; email: string | null; email_confirmed?: boolean; provider?: string | null }

/**
 * The logged-in session and a fresh auth.users read must name the SAME
 * account (id and email) before anything is activated under that id.
 */
export function checkIdentity(session: Identity, sb: Identity | null): { ok: true } | { ok: false; error: string; code: number } {
  if (!sb) return { ok: false, code: 503, error: '暂时无法向账户系统确认你的身份，请稍后再试' }
  if (sb.id !== session.id || !isUserId(sb.id)) return { ok: false, code: 400, error: '账户 ID 与账户系统记录不一致，请退出后重新登录' }
  const a = (session.email || '').toLowerCase(), b = (sb.email || '').toLowerCase()
  if (!a || a !== b) return { ok: false, code: 400, error: '账户邮箱与账户系统记录不一致，请退出后重新登录' }
  if (!sb.email_confirmed) return { ok: false, code: 400, error: '请先验证你的邮箱，再连接 Kalshi 账户' }
  return { ok: true }
}

/** Keep the registry email equal to auth.users (it can change after /verify). */
export function syncEmail(uid: string, ident: Identity): { from: string | null; to: string | null } | null {
  const reg = readRegistry()
  const rec = reg[uid]
  if (!rec || !ident.email || ident.id !== uid) return null
  if ((rec.email || '').toLowerCase() === ident.email.toLowerCase()
      && rec.email_confirmed === !!ident.email_confirmed) return null
  const from = rec.email
  reg[uid] = { ...rec, email: ident.email, email_confirmed: !!ident.email_confirmed,
    supabase_checked_at: new Date().toISOString() }
  writeAtomic(registryPath(), JSON.stringify(reg, null, 2))
  return { from, to: ident.email }
}

/** Append-only event log. Never receives key ids in full or key material. */
export function audit(uid: string, email: string | null, event: string, detail: Record<string, unknown> = {}) {
  try {
    ensureDirs()
    const file = path.join(KEY_DIR, 'audit.jsonl')
    const fd = fs.openSync(file, 'a', 0o600)
    try {
      fs.writeSync(fd, JSON.stringify({ ts: new Date().toISOString(), user_id: uid, email, event, ...detail }) + '\n')
    } finally {
      fs.closeSync(fd)
    }
    fs.chmodSync(file, 0o600)
  } catch (e) {
    console.warn('[kalshi-keys] audit write failed:', (e as Error)?.message)
  }
}

export function readRegistry(): Record<string, RegistryEntry> {
  try { return JSON.parse(fs.readFileSync(registryPath(), 'utf8')) } catch { return {} }
}

export function maskKeyId(k: string) { return `${k.slice(0, 8)}…${k.slice(-4)}` }

export function savePendingKeyId(uid: string, keyId: string) {
  ensureDirs()
  writeAtomic(pendingPath(uid), JSON.stringify({ key_id: keyId, saved_at: new Date().toISOString() }))
}

export function readPendingKeyId(uid: string): string | null {
  try { return JSON.parse(fs.readFileSync(pendingPath(uid), 'utf8')).key_id ?? null } catch { return null }
}

/**
 * First upload -> ~/.kalshi/prod_<uid>.pem (the owner's spec). While a key is
 * ACTIVE (possibly mirroring live orders) a new upload goes to pending/ and
 * replaces the live file only after it passes the live check, so an unverified
 * file can never swap out the key the order router is signing with.
 */
export function savePem(uid: string, content: string) {
  ensureDirs()
  writeAtomic(activeKeyId(uid) ? pendingPemPath(uid) : pemPath(uid), content)
}

export function hasPem(uid: string) { return fs.existsSync(pemPath(uid)) }
export function hasPendingPem(uid: string) { return fs.existsSync(pendingPemPath(uid)) }
/** The pem a /verify should test: a pending replacement if any, else the stored one. */
export function pemForVerify(uid: string): string | null {
  for (const p of [pendingPemPath(uid), pemPath(uid)]) if (fs.existsSync(p)) return p
  return null
}
export function isDisabled(uid: string) { return fs.existsSync(disabledPath(uid)) }

function readEnvLines(): string[] {
  try { return fs.readFileSync(envPath(), 'utf8').split('\n').filter(l => l.trim() !== '') } catch { return [] }
}

/** Activate: write the suffixed env pair + registry, clear pending/disabled. */
export const keyHash = (keyId: string) => crypto.createHash('sha256').update(keyId).digest('hex')

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..')

/** The owner's own Kalshi PROD key ids (never shown, only compared). */
export function ownerKeyIds(): string[] {
  if (process.env.KALSHI_OWNER_KEY_IDS !== undefined) {           // tests only
    return process.env.KALSHI_OWNER_KEY_IDS.split(',').filter(Boolean)
  }
  const out = new Set<string>()
  for (const f of ['prediction_market/.env', 'prediction_market_soccer/.env', 'crypto_trading/.env']) {
    try {
      for (const line of fs.readFileSync(path.join(REPO_ROOT, f), 'utf8').split('\n')) {
        const m = /^(?:export\s+)?(KALSHI_PROD_API_KEY_ID|KALSHI_MARGIN_KEY_ID)\s*=\s*["']?([^"'\s]+)/.exec(line)
        if (m) out.add(m[2])
      }
    } catch { /* file absent */ }
  }
  return [...out]
}

/**
 * Which platform account already owns the Kalshi ACCOUNT behind these key
 * ids: 'owner' (the platform's own account), 'other' (another user id), or
 * null. Synchronous on purpose: called right before activate() with no await
 * in between, so two concurrent checks cannot both bind one Kalshi account.
 */
export function accountConflict(uid: string, accountKeyIds: string[], owner: string[] = ownerKeyIds()): 'owner' | 'other' | null {
  const mine = new Set(accountKeyIds.map(keyHash))
  if (owner.some(k => mine.has(keyHash(k)))) return 'owner'
  for (const line of readEnvLines()) {
    const m = /^KALSHI_PROD_API_KEY_ID_([0-9a-f-]{36})=(.+)$/.exec(line)
    if (m && m[1] !== uid && mine.has(keyHash(m[2]))) return 'other'
  }
  for (const [other, rec] of Object.entries(readRegistry())) {
    if (other !== uid && (rec?.account_key_hashes ?? []).some(h => mine.has(h))) return 'other'
  }
  return null
}

export function activate(uid: string, ident: Identity, keyId: string, accountKeyIds: string[] = [keyId]) {
  if (!isUserId(uid) || ident.id !== uid) throw new Error('identity mismatch')
  ensureDirs()
  if (fs.existsSync(pendingPemPath(uid))) {           // verified replacement goes live
    fs.renameSync(pendingPemPath(uid), pemPath(uid))
    fs.chmodSync(pemPath(uid), 0o600)
  }
  const suffix = `_${uid}=`
  const lines = readEnvLines().filter(l => !l.startsWith(`KALSHI_PROD_API_KEY_ID${suffix}`)
    && !l.startsWith(`KALSHI_PROD_PRIVATE_KEY_PATH${suffix}`))
  lines.push(`KALSHI_PROD_API_KEY_ID_${uid}=${keyId}`, `KALSHI_PROD_PRIVATE_KEY_PATH_${uid}=${pemPath(uid)}`)
  writeAtomic(envPath(), lines.join('\n') + '\n')
  const reg = readRegistry()
  const now = new Date().toISOString()
  reg[uid] = {
    user_id: uid,
    email: ident.email,
    email_confirmed: !!ident.email_confirmed,
    provider: ident.provider ?? null,
    status: 'active',
    validated_at: now,
    first_validated_at: reg[uid]?.first_validated_at ?? now,
    supabase_checked_at: now,
    key_id_masked: maskKeyId(keyId),
    account_key_hashes: [...new Set([keyId, ...accountKeyIds])].map(keyHash).sort(),
  }
  writeAtomic(registryPath(), JSON.stringify(reg, null, 2))
  fs.rmSync(pendingPath(uid), { force: true })
  fs.rmSync(disabledPath(uid), { force: true })
}

// Owner-controlled live-trading allowlist. The owner edits
// KALSHI_PROD_TRADING_USER_IDS in crypto_trading/.env BY HAND and restarts the
// runner; the RUNNING W7 process then publishes its effective list to
// ~/.kalshi/trading_active.json. Reported only while that process is alive, so
// the panel shows what is actually mirrored, never an un-restarted edit.
export function tradingEnabled(uid: string): boolean {
  try {
    const j = JSON.parse(fs.readFileSync(path.join(KEY_DIR, 'trading_active.json'), 'utf8'))
    if (!Array.isArray(j.user_ids) || !j.user_ids.includes(uid)) return false
    try { process.kill(Number(j.pid), 0) } catch { return false }   // runner gone
    return true
  } catch {
    return false
  }
}

export function activeKeyId(uid: string): string | null {
  const prefix = `KALSHI_PROD_API_KEY_ID_${uid}=`
  const line = readEnvLines().find(l => l.startsWith(prefix))
  return line ? line.slice(prefix.length) : null
}

/** Disconnect: remove env pair, registry entry, key file, pending, marker.
 *  A live-trading application is withdrawn too; if the owner had approved this
 *  user, a stop marker is left so reconnecting later never resumes trading by
 *  itself (only the owner removes it). */
export function deactivate(uid: string) {
  ensureDirs()
  if (tradingState(uid, false).approved_ratio !== null) stopTrading(uid, 'user', 'disconnected')
  fs.rmSync(tradingFile('requests', uid), { force: true })
  const suffix = `_${uid}=`
  writeAtomic(envPath(), readEnvLines().filter(l => !l.startsWith(`KALSHI_PROD_API_KEY_ID${suffix}`)
    && !l.startsWith(`KALSHI_PROD_PRIVATE_KEY_PATH${suffix}`)).join('\n') + '\n')
  const reg = readRegistry(); delete reg[uid]
  writeAtomic(registryPath(), JSON.stringify(reg, null, 2))
  for (const f of [pemPath(uid), pendingPemPath(uid), pendingPath(uid), disabledPath(uid),
    path.join(LEDGER_DIR, `${uid}.json`)]) fs.rmSync(f, { force: true })
}

// ── live-trading application (2026-10-04, owner directive: a standard flow) ─
// The user applies here (risk acknowledgement + requested copy ratio) and can
// stop here at any time. An application never trades by itself: the owner
// reviews it (crypto_trading/ops/kalshi_users.sh review <email>) and then, by
// hand, writes trading/approved/<uid>.json, adds the id to the runner's
// allowlist and restarts it. The runner honours a stop marker on the very next
// order. Ratios and the acknowledgement version must match the Python side
// (crypto_common/config.py MIRROR_RATIOS) and the panel (src/lib/kalshiUser.ts).
export const MIRROR_RATIOS = [0.1, 0.25, 0.5, 1] as const
export const TRADING_ACK_VERSION = '2026-10-04'
type TradingKind = 'requests' | 'approved' | 'stopped'
const tradingFile = (kind: TradingKind, uid: string) => path.join(KEY_DIR, 'trading', kind, `${uid}.json`)

function ownRecord(kind: TradingKind, uid: string): Record<string, any> | null {
  try {
    const j = JSON.parse(fs.readFileSync(tradingFile(kind, uid), 'utf8'))
    return j && typeof j === 'object' && !Array.isArray(j) && j.user_id === uid ? j : null
  } catch { return null }
}

const validRatio = (r: unknown): r is number => typeof r === 'number' && r > 0 && r <= 1

export type TradingState = {
  state: 'none' | 'requested' | 'approved' | 'active' | 'stopped'
  requested_ratio: number | null
  requested_at: string | null
  approved_ratio: number | null
  stopped_at: string | null
  stopped_by: 'user' | 'owner' | null
}

/** none -> requested (applied) -> approved (owner's record, waiting for the runner) -> active; stopped. */
export function tradingState(uid: string, connected: boolean): TradingState {
  const req = ownRecord('requests', uid), appr = ownRecord('approved', uid), stop = ownRecord('stopped', uid)
  const base = {
    requested_ratio: req && validRatio(req.ratio) ? req.ratio : null,
    requested_at: typeof req?.requested_at === 'string' ? req.requested_at : null,
    approved_ratio: appr && validRatio(appr.ratio) ? appr.ratio : null,
    stopped_at: typeof stop?.stopped_at === 'string' ? stop.stopped_at : null,
    stopped_by: stop ? (stop.by === 'owner' ? 'owner' as const : 'user' as const) : null,
  }
  let state: TradingState['state'] = 'none'
  if (base.approved_ratio !== null && !stop) state = connected && tradingEnabled(uid) ? 'active' : 'approved'
  else if (req) state = 'requested'
  else if (stop) state = 'stopped'
  return { state, ...base }
}

export function saveTradingRequest(uid: string, email: string | null, ratio: number) {
  if (!isUserId(uid)) throw new Error('bad user id')
  ensureDirs()
  writeAtomic(tradingFile('requests', uid), JSON.stringify({
    user_id: uid, email, ratio, ack_version: TRADING_ACK_VERSION, requested_at: new Date().toISOString(),
  }))
}

export function cancelTradingRequest(uid: string) {
  fs.rmSync(tradingFile('requests', uid), { force: true })
}

/** Stop marker: the runner skips this user from the very next order. */
export function stopTrading(uid: string, by: 'user' | 'owner', reason = '') {
  if (!isUserId(uid)) throw new Error('bad user id')
  ensureDirs()
  writeAtomic(tradingFile('stopped', uid), JSON.stringify({ user_id: uid, by, reason, stopped_at: new Date().toISOString() }))
  fs.rmSync(tradingFile('requests', uid), { force: true })
}

/** Best-effort macOS desktop notice to the owner (never blocks, never throws). */
export function notifyOwner(message: string) {
  if (process.platform !== 'darwin' || process.env.KALSHI_NOTIFY_OWNER === '0') return
  const text = message.replace(/["\\\r\n]/g, ' ').slice(0, 180)
  try {
    execFile('/usr/bin/osascript', ['-e', `display notification "${text}" with title "Kalshi 实盘跟单"`],
      { timeout: 5000 }, () => {})
  } catch { /* notification is optional */ }
}

// ── live check: one signed read-only GET against Kalshi PROD ──────────────
export function signedHeaders(pem: string, keyId: string, method: string, apiPath: string) {
  const ts = String(Date.now())
  const msg = Buffer.from(`${ts}${method}${API_ROOT}${apiPath}`)
  const key = crypto.createPrivateKey(pem)
  // Same pre-sign text and headers for both key types (Kalshi changelog 2026-09-24): RSA -> RSA-PSS/SHA-256,
  // Ed25519 -> a plain Ed25519 signature (Node: algorithm null).
  const sig = key.asymmetricKeyType === 'ed25519'
    ? crypto.sign(null, msg, key).toString('base64')
    : crypto.sign('sha256', msg, { key, padding: crypto.constants.RSA_PKCS1_PSS_PADDING, saltLength: crypto.constants.RSA_PSS_SALTLEN_DIGEST }).toString('base64')
  return { 'KALSHI-ACCESS-KEY': keyId, 'KALSHI-ACCESS-TIMESTAMP': ts, 'KALSHI-ACCESS-SIGNATURE': sig }
}

/** Every key id on the Kalshi account behind this key (signed read-only GET /api_keys). */
export async function fetchAccountKeyIds(pem: string, keyId: string, fetchImpl: typeof fetch = fetch):
    Promise<{ ok: true; key_ids: string[] } | { ok: false; error: string }> {
  try {
    const res = await fetchImpl(`${PROD_BASE}${API_ROOT}/api_keys`, {
      headers: signedHeaders(pem, keyId, 'GET', '/api_keys'), signal: AbortSignal.timeout(10000),
    })
    if (res.status === 401 || res.status === 403) {
      return { ok: false, error: 'Kalshi 拒绝认证：API Key 与私钥不匹配，或 Key 已被撤销' }
    }
    if (!res.ok) return { ok: false, error: `无法读取 Kalshi 账户的 Key 列表（HTTP ${res.status}），请稍后再试` }
    const body: any = await res.json()
    const ids: string[] = (Array.isArray(body?.api_keys) ? body.api_keys : [])
      .map((k: any) => k?.api_key_id).filter((x: unknown) => typeof x === 'string')
    if (!ids.includes(keyId)) return { ok: false, error: '无法确认这个 API Key 所属的 Kalshi 账户' }
    return { ok: true, key_ids: ids }
  } catch {
    return { ok: false, error: '无法连接 Kalshi（网络超时）' }
  }
}

// Measured 2026-10-01 against the owner's account: the integer `balance` field
// is NOT the account total; `balance_dollars` equals the sum of
// `balance_breakdown[].balance` (dollar strings, one per exchange instance).
// Crypto 15-minute markets settle on exchange_index 2.
export async function fetchBalance(pem: string, keyId: string, fetchImpl: typeof fetch = fetch):
    Promise<{ ok: true; balance_usd: number; instance2_usd: number | null } | { ok: false; error: string; status?: number }> {
  try {
    const res = await fetchImpl(`${PROD_BASE}${API_ROOT}/portfolio/balance`, {
      headers: signedHeaders(pem, keyId, 'GET', '/portfolio/balance'), signal: AbortSignal.timeout(10000),
    })
    if (res.status === 401 || res.status === 403) {
      return { ok: false, status: res.status, error: 'Kalshi 拒绝认证：API Key 与私钥不匹配，或 Key 已被撤销' }
    }
    if (!res.ok) return { ok: false, status: res.status, error: `Kalshi 返回错误（HTTP ${res.status}），请稍后再试` }
    const body: any = await res.json()
    const total = Number(body?.balance_dollars)
    if (!Number.isFinite(total)) return { ok: false, error: 'Kalshi 返回的余额格式无法识别' }
    const i2 = Array.isArray(body?.balance_breakdown)
      ? body.balance_breakdown.find((x: any) => x?.exchange_index === 2) : null
    const i2usd = i2 ? Number(i2.balance) : NaN
    return { ok: true, balance_usd: total, instance2_usd: Number.isFinite(i2usd) ? i2usd : null }
  } catch {
    return { ok: false, error: '无法连接 Kalshi（网络超时）' }
  }
}
