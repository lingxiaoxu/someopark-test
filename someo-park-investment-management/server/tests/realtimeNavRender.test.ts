import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';
import * as shared from '../../shared/realtimeNav.js';
import {
  frozenMirror, intradayLatest, intradayOfficial,
  midnightLatest, midnightOfficial, midnightPrevClose,
} from './fixtures/realtimeNavGolden.js';

const REFERENCE_COMMIT = 'f2fe5631b04c652c2923c22f09c4cfa3b65df2ad';
const REFERENCE_PATH = 'someo-park-investment-management/src/components/artifacts/RealtimeNavViewer.tsx';
const currentSource = readFileSync(new URL('../../src/components/artifacts/RealtimeNavViewer.tsx', import.meta.url), 'utf8');
const locale = JSON.parse(readFileSync(new URL('../../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
type Input = Parameters<typeof shared.buildRealtimeNavPanel>[0];

function baseInput(): Input {
  return structuredClone({ latest: midnightLatest, official: midnightOfficial,
    mirror: frozenMirror, prevClose: midnightPrevClose, streamRows: [],
    reconcile: { date: '2026-09-08', verdict: 'ok', age_bdays: 1, stale: false },
    nowMs: Date.parse('2026-09-09T05:54:55Z') });
}

const translate = (key: string, args: Record<string, unknown> = {}) => {
  const value = key.split('.').reduce((p, k) => p?.[k], locale);
  return typeof value === 'string'
    ? value.replace(/\{\{(\w+)\}\}/g, (whole, k) => args[k] === undefined ? whole : String(args[k]))
    : key;
};

/**
 * Real React rendering with only state/effect injection. No polling, browser,
 * server, quotes or LLM calls. An empty chart stream deliberately excludes
 * Recharts rendering; all headline, quality, card and expanded holding DOM is
 * still produced by the real old/new component source.
 */
function render(source: string, input: Input): string {
  const state = [
    input.latest, input.streamRows ?? [], { date: '20260909', rows: [], isToday: true },
    input.reconcile, input.prevClose, input.official, input.mirror,
    '1m', new Set(input.latest.nodes.map(n => n.node_id)),
    new Set(['MRPT', 'MTFS', 'SSRS', 'AISS', 'AEUS', 'BDC', 'PORTFOLIO']),
    input.latest.structure_hash, false, null, false,
  ];
  let used = 0;
  const react = {
    ...React,
    useState: () => {
      assert.ok(used < state.length, 'unexpected state hook added: update the explicit fixture mapping');
      return [state[used++], () => {}];
    },
    useMemo: (fn: () => unknown) => fn(),
    useCallback: (fn: unknown) => fn,
    useEffect: () => {},
  };
  const reactModule = { __esModule: true, ...react, default: react };
  const empty = () => null;
  const graph = Object.fromEntries(['LineChart', 'Line', 'XAxis', 'YAxis', 'CartesianGrid',
    'Tooltip', 'ResponsiveContainer', 'ReferenceLine'].map(k => [k, empty]));
  const requireMock = (name: string) => {
    if (name === 'react') return reactModule;
    if (name === 'react-i18next') return { useTranslation: () => ({ t: translate }) };
    if (name === 'recharts') return graph;
    if (name === '../LoadingState' || name === '../ErrorState') return { __esModule: true, default: empty };
    if (name === '../../lib/api') return { API_BASE: '', apiHeaders: () => ({}) };
    if (name === '../../../shared/realtimeNav' || name === '../../../shared/realtimeNav.js') return shared;
    throw new Error(`unexpected component dependency ${name}`);
  };
  const compiled = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
    jsx: ts.JsxEmit.React, esModuleInterop: true,
  } }).outputText;
  const mod = { exports: {} as { default?: React.ComponentType } };
  class FixedDate extends Date { static now() { return input.nowMs!; } }
  runInNewContext(compiled, { module: mod, exports: mod.exports, require: requireMock,
    Date: FixedDate, fetch: () => { throw new Error('render-only test must not fetch'); } },
  { filename: 'RealtimeNavViewer.acceptance.cjs', timeout: 5000 });
  assert.ok(mod.exports.default);
  const html = renderToStaticMarkup(React.createElement(mod.exports.default));
  assert.equal(used, state.length);
  return html;
}

test('pre-extraction and shared-helper panels render identical non-chart HTML', async t => {
  let oldSource: string;
  try {
    oldSource = execFileSync('git', ['show', `${REFERENCE_COMMIT}:${REFERENCE_PATH}`], {
      cwd: fileURLToPath(new URL('../../', import.meta.url)), encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'], maxBuffer: 1024 * 1024,
    });
  } catch {
    // Shallow/no-Git distribution: the independent fixed numeric golden tests
    // remain mandatory and have no dependency on repository history.
    t.skip(`pre-extraction commit ${REFERENCE_COMMIT} unavailable in this checkout`);
    return;
  }
  const cases: [string, Input][] = [['actual midnight capture', baseInput()]];
  cases.push(['actual intraday capture', { ...baseInput(), latest: structuredClone(intradayLatest),
    official: structuredClone(intradayOfficial), nowMs: Date.parse('2026-09-09T18:30:17Z') }]);

  const holdings = baseInput();
  const aiss = holdings.latest.nodes.find(n => n.display_name === 'AISS')!;
  aiss.holdings = [{ id: 'LONG', name: 'LONG', shares: 5, value: 50 },
    { id: 'SHORT', name: 'SHORT', shares: -7, value: -70 }];
  holdings.mirror!.scalars.aiss = 0.5;
  for (const [cohort, m] of [['L', 0], ['S', 0.5], ['F', 1]] as const) {
    const name = `${cohort}/PAIR`;
    holdings.latest.nodes.push({ node_id: name, display_name: name, kind: 'pair',
      value: 50, parent_id: 'SPSTJZ54GCF',
      holdings: [{ id: `${cohort}-L`, name: `${cohort}-L`, shares: 5, value: 50 },
        { id: `${cohort}-S`, name: `${cohort}-S`, shares: -5, value: -50 }] });
    (holdings.mirror!.cohorts!.mtfs ||= {})[name] = { cohort, m };
  }
  cases.push(['expanded positive/negative shares and L/S/F cohorts', holdings]);

  const missing = structuredClone(holdings);
  delete missing.official!.mtfs;
  delete missing.mirror!.scalars.aiss;
  delete missing.mirror!.capital_base!.mrpt;
  cases.push(['mixed missing official/C/k ledger fallbacks', missing]);

  const failed = baseInput();
  failed.nowMs = Date.parse(failed.latest.ts) + 601000;
  failed.latest.stale = true; failed.latest.missing = ['X'];
  failed.latest.rebuild_error = 'inventory/account mismatch';
  failed.latest.rebuild_error_age_s = 600;
  failed.reconcile = { date: '2026-09-04', verdict: 'breach', stale: true, age_bdays: 2 };
  cases.push(['dead heartbeat, stale breach and structure failure', failed]);

  const emptyNodes = baseInput(); emptyNodes.latest.nodes = [];
  emptyNodes.prevClose = null; emptyNodes.mirror = null;
  cases.push(['no nodes or rolloff', emptyNodes]);
  for (const [name, input] of cases) {
    await t.test(name, () => {
      const before = render(oldSource, input), after = render(currentSource, input);
      assert.equal(after, before);
    });
  }
});
