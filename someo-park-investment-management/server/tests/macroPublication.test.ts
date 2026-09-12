import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { macroContextForArtifacts, MACRO_ARTIFACT_TYPES } from '../tools/macroMarketTool.js';
import { macroMarketDataTool } from '../tools/macroMarketDataTool.js';
import { markDisplayStatus } from '../../src/components/macro/markPresentation.js';

const NOW = Date.parse('2026-09-10T06:00:00Z');

test('a new mark timestamp never disguises an expired book or unverified historical quote', () => {
  const recent = { mark_status: 'marked' as const, mark_ts: new Date(NOW).toISOString(),
    quote_ts: new Date(NOW - 300_000).toISOString(), quote_max_age_seconds: 1200 };
  assert.equal(markDisplayStatus(recent, NOW), 'marked');
  assert.equal(markDisplayStatus({ ...recent, quote_ts: '2026-09-09T09:04:00Z' }, NOW), 'stale');
  assert.equal(markDisplayStatus({ ...recent, quote_ts: undefined }, NOW), 'unverified');
  assert.equal(markDisplayStatus({ ...recent, quote_ts: new Date(NOW + 1).toISOString() }, NOW), 'stale');
  assert.equal(markDisplayStatus({ mark_ts: new Date(NOW).toISOString() }, NOW), 'unverified');
  for (const status of ['stale', 'missing', 'illiquid', 'unverified'] as const) {
    assert.equal(markDisplayStatus({ ...recent, mark_status: status }, NOW), status);
  }
});

test('replay panel analysis and agent tool read the newly published result and preserve alarm counts', async t => {
  assert.ok((MACRO_ARTIFACT_TYPES as readonly string[]).includes('macro_livereplay'));
  const leg = { series: 'KXCPI', period: '2026-08', detail: 'missing expected trade' };
  const fixture = { generated_at: '2026-09-09T15:22:00Z', latest_ts: '2026-09-09T15:21:00Z',
    latest: { window_start: '2026-08-11', window_end: '2026-09-08', days: 28,
      generated_at: '2026-09-09T15:21:00Z', replay: { n_trades: 5 }, live: { n_trades: 8 },
      reconciliation: { verdict: 'REVIEW', n_unexplained: 1, n_matched: 4,
        n_replay_only: 1, n_live_only: 4, unexplained: [leg],
        matched: Array(1000).fill(leg), replay_only: Array(1000).fill(leg) },
      opportunity: { n_pass: 100, infra_share: .2, by_bucket: { infra: 20, strategy: 80 } } },
    history: Array.from({ length: 60 }, (_, i) => ({ window_end: String(i), n_unexplained: i ? 0 : 1 })) };
  t.mock.method(fs.promises, 'readFile', async (file: any) => {
    assert.equal(path.basename(String(file)), 'macro_livereplay.json');
    return JSON.stringify(fixture);
  });
  const context = await macroContextForArtifacts(['macro_livereplay']);
  assert.match(context, /"window_end":"2026-09-08"/);
  assert.match(context, /"n_unexplained":1/);
  assert.match(context, /missing expected trade/);
  assert.match(context, /latest_ts is the research result time/);
  assert.doesNotMatch(context, /\[truncated\]/);
  assert.ok(context.length < 8000);
  assert.equal(await macroMarketDataTool.execute({ view: 'macro_livereplay' }), context);
});

test('decision grounding keeps quote age and unavailable totals ahead of its ledger tail', async t => {
  const valuation = { n_legs: 5, n_unmarked: 5, mark_status: 'stale', quote_ts: '2026-09-09T09:05:00Z' };
  t.mock.method(fs.promises, 'readFile', async () => JSON.stringify({
    generated_at: '2026-09-10T06:00:00Z', valuation,
    latest_marks: [{ ticker: 'KXCPI-T1', mid: null, pnl_usd: -.02, ...valuation }],
    decisions: Array.from({ length: 25 }, (_, i) => ({ id: i, kind: 'pass', inputs_json: 'x'.repeat(10000) })),
  }));
  const context = await macroContextForArtifacts(['macro_decisions']);
  assert.match(context, /"n_unmarked":5/);
  assert.match(context, /"mid":null/);
  assert.match(context, /generated_at and mark_ts are not quote freshness/);
  assert.match(context, /not describe it as a complete live market valuation/);
  assert.doesNotMatch(context, /inputs_json|\[truncated\]/);
});
