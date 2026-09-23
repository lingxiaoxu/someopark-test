import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';
import * as pairAnalysis from '../../shared/performancePairAnalysis.js';

const source = readFileSync(new URL('../../src/components/artifacts/StrategyPerformanceViewer.tsx', import.meta.url), 'utf8');
const locale = JSON.parse(readFileSync(new URL('../../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
const translate = (key: string, args: Record<string, unknown> = {}) => {
  const value = key.split('.').reduce((p, k) => p?.[k], locale);
  return typeof value === 'string'
    ? value.replace(/\{\{(\w+)\}\}/g, (whole, k) => args[k] === undefined ? whole : String(args[k]))
    : key;
};
const realStrategies = ['mrpt', 'mtfs', 'sr', 'aiss', 'aeus', 'bdc'];
const benchmarks = ['spy', 'smh', 'soxx', 'mags'];
const keys = [...realStrategies, 'combined', 'master', ...benchmarks];
const metrics = ['RETURN', 'SHARPE', 'MAX DD', 'WIN RATE', 'NET PnL'];
const rows = ['2026-09-17', '2026-09-18', '2026-09-21'].map((date, day) => ({
  date, ...Object.fromEntries(keys.map((key, i) => [`${key}_equity`, (i + 1) * [1000, 1100, 1050][day]])),
}));
type Row = { date: string; [key: string]: string | number | null };
type RenderOptions = {
  activeKeys?: string[];
  rows?: Row[];
  startDate?: string;
  endDate?: string;
};

/** Render the real scorecards with fixed data, without network or chart layout. */
function render(mode: 'strategies' | 'master', options: RenderOptions = {}) {
  const available = mode === 'master' ? [...realStrategies, 'master', ...benchmarks]
    : [...realStrategies.filter(k => k !== 'bdc'), 'combined'];
  const active = options.activeKeys ?? available;
  const data = options.rows ?? rows;
  const states = [mode, data, data, false, false, null, new Set(active),
    options.startDate ?? data[0].date, options.endDate ?? data[data.length - 1].date];
  const elements: Record<string, React.ReactElement> = {};
  const analyses: React.ReactElement[] = [];
  const panels: React.ReactElement[] = [];
  let used = 0;
  const react = {
    ...React,
    createElement: (type: any, props: any, ...children: any[]) => {
      const element = React.createElement(type, props, ...children);
      if (type === 'div' && props?.key && props?.style?.padding === '12px')
        elements[props.key] = element;
      if (type === 'div' && props?.['data-pair-analysis'] !== undefined) analyses.push(element);
      if (type === 'div' && props?.style?.padding === '16px') panels.push(element);
      return element;
    },
    useState: () => {
      assert.ok(used < states.length, 'update the explicit fixture mapping if hooks change');
      return [states[used++], () => {}];
    },
    useMemo: (fn: () => unknown) => fn(),
    useRef: () => ({ current: false }),
    useEffect: () => {},
  };
  const empty = () => null;
  const childrenOnly = ({ children }: { children?: React.ReactNode }) =>
    React.createElement(React.Fragment, null, children);
  const graph = Object.fromEntries(['LineChart', 'Line', 'XAxis', 'YAxis', 'CartesianGrid',
    'Tooltip', 'ResponsiveContainer', 'AreaChart', 'Area', 'ReferenceLine'].map(k => [k, childrenOnly]));
  const requireMock = (name: string) => {
    if (name === 'react') return { __esModule: true, ...react, default: react };
    if (name === 'react-i18next') return { useTranslation: () => ({ t: translate }) };
    if (name === 'recharts') return graph;
    if (name === './SizedChart') return { __esModule: true, default: childrenOnly };
    if (['../LoadingState', '../ErrorState'].includes(name))
      return { __esModule: true, default: empty };
    if (name === '../../lib/api') return { API_BASE: '', apiHeaders: () => ({}) };
    if (name === '../../../shared/performancePairAnalysis'
        || name === '../../../shared/performancePairAnalysis.js') return pairAnalysis;
    throw new Error(`unexpected component dependency ${name}`);
  };
  const compiled = ts.transpileModule(source, { compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
    jsx: ts.JsxEmit.React, esModuleInterop: true,
  } }).outputText;
  const mod = { exports: {} as { default?: React.ComponentType } };
  runInNewContext(compiled, { module: mod, exports: mod.exports, require: requireMock,
    fetch: () => { throw new Error('render-only test must not fetch'); } },
  { filename: 'StrategyPerformanceViewer.acceptance.cjs', timeout: 5000 });
  assert.ok(mod.exports.default);
  const html = renderToStaticMarkup(React.createElement(mod.exports.default));
  assert.equal(used, states.length);
  const cards = Object.fromEntries(Object.entries(elements).map(([key, element]) =>
    [key, renderToStaticMarkup(element)]));
  assert.deepEqual(Object.keys(cards).sort(), active.filter(k => available.includes(k)).sort(),
    'all selected visible scorecards are actually rendered');
  return { html, cards, analyses: analyses.map(element => renderToStaticMarkup(element)),
    panels: panels.map(element => renderToStaticMarkup(element)) };
}

function cardMetrics(html: string) {
  return metrics.filter(metric => html.includes(`>${metric}</div>`));
}

function assertFullStats(cards: Record<string, string>, selected: string[]) {
  for (const key of selected) {
    assert.deepEqual(cardMetrics(cards[key]), metrics, `${key} retains all five metrics`);
    const expectedPnL = (keys.indexOf(key) + 1) * 50;
    assert.ok(cards[key].includes(`>$${expectedPnL}</div>`), `${key} retains its original net PnL amount`);
    assert.ok(cards[key].includes('>+5.00%</div>'), `${key} return calculation is unchanged`);
    assert.ok(cards[key].includes('>-4.55%</div>'), `${key} drawdown calculation is unchanged`);
    assert.ok(cards[key].includes('>50%</div>'), `${key} win-rate calculation is unchanged`);
  }
}

test('master benchmark scorecards expose only return and Sharpe while six strategies and master retain all statistics', () => {
  const { html, cards } = render('master');
  for (const key of benchmarks) {
    assert.deepEqual(cardMetrics(cards[key]), ['RETURN', 'SHARPE'], `${key} is a two-metric benchmark card`);
    assert.ok(!cards[key].includes('$'), `${key} no longer displays notional dollar PnL`);
    assert.ok(cards[key].includes('>+5.00%</div>'), `${key} return still uses the same benchmark data`);
  }
  assertFullStats(cards, [...realStrategies, 'master']);
  assert.ok(html.includes('3 Trading Days · Master AI Portfolio · MRPT + MTFS + SSRS + AISS + AEUS + PC BDC'));
  assert.ok(!html.includes('Equal 1/3'), 'footer no longer claims an obsolete allocation split');
});

test('strategy mode retains all five strategies and combined statistics with the five-strategy label', () => {
  const { html, cards } = render('strategies');
  assertFullStats(cards, [...realStrategies.filter(k => k !== 'bdc'), 'combined']);
  assert.ok(cards.combined.includes('COMBINED 5 AI ENABLED SYSTEMATIC STRATEGIES'));
  assert.ok(!html.includes('COMBINED 4 AI ENABLED SYSTEMATIC STRATEGIES'));
  assert.ok(html.includes('3 Trading Days · MRPT + MTFS + SSRS + AISS + AEUS Strategies'));
});

test('pair analysis appears only for exactly two lines visible in the current mode', () => {
  for (const activeKeys of [['aiss'], ['aiss', 'aeus', 'spy'], []]) {
    const result = render('master', { activeKeys });
    assert.equal(result.analyses.length, 0, `${activeKeys.length} selected lines hide analysis`);
  }
  const pair = render('master', { activeKeys: ['aiss', 'spy'] });
  assert.equal(pair.analyses.length, 1, 'a real strategy and benchmark form an analyzable pair');
  assert.match(pair.analyses[0], /AISS/);
  assert.match(pair.analyses[0], /SPY/);
  for (const css of ['font-size:8px', 'font-family:var(--font-mono)',
    'background:rgba(255,255,255,0.94)', 'border:1px solid #111'])
    assert.ok(pair.analyses[0].includes(css), `analysis retains compact tooltip styling: ${css}`);
  assert.equal(pair.panels.length, 3);
  assert.ok(pair.panels[0].includes('data-pair-analysis'), 'analysis belongs to the equity curve');
  for (const panel of pair.panels.slice(1))
    assert.ok(!panel.includes('data-pair-analysis'), 'drawdown and daily PnL keep their previous presentation');
  const hiddenBenchmark = render('strategies', { activeKeys: ['mrpt', 'mtfs', 'spy'] });
  assert.equal(hiddenBenchmark.analyses.length, 1, 'a benchmark hidden by strategy mode is not counted');
  const oneVisible = render('strategies', { activeKeys: ['aiss', 'spy'] });
  assert.equal(oneVisible.analyses.length, 0, 'switching modes cannot analyze a line that is no longer shown');
});

function pairedRows(): Row[] {
  // Daily A returns are +10%, -10%, +20%, -20%; B is half of A on every interval.
  // Sample covariance is 166 2/3 %², correlation 1, and beta(A/B) 2.
  const a = [100, 110, 99, 118.8, 95.04];
  const b = [200, 210, 199.5, 219.45, 197.505];
  return ['2026-09-16', '2026-09-17', '2026-09-18', '2026-09-21', '2026-09-22'].map((date, i) => ({
    ...rows[0], date, aiss_equity: a[i], spy_equity: b[i],
  }));
}

function analysisText(result: ReturnType<typeof render>) {
  assert.equal(result.analyses.length, 1);
  return result.analyses[0].replace(/<[^>]*>/g, '');
}

test('pair metrics use daily returns, percent-squared covariance, and the current selected date window', () => {
  const options = { activeKeys: ['aiss', 'spy'], rows: pairedRows() };
  const full = analysisText(render('master', options));
  assert.ok(full.includes('相关性 ρ1.00'));
  assert.ok(full.includes('协方差（%²）167'));
  assert.ok(full.includes('β AISS/SPY2.00'));
  assert.ok(full.includes('同期净值收益 · n=4'));
  assert.ok(full.includes('2026-09-16 → 2026-09-22'));
  assert.ok(full.includes('样本较少，仅供参考'));
  const shorter = analysisText(render('master', { ...options, startDate: '2026-09-17' }));
  assert.ok(shorter.includes('协方差（%²）217'), 'changing date range recalculates covariance');
  assert.ok(shorter.includes('同期净值收益 · n=3'));
  assert.ok(shorter.includes('2026-09-17 → 2026-09-22'));
  assert.ok(!shorter.includes('2026-09-16'), 'earlier record is not used as an out-of-window return anchor');
  const changedPair = analysisText(render('master', { ...options, activeKeys: ['aiss', 'master'] }));
  assert.ok(changedPair.includes('β AISS/MASTER'), 'changing selected keys updates pair identity');
  assert.ok(changedPair.includes('组合包含该策略'), 'strategy-versus-aggregate comparison explains overlap');
});

test('pair analysis reports insufficient and constant samples without inventing finite correlations', () => {
  const small = analysisText(render('master', {
    activeKeys: ['aiss', 'spy'], rows: pairedRows(), endDate: '2026-09-17',
  }));
  assert.ok(small.includes('有效样本不足 3 个'));
  assert.ok(small.includes('同期净值收益 · n=1'));
  assert.ok(small.includes('相关性 ρ—'));
  const flat = pairedRows().map(row => ({ ...row, aiss_equity: 100, spy_equity: 200 }));
  const constant = analysisText(render('master', { activeKeys: ['aiss', 'spy'], rows: flat }));
  assert.ok(constant.includes('收益无波动'));
  assert.ok(constant.includes('相关性 ρ—'));
  assert.ok(constant.includes('β AISS/SPY—'));
  assert.ok(constant.includes('协方差（%²）0'));
  assert.doesNotMatch(small + constant, /NaN|Infinity/);
});
