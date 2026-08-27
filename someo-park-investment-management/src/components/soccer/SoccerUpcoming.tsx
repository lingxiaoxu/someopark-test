/**
 * SoccerUpcoming — the "即将开赛 / Upcoming Matches" card on the Club Soccer Market
 * welcome screen. Mirrors PredictionUpcoming's chrome (pixel card + corner dots)
 * with the §3.7 league→match hierarchy: LIVE matches first (cross-league, pulsing
 * badge, click → soccer_inplay), then the soonest fixtures GROUPED BY LEAGUE.
 * Polls upcoming.json every 60s and inplay_live.json every 30s; both files may not
 * exist yet → clean empty state. The Regulation/Advances selector renders only
 * when the data carries ≥1 caps.advance match (§3.0 capability rule).
 */
import { useTranslation } from 'react-i18next';
import { useSetArtifact } from '../../contexts/ArtifactContext';
import { usePoll } from '../prediction/usePoll';
import { AdvanceModeToggle } from '../prediction/AdvanceMode';
import { getSoccerUpcoming, getSoccerInplay, type SoccerUpcomingMatch } from '../../lib/soccerApi';
import SoccerMatchCard, { clubName } from './SoccerMatchCard';
import { leagueLabel } from './soccerLabels';

const pct = (v?: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`);

// A live match: pulsing badge + score + minute + live model; click → in-play view.
function LiveCard({ m }: { m: any }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const setArtifact = useSetArtifact();
  const openInplay = () => setArtifact({ type: 'soccer_inplay', title: t('soccer.inPlay') });
  return (
    <div
      className="pair-card"
      onClick={openInplay}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openInplay(); } }}
      style={{ display: 'flex', flexDirection: 'column', gap: 5, minWidth: 280, flex: '1 1 320px', borderLeft: '4px solid var(--error)', cursor: 'pointer' }}
    >
      <div className="flex items-center justify-between">
        <span style={{ fontSize: 13, fontWeight: 700, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)', textTransform: 'uppercase', letterSpacing: '.03em' }}>
          <span className="pulse" style={{ color: 'var(--error)', marginRight: 6 }}>● {t('soccer.liveBadge')}</span>
          {clubName(m.home, lang, t)} <b style={{ color: 'var(--text-primary)' }}>{m.score ?? ''}</b> {clubName(m.away, lang, t)}
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{m.minute != null ? t('soccer.minuteSuffix', { n: m.minute }) : ''}</span>
      </div>
      <div style={{ fontSize: 9, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}>
        {leagueLabel({ league: m.league ?? '', zh: m.league_zh }, lang, t)}
      </div>
      <div style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}>
        {t('soccer.model')}: {t('soccer.abbrHome')} {pct(m.model?.home)} · {t('soccer.abbrDraw')} {pct(m.model?.draw)} · {t('soccer.abbrAway')} {pct(m.model?.away)}
      </div>
      <div style={{ fontSize: 10, fontFamily: 'var(--font-mono)', color: 'var(--success)', fontWeight: 700 }}>
        {m.opportunities?.length ? `${m.opportunities.length} ` : ''}{t('soccer.inPlay')} →
      </div>
    </div>
  );
}

export default function SoccerUpcoming() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const setArtifact = useSetArtifact();
  const up = usePoll<{ matches?: SoccerUpcomingMatch[]; recent_finished?: any[] }>(() => getSoccerUpcoming(), 60000);
  const live = usePoll<{ matches?: any[] }>(() => getSoccerInplay(), 30000);

  const SLOTS = 6;
  const now = Date.now();
  const liveMatches = (live.data?.matches ?? []).slice(0, SLOTS);
  const liveKeys = new Set(liveMatches.map((m: any) => `${m.home?.id}|${m.away?.id}`));
  // Soonest not-started fixtures (never a match already confirmed live).
  const all = up.data?.matches ?? [];
  const future = all.filter((m) => new Date(m.kickoff).getTime() > now);
  const pool = (future.length ? future : all)
    .filter((m) => !liveKeys.has(`${m.home?.id}|${m.away?.id}`));
  const upMatches = pool.slice(0, Math.max(0, SLOTS - liveMatches.length));

  // §3.7: group the upcoming slice by league (LIVE stays on top, cross-league).
  const groups = new Map<string, { zh?: string; matches: SoccerUpcomingMatch[] }>();
  for (const m of upMatches) {
    const key = m.league || '';
    if (!groups.has(key)) groups.set(key, { zh: m.league_zh, matches: [] });
    groups.get(key)!.matches.push(m);
  }

  const hasAdvance = all.some((m) => m.caps?.advance) || liveMatches.some((m: any) => m.caps?.advance);
  const total = liveMatches.length + upMatches.length;
  const loading = up.loading && live.loading && !total;
  const openPredictions = () => setArtifact({ type: 'soccer_predictions', title: t('soccer.todaysPredictions') });

  return (
    <div className="p-4 relative" style={{ background: 'var(--paper)', border: '3px solid var(--ink)', boxShadow: 'var(--shadow-pixel-sm)' }}>
      <div style={{ position: 'absolute', top: -2, left: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div style={{ position: 'absolute', top: -2, right: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div style={{ position: 'absolute', bottom: -2, left: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div style={{ position: 'absolute', bottom: -2, right: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div className="flex items-center justify-between mb-3">
        <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.14em', textTransform: 'uppercase', color: 'var(--text-primary)', fontFamily: 'var(--font-mono)' }}>
          {t('soccer.upcomingMatches')} <span style={{ color: 'var(--success)' }}>({total})</span>
          {!!liveMatches.length && <span className="pulse" style={{ color: 'var(--error)', marginLeft: 8 }}>● {liveMatches.length} {t('soccer.live')}</span>}
        </div>
        <div className="flex items-center gap-2">
          {/* Same slot the World Cup module puts "World Cup 2026" in: a quiet label
              naming WHAT this board covers, sitting left of the reg/advance toggle.
              Clicking it still opens the predictions view — the affordance the club
              version added — but it now reads as the board's title rather than as a
              second control competing with the toggle beside it. */}
          <button onClick={openPredictions} title={t('soccer.todaysPredictions')}
            style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', background: 'transparent', border: 'none', cursor: 'pointer', padding: 0 }}>
            {t('soccer.boardLabel')}
          </button>
          {hasAdvance && <AdvanceModeToggle />}
        </div>
      </div>
      {loading ? (
        <div className="text-xs py-2" style={{ color: 'var(--text-muted)' }}>{t('soccer.loadingUpcoming')}</div>
      ) : !total ? (
        <div className="text-xs py-2" style={{ color: 'var(--text-muted)' }}>{t('soccer.noUpcoming')}</div>
      ) : (
        <>
          {!!liveMatches.length && (
            <div className="flex flex-wrap gap-2" style={{ marginBottom: 8 }}>
              {liveMatches.map((m: any, i: number) => (
                <span key={'live' + (m.fixture_id ?? i)} style={{ display: 'contents' }}><LiveCard m={m} /></span>
              ))}
            </div>
          )}
          {[...groups.entries()].map(([league, g]) => (
            <div key={league}>
              <div style={{ fontSize: 9, fontWeight: 700, letterSpacing: '.12em', textTransform: 'uppercase', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', margin: '6px 0 4px' }}>
                {leagueLabel({ league, zh: g.zh }, lang, t)}
              </div>
              <div className="flex flex-wrap gap-2">
                {g.matches.map((m, i) => (
                  <span key={(m.kickoff ?? '') + (m.home?.id ?? i)} style={{ display: 'contents' }}><SoccerMatchCard m={m} /></span>
                ))}
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}
