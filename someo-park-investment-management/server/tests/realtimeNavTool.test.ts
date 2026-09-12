import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { buildRealtimeNav, realtimeNavGrounding, realtimeNavTool } from '../tools/realtimeNavTool.js';
import { buildRealtimeNavPanel, roundedNavMoney, type NavPanelInput } from '../../shared/realtimeNav.js';
import { frozenMirror, midnightLatest, midnightOfficial, midnightPrevClose,
  intradayLatest, intradayOfficial } from './fixtures/realtimeNavGolden.js';

const NOW = Date.parse('2026-09-09T05:54:55Z');
const input = (): NavPanelInput => structuredClone({
  latest: midnightLatest, official: midnightOfficial, mirror: frozenMirror,
  prevClose: midnightPrevClose, streamRows: [],
  reconcile: { date: '2026-09-08', verdict: 'ok', age_bdays: 1, stale: false }, nowMs: NOW,
});

test('chat serializes the actual panel amount/percentage and keeps ledger diagnostics distinct', () => {
  for (const daytime of [false, true]) {
    const i = input();
    if (daytime) { i.latest = structuredClone(intradayLatest); i.official = structuredClone(intradayOfficial); }
    const panel = buildRealtimeNavPanel(i), tool = buildRealtimeNav(i);
    assert.equal(tool.portfolio.value, roundedNavMoney(panel.portfolio.value));
    assert.equal(tool.portfolio.display_return, panel.portfolio.display_return);
    assert.equal(tool.portfolio.official_anchored_value, tool.portfolio.value);
    for (const card of panel.strategies) {
      const row = tool.strategies.find((s: any) => s.strategy === card.strategy);
      assert.equal(row.display_value, card.display_value);
      assert.equal(row.display_return, card.display_return);
      assert.deepEqual(row.holdings, card.holdings);
      assert.equal(row.day_pnl_basis, 'ledger');
    }
    assert.equal(tool.portfolio.day_pnl_basis, 'ledger');
  }
  assert.equal(buildRealtimeNav(input()).portfolio.value, 7040623);
  assert.equal(buildRealtimeNav(input()).portfolio.display_return, '+1.34%');
});

test('chat ledger fallback includes a displayed value and failed official check, never the old formula', () => {
  const i = input();
  i.mirror = null;
  const tool = buildRealtimeNav(i);
  const pf = i.latest.nodes.find(n => n.kind === 'portfolio')!;
  assert.equal(tool.portfolio.value, roundedNavMoney(pf.value));
  assert.equal(tool.portfolio.official_anchored_value, null);
  assert.equal(tool.portfolio.basis, 'ledger');
  assert.equal(tool.quality_checks.states.official_anchor, 'fail');
  assert.ok(tool.strategies.every((s: any) => s.official_anchored_value === null));
});

test('chat preserves frozen inputs, historical K and the independently sourced panel date label', () => {
  const i = input(), before = structuredClone(i);
  const tool = buildRealtimeNav(i);
  assert.deepEqual(i, before);
  assert.deepEqual(tool.rolloff, frozenMirror.rolloff);
  assert.equal(tool.rolloff.k_equity, -12004.77);
  assert.equal(tool.portfolio.comparison_date, '20260908');
  assert.ok(tool.strategies.every((s: any) => s.official_eod.date === '2026-09-04'));
  assert.equal(roundedNavMoney(-1.5), -2);
});

test('existing pair/subsector dollar PnL remains available and expanded capital balances', () => {
  const i = input();
  const s = i.latest.nodes.find(n => n.display_name === 'MTFS')!;
  i.latest.nodes.push({ node_id: 'pair', kind: 'pair', parent_id: s.node_id,
    display_name: 'LONG/SHORT', value: 500, day_return: 0.01234, day_pnl: 123.45,
    holdings: [{ id: 'L', name: 'LONG', shares: 10, value: 1000 },
      { id: 'S', name: 'SHORT', shares: -5, value: -500 }] });
  i.mirror!.cohorts = { mtfs: { 'LONG/SHORT': { cohort: 'F', m: 1 } } };
  const tool = buildRealtimeNav(i);
  const pair = tool.mid_layers.find((n: any) => n.name === 'LONG/SHORT');
  assert.equal(pair.day_pnl_usd, 123.45);
  assert.equal(pair.day_return_pct, 1.234);
  assert.equal(pair.day_pnl_basis, 'ledger');
  assert.deepEqual(pair.holdings.map((h: any) => h.qc_shares), [10, -5]);
  const capital = tool.strategies.find((n: any) => n.strategy === 'MTFS').capital;
  assert.equal(capital.Lmv, 1000);
  assert.equal(capital.Smv, 500);
  assert.equal(capital.restrictedCash, 510);
  assert.equal(capital.freeCash + capital.restrictedCash + capital.Lmv - capital.Smv - capital.marginLoan, s.value);
  assert.equal(capital.gap, 0);
});

test('real tool execute and ordinary-chat grounding use the same read-only adapters and snapshot', async t => {
  // Virtual filesystem: exercise production readers without reading/writing production state.
  const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
  const out = path.join(repo, 'controller/output');
  const state = path.join(repo, 'trading_quantconnect/state');
  const data = path.join(repo, 'someo-park-investment-management/public/data');
  const i = input();
  const files = new Map<string, string>();
  const put = (p: string, value: unknown) => files.set(p, JSON.stringify(value));
  put(path.join(out, 'nav_latest.json'), i.latest);
  put(path.join(out, 'reconcile_2026-09-08.json'), { date: '2026-09-08', verdict: 'ok' });
  put(path.join(out, 'day_state.json'), { date: '2026-09-09', base_value: i.prevClose!.values });
  for (const date of ['20260908', '20260909']) files.set(path.join(out, `nav_stream_${date}.csv`),
    'ts,node_id,display_name,value,day_return\n' + i.latest.nodes.map(n =>
      `${i.latest.ts},${n.node_id},${n.display_name},${n.value},${n.day_return ?? ''}`).join('\n'));
  const off = i.official!;
  put(path.join(data, 'strategy_performance.json'), [{ date: '2026-09-04',
    mrpt_equity: off.mrpt!.value, mtfs_equity: off.mtfs!.value }]);
  put(path.join(data, 'master_portfolio_performance.json'), [{ date: '2026-09-04',
    sr_equity: off.ssrs!.value, aiss_equity: off.aiss!.value, aeus_equity: off.aeus!.value }]);
  put(path.join(data, 'private_credit_bdc_performance.json'), [{ date: '2026-09-04', bdc_equity: off.bdc!.value }]);
  put(path.join(state, 'exporter_state.json'), { scalars: i.mirror!.scalars,
    scalar_basis: { official: { mrpt: 0, mtfs: 0 }, ledger: i.mirror!.capital_base } });
  put(path.join(state, 'rolloff.json'), i.mirror!.rolloff);
  for (const f of ['legacy_positions.json', 'scaled_positions.json']) put(path.join(state, f), { frozen: {} });
  for (const st of ['mrpt', 'mtfs']) put(path.join(repo, `inventory_${st}.json`), { pairs: {} });

  const originalRead = fs.readFileSync, originalExists = fs.existsSync, originalDir = fs.readdirSync;
  const protectedPath = (p: string) => p.startsWith(out) || p.startsWith(state) || p.startsWith(data)
    || /inventory_(mrpt|mtfs)\.json$/.test(p);
  t.mock.method(Date, 'now', () => NOW);
  t.mock.method(fs, 'readFileSync', (p: any, options: any) => {
    const key = String(p);
    if (files.has(key)) return files.get(key)!;
    if (protectedPath(key)) throw new Error(`missing fixture: ${key}`);
    return originalRead(p, options);
  });
  t.mock.method(fs, 'existsSync', (p: any) => protectedPath(String(p))
    ? String(p) === out || files.has(String(p)) : originalExists(p));
  t.mock.method(fs, 'readdirSync', (p: any, options: any) => String(p) === out
    ? [...files.keys()].filter(k => path.dirname(k) === out).map(k => path.basename(k))
    : originalDir(p, options));
  t.mock.method(fs, 'writeFileSync', () => { throw new Error('NAV must be read-only'); });
  t.mock.method(globalThis, 'fetch', () => { throw new Error('NAV must not call a network/LLM'); });

  const tool: any = await realtimeNavTool.execute({});
  assert.equal(tool.error, undefined);
  assert.equal(tool.portfolio.value, 7040623);
  assert.equal(tool.quality_checks.reconcile_age_bdays, 1);
  assert.equal(tool.quality_checks.status, 'pass');
  assert.deepEqual(tool.input_errors, {});
  const grounding = await realtimeNavGrounding();
  assert.ok(grounding!.endsWith(JSON.stringify(tool)));
  assert.equal(realtimeNavTool.isReadOnly!(), true);
});
