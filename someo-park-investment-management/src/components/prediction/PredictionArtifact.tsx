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
} from '../../lib/api';
import { useState } from 'react';
import { PREDICTION_ITEMS } from './PredictionArtifactGrid';
import { tCountry } from '../../i18n/countries';
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
        rows={champ.map((c: any) => [tCountry(c.name), c.fifa_rank != null ? `#${c.fifa_rank}` : '—', c.group, pct(c.p_champion),
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
                <td style={{ ...td, textAlign: 'left', color: 'var(--text-primary)', fontWeight: 600, ...mono }}>{tCountry(e.name)}</td>
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
  const gb = (data?.golden_boot ?? []).slice(0, 16);
  return (
    <div>
      <Title sub={tr('prediction.subGoldenBoot')}>Golden Boot</Title>
      <DataTable cols={[tr('prediction.colPlayer'), tr('prediction.colTeam'), tr('prediction.colGoals'), 'P(boot)', 'E[goals]']}
        rows={gb.map((p: any) => [p.name, tCountry(p.team), p.goals ?? 0, pct(p.p_golden_boot), num(p.e_goals, 2)])} />
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
          t.fifa_rank != null ? `#${t.fifa_rank}` : '—', tCountry(t.name), (t.score_z >= 0 ? '+' : '') + t.score_z.toFixed(2),
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
    [cap?.liveT, cap?.live], [cap?.otherT, cap?.other],
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
            if (!a || !a.model) return [`${tCountry(m.home)} v ${tCountry(m.away)}`, '—', '—',
              <span style={{ color: 'var(--text-muted)' }}>{tr('prediction.regulationOnly')}</span>];
            const e = a.edge_vs_market || {};
            const side = Math.abs(e.home ?? 0) >= Math.abs(e.away ?? 0) ? 'H' : 'A';
            const val = side === 'H' ? (e.home ?? 0) : (e.away ?? 0);
            return [
              `${tCountry(m.home)} v ${tCountry(m.away)}`,
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
              `${tCountry(m.home)} v ${tCountry(m.away)}`,
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
            if (!a || !a.model) return [`${tCountry(m.home?.name)} v ${tCountry(m.away?.name)}`, m.et ?? '',
              <span style={{ color: 'var(--text-muted)' }}>{tr('prediction.regulationOnly')}</span>, '—', '—', '—'];
            return [`${tCountry(m.home?.name)} v ${tCountry(m.away?.name)}`, m.et ?? '',
              pct(a.model.home, 0), cc(a.model.cents?.home), pct(a.model.away, 0), cc(a.model.cents?.away)];
          })} />
      ) : (
        <DataTable cols={[tr('prediction.match'), 'ET', 'H', 'H¢', 'D', 'D¢', 'A', 'A¢', 'O2.5']}
          rows={ms.map((m: any) => [`${tCountry(m.home?.name)} v ${tCountry(m.away?.name)}`, m.et ?? '',
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
            {tCountry(m.home?.name)} vs {tCountry(m.away?.name)} <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>{m.et}</span>
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

function Schedule() {
  const { t: tr } = useTranslation();
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
  return (
    <div>
      <Title sub={`${tr('prediction.subSchedule')} · ${ms.length} ${tr('prediction.lblMatches')}${played ? ` (${played} ${tr('prediction.finished')})` : ''}`}>Schedule</Title>
      <DataTable cols={['ET', tr('prediction.colRound'), tr('prediction.match'), tr('prediction.colResult')]}
        rows={ms.map((m: any) => [
          m.et ?? m.kickoff, roundLabel(m.round),
          `${tCountry(m.home?.name)} v ${tCountry(m.away?.name)}`,
          m.finished
            ? <span style={{ fontWeight: 700 }}>{m.score}</span>
            : <span style={{ color: 'var(--text-muted)' }}>{m.status === 'NS' ? '—' : m.status}</span>,
        ])} />
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
              {tCountry(m.home.name)} <b>{m.score}</b> {tCountry(m.away.name)}
            </span>
            <span style={{ fontSize: 11, color: 'var(--text-muted)', ...mono }}>{
              m.period === 'pens' ? `${tr('prediction.periodPens')}${m.shootout ? ` ${m.shootout.home}-${m.shootout.away}` : ''}`
                : m.period === 'et' ? `${tr('prediction.periodEt')} ${m.minute}'`
                : `${m.minute}'`
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
              tr('prediction.side.' + o.side, { defaultValue: o.side }),
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
  const sideLabel = (b: any) => (b.pick === 'draw' ? tr('prediction.drawResult') : tCountry(b.pick_team));
  const argmaxLabel = (b: any) => (b.model_pick === 'draw' ? tr('prediction.drawResult') : tCountry(b.model_pick_team));
  const wl = (won: boolean) => <span style={{ color: won ? 'var(--success)' : 'var(--error)', fontWeight: 700 }}>{won ? tr('prediction.betWon') : tr('prediction.betLost')}</span>;
  const cVal = (v: number | null | undefined): ReactNode => v == null ? '—' : <span style={{ color: v >= 0 ? 'var(--success)' : 'var(--error)' }}>{v >= 0 ? '+' : ''}{cc(v)}</span>;
  const matchup = (b: any) => `${tCountry(b.home)} ${b.score} ${tCountry(b.away)}`;

  const tab = (m: typeof mode, label: string) => (
    <button key={m} onClick={() => setMode(m)} style={{ padding: '3px 10px', fontSize: 11, fontFamily: 'var(--font-mono)', fontWeight: mode === m ? 700 : 400, color: mode === m ? 'var(--text-primary)' : 'var(--text-muted)', background: mode === m ? 'var(--bg-tertiary)' : 'transparent', border: `1px solid ${mode === m ? 'var(--accent-primary)' : 'var(--border-subtle)'}`, borderRadius: 4, cursor: 'pointer', marginRight: 6 }}>{label}</button>
  );

  // Unified headline format across all 3 modes: <record W-L> · <PnL¢> · <context>.
  const hl = (record: string, pnl: number, context: ReactNode) =>
    <><b>{record}</b> · {cVal(pnl)} · <span style={{ color: 'var(--text-muted)' }}>{context}</span></>;
  const nb = data.n_decision_bets ?? log.length;
  const headline = (): ReactNode => {
    if (mode === 'cashout') return hl(data.realized_record, data.realized_pnl_cents_total, <>{nb} {tr('prediction.lblBets')} · {data.n_smart_sold ?? 0} {tr('prediction.lblCashedOut')}</>);
    if (mode === 'hold') return hl(data.hold_record || data.pnl_record, data.hold_pnl_cents_total, <>{nb} {tr('prediction.lblBets')} · {data.n_skipped ?? 0} {tr('prediction.lblSkipped')}</>);
    return hl(data.argmax_record, data.argmax_pnl_cents_total, <>{log.length} {tr('prediction.lblMatchesAll')} · {tr('prediction.lblModelAcc')} {pct(data.model_pred_accuracy, 0)}</>);
  };
  const note = (): string => mode === 'cashout'
    ? tr('prediction.noteCashout', { entry: cc(data.avg_entry_cents), clv: ((data.avg_clv_cents ?? 0) >= 0 ? '+' : '') + cc(data.avg_clv_cents) })
    : mode === 'hold' ? tr('prediction.noteHold') : tr('prediction.noteArgmax');

  const cols = mode === 'argmax'
    ? [tr('prediction.colDate'), tr('prediction.colMatchup'), tr('prediction.colOurPick'), tr('prediction.colResult'), tr('prediction.colEntryC'), tr('prediction.colRealizedC'), 'Cum¢']
    : mode === 'cashout'
      ? [tr('prediction.colDate'), tr('prediction.colMatchup'), tr('prediction.colOurPick'), tr('prediction.colStake'), tr('prediction.colExit'), tr('prediction.colEntryC'), tr('prediction.colRealizedC'), 'Cum¢']
      : [tr('prediction.colDate'), tr('prediction.colMatchup'), tr('prediction.colOurPick'), tr('prediction.colStake'), tr('prediction.colResult'), tr('prediction.colEntryC'), tr('prediction.colPnlC'), 'Cum¢'];

  const muted = (x: ReactNode) => <span style={{ color: 'var(--text-muted)' }}>{x}</span>;
  const rows = log.map((b: any) => {
    if (mode === 'argmax')
      return [b.date?.slice(5), matchup(b), argmaxLabel(b), wl(b.model_won), cc(b.argmax_entry_cents), cVal(b.argmax_pnl_cents), cVal(b.argmax_cum_pnl_cents)];
    if (b.bet === false) {
      const base = [b.date?.slice(5), matchup(b), muted(tr('prediction.noBetShort')), muted('$0')];
      return mode === 'cashout' ? [...base, muted('—'), '—', '—', '—'] : [...base, muted('—'), '—', '—', '—'];
    }
    if (mode === 'cashout') {
      const se = b.smart_exit;
      const exit = se
        ? <span style={{ color: se.pnl_c >= 0 ? 'var(--success)' : 'var(--error)' }}>{tr('prediction.smartExitSold', { min: se.sold_min, c: Math.round(se.sold_c) })}</span>
        : <span style={{ color: b.won ? 'var(--success)' : 'var(--error)' }}>{tr(b.won ? 'prediction.exitSettleWon' : 'prediction.exitSettleLost')}</span>;
      return [b.date?.slice(5), matchup(b), sideLabel(b), `$${num(b.stake_usd, 2)}`, exit, cc(b.entry_cents), cVal(b.realized_pnl_cents), cVal(b.realized_cum_pnl_cents)];
    }
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
      <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 10, ...mono }}>{overviewHeadline(data?.performance, data?.headline)}</div>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.1em', margin: '8px 0 4px', ...mono }}>{tr('prediction.secInterfaces')}</div>
      <DataTable cols={[tr('prediction.colCat'), tr('prediction.colCommand'), tr('prediction.colPurpose')]} rows={(data?.interfaces ?? []).map((i: any) => [<span style={{ whiteSpace: 'nowrap' }}>{tDyn(i.category)}</span>, i.command?.replace('python -m prediction_market.', ''), tDyn(i.purpose)])} />
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
          // Read-only data source (never trades): geoblocked in the US, no account.
          ['Polymarket Global', tr('prediction.roleReference'), money(0), tr('prediction.tradingReadonly')],
        ]} />
      <div style={{ fontSize: 11, color: 'var(--text-muted)', ...mono }}>{tr('prediction.lblExecutable')}: {(g.executable_venues ?? []).join(', ')}</div>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginTop: 6 }}>{tr('prediction.venueGlobalNote')}</div>
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
          t.fifa_rank != null ? `#${t.fifa_rank}` : '—', tCountry(t.name), (t.form_z >= 0 ? '+' : '') + t.form_z.toFixed(2),
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
                <td style={{ ...td, position: 'sticky', left: 0, background: 'var(--bg-primary)', textAlign: 'left', whiteSpace: 'nowrap', fontWeight: 600, color: 'var(--text-primary)' }}>{tCountry(t.name)}</td>
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
                {tCountry(m.home?.name)} vs {tCountry(m.away?.name)}
                {m.settled && <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}> · {m.score}</span>}
              </div>
              <div style={{ fontSize: 10.5, ...mono, marginBottom: 6, color: 'var(--text-secondary)', display: 'flex', flexDirection: 'column', gap: 1 }}>
                {b.bet === false ? (
                  <span style={{ color: 'var(--text-muted)' }}>{tr('prediction.ourBet')}: {tr('prediction.noBet')}</span>
                ) : (() => {
                  const s = m.smart_exit;
                  return (<>
                    {/* line 1: what we bet — side · type · stake */}
                    <div>{tr('prediction.ptBet')}: <b style={{ color: 'var(--text-primary)' }}>{tCountry(b.pick_team)}</b> <span style={{ color: 'var(--text-muted)' }}>· {tr('prediction.ptSingle')}</span>{b.stake_usd != null ? <> · ${num(b.stake_usd, 2)}</> : null}</div>
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
                {/* argmax reference line */}
                {b.argmax && (() => {
                  const a = b.argmax; const am = a.mtm;
                  const aColor = am ? (am.pnl_c > 0 ? 'var(--success)' : am.pnl_c < 0 ? 'var(--error)' : 'var(--text-muted)') : 'var(--text-muted)';
                  return (
                    <div style={{ color: 'var(--text-muted)' }}>{tr('prediction.argmaxShort')}: <b>{tCountry(a.pick_team)}</b>{am && <> · {cc(am.entry_c)} → {cc(am.ft_c)} · <span style={{ color: aColor }}>{am.pnl_c >= 0 ? '+' : ''}{cc(am.pnl_c)}</span></>}</div>
                  );
                })()}
              </div>
              <DataTable
                cols={[tr('prediction.colMilestone'), tr('prediction.colScore'), `${sideName.home}¢`, `${sideName.draw}¢`, `${sideName.away}¢`]}
                rows={(m.marks ?? []).map((mk: any) => {
                  const hl = (side: string) => ({ fontWeight: b.side === side ? 700 : 400, color: b.side === side ? 'var(--text-primary)' : 'var(--ink)' });
                  return [
                    <b>{mk.milestone}</b>, mk.score,
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
const REGISTRY: Record<string, () => ReactElement> = {
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
  wc_overview: OverviewCard,
  wc_venues: Venues,
  wc_budget: Budget,
  wc_pdfs: Pdfs,
};

const KEY_BY_TYPE: Record<string, string> = Object.fromEntries(PREDICTION_ITEMS.map(i => [i.type, i.i18nKey]));
// Artifact types that carry the Regulation/Advances selector — rendered on the artifact's
// title row (top, right-aligned), matching the stock-mode viewers' header-row selector.
const ADVANCE_SELECTOR_TYPES = new Set(['wc_match_pricing', 'wc_divergence', 'wc_inplay', 'wc_predictions']);

export default function PredictionArtifact({ type }: { type: string }) {
  const { t } = useTranslation();
  const View = REGISTRY[type];
  if (!View) return <div className="text-xs py-3" style={{ color: 'var(--text-muted)', ...mono }}>Unknown artifact: {type}</div>;
  const key = KEY_BY_TYPE[type];
  return (
    <div>
      {key && (
        <div className="flex items-center justify-between" style={{ marginBottom: 6, minHeight: 22 }}>
          <div style={{ fontSize: 13, fontWeight: 700, letterSpacing: '.1em', textTransform: 'uppercase', color: 'var(--text-primary)', ...mono }}>{t(`prediction.${key}`)}</div>
          {ADVANCE_SELECTOR_TYPES.has(type) && <AdvanceModeToggle />}
        </div>
      )}
      <View />
    </div>
  );
}

export const isPredictionArtifact = (type?: string) => !!type && type.startsWith('wc_');
