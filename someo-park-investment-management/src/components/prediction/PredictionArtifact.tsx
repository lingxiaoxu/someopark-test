/**
 * PredictionArtifact — renders the right-panel content for every World Cup `wc_*`
 * artifact. One dispatcher + a set of real data viewers (no stubs), each fetching
 * the static JSON synced from prediction_market/data/output/ via getWC*.
 * Styling uses CSS vars / the .table & .card classes, so it inverts with the theme.
 */
import type { CSSProperties, ReactNode, ReactElement } from 'react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import {
  getWCChampion, getWCDivergence, getWCUpcoming, getWCPerformance,
  getWCRisk, getWCCalibration, getWCInplayLive, getWCOverview, getWCBacktest, getWCSquad, getWCParams, getWCForm,
  API_BASE,
} from '../../lib/api';
import { useState } from 'react';
import { PREDICTION_ITEMS } from './PredictionArtifactGrid';
import { tCountry } from '../../i18n/countries';
import { tDyn } from '../../i18n/predictionStrings';
import { usePoll } from './usePoll';

// ── shared primitives ─────────────────────────────────────────────────────────
const pct = (v?: number | null, d = 1) => (v == null || isNaN(v) ? '—' : `${(v * 100).toFixed(d)}%`);
const num = (v?: number | null, d = 3) => (v == null || isNaN(v) ? '—' : v.toFixed(d));
const money = (v: any) => (typeof v === 'number' ? `$${v.toFixed(2)}` : String(v ?? '—'));

const mono: CSSProperties = { fontFamily: 'var(--font-mono)' };

function Loading() { return <div className="text-xs py-3" style={{ color: 'var(--text-muted)', ...mono }}>Loading…</div>; }
function ErrorBox({ e }: { e: string }) {
  return <div className="text-xs py-3" style={{ color: 'var(--error)', ...mono }}>Failed to load: {e}. Run the exporter + <code>npm run sync:wc</code>.</div>;
}
// Heading is rendered (translated) by the dispatcher; Title keeps only the sub line.
function Title({ children, sub }: { children?: ReactNode; sub?: string }) {
  void children;
  return sub ? (
    <div className="mb-3" style={{ fontSize: 10, color: 'var(--text-muted)', ...mono }}>{sub}</div>
  ) : null;
}
function KV({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <table className="table" style={{ marginBottom: 12 }}>
      <tbody>
        {rows.map(([k, v], i) => (
          <tr key={i}><td style={{ fontWeight: 700, width: '50%' }}>{k}</td><td style={{ textAlign: 'right' }}>{v}</td></tr>
        ))}
      </tbody>
    </table>
  );
}
function DataTable({ cols, rows }: { cols: string[]; rows: ReactNode[][] }) {
  return (
    <table className="table">
      <thead><tr>{cols.map((c, i) => <th key={i} style={{ textAlign: i === 0 ? 'left' : 'right' }}>{c}</th>)}</tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>{r.map((cell, j) => <td key={j} style={{ textAlign: j === 0 ? 'left' : 'right' }}>{cell}</td>)}</tr>
        ))}
      </tbody>
    </table>
  );
}
function Notes({ items }: { items?: string[] }) {
  if (!items?.length) return null;
  return (
    <ul style={{ marginTop: 10, paddingLeft: 16, fontSize: 11, color: 'var(--text-muted)', ...mono }}>
      {items.map((n, i) => <li key={i} style={{ marginBottom: 4 }}>{tDyn(n)}</li>)}
    </ul>
  );
}

// ── viewers ───────────────────────────────────────────────────────────────────
function ChampionOdds() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCChampion(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const champ = (data?.champion ?? []).slice(0, 16);
  return (
    <div>
      <Title sub={`${tr('prediction.subChampion')} · ${data?.meta?.n_sims?.toLocaleString?.() ?? ''} sims`}>Champion Odds</Title>
      <DataTable cols={[tr('prediction.team'), 'FIFA', 'Grp', tr('prediction.colChamp'), tr('prediction.colFinal'), 'SF', tr('prediction.colRating')]}
        rows={champ.map((c: any) => [tCountry(c.name), c.fifa_rank != null ? `#${c.fifa_rank}` : '—', c.group, pct(c.p_champion), pct(c.p_final), pct(c.p_sf), num(c.rating, 3)])} />
    </div>
  );
}

function GoldenBoot() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCChampion(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const gb = (data?.golden_boot ?? []).slice(0, 16);
  return (
    <div>
      <Title sub={tr('prediction.subGoldenBoot')}>Golden Boot</Title>
      <DataTable cols={[tr('prediction.colPlayer'), tr('prediction.colTeam'), 'P(boot)', 'E[goals]']}
        rows={gb.map((p: any) => [p.name, tCountry(p.team), pct(p.p_golden_boot), num(p.e_goals, 2)])} />
    </div>
  );
}

function SquadStrength() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCSquad(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const teams = (data?.teams ?? []).slice(0, 24);
  return (
    <div>
      <Title sub={tr('prediction.subSquad')}>Squad Strength</Title>
      <DataTable cols={['#', tr('prediction.team'), tr('prediction.colSquadScore'), tr('prediction.colRating'), 'GA/90', tr('prediction.colTopPlayers')]}
        rows={teams.map((t: any) => [
          t.rank, tCountry(t.name), (t.score_z >= 0 ? '+' : '') + t.score_z.toFixed(2),
          t.mw_rating?.toFixed(2), t.ga_per90?.toFixed(2),
          (t.top_players ?? []).slice(0, 2).map((p: any) => `${p.name} (${p.goals}g)`).join(', '),
        ])} />
    </div>
  );
}

// Client-facing capability overview — what the system does across data / pre-match /
// in-play / discipline. Deliberately NO model parameters, methods or version numbers.
function Methodology() {
  const { t: tr } = useTranslation();
  const cap = tr('prediction.cap', { returnObjects: true }) as any;
  const sections: [string, string[]][] = [
    [cap?.dataT, cap?.data], [cap?.preT, cap?.pre], [cap?.liveT, cap?.live], [cap?.otherT, cap?.other],
  ];
  return (
    <div>
      <Title sub={tr('prediction.subMethodology')}>What This System Does</Title>
      {sections.filter(([t, items]) => t && Array.isArray(items)).map(([title, items]) => (
        <div key={title} style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: '.08em', textTransform: 'uppercase', color: 'var(--text-primary)', ...mono, marginBottom: 6 }}>{title}</div>
          <ul style={{ paddingLeft: 16, fontSize: 11.5, lineHeight: 1.6, color: 'var(--text-secondary)', ...mono }}>
            {items.map((it, i) => <li key={i} style={{ marginBottom: 3 }}>{it}</li>)}
          </ul>
        </div>
      ))}
    </div>
  );
}

function Divergence() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any[]>(() => getWCDivergence(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const rows = (data ?? []);
  return (
    <div>
      <Title sub={tr('prediction.subDivergence')}>Model vs Market</Title>
      <DataTable cols={[tr('prediction.match'), 'Model H/D/A', 'Book H/D/A', tr('prediction.colEdge')]}
        rows={rows.map((m: any) => {
          const e = m.edge_vs_book || {};
          const best = Math.max(Math.abs(e.home ?? 0), Math.abs(e.draw ?? 0), Math.abs(e.away ?? 0));
          const side = best === Math.abs(e.home ?? 0) ? 'H' : best === Math.abs(e.draw ?? 0) ? 'D' : 'A';
          const val = side === 'H' ? e.home : side === 'D' ? e.draw : e.away;
          return [
            `${tCountry(m.home)} v ${tCountry(m.away)}`,
            `${pct(m.model?.home, 0)}/${pct(m.model?.draw, 0)}/${pct(m.model?.away, 0)}`,
            `${pct(m.book_devig?.home, 0)}/${pct(m.book_devig?.draw, 0)}/${pct(m.book_devig?.away, 0)}`,
            <span style={{ color: Math.abs(val) >= 0.05 ? 'var(--success)' : 'var(--text-muted)' }}>{side} {val >= 0 ? '+' : ''}{pct(val, 1)}</span>,
          ];
        })} />
    </div>
  );
}

function Predictions() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCUpcoming(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const ms = data?.matches ?? [];
  return (
    <div>
      <Title sub={tr('prediction.subPredictions')}>Today's Predictions</Title>
      <DataTable cols={[tr('prediction.match'), 'ET', 'H', 'D', 'A', 'O2.5']}
        rows={ms.map((m: any) => [`${tCountry(m.home?.name)} v ${tCountry(m.away?.name)}`, m.et ?? '', pct(m.model?.home, 0), pct(m.model?.draw, 0), pct(m.model?.away, 0), pct(m.model?.over_2_5, 0)])} />
      <Legend />
    </div>
  );
}

// Explains the column abbreviations (H / D / A / O2.5 / BTTS) inline, so the
// prediction tables are self-documenting in every language.
function Legend() {
  const { t: tr } = useTranslation();
  return (
    <div style={{ marginTop: 10, fontSize: 10, lineHeight: 1.5, color: 'var(--text-muted)', ...mono }}>
      {tr('prediction.legendAbbrev')}
    </div>
  );
}

function MatchPricing() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCUpcoming(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const ms = data?.matches ?? [];
  return (
    <div>
      <Title sub={tr('prediction.subMatchPricing')}>Match Pricing</Title>
      {ms.map((m: any, i: number) => (
        <div key={i} className="card" style={{ marginBottom: 10 }}>
          <div style={{ fontWeight: 700, fontSize: 12, ...mono, marginBottom: 6 }}>{tCountry(m.home?.name)} vs {tCountry(m.away?.name)} <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>{m.et}</span></div>
          <DataTable cols={['', tr('prediction.home'), tr('prediction.draw'), tr('prediction.away')]}
            rows={[
              [tr('prediction.model'), pct(m.model?.home, 0), pct(m.model?.draw, 0), pct(m.model?.away, 0)],
              [tr('prediction.kalshiAsk'), num(m.kalshi?.home?.ask, 2), num(m.kalshi?.draw?.ask, 2), num(m.kalshi?.away?.ask, 2)],
              [tr('prediction.polyUsAsk'), num(m.poly_us?.home?.ask, 2), num(m.poly_us?.draw?.ask, 2), num(m.poly_us?.away?.ask, 2)],
            ]} />
        </div>
      ))}
    </div>
  );
}

function Schedule() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCUpcoming(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const ms = data?.matches ?? [];
  return (
    <div>
      <Title sub={tr('prediction.subSchedule')}>Schedule</Title>
      <DataTable cols={['ET', 'Round', tr('prediction.match')]}
        rows={ms.map((m: any) => [m.et ?? m.kickoff, m.round ?? '', `${tCountry(m.home?.name)} v ${tCountry(m.away?.name)}`])} />
    </div>
  );
}

const KIND_COLOR: Record<string, string> = {
  lock_arb: 'var(--success)', relative_value: 'var(--text-primary)', tactic: 'var(--text-secondary)',
};

function InPlay() {
  const { t: tr } = useTranslation();
  // Polls every 30s so the live model + tricks refresh during a match (no reload).
  const { data, loading, updatedAt } = usePoll<any>(() => getWCInplayLive(), 30000);
  const matches = data?.matches ?? [];
  const upd = updatedAt ? new Date(updatedAt).toLocaleTimeString() : '';
  return (
    <div>
      <Title sub={tr('prediction.subInPlay')}>In-Play Arbitrage</Title>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 8, ...mono }}>
        ● {tr('prediction.autoRefresh')} 30s{upd ? ` · ${tr('prediction.updated')} ${upd}` : ''} · {data?.n_live ?? 0} {tr('prediction.live')}
      </div>
      {loading && !matches.length ? <Loading /> : !matches.length ? (
        <div className="text-xs py-2" style={{ color: 'var(--text-muted)', ...mono }}>{tr('prediction.noLiveMatches')}</div>
      ) : matches.map((m: any) => (
        <div key={m.fixture_id} className="card" style={{ marginBottom: 12 }}>
          {/* live header */}
          <div className="flex items-center justify-between" style={{ marginBottom: 6 }}>
            <span style={{ fontWeight: 700, fontSize: 13, ...mono, color: 'var(--text-primary)' }}>
              <span style={{ color: 'var(--error)', fontWeight: 700, marginRight: 6 }} className="pulse">● {tr('prediction.liveBadge')}</span>
              {tCountry(m.home.name)} <b>{m.score}</b> {tCountry(m.away.name)}
            </span>
            <span style={{ fontSize: 11, color: 'var(--text-muted)', ...mono }}>{m.minute}'{m.reds !== '0-0' ? ` · 🟥 ${m.reds}` : ''}</span>
          </div>
          {/* live model */}
          <div style={{ fontSize: 11, ...mono, color: 'var(--text-secondary)', marginBottom: 2 }}>
            {tr('prediction.model')}: H {pct(m.model.home, 0)} · D {pct(m.model.draw, 0)} · A {pct(m.model.away, 0)} · O2.5 {pct(m.model.over_2_5, 0)}
          </div>
          <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginBottom: 6 }}>
            xG {m.xg.home ?? '—'} / {m.xg.away ?? '—'} · {tr('prediction.expGoals')} {num(m.model.exp_remaining_goals, 2)}
          </div>
          {/* opportunities / tricks */}
          {m.opportunities?.length ? (
            <DataTable cols={[tr('prediction.colKind'), tr('prediction.colAction'), tr('prediction.colSide'), tr('prediction.colEdge'), tr('prediction.colReason')]}
              rows={m.opportunities.map((o: any) => [
                <span style={{ color: KIND_COLOR[o.kind] ?? 'var(--text-secondary)', fontWeight: 700 }}>{o.kind}</span>,
                o.action, o.side, o.edge != null ? num(o.edge, 3) : '—', (o.reason || '').slice(0, 60),
              ])} />
          ) : <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono }}>{tr('prediction.noOpps')}</div>}
        </div>
      ))}
    </div>
  );
}

function PerformanceCard() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCPerformance(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  // Gate verdict comes from the CALIBRATED Brier (trade_grade), not the raw model —
  // the raw model is over-confident on this small sample but calibration recovers it.
  const pass = !!data?.trade_grade;
  const hasCal = data?.calibrated_brier != null;
  return (
    <div>
      <Title sub={tr('prediction.subPerformance')}>Accuracy & P&L</Title>
      <KV rows={[
        [tr('prediction.lblSettled'), data?.n_settled],
        [tr('prediction.lblBrierBetter'), `${num(data?.brier, 4)} vs uniform ${num(data?.brier_uniform, 4)}`],
        ...(hasCal ? [[tr('prediction.lblBrierCalibrated'), <span style={{ color: pass ? 'var(--success)' : 'var(--ink)' }}>{`${num(data?.calibrated_brier, 4)} ≤ uniform ${num(data?.brier_uniform, 4)}`}</span>] as [string, ReactNode]] : []),
        ['Log-loss', num(data?.log_loss, 4)],
        [tr('prediction.lblFavHit'), pct(data?.favourite_hit_rate, 0)],
        [tr('prediction.lblCalibPnl'), `${num(data?.calibration_pnl, 2)}u (${num(data?.calibration_pnl_per_bet, 3)}u/bet)`],
        [tr('prediction.lblTradeGrade'), <span style={{ color: pass ? 'var(--success)' : 'var(--error)', fontWeight: 700 }}>{pass ? tr('prediction.gradePassCalibrated') : tr('prediction.gradeBlock')}</span>],
      ]} />
      <BetLog data={data} />
      <Notes items={data?.notes} />
    </div>
  );
}

// Production track record: flat 1u on our predicted outcome every match since the
// opener, settled at the closing book odds — what we predicted, bet, and the result.
function BetLog({ data }: { data: any }) {
  const { t: tr } = useTranslation();
  const log: any[] = data?.bet_log ?? [];
  if (!log.length) return null;
  const pnl = data?.pnl_units ?? 0;
  const pnlColor = pnl > 0 ? 'var(--success)' : pnl < 0 ? 'var(--error)' : 'var(--ink)';
  const sideLabel = (b: any) => (b.pick === 'draw' ? tr('prediction.drawResult') : tCountry(b.pick_team));
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ fontSize: 11, fontFamily: 'var(--font-mono)', marginBottom: 6, color: 'var(--text-secondary)' }}>
        {tr('prediction.lblTrackRecord')}: <b>{data.pnl_record}</b> · <b style={{ color: pnlColor }}>{pnl > 0 ? '+' : ''}{num(pnl, 2)}u</b>
        {' '}({pct(data.pnl_roi, 1)} ROI) · {tr('prediction.colDate')} ≥ {data.bet_since}
      </div>
      <DataTable
        cols={[tr('prediction.colDate'), tr('prediction.colMatchup'), tr('prediction.colOurPick'), tr('prediction.colResult'), 'Odds', 'P&L', 'Cum']}
        rows={log.map((b: any) => [
          b.date?.slice(5),
          `${tCountry(b.home)} ${b.score} ${tCountry(b.away)}`,
          sideLabel(b),
          <span style={{ color: b.won ? 'var(--success)' : 'var(--error)', fontWeight: 700 }}>{b.won ? tr('prediction.betWon') : tr('prediction.betLost')}</span>,
          num(b.dec_odds, 2),
          <span style={{ color: b.pnl >= 0 ? 'var(--success)' : 'var(--error)' }}>{b.pnl >= 0 ? '+' : ''}{num(b.pnl, 2)}</span>,
          <span style={{ color: b.cum_pnl >= 0 ? 'var(--success)' : 'var(--error)' }}>{b.cum_pnl >= 0 ? '+' : ''}{num(b.cum_pnl, 2)}</span>,
        ])} />
    </div>
  );
}

function RiskCard() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCRisk(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const g = data?.gates ?? {}, b = data?.venue_balances ?? {}, ab = data?.api_budget ?? {};
  return (
    <div>
      <Title sub={tr('prediction.subRisk')}>Risk Report</Title>
      <KV rows={[
        [tr('prediction.lblKalshiEnv'), g.kalshi_env],
        [tr('prediction.lblKalshiTrading'), String(g.kalshi_trading_enabled)],
        [tr('prediction.lblPolyUsTrading'), String(g.pmus_trading_enabled)],
        [tr('prediction.lblOrderCap'), money(g.hard_order_cap_usd)],
        [tr('prediction.lblKalshiDemo'), money(b.kalshi_demo_usd)],
        ['Poly US', money(b.polymarket_us_usd)],
        [tr('prediction.lblKalshiProd'), tDyn(String(b.kalshi_prod_usd))],
        [tr('prediction.lblApiBudget'), `${ab.used}/${ab.cap} (${pct(ab.pct, 0)})`],
        [tr('prediction.lblCalibration'), <span style={{ color: data?.calibration_gate?.trade_grade ? 'var(--success)' : 'var(--error)' }}>{tDyn(data?.calibration_gate?.status)}</span>],
      ]} />
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.1em', margin: '8px 0 4px', ...mono, color: 'var(--text-primary)' }}>{tr('prediction.secBlocked')}</div>
      <ul style={{ paddingLeft: 16, fontSize: 11, color: 'var(--error)', ...mono }}>
        {(data?.blocked_summary ?? []).map((x: string, i: number) => <li key={i} style={{ marginBottom: 3 }}>{tDyn(x)}</li>)}
      </ul>
    </div>
  );
}

function Calibration() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCCalibration(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  return (
    <div>
      <Title sub={tr('prediction.subCalibration')}>Calibration (OOS)</Title>
      <KV rows={[
        [tr('prediction.lblMatches'), data?.n_matches],
        ['Brier', `${num(data?.brier, 4)} (uniform ${num(data?.brier_uniform, 4)})`],
        ['Log-loss', num(data?.log_loss, 4)],
        [tr('prediction.lblFavHit'), pct(data?.favourite_hit_rate, 0)],
        [tr('prediction.lblPredObsDraw'), `${pct(data?.pred_draw_rate, 0)} vs ${pct(data?.obs_draw_rate, 0)}`],
        [tr('prediction.lblAvgGoals'), `${num(data?.pred_avg_total_goals, 2)} / ${num(data?.obs_avg_total_goals, 2)}`],
      ]} />
      <Notes items={Array.isArray(data?.notes) ? data.notes : data?.notes ? [data.notes] : []} />
    </div>
  );
}

function ParamSweep() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCParams(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const fmtParams = (p: any) => Object.entries(p ?? {}).map(([k, v]) => `${k.replace('_', ' ')}=${v}`).join('  ');
  const all = (data?.results_all ?? []).slice(0, 40);   // top 40 of the 180 (ranked)
  return (
    <div>
      <Title sub={tr('prediction.subParams')}>Parameter Sweep</Title>
      <KV rows={[
        [tr('prediction.lblSelected'), <b style={{ color: 'var(--success)' }}>{data?.best?.brier?.toFixed?.(4)}</b>],
        [tr('prediction.lblVsCurrent'), data?.baseline?.brier],
        [tr('prediction.lblVsUniform'), data?.uniform_brier],
        [tr('prediction.colParams'), <span style={{ fontSize: 10 }}>{fmtParams(data?.best?.params)}</span>],
      ]} />
      <div style={{ fontSize: 11, color: 'var(--text-muted)', ...mono, margin: '4px 0 10px' }}>{tr('prediction.paramsWhy')}</div>
      <DataTable cols={['#', 'Brier', 'Acc', tr('prediction.colParams'), '>uni?']}
        rows={all.map((r: any) => [
          r.rank, r.brier, r.acc, <span style={{ fontSize: 10 }}>{fmtParams(r.params)}</span>,
          r.beats_uniform ? <span style={{ color: 'var(--success)' }}>✓</span> : <span style={{ color: 'var(--error)' }}>✗</span>,
        ])} />
    </div>
  );
}

function Backtest() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCBacktest(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const b = data?.brier ?? {};
  const pass = !!data?.trade_grade;
  return (
    <div>
      <Title sub={tr('prediction.subBacktest')}>Backtest (OOS)</Title>
      <KV rows={[
        [tr('prediction.lblSettled'), <b>{data?.n_settled}</b>],
        [tr('prediction.lblModelBrier'), <b style={{ color: pass ? 'var(--success)' : 'var(--error)' }}>{b.model}</b>],
        [tr('prediction.lblModelRaw'), b.model_raw ?? '—'],
        [tr('prediction.lblBookBrier'), b.book],
        ['Brier (uniform)', b.uniform],
        [tr('prediction.lblDrawRate'), data?.draw_rate != null ? pct(data.draw_rate, 0) : '—'],
        [tr('prediction.lblFavHit'), `${tr('prediction.model')} ${data?.accuracy?.model_fav_hit} · book ${data?.accuracy?.book_fav_hit}`],
      ]} />
      {/* blend curve — shows blending toward the book does NOT help */}
      {!!(data?.blend_curve?.length) && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, margin: '2px 0 8px' }}>
          {tr('prediction.lblBlend')}: {data.blend_curve.map((c: any) => `${Math.round(c.w * 100)}%→${c.brier}`).join('  ')}
        </div>
      )}
      <div style={{ fontSize: 11, color: pass ? 'var(--success)' : 'var(--error)', ...mono, marginBottom: 10, fontWeight: 700 }}>
        {pass ? tr('prediction.backtestPass') : tr('prediction.backtestVerdict')}
      </div>
      <DataTable cols={[tr('prediction.match'), 'Score', tr('prediction.colResult'), tr('prediction.model'), 'Book']}
        rows={(data?.matches ?? []).map((m: any) => [
          `${tCountry(m.home)} v ${tCountry(m.away)}`, m.score, m.result,
          `${m.model_pick} ${m.model_p != null ? pct(m.model_p, 0) : ''}`,
          m.book_pick ? `${m.book_pick} ${pct(m.book_p, 0)}` : '—',
        ])} />
    </div>
  );
}

function OverviewCard() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCOverview(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  return (
    <div>
      <Title sub={data?.as_of ? `as of ${data.as_of}` : undefined}>System Overview</Title>
      <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 10, ...mono }}>{tDyn(data?.headline)}</div>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.1em', margin: '8px 0 4px', ...mono }}>{tr('prediction.secInterfaces')}</div>
      <DataTable cols={[tr('prediction.colCat'), tr('prediction.colCommand'), tr('prediction.colPurpose')]} rows={(data?.interfaces ?? []).map((i: any) => [tDyn(i.category), i.command?.replace('python -m prediction_market.', ''), tDyn(i.purpose)])} />
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.1em', margin: '10px 0 4px', ...mono }}>{tr('prediction.secSchedule')}</div>
      <DataTable cols={[tr('prediction.colWhen'), tr('prediction.colRuns'), tr('prediction.colFreq')]} rows={(data?.schedule ?? []).map((s: any) => [tDyn(s.when), s.runs, tDyn(s.frequency)])} />
    </div>
  );
}

function Venues() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCRisk(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const g = data?.gates ?? {}, b = data?.venue_balances ?? {};
  return (
    <div>
      <Title sub={tr('prediction.subVenues')}>Venues & Gates</Title>
      <DataTable cols={[tr('prediction.colVenue'), tr('prediction.colRole'), tr('prediction.colBalance'), tr('prediction.colTrading')]}
        rows={[
          ['Kalshi (demo)', tr('prediction.roleExecute'), money(b.kalshi_demo_usd), String(g.kalshi_trading_enabled)],
          ['Polymarket US', tr('prediction.roleExecute'), money(b.polymarket_us_usd), String(g.pmus_trading_enabled)],
          ['Kalshi (prod)', tr('prediction.roleRealMoney'), tDyn(String(b.kalshi_prod_usd)), tr('prediction.tradingGated')],
        ]} />
      <div style={{ fontSize: 11, color: 'var(--text-muted)', ...mono }}>{tr('prediction.lblExecutable')}: {(g.executable_venues ?? []).join(', ')}</div>
    </div>
  );
}

function Budget() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCRisk(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const ab = data?.api_budget ?? {};
  const frac = Math.min(1, (ab.used ?? 0) / (ab.cap ?? 1));
  return (
    <div>
      <Title sub={tr('prediction.subBudget')}>API Budget / Health</Title>
      <KV rows={[[tr('prediction.lblUsed'), ab.used], [tr('prediction.lblCap'), ab.cap], [tr('prediction.lblUtilisation'), pct(ab.pct, 0)]]} />
      <div style={{ height: 10, background: 'var(--bg-tertiary)', border: '1px solid var(--border-subtle)' }}>
        <div style={{ width: `${frac * 100}%`, height: '100%', background: frac > 0.8 ? 'var(--error)' : 'var(--success)' }} />
      </div>
    </div>
  );
}

function FormCard() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCForm(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const teams = (data?.teams ?? []).slice(0, 24);
  return (
    <div>
      <Title sub={tr('prediction.subForm')}>Recent Form</Title>
      <DataTable cols={['#', tr('prediction.team'), tr('prediction.colForm'), 'wGD', tr('prediction.colRecent')]}
        rows={teams.map((t: any) => [
          t.rank, tCountry(t.name), (t.form_z >= 0 ? '+' : '') + t.form_z.toFixed(2),
          (t.weighted_gd >= 0 ? '+' : '') + t.weighted_gd.toFixed(2),
          (t.recent ?? []).join(' '),
        ])} />
    </div>
  );
}

// Two institutional-style PDF reports (PnL track record + Risk), displayed INLINE
// via the local Express server over the Cloudflare tunnel ({API_BASE}/data/*.pdf,
// served without auth), not just a download. Mirrors the stock-mode report viewers.
function Pdfs() {
  const { t: tr } = useTranslation();
  const reports = [
    { key: 'pnl', file: 'performance_report.pdf', label: tr('prediction.pdfPerf') },
    { key: 'risk', file: 'risk_report.pdf', label: tr('prediction.pdfRisk') },
  ];
  const [active, setActive] = useState('pnl');
  const cur = reports.find((r) => r.key === active) ?? reports[0];
  const url = `${API_BASE}/data/${cur.file}`;
  const tab = (on: boolean): CSSProperties => ({
    padding: '6px 14px', border: '2px solid var(--ink)', cursor: 'pointer', ...mono, fontSize: 12, fontWeight: 700,
    background: on ? 'var(--ink)' : 'var(--paper)', color: on ? 'var(--paper)' : 'var(--ink)', marginRight: 8,
  });
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Title sub={tr('prediction.subPdfs')}>Reports</Title>
      <div className="flex items-center justify-between" style={{ marginBottom: 10 }}>
        <div>
          {reports.map((r) => (
            <button key={r.key} style={tab(r.key === active)} onClick={() => setActive(r.key)}>{r.label}</button>
          ))}
        </div>
        <a href={url} target="_blank" rel="noreferrer" style={{ ...mono, fontSize: 11, color: 'var(--text-muted)', textDecoration: 'underline' }}>
          {tr('common.open')} ↗
        </a>
      </div>
      {/* Fill to the bottom edge: viewport-relative min-height so the viewer is tall
          regardless of the flex chain through the panel's overflow-auto container. */}
      <div style={{ flex: 1, minHeight: 'calc(100vh - 150px)', border: '2px solid var(--ink)', background: '#fff' }}>
        <iframe key={cur.key} src={url} title={cur.label} style={{ width: '100%', height: '100%', minHeight: 'calc(100vh - 150px)', border: 'none' }} />
      </div>
    </div>
  );
}

// ── dispatcher ────────────────────────────────────────────────────────────────
const REGISTRY: Record<string, () => ReactElement> = {
  wc_champion: ChampionOdds,
  wc_golden_boot: GoldenBoot,
  wc_squad: SquadStrength,
  wc_form: FormCard,
  wc_methodology: Methodology,
  wc_divergence: Divergence,
  wc_predictions: Predictions,
  wc_match_pricing: MatchPricing,
  wc_schedule: Schedule,
  wc_inplay: InPlay,
  wc_performance: PerformanceCard,
  wc_risk: RiskCard,
  wc_calibration: Calibration,
  wc_backtest: Backtest,
  wc_params: ParamSweep,
  wc_overview: OverviewCard,
  wc_venues: Venues,
  wc_budget: Budget,
  wc_pdfs: Pdfs,
};

const KEY_BY_TYPE: Record<string, string> = Object.fromEntries(PREDICTION_ITEMS.map(i => [i.type, i.i18nKey]));

export default function PredictionArtifact({ type }: { type: string }) {
  const { t } = useTranslation();
  const View = REGISTRY[type];
  if (!View) return <div className="text-xs py-3" style={{ color: 'var(--text-muted)', ...mono }}>Unknown artifact: {type}</div>;
  const key = KEY_BY_TYPE[type];
  return (
    <div>
      {key && <div style={{ fontSize: 13, fontWeight: 700, letterSpacing: '.1em', textTransform: 'uppercase', color: 'var(--text-primary)', marginBottom: 6, ...mono }}>{t(`prediction.${key}`)}</div>}
      <View />
    </div>
  );
}

export const isPredictionArtifact = (type?: string) => !!type && type.startsWith('wc_');
