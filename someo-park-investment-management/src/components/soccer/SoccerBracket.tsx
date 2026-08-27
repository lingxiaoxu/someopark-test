/**
 * SoccerBracket — the knockout BRACKET card (C-18), reading `bracket.json` from
 * ops/cup_bracket_export.
 *
 * This is the card where the World Cup module's dead end gets closed: there, the
 * bracket tab ignored the backend entirely and drew a hardcoded R32 tree, so the
 * screen could not follow a draw. Here the rounds, the pairings and the advance
 * prices all come out of the payload — nothing about a competition's shape is
 * known to this file. Concretely that means Libertadores/Sudamericana render a
 * real bracket today and UCL/UEL/UECL fill in by themselves once their draw lands,
 * with no `if (league === 'ucl')` anywhere (§3.0 前端零赛制判断逻辑).
 *
 * A competition with no knockout at all (the big five + Brasileirão) is simply
 * absent from `leagues`, so it never gets a chip — the card hides its own
 * irrelevant halves instead of the grid having to know which leagues are cups.
 */
import type { CSSProperties } from 'react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import { apiHeaders } from '../../lib/api';
import { soccerFileUrl } from '../../lib/soccerApi';
import { leagueLabel, stageLabel, fmtDate, fmtDateTime, type ChipLeague } from './soccerLabels';

// ── payload contract (loose on purpose — the exporter is the truth) ──────────
type ClubRef = { club_id: string | null; name: string; zh?: string; logo?: string | null };
type Leg = {
  fixture_id: number; kickoff: string | null; status: string | null;
  host: 'a' | 'b'; goals_a: number | null; goals_b: number | null;
};
type Tie = {
  id: string; round: string; kind: 'two_leg' | 'single';
  a: ClubRef | null; b: ClubRef | null; legs: Leg[];
  agg_a: number | null; agg_b: number | null;
  decided: boolean; status: 'decided' | 'live' | 'scheduled';
  winner: 'a' | 'b' | null; p_a: number | null; neutral: boolean;
};
type Round = { round: string; stage: string; n_ties: number; open: boolean; ties: Tie[] };
type ChampionRow = { club_id: string; name: string; zh?: string; logo?: string | null; p: number };
type LeagueBracket = {
  league: string; name: string; zh?: string; kind: string;
  state: 'ok' | 'pending_draw' | 'pending_bracket';
  league_phase: { drawn: boolean; n_fixtures: number } | null;
  et_in_ties: boolean; n_rounds: number; rounds: Round[];
  champion: ChampionRow[] | null;
};
type BracketDoc = { as_of?: string; leagues?: LeagueBracket[]; note?: string };

// Own fetcher rather than a soccerApi getter: this card is the only consumer of
// bracket.json, and keeping it here means the file lands without touching the
// shared module. Same defensive parse as soccerApi (Python can emit bare NaN).
async function getSoccerBracket(): Promise<BracketDoc> {
  const res = await fetch(`${soccerFileUrl('bracket.json')}?_=${Date.now()}`,
    { headers: apiHeaders(), cache: 'no-store' });
  if (!res.ok) throw new Error(`API error: ${res.status} ${res.statusText}`);
  const text = await res.text();
  return JSON.parse(text.replace(/(?<![\w"'-])-?(?:NaN|Infinity)(?![\w"])/g, 'null'));
}

const mono: CSSProperties = { fontFamily: 'var(--font-mono)' };
const pct = (v?: number | null) => (v == null || isNaN(v) ? '—' : `${(v * 100).toFixed(0)}%`);

// ── small pieces ─────────────────────────────────────────────────────────────
function Chips({ leagues, value, onChange }: {
  leagues: ChipLeague[]; value: string; onChange: (v: string) => void;
}) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  if (!leagues.length) return null;
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginBottom: 10 }}>
      {leagues.map((l) => {
        const on = value === l.league;
        return (
          <button key={l.league} onClick={() => onChange(l.league)}
            style={{
              padding: '2px 9px', fontSize: 10, ...mono, fontWeight: 700, letterSpacing: '.04em',
              border: '1px solid var(--text-primary)', cursor: 'pointer', whiteSpace: 'nowrap',
              background: on ? 'var(--text-primary)' : 'transparent',
              color: on ? 'var(--bg-primary)' : 'var(--text-muted)', transition: 'all .1s',
            }}>
            {leagueLabel(l, lang, t)}
          </button>
        );
      })}
    </div>
  );
}

function Banner({ title, hint }: { title: string; hint?: string }) {
  return (
    <div style={{ padding: '14px 12px', border: '1px dashed var(--border-subtle)', textAlign: 'center', ...mono }}>
      <div style={{ fontSize: 12, color: 'var(--text-secondary)', fontWeight: 700 }}>{title}</div>
      {hint && <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

function Crest({ club }: { club: ClubRef | null }) {
  if (!club?.logo) return <span style={{ width: 14, height: 14, flex: '0 0 14px' }} />;
  return <img src={club.logo} alt="" loading="lazy" style={{ width: 14, height: 14, flex: '0 0 14px', objectFit: 'contain' }} />;
}

/** One side of a tie: crest, name, aggregate goals, and (while the tie is open)
 * the model's advance price. The winner carries the emphasis — a bracket is read
 * by scanning for who survived, so that has to be the loudest thing on the row. */
function Side({ club, goals, p, won, dim, lang }: {
  club: ClubRef | null; goals: number | null; p: number | null;
  won: boolean; dim: boolean; lang: string;
}) {
  const name = club ? ((lang || '').startsWith('zh') && club.zh ? club.zh : club.name) : '—';
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0' }}>
      <Crest club={club} />
      <span style={{
        flex: 1, fontSize: 11, ...mono, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
        fontWeight: won ? 700 : 400,
        color: won ? 'var(--text-primary)' : dim ? 'var(--text-muted)' : 'var(--text-secondary)',
      }}>{name}</span>
      {goals != null && (
        <span style={{ fontSize: 11, ...mono, fontWeight: 700, color: won ? 'var(--text-primary)' : 'var(--text-muted)', minWidth: 10, textAlign: 'right' }}>
          {goals}
        </span>
      )}
      {p != null && (
        <span style={{ fontSize: 10, ...mono, color: 'var(--accent-primary)', minWidth: 30, textAlign: 'right' }}>
          {pct(p)}
        </span>
      )}
    </div>
  );
}

// `key?` is declared in the props of the two list-rendered components below: the
// React typings this app compiles against do not inject `key` into a custom
// component's attributes, so a keyed <TieCard/> fails tsc without it. Repo idiom
// (RegimeDashboard's IndicatorCard, InventoryHistoryViewer's SectorDetail).
function TieCard({ tie, lang }: { tie: Tie; lang: string; key?: string }) {
  const { t } = useTranslation();
  const open = !tie.decided;
  const pa = open ? tie.p_a : null;
  const pb = open && tie.p_a != null ? 1 - tie.p_a : null;
  const hasAgg = tie.agg_a != null || tie.agg_b != null;

  // Leg line: a played leg shows its score, a future one its date. Two-leg ties
  // label the legs so "1-0 · 2-1" can't be misread as a single 3-1 result. The two
  // labels are separate keys rather than one templated "L{{n}}" because the natural
  // words are not numbered in every language (ida/vuelta, aller/retour, 首回合/次回合).
  // A single-match knockout always shows its DATE here: its score is already the
  // aggregate on the two rows above, and repeating it reads as a second result.
  const legs = tie.legs.map((l, i) => {
    const label = tie.kind === 'two_leg' && i < 2 ? t(`soccer.bracket.leg${i + 1}`) : '';
    const showScore = tie.kind === 'two_leg' && l.goals_a != null && l.goals_b != null;
    const body = showScore ? `${l.goals_a}-${l.goals_b}` : fmtDate(l.kickoff, lang) || '—';
    return `${label}${label ? ' ' : ''}${body}`;
  }).join(' · ');

  const badge = tie.status === 'live'
    ? { text: t('soccer.liveBadge'), color: 'var(--error)' }
    : tie.status === 'scheduled'
      ? { text: t('soccer.bracket.scheduled'), color: 'var(--text-muted)' }
      : null;

  return (
    <div style={{
      border: '1px solid var(--border-subtle)', background: 'var(--bg-secondary)',
      padding: '6px 8px', borderLeft: `2px solid ${open ? 'var(--accent-primary)' : 'var(--border-subtle)'}`,
    }}>
      <Side club={tie.a} goals={hasAgg ? tie.agg_a : null} p={pa} won={tie.winner === 'a'} dim={tie.winner === 'b'} lang={lang} />
      <Side club={tie.b} goals={hasAgg ? tie.agg_b : null} p={pb} won={tie.winner === 'b'} dim={tie.winner === 'a'} lang={lang} />
      <div style={{ display: 'flex', gap: 6, alignItems: 'baseline', marginTop: 3, fontSize: 9, ...mono, color: 'var(--text-muted)' }}>
        <span style={{ flex: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{legs}</span>
        {tie.neutral && <span>{t('soccer.bracket.neutral')}</span>}
        {badge && <span style={{ color: badge.color, fontWeight: 700 }}>{badge.text}</span>}
      </div>
    </div>
  );
}

function RoundSection({ r, defaultOpen, lang }: { r: Round; defaultOpen: boolean; lang: string; key?: string }) {
  const { t } = useTranslation();
  const [show, setShow] = useState(defaultOpen);
  return (
    <div style={{ marginBottom: 10 }}>
      <button onClick={() => setShow((s) => !s)}
        style={{
          display: 'flex', alignItems: 'center', gap: 6, width: '100%', textAlign: 'left',
          background: 'transparent', border: 'none', cursor: 'pointer', padding: '2px 0',
          fontSize: 10, fontWeight: 700, letterSpacing: '.1em', textTransform: 'uppercase',
          color: r.open ? 'var(--text-primary)' : 'var(--text-muted)', ...mono,
        }}>
        <span>{show ? '▾' : '▸'}</span>
        <span>{stageLabel(r.round, t)}</span>
        <span style={{ fontWeight: 400, letterSpacing: 0, textTransform: 'none' }}>
          {t('soccer.bracket.tieCount', { n: r.n_ties })}
        </span>
        {r.open && <span style={{ color: 'var(--accent-primary)' }}>●</span>}
      </button>
      {show && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))', gap: 6, marginTop: 5 }}>
          {r.ties.map((tie) => <TieCard key={tie.id} tie={tie} lang={lang} />)}
        </div>
      )}
    </div>
  );
}

function ChampionBoard({ rows, lang }: { rows: ChampionRow[]; lang: string }) {
  const { t } = useTranslation();
  const top = rows[0]?.p || 1;
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.1em', textTransform: 'uppercase', color: 'var(--text-muted)', ...mono, marginBottom: 5 }}>
        {t('soccer.bracket.championTitle')}
      </div>
      {rows.map((c) => (
        <div key={c.club_id} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0' }}>
          <Crest club={c} />
          <span style={{ flex: 1, fontSize: 11, ...mono, color: 'var(--text-secondary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {(lang || '').startsWith('zh') && c.zh ? c.zh : c.name}
          </span>
          <span style={{ width: 60, height: 5, background: 'var(--bg-tertiary)' }}>
            <span style={{ display: 'block', height: '100%', width: `${Math.max(2, (c.p / top) * 100)}%`, background: 'var(--accent-primary)' }} />
          </span>
          <span style={{ fontSize: 10, ...mono, minWidth: 34, textAlign: 'right', color: 'var(--text-primary)' }}>
            {(c.p * 100).toFixed(1)}%
          </span>
        </div>
      ))}
    </div>
  );
}

// ── card ─────────────────────────────────────────────────────────────────────
const LEAGUE_LS_KEY = 'soccer-league';   // same preference the other soccer cards persist

export default function SoccerBracket() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const { data, loading, error } = useApi<BracketDoc>(getSoccerBracket, []);
  const [pick, setPick] = useState<string>(() => {
    try { return localStorage.getItem(LEAGUE_LS_KEY) || ''; } catch { return ''; }
  });

  if (loading) return <div className="text-xs py-3" style={{ color: 'var(--text-muted)', ...mono }}>{t('common.loading')}</div>;
  // A missing file is the normal state until the exporter has run once — that is an
  // empty state with a reason, not the red "load failed" an actual break deserves.
  if (error || !data) return <Banner title={t('soccer.bracket.empty')} hint={t('soccer.bracket.emptyHint')} />;

  const leagues = data.leagues || [];
  if (!leagues.length) return <Banner title={t('soccer.bracket.empty')} hint={t('soccer.bracket.emptyHint')} />;

  const chips: ChipLeague[] = leagues.map((l) => ({ league: l.league, name: l.name, zh: l.zh }));
  const cur = leagues.find((l) => l.league === pick) || leagues[0];
  const choose = (v: string) => {
    setPick(v);
    try { localStorage.setItem(LEAGUE_LS_KEY, v); } catch { /* private mode — ignore */ }
  };

  const rounds = cur.rounds || [];
  const anyOpen = rounds.some((r) => r.open);
  const nTies = rounds.reduce((s, r) => s + r.n_ties, 0);
  // Expanding every round would bury the live one under ~100 settled qualifiers
  // (UECL), and expanding none leaves a finished competition looking empty — so
  // open rounds expand, and a fully-settled bracket expands its last round.
  const openBy = (r: Round, i: number) => (anyOpen ? r.open : i === rounds.length - 1);

  return (
    <div>
      <Chips leagues={chips} value={cur.league} onChange={choose} />
      <div className="mb-3" style={{ fontSize: 10, color: 'var(--text-muted)', ...mono }}>
        {t('soccer.bracket.sub', { rounds: rounds.length, ties: nTies })}
        {data.as_of ? ` · ${t('soccer.asOf')} ${fmtDateTime(data.as_of, lang)}` : ''}
      </div>

      {cur.league_phase && !cur.league_phase.drawn && (
        <div style={{ marginBottom: 10 }}>
          <Banner title={t('soccer.bracket.leaguePhasePending')} hint={t('soccer.bracket.leaguePhasePendingHint')} />
        </div>
      )}

      {rounds.length === 0
        ? <Banner
            title={cur.state === 'pending_draw' ? t('soccer.bracket.pendingDraw') : t('soccer.bracket.pendingBracket')}
            hint={cur.state === 'pending_draw' ? t('soccer.bracket.pendingDrawHint') : t('soccer.bracket.pendingBracketHint')} />
        // keyed by league too: "Quarter-finals" exists in most competitions, and a
        // bare round-name key would carry one league's expand state into the next.
        : rounds.map((r, i) => <RoundSection key={`${cur.league}:${r.round}`} r={r} defaultOpen={openBy(r, i)} lang={lang} />)}

      {cur.champion && cur.champion.length > 0 && <ChampionBoard rows={cur.champion} lang={lang} />}

      <div style={{ fontSize: 9, color: 'var(--text-muted)', ...mono, marginTop: 12, lineHeight: 1.5 }}>
        {t('soccer.bracket.note')}
      </div>
    </div>
  );
}
