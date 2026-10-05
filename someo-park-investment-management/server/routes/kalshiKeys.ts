// server/routes/kalshiKeys.ts — per-user Kalshi PROD credentials (2026-10-01).
//
// Identity is ALWAYS the verified Supabase JWT (Authorization: Bearer <token>);
// a client-supplied user id is never read. The owner account never stores a
// key here (it trades with the server's own un-suffixed KALSHI_PROD_* keys).
// A key goes live only after one signed read-only GET /portfolio/balance
// against Kalshi PROD succeeds with that exact key id + private key.
// Before activation the id + email are re-read from auth.users by id
// (supabaseUserById) and must match the session, so every artifact keyed by
// this id belongs to exactly this Supabase account.
// Validation never starts trading: that needs the owner's manual allowlist.
// Live trading (2026-10-04): the user APPLIES here (risk acknowledgement +
// ratio) and can STOP here; an application never trades by itself - the owner
// approves by hand and restarts the runner.

import { Router, type Request, type Response, type NextFunction, type RequestHandler } from 'express'
import fs from 'node:fs'
import path from 'node:path'
import { userFromToken, supabaseUserById } from '../utils/supabaseAdmin.js'
import {
  OWNER_EMAIL, isUserId, checkKeyId, checkPem, savePendingKeyId, readPendingKeyId,
  savePem, hasPem, hasPendingPem, pemPath, pemForVerify, activate, deactivate, activeKeyId, pemKeyType,
  readRegistry, isDisabled, fetchBalance, maskKeyId, PEM_MAX_BYTES, tradingEnabled, LEDGER_DIR,
  checkIdentity, audit, type Identity, fetchAccountKeyIds, accountConflict, syncEmail,
  MIRROR_RATIOS, TRADING_ACK_VERSION, tradingState, saveTradingRequest, cancelTradingRequest, stopTrading, notifyOwner,
} from '../utils/kalshiUserKeys.js'

type Authed = Request & { user?: Identity }
type Auth = (token?: string) => Promise<Identity | null>
type Lookup = (id: string) => Promise<Identity | null>

// Express 4 does not catch rejected async handlers, and an unhandled rejection
// terminates Node - one user's disk error must never take the owner's backend
// down. Every handler goes through this wrapper.
const safe = (fn: (req: Authed, res: Response) => unknown): RequestHandler =>
  async (req, res) => {
    try {
      await fn(req as Authed, res)
    } catch (e) {
      console.error('[kalshi-keys]', (e as Error)?.message)
      if (!res.headersSent) res.status(500).json({ error: '服务器暂时无法处理，请稍后再试' })
    }
  }

// `auth` / `lookup` are injectable for tests only; production uses real Supabase.
export function createKalshiKeysRouter(auth: Auth = userFromToken, lookup: Lookup = supabaseUserById) {
  const router = Router()

  const requireUser = async (req: Authed, res: Response, next: NextFunction) => {
    try {
      const h = req.headers.authorization || ''
      const token = h.startsWith('Bearer ') ? h.slice(7) : undefined
      const user = await auth(token)
      if (!user || !isUserId(user.id)) return res.status(401).json({ error: '请先登录' })
      req.user = user
      next()
    } catch {
      res.status(401).json({ error: '请先登录' })
    }
  }

  const isOwner = (req: Authed) => (req.user?.email || '').toLowerCase() === OWNER_EMAIL

  // Per-user, PER-ACTION throttle (a shared counter locked users out of /verify
  // after a few corrected typos - caught by the 2026-10-01 end-to-end check).
  const hits = new Map<string, number[]>()
  const throttle = (uid: string, action: string, max = 10, windowMs = 60_000) => {
    const key = `${uid}:${action}`
    const now = Date.now()
    const arr = (hits.get(key) || []).filter(t => now - t < windowMs)
    arr.push(now); hits.set(key, arr)
    return arr.length > max
  }

  // Cached so every page view does not hit Kalshi.
  const balanceCache = new Map<string, { at: number; body: any }>()

  router.use(requireUser as RequestHandler)

  router.get('/status', safe((req, res) => {
    const uid = req.user!.id
    if (isOwner(req)) return res.json({ owner: true, connected: true, source: 'owner' })
    const synced = syncEmail(uid, req.user!)            // auth.users email changed since /verify
    if (synced) audit(uid, synced.to, 'email_synced', { from: synced.from })
    const reg = readRegistry()[uid]
    const active = !!reg && !!activeKeyId(uid) && hasPem(uid)
    const disabled = active && isDisabled(uid)
    const pendingKey = readPendingKeyId(uid)
    res.json({
      owner: false,
      user_id: uid,
      email: req.user!.email,
      connected: active && !disabled,
      auth_failed: disabled,
      validated_at: reg?.validated_at ?? null,
      key_id_masked: reg?.key_id_masked ?? null,
      pending_key_id: !!pendingKey,
      pending_key_id_masked: pendingKey ? maskKeyId(pendingKey) : null,
      pem_uploaded: hasPem(uid) || hasPendingPem(uid),
      pending_pem: hasPendingPem(uid),
      trading_enabled: active && !disabled && tradingEnabled(uid) && tradingState(uid, true).state === 'active',
      trading: reg ? tradingState(uid, active && !disabled) : null,
    })
  }))

  const connectedNow = (uid: string) => !!readRegistry()[uid] && !!activeKeyId(uid) && hasPem(uid) && !isDisabled(uid)

  router.post('/trading/request', safe((req, res) => {
    if (isOwner(req)) return res.status(403).json({ error: '主账户无需申请' })
    const uid = req.user!.id
    if (throttle(uid, 'trading', 12)) return res.status(429).json({ error: '操作过于频繁，请稍后再试' })
    if (!connectedNow(uid)) return res.status(409).json({ error: '请先连接并通过检测你的 Kalshi 账户' })
    const { ratio, ack, ack_version } = (req.body ?? {}) as Record<string, unknown>
    if (!MIRROR_RATIOS.some(r => r === ratio)) return res.status(400).json({ error: '跟单比例只能是 10%、25%、50% 或 100%' })
    if (ack !== true || ack_version !== TRADING_ACK_VERSION) return res.status(400).json({ error: '请先阅读并勾选风险确认' })
    const st = tradingState(uid, true).state
    if (st === 'active' || st === 'approved') return res.status(409).json({ error: '已经开通；如需修改比例，请先停止跟单再重新申请' })
    saveTradingRequest(uid, req.user!.email, ratio as number)
    audit(uid, req.user!.email, 'trading_requested', { ratio, ack_version })
    notifyOwner(`${req.user!.email ?? uid} 申请开通实盘跟单（比例 ${Math.round((ratio as number) * 100)}%）`)
    res.json({ ok: true, trading: tradingState(uid, true) })
  }))

  router.delete('/trading/request', safe((req, res) => {
    if (isOwner(req)) return res.status(403).json({ error: '主账户无需申请' })
    const uid = req.user!.id
    if (tradingState(uid, connectedNow(uid)).state !== 'requested') return res.status(409).json({ error: '没有待审核的申请' })
    cancelTradingRequest(uid)
    audit(uid, req.user!.email, 'trading_request_cancelled')
    res.json({ ok: true, trading: tradingState(uid, connectedNow(uid)) })
  }))

  // Stopping is always allowed and immediate; it never needs the owner.
  router.post('/trading/stop', safe((req, res) => {
    if (isOwner(req)) return res.status(403).json({ error: '主账户请在服务器上操作' })
    const uid = req.user!.id
    const st = tradingState(uid, connectedNow(uid)).state
    if (st !== 'active' && st !== 'approved') return res.status(409).json({ error: '当前没有开通实盘跟单' })
    stopTrading(uid, 'user', 'panel')
    audit(uid, req.user!.email, 'trading_stopped', { by: 'user', was: st })
    notifyOwner(`${req.user!.email ?? uid} 已停止实盘跟单`)
    res.json({ ok: true, trading: tradingState(uid, connectedNow(uid)) })
  }))

  router.post('/key-id', safe((req, res) => {
    if (isOwner(req)) return res.status(403).json({ error: '主账户使用平台密钥，无需填写' })
    if (throttle(req.user!.id, 'key-id')) return res.status(429).json({ error: '操作过于频繁，请稍后再试' })
    const raw = (req.body ?? {}).key_id
    const c = checkKeyId(raw)
    if ('error' in c) return res.status(400).json({ error: c.error })
    savePendingKeyId(req.user!.id, raw)
    audit(req.user!.id, req.user!.email, 'key_id_saved', { key_id_masked: maskKeyId(raw) })
    res.json({ ok: true, key_id_masked: maskKeyId(raw) })
  }))

  router.post('/pem', safe((req, res) => {
    if (isOwner(req)) return res.status(403).json({ error: '主账户使用平台密钥，无需上传' })
    if (throttle(req.user!.id, 'pem')) return res.status(429).json({ error: '操作过于频繁，请稍后再试' })
    const len = Number(req.headers['content-length'] || 0)
    if (len > PEM_MAX_BYTES * 2 + 1024) return res.status(413).json({ error: '文件过大：私钥文件只有几 KB' })
    const body = req.body ?? {}
    if (Array.isArray(body) || Array.isArray(body.files) || Object.keys(body).some(k => k !== 'filename' && k !== 'content')) {
      return res.status(400).json({ error: '只能上传一个私钥文件（Kalshi 下载的 .txt 或 .pem）' })
    }
    const c = checkPem(body.filename, body.content)
    if ('error' in c) return res.status(400).json({ error: c.error })
    savePem(req.user!.id, body.content)
    audit(req.user!.id, req.user!.email, 'pem_uploaded', { pending: hasPendingPem(req.user!.id), key_type: pemKeyType(body.content) })
    res.json({ ok: true, pending: hasPendingPem(req.user!.id) })
  }))

  router.post('/verify', safe(async (req, res) => {
    if (isOwner(req)) return res.status(403).json({ error: '主账户使用平台密钥' })
    const uid = req.user!.id
    if (throttle(uid, 'verify', 6)) return res.status(429).json({ error: '检测过于频繁，请稍后再试' })
    const keyId = readPendingKeyId(uid) || activeKeyId(uid)
    if (!keyId) return res.status(400).json({ error: '请先保存 Kalshi Production API Key' })
    const pemFile = pemForVerify(uid)
    if (!pemFile) return res.status(400).json({ error: '请先上传 Kalshi 下载的私钥文件（.txt 或 .pem）' })
    const sb = await lookup(uid)                          // fresh auth.users read by id
    const idc = checkIdentity(req.user!, sb)
    if ('error' in idc) {
      audit(uid, req.user!.email, 'verify_rejected', { reason: idc.error })
      return res.status(idc.code).json({ error: idc.error })
    }
    const pem = fs.readFileSync(pemFile, 'utf8')
    const r = await fetchBalance(pem, keyId)
    if ('error' in r) {
      audit(uid, req.user!.email, 'verify_failed', { key_id_masked: maskKeyId(keyId), reason: r.error })
      return res.status(400).json({ error: r.error })
    }
    const acct = await fetchAccountKeyIds(pem, keyId)
    if ('error' in acct) {
      audit(uid, req.user!.email, 'verify_failed', { key_id_masked: maskKeyId(keyId), reason: acct.error })
      return res.status(400).json({ error: acct.error })
    }
    // one Kalshi account <-> one platform account. No await from here to activate().
    const conflict = accountConflict(uid, acct.key_ids)
    if (conflict) {
      const error = conflict === 'owner'
        ? '这个 Kalshi 账户是平台主账户，不能连接到其他平台账户'
        : '这个 Kalshi 账户已连接到另一个平台账户；同一个 Kalshi 账户只能连接一个平台账户'
      audit(uid, req.user!.email, 'verify_rejected', { key_id_masked: maskKeyId(keyId), reason: error })
      return res.status(409).json({ error })
    }
    activate(uid, sb!, keyId, acct.key_ids)
    audit(uid, sb!.email, 'verified', { key_id_masked: maskKeyId(keyId), provider: sb!.provider ?? null })
    balanceCache.delete(uid)
    res.json({ ok: true, balance_usd: r.balance_usd, instance2_usd: r.instance2_usd })
  }))

  router.delete('/', safe((req, res) => {
    if (isOwner(req)) return res.status(403).json({ error: '主账户使用平台密钥' })
    deactivate(req.user!.id)
    audit(req.user!.id, req.user!.email, 'disconnected')
    balanceCache.delete(req.user!.id)
    res.json({ ok: true })
  }))

  router.get('/balance', safe(async (req, res) => {
    const uid = req.user!.id
    if (isOwner(req)) return res.json({ source: 'owner' })   // pages keep the owner's own figure
    const keyId = activeKeyId(uid)
    if (!keyId || !hasPem(uid) || isDisabled(uid)) return res.json({ source: 'owner' })
    const hit = balanceCache.get(uid)
    if (hit && Date.now() - hit.at < 30_000) return res.json(hit.body)
    const r = await fetchBalance(fs.readFileSync(pemPath(uid), 'utf8'), keyId)
    const body = 'error' in r
      ? { source: 'user', error: r.error }
      : { source: 'user', balance_usd: r.balance_usd, instance2_usd: r.instance2_usd, as_of: new Date().toISOString() }
    balanceCache.set(uid, { at: Date.now(), body })
    res.json(body)
  }))

  // Private per-user ledger, written by the exporter OUTSIDE the repo
  // (~/.kalshi/ledgers/<uid>.json) and served only to its owner.
  router.get('/ledger', safe((req, res) => {
    const uid = req.user!.id
    if (isOwner(req) || !activeKeyId(uid)) return res.json({ source: 'owner' })
    try {
      const ledger = JSON.parse(fs.readFileSync(path.join(LEDGER_DIR, `${uid}.json`), 'utf8'))
      if (ledger?.user_id !== uid) return res.json({ source: 'user', empty: true })   // never another id's book
      res.json({ source: 'user', ...ledger })
    } catch {
      res.json({ source: 'user', empty: true })
    }
  }))

  return router
}

export default createKalshiKeysRouter()
