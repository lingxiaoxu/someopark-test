/**
 * PredictionUpcoming — the "今日比赛 / Upcoming Matches" card in Prediction Market mode.
 * Shows LIVE matches first (prominent pulsing badge + score + minute + per-minute
 * live model), then the next not-started fixtures. Both auto-refresh: the live
 * feed every 30s (model + score update each poll during a match), the upcoming
 * list every 60s. Mirrors the Active Pairs card chrome (pixel card + corner dots).
 */
import { useTranslation } from 'react-i18next';
import { getWCUpcoming, getWCInplayLive } from '../../lib/api';
import { tCountry } from '../../i18n/countries';
import MatchCard, { type UpcomingMatch } from './MatchCard';
import { usePoll } from './usePoll';

const pct = (v?: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`);

function pickUpcoming(matches: UpcomingMatch[]): UpcomingMatch[] {
  const now = Date.now();
  const future = matches.filter((m) => new Date(m.kickoff).getTime() > now);
  const pool = future.length ? future : matches;
  return pool.slice(0, 3);
}

function LiveCard({ m }: { m: any }) {
  const { t } = useTranslation();
  return (
    <div className="pair-card" style={{ display: 'flex', flexDirection: 'column', gap: 5, minWidth: 280, flex: '1 1 320px', borderLeft: '4px solid var(--error)' }}>
      <div className="flex items-center justify-between">
        <span style={{ fontSize: 13, fontWeight: 700, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)', textTransform: 'uppercase', letterSpacing: '.03em' }}>
          <span className="pulse" style={{ color: 'var(--error)', marginRight: 6 }}>● {t('prediction.liveBadge')}</span>
          {tCountry(m.home.name)} <b style={{ color: 'var(--text-primary)' }}>{m.score}</b> {tCountry(m.away.name)}
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{m.minute}'</span>
      </div>
      <div style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}>
        {t('prediction.model')}: H {pct(m.model?.home)} · D {pct(m.model?.draw)} · A {pct(m.model?.away)}
      </div>
      {!!(m.opportunities?.length) && (
        <div style={{ fontSize: 10, fontFamily: 'var(--font-mono)', color: 'var(--success)', fontWeight: 700 }}>
          {m.opportunities.length} {t('prediction.inPlayArb')} →
        </div>
      )}
    </div>
  );
}

export default function PredictionUpcoming() {
  const { t } = useTranslation();
  const up = usePoll<{ matches?: UpcomingMatch[] }>(() => getWCUpcoming(), 60000);
  const live = usePoll<{ matches?: any[] }>(() => getWCInplayLive(), 30000);

  const liveMatches = live.data?.matches ?? [];
  const upMatches = up.data?.matches ? pickUpcoming(up.data.matches) : [];
  const total = liveMatches.length + upMatches.length;
  const loading = up.loading && live.loading && !total;

  return (
    <div className="p-4 relative" style={{ background: 'var(--paper)', border: '3px solid var(--ink)', boxShadow: 'var(--shadow-pixel-sm)' }}>
      <div style={{ position: 'absolute', top: -2, left: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div style={{ position: 'absolute', top: -2, right: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div style={{ position: 'absolute', bottom: -2, left: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div style={{ position: 'absolute', bottom: -2, right: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div className="flex items-center justify-between mb-3">
        <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.14em', textTransform: 'uppercase', color: 'var(--text-primary)', fontFamily: 'var(--font-mono)' }}>
          {t('prediction.upcomingMatches')} <span style={{ color: 'var(--success)' }}>({total})</span>
          {!!liveMatches.length && <span className="pulse" style={{ color: 'var(--error)', marginLeft: 8 }}>● {liveMatches.length} {t('prediction.live')}</span>}
        </div>
        <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>World Cup 2026</div>
      </div>
      {loading ? (
        <div className="text-xs py-2" style={{ color: 'var(--text-muted)' }}>{t('prediction.loadingUpcoming')}</div>
      ) : !total ? (
        <div className="text-xs py-2" style={{ color: 'var(--text-muted)' }}>{t('prediction.noUpcoming')}</div>
      ) : (
        <div className="flex flex-wrap gap-2">
          {liveMatches.map((m) => (
            <span key={'live' + m.fixture_id} style={{ display: 'contents' }}><LiveCard m={m} /></span>
          ))}
          {upMatches.map((m) => (
            <span key={m.kickoff + m.home.id} style={{ display: 'contents' }}><MatchCard m={m} /></span>
          ))}
        </div>
      )}
    </div>
  );
}
