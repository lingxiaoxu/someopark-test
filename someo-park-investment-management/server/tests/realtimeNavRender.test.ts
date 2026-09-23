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
type GraphCalls = Record<string, Record<string, any>[]>;
type RenderOptions = {
  chartStream?: { date: string; rows: Record<string, unknown>[]; isToday: boolean };
  activeLines?: string[];
  graphCalls?: GraphCalls;
};

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
 * server, quotes or LLM calls. Empty chart streams exclude Recharts from the
 * whole-DOM comparison; chart tests instead capture the real data/axis props.
 * Headline, quality, card and holding DOM always use the real component source.
 */
function render(source: string, input: Input, options: RenderOptions = {}): string {
  const state = [
    input.latest, input.streamRows ?? [], options.chartStream ?? { date: '20260909', rows: [], isToday: true },
    input.reconcile, input.prevClose, input.official, input.mirror,
    '1m', new Set(input.latest.nodes.map(n => n.node_id)),
    new Set(options.activeLines ?? ['MRPT', 'MTFS', 'SSRS', 'AISS', 'AEUS', 'BDC', 'PORTFOLIO']),
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
    'Tooltip', 'ResponsiveContainer', 'ReferenceLine'].map(k => [k, options.graphCalls
      ? (props: Record<string, any>) => {
        (options.graphCalls![k] ||= []).push(props);
        return React.createElement(React.Fragment, null, props.children);
      } : empty]));
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

/**
 * The intentional differences from the captured reference UI are removal
 * of three legacy mirror decorations and the account rolloff tooltip. Locate
 * them by translation calls, not line numbers or broad HTML/text normalization.
 * The shared locale gives the retained account line its short QC label.
 * All other source bytes remain identical. The separately authorized quality
 * redesign is isolated below; retained amounts, tooltips, share projections
 * and holding expansion controls remain covered by whole-DOM parity.
 */
function withoutLegacyMirrorDecorations(source: string): string {
  const file = ts.createSourceFile('reference.tsx', source, ts.ScriptTarget.Latest,
    true, ts.ScriptKind.TSX);
  const removals: { start: number; end: number; kind: string }[] = [];
  const translationKeys = (node: ts.Node): Set<string> => {
    const keys = new Set<string>();
    const visit = (child: ts.Node) => {
      if (ts.isCallExpression(child) && ts.isIdentifier(child.expression)
          && child.expression.text === 't') {
        const key = child.arguments[0];
        if (key && ts.isStringLiteralLike(key)) keys.add(key.text);
        else if (key && ts.isTemplateExpression(key))
          keys.add(key.head.text + key.templateSpans.map(s => '${}' + s.literal.text).join(''));
      }
      ts.forEachChild(child, visit);
    };
    visit(node);
    return keys;
  };
  const visit = (node: ts.Node) => {
    if (ts.isJsxAttribute(node) && node.name.getText(file) === 'title'
        && translationKeys(node).has('realtimeNav.rolloffTitle')) {
      removals.push({ start: node.getStart(file), end: node.end, kind: 'account rolloff tooltip' });
      return;
    }
    if (ts.isJsxExpression(node) && node.expression
        && ts.isBinaryExpression(node.expression)
        && node.expression.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken) {
      let element: ts.Expression = node.expression.right;
      while (ts.isParenthesizedExpression(element)) element = element.expression;
      if (!ts.isJsxElement(element)) { ts.forEachChild(node, visit); return; }
      const tag = element.openingElement.tagName.getText(file);
      const keys = translationKeys(element);
      const kind = tag === 'div' && keys.has('realtimeNav.wfKTitle') ? 'waterfall K/L/S/F'
        : tag === 'span' && keys.has('realtimeNav.qcScaledShort') ? 'scaled holding badge'
        : tag === 'span' && keys.has('realtimeNav.cohort${}Short') ? 'pair cohort badge'
        : null;
      if (kind) {
        removals.push({ start: node.getStart(file), end: node.end, kind });
        return; // Do not select any nested expression inside a removed block.
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  assert.deepEqual(removals.map(r => r.kind).sort(),
    ['account rolloff tooltip', 'pair cohort badge', 'scaled holding badge', 'waterfall K/L/S/F'],
    'reference adjustment must remove exactly the authorized JSX decorations and account tooltip');
  let adjusted = source;
  for (const range of removals.sort((a, b) => b.start - a.start))
    adjusted = adjusted.slice(0, range.start) + adjusted.slice(range.end);
  return adjusted;
}

/** Exclude only the expressly redesigned quality section from whole-DOM parity.
 * Old QC text and the old standalone account row have distinct AST anchors;
 * the new disclosure has a unique data attribute. Amounts, cards, holdings,
 * market/reconcile badges and all other tooltips remain byte-for-byte covered.
 */
function withoutQualitySection(source: string, current: boolean): string {
  const file = ts.createSourceFile('quality.tsx', source, ts.ScriptTarget.Latest,
    true, ts.ScriptKind.TSX);
  const ranges: { start: number; end: number; kind: string }[] = [];
  const visit = (node: ts.Node) => {
    if (ts.isJsxElement(node)) {
      const attrs = node.openingElement.attributes.properties;
      const isCurrent = current && node.openingElement.tagName.getText(file) === 'details'
        && attrs.some(a => ts.isJsxAttribute(a) && a.name.getText(file) === 'data-nav-quality');
      const isOld = !current && node.openingElement.tagName.getText(file) === 'div'
        && attrs.some(a => ts.isJsxAttribute(a) && a.name.getText(file) === 'title'
          && a.getText(file).includes("t('realtimeNav.qcTitle')"));
      if (isCurrent || isOld) {
        ranges.push({ start: node.getStart(file), end: node.end, kind: 'quality' });
        return;
      }
    }
    if (!current && ts.isJsxExpression(node) && node.expression
        && ts.isBinaryExpression(node.expression)
        && node.expression.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
        && node.expression.left.getText(file) === 'mirror?.rolloff'
        && node.expression.right.getText(file).includes("t('realtimeNav.rolloffDone'")) {
      ranges.push({ start: node.getStart(file), end: node.end, kind: 'account' });
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  assert.deepEqual(ranges.map(r => r.kind).sort(), current ? ['quality'] : ['account', 'quality'],
    'exclude precisely the authorized quality disclosure and previous standalone QC row');
  for (const range of ranges.sort((a, b) => b.start - a.start))
    source = source.slice(0, range.start) + source.slice(range.end);
  return source;
}

function qualityDisclosure(html: string) {
  const matches = [...html.matchAll(/<details\b([^>]*)data-nav-quality[^>]*>([\s\S]*?)<\/details>/g)];
  assert.equal(matches.length, 1, 'one quality disclosure is rendered');
  const [full, attributes, body] = matches[0];
  const summary = /^<summary\b([^>]*)>([\s\S]*?)<\/summary>/.exec(body);
  assert.ok(summary, 'native summary is the first child so mouse and keyboard can toggle details');
  assert.doesNotMatch(full.slice(0, full.indexOf('>')), /\bopen(?:\s|=|$)/,
    'quality details start collapsed');
  return { full, attributes, summary: summary[0], summaryText: summary[2].replace(/<[^>]*>/g, ''),
    detail: body.slice(summary[0].length), body };
}

function allStrategyHoldings(): Input {
  const input = baseInput();
  for (const strategy of input.latest.nodes.filter(n => n.kind === 'strategy')) {
    const name = strategy.display_name;
    if (shared.ADDITIVE.has(name)) {
      const key = shared.OFFICIAL_KEY[name];
      input.mirror!.cohorts![key] = {};
      for (const [cohort, m] of [['L', 0], ['S', 0.5], ['F', 1]] as const) {
        const pair = `${name}-${cohort}/PAIR`;
        input.latest.nodes.push({ node_id: pair, display_name: pair, kind: 'pair',
          value: 50, parent_id: strategy.node_id,
          holdings: [{ id: `${pair}-L`, name: `${pair}-LONG`, shares: 9, value: 90 },
            { id: `${pair}-S`, name: `${pair}-SHORT`, shares: -5, value: -50 }] });
        input.mirror!.cohorts![key][pair] = { cohort, m };
      }
    } else {
      const holdings = [{ id: `${name}-LONG`, name: `${name}-LONG`, shares: 9, value: 90 },
        { id: `${name}-SHORT`, name: `${name}-SHORT`, shares: -5, value: -50 }];
      if (name === 'AISS' || name === 'AEUS')
        input.latest.nodes.push({ node_id: `${name}-subsector`, display_name: `${name}-subsector`,
          kind: 'subsector', value: 40, parent_id: strategy.node_id, holdings });
      else strategy.holdings = holdings; // SSRS/BDC use the direct holding path.
    }
  }
  return input;
}

function assertLegacyDecorationsAbsent(html: string) {
  // Only visible labels are removed. Kept QC share tooltips may still explain
  // a cohort; the account-level row retains only its short QC label.
  const text = html.replace(/<[^>]*>/g, '');
  for (const key of ['wfK', 'cohortLShort', 'cohortSShort', 'cohortFShort'])
    assert.ok(!text.includes(translate(`realtimeNav.${key}`)), `legacy visible label remains: ${key}`);
  assert.doesNotMatch(text, /×\d+(?:\.\d+)? 全额/);
}

test('all six strategy holding paths hide legacy labels and retain financial detail', () => {
  const input = allStrategyHoldings();
  const html = render(currentSource, input);
  assertLegacyDecorationsAbsent(html);
  for (const strategy of ['MRPT', 'MTFS', 'SSRS', 'AISS', 'AEUS', 'BDC'])
    assert.ok(html.includes(`${strategy}-`), `${strategy} expanded holdings are exercised`);
  for (const key of ['wfLong', 'wfShort', 'wfRestricted', 'wfFreeCash', 'wfMarginLoan',
    'wfLedgerEq', 'wfCapBase', 'wfNet', 'wfGross'])
    assert.ok(html.includes(translate(`realtimeNav.${key}`)), `retained financial row missing: ${key}`);
  assert.ok(html.includes('→ QC'), 'numeric QC share projection remains visible');
  const quality = qualityDisclosure(html);
  assert.match(quality.detail, /✓\s*QC对账/, 'short QC label remains inside expanded details');
  assert.ok(!quality.summaryText.includes('QC对账'), 'collapsed summary omits the account label');
  assert.doesNotMatch(html, /legacy\/S|K 定格|退场日实测/,
    'legacy account explanation must not remain in visible text or tooltips');
  assert.ok(!html.includes(frozenMirror.rolloff!.measured_on), 'frozen measurement date remains backend-only');
  assert.ok(!html.includes(Math.abs(frozenMirror.rolloff!.k_equity).toLocaleString('en-US', {
    minimumFractionDigits: 0, maximumFractionDigits: 0,
  })), 'frozen account K amount remains backend-only');
});

test('quality details default closed, keep check order, and put QC reconciliation last', () => {
  const html = render(currentSource, baseInput());
  const quality = qualityDisclosure(html);
  assert.match(quality.summaryText, /状态正常/);
  assert.match(quality.summaryText, /详情/);
  assert.doesNotMatch(quality.summaryText, /Quality checks|双引擎|心跳|持仓级|QC对账/);
  const labels = ['qcDual', 'qcHeartbeat', 'qcFresh', 'qcQuotes', 'qcRecon', 'qcAnchor', 'qcStruct']
    .map(key => translate(`realtimeNav.${key}`));
  const detailText = quality.detail.replace(/<[^>]*>/g, '');
  let previous = -1;
  for (const label of [...labels, 'QC对账']) {
    const index = detailText.indexOf(label);
    assert.ok(index > previous, `${label} remains in the original check order with QC last`);
    previous = index;
  }
  assert.equal((quality.detail.match(/QC对账/g) ?? []).length, 1, 'QC label appears once');
  const outside = html.replace(quality.full, '');
  assert.ok(!outside.includes('QC对账'), 'the old standalone QC row is gone');
  const withoutRolloff = baseInput(); withoutRolloff.mirror!.rolloff = null;
  assert.ok(!qualityDisclosure(render(currentSource, withoutRolloff)).detail.includes('QC对账'),
    'missing rolloff never invents a historical QC marker');
});

test('collapsed quality light differentiates healthy, pending, and critical failures', async t => {
  const cases: [string, Input, string, string][] = [['healthy', baseInput(), '状态正常', '#16a34a']];
  const oldRecon = baseInput();
  oldRecon.reconcile = { verdict: 'ok', stale: true, age_bdays: 2 };
  cases.push(['old reconciliation', oldRecon, '状态有警告', '#b45309']);
  const lag = baseInput(); lag.nowMs = Date.parse(lag.latest.ts) + 181000;
  cases.push(['heartbeat lag', lag, '状态有警告', '#b45309']);
  const shortSync = baseInput();
  shortSync.latest.rebuild_error = 'inventory/account mismatch'; shortSync.latest.rebuild_error_age_s = 599;
  cases.push(['short structure update', shortSync, '状态有警告', '#b45309']);
  const breach = baseInput(); breach.reconcile = { verdict: 'breach', stale: true, age_bdays: 2 };
  cases.push(['stale breach still critical', breach, '状态不正常', '#e11d48']);
  const dead = baseInput(); dead.nowMs = Date.parse(dead.latest.ts) + 601000;
  cases.push(['dead heartbeat', dead, '状态不正常', '#e11d48']);
  const longSync = structuredClone(shortSync); longSync.latest.rebuild_error_age_s = 600;
  cases.push(['persistent structure mismatch', longSync, '状态不正常', '#e11d48']);
  const stalePrice = baseInput(); stalePrice.latest.stale = true;
  cases.push(['stale price', stalePrice, '状态不正常', '#e11d48']);
  const missingQuotes = baseInput(); missingQuotes.latest.missing = ['AISS'];
  cases.push(['missing quote', missingQuotes, '状态不正常', '#e11d48']);
  const missingAnchor = baseInput(); delete missingAnchor.official!.aiss;
  cases.push(['missing official anchor', missingAnchor, '状态不正常', '#e11d48']);
  for (const [name, input, label, color] of cases) {
    await t.test(name, () => {
      const quality = qualityDisclosure(render(currentSource, input));
      assert.ok(quality.summaryText.includes(label), name);
      assert.ok(quality.summary.includes(color), `${name}: summary light matches status`);
    });
  }
});

function chartFixture() {
  const input = baseInput();
  input.latest.nodes = input.latest.nodes.filter(n => ['AISS', 'BDC', 'PORTFOLIO'].includes(n.display_name));
  for (const node of input.latest.nodes) node.value = node.display_name === 'AISS' ? 100
    : node.display_name === 'BDC' ? 200 : 300;
  input.prevClose = { date: '20260908',
    values: Object.fromEntries(input.latest.nodes.map(n => [n.node_id, n.value])) };
  input.mirror!.scalars.aiss = 1; input.mirror!.scalars.bdc = 1;
  input.official = { aiss: { date: '2026-09-08', value: 100 }, bdc: { date: '2026-09-08', value: 200 } };
  const rows = ['2026-09-09T13:35:00Z', '2026-09-09T13:36:00Z'].flatMap((ts, i) => [
    { ts, display_name: 'AISS', value: 102 + i, day_return: [0.02, 0.03][i] },
    { ts, display_name: 'BDC', value: 240 + 10 * i, day_return: [0.2, 0.25][i] },
    // Deliberately wrong ledger weighting: the production chart must still use
    // its existing official weights independently of legend selection.
    { ts, display_name: 'PORTFOLIO', value: 300, day_return: 0.9 },
  ]);
  return { input, chartStream: { date: '20260909', rows, isToday: true } };
}

function chartCapture(activeLines: string[], fixture = chartFixture()) {
  const graphCalls: GraphCalls = {};
  render(currentSource, fixture.input, { chartStream: fixture.chartStream, activeLines, graphCalls });
  assert.equal(graphCalls.LineChart?.length, 1, 'fixture renders a real chart instead of its empty state');
  const axes = graphCalls.YAxis;
  assert.equal(axes?.length, 2, 'both percentage and equivalent-dollar axes remain');
  const ret = axes.find(a => a.yAxisId === 'ret')!;
  const eq = axes.find(a => a.yAxisId === 'eq')!;
  return { ret, eq, data: graphCalls.LineChart[0].data as Record<string, number>[],
    names: graphCalls.Line.filter(line => line.yAxisId === 'ret').map(line => line.dataKey) };
}

test('chart legend selection fits both axes to visible lines without changing NAV data or weights', () => {
  const all = chartCapture(['AISS', 'BDC', 'PORTFOLIO']);
  assert.deepEqual(Array.from(all.ret.domain), [-28.75, 28.75]);
  assert.deepEqual(all.names.sort(), ['AISS', 'BDC', 'PORTFOLIO']);
  assert.equal(all.data[0].PORTFOLIO, 14, 'official 100:200 capital weights are preserved');
  assert.ok(Math.abs(all.data[1].PORTFOLIO - 53 / 3) < 1e-12);
  const single = chartCapture(['AISS']);
  assert.deepEqual(Array.from(single.ret.domain), [-3.45, 3.45],
    'hidden high-amplitude strategy and portfolio no longer hold the percentage axis open');
  assert.deepEqual(single.names, ['AISS']);
  assert.equal(JSON.stringify(single.data), JSON.stringify(all.data), 'legend only changes visibility and axis limits');
  for (const [i, bound] of [-3.45, 3.45].entries())
    assert.ok(Math.abs(single.eq.domain[i] - 300 * (1 + bound / 100)) < 1e-9,
      'right dollar axis stays aligned to the same percentage bounds');
  assert.equal(single.eq.allowDataOverflow, true,
    'invisible portfolio-dollar helper cannot expand the right axis away from selected lines');
  const portfolio = chartCapture(['PORTFOLIO']);
  assert.deepEqual(Array.from(portfolio.ret.domain), [-20.32, 20.32]);
  assert.deepEqual(portfolio.names, ['PORTFOLIO']);
});

test('chart scale stays finite for no selection, flat lines, missing data and invalid points', () => {
  const none = chartCapture([]);
  assert.deepEqual(Array.from(none.ret.domain), [-0.58, 0.58]);
  assert.deepEqual(none.names, []);
  const flat = chartFixture();
  for (const row of flat.chartStream.rows) if (row.display_name === 'AISS') row.day_return = 0;
  assert.deepEqual(Array.from(chartCapture(['AISS'], flat).ret.domain), [-0.58, 0.58]);
  for (const row of flat.chartStream.rows) if (row.display_name === 'AISS') row.day_return = -0.05;
  assert.deepEqual(Array.from(chartCapture(['AISS'], flat).ret.domain), [-5.75, 5.75],
    'negative return magnitudes set the same symmetric domain');
  for (const row of flat.chartStream.rows) if (row.display_name === 'AISS') row.day_return = Number.NaN;
  assert.deepEqual(Array.from(chartCapture(['AISS'], flat).ret.domain), [-0.58, 0.58],
    'nonfinite quote points do not poison either axis');
  flat.chartStream.rows = flat.chartStream.rows.filter(row => row.display_name !== 'AISS');
  assert.deepEqual(Array.from(chartCapture(['AISS'], flat).ret.domain), [-0.58, 0.58],
    'a selected strategy without observations preserves a safe empty range');
});

test('panels retain identical non-chart HTML outside authorized legacy and quality presentation changes', async t => {
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
  const adjustedReference = withoutQualitySection(withoutLegacyMirrorDecorations(oldSource), false);
  const adjustedCurrent = withoutQualitySection(currentSource, true);
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
  cases.push(['all six strategies, paired/subsector/direct holdings', allStrategyHoldings()]);

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
      const before = render(adjustedReference, input), after = render(adjustedCurrent, input);
      assert.equal(after, before);
      assertLegacyDecorationsAbsent(after);
    });
  }
});
