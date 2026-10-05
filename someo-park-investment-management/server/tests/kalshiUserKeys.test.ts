import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

// Isolate ALL storage in a temp dir before the module reads KALSHI_USER_KEY_DIR.
const DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'kalshi-keys-'))
process.env.KALSHI_USER_KEY_DIR = DIR
process.env.KALSHI_NOTIFY_OWNER = '0'          // never pop desktop notices from tests
const K = await import('../utils/kalshiUserKeys.ts')

const UID = 'abcdef12-2222-4333-8444-5555555555ab'
const KEY = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
const ID = { id: UID, email: 'u@example.com', email_confirmed: true, provider: 'google' }
const rsaPem = (bits = 2048) => crypto.generateKeyPairSync('rsa', { modulusLength: bits })
  .privateKey.export({ type: 'pkcs8', format: 'pem' }).toString()

test('key id: exact 36-char lowercase UUID, whitespace is an error not a trim', () => {
  assert.equal(K.checkKeyId(KEY).ok, true)
  for (const bad of [` ${KEY}`, `${KEY} `, `${KEY}\n`, KEY.toUpperCase(), KEY.slice(1), '', 42, null]) {
    assert.equal(K.checkKeyId(bad as any).ok, false, String(bad))
  }
  const ws = K.checkKeyId(`${KEY} `)
  assert.equal(ws.ok, false); assert.match((ws as any).error, /空格/)
})

test('user id doubles as the path-traversal guard', () => {
  assert.equal(K.isUserId(UID), true)
  for (const bad of ['../etc/passwd', `${UID}/../x`, 'abc', UID.toUpperCase(), '']) {
    assert.equal(K.isUserId(bad), false, bad)
  }
})

test('pem: one small unencrypted RSA private key, .pem name, nothing else', () => {
  const pem = rsaPem()
  assert.equal(K.checkPem('kalshi.pem', pem).ok, true)
  assert.equal(K.checkPem('KEY.PEM', pem).ok, true)
  // wrong type / name / path
  for (const name of ['kalshi.pem.exe', '../kalshi.pem', 'a/b.pem', 'kalshi.p12', 'kalshi.json']) {
    assert.equal(K.checkPem(name, pem).ok, false, name)
  }
  // size bounds
  assert.equal(K.checkPem('k.pem', 'x'.repeat(50)).ok, false)
  assert.equal(K.checkPem('k.pem', pem + 'A'.repeat(K.PEM_MAX_BYTES)).ok, false)
  // two keys in one file, public key, encrypted key, garbage
  assert.equal(K.checkPem('k.pem', pem + pem).ok, false)
  const pub = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 }).publicKey
    .export({ type: 'spki', format: 'pem' }).toString()
  assert.equal(K.checkPem('k.pem', pub).ok, false)
  const enc = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 }).privateKey
    .export({ type: 'pkcs8', format: 'pem', cipher: 'aes-256-cbc', passphrase: 'x' }).toString()
  assert.equal(K.checkPem('k.pem', enc).ok, false)
  assert.equal(K.checkPem('k.pem', '-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n' + 'A'.repeat(300)).ok, false)
  // non-RSA
  const ec = crypto.generateKeyPairSync('ec', { namedCurve: 'P-256' }).privateKey
    .export({ type: 'pkcs8', format: 'pem' }).toString()
  assert.equal(K.checkPem('k.pem', ec.padEnd(260, '\n')).ok, false)
  // multiple files / non-string payloads
  assert.equal(K.checkPem(['a.pem', 'b.pem'] as any, pem).ok, false)
  assert.equal(K.checkPem('k.pem', [pem] as any).ok, false)
})

test('storage: 0600 files under a 0700 dir; env pair only after activate; owner names untouched', () => {
  const pem = rsaPem()
  K.savePendingKeyId(UID, KEY)
  K.savePem(UID, pem)
  assert.equal(fs.statSync(path.join(DIR, `prod_${UID}.pem`)).mode & 0o777, 0o600)
  assert.equal(fs.statSync(DIR).mode & 0o777, 0o700)
  assert.equal(K.activeKeyId(UID), null)                    // not live before verification
  K.activate(UID, ID, KEY)
  const env = fs.readFileSync(path.join(DIR, 'users.env'), 'utf8')
  assert.match(env, new RegExp(`^KALSHI_PROD_API_KEY_ID_${UID}=${KEY}$`, 'm'))
  assert.match(env, new RegExp(`^KALSHI_PROD_PRIVATE_KEY_PATH_${UID}=.*prod_${UID}\\.pem$`, 'm'))
  assert.doesNotMatch(env, /^KALSHI_PROD_API_KEY_ID=/m)        // never the owner's un-suffixed name
  assert.equal(fs.statSync(path.join(DIR, 'users.env')).mode & 0o777, 0o600)
  assert.equal(K.activeKeyId(UID), KEY)
  assert.equal(K.readRegistry()[UID].status, 'active')
  // re-activation replaces, never duplicates
  K.activate(UID, ID, KEY)
  assert.equal(fs.readFileSync(path.join(DIR, 'users.env'), 'utf8').split('\n')
    .filter(l => l.startsWith(`KALSHI_PROD_API_KEY_ID_${UID}=`)).length, 1)
  K.deactivate(UID)
  assert.equal(K.activeKeyId(UID), null)
  assert.equal(fs.existsSync(path.join(DIR, `prod_${UID}.pem`)), false)
})

test('live check: 401 is reported as a key mismatch and never activates', async () => {
  const fake401 = (async () => new Response('{}', { status: 401 })) as typeof fetch
  const r = await K.fetchBalance(rsaPem(), KEY, fake401)
  assert.equal(r.ok, false); assert.match((r as any).error, /不匹配/)
  const ok = (async () => new Response(JSON.stringify({ balance: 999, balance_dollars: '123.45',
    balance_breakdown: [{ exchange_index: 0, balance: '23.45' }, { exchange_index: 2, balance: '100.00' }] }),
    { status: 200 })) as typeof fetch
  const r2 = await K.fetchBalance(rsaPem(), KEY, ok)
  assert.deepEqual(r2, { ok: true, balance_usd: 123.45, instance2_usd: 100 })   // balance_dollars, not the int
})

test('signature is RSA-PSS over ts+method+/trade-api/v2+path', () => {
  const kp = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 })
  const pem = kp.privateKey.export({ type: 'pkcs8', format: 'pem' }).toString()
  const h = K.signedHeaders(pem, KEY, 'GET', '/portfolio/balance')
  const ok = crypto.verify('sha256', Buffer.from(`${h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/portfolio/balance`),
    { key: kp.publicKey, padding: crypto.constants.RSA_PKCS1_PSS_PADDING, saltLength: crypto.constants.RSA_PSS_SALTLEN_DIGEST },
    Buffer.from(h['KALSHI-ACCESS-SIGNATURE'], 'base64'))
  assert.equal(ok, true)
})

test('re-upload while ACTIVE goes to pending and replaces the live key only on activate', () => {
  const live = rsaPem(), next = rsaPem()
  K.savePendingKeyId(UID, KEY); K.savePem(UID, live); K.activate(UID, ID, KEY)
  K.savePem(UID, next)                                   // user uploads a new key while connected
  assert.equal(fs.readFileSync(path.join(DIR, `prod_${UID}.pem`), 'utf8'), live)   // live key untouched
  assert.equal(K.hasPendingPem(UID), true)
  assert.equal(K.pemForVerify(UID), K.pendingPemPath(UID))
  K.activate(UID, ID, KEY)                  // only after the live check passes
  assert.equal(fs.readFileSync(path.join(DIR, `prod_${UID}.pem`), 'utf8'), next)
  assert.equal(K.hasPendingPem(UID), false)
  assert.equal(fs.statSync(path.join(DIR, `prod_${UID}.pem`)).mode & 0o777, 0o600)
  K.deactivate(UID)
})

test('trading_enabled reflects the RUNNING runner allowlist only', () => {
  const f = path.join(DIR, 'trading_active.json')
  fs.writeFileSync(f, JSON.stringify({ user_ids: [UID], pid: process.pid }))
  assert.equal(K.tradingEnabled(UID), true)
  assert.equal(K.tradingEnabled('abcdef12-2222-4333-8444-000000000000'), false)
  fs.writeFileSync(f, JSON.stringify({ user_ids: [UID], pid: 999999 }))   // runner gone
  assert.equal(K.tradingEnabled(UID), false)
  fs.rmSync(f)
  assert.equal(K.tradingEnabled(UID), false)
})

test('identity: session and a fresh auth.users read must be the same id + email', () => {
  const sess = { id: UID, email: 'U@Example.com' }
  assert.equal(K.checkIdentity(sess, { ...ID }).ok, true)                       // email case-insensitive
  const r503 = K.checkIdentity(sess, null)
  assert.ok('error' in r503 && r503.code === 503)
  for (const sb of [{ ...ID, id: 'abcdef12-2222-4333-8444-000000000000' },      // other account
                    { ...ID, email: 'other@example.com' },                       // email changed
                    { ...ID, email: null },
                    { ...ID, email_confirmed: false }]) {                        // unverified email
    const r = K.checkIdentity(sess, sb)
    assert.ok('error' in r && r.code === 400, JSON.stringify(sb))
  }
  assert.ok('error' in K.checkIdentity({ id: UID, email: null }, { ...ID }))
})

test('registry records the SAME id + email everywhere; re-verify keeps first_validated_at', () => {
  K.savePendingKeyId(UID, KEY); K.savePem(UID, rsaPem()); K.activate(UID, ID, KEY)
  const first = K.readRegistry()[UID]
  assert.equal(first.user_id, UID)
  assert.equal(first.email, 'u@example.com')
  assert.equal(first.email_confirmed, true)
  assert.equal(first.provider, 'google')
  const envTxt = fs.readFileSync(path.join(DIR, 'users.env'), 'utf8')
  assert.ok(envTxt.includes(`KALSHI_PROD_API_KEY_ID_${UID}=${KEY}`))
  assert.ok(envTxt.includes(`KALSHI_PROD_PRIVATE_KEY_PATH_${UID}=${path.join(DIR, `prod_${UID}.pem`)}`))
  K.activate(UID, { ...ID, email: 'new@example.com' }, KEY)
  const again = K.readRegistry()[UID]
  assert.equal(again.first_validated_at, first.first_validated_at)
  assert.equal(again.email, 'new@example.com')
  assert.throws(() => K.activate(UID, { ...ID, id: 'abcdef12-2222-4333-8444-000000000000' }, KEY))
  // disconnect removes everything keyed by this id, including the private ledger
  fs.mkdirSync(K.LEDGER_DIR, { recursive: true })
  fs.writeFileSync(path.join(K.LEDGER_DIR, `${UID}.json`), '{}')
  K.deactivate(UID)
  assert.equal(K.readRegistry()[UID], undefined)
  for (const f of [`prod_${UID}.pem`, `ledgers/${UID}.json`, `pending/${UID}.json`]) {
    assert.equal(fs.existsSync(path.join(DIR, f)), false, f)
  }
})

test('audit log is private and never holds key material', () => {
  K.audit(UID, 'u@example.com', 'verified', { key_id_masked: K.maskKeyId(KEY) })
  const file = path.join(DIR, 'audit.jsonl')
  assert.equal(fs.statSync(file).mode & 0o777, 0o600)
  const last = JSON.parse(fs.readFileSync(file, 'utf8').trim().split('\n').at(-1)!)
  assert.equal(last.user_id, UID)
  assert.equal(last.event, 'verified')
  assert.ok(!fs.readFileSync(file, 'utf8').includes(KEY))
  assert.ok(!fs.readFileSync(file, 'utf8').includes('PRIVATE KEY'))
})

test('route: /verify refuses before touching Kalshi when Supabase disagrees', async () => {
  const express = (await import('express')).default
  const { createKalshiKeysRouter } = await import('../routes/kalshiKeys.ts')
  const U2 = 'abcdef12-2222-4333-8444-5555555555cd'
  let sbAnswer: any = null
  const app = express(); app.use(express.json())
  app.use('/k', createKalshiKeysRouter(async () => ({ id: U2, email: 'a@example.com' }), async () => sbAnswer))
  const srv = app.listen(0); const base = `http://127.0.0.1:${(srv.address() as any).port}/k`
  const post = (p: string, body?: any) => fetch(base + p, { method: 'POST', headers: { Authorization: 'Bearer t',
    'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined })
  try {
    assert.equal((await post('/key-id', { key_id: KEY })).status, 200)
    assert.equal((await post('/pem', { filename: 'k.pem', content: rsaPem() })).status, 200)
    sbAnswer = null
    assert.equal((await post('/verify')).status, 503)                         // Supabase unreachable
    sbAnswer = { id: U2, email: 'someone-else@example.com', email_confirmed: true }
    const r = await post('/verify')
    assert.equal(r.status, 400)
    assert.match((await r.json() as any).error, /邮箱/)
    assert.equal(K.readRegistry()[U2], undefined)                             // nothing activated
    const st = await (await fetch(base + '/status', { headers: { Authorization: 'Bearer t' } })).json() as any
    assert.equal(st.user_id, U2)
    assert.equal(st.connected, false)
    const log = fs.readFileSync(path.join(DIR, 'audit.jsonl'), 'utf8')
    assert.ok(log.includes('"verify_rejected"') && log.includes(U2))
  } finally {
    srv.close()
    K.deactivate(U2)
  }
})

test('one Kalshi account <-> one platform account (owner account refused too)', () => {
  const A = 'abcdef12-2222-4333-8444-0000000000a1', B = 'abcdef12-2222-4333-8444-0000000000b2'
  const k1 = 'aaaaaaaa-bbbb-4ccc-8ddd-000000000001', k2 = 'aaaaaaaa-bbbb-4ccc-8ddd-000000000002'
  const owner = ['aaaaaaaa-bbbb-4ccc-8ddd-0000000000ff']
  assert.equal(K.accountConflict(A, [k1, k2], owner), null)
  K.savePendingKeyId(A, k1); K.savePem(A, rsaPem()); K.activate(A, { ...ID, id: A }, k1, [k1, k2])
  assert.deepEqual(K.readRegistry()[A].account_key_hashes, [K.keyHash(k1), K.keyHash(k2)].sort())
  assert.equal(K.accountConflict(A, [k1, k2], owner), null)             // re-verify of the same user
  assert.equal(K.accountConflict(B, [k1], owner), 'other')              // same key id
  assert.equal(K.accountConflict(B, [k2], owner), 'other')              // other key of the same account
  assert.equal(K.accountConflict(B, ['aaaaaaaa-bbbb-4ccc-8ddd-000000000003', owner[0]], owner), 'owner')
  K.deactivate(A)
  assert.equal(K.accountConflict(B, [k1, k2], owner), null)             // freed after disconnect
})

test('account key list: signed GET /api_keys, must contain the key being verified', async () => {
  const pem = rsaPem()
  let seen: any = null
  const ok = await K.fetchAccountKeyIds(pem, KEY, (async (url: string, init: any) => {
    seen = { url, h: init.headers }
    return new Response(JSON.stringify({ api_keys: [{ api_key_id: KEY }, { api_key_id: 'x' }] }), { status: 200 })
  }) as any)
  assert.ok('key_ids' in ok && ok.key_ids.length === 2)
  assert.equal(seen.url, `${K.PROD_BASE}${K.API_ROOT}/api_keys`)
  assert.equal(seen.h['KALSHI-ACCESS-KEY'], KEY)
  const missing = await K.fetchAccountKeyIds(pem, KEY, (async () =>
    new Response(JSON.stringify({ api_keys: [{ api_key_id: 'x' }] }), { status: 200 })) as any)
  assert.ok('error' in missing)
  const denied = await K.fetchAccountKeyIds(pem, KEY, (async () => new Response('', { status: 401 })) as any)
  assert.ok('error' in denied && /拒绝认证/.test(denied.error))
})

test('email follows auth.users after a change, id never changes', () => {
  K.savePendingKeyId(UID, KEY); K.savePem(UID, rsaPem()); K.activate(UID, ID, KEY)
  assert.equal(K.syncEmail(UID, { ...ID }), null)                        // nothing to do
  assert.deepEqual(K.syncEmail(UID, { ...ID, email: 'moved@example.com' }), { from: 'u@example.com', to: 'moved@example.com' })
  const rec = K.readRegistry()[UID]
  assert.equal(rec.email, 'moved@example.com')
  assert.equal(rec.user_id, UID)
  assert.equal(K.syncEmail(UID, { ...ID, id: 'abcdef12-2222-4333-8444-000000000000', email: 'x@y.z' }), null)
  K.deactivate(UID)
})


// Kalshi's web app issues Ed25519 keys by default since 2026-10-01 and downloads them as `<key name>.txt`
// (PKCS#8 PEM text inside). The old RSA .pem path must keep working for existing users.
const edPem = () => crypto.generateKeyPairSync('ed25519').privateKey.export({ type: 'pkcs8', format: 'pem' }).toString()

test('pem: an Ed25519 key downloaded as .txt is accepted, so are .key/.pem names; type is reported', () => {
  const pem = edPem()
  assert.ok(pem.length < 200 && pem.length >= K.PEM_MIN_BYTES)
  assert.equal(K.checkPem('SomeoParkTestKey.txt', pem).ok, true)
  assert.equal(K.checkPem('kalshi.pem', pem).ok, true)
  assert.equal(K.checkPem('kalshi.key', pem).ok, true)
  assert.equal(K.checkPem('kalshi.txt', rsaPem()).ok, true)
  assert.equal(K.pemKeyType(pem), 'ed25519'); assert.equal(K.pemKeyType(rsaPem()), 'rsa')
  const ec = crypto.generateKeyPairSync('ec', { namedCurve: 'P-256' }).privateKey.export({ type: 'pkcs8', format: 'pem' }).toString()
  const r = K.checkPem('k.txt', ec); assert.equal(r.ok, false); assert.match((r as any).error, /RSA 或 Ed25519/)
})

test('signed headers: Ed25519 and RSA signatures both verify over the same pre-sign text', () => {
  for (const kind of ['ed25519', 'rsa'] as const) {
    const kp = crypto.generateKeyPairSync(kind as any, kind === 'rsa' ? { modulusLength: 2048 } : undefined)
    const pem = kp.privateKey.export({ type: 'pkcs8', format: 'pem' }).toString()
    const h = K.signedHeaders(pem, KEY, 'GET', '/portfolio/balance')
    const msg = Buffer.from(`${h['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/portfolio/balance`)
    const sig = Buffer.from(h['KALSHI-ACCESS-SIGNATURE'], 'base64')
    const ok = kind === 'ed25519'
      ? crypto.verify(null, msg, kp.publicKey, sig)
      : crypto.verify('sha256', msg, { key: kp.publicKey, padding: crypto.constants.RSA_PKCS1_PSS_PADDING, saltLength: crypto.constants.RSA_PSS_SALTLEN_DIGEST }, sig)
    assert.equal(ok, true, kind)
    assert.equal(h['KALSHI-ACCESS-KEY'], KEY)
  }
})

test('live trading: apply -> owner approves by hand -> active -> stop; disconnect never resumes', async () => {
  const express = (await import('express')).default
  const { createKalshiKeysRouter } = await import('../routes/kalshiKeys.ts')
  const U = 'abcdef12-2222-4333-8444-5555555555ef'
  const KU = 'aaaaaaaa-bbbb-4ccc-8ddd-0000000000e1'
  const app = express(); app.use(express.json())
  app.use('/k', createKalshiKeysRouter(async () => ({ id: U, email: 't@example.com' }), async () => null))
  const srv = app.listen(0); const base = `http://127.0.0.1:${(srv.address() as any).port}/k`
  const call = async (method: string, p: string, body?: any) => {
    const r = await fetch(base + p, { method, headers: { Authorization: 'Bearer t', 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined })
    return { status: r.status, body: await r.json() as any }
  }
  const apply = (ratio: any, ack: any = true, ack_version: any = K.TRADING_ACK_VERSION) =>
    call('POST', '/trading/request', { ratio, ack, ack_version })
  const tfile = (kind: string) => path.join(DIR, 'trading', kind, `${U}.json`)
  try {
    assert.equal((await apply(0.25)).status, 409)                                   // no verified key yet
    K.savePendingKeyId(U, KU); K.savePem(U, rsaPem()); K.activate(U, { ...ID, id: U, email: 't@example.com' }, KU)
    let st = (await call('GET', '/status')).body
    assert.equal(st.connected, true); assert.equal(st.trading.state, 'none'); assert.equal(st.trading_enabled, false)
    for (const bad of [0.3, 2, '0.25', 0]) assert.equal((await apply(bad)).status, 400, String(bad))
    assert.equal((await apply(0.25, false)).status, 400)                            // risk box not ticked
    assert.equal((await apply(0.25, true, '2020-01-01')).status, 400)               // stale acknowledgement text
    const ok = await apply(0.25)
    assert.equal(ok.status, 200); assert.equal(ok.body.trading.state, 'requested'); assert.equal(ok.body.trading.requested_ratio, 0.25)
    assert.equal(fs.statSync(tfile('requests')).mode & 0o777, 0o600)
    const rec = JSON.parse(fs.readFileSync(tfile('requests'), 'utf8'))
    assert.deepEqual([rec.user_id, rec.email, rec.ratio, rec.ack_version], [U, 't@example.com', 0.25, K.TRADING_ACK_VERSION])
    assert.equal(fs.existsSync(tfile('approved')), false)                           // never approves itself
    assert.equal((await call('DELETE', '/trading/request')).body.trading.state, 'none')
    assert.equal((await call('POST', '/trading/stop')).status, 409)                 // nothing to stop
    // the owner approves BY HAND; until the runner reloads its list it is only "approved"
    await apply(0.5)
    fs.mkdirSync(path.join(DIR, 'trading', 'approved'), { recursive: true })
    fs.writeFileSync(tfile('approved'), JSON.stringify({ user_id: U, ratio: 0.5 }))
    assert.equal((await call('GET', '/status')).body.trading.state, 'approved')
    fs.writeFileSync(path.join(DIR, 'trading_active.json'), JSON.stringify({ user_ids: [U], pid: process.pid }))
    st = (await call('GET', '/status')).body
    assert.equal(st.trading.state, 'active'); assert.equal(st.trading.approved_ratio, 0.5); assert.equal(st.trading_enabled, true)
    assert.equal((await apply(1)).status, 409)                                      // no ratio change while active
    const stop = await call('POST', '/trading/stop')
    assert.equal(stop.status, 200); assert.equal(stop.body.trading.state, 'stopped'); assert.equal(stop.body.trading.stopped_by, 'user')
    assert.equal(fs.statSync(tfile('stopped')).mode & 0o777, 0o600)
    assert.equal((await call('GET', '/status')).body.trading_enabled, false)
    assert.equal((await apply(0.25)).body.trading.state, 'requested')               // re-applying waits for the owner:
    assert.ok(fs.existsSync(tfile('stopped')))                                      // the stop marker stays
    fs.rmSync(tfile('stopped'))                                                     // owner lifts it by hand
    assert.equal((await call('GET', '/status')).body.trading.state, 'active')
    assert.equal((await call('DELETE', '/')).status, 200)                           // disconnect:
    assert.ok(fs.existsSync(tfile('stopped')) && !fs.existsSync(tfile('requests'))) // stop kept, application gone
    const log = fs.readFileSync(path.join(DIR, 'audit.jsonl'), 'utf8')
    for (const ev of ['trading_requested', 'trading_request_cancelled', 'trading_stopped']) assert.ok(log.includes(`"${ev}"`), ev)
  } finally {
    srv.close()
    fs.rmSync(path.join(DIR, 'trading_active.json'), { force: true })
    for (const k of ['requests', 'approved', 'stopped']) fs.rmSync(tfile(k), { force: true })
    K.deactivate(U)
  }
})
