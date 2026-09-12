import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import test from 'node:test';
import vm from 'node:vm';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const componentDir = new URL('../../src/components/macro/', import.meta.url);
const NOW = Date.parse('2026-09-10T06:00:00Z');

/** Render the shipped component, stubbing only data access and translation.
 * Compile in memory to expose the private view without adding a production API.
 * Its MarkValue, tables, and formatting remain the actual production code. */
function renderPerformance(valuation: Record<string, unknown>) {
  const fixture = { unrealized_usd: 12.34, valuation, bankroll_usd: 1000, mode: 'paper',
    track: { cutover: '2026-08-11', combined: { won: 0, n_trades: 0, realized: 0, roi: 0 },
      live: { open: { n: 1, unrealized: -4.56, valuation } } } };
  class Clock extends Date { static now() { return NOW; } }
  const cache = new Map<string, any>();
  const load = (name: string): any => {
    if (cache.has(name)) return cache.get(name);
    const file = new URL(name, componentDir);
    const source = fs.readFileSync(file, 'utf8') + (name === 'MacroArtifact.tsx'
      ? '\nexport { PerformanceView };\n' : '');
    const compiled = ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
      target: ts.ScriptTarget.ES2022, esModuleInterop: true,
    } }).outputText;
    const module = { exports: {} as any };
    const localRequire = (id: string): any => {
      if (id === 'react-i18next') return { useTranslation: () => ({
        t: (key: string) => key, i18n: { language: 'en' },
      }) };
      if (id === './macroApi') return {
        getMacroJson: () => fixture, getMacroPricetrack: () => ({ track: [] }),
      };
      if (id === './markPresentation') return load('markPresentation.ts');
      if (id === './primitives') return { ...load('primitives.tsx'),
        useMacroPoll: (fetch: () => unknown) => ({ data: fetch(), loading: false, error: null }),
      };
      if (id === './MacroArtifactGrid') return { MACRO_ITEMS: [], groupOfType: () => undefined };
      if (id === '../../contexts/ArtifactContext') return { useSetArtifact: () => () => {} };
      if (id === 'recharts') return {};
      return require(id);
    };
    vm.runInNewContext(compiled, { module, exports: module.exports, require: localRequire,
      Date: Clock, setInterval, clearInterval, console }, { filename: file.pathname });
    cache.set(name, module.exports);
    return module.exports;
  };
  return renderToStaticMarkup(React.createElement(load('MacroArtifact.tsx').PerformanceView));
}

const marked = (ageSeconds: number) => ({ mark_status: 'marked', n_legs: 1, n_unmarked: 0,
  quote_ts: new Date(NOW - ageSeconds * 1000).toISOString(), quote_max_age_seconds: 1200,
  mark_ts: new Date(NOW).toISOString() });

function pnlRows(html: string) {
  return ['macro.unrealized', 'macro.trackOpen'].map(label => {
    const row = html.match(new RegExp(`<tr[^>]*>(?:(?!<tr).)*${label}(?:(?!</tr>).)*</tr>`))?.[0];
    assert.ok(row, `missing ${label} row`);
    return row;
  });
}

test('both performance PnL consumers honor the exact quote-expiry boundary', () => {
  for (const row of pnlRows(renderPerformance(marked(1200)))) {
    assert.doesNotMatch(row, /macro.markUnavailable|macro.lastRecordedValue/);
    assert.match(row, /var\(--(?:success|error)\)/);
  }
  for (const row of pnlRows(renderPerformance(marked(1200.001)))) {
    assert.match(row, /macro.markUnavailable/);
    assert.match(row, /macro.lastRecordedValue/);
    assert.match(row, /var\(--warning\)/);
    assert.doesNotMatch(row, /var\(--(?:success|error)\)/);
  }
});

test('unavailable export and legacy valuation keep carrying values distinct from live PnL', () => {
  for (const valuation of [{ ...marked(0), mark_status: 'stale' }, {}]) {
    const html = renderPerformance(valuation);
    for (const row of pnlRows(html)) {
      assert.match(row, /macro.markUnavailable/);
      assert.match(row, /macro.carryValue/);
      assert.doesNotMatch(row, /var\(--(?:success|error)\)/);
    }
    assert.match(html, /\$12\.34/);
    assert.match(html, /\$-4\.56/);
  }
});
