import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { usesOwnKalshi, withUserKalshiProd } from '../../src/lib/kalshiUserPolicy.ts'
import { applyUserLedger } from '../../src/crypto-markets/userLedger.ts'
import { SnapshotSchema } from '../../src/crypto-markets/types.ts'

const OWNER = { owner: true, connected: true }
const NEW_USER = { owner: false, connected: false }
const CONNECTED = { owner: false, connected: true }

test('only a connected NON-owner uses his own Kalshi account', () => {
  assert.equal(usesOwnKalshi(null), false)            // logged out / server down
  assert.equal(usesOwnKalshi(OWNER), false)            // lxu912 -> unchanged
  assert.equal(usesOwnKalshi(NEW_USER), false)         // no / failed credentials
  assert.equal(usesOwnKalshi({ owner: false, connected: false, auth_failed: true }), false)
  assert.equal(usesOwnKalshi(CONNECTED), true)
})

test('balance override: owner and unconnected get the IDENTICAL object back', () => {
  const b = { kalshi_prod_usd: 1234.5, kalshi_demo_usd: 9 }
  assert.equal(withUserKalshiProd(b, { status: OWNER, balanceUsd: 77 }), b)
  assert.equal(withUserKalshiProd(b, { status: NEW_USER, balanceUsd: 77 }), b)
  assert.equal(withUserKalshiProd(b, { status: null, balanceUsd: null }), b)
  assert.deepEqual(withUserKalshiProd(b, { status: CONNECTED, balanceUsd: 77 }), { kalshi_prod_usd: 77, kalshi_demo_usd: 9 })
  // unreadable user balance: '—', never the owner's figure
  assert.equal(withUserKalshiProd(b, { status: CONNECTED, balanceUsd: null }).kalshi_prod_usd, '—')
  assert.equal(b.kalshi_prod_usd, 1234.5)              // input never mutated
  const fmt = (v: number) => `$${v.toFixed(2)}`
  assert.equal(withUserKalshiProd(b, { status: CONNECTED, balanceUsd: 77 }, fmt).kalshi_prod_usd, '$77.00')
  assert.equal(withUserKalshiProd(b, { status: OWNER, balanceUsd: 77 }, fmt), b)   // owner still identical
})

test('crypto ledger merge: owner source is a no-op; user replaces ONLY fave prod-ledger fields', () => {
  const snap = SnapshotSchema.parse(JSON.parse(fs.readFileSync('public/data/crypto_prediction/snapshot.json', 'utf8')))
  assert.equal(applyUserLedger(snap, { source: 'owner' }), snap)   // identical reference
  assert.equal(applyUserLedger(snap, null), snap)
  const fave = snap.strategies.fave!
  const mine = { source: 'user', prod: fave.paper, orders: fave.orders.slice(0, 2),
                 positions: [], settlements: fave.settlements.slice(0, 1) }
  const out = applyUserLedger(snap, mine)
  assert.deepEqual(out.strategies.fave!.prod, mine.prod)   // zod re-materialises; compare values
  assert.equal(out.strategies.fave!.orders.length, 2)
  assert.equal(out.strategies.fave!.paper, fave.paper)          // paper book untouched
  assert.equal(out.strategies.fave!.demo, fave.demo)            // demo book untouched
  assert.equal(out.strategies.pfme, snap.strategies.pfme)        // other strategy untouched
  assert.ok(SnapshotSchema.safeParse(out).success)               // still a valid snapshot
  // the Execution view switches to the user's own stats (or empty), never the owner's
  assert.notDeepEqual(out.strategies.fave!.execution.all, fave.execution.all)
  assert.equal(out.strategies.fave!.execution.all.filled_contracts, null)
  assert.equal(out.strategies.fave!.execution.exits, fave.execution.exits)
  // connected but ledger missing/unreadable -> EMPTY own view, never the owner's numbers
  const empty = applyUserLedger(snap, { source: 'user', empty: true })
  assert.equal(empty.strategies.fave!.orders.length, 0)
  assert.equal(empty.strategies.fave!.prod!.net_pnl_usd, null)
  assert.ok(SnapshotSchema.safeParse(empty).success)
})
