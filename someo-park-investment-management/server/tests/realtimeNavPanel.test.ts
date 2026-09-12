import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  buildRealtimeNavPanel, holdingsPresentation, officialAnchor,
} from '../../shared/realtimeNav.js';
import {
  frozenMirror, intradayExpected, intradayLatest, intradayOfficial,
  midnightExpected, midnightLatest, midnightOfficial, midnightPrevClose,
} from './fixtures/realtimeNavGolden.js';

type PanelInput = Parameters<typeof buildRealtimeNavPanel>[0];
const NOW = Date.parse('2026-09-09T05:54:55Z');
const copy = <T>(value: T): T => structuredClone(value);
const near = (actual: number | null, expected: number, tolerance = 1e-8) => {
  assert.equal(typeof actual, 'number');
  assert.ok(Math.abs(actual! - expected) <= tolerance,
    `expected ${actual} to be within ${tolerance} of ${expected}`);
};
const input = (overrides: Partial<PanelInput> = {}): PanelInput => ({
  latest: copy(midnightLatest), official: copy(midnightOfficial),
  mirror: copy(frozenMirror), prevClose: copy(midnightPrevClose), streamRows: [],
  reconcile: { date: '2026-09-08', verdict: 'ok', age_bdays: 1, stale: false },
  nowMs: NOW, ...overrides,
});

/**
 * Behavior-preservation oracle transcribed from the PRE-CHANGE panel at commit
 * f2fe5631b04c652c2923c22f09c4cfa3b65df2ad, lines 336–347, 447–454,
 * 476–484, 536–545 and 584–585. It does not call the shared implementation.
 * This deliberately preserves existing fallback/date semantics; it does not
 * claim those labels establish fresh prices or a QuantConnect equity match.
 */
function legacyOracle(i: PanelInput) {
  const names: Record<string, string> = {
    MRPT: 'mrpt', MTFS: 'mtfs', SSRS: 'ssrs', AISS: 'aiss', AEUS: 'aeus', BDC: 'bdc',
  };
  const strategies = i.latest.nodes.filter(n => n.kind === 'strategy');
  const portfolio = i.latest.nodes.find(n => n.kind === 'portfolio');
  const previous: Record<string, number> = {};
  for (const n of i.latest.nodes) {
    const value = i.prevClose?.values?.[n.node_id];
    if (value !== undefined) previous[n.display_name] = value;
  }
  const day = (name: string, value: number) => {
    const node = i.latest.nodes.find(n => n.display_name === name);
    if (node?.day_return !== undefined && node.day_return !== null) return node.day_return * 100;
    const base = previous[name] ?? i.streamRows?.find(r => r.display_name === name)?.value;
    return base ? (value / Number(base) - 1) * 100 : null;
  };
  const parts = strategies.map(n => {
    const key = names[n.display_name], off = i.official?.[key];
    if (!off) return null;
    if (['MRPT', 'MTFS'].includes(n.display_name)) {
      const c = i.mirror?.capital_base?.[key];
      return typeof c === 'number' ? { ...off, live: n.value - c } : null;
    }
    const k = i.mirror?.scalars?.[key];
    return typeof k === 'number' && k > 0 ? { ...off, live: n.value * k } : null;
  });
  const allOfficial = parts.length > 0 && parts.every(Boolean);
  const value = allOfficial ? parts.reduce((s, p) => s + p!.live, 0) : portfolio?.value ?? 0;
  const base = allOfficial ? parts.reduce((s, p) => s + p!.value, 0) : 0;
  const pct = allOfficial && base ? (value / base - 1) * 100
    : portfolio ? day('PORTFOLIO', portfolio.value) : null;
  return { value, base, pct, basis: allOfficial ? 'official' : 'ledger',
    date: Object.keys(previous).length && i.prevClose?.date ? i.prevClose.date : null };
}

test('frozen midnight capture reproduces the displayed six-strategy total, not the old chat formula', () => {
  const result = buildRealtimeNavPanel(input());
  near(result.portfolio.value, midnightExpected.main);
  near(result.portfolio.official_anchored_value, midnightExpected.main);
  near(result.portfolio.official_eod_total, midnightExpected.officialBase);
  near(result.portfolio.day_return_pct, midnightExpected.pct);
  assert.equal(result.portfolio.display_value, '7,040,623');
  assert.equal(result.portfolio.display_return, '+1.34%');
  assert.equal(result.portfolio.basis, 'official');
  assert.equal(result.strategies.length, 6);
  for (const s of result.strategies) {
    near(s.value, midnightExpected.perStrategy[s.strategy]);
    assert.equal(s.official_eod?.date, '2026-09-04');
  }
  // All captured controller day_return values were zero. EOD × (1+r) gives the
  // old $6,947,586 total and must never replace the frozen panel expectation.
  const oldTotal = Object.values(midnightOfficial).reduce((a, p) => a + p.value, 0);
  near(oldTotal, midnightExpected.oldFormulaTotal);
  assert.ok(result.portfolio.value - oldTotal > 93036);
});

test('frozen daytime capture preserves additive MRPT/MTFS dollar values after EOD catches up', () => {
  const result = buildRealtimeNavPanel(input({
    latest: copy(intradayLatest), official: copy(intradayOfficial),
    nowMs: Date.parse('2026-09-09T18:30:17Z'),
  }));
  near(result.portfolio.value, intradayExpected.main);
  near(result.portfolio.day_return_pct, intradayExpected.pct);
  assert.equal(result.portfolio.display_value, '7,054,240');
  assert.equal(result.portfolio.display_return, '+0.19%');
  for (const s of result.strategies) near(s.value, intradayExpected.perStrategy[s.strategy]);
  assert.equal(result.strategies.find(s => s.strategy === 'MTFS')?.display_return, '+6.21%');
  assert.equal(result.strategies.find(s => s.strategy === 'MRPT')?.display_return, '+0.08%');
  near(intradayExpected.main - intradayExpected.oldFormulaTotal, 12917.780471470207);
});

test('missing official row, multiplier or additive capital base preserves whole-portfolio ledger fallback', () => {
  const cases: PanelInput[] = [];
  const missingOfficial = input(); delete missingOfficial.official!.mtfs; cases.push(missingOfficial);
  const missingK = input(); delete missingK.mirror!.scalars.aiss; cases.push(missingK);
  const missingC = input(); delete missingC.mirror!.capital_base!.mrpt; cases.push(missingC);
  cases.push(input({ official: null }), input({ mirror: null }));
  for (const i of cases) {
    const result = buildRealtimeNavPanel(i), old = legacyOracle(i);
    near(result.portfolio.value, 6047731.97);
    near(result.portfolio.value, old.value);
    assert.equal(result.portfolio.basis, 'ledger');
    assert.equal(result.portfolio.official_anchored_value, null);
    assert.equal(result.portfolio.official_eod_total, 0);
    assert.equal(result.portfolio.day_return_pct, 0);
    assert.equal(result.quality.states.official_anchor, 'fail');
  }
  assert.equal(officialAnchor('MRPT', 100, midnightOfficial, null), null);
  assert.equal(officialAnchor('UNKNOWN', 100, midnightOfficial, frozenMirror), null);
});

test('a zero official EOD is present, a zero C is usable, and nonpositive multipliers are missing', () => {
  const i = input();
  i.latest.nodes = [
    { node_id: 'm', display_name: 'MRPT', kind: 'strategy', value: 25, day_return: 0.2 },
    { node_id: 'p', display_name: 'PORTFOLIO', kind: 'portfolio', value: 25, day_return: 0.2 },
  ];
  i.official = { mrpt: { date: '2026-09-08', value: 0 } };
  i.mirror = { scalars: {}, capital_base: { mrpt: 0 } };
  const result = buildRealtimeNavPanel(i);
  assert.equal(result.portfolio.basis, 'official');
  assert.equal(result.portfolio.value, 25);
  assert.equal(result.portfolio.official_eod_total, 0);
  assert.equal(result.portfolio.day_return_pct, 20); // original UI fallback when EOD sum is zero
  assert.equal(result.quality.states.official_anchor, 'pass');
  for (const k of [0, -1]) {
    assert.equal(officialAnchor('AISS', 100, midnightOfficial, { scalars: { aiss: k } }), null);
  }
});

test('no strategy nodes and no portfolio preserves a zero ledger amount and unknown return', () => {
  const i = input(); i.latest.nodes = []; i.prevClose = null;
  const result = buildRealtimeNavPanel(i), old = legacyOracle(i);
  assert.equal(result.portfolio.value, old.value);
  assert.equal(result.portfolio.value, 0);
  assert.equal(result.portfolio.basis, 'ledger');
  assert.equal(result.portfolio.day_return_pct, null);
  assert.equal(result.portfolio.display_return, null);
  assert.deepEqual(result.strategies, []);
});

test('legacy return fallback prefers published day_return, then previous close, then first stream row', () => {
  const i = input({ official: null, prevClose: null });
  i.latest.nodes = [{ node_id: 'p', display_name: 'PORTFOLIO', kind: 'portfolio', value: 120 }];
  i.streamRows = [{ display_name: 'PORTFOLIO', value: 80 }];
  near(buildRealtimeNavPanel(i).portfolio.day_return_pct, 50);
  i.prevClose = { date: '20260908', values: { p: 100 } };
  near(buildRealtimeNavPanel(i).portfolio.day_return_pct, 20);
  i.latest.nodes[0].day_return = 0;
  assert.equal(buildRealtimeNavPanel(i).portfolio.day_return_pct, 0);
  delete i.latest.nodes[0].day_return;
  i.prevClose.values.p = 0;
  assert.equal(buildRealtimeNavPanel(i).portfolio.day_return_pct, null);
});

test('date label preserves stream date independently of official EOD date and checks mapped previous values', () => {
  const i = input(), result = buildRealtimeNavPanel(i);
  assert.equal(result.portfolio.comparison_date, '20260908');
  assert.equal(result.portfolio.comparison_date, legacyOracle(i).date);
  assert.equal(result.strategies[0].official_eod?.date, '2026-09-04');
  i.prevClose = { date: '20260908', values: { unrelated: 100 } };
  assert.equal(buildRealtimeNavPanel(i).portfolio.comparison_date, null);
  i.prevClose.values = { [i.latest.nodes[0].node_id]: 0 };
  assert.equal(buildRealtimeNavPanel(i).portfolio.comparison_date, '20260908');
});

test('banker rounding preserves positive and negative half ties in multiplicative displayed/QC shares', () => {
  const st = { node_id: 'a', display_name: 'AISS', kind: 'strategy', value: 100 };
  const mirror = { scalars: { aiss: 0.5 } };
  for (const [ledger, shares] of [[3, 2], [5, 2], [7, 4], [-3, -2], [-5, -2], [-7, -4]]) {
    const h = holdingsPresentation({ id: 'X', name: 'X', shares: ledger, value: ledger * 10 },
      st, midnightOfficial, mirror);
    assert.equal(h.shares, shares);
    assert.equal(h.qc_shares, shares);
    assert.equal(h.ledger_shares, ledger);
    assert.equal(h.value, ledger * 5); // dollar value is not re-marked using rounded shares
    assert.equal(h.missing_scalar, false);
  }
});

test('additive holdings remain ledger shares and dollars while L/S/F QC cohorts scale independently', () => {
  const st = { node_id: 'm', display_name: 'MTFS', kind: 'strategy', value: 950000 };
  for (const [cohort, multiplier, ledger, qc] of [
    ['L', 0, 5, 0], ['S', 0.5, 5, 2], ['S', 0.5, -5, -2], ['F', 1, 5.5, 6],
  ] as const) {
    const h = holdingsPresentation({ id: 'X', name: 'X', shares: ledger, value: ledger * 10 },
      st, midnightOfficial, frozenMirror, { cohort, m: multiplier });
    assert.equal(h.shares, ledger);
    assert.equal(h.qc_shares, qc);
    assert.equal(h.value, ledger * 10);
    assert.equal(h.missing_scalar, false);
  }
  const h = holdingsPresentation({ id: 'X', name: 'X', shares: 5, value: 50 },
    st, midnightOfficial, frozenMirror);
  assert.equal(h.qc_shares, null);
});

test('missing scalar preserves ledger holding values and flags missing QC share projection', () => {
  const st = { node_id: 'a', display_name: 'AISS', kind: 'strategy', value: 100 };
  const holding = { id: 'X', name: 'X', shares: 5, value: 50 };
  const h = holdingsPresentation(holding, st, midnightOfficial, { scalars: {} });
  assert.equal(h.shares, 5);
  assert.equal(h.value, 50);
  assert.equal(h.qc_shares, null);
  assert.equal(h.missing_scalar, true);
  // With a valid k but no official row, the original UI still projects shares,
  // while the amount falls back to ledger scale. This is a parity test only.
  const withoutOfficial = holdingsPresentation(holding, st, null, { scalars: { aiss: 0.5 } });
  assert.equal(withoutOfficial.shares, 2);
  assert.equal(withoutOfficial.qc_shares, 2);
  assert.equal(withoutOfficial.value, 50);
});

test('nested pair holdings receive their independently supplied QC cohort', () => {
  const i = input();
  i.latest.nodes.push({ node_id: 'pair', display_name: 'TEST/PAIR', kind: 'pair',
    value: 50, parent_id: 'SPSTWPVC13D',
    holdings: [{ id: 'X', name: 'X', shares: -5, value: -50 }] });
  i.mirror!.cohorts = { mrpt: { 'TEST/PAIR': { cohort: 'S', m: 0.5 } } };
  const child = buildRealtimeNavPanel(i).strategies.find(s => s.strategy === 'MRPT')!.children[0];
  assert.equal(child.holdings[0].shares, -5);
  assert.equal(child.holdings[0].qc_shares, -2);
  assert.equal(child.holdings[0].value, -50);
});

test('heartbeat uses strict 180/600 second thresholds; future tick age clamps to zero', () => {
  for (const [age, state, dead, lagging] of [
    [-1, 'pass', false, false], [180, 'pass', false, false],
    [180.001, 'pending', false, true], [600, 'pending', false, true],
    [600.001, 'fail', true, true],
  ] as const) {
    const i = input(); i.latest.ts = new Date(NOW - age * 1000).toISOString();
    const result = buildRealtimeNavPanel(i);
    assert.equal(result.quality.states.heartbeat, state);
    assert.equal(result.feed.dead, dead);
    assert.equal(result.feed.lagging, lagging);
    near(result.feed.ageSeconds, Math.max(0, age), 1e-5);
  }
});

test('reconcile breach stays red when stale; stale ok and missing/partial verdicts remain pending', () => {
  for (const [verdict, stale, state] of [
    ['ok', false, 'pass'], ['ok', true, 'pending'], ['breach', true, 'fail'],
    ['breach', false, 'fail'], ['partial', false, 'pending'], ['none', false, 'pending'],
  ]) {
    const result = buildRealtimeNavPanel(input({ reconcile: { verdict, stale } }));
    assert.equal(result.quality.states.reconcile, state);
  }
  assert.equal(buildRealtimeNavPanel(input({ reconcile: null })).quality.states.reconcile, 'pending');
});

test('structure sync turns red at 600 seconds while a missing age remains pending', () => {
  for (const [age, state] of [[null, 'pending'], [599.999, 'pending'], [600, 'fail'], [601, 'fail']] as const) {
    const i = input(); i.latest.rebuild_error = 'inventory/account mismatch';
    i.latest.rebuild_error_age_s = age;
    assert.equal(buildRealtimeNavPanel(i).quality.states.structure_sync, state);
  }
  const i = input(); i.latest.rebuild_error_age_s = 900;
  assert.equal(buildRealtimeNavPanel(i).quality.states.structure_sync, 'pass');
});

test('seven-state summary preserves existing closed-market carry semantics and failure precedence', () => {
  const all = buildRealtimeNavPanel(input());
  assert.equal(all.quality.status, 'pass');
  assert.equal(all.quality.allPass, true);
  assert.equal(all.quality.anyFail, false);
  assert.equal(Object.keys(all.quality.states).length, 7);
  assert.ok(Object.values(all.quality.states).every(s => s === 'pass'));
  const i = input(); i.latest.stale = true; i.latest.missing = ['X'];
  i.reconcile = { verdict: 'ok', stale: true };
  const failed = buildRealtimeNavPanel(i);
  assert.equal(failed.quality.states.price_fresh, 'fail');
  assert.equal(failed.quality.states.full_book_quotes, 'fail');
  assert.equal(failed.quality.states.reconcile, 'pending');
  assert.equal(failed.quality.states.dual_engine_match, 'pass');
  assert.equal(failed.quality.status, 'fail');
  assert.equal(failed.quality.allPass, false);
  assert.equal(failed.quality.anyFail, true);
});

test('feed presentation preserves null-delay carry and subtracts actual delayed-feed minutes', () => {
  const carry = buildRealtimeNavPanel(input()).feed;
  assert.equal(carry.delayMin, 0);
  assert.equal(Date.parse(carry.priceTs), Date.parse(midnightLatest.ts));
  assert.equal(carry.priceTimeEt, '01:53:55');
  assert.equal(carry.timeEt, '01:53:55');
  const i = input(); i.latest.feed_delay_min = 15;
  const delayed = buildRealtimeNavPanel(i).feed;
  assert.equal(Date.parse(delayed.priceTs), Date.parse(midnightLatest.ts) - 900000);
  assert.equal(delayed.priceTimeEt, '01:38:55');
});

test('rolloff preserves the frozen historical object independently of current quality state', () => {
  const i = input(); i.reconcile = { verdict: 'breach', stale: true };
  const result = buildRealtimeNavPanel(i);
  assert.deepEqual(result.rolloff, frozenMirror.rolloff);
  assert.equal(result.rolloff?.k_equity, -12004.77);
  assert.equal(result.rolloff?.measured_on, '2026-08-31');
  assert.equal(result.quality.status, 'fail');
  delete i.mirror!.rolloff;
  assert.equal(buildRealtimeNavPanel(i).rolloff, null);
});

test('the front-end headline uses the shared entry point exercised by the golden cases', () => {
  const viewer = readFileSync(new URL('../../src/components/artifacts/RealtimeNavViewer.tsx', import.meta.url), 'utf8');
  assert.match(viewer, /import\s*\{[^}]*\bbuildRealtimeNavPanel\b[^}]*\}\s*from\s*['"][^'"]*shared\/realtimeNav(?:\.js)?['"]/);
  const binding = /const\s+(\w+)\s*=\s*buildRealtimeNavPanel\s*\(/.exec(viewer)?.[1];
  assert.ok(binding, 'the viewer must call the tested shared panel computation');
  assert.ok(viewer.includes(`${binding}.portfolio.display_value`), 'headline dollars use the shared display value');
  assert.ok(viewer.includes(`${binding}.portfolio.day_return_pct`), 'headline return uses the shared percentage');
});
