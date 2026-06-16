/**
 * PredictionUpcoming — replaces the "Active Pairs" card in Prediction Market mode.
 * Shows the next 1–3 upcoming World Cup matches (teams + model + book + real
 * Kalshi/Poly quotes) from data/upcoming.json. Matches whose kickoff has passed
 * roll off automatically on the next backend sync, so this always shows the
 * soonest fixtures. Mirrors the Active Pairs card chrome (pixel card + corner dots).
 */
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import { getWCUpcoming } from '../../lib/api';
import MatchCard, { type UpcomingMatch } from './MatchCard';

function pickUpcoming(matches: UpcomingMatch[]): UpcomingMatch[] {
  const now = Date.now();
  const future = matches.filter((m) => new Date(m.kickoff).getTime() > now);
  // Always show at least one: if everything has kicked off, fall back to the
  // soonest entries the backend still lists (it only emits NS fixtures).
  const pool = future.length ? future : matches;
  return pool.slice(0, 3);
}

export default function PredictionUpcoming() {
  const { t } = useTranslation();
  const { data, loading, error } = useApi<{ matches?: UpcomingMatch[] }>(() => getWCUpcoming(), []);
  const matches = data?.matches ? pickUpcoming(data.matches) : [];

  return (
    <div className="p-4 relative" style={{ background: 'var(--paper)', border: '3px solid var(--ink)', boxShadow: 'var(--shadow-pixel-sm)' }}>
      {/* Corner dots */}
      <div style={{ position: 'absolute', top: -2, left: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div style={{ position: 'absolute', top: -2, right: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div style={{ position: 'absolute', bottom: -2, left: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div style={{ position: 'absolute', bottom: -2, right: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div className="flex items-center justify-between mb-3">
        <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.14em', textTransform: 'uppercase', color: 'var(--text-primary)', fontFamily: 'var(--font-mono)' }}>
          {t('prediction.upcomingMatches')} <span style={{ color: 'var(--success)' }}>({matches.length})</span>
        </div>
        <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>World Cup 2026</div>
      </div>
      {loading ? (
        <div className="text-xs py-2" style={{ color: 'var(--text-muted)' }}>{t('prediction.loadingUpcoming')}</div>
      ) : error || !matches.length ? (
        <div className="text-xs py-2" style={{ color: 'var(--text-muted)' }}>{t('prediction.noUpcoming')}</div>
      ) : (
        <div className="flex flex-wrap gap-2">
          {matches.map((m) => (
            <span key={m.kickoff + m.home.id} style={{ display: 'contents' }}><MatchCard m={m} /></span>
          ))}
        </div>
      )}
    </div>
  );
}
