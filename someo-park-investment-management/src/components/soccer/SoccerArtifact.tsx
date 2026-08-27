/**
 * SoccerArtifact — renders the right-panel content for every Club Soccer `soccer_*`
 * artifact. One REGISTRY dispatcher + a set of real data viewers (mirrors
 * prediction/PredictionArtifact.tsx's structure), each fetching the static JSON
 * synced from prediction_market_soccer/data/output/ via lib/soccerApi.
 *
 * Structure rules (§3.7 / §3.0 of the transform plan):
 *  - "league → match" hierarchy: every view carries a LeagueChips selector built
 *    from the DATA's `leagues` array (choice persisted in localStorage).
 *  - capability-driven visibility: advance lens / agg badges / board types render
 *    from backend-computed `caps` + `kind` fields ONLY — the frontend never tests
 *    league ids ("if (league === 'ucl')" is banned).
 *  - live files (upcoming/inplay/schedule) may not exist yet → clean empty states,
 *    never a hard error.
 */
import type { CSSProperties, ReactNode, ReactElement } from 'react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import { usePoll } from '../prediction/usePoll';
import { AdvanceModeToggle, useAdvanceMode } from '../prediction/AdvanceMode';
import {
  getSoccerModel, getSoccerSeasonOdds, getSoccerUpcoming, getSoccerInplay, getSoccerSchedule,
  getSoccerSquad, getSoccerStyles, getSoccerForm, getSoccerXvMatches, getSoccerXvChampion,
  getSoccerMilestones, getSoccerPerformance, getSoccerOos, getSoccerBacktest, getSoccerParams,
  getSoccerRisk, getSoccerOverview, soccerFileUrl,
  type SoccerModelLeague, type SoccerUpcomingMatch,
} from '../../lib/soccerApi';
import SoccerMatchCard, { clubName } from './SoccerMatchCard';
import SoccerBracket from './SoccerBracket';
import {
  leagueLabel, stageLabel, statusLabel, sideAbbr,
  fmtDate, fmtDateTime, fmtTime, fmtInt, fmtMoney,
  useLocalizedNote, useLocalizedNotes, type ChipLeague,
} from './soccerLabels';
import { SOCCER_ITEMS } from './SoccerArtifactGrid';

// The label helpers used to live here; they moved to ./soccerLabels so the match card
// can share them without a circular import. Re-exported for existing deep imports.
export { leagueLabel, useLocalizedNote };

// ── shared primitives (same conventions as PredictionArtifact) ───────────────
const pct = (v?: number | null, d = 1) => (v == null || isNaN(v) ? '—' : `${(v * 100).toFixed(d)}%`);
const cc = (v?: number | null, d = 0) => (v == null || isNaN(v) ? '—' : `${v.toFixed(d)}¢`);
// pcent() converts a 0–1 probability → ¢ (per-contract), cc() formats an already-¢ value.
const pcent = (v?: number | null, d = 0) => (v == null || isNaN(v) ? '—' : `${(v * 100).toFixed(d)}¢`);
const num = (v?: number | null, d = 1) => (v == null || isNaN(v) ? '—' : v.toFixed(d));
const signed = (v?: number | null, d = 2) => (v == null || isNaN(v) ? '—' : (v >= 0 ? '+' : '') + v.toFixed(d));
const mono: CSSProperties = { fontFamily: 'var(--font-mono)' };

const SYNC_CMD = 'npm run sync:soccer';

function Loading() {
  const { t } = useTranslation();
  return <div className="text-xs py-3" style={{ color: 'var(--text-muted)', ...mono }}>{t('common.loading')}</div>;
}
function ErrorBox({ e }: { e: string }) {
  const { t } = useTranslation();
  // The command stays literal, but the sentence around it is a template — word order
  // differs enough in ja/fr that "<msg>. <cmd>" reads as a fragment there.
  return (
    <div className="text-xs py-3" style={{ color: 'var(--error)', ...mono }}>
      <div>{t('soccer.loadFailed')}: {e}</div>
      <div>{t('soccer.loadFailedHint', { cmd: SYNC_CMD })}</div>
    </div>
  );
}
/** Clean empty state for live files the backend hasn't produced yet (upcoming /
 * inplay / schedule) — an absent file is expected, not an error. */
function EmptyBox({ title, hint }: { title: string; hint?: string }) {
  return (
    <div style={{ padding: '18px 12px', border: '1px dashed var(--border-subtle)', textAlign: 'center', ...mono }}>
      <div style={{ fontSize: 12, color: 'var(--text-secondary)', fontWeight: 700 }}>{title}</div>
      {hint && <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 4 }}>{hint}</div>}
    </div>
  );
}
// Heading is rendered (translated) by the dispatcher; Title keeps the sub line +
// an optional right-aligned control (e.g. the Regulation/Advances selector).
function Title({ sub, right }: { sub?: string; right?: ReactNode }) {
  if (!sub && !right) return null;
  return (
    <div className="mb-3 flex items-center justify-between" style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, minHeight: 22 }}>
      <span>{sub}</span>
      {right}
    </div>
  );
}
// Optional column sorting (same contract as PredictionArtifact's DataTable): pass
// `sortableCols` (clickable column indices) and `sortVals` (raw comparable value per
// row per col — numbers numeric, strings lexical, nulls last). One click ascending,
// click again descending. `defaultSort` sets the initial order without a click.
function DataTable({ cols, rows, sortableCols, sortVals, defaultSort }: {
  cols: ReactNode[]; rows: ReactNode[][];
  sortableCols?: number[];
  sortVals?: (number | string | null | undefined)[][];
  defaultSort?: { col: number; dir: 'asc' | 'desc' };
}) {
  const [sort, setSort] = useState<{ col: number; dir: 'asc' | 'desc' } | null>(defaultSort ?? null);
  const canSort = new Set(sortableCols ?? []);
  const order = rows.map((_, i) => i);
  if (sort && canSort.has(sort.col)) {
    const acc = (i: number) => (sortVals ? sortVals[i]?.[sort.col] : (rows[i]?.[sort.col] as any));
    // Nulls always sink to the bottom — in BOTH directions. Sorting ascending and
    // reversing sent every missing value to the top of a descending table (the squad
    // card opened on 100+ rows of "—" instead of the strongest clubs), so the
    // direction is applied inside the comparator and the null rule sits outside it.
    const dir = sort.dir === 'desc' ? -1 : 1;
    order.sort((a, b) => {
      const va = acc(a), vb = acc(b);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
      return String(va).localeCompare(String(vb)) * dir;
    });
  }
  const click = (j: number) => {
    if (!canSort.has(j)) return;
    setSort((s) => (s && s.col === j ? { col: j, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { col: j, dir: 'asc' }));
  };
  const arrow = (j: number) => (sort?.col === j ? (sort.dir === 'asc' ? ' ▲' : ' ▼') : '');
  return (
    <table className="table">
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

// ── league chips (shared across views; §3.7 hierarchy) ───────────────────────
const LEAGUE_LS_KEY = 'soccer-league';
function loadLeaguePref(): string {
  try { return localStorage.getItem(LEAGUE_LS_KEY) || ''; } catch { return ''; }
}
function saveLeaguePref(v: string) {
  try { localStorage.setItem(LEAGUE_LS_KEY, v); } catch { /* private mode — ignore */ }
}
/** Persisted league selection, validated against the leagues present in the data
 * (falls back to 'all' when allowed, else the first league). */
function useLeagueChoice(available: string[], allowAll = false): [string, (v: string) => void] {
  const [sel, setSel] = useState<string>(() => loadLeaguePref() || (allowAll ? 'all' : ''));
  const active = (allowAll && sel === 'all') || available.includes(sel)
    ? sel
    : (allowAll ? 'all' : (available[0] ?? ''));
  const choose = (v: string) => { setSel(v); saveLeaguePref(v); };
  return [active, choose];
}

function LeagueChips({ leagues, value, onChange, allowAll }: {
  leagues: ChipLeague[]; value: string; onChange: (v: string) => void; allowAll?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const chips: ChipLeague[] = allowAll
    ? [{ league: 'all', name: t('soccer.allLeagues'), zh: t('soccer.allLeagues') }, ...leagues]
    : leagues;
  if (!chips.length) return null;
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 10 }}>
      {chips.map((l) => {
        const on = value === l.league;
        return (
          <button key={l.league} onClick={() => onChange(l.league)}
            style={{
              padding: '2px 9px', fontSize: 10, ...mono, fontWeight: 700, letterSpacing: '.04em',
              border: '1px solid var(--text-primary)', cursor: 'pointer', whiteSpace: 'nowrap',
              background: on ? 'var(--text-primary)' : 'transparent',
              color: on ? 'var(--bg-primary)' : 'var(--text-muted)',
              transition: 'all .1s',
            }}>
            {leagueLabel(l, lang, t)}
          </button>
        );
      })}
    </div>
  );
}

// Signed-edge cell: green when positive, red when negative (ReachRound-style).
function edgeCell(edge?: number | null): ReactNode {
  if (edge == null || isNaN(edge)) return <span style={{ color: 'var(--text-muted)' }}>—</span>;
  const color = edge > 0 ? 'var(--success)' : edge < 0 ? 'var(--error)' : 'var(--text-muted)';
  return <span style={{ color, fontWeight: 700 }}>{edge >= 0 ? '+' : ''}{edge.toFixed(1)}¢</span>;
}

// League header shown when a view mixes leagues ('all' chip / grouped lists).
// One label, in the reader's language — the old version appended the backend's Chinese
// name for everyone who was NOT reading Chinese, which is the leak in reverse.
function LeagueHeader({ zh, id, lang }: { zh?: string; id?: string; lang: string }) {
  const { t } = useTranslation();
  return (
    <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.1em', textTransform: 'uppercase', color: 'var(--text-muted)', ...mono, margin: '10px 0 6px' }}>
      {leagueLabel({ league: id ?? '', zh }, lang, t)}
    </div>
  );
}

// Distinct leagues present in an upcoming/schedule match list, in first-seen order.
function leaguesOf(ms: { league?: string; league_zh?: string }[]): ChipLeague[] {
  const seen = new Map<string, ChipLeague>();
  for (const m of ms) {
    if (m.league && !seen.has(m.league)) seen.set(m.league, { league: m.league, zh: m.league_zh });
  }
  return [...seen.values()];
}

// ── viewers ──────────────────────────────────────────────────────────────────

/** (a) Season odds — per-league boards (champion / top-N / relegation / qual…)
 * straight from season_odds.json. Which boards a league gets is decided by the
 * BACKEND (per kind); the frontend renders whatever `boards` it is handed. */
function SeasonOdds() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const noteOf = useLocalizedNote();
  const { data, loading, error } = useApi<any>(() => getSoccerSeasonOdds(), []);
  const leagues = (data?.leagues ?? []) as any[];
  const [sel, choose] = useLeagueChoice(leagues.map((l) => l.league));
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const lg = leagues.find((l) => l.league === sel) ?? leagues[0];
  const asOf = fmtDateTime(data?.as_of, lang);
  return (
    <div>
      <Title sub={`${t('soccer.subSeasonOdds')}${asOf ? ` · ${t('soccer.asOf')} ${asOf}` : ''}`} />
      <LeagueChips leagues={leagues} value={lg?.league ?? ''} onChange={choose} />
      {!lg && <EmptyBox title={t('soccer.empty')} />}
      {lg && !(lg.boards ?? []).length && (
        <EmptyBox title={t(`soccer.oddsState.${lg.state ?? 'ok'}`, { defaultValue: t('soccer.empty') })}
          hint={t('soccer.oddsStateHint', { defaultValue: '' })} />
      )}
      {(lg?.boards ?? []).map((b: any, bi: number) => {
        const rows = [...(b.rows ?? [])].sort((a: any, x: any) => (x.model_pct ?? -1) - (a.model_pct ?? -1));
        return (
          <div key={bi} style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-primary)', ...mono, marginBottom: 4 }}>
              {/* b.label is the exporter's Chinese board name — the English slug is the
                  safe default for a family that has no key yet. */}
              {t(`soccer.family.${b.family}`, { defaultValue: b.family })}
              {b.kalshi_series && (
                <span style={{ fontWeight: 400, color: 'var(--text-muted)', marginLeft: 8 }}>
                  {t('soccer.colSeries')} {b.kalshi_series}
                </span>
              )}
            </div>
            <DataTable
              cols={[t('soccer.colClub'), t('soccer.colModelPct'), t('soccer.colModelC'), t('soccer.colKalshiC'), t('soccer.colPolyC'), t('soccer.colEdge')]}
              rows={rows.map((r: any) => [
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                  {r.logo && <img src={r.logo} alt="" width={14} height={14} style={{ display: 'inline-block' }} />}
                  {clubName(r, lang, t)}
                </span>,
                pct(r.model_pct), cc(r.model_c), cc(r.kalshi_c), cc(r.poly_c), edgeCell(r.edge_vs_kalshi),
              ])} />
          </div>
        );
      })}
      {noteOf(data?.note, data?.note_key) && <div style={{ fontSize: 9, color: 'var(--text-muted)', ...mono, marginTop: 6 }}>{noteOf(data?.note, data?.note_key)}</div>}
    </div>
  );
}

/** (b) League table — live standings (pos/club/played/pts/gd/gf) joined with the
 * model's e_points / e_rank. Zone colouring is data-driven (top_n / releg_* from
 * the league row), so swiss/cup kinds simply have no relegation zone. */
function LeagueTable() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const { data, loading, error } = useApi<any>(() => getSoccerModel(), []);
  const leagues = (data?.leagues ?? []) as SoccerModelLeague[];
  const [sel, choose] = useLeagueChoice(leagues.map((l) => l.league));
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const lg = leagues.find((l) => l.league === sel) ?? leagues[0];
  const table = [...(lg?.table ?? [])].sort((a, b) => b.pts - a.pts || b.gd - a.gd || b.gf - a.gf);
  const oddsById: Record<string, any> = Object.fromEntries((lg?.season_odds ?? []).map((o) => [o.club_id, o]));
  const topN = lg?.top_n ?? 0, relegD = lg?.releg_direct ?? 0, relegP = lg?.releg_playoff ?? 0, n = table.length;
  const posCell = (i: number) => {
    const rank = i + 1;
    let color = 'var(--text-muted)';
    if (topN && rank <= topN) color = 'var(--success)';
    else if (relegD && rank > n - relegD) color = 'var(--error)';
    else if (relegP && rank > n - relegD - relegP) color = 'var(--warning, #d08b00)';
    return <span style={{ color, fontWeight: 700 }}>{rank}</span>;
  };
  return (
    <div>
      <Title sub={`${t('soccer.subLeagueTable')}${lg ? ` · ${t(`soccer.kind.${lg.kind}`, { defaultValue: lg.kind })}` : ''}`} />
      <LeagueChips leagues={leagues} value={lg?.league ?? ''} onChange={choose} />
      {!table.length ? <EmptyBox title={t('soccer.empty')} /> : (
        <DataTable
          cols={[t('soccer.colPos'), t('soccer.colClub'), t('soccer.colPlayed'), t('soccer.colPts'), t('soccer.colGd'), t('soccer.colGf'), t('soccer.colEPoints'), t('soccer.colERank')]}
          rows={table.map((r, i) => {
            const o = oddsById[r.club_id] || {};
            return [
              posCell(i),
              <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{clubName(r, lang, t)}</span>,
              r.played, <b>{r.pts}</b>, r.gd > 0 ? `+${r.gd}` : r.gd, r.gf,
              num(o.e_points), num(o.e_rank),
            ];
          })} />
      )}
      {!!(topN || relegD || relegP) && (
        <div style={{ marginTop: 8, fontSize: 9, color: 'var(--text-muted)', ...mono }}>
          {topN ? <span style={{ color: 'var(--success)' }}>■ {t('soccer.zoneTop', { n: topN })}</span> : null}
          {relegP ? <span style={{ marginLeft: 10, color: 'var(--warning, #d08b00)' }}>■ {t('soccer.zoneRelegPlayoff')}</span> : null}
          {relegD ? <span style={{ marginLeft: 10, color: 'var(--error)' }}>■ {t('soccer.zoneReleg')}</span> : null}
        </div>
      )}
    </div>
  );
}

/** Shared body for the two upcoming-match views (pricing = chips + per-league list;
 * predictions = grouped by ET date). Both reuse SoccerMatchCard; the Regulation /
 * Advances selector renders ONLY when ≥1 match carries caps.advance (§3.0). */
function MatchPricing() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const { data, loading, error } = useApi<any>(() => getSoccerUpcoming(), []);
  const ms = (data?.matches ?? []) as SoccerUpcomingMatch[];
  const leagues = leaguesOf(ms);
  const [sel, choose] = useLeagueChoice(leagues.map((l) => l.league), true);
  if (loading) return <Loading />;
  if (error || !ms.length) return <EmptyBox title={t('soccer.noUpcoming')} hint={t('soccer.emptyHint')} />;
  const hasAdvance = ms.some((m) => m.caps?.advance);
  const shown = sel === 'all' ? ms : ms.filter((m) => m.league === sel);
  return (
    <div>
      <Title sub={`${t('soccer.subMatchPricing')} · ${shown.length} ${t('soccer.matches')}`}
        right={hasAdvance ? <AdvanceModeToggle /> : undefined} />
      <LeagueChips leagues={leagues} value={sel} onChange={choose} allowAll />
      {sel === 'all'
        ? leagues.map((l) => {
            const group = shown.filter((m) => m.league === l.league);
            if (!group.length) return null;
            return (
              <div key={l.league}>
                <LeagueHeader zh={l.zh} id={l.league} lang={lang} />
                <div className="flex flex-wrap gap-2">
                  {group.map((m, i) => <span key={`${m.fixture_id ?? i}`} style={{ display: 'contents' }}><SoccerMatchCard m={m} /></span>)}
                </div>
              </div>
            );
          })
        : (
          <div className="flex flex-wrap gap-2">
            {shown.map((m, i) => <span key={`${m.fixture_id ?? i}`} style={{ display: 'contents' }}><SoccerMatchCard m={m} /></span>)}
          </div>
        )}
    </div>
  );
}

/** (d) Today's predictions — every priced upcoming match, grouped by ET date
 * (soonest first), cards decision-forward. */
function Predictions() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const { data, loading, error } = useApi<any>(() => getSoccerUpcoming(), []);
  if (loading) return <Loading />;
  const ms = (data?.matches ?? []) as SoccerUpcomingMatch[];
  if (error || !ms.length) return <EmptyBox title={t('soccer.noUpcoming')} hint={t('soccer.emptyHint')} />;
  const hasAdvance = ms.some((m) => m.caps?.advance);
  const sorted = [...ms].sort((a, b) => String(a.kickoff).localeCompare(String(b.kickoff)));
  const byDate = new Map<string, SoccerUpcomingMatch[]>();
  for (const m of sorted) {
    const d = m.et_date || (m.kickoff || '').slice(0, 10);
    if (!byDate.has(d)) byDate.set(d, []);
    byDate.get(d)!.push(m);
  }
  const nBets = ms.filter((m) => m.decision?.bet).length;
  return (
    <div>
      <Title sub={`${t('soccer.subPredictions')} · ${ms.length} ${t('soccer.matches')} · ${nBets} ${t('soccer.bets')}`}
        right={hasAdvance ? <AdvanceModeToggle /> : undefined} />
      {[...byDate.entries()].map(([d, group]) => (
        <div key={d}>
          <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.1em', color: 'var(--text-muted)', ...mono, margin: '10px 0 6px' }}>{fmtDate(d, lang) || d}</div>
          <div className="flex flex-wrap gap-2">
            {group.map((m, i) => <span key={`${m.fixture_id ?? i}`} style={{ display: 'contents' }}><SoccerMatchCard m={m} showLeague /></span>)}
          </div>
        </div>
      ))}
    </div>
  );
}

/** (e) Schedule — full fixture list by league chips (falls back to upcoming.json
 * while schedule.json isn't produced yet; both absent → clean empty state). */
function ScheduleView() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const sched = useApi<any>(() => getSoccerSchedule(), []);
  const up = useApi<any>(() => getSoccerUpcoming(), []);
  const ms: any[] = (sched.data?.matches?.length ? sched.data.matches : up.data?.matches) ?? [];
  const leagues = leaguesOf(ms);
  const [sel, choose] = useLeagueChoice(leagues.map((l) => l.league), true);
  if (sched.loading && up.loading) return <Loading />;
  if (!ms.length) return <EmptyBox title={t('soccer.empty')} hint={t('soccer.emptyHint')} />;
  const shown = sel === 'all' ? ms : ms.filter((m) => m.league === sel);
  const played = shown.filter((m) => m.finished).length;
  return (
    <div>
      <Title sub={`${t('soccer.subSchedule')} · ${shown.length} ${t('soccer.matches')}${played ? ` (${played} ${t('soccer.finished')})` : ''}`} />
      <LeagueChips leagues={leagues} value={sel} onChange={choose} allowAll />
      <DataTable
        cols={[t('soccer.colKickoff'), t('soccer.colLeague'), t('soccer.colRound'), t('soccer.colMatch'), t('soccer.colResult')]}
        rows={shown.map((m: any) => [
          m.et ?? m.kickoff,
          leagueLabel({ league: m.league ?? '', zh: m.league_zh }, lang, t),
          stageLabel(m.round, t),
          <span>{clubName(m.home, lang, t)} <span style={{ color: 'var(--text-muted)' }}>{t('soccer.versus')}</span> {clubName(m.away, lang, t)}</span>,
          m.finished
            ? <span style={{ fontWeight: 700 }}>{m.score}</span>
            : <span style={{ color: 'var(--text-muted)' }}>{!m.status || m.status === 'NS' ? '—' : statusLabel(m.status, t)}</span>,
        ])} />
    </div>
  );
}

/** (f) In-play — empty-state-first live view (inplay_live.json may not exist for
 * long stretches). Polls every 30s; live cards mirror the WC in-play summary
 * (score/minute/model/quotes) with the league tag added, advance line caps-gated. */
function InPlay() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const { mode } = useAdvanceMode();
  const { data, loading, error } = usePoll<any>(() => getSoccerInplay(), 30000);
  if (loading && !data) return <Loading />;
  const ms: any[] = data?.matches ?? [];
  const hasAdvance = ms.some((m) => m.caps?.advance);
  if (error || !ms.length) {
    return (
      <div>
        <Title sub={t('soccer.subInplay')} />
        <EmptyBox title={t('soccer.noLiveMatches')} hint={t('soccer.emptyHint')} />
      </div>
    );
  }
  return (
    <div>
      <Title sub={`${t('soccer.subInplay')} · ${ms.length} ${t('soccer.live')}`}
        right={hasAdvance ? <AdvanceModeToggle /> : undefined} />
      {ms.map((m: any, i: number) => {
        const adv = mode === 'advance' && m.caps?.advance && m.advance?.model ? m.advance : null;
        const q = m.prices?.kalshi || m.prices?.poly_us;
        // Venue names match the match card's venueLabelMap — 'Poly' was an internal short form.
        const qsrc = m.prices?.kalshi ? 'Kalshi' : m.prices?.poly_us ? 'Polymarket US' : null;
        return (
          <div key={m.fixture_id ?? i} className="card" style={{ marginBottom: 10, borderLeft: '4px solid var(--error)' }}>
            <div className="flex items-center justify-between">
              <span style={{ fontSize: 12, fontWeight: 700, ...mono, color: 'var(--text-primary)' }}>
                <span className="pulse" style={{ color: 'var(--error)', marginRight: 6 }}>● {t('soccer.liveBadge')}</span>
                {clubName(m.home, lang, t)} <b>{m.score ?? ''}</b> {clubName(m.away, lang, t)}
              </span>
              <span style={{ fontSize: 10, color: 'var(--text-muted)', ...mono }}>{m.minute != null ? t('soccer.minuteSuffix', { n: m.minute }) : statusLabel(m.status, t)}</span>
            </div>
            <div style={{ fontSize: 9, color: 'var(--text-muted)', ...mono, marginTop: 2 }}>
              {leagueLabel({ league: m.league ?? '', zh: m.league_zh }, lang, t)}{m.round ? ` · ${stageLabel(m.round, t)}` : ''}
            </div>
            <div style={{ fontSize: 11, ...mono, color: 'var(--text-secondary)', marginTop: 4 }}>
              {t('soccer.model')}: {t('soccer.abbrHome')} {pct(m.model?.home, 0)} · {t('soccer.abbrDraw')} {pct(m.model?.draw, 0)} · {t('soccer.abbrAway')} {pct(m.model?.away, 0)}
            </div>
            {adv && (
              <div style={{ fontSize: 10, ...mono, color: 'var(--accent-primary)', marginTop: 2 }}>
                {t('soccer.modeAdvance')}: {t('soccer.abbrHome')} {pct(adv.model?.home, 0)} · {t('soccer.abbrAway')} {pct(adv.model?.away, 0)}
              </div>
            )}
            {q && qsrc && (
              <div style={{ fontSize: 10, ...mono, color: 'var(--text-muted)', marginTop: 2 }}>
                {qsrc}: {t('soccer.abbrHome')} {cc(q.home?.mid_c)} · {t('soccer.abbrDraw')} {cc(q.draw?.mid_c)} · {t('soccer.abbrAway')} {cc(q.away?.mid_c)}
              </div>
            )}
            {!!m.opportunities?.length && (
              <div style={{ fontSize: 10, ...mono, color: 'var(--success)', fontWeight: 700, marginTop: 3 }}>
                {m.opportunities.length} {t('soccer.signals')}
              </div>
            )}
          </div>
        );
      })}
      {data?.ts && <div style={{ fontSize: 9, color: 'var(--text-muted)', ...mono, marginTop: 4 }}>{t('soccer.asOf')} {fmtTime(data.ts, lang)}</div>}
    </div>
  );
}

/** (g) Model notes — meta + model_notes prose + per-league coverage summary. */
function ModelNotes() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const notesOf = useLocalizedNotes();
  const { data, loading, error } = useApi<any>(() => getSoccerModel(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const meta = data?.meta ?? {};
  const leagues = (data?.leagues ?? []) as SoccerModelLeague[];
  const modelNotes = notesOf(meta.model_notes, meta.model_notes_i18n);
  return (
    <div>
      <Title sub={t('soccer.subModelNotes')} />
      <KV rows={[
        [t('soccer.runTs'), fmtDateTime(meta.run_ts, lang) || '—'],
        [t('soccer.codeVersion'), <span style={mono}>{meta.code_version ?? '—'}</span>],
        [t('soccer.sims'), fmtInt(meta.n_sims, lang)],
      ]} />
      {!!modelNotes.length && (
        <ul style={{ margin: '0 0 12px', paddingLeft: 16, fontSize: 11, color: 'var(--text-muted)', ...mono }}>
          {modelNotes.map((n: string, i: number) => <li key={i} style={{ marginBottom: 4 }}>{n}</li>)}
        </ul>
      )}
      <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-primary)', ...mono, marginBottom: 4 }}>{t('soccer.coverage')}</div>
      <DataTable
        cols={[t('soccer.colLeague'), t('soccer.colKind'), t('soccer.colTeams'), t('soccer.colRemaining')]}
        rows={leagues.map((l) => [
          <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{leagueLabel(l, lang, t)}</span>,
          t(`soccer.kind.${l.kind}`, { defaultValue: l.kind }),
          l.n_teams, l.n_remaining,
        ])} />
    </div>
  );
}

/** Best-effort display name for fields that may be a plain string (WC-style) or a
 * {name, zh} ref (club-style) — the xv/backtest exporters are in flux. */
function anyName(x: any, lang: string, t?: (k: string, o?: any) => string): string {
  if (x == null) return '—';
  if (typeof x === 'string') return x;
  return clubName(x, lang, t);
}

/** 射手王 — per-league top-scorer boards from soccer_model.json leagues[].top_scorer
 * (Phase 2b backend; full-card empty state until it lands). Mirrors wc_golden_boot. */
function TopScorer() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const { data, loading, error } = useApi<any>(() => getSoccerModel(), []);
  const withTs = ((data?.leagues ?? []) as any[]).filter((l) => l.top_scorer?.length);
  const [sel, choose] = useLeagueChoice(withTs.map((l) => l.league));
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  if (!withTs.length) {
    return (<div><Title sub={t('soccer.subTopScorer')} /><EmptyBox title={t('soccer.emptyTopScorer')} hint={t('soccer.emptyHint')} /></div>);
  }
  const lg = withTs.find((l) => l.league === sel) ?? withTs[0];
  const rows = [...(lg.top_scorer ?? [])]
    .sort((a: any, b: any) => ((b.p_top_scorer ?? b.p_golden_boot ?? 0) - (a.p_top_scorer ?? a.p_golden_boot ?? 0))
      || ((b.goals ?? 0) - (a.goals ?? 0)) || ((b.e_goals ?? 0) - (a.e_goals ?? 0)))
    .slice(0, 20);
  return (
    <div>
      <Title sub={t('soccer.subTopScorer')} />
      <LeagueChips leagues={withTs} value={lg?.league ?? ''} onChange={choose} />
      <DataTable cols={[t('soccer.colPlayer'), t('soccer.colClub'), t('soccer.colGoals'), t('soccer.colPTop'), t('soccer.colEGoals')]}
        rows={rows.map((p: any) => [
          p.name ?? p.player ?? '—',
          anyName(p.team ?? p.club, lang, t),
          p.goals ?? 0, pct(p.p_top_scorer ?? p.p_golden_boot), num(p.e_goals, 2),
        ])} />
    </div>
  );
}

/** 阵容强度 — mirror of wc_squad. Club rows use the same field names; `fifa_rank`
 * now carries the club Elo/global rank, so the column is labelled "Rank". */

/** Rank shown in the club tables. With a single competition selected the reader
 *  wants that club's place IN that competition (Bayern is Bundesliga #2, not
 *  global #194); the cross-competition Elo rank only means something in the
 *  "all" view, where the rows genuinely span every competition. */
function rankCell(x: any, sel: string): string {
  if (sel && sel !== 'all' && x?.league_rank != null) {
    return `#${x.league_rank}${x.league_n ? `/${x.league_n}` : ''}`;
  }
  return x?.fifa_rank != null ? `#${x.fifa_rank}` : '—';
}

function SquadStrength() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const { data, loading, error } = useApi<any>(() => getSoccerSquad(), []);
  const teams = (data?.teams ?? []) as any[];
  const leagues = leaguesOf(teams);
  const [sel, choose] = useLeagueChoice(leagues.map((l) => l.league), true);
  if (loading) return <Loading />;
  if (error || !teams.length) {
    return (<div><Title sub={t('soccer.subSquad')} /><EmptyBox title={t('soccer.empty')} hint={t('soccer.emptyHint')} /></div>);
  }
  const shown = (sel === 'all' || !leagues.length) ? teams : teams.filter((x) => x.league === sel);
  // A player line is either season data (goals) or an FC26 talent row (overall) —
  // never print "(nullg)" for the talent rows that have no season stats yet.
  const playerLabel = (p: any) => (p?.goals != null ? `${p.name} (${t('soccer.goalsSuffix', { n: p.goals })})`
    : p?.ovr != null ? `${p.name} (${p.ovr})` : `${p?.name ?? ''}`);
  const playersFull = (ps: any[]) => (ps ?? []).map(playerLabel).join(', ');
  return (
    <div>
      <Title sub={t('soccer.subSquad')} />
      {leagues.length > 0 && <LeagueChips leagues={leagues} value={sel} onChange={choose} allowAll />}
      <DataTable cols={[t('soccer.colRank'), t('soccer.colClub'), t('soccer.colSquadScore'), t('soccer.colRating'), t('soccer.colGaPer90'),
        <span title={t('soccer.colFc26Hint')}>{t('soccer.colFc26')}</span>, t('soccer.colTopPlayers')]}
        rows={shown.map((x: any) => [
          rankCell(x, sel),
          <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{clubName(x, lang, t)}</span>,
          signed(x.score_z), num(x.mw_rating, 2), num(x.ga_per90, 2),
          num(x.talent_ovr, 1),
          // top 3 inline (WC layout); full list in the hover title.
          <span title={playersFull(x.top_players)}>
            {(x.top_players ?? []).slice(0, 3).map(playerLabel).join(', ') || '—'}
          </span>,
        ])}
        sortableCols={[0, 2, 3, 4, 5]}
        sortVals={shown.map((x: any) => [x.fifa_rank, null, x.score_z, x.mw_rating, x.ga_per90, x.talent_ovr, null])}
        defaultSort={{ col: 2, dir: 'desc' }} />
    </div>
  );
}

/** 球风矩阵 — mirror of wc_styles: clubs × 10 style columns, filled cell = the
 * club's possession (ranked within a style). Rows carry `league` → LeagueChips
 * filter. Style names reuse the WC prediction.style.* translations (club-agnostic). */
function TeamStyles() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const { data, loading, error } = useApi<any>(() => getSoccerStyles(), []);
  const [sortCode, setSortCode] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<'desc' | 'asc'>('desc');
  const teamsAll = (data?.teams ?? []) as any[];
  const leagues = leaguesOf(teamsAll);
  const [sel, choose] = useLeagueChoice(leagues.map((l) => l.league), true);
  if (loading) return <Loading />;
  const styles = (data?.styles ?? []) as any[];
  if (error || !teamsAll.length || !styles.length) {
    return (<div><Title sub={t('soccer.subStyles')} /><EmptyBox title={t('soccer.empty')} hint={t('soccer.emptyHint')} /></div>);
  }
  const teams = (sel === 'all' || !leagues.length) ? teamsAll : teamsAll.filter((x) => x.league === sel);
  const codeIdx: Record<string, number> = {};
  styles.forEach((s: any, i: number) => { codeIdx[s.code] = i; });
  const cellOf = (x: any, code: string) => (x.styles || []).find((s: any) => s.code === code);
  // team_styles.json labels are bilingual ("控球传导 Possession"); the Latin half is the
  // only safe default for a style that has no key yet — splitting off the Chinese half
  // handed 控球传导 to every non-Chinese reader.
  const latinPart = (label: string) => ((label || '').match(/[A-Za-z][A-Za-z\s'-]*$/)?.[0] || '').trim();
  const styleName = (s: any) => t('prediction.style.' + s.code, { defaultValue: latinPart(s.label) || s.code });
  const onSort = (code: string) => {
    if (sortCode === code) setSortDir((d) => (d === 'desc' ? 'asc' : 'desc'));
    else { setSortCode(code); setSortDir('desc'); }
  };
  const cellPoss = (x: any, code: string) => { const c = cellOf(x, code); return c ? c.poss : -1; };
  const rows = [...teams].sort((a: any, b: any) => {
    if (sortCode) {
      const va = cellPoss(a, sortCode), vb = cellPoss(b, sortCode);
      if (va !== vb) return sortDir === 'desc' ? vb - va : va - vb;
      return a.team_id < b.team_id ? -1 : 1;
    }
    const ca = codeIdx[a.styles?.[0]?.code] ?? 99, cb = codeIdx[b.styles?.[0]?.code] ?? 99;
    return ca - cb || ((b.poss ?? 0) - (a.poss ?? 0));
  });
  const th: CSSProperties = { padding: '4px 5px', borderBottom: '2px solid var(--border-subtle)', fontWeight: 700, color: 'var(--text-secondary)', whiteSpace: 'nowrap', textAlign: 'center' };
  const td: CSSProperties = { padding: '3px 5px', borderBottom: '1px solid var(--border-subtle)', textAlign: 'center' };
  return (
    <div>
      <Title sub={t('soccer.subStyles')} />
      {leagues.length > 0 && <LeagueChips leagues={leagues} value={sel} onChange={choose} allowAll />}
      <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginBottom: 8 }}>
        {t('soccer.stylesNote')}{data?.n != null ? ` · ${data.n} ${t('soccer.clubs')}` : ''}
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table style={{ borderCollapse: 'collapse', fontSize: 10, ...mono, width: '100%' }}>
          <thead>
            <tr>
              <th style={{ ...th, position: 'sticky', left: 0, background: 'var(--bg-secondary)', textAlign: 'left', zIndex: 1 }}>{t('soccer.colClub')}</th>
              {styles.map((s: any) => (
                <th key={s.code} onClick={() => onSort(s.code)} title={styleName(s)}
                    style={{ ...th, cursor: 'pointer', color: sortCode === s.code ? 'var(--accent-primary)' : 'var(--text-secondary)' }}>
                  {styleName(s)}{sortCode === s.code ? (sortDir === 'desc' ? ' ↓' : ' ↑') : ''}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((x: any) => (
              <tr key={x.team_id}>
                <td style={{ ...td, position: 'sticky', left: 0, background: 'var(--bg-primary)', textAlign: 'left', whiteSpace: 'nowrap', fontWeight: 600, color: 'var(--text-primary)' }}>{clubName(x, lang, t)}</td>
                {styles.map((s: any) => {
                  const c = cellOf(x, s.code);
                  return (
                    <td key={s.code}
                        style={{ ...td, background: c ? 'var(--bg-tertiary)' : 'transparent', color: c ? 'var(--text-primary)' : 'var(--text-muted)', fontWeight: c ? 700 : 400 }}
                        title={c ? `${clubName(x, lang, t)} · ${styleName(s)} · ${t('soccer.possession')} ${Math.round(c.poss * 100)}% · #${c.rank}` : ''}>
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

/** 近期状态 — mirror of wc_form (form_z / weighted_gd / recent list); the recent
 * letter badges are cup/Europe for clubs, explained by the legend line. */
function FormCard() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const { data, loading, error } = useApi<any>(() => getSoccerForm(), []);
  const teams = (data?.teams ?? []) as any[];
  const leagues = leaguesOf(teams);
  const [sel, choose] = useLeagueChoice(leagues.map((l) => l.league), true);
  if (loading) return <Loading />;
  if (error || !teams.length) {
    return (<div><Title sub={t('soccer.subForm')} /><EmptyBox title={t('soccer.empty')} hint={t('soccer.emptyHint')} /></div>);
  }
  const shown = (sel === 'all' || !leagues.length) ? teams : teams.filter((x) => x.league === sel);
  return (
    <div>
      <Title sub={t('soccer.subForm')} />
      {leagues.length > 0 && <LeagueChips leagues={leagues} value={sel} onChange={choose} allowAll />}
      <DataTable cols={[t('soccer.colRank'), t('soccer.colClub'), t('soccer.colForm'), t('soccer.colWgd'), t('soccer.colRecent')]}
        rows={shown.map((x: any) => [
          rankCell(x, sel),
          <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{clubName(x, lang, t)}</span>,
          signed(x.form_z), signed(x.weighted_gd),
          (x.recent ?? []).join(' '),
        ])}
        sortableCols={[0, 2, 3]}
        sortVals={shown.map((x: any) => [x.fifa_rank, null, x.form_z, x.weighted_gd, null])}
        defaultSort={{ col: 2, dir: 'desc' }} />
      <div style={{ marginTop: 8, fontSize: 10, color: 'var(--text-muted)', ...mono }}>{t('soccer.formLegend')}</div>
    </div>
  );
}

// Signed model−market divergence cell: green/red when |div| ≥ 5pp, muted below.
function divCell(side: string, val?: number | null): ReactNode {
  if (val == null || isNaN(val)) return <span style={{ color: 'var(--text-muted)' }}>—</span>;
  const color = Math.abs(val) >= 0.05 ? (val >= 0 ? 'var(--success)' : 'var(--error)') : 'var(--text-muted)';
  return <span style={{ color, fontWeight: Math.abs(val) >= 0.05 ? 700 : 400 }}>{side} {val >= 0 ? '+' : ''}{pct(val, 1)} ({val >= 0 ? '+' : ''}{pcent(val)})</span>;
}

/** 模型vs市场 — two tabs: match 3-ways (xv_matches.json, sorted by max_abs) and
 * champion boards (xv_champion.json, per-league chips). */
function Divergence() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const [tab, setTab] = useState<'matches' | 'champion'>('matches');
  const xm = useApi<any>(() => getSoccerXvMatches(), []);
  const xc = useApi<any>(() => getSoccerXvChampion(), []);
  const chLeagues = (xc.data?.leagues ?? []) as any[];
  const [sel, choose] = useLeagueChoice(chLeagues.map((l) => l.league));
  if (xm.loading && xc.loading) return <Loading />;
  const ink = 'var(--text-primary)';
  const opts: ['matches' | 'champion', string][] = [
    ['matches', t('soccer.tabMatches')], ['champion', t('soccer.tabChampion')],
  ];
  const toggle = (
    <div className="flex overflow-hidden" style={{ border: `2px solid ${ink}`, flexShrink: 0 }}>
      {opts.map(([val, label], i) => (
        <button key={val} onClick={() => setTab(val)}
          style={{ padding: '3px 12px', fontSize: '10px', fontFamily: 'var(--font-mono)', fontWeight: 700,
            letterSpacing: '.06em', textTransform: 'uppercase', transition: 'all .1s',
            background: tab === val ? ink : 'transparent',
            color: tab === val ? 'var(--bg-primary)' : 'var(--text-muted)',
            border: 'none', borderLeft: i > 0 ? `2px solid ${ink}` : 'none',
            cursor: 'pointer', whiteSpace: 'nowrap' }}>{label}</button>
      ))}
    </div>
  );
  let body: ReactNode;
  if (tab === 'matches') {
    const ms = [...(xm.data?.matches ?? [])].sort((a: any, b: any) => (b.max_abs ?? 0) - (a.max_abs ?? 0));
    body = (xm.error || !ms.length)
      ? <EmptyBox title={t('soccer.empty')} hint={t('soccer.emptyHint')} />
      : (
        <DataTable cols={[t('soccer.colMatch'),
          t('soccer.colHda', { label: t('soccer.model'), h: t('soccer.abbrHome'), d: t('soccer.abbrDraw'), a: t('soccer.abbrAway') }),
          t('soccer.colHda', { label: t('soccer.colRef'), h: t('soccer.abbrHome'), d: t('soccer.abbrDraw'), a: t('soccer.abbrAway') }),
          t('soccer.colDivergence')]}
          rows={ms.map((m: any) => {
            const ref = m.kalshi_devig ?? m.poly_devig ?? m.book_devig;
            const src = m.ref_source ?? (m.kalshi_devig ? 'kalshi' : m.poly_devig ? 'poly' : m.book_devig ? 'book' : '');
            const side = m.max_side as string | undefined;
            const val = side ? m.divergence?.[side] : null;
            return [
              <span><span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{anyName(m.home, lang, t)} {t('soccer.versus')} {anyName(m.away, lang, t)}</span>
                {m.league ? <span style={{ color: 'var(--text-muted)', fontSize: 9, marginLeft: 5 }}>{leagueLabel({ league: String(m.league) }, lang, t)}</span> : null}</span>,
              `${pcent(m.model?.home)}/${pcent(m.model?.draw)}/${pcent(m.model?.away)}`,
              ref ? <span>{`${pcent(ref.home)}/${pcent(ref.draw)}/${pcent(ref.away)}`}{src ? <span style={{ color: 'var(--text-muted)', fontSize: 9, marginLeft: 4 }}>{t(`soccer.refSource.${src}`, { defaultValue: src })}</span> : null}</span> : '—',
              divCell(side ? sideAbbr(side, t) : '', val ?? m.max_abs),
            ];
          })} />
      );
  } else {
    const lg = chLeagues.find((l) => l.league === sel) ?? chLeagues[0];
    body = (xc.error || !chLeagues.length)
      ? <EmptyBox title={t('soccer.empty')} hint={t('soccer.emptyHint')} />
      : (
        <div>
          <LeagueChips leagues={chLeagues} value={lg?.league ?? ''} onChange={choose} />
          {lg?.series && <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginBottom: 4 }}>{lg.series}</div>}
          <DataTable cols={[t('soccer.colClub'), t('soccer.colPModel'), t('soccer.colKalshiC'), t('soccer.colDevig'), t('soccer.colDivergence')]}
            rows={([...(lg?.rows ?? [])] as any[])
              .sort((a, b) => Math.abs(b.divergence ?? 0) - Math.abs(a.divergence ?? 0))
              .map((r: any) => [
                <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{clubName(r, lang, t)}</span>,
                pct(r.p_model), cc(r.kalshi_c), pct(r.p_kalshi_devig),
                divCell('', r.divergence),
              ])} />
        </div>
      );
  }
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 8 }}>
        <Title sub={t('soccer.subDivergence')} />
        {toggle}
      </div>
      {body}
    </div>
  );
}

/** 价格轨迹 — mirror of wc_pricetrack: per-contract ¢ at each milestone
 * (PRE→T15→T30→HT→T60→T75→FT). Empty until matches run under our live loop. */
function PriceTrack() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const { data, loading, error } = useApi<any>(() => getSoccerMilestones(), []);
  if (loading) return <Loading />;
  const matches = (data?.matches ?? []) as any[];
  if (error || !matches.length) {
    return (<div><Title sub={t('soccer.subPricetrack')} /><EmptyBox title={t('soccer.empty')} hint={t('soccer.emptyPricetrack')} /></div>);
  }
  return (
    <div>
      <Title sub={t('soccer.subPricetrack')} />
      {matches.map((m: any, mi: number) => {
        const b = m.our_bet || {}; const mtm = m.mtm; const s = m.smart_exit;
        return (
          <div key={m.fixture_id ?? mi} className="card" style={{ marginBottom: 12 }}>
            <div style={{ fontWeight: 700, fontSize: 12, ...mono, marginBottom: 4 }}>
              {anyName(m.home, lang, t)} {t('soccer.versus')} {anyName(m.away, lang, t)}
              {m.settled && <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}> · {m.score}</span>}
              {m.league ? <span style={{ color: 'var(--text-muted)', fontWeight: 400, fontSize: 10, marginLeft: 6 }}>{leagueLabel({ league: String(m.league) }, lang, t)}</span> : null}
            </div>
            <div style={{ fontSize: 10.5, ...mono, marginBottom: 6, color: 'var(--text-secondary)', display: 'flex', flexDirection: 'column', gap: 1 }}>
              {b.bet === false ? (
                <span style={{ color: 'var(--text-muted)' }}>{t('soccer.ptBet')}: {t('soccer.noBet')}</span>
              ) : (<>
                <div>{t('soccer.ptBet')}: <b style={{ color: 'var(--text-primary)' }}>{anyName(b.pick_team ?? b.pick, lang, t)}</b>{b.stake_usd != null ? <> · {fmtMoney(b.stake_usd, lang)}</> : null}</div>
                {s ? (
                  <div>　{t('soccer.ptBuy')} <b>{t('soccer.milestone.PRE')} {cc(b.entry_cents)}</b> → {t('soccer.ptSell')} <b>{s.sold_min}′ {Math.round(s.sold_c)}¢</b> · {t('soccer.lblRealized')} <b style={{ color: s.pnl_c >= 0 ? 'var(--success)' : 'var(--error)' }}>{s.pnl_c >= 0 ? '+' : ''}{cc(s.pnl_c)}</b></div>
                ) : mtm ? (
                  <div>　{t('soccer.ptBuy')} <b>{t('soccer.milestone.PRE')} {cc(b.entry_cents)}</b> → {t('soccer.lblSettle')} <b>{cc(mtm.ft_c)}</b> · <b style={{ color: mtm.pnl_c >= 0 ? 'var(--success)' : 'var(--error)' }}>{mtm.pnl_c >= 0 ? '+' : ''}{cc(mtm.pnl_c)} {mtm.won ? t('soccer.betWon') : t('soccer.betLost')}</b></div>
                ) : null}
                {s && mtm && (
                  <div style={{ color: 'var(--text-muted)' }}>　{t('soccer.ptIfHeld')}: {cc(mtm.ft_c)} · {mtm.pnl_c >= 0 ? '+' : ''}{cc(mtm.pnl_c)}</div>
                )}
              </>)}
            </div>
            <DataTable
              cols={[t('soccer.colMilestone'), t('soccer.colScore'),
                `${t('soccer.abbrHome')}¢`, `${t('soccer.abbrDraw')}¢`, `${t('soccer.abbrAway')}¢`]}
              rows={(m.marks ?? []).map((mk: any) => {
                const px = mk.poly_c ?? mk.kalshi_c ?? mk.model_c ?? {};
                const hl = (side: string) => ({ fontWeight: b.side === side ? 700 : 400, color: b.side === side ? 'var(--text-primary)' : undefined });
                return [
                  <b>{t(`soccer.milestone.${mk.milestone}`, { defaultValue: mk.milestone })}</b>, mk.score,
                  <span style={hl('home')}>{cc(px.home)}</span>,
                  <span style={hl('draw')}>{cc(px.draw)}</span>,
                  <span style={hl('away')}>{cc(px.away)}</span>,
                ];
              })} />
          </div>
        );
      })}
      <div style={{ marginTop: 8, fontSize: 10, color: 'var(--text-muted)', ...mono }}>{t('soccer.priceTrackNote')}</div>
    </div>
  );
}

// W/L + signed-¢ cells shared by the performance views.
const wlCell = (t: (k: string) => string, won?: boolean) =>
  <span style={{ color: won ? 'var(--success)' : 'var(--error)', fontWeight: 700 }}>{won ? t('soccer.betWon') : t('soccer.betLost')}</span>;
const cVal = (v: number | null | undefined): ReactNode =>
  v == null ? '—' : <span style={{ color: v >= 0 ? 'var(--success)' : 'var(--error)' }}>{v >= 0 ? '+' : ''}{cc(v)}</span>;

/** 准确度与盈亏 — mirror of wc_performance (frozen-ledger KV + bet log). Cold-start
 * empty state until the first settlements land. */
function PerformanceCard() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const notesOf = useLocalizedNotes();
  const { data, loading, error } = useApi<any>(() => getSoccerPerformance(), []);
  if (loading) return <Loading />;
  const settled = data?.n_settled ?? 0;
  const log = (data?.bet_log ?? []) as any[];
  if (error || !data || (!settled && !log.length)) {
    return (<div><Title sub={t('soccer.subPerformance')} /><EmptyBox title={t('soccer.emptyPerformance')} hint={t('soccer.emptyHint')} /></div>);
  }
  const pass = !!data.trade_grade;
  const hasCal = data.calibrated_brier != null;
  return (
    <div>
      <Title sub={t('soccer.subPerformance')} />
      <KV rows={[
        [t('soccer.lblSettled'), settled],
        [t('soccer.lblBrier'), t('soccer.cmpVsUniform', { v: num(data.brier, 4), u: num(data.brier_uniform, 4) })],
        ...(hasCal ? [[t('soccer.lblBrierCal'), <span style={{ color: pass ? 'var(--success)' : undefined }}>{t('soccer.cmpLeUniform', { v: num(data.calibrated_brier, 4), u: num(data.brier_uniform, 4) })}</span>] as [string, ReactNode]] : []),
        [t('soccer.lblLogLoss'), num(data.log_loss, 4)],
        [t('soccer.lblModelAcc'), pct(data.model_pred_accuracy ?? data.favourite_hit_rate, 0)],
        ...(data.avg_clv_cents != null ? [[t('soccer.lblAvgClv'), <span style={{ color: data.avg_clv_cents > 0 ? 'var(--success)' : undefined }}>{data.avg_clv_cents > 0 ? '+' : ''}{cc(data.avg_clv_cents)}</span>] as [string, ReactNode]] : []),
        [t('soccer.lblTradeGrade'), <span style={{ color: pass ? 'var(--success)' : 'var(--error)', fontWeight: 700 }}>{pass ? t('soccer.gradePass') : t('soccer.gradeBlock')}</span>],
      ]} />
      {!!log.length && (() => {
        // The exporter ships the record pre-formatted as "3W-12L"; W/L are English
        // initials, so it is re-composed from the two numbers instead.
        const raw = data.realized_record ?? data.hold_record ?? data.pnl_record;
        const wl = String(raw ?? '').match(/^(\d+)\s*W\s*-\s*(\d+)\s*L$/i);
        const record = wl ? t('soccer.record', { w: wl[1], l: wl[2] }) : raw;
        const pnl = data.combined_pnl_cents_total ?? data.hold_pnl_cents_total ?? data.pnl_cents_total;
        return (
          <div style={{ marginTop: 14 }}>
            {record && <div style={{ fontSize: 12, ...mono, marginBottom: 6, color: 'var(--text-primary)' }}><b>{record}</b>{pnl != null ? <> · {cVal(pnl)}</> : null}</div>}
            <DataTable cols={[t('soccer.colDate'), t('soccer.colMatch'), t('soccer.colPick'), t('soccer.colStake'), t('soccer.colResult'), t('soccer.colEntryC'), t('soccer.colPnlC'), t('soccer.colCumC')]}
              rows={log.map((b: any) => {
                const matchup = <span>{anyName(b.home, lang, t)} {b.score ?? ''} {anyName(b.away, lang, t)}</span>;
                if (b.bet === false) {
                  const m0 = <span style={{ color: 'var(--text-muted)' }}>—</span>;
                  return [b.date?.slice(5) ?? '—', matchup, <span style={{ color: 'var(--text-muted)' }}>{t('soccer.noBet')}</span>, m0, m0, '—', '—', cVal(b.cum_pnl_cents ?? b.combined_cum_pnl_cents)];
                }
                return [
                  b.date?.slice(5) ?? '—', matchup,
                  b.pick === 'draw' ? t('soccer.drawResult') : anyName(b.pick_team ?? b.pick, lang, t),
                  b.stake_usd != null ? fmtMoney(b.stake_usd, lang) : '—',
                  wlCell(t, b.won),
                  cc(b.entry_cents),
                  cVal(b.pnl_cents ?? b.realized_pnl_cents),
                  cVal(b.cum_pnl_cents ?? b.combined_cum_pnl_cents),
                ];
              })} />
          </div>
        );
      })()}
      {(() => {
        // performance_report.json ships notes_i18n=[{key,args}] next to the English
        // prose; the keys are the same templates the WC module already translates.
        const notes = notesOf(data.notes, data.notes_i18n);
        return notes.length ? (
          <ul style={{ marginTop: 10, paddingLeft: 16, fontSize: 11, color: 'var(--text-muted)', ...mono }}>
            {notes.map((n, i) => <li key={i} style={{ marginBottom: 4 }}>{n}</li>)}
          </ul>
        ) : null;
      })()}
    </div>
  );
}

/** 校准 (OOS) — mirror of wc_calibration reading oos_report.json. */
function Calibration() {
  const { t } = useTranslation();
  const notesOf = useLocalizedNotes();
  const { data, loading, error } = useApi<any>(() => getSoccerOos(), []);
  if (loading) return <Loading />;
  if (error || !data || data.n_matches == null) {
    return (<div><Title sub={t('soccer.subCalibration')} /><EmptyBox title={t('soccer.empty')} hint={t('soccer.emptyHint')} /></div>);
  }
  const ci = Array.isArray(data.brier_ci95) ? ` [${num(data.brier_ci95[0], 4)}, ${num(data.brier_ci95[1], 4)}]` : '';
  const baselines = data.baselines && typeof data.baselines === 'object'
    ? Object.entries(data.baselines).map(([k, v]) => [
      t('soccer.lblBaselineBrier', { name: t(`soccer.baseline.${k}`, { defaultValue: k }) }),
      num(v as number, 4),
    ] as [string, ReactNode])
    : [];
  return (
    <div>
      <Title sub={t('soccer.subCalibration')} />
      <KV rows={[
        [t('soccer.matches'), data.n_matches],
        [t('soccer.lblBrier'), `${num(data.brier, 4)}${ci}`],
        ...(data.brier_uniform != null ? [[t('soccer.lblBrierUniform'), num(data.brier_uniform, 4)] as [string, ReactNode]] : []),
        ...baselines,
        ...(data.log_loss != null ? [[t('soccer.lblLogLoss'), num(data.log_loss, 4)] as [string, ReactNode]] : []),
        ...(data.favourite_hit_rate != null ? [[t('soccer.lblFavHit'), pct(data.favourite_hit_rate, 0)] as [string, ReactNode]] : []),
        ...(data.pred_draw_rate != null ? [[t('soccer.lblDrawRate'), `${pct(data.pred_draw_rate, 0)} vs ${pct(data.obs_draw_rate, 0)}`] as [string, ReactNode]] : []),
        ...(data.pred_avg_total_goals != null ? [[t('soccer.lblAvgGoals'), `${num(data.pred_avg_total_goals, 2)} / ${num(data.obs_avg_total_goals, 2)}`] as [string, ReactNode]] : []),
      ]} />
      {(() => {
        const all = [...notesOf(data.notes, data.notes_i18n), ...notesOf(data.bias_notes, data.bias_notes_i18n)];
        return all.length ? (
          <ul style={{ marginTop: 10, paddingLeft: 16, fontSize: 11, color: 'var(--text-muted)', ...mono }}>
            {all.map((n, i) => <li key={i} style={{ marginBottom: 4 }}>{n}</li>)}
          </ul>
        ) : null;
      })()}
    </div>
  );
}

/** 回测 (OOS) — mirror of wc_backtest (model vs book vs uniform + blend curve). */
function Backtest() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const { data, loading, error } = useApi<any>(() => getSoccerBacktest(), []);
  if (loading) return <Loading />;
  const ms = (data?.matches ?? []) as any[];
  if (error || !data || (!data.n_settled && !ms.length)) {
    return (<div><Title sub={t('soccer.subBacktest')} /><EmptyBox title={t('soccer.empty')} hint={t('soccer.emptyHint')} /></div>);
  }
  const b = data.brier ?? {};
  const pass = !!data.trade_grade;
  return (
    <div>
      <Title sub={t('soccer.subBacktest')} />
      <KV rows={[
        [t('soccer.lblSettled'), <b>{data.n_settled}</b>],
        [t('soccer.lblModelBrier'), <b style={{ color: pass ? 'var(--success)' : 'var(--error)' }}>{b.model}</b>],
        ...(b.model_raw != null ? [[t('soccer.lblModelRaw'), b.model_raw] as [string, ReactNode]] : []),
        [t('soccer.lblBookBrier'), b.book ?? '—'],
        [t('soccer.lblBrierUniform'), b.uniform ?? '—'],
        ...(data.draw_rate != null ? [[t('soccer.lblDrawRate'), pct(data.draw_rate, 0)] as [string, ReactNode]] : []),
        ...(data.accuracy ? [[t('soccer.lblFavHit'), `${t('soccer.model')} ${data.accuracy.model_fav_hit ?? '—'} · ${t('soccer.colBook')} ${data.accuracy.book_fav_hit ?? '—'}`] as [string, ReactNode]] : []),
      ]} />
      {!!(data.blend_curve?.length) && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, margin: '2px 0 8px' }}>
          {t('soccer.lblBlend')}: {data.blend_curve.map((c: any) => `${Math.round(c.w * 100)}%→${c.brier}`).join('  ')}
        </div>
      )}
      <div style={{ fontSize: 11, color: pass ? 'var(--success)' : 'var(--error)', ...mono, marginBottom: 10, fontWeight: 700 }}>
        {pass ? t('soccer.backtestPass') : t('soccer.backtestFail')}
      </div>
      {!!ms.length && (
        <DataTable cols={[t('soccer.colMatch'), t('soccer.colScore'), t('soccer.colResult'), t('soccer.model'), t('soccer.colBook')]}
          rows={ms.map((m: any) => [
            <span>{anyName(m.home, lang, t)} {t('soccer.versus')} {anyName(m.away, lang, t)}</span>, m.score,
            sideAbbr(m.result, t),
            `${m.model_pick ? sideAbbr(m.model_pick, t) : '—'} ${m.model_p != null ? pct(m.model_p, 0) : ''}`,
            m.book_pick ? `${sideAbbr(m.book_pick, t)} ${pct(m.book_p, 0)}` : '—',
          ])} />
      )}
    </div>
  );
}

/** 参数扫描 — mirror of wc_params. The sweep is DISABLED during cold-start (~6
 * weeks sample discipline), so the missing-file state says exactly that. */
function ParamSweep() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const { data, loading, error } = useApi<any>(() => getSoccerParams(), []);
  if (loading) return <Loading />;
  if (error || !data?.candidates) {
    return (<div><Title sub={t('soccer.subParams')} /><EmptyBox title={t('soccer.emptyParams')} hint={t('soccer.emptyParamsHint')} /></div>);
  }
  // Candidates are compared on the SAME held-out window, so the table reads as a
  // decision record: which parameter set was chosen, against what alternatives,
  // and by how much. The per-competition breakdown sits underneath because a set
  // that wins overall can still lose in a specific league.
  const cands: [string, any][] = Object.entries(data.candidates);
  const winner = data.winner;
  const best = cands.find(([k]) => k === winner)?.[1];
  const perLg: [string, any][] = Object.entries(best?.test?.per_league ?? {})
    .sort((a: any, b: any) => (a[1]?.brier ?? 9) - (b[1]?.brier ?? 9)) as any;
  const fmtParams = (p: any) => Object.entries(p ?? {})
    .map(([k, v]) => `${t(`soccer.param.${k}`, { defaultValue: k.replace(/_/g, ' ') })}=${v}`).join('  ');
  return (
    <div>
      <Title sub={`${data.n_matches ?? '—'} ${t('soccer.matches')} · ${t('soccer.colTest')} ${data.n_test ?? '—'} (${data.test_days}d)`} />
      {data.ts && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginTop: -6, marginBottom: 8 }}>
          {t('soccer.asOf')} {fmtDateTime(data.ts, lang)}
        </div>
      )}
      <DataTable
        cols={[t('soccer.colCandidate'), t('soccer.colTestBrier'), t('soccer.colAcc'), t('soccer.colTrainBrier'), t('soccer.colParams')]}
        rows={cands.map(([k, v]: any) => [
          <span style={{ fontWeight: k === winner ? 700 : 400, color: k === winner ? 'var(--success)' : undefined }}>
            {t(`soccer.paramCand.${k}`, { defaultValue: k })}{k === winner ? ' ✓' : ''}
          </span>,
          num(v?.test?.brier, 4), pct(v?.test?.acc), num(v?.train?.brier, 4),
          <span style={{ fontSize: 10 }}>{fmtParams(v?.params)}</span>,
        ])} />
      {perLg.length > 0 && (
        <>
          <div style={{ fontSize: 11, fontWeight: 700, ...mono, margin: '12px 0 4px' }}>
            {t('soccer.paramPerLeague')}
          </div>
          <DataTable
            cols={[t('soccer.colLeague'), t('soccer.colTestBrier'), t('soccer.colMatches')]}
            rows={perLg.map(([lg, v]: any) => [
              leagueLabel({ league: lg } as any, lang, t), num(v?.brier, 4), v?.n ?? '—',
            ])} />
        </>
      )}
      {data.disclosure && (
        <div style={{ fontSize: 9, color: 'var(--text-muted)', ...mono, marginTop: 8 }}>{data.disclosure}</div>
      )}
    </div>
  );
}
function VenuesApi() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const noteOf = useLocalizedNote();
  const { data, loading, error } = useApi<any>(() => getSoccerRisk(), []);
  if (loading) return <Loading />;
  if (error || !data) {
    return (<div><Title sub={t('soccer.subVenues')} /><EmptyBox title={t('soccer.empty')} hint={t('soccer.emptyHint')} /></div>);
  }
  const g = data.gates ?? {}, b = data.venue_balances ?? {}, ab = data.api_budget ?? {};
  const cal = data.calibration_gate ?? {};
  // blocked_summary is English risk prose. Prefer the keyed export; otherwise rebuild the
  // same guardrails from the gate flags so the safety lines read in the UI language, and
  // only fall back to the raw sentences when neither is available.
  const rawBlocked: string[] = (data.blocked_summary ?? []);
  const derivedBlocked: string[] = [
    ...(g.pmus_trading_enabled === false ? [t('soccer.blocked.pmusOrders')] : []),
    ...(g.kalshi_trading_enabled === false ? [t('soccer.blocked.kalshiOrders')] : []),
    ...(g.hard_order_cap_usd != null ? [t('soccer.blocked.orderCap', { cap: fmtMoney(g.hard_order_cap_usd, lang) })] : []),
  ];
  const blocked: string[] = Array.isArray(data.blocked_i18n) && data.blocked_i18n.length
    ? data.blocked_i18n.map((x: any) => (typeof x === 'string' ? noteOf(null, x) : noteOf(null, x?.key, x?.args))).filter(Boolean)
    : (derivedBlocked.length ? derivedBlocked : rawBlocked);
  const tradingFlag = (v: any) => (typeof v === 'boolean' ? t(v ? 'soccer.tradingOn' : 'soccer.tradingOff') : '—');
  const frac = Math.min(1, (ab.used ?? 0) / (ab.cap ?? 1));
  const bar = (f: number, w = 110) => (
    <span style={{ display: 'inline-block', width: w, height: 8, background: 'var(--bg-tertiary)', border: '1px solid var(--border-subtle)', verticalAlign: 'middle' }}>
      <span style={{ display: 'block', width: `${f * 100}%`, height: '100%', background: f > 0.8 ? 'var(--error)' : 'var(--success)' }} />
    </span>
  );
  // calibration_gate.status is an English phrase ("PASS (calibrated)") the backend
  // pre-formats; the same verdict is already carried by trade_grade + method.
  const calVerdict = cal.trade_grade == null ? '—'
    : `${t(cal.trade_grade ? 'soccer.gradePass' : 'soccer.gradeBlock')}${cal.method ? ` · ${t(`soccer.calMethod.${cal.method}`, { defaultValue: cal.method })}` : ''}`;
  const controls: [string, ReactNode][] = [
    [t('soccer.lblKalshiEnv'), t(`soccer.env.${g.kalshi_env}`, { defaultValue: g.kalshi_env ?? '—' })],
    [t('soccer.lblOrderCap'), fmtMoney(g.hard_order_cap_usd, lang)],
    [t('soccer.lblCalibrationGate'), <span style={{ color: cal.trade_grade ? 'var(--success)' : 'var(--error)' }}>{calVerdict}</span>],
    [t('soccer.lblApiBudget'), <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, color: frac > 0.8 ? 'var(--error)' : undefined }}>
      {ab.used ?? '—'}/{ab.cap ?? '—'} ({pct(ab.pct, 0)}) {bar(frac)}
    </span>],
    ...(ab.month_used != null ? [(() => {
      const mfrac = Math.min(1, (ab.month_used ?? 0) / (ab.month_cap ?? 1));
      return [t('soccer.lblMonthBackstop'), <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
        {ab.month_used}/{ab.month_cap} {bar(mfrac)}
      </span>] as [string, ReactNode];
    })()] : []),
  ];
  return (
    <div>
      <Title sub={t('soccer.subVenues')} />
      <table className="table">
        <thead><tr>
          <th style={{ textAlign: 'left' }}>{t('soccer.colVenue')}</th>
          <th style={{ textAlign: 'right' }}>{t('soccer.colRole')}</th>
          <th style={{ textAlign: 'right' }}>{t('soccer.colBalance')}</th>
          <th style={{ textAlign: 'right' }}>{t('soccer.colTrading')}</th>
        </tr></thead>
        <tbody>
          {([
            [t('soccer.venueWithEnv', { venue: 'Kalshi', env: t('soccer.env.demo') }), t('soccer.roleExecute'), fmtMoney(b.kalshi_demo_usd, lang), tradingFlag(g.kalshi_trading_enabled)],
            ['Polymarket US', t('soccer.roleExecute'), fmtMoney(b.polymarket_us_usd, lang), tradingFlag(g.pmus_trading_enabled)],
            // The prod balance field carries an English sentence when the key was never
            // queried — only a real number belongs in a balance cell.
            [t('soccer.venueWithEnv', { venue: 'Kalshi', env: t('soccer.env.prod') }), t('soccer.roleRealMoney'),
              typeof b.kalshi_prod_usd === 'number' ? fmtMoney(b.kalshi_prod_usd, lang) : t('soccer.balanceNotQueried'), t('soccer.tradingGated')],
            ['Polymarket Global', t('soccer.roleReference'), fmtMoney(0, lang), t('soccer.tradingReadonly')],
          ] as ReactNode[][]).map((r, i) => (
            <tr key={`v${i}`}>{r.map((c, j) => <td key={j} style={{ textAlign: j === 0 ? 'left' : 'right' }}>{c}</td>)}</tr>
          ))}
          {controls.map(([k, v], i) => (
            <tr key={`g${i}`}>
              <td style={{ textAlign: 'left', fontWeight: 700 }}>{k}</td>
              <td colSpan={3} style={{ textAlign: 'right' }}>{v}</td>
            </tr>
          ))}
          {blocked.map((x, i) => (
            <tr key={`b${i}`}>
              <td colSpan={4} style={{ textAlign: 'left', color: 'var(--error)' }}>⛔ {x}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ marginTop: 10, fontSize: 10, color: 'var(--text-muted)', ...mono }}>{t('soccer.budgetNote')}</div>
    </div>
  );
}

/** 系统与模型说明 — reads frontend_overview.json (headline + gate badge + per-league
 * leaders + model notes + paper-mode disclosure). Until that exporter lands, falls
 * back to the soccer_model.json meta view (the v1 model-notes body) — which also
 * keeps the legacy soccer_model_notes deep-link meaningful. */
function Overview() {
  const { t, i18n } = useTranslation();
  const noteOf = useLocalizedNote();
  const notesOf = useLocalizedNotes();
  const lang = i18n.language || '';
  const { data, loading, error } = useApi<any>(() => getSoccerOverview(), []);
  if (loading) return <Loading />;
  if (error || !data) return <ModelNotes />;
  const leagues = (data.leagues ?? []) as any[];
  const series = data.series && typeof data.series === 'object' ? Object.entries(data.series) : [];
  const cal = data.calibration ?? {};
  const calBrier = cal.calibrated_brier ?? cal.brier;
  const calUniform = cal.uniform_brier ?? cal.brier_uniform;
  // The exporter writes `headline` in Chinese. Prefer its key, else rebuild the same
  // sentence from the calibration block through the five-language gate templates.
  const headline = noteOf(data.headline, data.headline_i18n?.key, data.headline_i18n?.args)
    || (calBrier != null
      ? t(`soccer.msg.overview.${data.gate_open ? 'gateOpen' : 'gateBlocked'}`,
        { brier: num(calBrier, 3), uniform: num(calUniform, 3), n: cal.n })
      : '');
  // `mode` is a machine token with a Chinese explanation glued on; keep the token and
  // let soccer.paperMode carry the explanation in the reader's language.
  const modeToken = String(data.mode ?? '').match(/^[a-z][a-z0-9_-]*/i)?.[0] || 'paper';
  const modelNotes = notesOf(data.model_notes, data.model_notes_i18n);
  return (
    <div>
      <Title sub={data.as_of ? `${t('soccer.asOf')} ${fmtDateTime(data.as_of, lang)}` : undefined}
        right={<span style={{ padding: '1px 8px', border: '1px solid', fontWeight: 700, ...mono,
          color: data.gate_open ? 'var(--success)' : 'var(--error)', borderColor: data.gate_open ? 'var(--success)' : 'var(--error)' }}>
          {data.gate_open ? t('soccer.gateOpen') : t('soccer.gateClosed')}
        </span>} />
      {!!headline && (
        <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 10, ...mono }}>{headline}</div>
      )}
      <KV rows={[
        [t('soccer.lblMode'), <span>{modeToken} <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>· {t('soccer.paperMode')}</span></span>],
        ...(data.n_upcoming != null ? [[t('soccer.lblUpcoming'), data.n_upcoming] as [string, ReactNode]] : []),
        ...(calBrier != null ? [[t('soccer.lblBrier'), calUniform != null
          ? t('soccer.cmpVsUniform', { v: num(calBrier, 4), u: num(calUniform, 4) })
          : num(calBrier, 4)] as [string, ReactNode]] : []),
      ]} />
      {!!leagues.length && (
        <>
          <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-primary)', ...mono, margin: '4px 0 4px' }}>{t('soccer.coverage')}</div>
          <DataTable cols={[t('soccer.colLeague'), t('soccer.colKind'), t('soccer.lblLeader'), t('soccer.colProb'), t('soccer.colRemaining')]}
            rows={leagues.map((l: any) => [
              <span style={{ color: 'var(--text-primary)', fontWeight: 600 }}>{leagueLabel(l, lang, t)}</span>,
              t(`soccer.kind.${l.kind}`, { defaultValue: l.kind ?? '—' }),
              anyName(l.leader, lang, t), pct(l.leader_p, 0), l.n_remaining,
            ])} />
        </>
      )}
      {!!series.length && (
        // series values are objects ({kalshi_game, kalshi_champion}) — interpolating them
        // straight into a template printed "[object Object]" next to a bare league id.
        <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginTop: 8 }}>
          {series.map(([k, v]) => {
            const tickers = v && typeof v === 'object' ? Object.values(v as any).filter(Boolean) : [v];
            return `${leagueLabel({ league: String(k) }, lang, t)}: ${tickers.join(' / ')}`;
          }).join(' · ')}
        </div>
      )}
      {!!modelNotes.length && (
        <ul style={{ margin: '10px 0 0', paddingLeft: 16, fontSize: 11, color: 'var(--text-muted)', ...mono }}>
          {modelNotes.map((n: string, i: number) => <li key={i} style={{ marginBottom: 4 }}>{n}</li>)}
        </ul>
      )}
    </div>
  );
}

/** 下载报告 — mirror of wc_pdfs (inline iframe + open link), /data/soccer/ paths.
 * The PDFs 404 during cold start — the note says so instead of a broken frame. */
function Pdfs() {
  const { t } = useTranslation();
  const reports = [
    { key: 'pnl', file: 'performance_report.pdf', label: t('soccer.pdfPerf') },
    { key: 'risk', file: 'risk_report.pdf', label: t('soccer.pdfRisk') },
  ];
  const [active, setActive] = useState('pnl');
  const [v] = useState(() => Date.now());   // cache-buster per mount
  const cur = reports.find((r) => r.key === active) ?? reports[0];
  const url = `${soccerFileUrl(cur.file)}?v=${v}`;
  const tabStyle = (on: boolean): CSSProperties => ({
    padding: '6px 14px', border: '2px solid var(--ink)', cursor: 'pointer', ...mono, fontSize: 12, fontWeight: 700,
    background: on ? 'var(--ink)' : 'var(--paper)', color: on ? 'var(--paper)' : 'var(--ink)', marginRight: 8,
  });
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Title sub={t('soccer.subPdfs')} />
      <div className="flex items-center justify-between" style={{ marginBottom: 6 }}>
        <div>
          {reports.map((r) => (
            <button key={r.key} style={tabStyle(r.key === active)} onClick={() => setActive(r.key)}>{r.label}</button>
          ))}
        </div>
        <a href={url} target="_blank" rel="noreferrer" style={{ ...mono, fontSize: 11, color: 'var(--text-muted)', textDecoration: 'underline' }}>
          {t('common.open')} ↗
        </a>
      </div>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginBottom: 8 }}>{t('soccer.pdfColdNote')}</div>
      <div style={{ flex: 1, minHeight: 'calc(100vh - 170px)', border: '2px solid var(--ink)', background: '#fff' }}>
        <iframe key={cur.key} src={url} title={cur.label} style={{ width: '100%', height: '100%', minHeight: 'calc(100vh - 170px)', border: 'none' }} />
      </div>
    </div>
  );
}

// ── dispatcher ───────────────────────────────────────────────────────────────
const REGISTRY: Record<string, () => ReactElement> = {
  soccer_season_odds: SeasonOdds,
  soccer_league_table: LeagueTable,
  soccer_top_scorer: TopScorer,
  soccer_bracket: SoccerBracket,
  soccer_squad: SquadStrength,
  soccer_styles: TeamStyles,
  soccer_form: FormCard,
  soccer_match_pricing: MatchPricing,
  soccer_predictions: Predictions,
  soccer_divergence: Divergence,
  soccer_schedule: ScheduleView,
  soccer_inplay: InPlay,
  soccer_pricetrack: PriceTrack,
  soccer_performance: PerformanceCard,
  soccer_calibration: Calibration,
  soccer_backtest: Backtest,
  soccer_params: ParamSweep,
  soccer_overview: Overview,
  soccer_venues: VenuesApi,
  soccer_pdfs: Pdfs,
  soccer_model_notes: Overview,   // v1 alias kept working (deep links / chat); grid card is soccer_overview
};

const KEY_BY_TYPE: Record<string, string> = {
  ...Object.fromEntries(SOCCER_ITEMS.map((i) => [i.type, i.i18nKey])),
  soccer_model_notes: 'overview',   // alias shares the overview title
};

export default function SoccerArtifact({ type, params }: { type: string; params?: any }) {
  const { t } = useTranslation();
  void params;
  const View = REGISTRY[type];
  if (!View) return <div className="text-xs py-3" style={{ color: 'var(--text-muted)', ...mono }}>{t('soccer.unknownArtifact', { type })}</div>;
  const key = KEY_BY_TYPE[type];
  return (
    <div>
      {key && (
        <div className="flex items-center justify-between" style={{ marginBottom: 6, minHeight: 22 }}>
          <div style={{ fontSize: 13, fontWeight: 700, letterSpacing: '.1em', textTransform: 'uppercase', color: 'var(--text-primary)', ...mono }}>{t(`soccer.${key}`)}</div>
        </div>
      )}
      <View />
    </div>
  );
}

export const isSoccerArtifact = (type?: string) => !!type && type.startsWith('soccer_');
