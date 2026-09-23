import assert from 'node:assert/strict';
import test from 'node:test';
import {
  analyzePerformancePair, type PerformanceEquityRow,
} from '../../shared/performancePairAnalysis.js';

function near(actual: number | null, expected: number, tolerance = 1e-12) {
  assert.notEqual(actual, null);
  assert.ok(Math.abs(actual! - expected) <= tolerance,
    `${actual} differs from ${expected} by more than ${tolerance}`);
}

function equityRows(returnsA: number[], returnsB: number[], baseA = 100, baseB = 100): PerformanceEquityRow[] {
  assert.equal(returnsA.length, returnsB.length);
  let a = baseA, b = baseB;
  const row = (i: number) => ({
    date: new Date(Date.UTC(2026, 0, i + 1)).toISOString().slice(0, 10),
    a_equity: a, b_equity: b,
  });
  return [row(0), ...returnsA.map((r, i) => {
    a *= 1 + r;
    b *= 1 + returnsB[i];
    return row(i + 1);
  })];
}

test('perfect positive correlation uses daily simple returns and sample covariance', () => {
  const result = analyzePerformancePair(equityRows([0.1, 0.2, 0.3], [0.2, 0.4, 0.6]), 'a', 'b');
  assert.equal(result.status, 'ok');
  assert.equal(result.sampleCount, 3);
  assert.equal(result.firstIntervalStartDate, '2026-01-01');
  assert.equal(result.sampleStartDate, '2026-01-02');
  assert.equal(result.sampleEndDate, '2026-01-04');
  near(result.correlation, 1);
  near(result.covariance, 0.02); // Divisor n-1; population covariance would be 0.01333.
  near(result.beta, 0.5);
  near(result.covariance! * 10000, 200, 1e-9);
});

test('perfect negative correlation and asymmetric beta direction', () => {
  const rows = equityRows([-0.2, 0, 0.2], [0.1, 0, -0.1]);
  const ab = analyzePerformancePair(rows, 'a', 'b');
  const ba = analyzePerformancePair(rows, 'b', 'a');
  near(ab.correlation, -1);
  near(ab.covariance, -0.02);
  near(ab.beta, -2);
  near(ba.beta, -0.5);
  near(ba.covariance, ab.covariance!);
  assert.equal(ab.sampleCount, 3, 'zero-return intervals remain valid observations');
});

test('orthogonal return deviations have zero covariance despite rising equity levels', () => {
  const result = analyzePerformancePair(equityRows([0.1, 0.2, 0.3], [0.2, 0.1, 0.2]), 'a', 'b');
  assert.equal(result.status, 'ok');
  near(result.covariance, 0);
  near(result.correlation, 0);
  near(result.beta, 0);
});

test('beta is cov(A,B)/var(B), not a symmetric correlation or ratio of volatilities', () => {
  const rows = equityRows([-0.1, 0, 0.1], [-0.1, 0, 0.2]);
  const ab = analyzePerformancePair(rows, 'a', 'b');
  const ba = analyzePerformancePair(rows, 'b', 'a');
  near(ab.covariance, 0.015);
  near(ab.beta, 9 / 14);
  near(ba.beta, 1.5);
  near(ab.correlation, 0.015 / Math.sqrt(0.01 * (7 / 300)));
});

test('rescaling either initial capital does not change return statistics', () => {
  const a = [0.03, -0.01, 0.02, 0, -0.04];
  const b = [-0.02, 0.04, 0.01, 0, 0.03];
  const original = analyzePerformancePair(equityRows(a, b), 'a', 'b');
  const scaled = analyzePerformancePair(equityRows(a, b, 7_000_000, 0.003), 'a', 'b');
  near(scaled.correlation, original.correlation!);
  near(scaled.covariance, original.covariance!);
  near(scaled.beta, original.beta!);
  assert.equal(scaled.sampleCount, original.sampleCount);
});

test('a missing equity blocks both neighboring intervals and cannot create a multi-day return', () => {
  const rows = equityRows([0.1, 0.2, 0.3, 0.4, 0.5], [0.2, 0.4, 0.6, 0.8, 1]);
  delete rows[2].a_equity;
  const result = analyzePerformancePair(rows, 'a', 'b');
  assert.equal(result.sampleCount, 3); // Intervals 0→1, 3→4, 4→5 only.
  near(result.covariance, 13 / 150);
  near(result.correlation, 1);
  near(result.beta, 0.5);
  assert.equal(result.sampleStartDate, '2026-01-02');
  assert.equal(result.sampleEndDate, '2026-01-06');
});

test('returns align on the same source interval, not independently filtered series', () => {
  const rows = equityRows([0.01, 0.02, 0.03, 0.04, 0.05], [0.05, 0.04, 0.03, 0.02, 0.01]);
  delete rows[1].a_equity;
  delete rows[4].b_equity;
  const result = analyzePerformancePair(rows, 'a', 'b');
  assert.equal(result.sampleCount, 1); // Only Jan 3→Jan 4 is shared and adjacent.
  assert.equal(result.firstIntervalStartDate, '2026-01-03');
  assert.equal(result.sampleStartDate, '2026-01-04');
  assert.equal(result.sampleEndDate, '2026-01-04');
  assert.equal(result.correlation, null);
  assert.equal(result.covariance, null);
});

for (const invalid of [undefined, null, NaN, Infinity, -Infinity, 0, -10, '100']) {
  test(`invalid equity ${String(invalid)} is not coerced or filled`, () => {
    const rows = equityRows([0.1, 0.2, 0.3, 0.4, 0.5], [0.2, 0.4, 0.6, 0.8, 1]);
    rows[2].b_equity = invalid;
    const result = analyzePerformancePair(rows, 'a', 'b');
    assert.equal(result.sampleCount, 3);
    near(result.covariance, 13 / 150);
    near(result.beta, 0.5);
  });
}

test('unsorted input is ordered by date without mutating the caller', () => {
  const rows = equityRows([0.1, 0.2, 0.3], [0.2, 0.4, 0.6]);
  const shuffled = [rows[2], rows[0], rows[3], rows[1]];
  const originalOrder = shuffled.map(row => row.date);
  assert.deepEqual(analyzePerformancePair(shuffled, 'a', 'b'), analyzePerformancePair(rows, 'a', 'b'));
  assert.deepEqual(shuffled.map(row => row.date), originalOrder);
});

test('duplicate dates reject the window instead of choosing a potentially conflicting value', () => {
  const rows = equityRows([0.1, 0.2, 0.3], [0.2, 0.4, 0.6]);
  rows.push({ ...rows[1], a_equity: 900 });
  const result = analyzePerformancePair(rows, 'a', 'b');
  assert.equal(result.status, 'invalid_dates');
  assert.equal(result.sampleCount, 0);
  assert.equal(result.correlation, null);
});

for (const date of ['2026-02-30', '2026-13-01', '2026-1-01', '', 'not-a-date']) {
  test(`invalid date ${JSON.stringify(date)} rejects the window`, () => {
    const rows = equityRows([0.1, 0.2, 0.3], [0.2, 0.4, 0.6]);
    rows[2].date = date;
    assert.equal(analyzePerformancePair(rows, 'a', 'b').status, 'invalid_dates');
  });
}

test('adjacent trading records may cross a weekend, without adding a synthetic sample', () => {
  const rows = equityRows([0.1, 0.2, 0.3], [0.2, 0.4, 0.6]);
  ['2026-01-02', '2026-01-05', '2026-01-06', '2026-01-07'].forEach((date, i) => { rows[i].date = date; });
  const result = analyzePerformancePair(rows, 'a', 'b');
  assert.equal(result.sampleCount, 3);
  assert.equal(result.sampleStartDate, '2026-01-05');
  near(result.correlation, 1);
});

test('short windows expose n and dates but do not publish rho or beta', () => {
  for (let n = 0; n < 3; n += 1) {
    const rows = equityRows([0.1, 0.2].slice(0, n), [0.2, 0.4].slice(0, n));
    const result = analyzePerformancePair(rows, 'a', 'b');
    assert.equal(result.status, 'insufficient_samples');
    assert.equal(result.sampleCount, n);
    assert.equal(result.correlation, null);
    assert.equal(result.beta, null);
    if (n < 2) assert.equal(result.covariance, null);
    else near(result.covariance, 0.01);
  }
  const empty = analyzePerformancePair([], 'a', 'b');
  assert.equal(empty.sampleCount, 0);
  assert.equal(empty.sampleStartDate, null);
  assert.equal(empty.firstIntervalStartDate, null);
});

test('zero variance returns covariance zero and withholds rho and beta', () => {
  for (const [a, b] of [
    [[0, 0, 0], [0.1, 0.2, 0.3]],
    [[0.1, 0.2, 0.3], [0, 0, 0]],
    [[0, 0, 0], [0, 0, 0]],
  ]) {
    const result = analyzePerformancePair(equityRows(a, b), 'a', 'b');
    assert.equal(result.status, 'zero_variance');
    assert.equal(result.sampleCount, 3);
    assert.equal(result.covariance, 0);
    assert.equal(result.correlation, null);
    assert.equal(result.beta, null);
  }
});

test('constant positive returns do not turn rounding noise into a reported correlation', () => {
  const returns = Array.from({ length: 40 }, () => 0.01);
  const result = analyzePerformancePair(equityRows(returns, returns, 123.45, 8_765_432.1), 'a', 'b');
  assert.equal(result.sampleCount, 40);
  assert.equal(result.status, 'zero_variance');
  assert.equal(result.covariance, 0);
  assert.equal(result.correlation, null);
  assert.equal(result.beta, null);
});

test('small but genuine return variation above rounding precision remains measurable', () => {
  const result = analyzePerformancePair(
    equityRows([0.01 - 1e-10, 0.01, 0.01 + 1e-10], [0.02 - 2e-10, 0.02, 0.02 + 2e-10]), 'a', 'b',
  );
  assert.equal(result.status, 'ok');
  near(result.correlation, 1, 1e-6);
  near(result.beta, 0.5, 1e-6);
});

test('overflowing ratio is excluded and never produces Infinity statistics', () => {
  const rows = [
    { date: '2026-01-01', a_equity: Number.MIN_VALUE, b_equity: 100 },
    { date: '2026-01-02', a_equity: Number.MAX_VALUE, b_equity: 110 },
  ];
  const result = analyzePerformancePair(rows, 'a', 'b');
  assert.equal(result.sampleCount, 0);
  assert.equal(result.covariance, null);
});

test('finite returns that overflow centered moments report unavailable statistics', () => {
  const rows = [1, 1e200, 1, 1e200].map((equity, i) => ({
    date: `2026-01-0${i + 1}`, a_equity: equity, b_equity: equity,
  }));
  const result = analyzePerformancePair(rows, 'a', 'b');
  assert.equal(result.sampleCount, 3);
  assert.equal(result.status, 'non_finite_statistics');
  assert.equal(result.correlation, null);
  assert.equal(result.covariance, null);
  assert.equal(result.beta, null);
});
