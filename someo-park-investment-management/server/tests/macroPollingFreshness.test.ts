import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import test from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';
import { markDisplayStatus } from '../../src/components/macro/markPresentation.js';

const require = createRequire(import.meta.url);

/** Deterministic hook host: state preserves React's Object.is bailout, refs keep
 * identity, effects mount once, and intervals use a controlled clock. This runs
 * the real useMacroPoll implementation, including its Promise error handling. */
function mountPolling(fetch: () => Promise<any>, now: () => number) {
  const states: any[] = [], refs: any[] = [];
  let cursor = 0, refCursor = 0, mounted = false, dirty = false;
  let interval: (() => void) | undefined, cleanup: (() => void) | undefined;
  let status: string, renders = 0;
  const react = {
    useState(initial: any) {
      const i = cursor++;
      if (!mounted) states[i] = initial;
      return [states[i], (update: any) => {
        const next = typeof update === 'function' ? update(states[i]) : update;
        if (!Object.is(next, states[i])) { states[i] = next; dirty = true; }
      }];
    },
    useRef(initial: any) {
      const i = refCursor++;
      if (!mounted) refs[i] = { current: initial };
      return refs[i];
    },
    useEffect(effect: () => () => void) { if (!mounted) cleanup = effect(); },
  };
  const file = new URL('../../src/components/macro/primitives.tsx', import.meta.url);
  const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  const module = { exports: {} as any };
  vm.runInNewContext(compiled, { module, exports: module.exports,
    require: (id: string) => id === 'react' ? react : id === 'react-i18next' ? {} : require(id),
    setInterval: (callback: () => void, ms: number) => {
      assert.equal(ms, 60_000); interval = callback; return 1;
    },
    clearInterval: () => { interval = undefined; },
  }, { filename: file.pathname });
  const render = () => {
    cursor = refCursor = 0; dirty = false; renders++;
    const { data } = module.exports.useMacroPoll(fetch);
    status = markDisplayStatus(data, now());
    mounted = true;
  };
  const flush = async () => {
    for (let i = 0; i < 8; i++) { await Promise.resolve(); if (dirty) render(); }
  };
  render();
  return {
    flush, poll: async () => { interval?.(); await flush(); },
    status: () => status, renders: () => renders,
    unmount: () => cleanup?.(),
  };
}

for (const outage of ['same error', 'hung request']) {
  test(`quote expiry still rerenders during ${outage}`, async () => {
    const start = Date.parse('2026-09-10T06:00:00Z');
    let now = start, calls = 0;
    const data = { mark_status: 'marked' as const, quote_max_age_seconds: 1200,
      quote_ts: new Date(start - 1100_000).toISOString() };
    const view = mountPolling(() => {
      if (++calls === 1) return Promise.resolve(data);
      return outage === 'same error' ? Promise.reject(new Error('offline')) : new Promise(() => {});
    }, () => now);
    await view.flush();
    assert.equal(view.status(), 'marked');
    now += 60_000;
    await view.poll();
    assert.equal(view.status(), 'marked'); // 1160 seconds old, initial outage
    const before = view.renders();
    now += 60_000;
    await view.poll();
    assert.equal(view.status(), 'stale'); // 1220 seconds old, same data/error state
    assert.ok(view.renders() > before, 'clock must rerender independently of the fetch');
    view.unmount();
    await view.poll();
    assert.equal(calls, 3, 'unmount must clear the polling timer');
  });
}
