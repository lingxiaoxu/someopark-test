import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { resolveAllowed, isDeniedPath, ALLOWED_ROOT } from '../tools/pathGuard.ts'
import { readFileTool } from '../tools/fileTool.ts'
import { listFilesTool } from '../tools/listFilesTool.ts'
import { searchContentTool } from '../tools/searchContentTool.ts'

const rel = (p: string) => path.join(ALLOWED_ROOT, p)

test('secret files and private dirs are refused, ordinary files allowed', () => {
  for (const p of ['someo-park-investment-management/.env', 'prediction_market/.env', 'crypto_trading/.env',
                   'x/.env.local', 'a/key.pem', 'a/b.key', 'a/id_rsa', 'a/service_secret.json', 'a/credentials.json',
                   'crypto_trading/trading_signals/prod_users/u.json', '.git/config']) {
    assert.equal(isDeniedPath(rel(p)), true, p)
    assert.throws(() => resolveAllowed(p), /not allowed/, p)
  }
  assert.doesNotThrow(() => resolveAllowed('someo-park-investment-management/package.json'))
})

test('sibling-prefix and symlink escapes are refused', () => {
  assert.throws(() => resolveAllowed(ALLOWED_ROOT + '-evil/x.txt'), /not allowed/)
  const link = rel(`someo-park-investment-management/server/tests/.tmp-escape-${process.pid}`)
  fs.symlinkSync(os.homedir(), link)
  try { assert.throws(() => resolveAllowed(link + '/anything'), /not allowed/) }
  finally { fs.unlinkSync(link) }
})

test('the real agent tools cannot read, list or grep credential files', async () => {
  await assert.rejects(() => readFileTool.execute({ file_path: 'someo-park-investment-management/.env' } as any), /not allowed/)
  await assert.rejects(() => readFileTool.execute({ file_path: 'prediction_market/.env' } as any), /not allowed/)
  const listed = await listFilesTool.execute({ pattern: '**/.env*', directory: 'someo-park-investment-management' } as any)
  assert.deepEqual(listed, [])
  const listed2 = await listFilesTool.execute({ pattern: '**/*.pem' } as any)
  assert.deepEqual(listed2, [])
  // ripgrep may be absent on this machine (the tool then errors - no leak either way);
  // when present, a caller glob must never re-include a credential file.
  for (const glob of [undefined, '.env', '**/.env', '*']) {
    let text = ''
    try {
      text = JSON.stringify(await searchContentTool.execute({ pattern: 'SUPABASE_SECRET_KEY=',
        path: 'someo-park-investment-management', glob, output_mode: 'files_with_matches' } as any))
    } catch (e: any) { text = '' }
    assert.doesNotMatch(text, /\.env/, `glob=${glob}`)
  }
  await assert.rejects(() => searchContentTool.execute({ pattern: 'KEY', path: 'prediction_market/.env' } as any), /not allowed/)
})
