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
  getWCRisk, getWCCalibration, getWCInplayLive, getWCInplayLiveAdvance, getWCOverview, getWCBacktest, getWCSquad, getWCParams, getWCForm,
  getWCMilestoneMarks, getWCSchedule, getWCReachRound, getWCStyles, API_BASE,
  getWCMicrofootball, analyzeMicrofootball, getWCDfm,
} from '../../lib/api';
import { useState, useRef, useEffect } from 'react';
import { TrajectoryPlayer } from './TrajectoryPlayer';
import { PREDICTION_ITEMS } from './PredictionArtifactGrid';
import { tCountry, countryKey } from '../../i18n/countries';
import CountryName from './CountryName';
import { PredictionFocusContext, usePredictionFocus, useCountryFocusScroll } from '../../contexts/PredictionFocusContext';
import { tDyn, overviewHeadline } from '../../i18n/predictionStrings';
import { usePoll } from './usePoll';
import { AdvanceModeToggle, useAdvanceMode } from './AdvanceMode';

// ── shared primitives ─────────────────────────────────────────────────────────
const pct = (v?: number | null, d = 1) => (v == null || isNaN(v) ? '—' : `${(v * 100).toFixed(d)}%`);
const num = (v?: number | null, d = 3) => (v == null || isNaN(v) ? '—' : v.toFixed(d));
const money = (v: any) => (typeof v === 'number' ? `$${v.toFixed(2)}` : String(v ?? '—'));
// per-contract cents: cc() formats an already-¢ value, pcent() converts a 0–1 prob → ¢.
const cc = (v?: number | null, d = 0) => (v == null || isNaN(v) ? '—' : `${v.toFixed(d)}¢`);
const pcent = (v?: number | null, d = 0) => (v == null || isNaN(v) ? '—' : `${(v * 100).toFixed(d)}¢`);

const mono: CSSProperties = { fontFamily: 'var(--font-mono)' };

function Loading() { return <div className="text-xs py-3" style={{ color: 'var(--text-muted)', ...mono }}>Loading…</div>; }
function ErrorBox({ e }: { e: string }) {
  return <div className="text-xs py-3" style={{ color: 'var(--error)', ...mono }}>Failed to load: {e}. Run the exporter + <code>npm run sync:wc</code>.</div>;
}
// Heading is rendered (translated) by the dispatcher; Title keeps only the sub line.
function Title({ children, sub, right }: { children?: ReactNode; sub?: string; right?: ReactNode }) {
  void children;
  if (!sub && !right) return null;
  // `right` (e.g. the Regulation/Advances selector) sits on the SAME row as the subtitle,
  // right-aligned, so the control lines up with the view's small header instead of below it.
  return (
    <div className="mb-3 flex items-center justify-between" style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, minHeight: 22 }}>
      <span>{sub}</span>
      {right}
    </div>
  );
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
// Optional column sorting: pass `sortableCols` (clickable column indices) and `sortVals`
// (raw comparable value per row per col — strings sort lexically, numbers numerically, nulls
// last). One click ascending, click again descending. `defaultSort` sets the initial order
// (and shows its arrow) without a click — used to surface a view's natural ordering.
function DataTable({ cols, rows, className, sortableCols, sortVals, defaultSort }: {
  cols: ReactNode[]; rows: ReactNode[][]; className?: string;
  sortableCols?: number[];
  sortVals?: (number | string | null | undefined)[][];
  defaultSort?: { col: number; dir: 'asc' | 'desc' };
}) {
  const [sort, setSort] = useState<{ col: number; dir: 'asc' | 'desc' } | null>(defaultSort ?? null);
  const canSort = new Set(sortableCols ?? []);
  const order = rows.map((_, i) => i);
  if (sort && canSort.has(sort.col)) {
    const acc = (i: number) => (sortVals ? sortVals[i]?.[sort.col] : (rows[i]?.[sort.col] as any));
    order.sort((a, b) => {
      const va = acc(a), vb = acc(b);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;            // nulls last (ascending)
      if (vb == null) return -1;
      if (typeof va === 'number' && typeof vb === 'number') return va - vb;
      return String(va).localeCompare(String(vb));
    });
    if (sort.dir === 'desc') order.reverse();
  }
  const click = (j: number) => {
    if (!canSort.has(j)) return;
    setSort((s) => (s && s.col === j ? { col: j, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { col: j, dir: 'asc' }));
  };
  const arrow = (j: number) => (sort?.col === j ? (sort.dir === 'asc' ? ' ▲' : ' ▼') : '');
  return (
    <table className={className ? `table ${className}` : 'table'}>
      <thead><tr>{cols.map((c, i) => (
        <th key={i} onClick={() => click(i)}
          style={{ textAlign: i === 0 ? 'left' : 'right', cursor: canSort.has(i) ? 'pointer' : undefined, userSelect: canSort.has(i) ? 'none' : undefined }}>
          {c}{arrow(i)}
        </th>
      ))}</tr></thead>
      <tbody>
        {order.map((ri) => (
          <tr key={ri}>{rows[ri].map((cell, j) => <td key={j} style={{ textAlign: j === 0 ? 'left' : 'right' }}>{cell}</td>)}</tr>
        ))}
      </tbody>
    </table>
  );
}
function Notes({ items, i18nItems }: { items?: string[]; i18nItems?: { key: string; args?: any }[] }) {
  const { t } = useTranslation();
  // Prefer the structured {key,args} notes → rendered in the active language; fall back to
  // the English prose (tDyn) for any note without a template / older data.
  const list: string[] = (i18nItems && i18nItems.length)
    ? i18nItems.map((n, i) => t('prediction.note.' + n.key, { ...(n.args || {}), defaultValue: items?.[i] ?? '' }) as string)
    : (items ?? []);
  if (!list.length) return null;
  return (
    <ul style={{ marginTop: 10, paddingLeft: 16, fontSize: 11, color: 'var(--text-muted)', ...mono }}>
      {list.map((n, i) => <li key={i} style={{ marginBottom: 4 }}>{n}</li>)}
    </ul>
  );
}

// Clickable country label for the cross-artifact navigator — drop-in for tCountry() at
// country render sites; also the scroll/highlight anchor. VS renders a "Home <sep> Away"
// pair. Both return nodes, so they slot into DataTable cells (ReactNode) and JSX alike.
const CN = (code?: string | null) => <CountryName code={code} />;
const VS = (h?: string | null, a?: string | null, sep: string = ' v ') => <>{CN(h)}{sep}{CN(a)}</>;

// ── viewers ───────────────────────────────────────────────────────────────────
function ChampionOdds() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCChampion(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const champ = (data?.champion ?? []);  // all 48 teams
  return (
    <div>
      <Title sub={`${tr('prediction.subChampion')} · ${data?.meta?.n_sims?.toLocaleString?.() ?? ''} sims`}>Champion Odds</Title>
      <DataTable cols={[tr('prediction.team'), 'FIFA', 'Grp', tr('prediction.colChamp'), 'Kalshi¢', 'Poly¢', tr('prediction.colFinal'), 'SF', tr('prediction.colRating')]}
        rows={champ.map((c: any) => [CN(c.name), c.fifa_rank != null ? `#${c.fifa_rank}` : '—', c.group, pct(c.p_champion),
          cc(c.kalshi_champ_c), cc(c.poly_champ_c), pct(c.p_final), pct(c.p_sf), num(c.rating, 3)])}
        sortableCols={[1, 2, 3, 4, 5, 6, 7, 8]}
        sortVals={champ.map((c: any) => [null, c.fifa_rank, c.group, c.p_champion, c.kalshi_champ_c, c.poly_champ_c, c.p_final, c.p_sf, c.rating])}
        defaultSort={{ col: 3, dir: 'desc' }} />
      <div style={{ marginTop: 8, fontSize: 10, color: 'var(--text-muted)', ...mono }}>{tr('prediction.dualUnitLegend')}</div>
    </div>
  );
}

function ReachRound() {
  const { t: tr } = useTranslation();
  // Re-fetched on every open (artifact remounts) + cache-busted in getWCReachRound.
  const { data, loading, error } = useApi<any>(() => getWCReachRound(), []);
  // Which of the 7 sortable columns is active + direction (null = default reach-strength order).
  const [sort, setSort] = useState<{ key: string; dir: 'asc' | 'desc' } | null>(null);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const rounds: any[] = data?.rounds ?? [];
  const roundLabel: Record<string, string> = {
    advance: tr('prediction.rrAdvance'), r16: tr('prediction.rrR16'),
    qf: tr('prediction.rrQF'), sf: tr('prediction.rrSF'), final: tr('prediction.rrFinal'),
  };
  // Pivot rounds → one row per team (48), columns grouped by round. group_gd / group_played /
  // group_rank are per-team (identical across rounds) → captured onto the row for the
  // 净胜球(完赛场次/排名) column.
  const teamMap: Record<string, any> = {};
  rounds.forEach((r) => (r.teams ?? []).forEach((t: any) => {
    const e = teamMap[t.team_id] || (teamMap[t.team_id] = { name: t.name, byRound: {} });
    e.byRound[r.key] = t;
    if (t.group_gd != null) { e.gd = t.group_gd; e.played = t.group_played; e.rank = t.group_rank; }
    if (t.group != null) { e.group = t.group; e.points = t.group_points; }
  }));
  // "+1 (2=1) (#3)" — signed group GD, then (played=matches-still-to-play), then (#in-group rank).
  // Half-width parens (CJK full-width ones are too wide). Group stage is 3 matches, so to-play = 3 − played.
  const gdLabel = (e: any) => {
    if (e.gd == null) return '—';
    const sign = e.gd > 0 ? '+' : '';
    const left = `(${e.played ?? 0}=${Math.max(0, 3 - (e.played ?? 0))})`;
    const right = e.rank != null ? ` (#${e.rank})` : '';
    return `${sign}${e.gd} ${left}${right}`;
  };
  const strength = (e: any) => rounds.reduce((s, r) => s + (e.byRound[r.key]?.model_pct ?? 0), 0);
  let teams = Object.values(teamMap).sort((a: any, b: any) => strength(b) - strength(a));
  // 7 sortable columns: 'group' (group letter asc, then points desc within a group), 'gd'
  // (net goal difference), and each round's model% ('round:<key>'). One click = ascending,
  // click again = descending (the whole order reverses, so 'group' desc = L→A, points asc).
  if (sort) {
    const arr = [...teams] as any[];
    if (sort.key === 'group') {
      arr.sort((a, b) => (a.group || '').localeCompare(b.group || '') || (b.points ?? -1) - (a.points ?? -1));
    } else {
      const val = (e: any) => (sort.key === 'gd' ? (e.gd ?? -999) : (e.byRound[sort.key.slice(6)]?.model_pct ?? -1));
      arr.sort((a, b) => val(a) - val(b));
    }
    if (sort.dir === 'desc') arr.reverse();
    teams = arr;
  }
  const clickSort = (key: string) =>
    setSort((s) => (s && s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' }));
  const arrow = (key: string) => (sort?.key === key ? (sort.dir === 'asc' ? ' ▲' : ' ▼') : '');
  const sortable = { cursor: 'pointer', userSelect: 'none' as const };
  const asOf = data?.as_of ? new Date(data.as_of).toLocaleString() : '';
  const bd = '1px solid var(--border-subtle)';
  const th: any = { fontSize: 9.5, fontWeight: 700, padding: '4px 6px', textAlign: 'right', color: 'var(--text-muted)', whiteSpace: 'nowrap' };
  const td: any = { fontSize: 10, padding: '3px 6px', textAlign: 'right', whiteSpace: 'nowrap' };
  const edgeCell = (t: any) => (t?.edge != null
    ? <span style={{ color: t.tradable ? 'var(--success)' : 'var(--text-muted)', fontWeight: t.tradable ? 700 : 400 }}>{t.edge >= 0 ? '+' : ''}{pct(t.edge, 0)}{t.tradable ? '★' : ''}</span>
    : <span style={{ color: 'var(--text-muted)' }}>—</span>);
  return (
    <div>
      <Title sub={tr('prediction.subReachRound')}>Reach Round</Title>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginBottom: 6 }}>{tr('prediction.rrAsOf')}: {asOf}</div>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ borderCollapse: 'collapse', ...mono }}>
          <thead>
            <tr style={{ borderBottom: bd }}>
              <th style={{ ...th, textAlign: 'left' }} rowSpan={2}>{tr('prediction.team')}</th>
              <th style={{ ...th, ...sortable, textAlign: 'right', borderLeft: bd, color: 'var(--text-primary)', whiteSpace: 'pre-line', lineHeight: 1.15, padding: '4px 3px', fontSize: 8.5 }} rowSpan={2} onClick={() => clickSort('group')}>{tr('prediction.rrGroupPts')}{arrow('group')}</th>
              <th style={{ ...th, ...sortable, textAlign: 'right', borderLeft: bd, color: 'var(--text-primary)', whiteSpace: 'pre-line', lineHeight: 1.15, padding: '4px 3px', fontSize: 8.5 }} rowSpan={2} onClick={() => clickSort('gd')}>{tr('prediction.rrGdGp')}{arrow('gd')}</th>
              {rounds.map((r) => <th key={r.key} colSpan={4} style={{ ...th, textAlign: 'center', color: 'var(--text-primary)', borderLeft: bd }}>{roundLabel[r.key] ?? r.label}</th>)}
            </tr>
            <tr style={{ borderBottom: bd }}>
              {rounds.map((r) => [
                <th key={r.key + 'm'} style={{ ...th, ...sortable, borderLeft: bd }} onClick={() => clickSort('round:' + r.key)}>{tr('prediction.rrModel')}{arrow('round:' + r.key)}</th>,
                <th key={r.key + 'k'} style={th}>K¢</th>,
                <th key={r.key + 'p'} style={th}>P¢</th>,
                <th key={r.key + 'e'} style={th}>{tr('prediction.colEdge')}</th>,
              ])}
            </tr>
          </thead>
          <tbody>
            {teams.map((e: any, i: number) => (
              <tr key={i} style={{ borderBottom: '1px solid var(--hairline)' }}>
                <td style={{ ...td, textAlign: 'left', color: 'var(--text-primary)', fontWeight: 600, ...mono }}>{CN(e.name)}</td>
                <td style={{ ...td, borderLeft: bd, padding: '3px 3px', fontSize: 9.5, color: 'var(--text-secondary)', ...mono }}>{e.group ? `${e.group} · ${e.points ?? 0}` : '—'}</td>
                <td style={{ ...td, borderLeft: bd, padding: '3px 3px', fontSize: 9.5, color: 'var(--text-secondary)', ...mono }}>{gdLabel(e)}</td>
                {rounds.map((r) => { const t = e.byRound[r.key]; return [
                  <td key={r.key + 'm'} style={{ ...td, borderLeft: bd, color: 'var(--text-secondary)', ...mono }}>{t ? pct(t.model_pct) : '—'}</td>,
                  <td key={r.key + 'k'} style={{ ...td, ...mono }}>{t ? cc(t.kalshi_c) : '—'}</td>,
                  <td key={r.key + 'p'} style={{ ...td, ...mono }}>{t ? cc(t.poly_c) : '—'}</td>,
                  <td key={r.key + 'e'} style={{ ...td, ...mono }}>{edgeCell(t)}</td>,
                ]; })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ marginTop: 8, fontSize: 10, color: 'var(--text-muted)', ...mono }}>{tr('prediction.reachRoundNote')}</div>
    </div>
  );
}

function GoldenBoot() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCChampion(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  // The model array is sorted by p_golden_boot only — 220 of 231 players tie at 0%, where the
  // order degrades to team-alphabetical (a wall of Algeria rows). Tie-break by actual goals,
  // then expected goals, BEFORE slicing, so the tail shows the leading scorers instead.
  const gb = (data?.golden_boot ?? [])
    .slice()
    .sort((a: any, b: any) => (b.p_golden_boot - a.p_golden_boot)
      || ((b.goals ?? 0) - (a.goals ?? 0))
      || ((b.e_goals ?? 0) - (a.e_goals ?? 0)))
    .slice(0, 16);
  return (
    <div>
      <Title sub={tr('prediction.subGoldenBoot')}>Golden Boot</Title>
      <DataTable cols={[tr('prediction.colPlayer'), tr('prediction.colTeam'), tr('prediction.colGoals'), 'P(boot)', 'E[goals]']}
        rows={gb.map((p: any) => [p.name, CN(p.team), p.goals ?? 0, pct(p.p_golden_boot), num(p.e_goals, 2)])} />
    </div>
  );
}

function SquadStrength() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCSquad(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const teams = (data?.teams ?? []);  // all 48 teams
  return (
    <div>
      <Title sub={tr('prediction.subSquad')}>Squad Strength</Title>
      <DataTable cols={['FIFA', tr('prediction.team'), tr('prediction.colSquadScore'), tr('prediction.colRating'), 'GA/90', tr('prediction.colTopPlayers')]}
        rows={teams.map((t: any) => [
          t.fifa_rank != null ? `#${t.fifa_rank}` : '—', CN(t.name), (t.score_z >= 0 ? '+' : '') + t.score_z.toFixed(2),
          t.mw_rating?.toFixed(2), t.ga_per90?.toFixed(2),
          (t.top_players ?? []).slice(0, 3).map((p: any) => `${p.name} (${p.goals}g)`).join(', '),
        ])}
        sortableCols={[0, 2, 3, 4]}
        sortVals={teams.map((t: any) => [t.fifa_rank, null, t.score_z, t.mw_rating, t.ga_per90, null])}
        defaultSort={{ col: 2, dir: 'desc' }} />
    </div>
  );
}

// Client-facing capability overview — what the system does across data / pre-match /
// in-play / discipline. Deliberately NO model parameters, methods or version numbers.
function Methodology() {
  const { t: tr } = useTranslation();
  const cap = tr('prediction.cap', { returnObjects: true }) as any;
  const sections: [string, string[]][] = [
    [cap?.dataT, cap?.data], [cap?.preT, cap?.pre], [cap?.decisionT, cap?.decision],
    [cap?.liveT, cap?.live], [cap?.simT, cap?.sim], [cap?.otherT, cap?.other],
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
  const { mode } = useAdvanceMode();
  const { data, loading, error } = useApi<any[]>(() => getWCDivergence(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const rows = (data ?? []);
  const advance = mode === 'advance';
  return (
    <div>
      <Title sub={tr('prediction.subDivergence')}>Model vs Market</Title>
      {advance ? (
        // 2-way "advances" lens: model_advance vs the venue advance de-vig (knockout only;
        // group rows auto-lock → shown as regulation-only "—").
        <DataTable cols={[tr('prediction.match'), tr('prediction.modelAdvHA'), tr('prediction.marketAdvHA'), tr('prediction.colEdge')]}
          rows={rows.map((m: any) => {
            const a = m.advance;
            if (!a || !a.model) return [VS(m.home, m.away), '—', '—',
              <span style={{ color: 'var(--text-muted)' }}>{tr('prediction.regulationOnly')}</span>];
            const e = a.edge_vs_market || {};
            const side = Math.abs(e.home ?? 0) >= Math.abs(e.away ?? 0) ? 'H' : 'A';
            const val = side === 'H' ? (e.home ?? 0) : (e.away ?? 0);
            return [
              VS(m.home, m.away),
              `${pcent(a.model.home)}/${pcent(a.model.away)}`,
              a.market_devig ? `${pcent(a.market_devig.home)}/${pcent(a.market_devig.away)}` : '—',
              <span style={{ color: Math.abs(val) >= 0.05 ? 'var(--success)' : 'var(--text-muted)' }}>{side} {val >= 0 ? '+' : ''}{pct(val, 1)} ({val >= 0 ? '+' : ''}{pcent(val)})</span>,
            ];
          })} />
      ) : (
        <DataTable cols={[tr('prediction.match'), 'Model ¢ H/D/A', 'Book ¢ H/D/A', tr('prediction.colEdge')]}
          rows={rows.map((m: any) => {
            const e = m.edge_vs_book || {};
            const best = Math.max(Math.abs(e.home ?? 0), Math.abs(e.draw ?? 0), Math.abs(e.away ?? 0));
            const side = best === Math.abs(e.home ?? 0) ? 'H' : best === Math.abs(e.draw ?? 0) ? 'D' : 'A';
            const val = side === 'H' ? e.home : side === 'D' ? e.draw : e.away;
            return [
              VS(m.home, m.away),
              `${pcent(m.model?.home)}/${pcent(m.model?.draw)}/${pcent(m.model?.away)}`,
              `${pcent(m.book_devig?.home)}/${pcent(m.book_devig?.draw)}/${pcent(m.book_devig?.away)}`,
              <span style={{ color: Math.abs(val) >= 0.05 ? 'var(--success)' : 'var(--text-muted)' }}>{side} {val >= 0 ? '+' : ''}{pct(val, 1)} ({val >= 0 ? '+' : ''}{pcent(val)})</span>,
            ];
          })} />
      )}
    </div>
  );
}

function Predictions() {
  const { t: tr } = useTranslation();
  const { mode } = useAdvanceMode();
  const { data, loading, error } = useApi<any>(() => getWCUpcoming(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const ms = data?.matches ?? [];
  const advance = mode === 'advance';
  return (
    <div>
      <Title sub={tr('prediction.subPredictions')}>Today's Predictions</Title>
      {advance ? (
        // 2-way "advances" lens: who advances (incl. ET + penalties). Group rows auto-lock
        // → shown as regulation-only.
        <DataTable cols={[tr('prediction.match'), 'ET', tr('prediction.colHomeAdv'), 'H¢', tr('prediction.colAwayAdv'), 'A¢']}
          rows={ms.map((m: any) => {
            const a = m.advance;
            if (!a || !a.model) return [VS(m.home?.name, m.away?.name), m.et ?? '',
              <span style={{ color: 'var(--text-muted)' }}>{tr('prediction.regulationOnly')}</span>, '—', '—', '—'];
            return [VS(m.home?.name, m.away?.name), m.et ?? '',
              pct(a.model.home, 0), cc(a.model.cents?.home), pct(a.model.away, 0), cc(a.model.cents?.away)];
          })} />
      ) : (
        <DataTable cols={[tr('prediction.match'), 'ET', 'H', 'H¢', 'D', 'D¢', 'A', 'A¢', 'O2.5']}
          rows={ms.map((m: any) => [VS(m.home?.name, m.away?.name), m.et ?? '',
            pct(m.model?.home, 0), cc(m.model?.cents?.home), pct(m.model?.draw, 0), cc(m.model?.cents?.draw),
            pct(m.model?.away, 0), cc(m.model?.cents?.away), pct(m.model?.over_2_5, 0)])} />
      )}
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
  const { mode } = useAdvanceMode();
  const advMode = mode === 'advance';
  const { data, loading, error } = useApi<any>(() => getWCUpcoming(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const ms = data?.matches ?? [];
  const dual = (prob?: number | null, c?: number | null) =>
    prob == null && c == null ? '—' : `${pct(prob, 0)} · ${cc(c)}`;
  const vprob = (q: any, side: string) =>
    (q?.devig?.[side] ?? (q?.[side]?.mid_c != null ? q[side].mid_c / 100 : null));
  return (
    <div>
      <Title sub={tr('prediction.subMatchPricing')}>Match Pricing</Title>
      {ms.map((m: any, i: number) => {
        // 2-way "advances" lens: model_advance vs venue advance prices (主/客, no draw).
        // Group / undecided knockout (no advance block) auto-lock to regulation.
        const adv = advMode && m.advance && m.advance.model ? m.advance : null;
        return (
        <div key={i} className="card" style={{ marginBottom: 10 }}>
          <div style={{ fontWeight: 700, fontSize: 12, ...mono, marginBottom: 6 }}>
            {VS(m.home?.name, m.away?.name, ' vs ')} <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>{m.et}</span>
            {adv && <span style={{ color: 'var(--accent-primary)', marginLeft: 6 }}>{tr('prediction.modeAdvance')}</span>}
            {advMode && !adv && <span style={{ color: 'var(--text-muted)', marginLeft: 6 }}>· {tr('prediction.regulationOnly')}</span>}
          </div>
          {adv ? (
            <DataTable cols={['', tr('prediction.colHomeAdv'), tr('prediction.colAwayAdv')]}
              rows={[
                [tr('prediction.model'), dual(adv.model?.home, adv.model?.cents?.home), dual(adv.model?.away, adv.model?.cents?.away)],
                [tr('prediction.kalshiAsk'), dual(vprob(adv.kalshi, 'home'), adv.kalshi?.home?.mid_c), dual(vprob(adv.kalshi, 'away'), adv.kalshi?.away?.mid_c)],
                [tr('prediction.polyUsAsk'), dual(vprob(adv.poly_us, 'home'), adv.poly_us?.home?.mid_c), dual(vprob(adv.poly_us, 'away'), adv.poly_us?.away?.mid_c)],
              ]} />
          ) : (
            <DataTable cols={['', tr('prediction.home'), tr('prediction.draw'), tr('prediction.away')]}
              rows={[
                [tr('prediction.model'),
                  dual(m.model?.home, m.model?.cents?.home), dual(m.model?.draw, m.model?.cents?.draw), dual(m.model?.away, m.model?.cents?.away)],
                [tr('prediction.kalshiAsk'),
                  dual(vprob(m.kalshi, 'home'), m.kalshi?.home?.mid_c), dual(vprob(m.kalshi, 'draw'), m.kalshi?.draw?.mid_c), dual(vprob(m.kalshi, 'away'), m.kalshi?.away?.mid_c)],
                [tr('prediction.polyUsAsk'),
                  dual(vprob(m.poly_us, 'home'), m.poly_us?.home?.mid_c), dual(vprob(m.poly_us, 'draw'), m.poly_us?.draw?.mid_c), dual(vprob(m.poly_us, 'away'), m.poly_us?.away?.mid_c)],
              ]} />
          )}
          <div style={{ fontSize: 9, color: 'var(--text-muted)', ...mono, marginTop: 4 }}>{tr('prediction.dualUnitLegend')}</div>
        </div>
        );
      })}
    </div>
  );
}

// ── knockout bracket view (Schedule's second mode) ─────────────────────────────
// Flag emoji for the hover card (team_id → flag).
const BR_FLAG: Record<string, string> = {
  algeria: '🇩🇿', argentina: '🇦🇷', australia: '🇦🇺', austria: '🇦🇹', belgium: '🇧🇪',
  bosnia_and_herzegovina: '🇧🇦', brazil: '🇧🇷', canada: '🇨🇦', cape_verde: '🇨🇻', colombia: '🇨🇴',
  cote_divoire: '🇨🇮', croatia: '🇭🇷', dr_congo: '🇨🇩', ecuador: '🇪🇨', egypt: '🇪🇬',
  england: '🏴󠁧󠁢󠁥󠁮󠁧󠁿', france: '🇫🇷', germany: '🇩🇪', ghana: '🇬🇭', japan: '🇯🇵',
  mexico: '🇲🇽', morocco: '🇲🇦', netherlands: '🇳🇱', norway: '🇳🇴', paraguay: '🇵🇾',
  portugal: '🇵🇹', senegal: '🇸🇳', south_africa: '🇿🇦', spain: '🇪🇸', sweden: '🇸🇪',
  switzerland: '🇨🇭', united_states: '🇺🇸',
};
// Static knockout metadata (official schedule): kickoff + venue are FIXED before the
// pairings are decided, so TBD slots can already show when/where they will be played; also
// venue fallbacks for the few fixtures whose API feed lacks venue. Keyed by tree node id.
const BR_META: Record<string, { et?: string; venue?: string }> = {
  'r16-2': { venue: 'Arlington · AT&T Stadium' },
  'r16-6': { venue: 'Atlanta · Mercedes-Benz Stadium' },
  'r16-7': { venue: 'Vancouver · BC Place' },
  'qf-0': { et: '07-09 16:00 ET', venue: 'Foxborough · Gillette Stadium' },
  'qf-1': { et: '07-10 15:00 ET', venue: 'Inglewood · SoFi Stadium' },
  'qf-2': { et: '07-11 17:00 ET', venue: 'Miami Gardens · Hard Rock Stadium' },
  'qf-3': { et: '07-11 21:00 ET', venue: 'Kansas City · Arrowhead Stadium' },
  'sf-0': { et: '07-14 15:00 ET', venue: 'Arlington · AT&T Stadium' },
  'sf-1': { et: '07-15 15:00 ET', venue: 'Atlanta · Mercedes-Benz Stadium' },
  'final-0': { et: '07-19 15:00 ET', venue: 'East Rutherford · MetLife Stadium' },
};

type BrTeam = { team_id: string; name: string; zh?: string } | null;
type BrNode = { id: string; a: BrTeam; b: BrTeam; fx: any; winner: BrTeam };

// The 16 R32 pairings in OFFICIAL tree order (left half rows 0-7, right half 8-15; adjacent
// pairs cascade inward: R16_i = winners of (2i, 2i+1), QF_i = winners of R16 (2i, 2i+1), ...).
// These pairings are settled facts (the R32 fixtures are all drawn/played); the model's
// knockout_bracket.json uses an approximate slotting and disagrees with the real draw, so the
// real fixture list + this fixed order is the single source of truth. Verified against the
// played R16 fixtures (PAR-FRA, CAN-MAR, MEX-ENG all match the adjacent-cascade).
const R32_TREE: [string, string][] = [
  ['germany', 'paraguay'], ['france', 'sweden'], ['south_africa', 'canada'], ['netherlands', 'morocco'],
  ['portugal', 'croatia'], ['spain', 'austria'], ['united_states', 'bosnia_and_herzegovina'], ['belgium', 'senegal'],
  ['brazil', 'japan'], ['cote_divoire', 'norway'], ['mexico', 'ecuador'], ['england', 'dr_congo'],
  ['argentina', 'cape_verde'], ['australia', 'egypt'], ['switzerland', 'algeria'], ['colombia', 'ghana'],
];

function BracketView() {
  const { t: tr } = useTranslation();
  const sched = useApi<any>(() => getWCSchedule(), []);
  const [hover, setHover] = useState<string | null>(null);
  if (sched.loading) return <Loading />;
  if (sched.error) return <ErrorBox e={sched.error} />;
  const ms: any[] = sched.data?.matches ?? [];
  const rnd = (r?: string) => {
    const low = (r || '').toLowerCase();
    if (low.includes('round of 32')) return 'r32';
    if (low.includes('round of 16')) return 'r16';
    if (low.includes('quarter')) return 'qf';
    if (low.includes('semi')) return 'sf';
    if (low.includes('3rd') || low.includes('third')) return 'third';
    if (low.includes('final')) return 'final';
    return '';
  };
  const T = (l: any): BrTeam => (l ? { team_id: l.team_id, name: l.name, zh: l.zh } : null);
  const findFx = (round: string, s1: Set<string>, s2: Set<string>) =>
    ms.find((m) => rnd(m.round) === round && m.home?.id && m.away?.id &&
      ((s1.has(m.home.id) && s2.has(m.away.id)) || (s2.has(m.home.id) && s1.has(m.away.id)))) || null;
  // Winner from the fixture result. A knockout decided on penalties has result='draw'
  // (level after 90/120) — its winner is BACK-FILLED from the next round's actual fixture
  // (whoever advanced appears there), inside up().
  const winOf = (n: BrNode): BrTeam => {
    if (!n.fx?.finished || !n.a || !n.b) return n.winner;
    if (n.fx.result === 'home' || n.fx.result === 'away') {
      const wid = n.fx.result === 'home' ? n.fx.home.id : n.fx.away.id;
      return n.a.team_id === wid ? n.a : n.b.team_id === wid ? n.b : null;
    }
    return n.winner;
  };
  // R32 nodes from the fixed tree order; team display objects come from the fixture itself
  // (authoritative names/zh), falling back to the id when a fixture is somehow absent.
  const r32: BrNode[] = R32_TREE.map(([ida, idb], i) => {
    const fx = findFx('r32', new Set([ida]), new Set([idb]));
    const fxTeam = (tid: string): BrTeam => {
      if (fx?.home?.id === tid) return T({ team_id: tid, name: fx.home.name, zh: fx.home.zh });
      if (fx?.away?.id === tid) return T({ team_id: tid, name: fx.away.name, zh: fx.away.zh });
      return T({ team_id: tid, name: tid.replace(/_/g, ' ') });
    };
    const n: BrNode = { id: `r32-${i}`, a: fxTeam(ida), b: fxTeam(idb), fx, winner: null };
    n.winner = winOf(n);
    return n;
  });
  const up = (prev: BrNode[], round: string): BrNode[] => {
    const out: BrNode[] = [];
    for (let i = 0; i + 1 < prev.length; i += 2) {
      const f1 = prev[i], f2 = prev[i + 1];
      const s1 = new Set([f1.a?.team_id, f1.b?.team_id].filter(Boolean) as string[]);
      const s2 = new Set([f2.a?.team_id, f2.b?.team_id].filter(Boolean) as string[]);
      const fx = findFx(round, s1, s2);
      let a = f1.winner, b = f2.winner;
      if (fx) {
        const pick = (f: BrNode, sset: Set<string>): BrTeam => {
          const tid = sset.has(fx.home.id) ? fx.home.id : sset.has(fx.away.id) ? fx.away.id : null;
          return tid ? (f.a?.team_id === tid ? f.a : f.b?.team_id === tid ? f.b : null) : null;
        };
        a = pick(f1, s1) ?? a; b = pick(f2, s2) ?? b;
        if (!f1.winner && a) f1.winner = a;   // back-fill a PEN-decided feeder
        if (!f2.winner && b) f2.winner = b;
      }
      const n: BrNode = { id: `${round}-${i / 2}`, a, b, fx, winner: null };
      n.winner = winOf(n);
      out.push(n);
    }
    return out;
  };
  const r16 = up(r32, 'r16'), qf = up(r16, 'qf'), sf = up(qf, 'sf'), fin = up(sf, 'final');
  if (r32.length < 16 || !sf.length || !fin.length) {
    return <div style={{ fontSize: 11, color: 'var(--text-muted)', ...mono }}>schedule.json missing knockout fixtures</div>;
  }
  const champ = fin[0].winner;

  // ── horizontal layout: left half → center final ← right half, SVG route lines ──
  const BOX_W = 78, BOX_H = 80, ROW = 88, GAP = 30, HDR = 22;
  const STEP = BOX_W + GAP;
  const colX = (k: number) => k * STEP;                       // 9 columns, 0..8
  const H = HDR + 8 * ROW + 8;
  const W = colX(8) + BOX_W;
  const yR32 = (i: number) => HDR + ((i % 8) + 0.5) * ROW;    // i: tree index 0-15
  const yR16 = (j: number) => (yR32(2 * (j % 4)) + yR32(2 * (j % 4) + 1)) / 2;
  const yQF = (k: number) => (yR16(2 * (k % 2)) + yR16(2 * (k % 2) + 1)) / 2;
  const ySF = (yQF(0) + yQF(1)) / 2;
  // (column, y-center, side) per node — left half feeds rightward, right half leftward.
  const pos: Record<string, { x: number; y: number; right: boolean }> = {};
  r32.forEach((n, i) => { pos[n.id] = { x: colX(i < 8 ? 0 : 8), y: yR32(i), right: i >= 8 }; });
  r16.forEach((n, j) => { pos[n.id] = { x: colX(j < 4 ? 1 : 7), y: yR16(j), right: j >= 4 }; });
  qf.forEach((n, k) => { pos[n.id] = { x: colX(k < 2 ? 2 : 6), y: yQF(k), right: k >= 2 }; });
  sf.forEach((n, s) => { pos[n.id] = { x: colX(s === 0 ? 3 : 5), y: ySF, right: s === 1 }; });
  pos[fin[0].id] = { x: colX(4), y: ySF, right: false };
  const champY = ySF - 1.5 * ROW;

  // Route lines: elbow from each feeder to its child slot. WHITE (= --text-primary) once the
  // feeder is decided (someone advanced along it), grey while undecided.
  const lines: { d: string; on: boolean }[] = [];
  const elbow = (from: BrNode, to: BrNode) => {
    const f = pos[from.id], t0 = pos[to.id];
    if (!f || !t0) return;
    const x1 = f.right ? f.x : f.x + BOX_W;
    const x2 = t0.right ? t0.x + BOX_W : t0.x;
    const midX = (x1 + x2) / 2;
    lines.push({ d: `M ${x1} ${f.y} H ${midX} V ${t0.y} H ${x2}`, on: !!from.winner });
  };
  const link = (prev: BrNode[], next: BrNode[]) => {
    next.forEach((n, i) => { elbow(prev[2 * i], n); elbow(prev[2 * i + 1], n); });
  };
  link(r32, r16); link(r16, qf); link(qf, sf);
  elbow(sf[0], fin[0]);
  // right SF → final's right edge (elbow() handles direction via pos.right flags)
  { const f = pos[sf[1].id], t0 = pos[fin[0].id];
    lines.push({ d: `M ${f.x} ${f.y} H ${(f.x + t0.x + BOX_W) / 2} V ${t0.y} H ${t0.x + BOX_W}`, on: !!sf[1].winner }); }
  // final → champion (vertical)
  lines.push({ d: `M ${colX(4) + BOX_W / 2} ${ySF - BOX_H / 2} V ${champY}`, on: !!champ });

  const nameOf = (t: BrTeam) => (t ? tCountry(t.name) : tr('prediction.brTbd'));
  const flagOf = (t: BrTeam) => (t ? (BR_FLAG[t.team_id] ?? '🏳') : '');
  const Box = ({ n }: { n: BrNode; key?: string }) => {
    const p = pos[n.id];
    const isH = hover === n.id;
    // OFFICIAL home/away ordering: the fixture's home team occupies the TOP half, away the
    // BOTTOM half. Before the fixture exists, fall back to the feeder-derived order.
    const byId = (tid?: string): BrTeam =>
      tid ? (n.a?.team_id === tid ? n.a : n.b?.team_id === tid ? n.b : null) : null;
    const top = (n.fx ? byId(n.fx.home?.id) : null) ?? n.a;
    const bot = (n.fx ? byId(n.fx.away?.id) : null) ?? n.b;
    const [gh, ga] = n.fx?.finished && n.fx.score ? String(n.fx.score).split('-') : [null, null];
    // Half row: name wraps (up to 3 lines), vertically centered in its EXACT 50% half.
    // Left bracket half: name left-aligned, goals pinned right. Right half: mirrored.
    const half = (t: BrTeam, goals: string | null) => {
      // Winner is BOLD at rest (advanced vs eliminated readable without hovering);
      // hover adds the green/dimmed emphasis on top.
      const won = !!(n.winner && t && t.team_id === n.winner.team_id);
      const win = isH && won;
      const lose = isH && n.winner && t && t.team_id !== n.winner.team_id;
      const nameEl = (
        <div style={{ flex: 1, fontSize: 9, lineHeight: '11px', ...mono,
          textAlign: p.right ? 'right' : 'left', overflow: 'hidden',
          display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical' as any,
          wordBreak: 'break-word',
          color: t ? 'var(--text-secondary)' : 'var(--text-muted)',
          ...(won ? { fontWeight: 700, color: 'var(--text-primary)' } : {}),
          ...(win ? { color: 'var(--success)' } : {}),
        }}>{nameOf(t)}</div>
      );
      const goalEl = goals != null && (
        <span style={{ fontSize: 11, fontWeight: 700, ...mono,
          color: win ? 'var(--success)' : 'var(--text-primary)' }}>{goals}</span>
      );
      const flagEl = t && (
        <span style={{ fontSize: 12, flexShrink: 0, lineHeight: 1 }}>{flagOf(t)}</span>
      );
      return (
        <div style={{ height: '50%', display: 'flex', alignItems: 'center', gap: 5,
          padding: '0 7px', ...(lose ? { opacity: 0.35 } : {}) }}>
          {p.right ? <>{goalEl}{nameEl}{flagEl}</> : <>{flagEl}{nameEl}{goalEl}</>}
        </div>
      );
    };
    // hover card — flags + score, shootout line, regulation-time scorers (home left, away right)
    const meta = BR_META[n.id];
    const venueStr = (n.fx?.venue
      ? [n.fx.venue.city, n.fx.venue.name].filter(Boolean).join(' · ')
      : '') || meta?.venue || '';
    const venueEl = venueStr && (
      <div style={{ fontSize: 8.5, color: 'var(--text-muted)', marginTop: 3, letterSpacing: '.03em' }}>
        {venueStr}
      </div>
    );
    const card = () => {
      if (!n.fx) return (
        <div style={{ textAlign: 'center' }}>
          <div>{tr('prediction.brTbd')}{meta?.et ? ` · ${meta.et}` : ''}</div>
          {venueEl}
        </div>
      );
      if (!n.fx.finished) return (
        <div style={{ textAlign: 'center' }}>
          <div>{tr('prediction.brUpcoming')} · {n.fx.et}</div>
          {venueEl}
        </div>
      );
      const sc: { home: { name: string; min: number }[]; away: { name: string; min: number }[] } =
        n.fx.scorers ?? { home: [], away: [] };
      const homeIsTop = n.fx.home?.id === top?.team_id;
      const fl = (t: BrTeam) => flagOf(t);
      return (
        <div style={{ textAlign: 'center' }}>
          <div style={{ color: 'var(--text-muted)', fontSize: 9 }}>✓ {tr('prediction.brDone')} · {n.fx.et}</div>
          <div style={{ fontSize: 13, fontWeight: 700, margin: '3px 0 1px' }}>
            {fl(homeIsTop ? top : bot)} {n.fx.score?.replace('-', ' : ')} {fl(homeIsTop ? bot : top)}
          </div>
          {n.fx.shootout && (
            <div style={{ fontSize: 10, color: 'var(--text-secondary)' }}>PEN {n.fx.shootout}</div>
          )}
          {(sc.home.length > 0 || sc.away.length > 0) && (
            <div style={{ display: 'flex', gap: 14, justifyContent: 'space-between', marginTop: 4 }}>
              <div style={{ textAlign: 'left' }}>
                {sc.home.map((s, i) => (
                  <div key={i} style={{ fontSize: 9, color: 'var(--text-secondary)' }}>⚽ {s.name} {s.min}′</div>
                ))}
              </div>
              <div style={{ textAlign: 'right' }}>
                {sc.away.map((s, i) => (
                  <div key={i} style={{ fontSize: 9, color: 'var(--text-secondary)' }}>{s.name} {s.min}′ ⚽</div>
                ))}
              </div>
            </div>
          )}
          {venueEl}
        </div>
      );
    };
    return (
      <div onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)}
        style={{ position: 'absolute', left: p.x, top: p.y - BOX_H / 2, width: BOX_W, height: BOX_H,
          display: 'flex', flexDirection: 'column',
          border: `1px solid ${isH ? 'var(--text-primary)' : 'var(--border-subtle)'}`,
          background: 'var(--bg-secondary)', cursor: n.fx ? 'pointer' : 'default', zIndex: isH ? 30 : 2,
          transition: 'border-color .1s' }}>
        {half(top, gh)}
        <div style={{ height: 1, flexShrink: 0, background: 'var(--border-subtle)' }} />
        {half(bot, ga)}
        {isH && (
          <div style={{ position: 'absolute', top: BOX_H + 4, zIndex: 40, whiteSpace: 'nowrap',
            ...(p.right ? { right: 0 } : { left: 0 }),
            background: 'var(--bg-primary)', border: '1px solid var(--border-subtle)',
            padding: '6px 10px', fontSize: 10, ...mono, boxShadow: '0 4px 14px rgba(0,0,0,.45)' }}>
            {card()}
          </div>
        )}
      </div>
    );
  };
  // column headers — uppercase mono, mirroring the project's section-label style
  const hdrs: [number, string][] = [
    [0, tr('prediction.round.r32')], [1, tr('prediction.round.r16')], [2, tr('prediction.round.qf')],
    [3, tr('prediction.round.sf')], [4, tr('prediction.round.final')], [5, tr('prediction.round.sf')],
    [6, tr('prediction.round.qf')], [7, tr('prediction.round.r16')], [8, tr('prediction.round.r32')],
  ];
  const all = [...r32, ...r16, ...qf, ...sf, ...fin];
  return (
    <div style={{ overflowX: 'auto', paddingBottom: 6 }}>
      <div style={{ position: 'relative', width: W, height: H }}>
        <svg width={W} height={H} style={{ position: 'absolute', inset: 0, zIndex: 1, pointerEvents: 'none' }}>
          {lines.map((l, i) => (
            <path key={i} d={l.d} fill="none"
              stroke={l.on ? 'var(--text-primary)' : 'var(--border-subtle)'}
              strokeWidth={l.on ? 1.6 : 1} />
          ))}
        </svg>
        {hdrs.map(([k, label]) => (
          <div key={k} style={{ position: 'absolute', left: colX(k), top: 0, width: BOX_W, textAlign: 'center',
            fontSize: 8.5, fontWeight: 700, letterSpacing: '.08em', textTransform: 'uppercase',
            color: 'var(--text-muted)', ...mono, whiteSpace: 'nowrap', overflow: 'hidden' }}>{label}</div>
        ))}
        {all.map((n) => <Box key={n.id} n={n} />)}
        {/* champion — centered above the final */}
        <div style={{ position: 'absolute', left: colX(4), top: champY - BOX_H / 2, width: BOX_W,
          border: `1px solid ${champ ? 'var(--text-primary)' : 'var(--border-subtle)'}`,
          background: 'var(--bg-secondary)', textAlign: 'center', padding: '4px 6px', zIndex: 2 }}>
          <div style={{ fontSize: 8.5, fontWeight: 700, letterSpacing: '.08em', textTransform: 'uppercase',
            color: 'var(--text-muted)', ...mono }}>{tr('prediction.brChampions')}</div>
          <div style={{ fontSize: 10, fontWeight: 700, ...mono, marginTop: 1,
            color: champ ? 'var(--text-primary)' : 'var(--text-muted)',
            overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' as any,
            wordBreak: 'break-word' }}>
            {champ ? nameOf(champ) : '—'}
          </div>
        </div>
      </div>
    </div>
  );
}

function Schedule() {
  const { t: tr } = useTranslation();
  const [view, setView] = useState<'list' | 'bracket'>('list');
  // Full fixed group-stage schedule (all 72, played + upcoming); knockouts auto-append
  // once they're drawn. Falls back to upcoming.json if schedule.json isn't synced yet.
  const sched = useApi<any>(() => getWCSchedule(), []);
  const upc = useApi<any>(() => getWCUpcoming(), []);
  if ((sched.loading && upc.loading)) return <Loading />;
  const ms = (sched.data?.matches?.length ? sched.data.matches : upc.data?.matches) ?? [];
  // Round label: group stage → "小组赛第N轮" (localized); knockout → "1/16 决赛 / Round of 32" etc.
  // (5 languages, prediction.round.*). Check semi/third BEFORE final (their names contain "final").
  const roundLabel = (r?: string): string => {
    const s = (r || '').trim(); const low = s.toLowerCase();
    const g = s.match(/group stage\s*-\s*(\d+)/i);
    if (g) return tr('prediction.round.group', { n: g[1] });
    if (low.includes('round of 32')) return tr('prediction.round.r32');
    if (low.includes('round of 16')) return tr('prediction.round.r16');
    if (low.includes('quarter')) return tr('prediction.round.qf');
    if (low.includes('semi')) return tr('prediction.round.sf');
    if (low.includes('3rd') || low.includes('third')) return tr('prediction.round.third');
    if (low.includes('final')) return tr('prediction.round.final');
    return s;
  };
  const played = ms.filter((m: any) => m.finished).length;
  // Segmented list/bracket switcher — same look + placement as the Regulation/Advances
  // toggle (AdvanceModeToggle): 2px pixel border, uppercase mono, selected = inverted.
  const ink = 'var(--text-primary)';
  const opts: ['list' | 'bracket', string][] = [
    ['list', tr('prediction.schedList')], ['bracket', tr('prediction.schedBracket')],
  ];
  const toggle = (
    <div className="flex overflow-hidden" style={{ border: `2px solid ${ink}`, flexShrink: 0 }}>
      {opts.map(([val, label], i) => (
        <button key={val} onClick={() => setView(val)}
          style={{ padding: '3px 12px', fontSize: '10px', fontFamily: 'var(--font-mono)', fontWeight: 700,
            letterSpacing: '.06em', textTransform: 'uppercase', transition: 'all .1s',
            background: view === val ? ink : 'transparent',
            color: view === val ? 'var(--bg-primary)' : 'var(--text-muted)',
            border: 'none', borderLeft: i > 0 ? `2px solid ${ink}` : 'none',
            cursor: 'pointer', whiteSpace: 'nowrap' }}>{label}</button>
      ))}
    </div>
  );
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8 }}>
        <Title sub={`${tr('prediction.subSchedule')} · ${ms.length} ${tr('prediction.lblMatches')}${played ? ` (${played} ${tr('prediction.finished')})` : ''}`}>Schedule</Title>
        {toggle}
      </div>
      {view === 'bracket' ? <BracketView /> : (
        <DataTable cols={['ET', tr('prediction.colRound'), tr('prediction.match'), tr('prediction.colResult')]}
          rows={ms.map((m: any) => [
            m.et ?? m.kickoff, roundLabel(m.round),
            VS(m.home?.name, m.away?.name),
            m.finished
              ? <span style={{ fontWeight: 700 }}>{m.score}</span>
              : <span style={{ color: 'var(--text-muted)' }}>{m.status === 'NS' ? '—' : m.status}</span>,
          ])} />
      )}
    </div>
  );
}

const KIND_COLOR: Record<string, string> = {
  lock_arb: 'var(--success)', relative_value: 'var(--text-primary)', tactic: 'var(--text-secondary)',
};

// Confidence tier badge colour (validated effectiveness rules — see plan 20-22).
const CONF_COLOR: Record<string, string> = {
  high: 'var(--success)', medium: 'var(--warning, #d08b00)', low: 'var(--text-muted)',
};

// Render an opportunity's reason from its i18n template (reason_key) + the live numbers
// (reason_args), in the active language. Sub-enums (side/carded) are themselves localized;
// the Part-2 strength+form basis becomes a parenthetical suffix; the cross-venue "also"
// clause is appended. Falls back to the English reason string when no key is present.
function renderOppReason(o: any, tr: (k: string, opts?: any) => string): string {
  if (!o.reason_key) return o.reason || '';
  const a: any = { ...(o.reason_args || {}) };
  if (a.side) a.side = tr('prediction.side.' + a.side, { defaultValue: a.side });
  if (a.carded) a.carded = tr('prediction.side.' + a.carded, { defaultValue: a.carded });
  // Data-mined tactics carry secondary side enums (the side to back / attack) — localize too.
  if (a.opp) a.opp = tr('prediction.side.' + a.opp, { defaultValue: a.opp });
  if (a.attack) a.attack = tr('prediction.side.' + a.attack, { defaultValue: a.attack });
  a.basisSuffix = a.basis && a.basis !== 'aligned'
    ? ' (' + tr('prediction.favBasis.' + a.basis, { defaultValue: a.basis }) + ')' : '';
  let s = tr('prediction.reason.' + o.reason_key, { ...a, defaultValue: o.reason || '' });
  if (a.also) s += tr('prediction.reason.alsoSuffix', { also: a.also });
  return s;
}

function InPlay() {
  const { t: tr } = useTranslation();
  const { mode } = useAdvanceMode();
  const adv = mode === 'advance';
  // Poll BOTH the 3-way and the 2-way advance live feeds every 20s, pick by mode (no lag on
  // toggle). The advance feed is the SEPARATE inplay_live_advance.json (knockout only).
  const live3 = usePoll<any>(() => getWCInplayLive(), 20000);
  const liveA = usePoll<any>(() => getWCInplayLiveAdvance(), 20000);
  const { data, loading, updatedAt } = adv ? liveA : live3;
  const matches = data?.matches ?? [];
  const upd = updatedAt ? new Date(updatedAt).toLocaleTimeString() : '';
  return (
    <div>
      <Title sub={tr('prediction.subInPlay')}>In-Play Arbitrage</Title>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono }} className="mb-2">
        ● {tr('prediction.autoRefresh')} 20s{upd ? ` · ${tr('prediction.updated')} ${upd}` : ''} · {data?.n_live ?? 0} {tr('prediction.live')}
        {adv && <span style={{ color: 'var(--accent-primary)', marginLeft: 6 }}>{tr('prediction.modeAdvance')}</span>}
      </div>
      {adv && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 8, ...mono, fontStyle: 'italic' }}>
          {tr('prediction.inplayAdvNote')}
        </div>
      )}
      {loading && !matches.length ? <Loading /> : !matches.length ? (
        <div className="text-xs py-2" style={{ color: 'var(--text-muted)', ...mono }}>{tr('prediction.noLiveMatches')}</div>
      ) : matches.map((m: any) => (
        <div key={m.fixture_id} className="card" style={{ marginBottom: 12 }}>
          {/* live header */}
          <div className="flex items-center justify-between" style={{ marginBottom: 6 }}>
            <span style={{ fontWeight: 700, fontSize: 13, ...mono, color: 'var(--text-primary)' }}>
              <span style={{ color: 'var(--error)', fontWeight: 700, marginRight: 6 }} className="pulse">● {tr('prediction.liveBadge')}</span>
              {CN(m.home.name)} <b>{m.score}</b> {CN(m.away.name)}
            </span>
            <span style={{ fontSize: 11, color: 'var(--text-muted)', ...mono }}>{
              m.period === 'pens' ? `${tr('prediction.periodPens')}${m.shootout ? ` ${m.shootout.home}-${m.shootout.away}` : ''}`
                : m.period === 'et' ? `${tr('prediction.periodEt')} ${m.minute}${m.stoppage ? `+${m.stoppage}` : ''}'`
                : `${m.minute}${m.stoppage ? `+${m.stoppage}` : ''}'`
            }{m.reds !== '0-0' ? ` · 🟥 ${m.reds}` : ''}</span>
          </div>
          {/* live model — probability + per-contract ¢ side by side. 2-way advance: H/A only
              (incl. ET+penalties), no draw. */}
          <div style={{ fontSize: 11, ...mono, color: 'var(--text-secondary)', marginBottom: 2 }}>
            {tr('prediction.model')}: {adv
              ? <>{tr('prediction.colHomeAdv')} {pct(m.model.home, 0)} ({cc(m.prices?.model_c?.home)}) · {tr('prediction.colAwayAdv')} {pct(m.model.away, 0)} ({cc(m.prices?.model_c?.away)})</>
              : <>H {pct(m.model.home, 0)} ({cc(m.prices?.model_c?.home)}) · D {pct(m.model.draw, 0)} ({cc(m.prices?.model_c?.draw)}) · A {pct(m.model.away, 0)} ({cc(m.prices?.model_c?.away)})</>}
          </div>
          {/* live market ¢ (executable) per venue */}
          {(m.prices?.kalshi || m.prices?.poly_us) && (
            <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginBottom: 2 }}>
              {/* mid_c (not ask_c): a deep in-the-money contract has an empty opposite
                  book so yes_ask is undefined — mid_c falls back to the live bid. */}
              {adv ? <>
                {m.prices?.kalshi && <>Kalshi: H {cc(m.prices.kalshi.home?.mid_c)} / A {cc(m.prices.kalshi.away?.mid_c)}　</>}
                {m.prices?.poly_us && <>Poly: H {cc(m.prices.poly_us.home?.mid_c)} / A {cc(m.prices.poly_us.away?.mid_c)}</>}
              </> : <>
                {m.prices?.kalshi && <>Kalshi: H {cc(m.prices.kalshi.home?.mid_c)} / D {cc(m.prices.kalshi.draw?.mid_c)} / A {cc(m.prices.kalshi.away?.mid_c)}　</>}
                {m.prices?.poly_us && <>Poly: H {cc(m.prices.poly_us.home?.mid_c)} / D {cc(m.prices.poly_us.draw?.mid_c)} / A {cc(m.prices.poly_us.away?.mid_c)}</>}
              </>}
            </div>
          )}
          <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginBottom: 6 }}>
            xG {m.xg.home ?? '—'} / {m.xg.away ?? '—'}{adv
              ? <> · {tr('prediction.advSplit')} {pct(m.model.p_reg_decides, 0)}/{pct(m.model.p_et_decides, 0)}/{pct(m.model.p_pens_decides, 0)}</>
              : <> · {tr('prediction.expGoals')} {num(m.model.exp_remaining_goals, 2)}</>}
          </div>
          {/* Regulation (90' 3-way) is SETTLED once the match passes 90' (ET/penalties): the
              result is decided, so no new entries / hedge — only "collect a held position".
              The live action continues in the Advances product. */}
          {!adv && m.period && m.period !== 'reg' && (
            <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginBottom: 6, fontStyle: 'italic' }}>
              {tr('prediction.reg90Settled')}
            </div>
          )}
          {/* opportunities / tricks — grouped by INTENT (manage a held position / new entry /
              event) so a held-position exit (e.g. overshoot sell) isn't read as contradicting
              a new-entry buy. Every signal is kept; only the grouping changes. */}
          {m.opportunities?.length ? (() => {
            const cols = [tr('prediction.colKind'), tr('prediction.colConf'), tr('prediction.colStake'), tr('prediction.colAction'), tr('prediction.colSide'), tr('prediction.colMarketC'), tr('prediction.colEdge'), tr('prediction.colEdgeC'), tr('prediction.colReason')];
            const confBadge = (o: any) => o.confidence ? (
              <span title={o.confidence_reason || ''} style={{ color: CONF_COLOR[o.confidence] ?? 'var(--text-muted)', fontWeight: 700, fontSize: 9, border: `1px solid ${CONF_COLOR[o.confidence] ?? 'var(--text-muted)'}`, borderRadius: 3, padding: '0 4px', textTransform: 'uppercase', letterSpacing: '.04em' }}>
                {tr('prediction.conf.' + o.confidence, { defaultValue: o.confidence })}
              </span>
            ) : '—';
            // Staking gate (the betting threshold): green $ when we'd actually bet it,
            // muted "advisory" when the signal is kept but below the gate. Tooltip = why.
            const stakeCell = (o: any) => o.actionable
              ? <span title={o.gate_reason || ''} style={{ color: 'var(--success)', fontWeight: 700 }}>${num(o.stake_usd ?? 0, 2)}</span>
              : <span title={o.gate_reason || ''} style={{ color: 'var(--text-muted)', fontStyle: 'italic' }}>{tr('prediction.advisory')}</span>;
            const oppRow = (o: any) => [
              <span style={{ color: KIND_COLOR[o.kind] ?? 'var(--text-secondary)', fontWeight: 700 }}>{tr('prediction.kind.' + o.kind, { defaultValue: o.kind })}</span>,
              confBadge(o),
              stakeCell(o),
              tr('prediction.action.' + o.action, { defaultValue: o.action }),
              // corner-total signal shares kind="tactic" but trades the corners book, not the
              // goal-totals book — label its 方向 as 角球小球/角球大球 so it never reads as a
              // goal Under/Over in the same column.
              tr('prediction.side.' + (o.venue === 'corners' ? 'corner_' + o.side : o.side), { defaultValue: o.side }),
              cc(o.market_c), o.edge != null ? num(o.edge, 3) : '—',
              <span style={{ color: (o.edge_c ?? 0) > 0 ? 'var(--success)' : 'var(--ink)' }}>{o.edge_c != null ? `${o.edge_c > 0 ? '+' : ''}${cc(o.edge_c)}` : '—'}</span>,
              renderOppReason(o, tr),
            ];
            return (['manage', 'entry', 'event'] as const).map(group => {
              const rows = m.opportunities.filter((o: any) => (o.intent || 'entry') === group);
              if (!rows.length) return null;
              return (
                <div key={group} style={{ marginBottom: 8 }}>
                  <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-muted)', ...mono, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 2 }}>
                    {tr('prediction.intent.' + group)}
                  </div>
                  <DataTable className="inplay-arb-table" cols={cols} rows={rows.map(oppRow)} />
                </div>
              );
            });
          })() : (!adv && m.period && m.period !== 'reg')
            ? null  /* settled 90' market — the reg90Settled note already explains the empty list */
            : <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono }}>{tr('prediction.noOpps')}</div>}
          {/* Hedge — protect a leading directional position by buying draw (the quant
              math lives in the backend strategy.inplay_hedge; here we only render). */}
          {!adv && m.hedge && (() => {
            const h = m.hedge;
            const homeName = tCountry(m.home.name), awayName = tCountry(m.away.name);
            const planLabel = (b: number) => b === 0 ? tr('prediction.hedge.planNone')
              : (h.full_hedge_b != null && Math.abs(b - h.full_hedge_b) < 0.01) ? tr('prediction.hedge.planFull')
              : tr('prediction.hedge.planBe');
            const sign = (x: number) => (x >= 0 ? '+' : '');
            const col = (x: number) => <span style={{ color: x >= 0 ? 'var(--success)' : 'var(--error)' }}>{sign(x)}{cc(x)}</span>;
            const cols = [tr('prediction.hedge.colPlan'), tr('prediction.hedge.colDrawB'),
              `${homeName} ${tr('prediction.hedge.win')}`, tr('prediction.side.draw'), `${awayName} ${tr('prediction.hedge.win')}`];
            // draw cell shows the share count AND the per-contract draw price (so the cost is
            // verifiable); draw_c at 1-decimal precision — the same value the back-end sizes on.
            const drawCell = (b: number) => b === 0 ? num(b, 2) : `${num(b, 2)} @${cc(h.draw_c, 1)}`;
            const rows = (h.payoff || []).map((r: any) => [planLabel(r.b), drawCell(r.b), col(r.home), col(r.draw), col(r.away)]);
            return (
              <div style={{ marginTop: 8, padding: '6px 8px', border: '1px solid var(--border)', borderRadius: 4, background: 'var(--bg-subtle)' }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--success)', ...mono, marginBottom: 3 }}>
                  🛡 {tr(h.lead_state === 'leading' ? 'prediction.hedge.titleLeading' : 'prediction.hedge.titleManage')}
                </div>
                {/* Always our pre-match position; the state (leading / level / behind) is noted. */}
                <div style={{ fontSize: 10, ...mono, marginBottom: 2, color: h.lead_state === 'leading' ? 'var(--text-secondary)' : 'var(--warning, #d08b00)' }}>
                  {tr('prediction.hedge.summary', {
                    team: tCountry(h.held_team),
                    entry: cc(h.entry_c, 1),
                    state: tr('prediction.hedge.state.' + h.lead_state),
                    draw: cc(h.draw_c, 1),
                    shares: h.shares_ref,
                  })}
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-primary)', ...mono, marginBottom: 4 }}>
                  {tr('prediction.hedge.breakEven', { b: num(h.break_even_b, 2) })}
                  {h.profit_if_win_c != null && <> · {h.profit_if_win_c >= 0
                    ? tr('prediction.hedge.profitIfWin', { team: tCountry(h.held_team), profit: cc(h.profit_if_win_c) })
                    : tr('prediction.hedge.lossIfWin', { team: tCountry(h.held_team), loss: cc(Math.abs(h.profit_if_win_c)) })}</>}
                </div>
                {/* fixed layout: the three outcome columns are EXACTLY equal width */}
                <table style={{ width: '100%', tableLayout: 'fixed', borderCollapse: 'collapse', ...mono, fontSize: 10, marginTop: 2 }}>
                  <colgroup>
                    <col style={{ width: '20%' }} />
                    <col style={{ width: '24%' }} />
                    <col style={{ width: '18.66%' }} />
                    <col style={{ width: '18.66%' }} />
                    <col style={{ width: '18.66%' }} />
                  </colgroup>
                  <thead>
                    <tr>{cols.map((c, i) => (
                      <th key={i} style={{ textAlign: i < 2 ? 'left' : 'right', padding: '2px 6px', color: 'var(--text-muted)', fontWeight: 700, borderBottom: '1px solid var(--border)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c}</th>
                    ))}</tr>
                  </thead>
                  <tbody>{rows.map((r, ri) => (
                    <tr key={ri}>{r.map((cell, ci) => (
                      <td key={ci} style={{ textAlign: ci < 2 ? 'left' : 'right', padding: '2px 6px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{cell}</td>
                    ))}</tr>
                  ))}</tbody>
                </table>
                <div style={{ fontSize: 9, color: 'var(--text-muted)', ...mono, marginTop: 3 }}>
                  ⚠ {tr('prediction.hedge.warnAway', { away: h.held_side === 'home' ? awayName : homeName })}
                </div>
                {h.knockout && (
                  <div style={{ fontSize: 9, color: 'var(--text-muted)', ...mono, marginTop: 1 }}>
                    ⏱ {tr('prediction.hedge.koNote')}
                  </div>
                )}
              </div>
            );
          })()}
          {/* 2-way ADVANCE hedge: protect held "X advances" by buying "opponent advances". */}
          {adv && m.hedge_advance && (() => {
            const h = m.hedge_advance;
            const homeName = tCountry(m.home.name), awayName = tCountry(m.away.name);
            const planLabel = (b: number) => b === 0 ? tr('prediction.hedge.planNone')
              : (h.full_hedge_b != null && Math.abs(b - h.full_hedge_b) < 0.01) ? tr('prediction.hedge.planFull')
              : tr('prediction.hedge.planBe');
            const sign = (x: number) => (x >= 0 ? '+' : '');
            const col = (x: number) => <span style={{ color: x >= 0 ? 'var(--success)' : 'var(--error)' }}>{sign(x)}{cc(x)}</span>;
            const cols = [tr('prediction.hedge.colPlan'), tr('prediction.hedge.colHedgeB'),
              `${homeName} ${tr('prediction.advancesShort')}`, `${awayName} ${tr('prediction.advancesShort')}`];
            const hedgeCell = (b: number) => b === 0 ? num(b, 2) : `${num(b, 2)} @${cc(h.away_adv_c, 1)}`;
            const rows = (h.payoff || []).map((r: any) => [planLabel(r.b), hedgeCell(r.b), col(r.home), col(r.away)]);
            return (
              <div style={{ marginTop: 8, padding: '6px 8px', border: '1px solid var(--border)', borderRadius: 4, background: 'var(--bg-subtle)' }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--success)', ...mono, marginBottom: 3 }}>
                  🛡 {tr('prediction.hedge.titleAdvance')}
                </div>
                <div style={{ fontSize: 10, ...mono, marginBottom: 2, color: h.lead_state === 'leading' ? 'var(--text-secondary)' : 'var(--warning, #d08b00)' }}>
                  {tr('prediction.hedge.summaryAdvance', {
                    team: tCountry(h.held_team), entry: cc(h.entry_c, 1),
                    state: tr('prediction.hedge.state.' + h.lead_state),
                    hedge: cc(h.away_adv_c, 1), shares: h.shares_ref,
                  })}
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-primary)', ...mono, marginBottom: 4 }}>
                  {tr('prediction.hedge.breakEvenAdvance', { b: num(h.break_even_b, 2) })}
                  {h.profit_if_win_c != null && <> · {h.profit_if_win_c >= 0
                    ? tr('prediction.hedge.profitIfWinAdvance', { team: tCountry(h.held_team), profit: cc(h.profit_if_win_c) })
                    : tr('prediction.hedge.lossIfWinAdvance', { team: tCountry(h.held_team), loss: cc(Math.abs(h.profit_if_win_c)) })}</>}
                </div>
                <table style={{ width: '100%', tableLayout: 'fixed', borderCollapse: 'collapse', ...mono, fontSize: 10, marginTop: 2 }}>
                  <colgroup>
                    <col style={{ width: '22%' }} /><col style={{ width: '26%' }} />
                    <col style={{ width: '26%' }} /><col style={{ width: '26%' }} />
                  </colgroup>
                  <thead>
                    <tr>{cols.map((c, i) => (
                      <th key={i} style={{ textAlign: i < 2 ? 'left' : 'right', padding: '2px 6px', color: 'var(--text-muted)', fontWeight: 700, borderBottom: '1px solid var(--border)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{c}</th>
                    ))}</tr>
                  </thead>
                  <tbody>{rows.map((r, ri) => (
                    <tr key={ri}>{r.map((cell, ci) => (
                      <td key={ci} style={{ textAlign: ci < 2 ? 'left' : 'right', padding: '2px 6px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{cell}</td>
                    ))}</tr>
                  ))}</tbody>
                </table>
                <div style={{ fontSize: 9, color: 'var(--text-muted)', ...mono, marginTop: 3 }}>
                  ⏱ {tr('prediction.hedge.advNote')}
                </div>
              </div>
            );
          })()}
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
        [tr('prediction.lblModelAcc'), pct(data?.model_pred_accuracy ?? data?.favourite_hit_rate, 0)],
        ...(data?.argmax_record ? [[tr('prediction.lblArgmaxRecord'), <span><b>{data.argmax_record}</b> · <span style={{ color: (data?.argmax_pnl_cents_total ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{(data?.argmax_pnl_cents_total ?? 0) >= 0 ? '+' : ''}{cc(data?.argmax_pnl_cents_total)}</span></span>] as [string, ReactNode]] : []),
        [tr('prediction.lblAvgClv'), <span style={{ color: (data?.avg_clv_cents ?? 0) > 0 ? 'var(--success)' : 'var(--ink)' }}>{(data?.avg_clv_cents ?? 0) > 0 ? '+' : ''}{cc(data?.avg_clv_cents)}</span>],
        [tr('prediction.lblCalibPnl'), `${num(data?.calibration_pnl, 2)}u (${num(data?.calibration_pnl_per_bet, 3)}u/bet)`],
        [tr('prediction.lblTradeGrade'), <span style={{ color: pass ? 'var(--success)' : 'var(--error)', fontWeight: 700 }}>{pass ? tr('prediction.gradePassCalibrated') : tr('prediction.gradeBlock')}</span>],
      ]} />
      <BetLog data={data} />
      <Notes items={data?.notes} i18nItems={data?.notes_i18n} />
    </div>
  );
}

// Bet log with 3 SELF-CONSISTENT modes so each口径 is unambiguous:
//   cashout — decision bet + smart-exit cash-out (the REAL strategy). "Exit" column shows
//             when/at what price we sold the over-reaction, OR settled W/L; ¢ is realised.
//   hold    — same picks held to settlement (reference).
//   argmax  — bet the most-likely side every match (the old naive rule, reference).
function BetLog({ data }: { data: any }) {
  const { t: tr } = useTranslation();
  const log: any[] = data?.bet_log ?? [];
  const [mode, setMode] = useState<'cashout' | 'hold' | 'argmax'>('cashout');
  if (!log.length) return null;
  // 下注 side label; a "·热门/fav" tag flags the HYBRID argmax fills (no pre-match edge → bet the
  // favourite at flat $1, same contract calc), so value vs argmax bets are distinguishable.
  const sideLabel = (b: any) => (<>{b.pick === 'draw' ? tr('prediction.drawResult') : CN(b.pick_team)}{b.bet_kind === 'argmax'
    ? <span style={{ color: 'var(--text-muted)', fontSize: 9 }}> ·{tr('prediction.betKindArgmax')}</span> : null}</>);
  const argmaxLabel = (b: any) => (b.model_pick === 'draw' ? tr('prediction.drawResult') : CN(b.model_pick_team));
  const wl = (won: boolean) => <span style={{ color: won ? 'var(--success)' : 'var(--error)', fontWeight: 700 }}>{won ? tr('prediction.betWon') : tr('prediction.betLost')}</span>;
  const cVal = (v: number | null | undefined): ReactNode => v == null ? '—' : <span style={{ color: v >= 0 ? 'var(--success)' : 'var(--error)' }}>{v >= 0 ? '+' : ''}{cc(v)}</span>;
  const matchup = (b: any) => <>{CN(b.home)} {b.score} {CN(b.away)}</>;

  const tab = (m: typeof mode, label: string) => (
    <button key={m} onClick={() => setMode(m)} style={{ padding: '3px 10px', fontSize: 11, fontFamily: 'var(--font-mono)', fontWeight: mode === m ? 700 : 400, color: mode === m ? 'var(--text-primary)' : 'var(--text-muted)', background: mode === m ? 'var(--bg-tertiary)' : 'transparent', border: `1px solid ${mode === m ? 'var(--accent-primary)' : 'var(--border-subtle)'}`, borderRadius: 4, cursor: 'pointer', marginRight: 6 }}>{label}</button>
  );

  // Unified headline format across all 3 modes: <record W-L> · <PnL¢> · <context>.
  const hl = (record: string, pnl: number, context: ReactNode) =>
    <><b>{record}</b> · {cVal(pnl)} · <span style={{ color: 'var(--text-muted)' }}>{context}</span></>;
  const nb = data.n_decision_bets ?? log.length;
  const headline = (): ReactNode => {
    // cashout headline = the COMBINED cumulative (smart-exit realised + in-play entry), with
    // the two streams broken out in the context line.
    if (mode === 'cashout') return hl(data.realized_record, data.combined_pnl_cents_total,
      <>{tr('prediction.colRealizedC')} {cc(data.realized_pnl_cents_total)} + {tr('prediction.colInplayRealized')} {data.inplay_record} {cc(data.inplay_pnl_cents_total)} = {tr('prediction.colCombinedCum')}</>);
    if (mode === 'hold') return hl(data.hold_record || data.pnl_record, data.hold_pnl_cents_total, <>{nb} {tr('prediction.lblBets')} · {data.n_skipped ?? 0} {tr('prediction.lblSkipped')}</>);
    return hl(data.argmax_record, data.argmax_pnl_cents_total, <>{log.length} {tr('prediction.lblMatchesAll')} · {tr('prediction.lblModelAcc')} {pct(data.model_pred_accuracy, 0)}</>);
  };
  const note = (): string => mode === 'cashout'
    // pass RAW ¢ numbers — the noteCashout template already writes the ¢ unit (avoid the ¢¢ dup).
    ? tr('prediction.noteCashout', { entry: data.avg_entry_cents == null ? '—' : Math.round(data.avg_entry_cents), clv: ((data.avg_clv_cents ?? 0) >= 0 ? '+' : '') + Math.round(data.avg_clv_cents ?? 0) })
    : mode === 'hold' ? tr('prediction.noteHold') : tr('prediction.noteArgmax');

  const cols = mode === 'argmax'
    ? [tr('prediction.colDate'), tr('prediction.colMatchup'), tr('prediction.colOurPick'), tr('prediction.colResult'), tr('prediction.colEntryC'), tr('prediction.colRealizedC'), 'Cum¢']
    // cashout: pre-match stream (下注/离场/入场¢/实现¢/赛前Cum) MIRRORED by the in-play stream
    // (盘中下注/盘中离场/盘中入场¢/盘中实现¢/盘中Cum), both $-sized identically, then 合计Cum.
    : mode === 'cashout'
      ? [tr('prediction.colDate'), tr('prediction.colMatchup'), tr('prediction.colOurPick'), tr('prediction.colStake'), tr('prediction.colExit'), tr('prediction.colEntryC'), tr('prediction.colRealizedC'), tr('prediction.colPreCum'), tr('prediction.colInplayBet'), tr('prediction.colInplayExit'), tr('prediction.colInplayEntryC'), tr('prediction.colInplayRealized'), tr('prediction.colInplayCum'), tr('prediction.colCombinedCum')]
      : [tr('prediction.colDate'), tr('prediction.colMatchup'), tr('prediction.colOurPick'), tr('prediction.colStake'), tr('prediction.colResult'), tr('prediction.colEntryC'), tr('prediction.colPnlC'), 'Cum¢'];

  const muted = (x: ReactNode) => <span style={{ color: 'var(--text-muted)' }}>{x}</span>;
  // Shared exit cell (pre-match OR in-play): smart-exit sold min/price if it fired, else the
  // settle W/L — one function so both streams show 离场/盘中离场 identically.
  const exitCell = (se: any, won: boolean) => se
    ? <span style={{ color: se.pnl_c >= 0 ? 'var(--success)' : 'var(--error)' }}>{tr('prediction.smartExitSold', { min: se.sold_min, c: Math.round(se.sold_c) })}</span>
    : <span style={{ color: won ? 'var(--success)' : 'var(--error)' }}>{tr(won ? 'prediction.exitSettleWon' : 'prediction.exitSettleLost')}</span>;
  // In-play 盘中下注 side + milestone (colour by win); other in-play cells are — when no entry.
  const inplayBet = (b: any) => b.inplay_side
    ? <span style={{ color: b.inplay_won ? 'var(--success)' : 'var(--error)' }}>{(b.inplay_side === 'draw' ? tr('prediction.drawResult') : CN(b.inplay_side_team))} {b.inplay_milestone}</span>
    : muted('—');
  // The 5 in-play columns for a cashout row: 盘中下注 / 盘中离场 / 盘中入场¢ / 盘中实现¢ / 盘中Cum¢.
  const inplayCells = (b: any) => b.inplay_side
    ? [inplayBet(b), exitCell(b.inplay_exit, b.inplay_won), cc(b.inplay_entry_cents), cVal(b.inplay_pnl_cents), cVal(b.inplay_cum_pnl_cents)]
    : [muted('—'), muted('—'), '—', '—', cVal(b.inplay_cum_pnl_cents)];
  const rows = log.map((b: any) => {
    if (mode === 'argmax')
      return [b.date?.slice(5), matchup(b), argmaxLabel(b), wl(b.model_won), cc(b.argmax_entry_cents), cVal(b.argmax_pnl_cents), cVal(b.argmax_cum_pnl_cents)];
    if (b.bet === false) {
      const base = [b.date?.slice(5), matchup(b), muted(tr('prediction.noBetShort')), muted('$0')];
      // cashout view: a no-bet match has empty pre-match cells but can still carry an IN-PLAY
      // entry → 赛前Cum unchanged, the in-play columns + 合计Cum reflect it.
      return mode === 'cashout'
        ? [...base, muted('—'), '—', '—', cVal(b.pre_cum_pnl_cents), ...inplayCells(b), cVal(b.combined_cum_pnl_cents)]
        : [...base, muted('—'), '—', '—', '—'];
    }
    if (mode === 'cashout')
      return [b.date?.slice(5), matchup(b), sideLabel(b), `$${num(b.stake_usd, 2)}`, exitCell(b.smart_exit, b.won), cc(b.entry_cents), cVal(b.realized_pnl_cents), cVal(b.pre_cum_pnl_cents), ...inplayCells(b), cVal(b.combined_cum_pnl_cents)];
    return [b.date?.slice(5), matchup(b), sideLabel(b), `$${num(b.stake_usd, 2)}`, wl(b.won), cc(b.entry_cents), cVal(b.pnl_cents), cVal(b.cum_pnl_cents)];
  });

  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ marginBottom: 8 }}>
        {tab('cashout', tr('prediction.modeCashout'))}
        {tab('hold', tr('prediction.modeHold'))}
        {tab('argmax', tr('prediction.modeArgmax'))}
      </div>
      <div style={{ fontSize: 12, fontFamily: 'var(--font-mono)', marginBottom: 4, color: 'var(--text-primary)' }}>{headline()}</div>
      <div style={{ fontSize: 10, fontFamily: 'var(--font-mono)', marginBottom: 6, color: 'var(--text-muted)', lineHeight: 1.55 }}>{note()}</div>
      <DataTable cols={cols} rows={rows} />
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
        [tr('prediction.lblApiBudget'), `${ab.used}/${ab.cap}/${tr('prediction.perDay')} (${pct(ab.pct, 0)})`],
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
  const fmtParams = (p: any) => Object.entries(p ?? {}).map(([k, v]) => `${k.replace(/_/g, ' ')}=${v}`).join('  ');
  const all = (data?.results_all ?? []).slice(0, 60);   // top 60 of the ranked sets
  // Live counts read straight from the data — how many param sets were scored, on how many matches.
  const nSets = data?.n_param_sets, nSettled = data?.n_settled;
  const subCount = nSets != null ? `${nSets} ${tr('prediction.paramSets')} · ${nSettled} ${tr('prediction.lblMatches')} · ${tr('prediction.subParams')}` : tr('prediction.subParams');
  return (
    <div>
      <Title sub={subCount}>Parameter Sweep</Title>
      {data?.generated_at && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginTop: -6, marginBottom: 8 }}>
          {tr('prediction.lblSweepUpdated')}: {new Date(data.generated_at).toLocaleString()}
        </div>
      )}
      <KV rows={[
        // Selected on the CALIBRATED Brier (the fair number vs uniform); raw shown alongside.
        [tr('prediction.lblSelected'), <span><b style={{ color: 'var(--success)' }}>{data?.best?.brier_cal?.toFixed?.(4)}</b> <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>(raw {data?.best?.brier?.toFixed?.(4)})</span></span>],
        [tr('prediction.lblVsCurrent'), `${num(data?.baseline?.brier_cal, 4)} (raw ${num(data?.baseline?.brier, 4)})`],
        [tr('prediction.lblVsUniform'), data?.uniform_brier],
        [tr('prediction.colParams'), <span style={{ fontSize: 10 }}>{fmtParams(data?.best?.params)}</span>],
      ]} />
      <div style={{ fontSize: 11, color: 'var(--text-muted)', ...mono, margin: '4px 0 10px' }}>{tr('prediction.paramsWhy')}</div>
      <DataTable cols={['#', tr('prediction.colBrierRaw'), tr('prediction.colBrierCal'), 'Acc', tr('prediction.colParams'), '>uni?']}
        rows={all.map((r: any) => [
          r.rank, r.brier,
          <b style={{ color: r.beats_uniform ? 'var(--success)' : 'var(--ink)' }}>{r.brier_cal}</b>,
          r.acc, <span style={{ fontSize: 10 }}>{fmtParams(r.params)}</span>,
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
          VS(m.home, m.away), m.score, m.result,
          `${m.model_pick} ${m.model_p != null ? pct(m.model_p, 0) : ''}`,
          m.book_pick ? `${m.book_pick} ${pct(m.book_p, 0)}` : '—',
        ])} />
    </div>
  );
}

// Merged "System & Model Notes" — system-overview headline, then the Model-Notes sections as
// click-to-expand accordions (moved UP to sit right before the interfaces table), then the
// interfaces + schedule tables. All on one page.
function OverviewModelNotes() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCOverview(), []);
  const cap = tr('prediction.cap', { returnObjects: true }) as any;
  const [open, setOpen] = useState<Record<string, boolean>>({});
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const mn = ([[cap?.dataT, cap?.data], [cap?.preT, cap?.pre], [cap?.decisionT, cap?.decision],
    [cap?.liveT, cap?.live], [cap?.simT, cap?.sim], [cap?.otherT, cap?.other]] as [string, string[]][])
    .filter(([t, items]) => t && Array.isArray(items));
  return (
    <div>
      <Title sub={data?.as_of ? `as of ${data.as_of}` : undefined}>System Overview</Title>
      <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 10, ...mono }}>{overviewHeadline(data?.performance, data?.headline)}</div>
      {/* — Model Notes (moved up, right before the interfaces section) — */}
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.1em', margin: '8px 0 6px', ...mono, color: 'var(--text-primary)' }}>{tr('prediction.modelNotes')}</div>
      {mn.map(([title, items]) => {
        const isOpen = !!open[title];
        return (
          <div key={title} style={{ borderTop: '1px solid var(--border-subtle)' }}>
            <button onClick={() => setOpen((o) => ({ ...o, [title]: !o[title] }))}
              style={{ width: '100%', textAlign: 'left', padding: '7px 2px', background: 'none', border: 'none', cursor: 'pointer', display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 11, fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', ...mono, color: 'var(--text-primary)' }}>
              <span>{title}</span><span style={{ color: 'var(--text-muted)' }}>{isOpen ? '▾' : '▸'}</span>
            </button>
            {isOpen && <ul style={{ fontSize: 11.5, lineHeight: 1.6, color: 'var(--text-secondary)', ...mono, padding: '0 0 8px 16px' }}>{items.map((it, i) => <li key={i} style={{ marginBottom: 3 }}>{it}</li>)}</ul>}
          </div>
        );
      })}
      {/* — Interfaces — */}
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.1em', margin: '16px 0 4px', ...mono }}>{tr('prediction.secInterfaces')}</div>
      <DataTable cols={[tr('prediction.colCat'), tr('prediction.colCommand'), tr('prediction.colPurpose')]} rows={(data?.interfaces ?? []).map((i: any) => [<span style={{ whiteSpace: 'nowrap' }}>{tDyn(i.category)}</span>, i.command?.replace('python -m prediction_market.', ''), tDyn(i.purpose)])} />
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.1em', margin: '10px 0 4px', ...mono }}>{tr('prediction.secSchedule')}</div>
      <DataTable cols={[tr('prediction.colWhen'), tr('prediction.colRuns'), tr('prediction.colFreq')]} rows={(data?.schedule ?? []).map((s: any) => [tDyn(s.when), s.runs, tDyn(s.frequency)])} />
    </div>
  );
}

// Merged "Venues & API" artifact: execution venues/balances, gates & risk controls (from the Risk
// Report), API budget/health, and the blocked/guardrail list — ALL in ONE table, followed by a
// single uniform small-print notes list. Nothing dropped; overlapping info deduped (the separate
// executable-venues line duplicated the table's Trading column, and the budget bar duplicated the
// utilisation % already shown in the API-budget row).
function VenuesApi() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCRisk(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const g = data?.gates ?? {}, b = data?.venue_balances ?? {}, ab = data?.api_budget ?? {};
  const cal = data?.calibration_gate ?? {};
  // Dedupe: blocked_summary always ends with the static "Every order hard-capped at $X notional."
  // guardrail line — the Order-cap row above already shows exactly that, so drop it here. (Kept
  // in the backend JSON: the PDF risk report and tests still consume the full list.)
  const blocked: string[] = (data?.blocked_summary ?? []).filter((x: string) => !/hard-capped/i.test(x));
  const overBudget = (ab.pct ?? 0) > 0.8;
  // Gates & risk controls + API budget — label/value rows folded into the one table below.
  const controls: [string, ReactNode][] = [
    [tr('prediction.lblKalshiEnv'), g.kalshi_env],
    [tr('prediction.lblOrderCap'), money(g.hard_order_cap_usd)],
    [tr('prediction.lblCalibration'), <span style={{ color: cal.trade_grade ? 'var(--success)' : 'var(--error)' }}>{tDyn(cal.status)}</span>],
    [tr('prediction.lblApiBudget'), <span style={{ color: overBudget ? 'var(--error)' : undefined }}>
      {ab.used ?? '—'}/{ab.cap ?? '—'} ({pct(ab.pct, 0)}) · {tr('prediction.lblRemaining')} {ab.cap != null && ab.used != null ? (ab.cap - ab.used) : '—'}
    </span>],
    // Month backstop keeps the little utilisation bar (inline, inside this row).
    ...(ab.month_used != null ? [(() => {
      const mfrac = Math.min(1, (ab.month_used ?? 0) / (ab.month_cap ?? 1));
      return [tr('prediction.lblMonthBackstop'), <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
        {ab.month_used}/{ab.month_cap}
        <span style={{ display: 'inline-block', width: 110, height: 8, background: 'var(--bg-tertiary)', border: '1px solid var(--border-subtle)' }}>
          <span style={{ display: 'block', width: `${mfrac * 100}%`, height: '100%', background: mfrac > 0.8 ? 'var(--error)' : 'var(--success)' }} />
        </span>
      </span>] as [string, ReactNode];
    })()] : []),
  ];
  return (
    <div>
      <Title sub={tr('prediction.subVenuesApi')}>Venues & API</Title>
      {/* THE one table — venues/balances, then gates & controls & API budget as label/value
          rows, then the blocked/guardrail rows. No sub-sections, no separate widgets. */}
      <table className="table">
        <thead><tr>
          <th style={{ textAlign: 'left' }}>{tr('prediction.colVenue')}</th>
          <th style={{ textAlign: 'right' }}>{tr('prediction.colRole')}</th>
          <th style={{ textAlign: 'right' }}>{tr('prediction.colBalance')}</th>
          <th style={{ textAlign: 'right' }}>{tr('prediction.colTrading')}</th>
        </tr></thead>
        <tbody>
          {([
            ['Kalshi (demo)', tr('prediction.roleExecute'), money(b.kalshi_demo_usd), String(g.kalshi_trading_enabled)],
            ['Polymarket US', tr('prediction.roleExecute'), money(b.polymarket_us_usd), String(g.pmus_trading_enabled)],
            ['Kalshi (prod)', tr('prediction.roleRealMoney'), tDyn(String(b.kalshi_prod_usd)), tr('prediction.tradingGated')],
            // Read-only data source (never trades): geoblocked in the US, no account.
            ['Polymarket Global', tr('prediction.roleReference'), money(0), tr('prediction.tradingReadonly')],
          ] as ReactNode[][]).map((r, i) => (
            <tr key={`v${i}`}>{r.map((c, j) => <td key={j} style={{ textAlign: j === 0 ? 'left' : 'right' }}>{c}</td>)}</tr>
          ))}
          {/* gates & risk controls + API budget — label left, value right across the row */}
          {controls.map(([k, v], i) => (
            <tr key={`g${i}`}>
              <td style={{ textAlign: 'left', fontWeight: 700 }}>{k}</td>
              <td colSpan={3} style={{ textAlign: 'right' }}>{v}</td>
            </tr>
          ))}
          {/* blocked / guardrails — full-width red rows */}
          {blocked.map((x, i) => (
            <tr key={`b${i}`}>
              <td colSpan={4} style={{ textAlign: 'left', color: 'var(--error)' }}>⛔ {tDyn(x)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {/* — Notes: one uniform small-print list. (The executable-venues line was deduped — it
          repeated the Trading column above; the budget bar was deduped — it repeated the %.) — */}
      <ul style={{ marginTop: 10, paddingLeft: 16, fontSize: 10, color: 'var(--text-muted)', ...mono }}>
        <li style={{ marginBottom: 4 }}>{tr('prediction.venueGlobalNote')}</li>
        <li style={{ marginBottom: 4 }}>{tr('prediction.budgetResetNote')}</li>
      </ul>
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
      <KV rows={[
        [tr('prediction.lblUsedToday'), `${ab.used ?? '—'} / ${ab.cap ?? '—'}`],
        [tr('prediction.lblUtilisation'), pct(ab.pct, 0)],
        [tr('prediction.lblRemaining'), ab.cap != null && ab.used != null ? (ab.cap - ab.used) : '—'],
        ...(ab.month_used != null ? [[tr('prediction.lblMonthBackstop'), `${ab.month_used} / ${ab.month_cap}`] as [string, ReactNode]] : []),
      ]} />
      <div style={{ height: 10, background: 'var(--bg-tertiary)', border: '1px solid var(--border-subtle)' }}>
        <div style={{ width: `${frac * 100}%`, height: '100%', background: frac > 0.8 ? 'var(--error)' : 'var(--success)' }} />
      </div>
      <div style={{ marginTop: 8, fontSize: 10, color: 'var(--text-muted)', ...mono }}>{tr('prediction.budgetResetNote')}</div>
    </div>
  );
}

function FormCard() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCForm(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const teams = (data?.teams ?? []);  // all 48 teams
  return (
    <div>
      <Title sub={tr('prediction.subForm')}>Recent Form</Title>
      <DataTable cols={['FIFA', tr('prediction.team'), tr('prediction.colForm'), 'wGD', tr('prediction.colRecent')]}
        rows={teams.map((t: any) => [
          t.fifa_rank != null ? `#${t.fifa_rank}` : '—', CN(t.name), (t.form_z >= 0 ? '+' : '') + t.form_z.toFixed(2),
          (t.weighted_gd >= 0 ? '+' : '') + t.weighted_gd.toFixed(2),
          (t.recent ?? []).join(' '),
        ])}
        sortableCols={[0, 2, 3]}
        sortVals={teams.map((t: any) => [t.fifa_rank, null, t.form_z, t.weighted_gd, null])}
        defaultSort={{ col: 2, dir: 'desc' }} />
    </div>
  );
}

// Team STYLE taxonomy — a 48 teams × 10 styles MATRIX (a team can play 1–2 styles, so
// 1–2 filled cells per row). Each filled cell shows the team's possession; within a style
// column teams are ranked by possession (cells do NOT sum to 100%). Built from a curated
// research prior blended with live API-Football metrics. Descriptive scouting aid.
function TeamStyles() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCStyles(), []);
  const [sortCode, setSortCode] = useState<string | null>(null);   // null = default block-diagonal order
  const [sortDir, setSortDir] = useState<'desc' | 'asc'>('desc');
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const teams = (data?.teams ?? []);
  const styles = (data?.styles ?? []);                 // [{code, label}]
  const codeIdx: Record<string, number> = {};
  styles.forEach((s: any, i: number) => { codeIdx[s.code] = i; });
  const cellOf = (t: any, code: string) => (t.styles || []).find((s: any) => s.code === code);
  const shortLabel = (label: string) => (label || '').split(' ')[0];   // fallback header (Chinese part)
  const styleName = (s: any) => tr('prediction.style.' + s.code, { defaultValue: shortLabel(s.label) });
  const onSort = (code: string) => {
    if (sortCode === code) setSortDir(d => (d === 'desc' ? 'asc' : 'desc'));
    else { setSortCode(code); setSortDir('desc'); }
  };
  // Default: primary style column then possession desc (readable block-diagonal matrix).
  // When a header is clicked: sort by that style column's possession; teams without that
  // style (cell = '·') sink to the bottom. Click again toggles asc/desc.
  const cellPoss = (t: any, code: string) => { const c = cellOf(t, code); return c ? c.poss : -1; };
  const rows = [...teams].sort((a: any, b: any) => {
    if (sortCode) {
      const va = cellPoss(a, sortCode), vb = cellPoss(b, sortCode);
      if (va !== vb) return sortDir === 'desc' ? vb - va : va - vb;
      return a.team_id < b.team_id ? -1 : 1;
    }
    const ca = codeIdx[a.styles?.[0]?.code] ?? 99, cb = codeIdx[b.styles?.[0]?.code] ?? 99;
    return ca - cb || (b.poss - a.poss);
  });
  const th: CSSProperties = { padding: '4px 5px', borderBottom: '2px solid var(--border-subtle)', fontWeight: 700, color: 'var(--text-secondary)', whiteSpace: 'nowrap', textAlign: 'center' };
  const td: CSSProperties = { padding: '3px 5px', borderBottom: '1px solid var(--border-subtle)', textAlign: 'center' };
  return (
    <div>
      <Title sub={tr('prediction.subStyles')}>Team Styles</Title>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginBottom: 8 }}>
        {tr('prediction.stylesNote')} · {data?.n ?? 0} {tr('prediction.lblMatchesAll')}
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ borderCollapse: 'collapse', fontSize: 10, ...mono, width: '100%' }}>
          <thead>
            <tr>
              <th style={{ ...th, position: 'sticky', left: 0, background: 'var(--bg-secondary)', textAlign: 'left', zIndex: 1 }}>{tr('prediction.team')}</th>
              {styles.map((s: any) => (
                <th key={s.code} onClick={() => onSort(s.code)} title={s.label}
                    style={{ ...th, cursor: 'pointer', color: sortCode === s.code ? 'var(--accent-primary)' : 'var(--text-secondary)' }}>
                  {styleName(s)}{sortCode === s.code ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((t: any) => (
              <tr key={t.team_id}>
                <td style={{ ...td, position: 'sticky', left: 0, background: 'var(--bg-primary)', textAlign: 'left', whiteSpace: 'nowrap', fontWeight: 600, color: 'var(--text-primary)' }}>{CN(t.name)}</td>
                {styles.map((s: any) => {
                  const c = cellOf(t, s.code);
                  return (
                    <td key={s.code}
                        style={{ ...td, background: c ? 'var(--bg-tertiary)' : 'transparent', color: c ? 'var(--text-primary)' : 'var(--text-muted)', fontWeight: c ? 700 : 400 }}
                        title={c ? `${tCountry(t.name)} · ${s.label} · poss ${Math.round(c.poss * 100)}% · #${c.rank} in style` : ''}>
                      {c ? Math.round(c.poss * 100) : '·'}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
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
  // Cache-buster: PDFs are served with a fixed URL over the tunnel and browsers cache
  // them hard, so a freshly-regenerated report (e.g. the new 11W-8L bet log) can look
  // stale. A per-mount version token forces a fresh fetch each time the view is opened.
  const [v] = useState(() => Date.now());
  const cur = reports.find((r) => r.key === active) ?? reports[0];
  const url = `${API_BASE}/data/${cur.file}?v=${v}`;
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

// Mark-to-market price tracks: per-contract ¢ + probability at each milestone
// (PRE→T15→T30→HT→T60→T75→FT), grading whether the market confirmed our pre-match pick.
function PriceTrack() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCMilestoneMarks(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const matches = data?.matches ?? [];
  const sideName: Record<string, string> = { home: tr('prediction.home'), draw: tr('prediction.draw'), away: tr('prediction.away') };
  return (
    <div>
      <Title sub={tr('prediction.subPriceTrack')}>Price Track</Title>
      {!matches.length ? <div className="text-xs py-2" style={{ color: 'var(--text-muted)', ...mono }}>—</div> :
        matches.map((m: any) => {
          const b = m.our_bet || {}; const mtm = m.mtm;
          return (
            <div key={m.fixture_id} className="card" style={{ marginBottom: 12 }}>
              <div style={{ fontWeight: 700, fontSize: 12, ...mono, marginBottom: 4 }}>
                {VS(m.home?.name, m.away?.name, ' vs ')}
                {m.settled && <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}> · {m.score}</span>}
              </div>
              <div style={{ fontSize: 10.5, ...mono, marginBottom: 6, color: 'var(--text-secondary)', display: 'flex', flexDirection: 'column', gap: 1 }}>
                {b.bet === false ? (
                  <span style={{ color: 'var(--text-muted)' }}>{tr('prediction.ourBet')}: {tr('prediction.noBet')}</span>
                ) : (() => {
                  const s = m.smart_exit;
                  return (<>
                    {/* line 1: what we bet — side · type · stake */}
                    <div>{tr('prediction.ptBet')}: <b style={{ color: 'var(--text-primary)' }}>{CN(b.pick_team)}</b> <span style={{ color: 'var(--text-muted)' }}>· {tr('prediction.ptSingle')}</span>{b.stake_usd != null ? <> · ${num(b.stake_usd, 2)}</> : null}</div>
                    {/* line 2: the realised path — buy(when/price) → sell(when/price) · realised · mode */}
                    {s ? (
                      <div>　{tr('prediction.ptBuy')} <b>PRE {cc(b.entry_cents)}</b> → {tr('prediction.ptSell')} <b>{s.sold_min}′ {Math.round(s.sold_c)}¢</b> · {tr('prediction.lblRealized')} <b style={{ color: s.pnl_c >= 0 ? 'var(--success)' : 'var(--error)' }}>{s.pnl_c >= 0 ? '+' : ''}{cc(s.pnl_c)}</b> <span style={{ color: 'var(--accent-primary)' }}>{tr('prediction.ptTagTiming')}</span></div>
                    ) : mtm ? (
                      <div>　{tr('prediction.ptBuy')} <b>PRE {cc(b.entry_cents)}</b> → {tr('prediction.lblSettle')} <b>{cc(mtm.ft_c)}</b> · <b style={{ color: mtm.pnl_c >= 0 ? 'var(--success)' : 'var(--error)' }}>{mtm.pnl_c >= 0 ? '+' : ''}{cc(mtm.pnl_c)} {mtm.won ? tr('prediction.betWon') : tr('prediction.betLost')}</b> <span style={{ color: 'var(--text-muted)' }}>{tr('prediction.ptTagHold')}</span></div>
                    ) : null}
                    {/* line 3 (only when we cashed out): the hold-to-FT reference */}
                    {s && mtm && (
                      <div style={{ color: 'var(--text-muted)' }}>　{tr('prediction.ptIfHeld')}: {cc(mtm.ft_c)} · {mtm.pnl_c >= 0 ? '+' : ''}{cc(mtm.pnl_c)}</div>
                    )}
                  </>);
                })()}
                {/* in-play entry line — the SECOND stream, on the same trajectory (三视图统一):
                    盘中 <side> <milestone> · buy entry¢ → sell/settle · realised¢ (per-contract). */}
                {m.inplay && (() => {
                  const ip = m.inplay; const ix = ip.exit; const rc = ip.realized_pnl_cents;
                  return (
                    <div style={{ color: 'var(--accent-primary)' }}>{tr('prediction.ptInplay')}: <b style={{ color: 'var(--text-primary)' }}>{CN(ip.pick_team)}</b> {ip.milestone} · {tr('prediction.ptBuy')} <b>{cc(ip.entry_cents)}</b> → {ix
                      ? <>{tr('prediction.ptSell')} <b>{ix.sold_min}′ {Math.round(ix.sold_c)}¢</b></>
                      : <>{tr('prediction.lblSettle')} <b>{cc(ip.won ? 100 : 0)}</b></>} · {tr('prediction.lblRealized')} <b style={{ color: rc >= 0 ? 'var(--success)' : 'var(--error)' }}>{rc >= 0 ? '+' : ''}{cc(rc)}</b></div>
                  );
                })()}
                {/* argmax reference line */}
                {b.argmax && (() => {
                  const a = b.argmax; const am = a.mtm;
                  const aColor = am ? (am.pnl_c > 0 ? 'var(--success)' : am.pnl_c < 0 ? 'var(--error)' : 'var(--text-muted)') : 'var(--text-muted)';
                  return (
                    <div style={{ color: 'var(--text-muted)' }}>{tr('prediction.argmaxShort')}: <b>{CN(a.pick_team)}</b>{am && <> · {cc(am.entry_c)} → {cc(am.ft_c)} · <span style={{ color: aColor }}>{am.pnl_c >= 0 ? '+' : ''}{cc(am.pnl_c)}</span></>}</div>
                  );
                })()}
              </div>
              <DataTable
                cols={[tr('prediction.colMilestone'), tr('prediction.colScore'), `${sideName.home}¢`, `${sideName.draw}¢`, `${sideName.away}¢`]}
                rows={(m.marks ?? []).map((mk: any) => {
                  const hl = (side: string) => ({ fontWeight: b.side === side ? 700 : 400, color: b.side === side ? 'var(--text-primary)' : 'var(--ink)' });
                  const ipHere = m.inplay && mk.milestone === m.inplay.milestone;   // ◆ = 盘中入场 here
                  return [
                    <b>{mk.milestone}{ipHere ? <span style={{ color: 'var(--accent-primary)' }}> ◆</span> : null}</b>, mk.score,
                    <span style={hl('home')}>{cc(mk.poly_c?.home)}</span>,
                    <span style={hl('draw')}>{cc(mk.poly_c?.draw)}</span>,
                    <span style={hl('away')}>{cc(mk.poly_c?.away)}</span>,
                  ];
                })} />
            </div>
          );
        })}
      <div style={{ marginTop: 8, fontSize: 10, color: 'var(--text-muted)', ...mono }}>{tr('prediction.priceTrackNote')}</div>
    </div>
  );
}

// ── dispatcher ────────────────────────────────────────────────────────────────
// ── MicroFootball Sim ──────────────────────────────────────────────────────────
// AI football-match simulations (10× per matchup): per-sim replay (GIF + interactive trajectory
// canvas) + stats, a 10-sim aggregate (the implied prediction), and on-demand LOCAL-nemotron
// analysis (aggregate + single-sim). Index from /data; heavy gif/trajectory from the /sim mount.
function AiResult({ state, onRun, label }: { state: { loading: boolean; text?: string; error?: string; cached?: boolean }; onRun: () => void; label: string }) {
  const { t: tr } = useTranslation();
  return (
    <div style={{ marginTop: 6 }}>
      <button onClick={onRun} disabled={state.loading}
        style={{ padding: '3px 12px', fontSize: 11, fontFamily: 'var(--font-mono)', fontWeight: 700, letterSpacing: '.04em', border: '1px solid var(--accent-primary)', borderRadius: 4, background: state.loading ? 'var(--bg-subtle)' : 'var(--bg-tertiary)', color: 'var(--text-primary)', cursor: state.loading ? 'default' : 'pointer' }}>
        {state.loading ? tr('prediction.mfAiLoading') : label}
      </button>
      {state.loading && <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginTop: 4, fontStyle: 'italic' }}>{tr('prediction.mfAiNote')}</div>}
      {state.error && <div style={{ fontSize: 11, color: 'var(--error)', ...mono, marginTop: 4 }}>{tr('prediction.mfAiError')}: {state.error}</div>}
      {state.text && (
        <div className="card" style={{ marginTop: 6, padding: '8px 10px', fontSize: 12, lineHeight: 1.6, color: 'var(--text-primary)', whiteSpace: 'pre-wrap' }}>
          <div style={{ fontSize: 9, color: 'var(--text-muted)', ...mono, marginBottom: 4, textTransform: 'uppercase', letterSpacing: '.06em' }}>🤖 {tr('prediction.mfAiTitle')} · Someo Park Local Model 120B{state.cached ? ` · ⚡ ${tr('prediction.mfCached')}` : ''}</div>
          {state.text}
        </div>
      )}
    </div>
  );
}

// Latest scheduled kickoff (ET string) for a matchup, matched by team names in either order
// against the World Cup schedule — if the same fixture recurs, the latest kickoff wins.
function scheduleDate(schedMatches: any[], home: string, away: string): string {
  const norm = (s: string) => (s || '').toLowerCase().replace(/[^a-z]/g, '');
  const nh = norm(home), na = norm(away);
  const hits = (schedMatches || []).filter((x: any) => {
    const a1 = norm(x.home?.name), a2 = norm(x.away?.name);
    return (a1 === nh && a2 === na) || (a1 === na && a2 === nh);
  });
  if (!hits.length) return '';
  hits.sort((p: any, q: any) => String(q.kickoff).localeCompare(String(p.kickoff)));  // latest first
  return hits[0].et || '';
}

function MicroFootballSim() {
  const { t: tr, i18n } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCMicrofootball(), []);
  const { data: schedData } = useApi<any>(() => getWCSchedule(), []);
  const { data: dfmData } = useApi<any>(() => getWCDfm(), []);
  const [mIdx, setMIdx] = useState(0);
  const [sIdx, setSIdx] = useState(0);
  const [viz, setViz] = useState<'gif' | 'canvas'>('gif');
  const [dfmMode, setDfmMode] = useState<'real_anchored' | 'engine_faithful'>('real_anchored');
  const [aiAgg, setAiAgg] = useState<{ loading: boolean; text?: string; error?: string; cached?: boolean }>({ loading: false });
  const [aiSim, setAiSim] = useState<{ loading: boolean; text?: string; error?: string; cached?: boolean }>({ loading: false });
  const [aiDfm, setAiDfm] = useState<{ loading: boolean; text?: string; error?: string; cached?: boolean }>({ loading: false });
  // Cross-artifact focus: if we arrived by clicking a country elsewhere, open the matchup
  // that team plays in (so the scroll/highlight lands, not a random default matchup).
  const focus = usePredictionFocus();
  useEffect(() => {
    if (!focus.country) return;
    const mus: any[] = data?.matchups ?? [];
    const i = mus.findIndex((mm) => countryKey(mm.home_name) === focus.country || countryKey(mm.away_name) === focus.country);
    if (i >= 0) { setMIdx(i); setSIdx(0); }
  }, [focus.country, focus.nonce, data]);
  if (loading) return <Loading />;
  if (error) return <ErrorBox e={error} />;
  const matchups: any[] = data?.matchups ?? [];
  if (!matchups.length) return <div className="text-xs py-2" style={{ color: 'var(--text-muted)', ...mono }}>{tr('prediction.mfNone')}</div>;
  const m = matchups[Math.min(mIdx, matchups.length - 1)];
  const sims: any[] = m.sims ?? [];
  const sim = sims[Math.min(sIdx, sims.length - 1)];
  // Hs/As = plain strings (titles, canvas props, table headers). H/A = clickable nodes
  // (the cross-artifact country navigator) used in the display rows below.
  const Hs = tCountry(m.home_name), As = tCountry(m.away_name);
  const H = CN(m.home_name), A = CN(m.away_name);
  const a = m.aggregate;

  const tab = (active: boolean, onClick: () => void, label: ReactNode, key: string) => (
    <button key={key} onClick={onClick} style={{ padding: '3px 10px', fontSize: 11, fontFamily: 'var(--font-mono)', fontWeight: active ? 700 : 400, color: active ? 'var(--text-primary)' : 'var(--text-muted)', background: active ? 'var(--bg-tertiary)' : 'transparent', border: `1px solid ${active ? 'var(--accent-primary)' : 'var(--border-subtle)'}`, borderRadius: 4, cursor: 'pointer', marginRight: 6, marginBottom: 4 }}>{label}</button>
  );
  const runAgg = async () => { setAiAgg({ loading: true }); try { const r = await analyzeMicrofootball(m.id, null, i18n.language); setAiAgg({ loading: false, text: r.analysis, cached: r.cached }); } catch (e: any) { setAiAgg({ loading: false, error: String(e?.message || e) }); } };
  const runSim = async () => { setAiSim({ loading: true }); try { const r = await analyzeMicrofootball(m.id, sim.sim_id, i18n.language); setAiSim({ loading: false, text: r.analysis, cached: r.cached }); } catch (e: any) { setAiSim({ loading: false, error: String(e?.message || e) }); } };
  // DFM analysis of the currently-shown diffusion view; cached on box A the same way as agg/sim.
  const runDfm = async () => { setAiDfm({ loading: true }); try { const r = await analyzeMicrofootball(m.id, null, i18n.language, { mode: 'dfm', dfm_mode: dfmMode }); setAiDfm({ loading: false, text: r.analysis, cached: r.cached }); } catch (e: any) { setAiDfm({ loading: false, error: String(e?.message || e) }); } };

  // stat rows for the per-sim table. Null-safe: an undefined stat renders '—', not "null%" —
  // e.g. save_pct is null when the keeper faced ZERO on-target shots (0/0 has no save rate).
  const v = (x: any, suffix = '') => (x == null ? '—' : `${x}${suffix}`);
  const statRow = (key: string, fmt: (s: any) => ReactNode) => [tr('prediction.' + key), fmt(sim.stats.home), fmt(sim.stats.away)];
  const simRows: ReactNode[][] = [
    statRow('mfPossession', (s) => v(s.possession_pct, '%')),
    statRow('mfShots', (s) => v(s.shots)),
    statRow('mfSot', (s) => v(s.shots_on_target_pct, '%')),
    statRow('mfXg', (s) => v(s.xg)),
    statRow('mfPasses', (s) => v(s.passes)),
    statRow('mfPassPct', (s) => v(s.pass_completion_pct, '%')),
    statRow('mfOffsides', (s) => v(s.offsides)),
    statRow('mfSequences', (s) => v(s.sequences)),
    statRow('mfSaves', (s) => v(s.save_pct, '%')),
    statRow('mfRecovery', (s) => v(s.recovery_sec, 's')),
  ];

  return (
    <div>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginBottom: 6 }}>{tr('prediction.subMicrofootball')}</div>
      {/* matchup tabs — each shows the fixture's scheduled date/time (from the World Cup schedule) */}
      <div style={{ marginBottom: 8 }}>
        {matchups.map((mm, i) => {
          const dt = scheduleDate(schedData?.matches || [], mm.home_name, mm.away_name);
          return tab(i === mIdx, () => { setMIdx(i); setSIdx(0); setAiSim({ loading: false }); setAiAgg({ loading: false }); setAiDfm({ loading: false }); },
            <span>{tCountry(mm.home_name)} v {tCountry(mm.away_name)}{dt && <span style={{ display: 'block', fontSize: 9, fontWeight: 400, color: 'var(--text-muted)', marginTop: 1 }}>{dt}</span>}</span>, mm.id);
        })}
      </div>

      {/* aggregate panel — the implied prediction across 10 sims */}
      <div className="card" style={{ marginBottom: 12, padding: '8px 10px' }}>
        <div style={{ fontSize: 11, fontWeight: 700, ...mono, marginBottom: 6, color: 'var(--text-primary)' }}>{tr('prediction.mfAggregate')} · {m.n_sims} {tr('prediction.mfSims')}</div>
        {/* W/D/L distribution bar */}
        <div style={{ display: 'flex', height: 18, borderRadius: 3, overflow: 'hidden', fontSize: 9, ...mono, marginBottom: 6 }}>
          <div title={`${Hs} ${a.record.home_wins}`} style={{ width: `${a.win_pct.home * 100}%`, background: '#3b82f6', color: '#fff', textAlign: 'center', lineHeight: '18px' }}>{a.record.home_wins}</div>
          <div title={`draw ${a.record.draws}`} style={{ width: `${a.win_pct.draw * 100}%`, background: 'var(--text-muted)', color: '#fff', textAlign: 'center', lineHeight: '18px' }}>{a.record.draws}</div>
          <div title={`${As} ${a.record.away_wins}`} style={{ width: `${a.win_pct.away * 100}%`, background: '#ef4444', color: '#fff', textAlign: 'center', lineHeight: '18px' }}>{a.record.away_wins}</div>
        </div>
        <KV rows={[
          [tr('prediction.mfWinPct'), <span style={mono}>{H} {pct(a.win_pct.home, 0)} · {tr('prediction.drawResult')} {pct(a.win_pct.draw, 0)} · {A} {pct(a.win_pct.away, 0)}</span>],
          [tr('prediction.mfAvgScore'), <span style={mono}>{H} {a.avg_score.home} – {a.avg_score.away} {A}</span>],
          [tr('prediction.mfAvgXg'), <span style={mono}>{H} {a.avg_xg.home} · {A} {a.avg_xg.away}</span>],
          [tr('prediction.mfAvgPossession'), <span style={mono}>{H} {a.avg_possession.home}% · {A} {a.avg_possession.away}%</span>],
          [tr('prediction.mfScoreDist'), <span style={mono}>{(a.score_distribution || []).map((d: any) => `${d.score} (${Math.round((d.count / m.n_sims) * 100)}%)`).join('　')}</span>],
        ]} />
        <AiResult state={aiAgg} onRun={runAgg} label={tr('prediction.mfAiAnalyze')} />
      </div>

      {/* DFM amplification panel — the diffusion model's 5000-sample distribution for this
          matchup (dfm/football production snapshot). Two views of the same samples:
          real_anchored (tournament-level intensity calibration, for real-match prediction)
          and engine_faithful (the sims' own distribution, amplified). */}
      {(() => {
        const d = dfmData?.matchups?.[m.id];
        if (!d) return null;
        const mode = d[dfmMode] ?? d.real_anchored;
        const st = (ch: string) => mode.stats?.[ch];
        const q = (ch: string, mul = 1, fix = 1, suffix = '') => {
          const s = st(ch);
          if (!s) return ['—', '—'];
          return [s.home, s.away].map((x: any) =>
            `${(x.p50 * mul).toFixed(fix)}${suffix} [${(x.p5 * mul).toFixed(fix)}–${(x.p95 * mul).toFixed(fix)}]`);
        };
        const [posH, posA] = q('poss_share', 100, 0, '%');
        const [shH, shA] = q('shots', 1, 0);
        const [coH, coA] = q('corners', 1, 0);
        const [xgH, xgA] = q('xg', 1, 1);
        return (
          <div className="card" style={{ marginBottom: 12, padding: '8px 10px' }}>
            <div className="flex items-center justify-between" style={{ marginBottom: 6 }}>
              <span style={{ fontSize: 11, fontWeight: 700, ...mono, color: 'var(--text-primary)' }}>
                {tr('prediction.dfmTitle')} · {d.n_samples} ({tr('prediction.dfmFromSims', { n: d.n_source_sims })})
              </span>
              <span>
                {tab(dfmMode === 'real_anchored', () => setDfmMode('real_anchored'), tr('prediction.dfmModeAnchored'), 'dfm-ra')}
                {tab(dfmMode === 'engine_faithful', () => setDfmMode('engine_faithful'), tr('prediction.dfmModeEngine'), 'dfm-ef')}
              </span>
            </div>
            <div style={{ display: 'flex', height: 18, borderRadius: 3, overflow: 'hidden', fontSize: 9, ...mono, marginBottom: 6 }}>
              <div title={Hs} style={{ width: `${mode.wdl.home * 100}%`, background: '#3b82f6', color: '#fff', textAlign: 'center', lineHeight: '18px' }}>{pct(mode.wdl.home, 0)}</div>
              <div title="draw" style={{ width: `${mode.wdl.draw * 100}%`, background: 'var(--text-muted)', color: '#fff', textAlign: 'center', lineHeight: '18px' }}>{pct(mode.wdl.draw, 0)}</div>
              <div title={As} style={{ width: `${mode.wdl.away * 100}%`, background: '#ef4444', color: '#fff', textAlign: 'center', lineHeight: '18px' }}>{pct(mode.wdl.away, 0)}</div>
            </div>
            <KV rows={[
              [tr('prediction.mfWinPct'), <span style={mono}>{H} {pct(mode.wdl.home, 1)} · {tr('prediction.drawResult')} {pct(mode.wdl.draw, 1)} · {A} {pct(mode.wdl.away, 1)}</span>],
              [tr('prediction.mfAvgScore'), <span style={mono}>{H} {mode.avg_goals?.replace('-', ' – ')} {A}</span>],
              [tr('prediction.mfScoreDist'), <span style={mono}>{(mode.scoreline_top10 || []).slice(0, 6).map((l: any) => `${l.score} (${Math.round(l.pct * 100)}%)`).join('　')}</span>],
              [tr('prediction.mfPossession'), <span style={mono}>{H} {posH} · {A} {posA}</span>],
              [tr('prediction.mfShots'), <span style={mono}>{H} {shH} · {A} {shA}</span>],
              [tr('prediction.dfmCorners'), <span style={mono}>{H} {coH} · {A} {coA}</span>],
              [tr('prediction.mfXg'), <span style={mono}>{H} {xgH} · {A} {xgA}</span>],
              [tr('prediction.dfmCards'), <span style={mono}>🟨 {mode.cards_per_match?.yellow} · 🟥 {mode.cards_per_match?.red}</span>],
            ]} />
            <div style={{ fontSize: 9, color: 'var(--text-muted)', ...mono, marginTop: 4 }}>{tr('prediction.dfmNote')} · {d.ts}</div>
            <AiResult state={aiDfm} onRun={runDfm} label={tr('prediction.dfmAiAnalyze')} />
          </div>
        );
      })()}

      {/* sim selector */}
      <div style={{ marginBottom: 6 }}>
        {sims.map((s, i) => tab(i === sIdx, () => { setSIdx(i); setAiSim({ loading: false }); }, `#${i + 1} (${s.score.home}-${s.score.away})`, s.sim_id))}
      </div>

      {/* per-sim panel */}
      {sim && (
        <div className="card" style={{ padding: '8px 10px' }}>
          <div className="flex items-center justify-between" style={{ marginBottom: 6 }}>
            <span style={{ fontSize: 13, fontWeight: 700, ...mono, color: 'var(--text-primary)' }}>{H} <b>{sim.score.home}-{sim.score.away}</b> {A}</span>
            <span>{tab(viz === 'gif', () => setViz('gif'), tr('prediction.mfGif'), 'gif')}{tab(viz === 'canvas', () => setViz('canvas'), tr('prediction.mfCanvas'), 'canvas')}</span>
          </div>
          <div style={{ textAlign: 'center', marginBottom: 6 }}>
            {viz === 'gif'
              ? <img src={`${API_BASE}${sim.gif_url}`} alt="replay" width={300} style={{ borderRadius: 4, display: 'inline-block', maxWidth: '100%' }} />
              : <TrajectoryPlayer src={`${API_BASE}${sim.traj_url}`} homeName={Hs} awayName={As} />}
          </div>
          <DataTable cols={['', Hs, As]} rows={simRows} />
          {sim.summary && <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginTop: 6 }}>{sim.summary.replace(/\s*\d+\s*拍\s*\/\s*/, '')}</div>}
          <AiResult state={aiSim} onRun={runSim} label={tr('prediction.mfAiAnalyzeSim')} />
        </div>
      )}
    </div>
  );
}

const REGISTRY: Record<string, () => ReactElement> = {
  wc_microfootball: MicroFootballSim,
  wc_pricetrack: PriceTrack,
  wc_champion: ChampionOdds,
  wc_reach_round: ReachRound,
  wc_golden_boot: GoldenBoot,
  wc_squad: SquadStrength,
  wc_styles: TeamStyles,
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
  wc_overview: OverviewModelNotes,
  wc_venues: VenuesApi,
  wc_budget: Budget,   // kept for backward-compat (deep-links/chat); merged into wc_venues in the grid
  wc_pdfs: Pdfs,
};

const KEY_BY_TYPE: Record<string, string> = Object.fromEntries(PREDICTION_ITEMS.map(i => [i.type, i.i18nKey]));
// Artifact types that carry the Regulation/Advances selector — rendered on the artifact's
// title row (top, right-aligned), matching the stock-mode viewers' header-row selector.
const ADVANCE_SELECTOR_TYPES = new Set(['wc_match_pricing', 'wc_divergence', 'wc_inplay', 'wc_predictions']);

export default function PredictionArtifact({ type, params }: { type: string; params?: any }) {
  const { t } = useTranslation();
  // Cross-artifact country focus (set when the user clicks a country in another artifact).
  const focusCountry: string | null = params?.focusCountry ?? null;
  const focusNonce: number = params?.focusNonce ?? 0;
  const containerRef = useRef<HTMLDivElement>(null);
  useCountryFocusScroll(containerRef, focusCountry, focusNonce);

  const View = REGISTRY[type];
  if (!View) return <div className="text-xs py-3" style={{ color: 'var(--text-muted)', ...mono }}>Unknown artifact: {type}</div>;
  const key = KEY_BY_TYPE[type];
  return (
    <PredictionFocusContext.Provider value={{ country: focusCountry, nonce: focusNonce, selfType: type }}>
      <div ref={containerRef}>
        {key && (
          <div className="flex items-center justify-between" style={{ marginBottom: 6, minHeight: 22 }}>
            <div style={{ fontSize: 13, fontWeight: 700, letterSpacing: '.1em', textTransform: 'uppercase', color: 'var(--text-primary)', ...mono }}>{t(`prediction.${key}`)}</div>
            {ADVANCE_SELECTOR_TYPES.has(type) && <AdvanceModeToggle />}
          </div>
        )}
        <View />
      </div>
    </PredictionFocusContext.Provider>
  );
}

export const isPredictionArtifact = (type?: string) => !!type && type.startsWith('wc_');
