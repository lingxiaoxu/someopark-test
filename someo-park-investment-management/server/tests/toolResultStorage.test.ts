import assert from 'node:assert/strict'
import { mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { createToolResultStore, DEFAULT_MAX_RESULT_SIZE_CHARS, generatePreview, getPersistenceThreshold } from '../utils/toolResultStorage.js'

async function fixture(t: test.TestContext) {
  const rootDir = await mkdtemp(path.join(os.tmpdir(), 'someo-storage-test-'))
  t.after(() => rm(rootDir, { recursive: true, force: true }))
  return { rootDir, store: createToolResultStore({ rootDir }) }
}

test('CC limits: 50k default/cap, smaller declarations, explicit paged-reader opt-out', () => {
  assert.equal(getPersistenceThreshold(), 50_000)
  assert.equal(getPersistenceThreshold(100_000), 50_000)
  assert.equal(getPersistenceThreshold(1000), 1000)
  assert.equal(getPersistenceThreshold(Infinity), Infinity)
  for (const value of [NaN, -1, 0, -Infinity]) assert.equal(getPersistenceThreshold(value), 50_000)
})

test('boundary result stays complete and creates no files', async t => {
  const { rootDir, store } = await fixture(t)
  const content = 'x'.repeat(DEFAULT_MAX_RESULT_SIZE_CHARS)
  assert.equal(await store.prepare(content, 'id', 'search'), content)
  assert.equal(await store.prepare('', 'empty', 'search'), '')
  assert.deepEqual(await readdir(rootDir), [])
})

test('full JSON, including late summary, survives persistence and paged read byte-for-byte', async t => {
  const { rootDir, store } = await fixture(t)
  const original = JSON.stringify({ records: Array.from({ length: 1500 }, (_, i) => ({ i, text: '财报内容与正常数据🌏'.repeat(4) })), summary: 'MEANINGFUL_TAIL 财报完整总结' }, null, 2)
  const notice = await store.prepare(original, 'tool-1', 'fixture_tool', 100_000)
  assert.match(notice, /<persisted-output>/)
  assert.match(notice, /read_tool_result/)
  assert.ok(notice.length < 4000)
  assert.doesNotMatch(notice, /MEANINGFUL_TAIL/)
  let offset = 0
  let reconstructed = ''
  for (;;) {
    const page = await store.readTool.execute({ result_id: 'tool-1', offset, limit: 7999 }) as any
    reconstructed += page.content
    assert.equal(page.total_chars, original.length)
    assert.equal(page.offset, offset)
    assert.equal(page.returned_chars, page.content.length)
    if (page.done) { assert.equal(page.next_offset, null); break }
    assert.ok(page.next_offset > offset)
    offset = page.next_offset
  }
  assert.equal(reconstructed, original)
  assert.equal(JSON.parse(reconstructed).summary, 'MEANINGFUL_TAIL 财报完整总结')
  const [run] = await readdir(rootDir)
  const runDir = path.join(rootDir, run)
  const [name] = await readdir(runDir)
  assert.equal(await readFile(path.join(runDir, name), 'utf8'), original)
  assert.equal((await stat(runDir)).mode & 0o777, 0o700)
  assert.equal((await stat(path.join(runDir, name))).mode & 0o777, 0o600)
  assert.equal(await store.prepare(original, 'tool-1', 'fixture_tool'), notice, 'repeat never overwrites stored results')
  assert.deepEqual(await readdir(runDir), [name])
})

test('independent stores cannot read each other, and reader rejects invalid offsets/paths', async t => {
  const { rootDir, store } = await fixture(t)
  const other = createToolResultStore({ rootDir })
  await store.prepare('a'.repeat(50_001), '../tool-untrusted', 'tool')
  await assert.rejects(other.readTool.execute({ result_id: '../tool-untrusted' }), /not found/)
  await assert.rejects(store.readTool.execute({ result_id: '../../etc/passwd' }), /not found/)
  for (const offset of [-1, 0.5, '0', 50_002, Infinity]) {
    await assert.rejects(store.readTool.execute({ result_id: '../tool-untrusted', offset }), /offset/)
  }
  for (const limit of [0, -1, 12_001, NaN, '1']) {
    await assert.rejects(store.readTool.execute({ result_id: '../tool-untrusted', limit }), /limit/)
  }
  const end = await store.readTool.execute({ result_id: '../tool-untrusted', offset: 50_001 }) as any
  assert.equal(end.done, true)
  assert.equal(end.content, '')
})

test('disk failure preserves complete output rather than lying about an available file', async t => {
  const { rootDir } = await fixture(t)
  const file = path.join(rootDir, 'not-a-directory')
  await writeFile(file, 'occupied')
  const failures: unknown[] = []
  const store = createToolResultStore({ rootDir: file, onPersistenceError: e => failures.push(e) })
  const original = 'x'.repeat(50_000) + 'IMPORTANT_TAIL'
  assert.equal(await store.prepare(original, 'a', 'tool'), original)
  assert.equal(failures.length, 1)
  await assert.rejects(store.readTool.execute({ result_id: 'a' }), /not found/)
})

test('changed files and conflicting IDs cannot return incorrect old results', async t => {
  const { rootDir } = await fixture(t)
  const failures: unknown[] = []
  const store = createToolResultStore({ rootDir, onPersistenceError: e => failures.push(e) })
  const original = 'A'.repeat(50_001)
  const changed = 'B'.repeat(50_001)
  await store.prepare(original, 'same-id', 'tool')
  assert.equal(await store.prepare(changed, 'same-id', 'tool'), changed)
  assert.equal(failures.length, 1)
  const [run] = await readdir(rootDir)
  const [file] = await readdir(path.join(rootDir, run))
  await writeFile(path.join(rootDir, run, file), changed)
  await assert.rejects(store.readTool.execute({ result_id: 'same-id' }), /integrity/)
})

test('preview and paged reads preserve Unicode; image payloads keep existing behavior', async t => {
  const { rootDir, store } = await fixture(t)
  assert.equal(generatePreview('x'.repeat(1999) + '🌏tail'), 'x'.repeat(1999))
  const original = '🌏'.repeat(25_001)
  await store.prepare(original, 'emoji', 'tool')
  const page = await store.readTool.execute({ result_id: 'emoji', limit: 1 }) as any
  assert.equal(page.content, '🌏')
  assert.equal(page.next_offset, 2)
  await assert.rejects(store.readTool.execute({ result_id: 'emoji', offset: 1 }), /offset splits a Unicode/)
  const image = 'data:image/png;base64,' + 'a'.repeat(60_000)
  assert.equal(await store.prepare(image, 'image', 'tool'), image)
  const longReader = 'r'.repeat(60_000)
  assert.equal(await store.prepare(longReader, 'reader', 'read_tool_result', Infinity), longReader)
  assert.equal((await readdir(rootDir)).length, 1)
})
