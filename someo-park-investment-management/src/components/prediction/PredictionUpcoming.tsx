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
import { useSetArtifact } from '../../contexts/ArtifactContext';
import MatchCard, { type UpcomingMatch } from './MatchCard';
import { AdvanceModeToggle } from './AdvanceMode';
import { usePoll } from './usePoll';

const pct = (v?: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`);
const cc = (v?: number | null) => (v == null ? '—' : `${Math.round(v)}¢`);

// Fill the remaining slots (after live matches) with the soonest not-started fixtures.
function pickUpcoming(matches: UpcomingMatch[], slots: number): UpcomingMatch[] {
  if (slots <= 0) return [];
  const now = Date.now();
  const future = matches.filter((m) => new Date(m.kickoff).getTime() > now);
  const pool = future.length ? future : matches;
  return pool.slice(0, slots);
}

// A live match keeps its slot in the top region (not dropped): pulsing LIVE badge,
// live score + minute + live model, and the whole card is clickable to jump into
// the in-play arbitrage view.
function LiveCard({ m }: { m: any }) {
  const { t } = useTranslation();
  const setArtifact = useSetArtifact();
  const openInplay = () => setArtifact({ type: 'wc_inplay', title: t('prediction.inPlayArb') });
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
          <span className="pulse" style={{ color: 'var(--error)', marginRight: 6 }}>● {t('prediction.liveBadge')}</span>
          {tCountry(m.home.name)} <b style={{ color: 'var(--text-primary)' }}>{m.score}</b> {tCountry(m.away.name)}
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{m.minute}'</span>
      </div>
      <div style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}>
        {t('prediction.model')}: H {pct(m.model?.home)} · D {pct(m.model?.draw)} · A {pct(m.model?.away)}
      </div>
      {(() => {
        const mk = m.prices?.kalshi || m.prices?.poly_us;
        const src = m.prices?.kalshi ? 'Kalshi' : m.prices?.poly_us ? 'Poly' : null;
        if (mk && src) return (
          <div style={{ fontSize: 10, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}>
            {/* mid_c falls back to the bid when a deep ITM contract has no ask. */}
            {src}: H {cc(mk.home?.mid_c)} · D {cc(mk.draw?.mid_c)} · A {cc(mk.away?.mid_c)}
          </div>
        );
        if (m.prices?.model_c) return (
          <div style={{ fontSize: 10, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}>
            ¢: H {cc(m.prices.model_c.home)} · D {cc(m.prices.model_c.draw)} · A {cc(m.prices.model_c.away)}
          </div>
        );
        return null;
      })()}
      <div style={{ fontSize: 10, fontFamily: 'var(--font-mono)', color: 'var(--success)', fontWeight: 700 }}>
        {m.opportunities?.length ? `${m.opportunities.length} ` : ''}{t('prediction.inPlayArb')} →
      </div>
    </div>
  );
}

// A match whose scheduled kickoff just passed but the live feed hasn't confirmed yet:
// "kicking off" placeholder so the slot isn't empty during the API's ~3-5 min status lag.
// Clickable into the in-play view (which will populate once the feed flips it live).
function KickingOffCard({ m }: { m: any }) {
  const { t } = useTranslation();
  const setArtifact = useSetArtifact();
  const openInplay = () => setArtifact({ type: 'wc_inplay', title: t('prediction.inPlayArb') });
  return (
    <div
      className="pair-card"
      onClick={openInplay}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openInplay(); } }}
      style={{ display: 'flex', flexDirection: 'column', gap: 5, minWidth: 280, flex: '1 1 320px', borderLeft: '4px solid var(--warning, #d08b00)', cursor: 'pointer' }}
    >
      <div className="flex items-center justify-between">
        <span style={{ fontSize: 13, fontWeight: 700, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)', textTransform: 'uppercase', letterSpacing: '.03em' }}>
          <span className="pulse" style={{ color: 'var(--warning, #d08b00)', marginRight: 6 }}>● {t('prediction.kickingOff')}</span>
          {tCountry(m.home?.name)} <b style={{ color: 'var(--text-muted)', fontWeight: 400 }}>vs</b> {tCountry(m.away?.name)}
        </span>
        <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{t('prediction.kickoffWaiting')}</span>
      </div>
      <div style={{ fontSize: 10, fontFamily: 'var(--font-mono)', color: 'var(--success)', fontWeight: 700 }}>
        {t('prediction.inPlayArb')} →
      </div>
    </div>
  );
}

// A just-finished match: marked FT with the final score, shown briefly so a live
// match that ends is acknowledged (not silently dropped) before it rolls off.
function FinishedCard({ m }: { m: any }) {
  const { t } = useTranslation();
  return (
    <div className="pair-card" style={{ display: 'flex', flexDirection: 'column', gap: 5, minWidth: 280, flex: '1 1 320px', borderLeft: '4px solid var(--text-muted)', opacity: 0.85 }}>
      <div className="flex items-center justify-between">
        <span style={{ fontSize: 13, fontWeight: 700, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)', textTransform: 'uppercase', letterSpacing: '.03em' }}>
          <span style={{ color: 'var(--text-muted)', marginRight: 6 }}>● {t('prediction.finished')}</span>
          {tCountry(m.home.name)} <b style={{ color: 'var(--text-primary)' }}>{m.score}</b> {tCountry(m.away.name)}
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{m.status}</span>
      </div>
    </div>
  );
}

export default function PredictionUpcoming() {
  const { t } = useTranslation();
  const up = usePoll<{ matches?: UpcomingMatch[]; recent_finished?: any[] }>(() => getWCUpcoming(), 60000);
  const live = usePoll<{ matches?: any[] }>(() => getWCInplayLive(), 20000);

  // Top region: live matches first (still in progress), then matches that just
  // finished (FT + score, marked ended), then the soonest not-started fixtures —
  // up to 4 slots, never dropping live/finished.
  const SLOTS = 4;
  const now = Date.now();
  const liveMatches = (live.data?.matches ?? []).slice(0, SLOTS);
  // Scheduled kickoff has passed but the live feed hasn't CONFIRMED the match yet
  // (API-Football flips a fixture to "1H" ~3-5 min after the real kickoff). Show a
  // "kicking off" placeholder in the SAME region so the slot isn't empty during that gap;
  // it rolls into liveMatches automatically once the feed confirms (deduped by team pair).
  // SAFETY: the real live trigger (liveMatches, sourced from inplay_live) is left untouched —
  // this only fills the pre-confirmation gap and never gates or delays the real entry.
  const liveKeys = new Set(liveMatches.map((m: any) => `${m.home?.id}|${m.away?.id}`));
  const kickingOff = (up.data?.matches ?? []).filter((m: any) => {
    const ko = new Date(m.kickoff).getTime();
    return ko <= now && ko > now - 25 * 60 * 1000 && !liveKeys.has(`${m.home?.id}|${m.away?.id}`);
  }).slice(0, Math.max(0, SLOTS - liveMatches.length));
  const koKeys = new Set(kickingOff.map((m: any) => `${m.home?.id}|${m.away?.id}`));
  const finishedMatches = (up.data?.recent_finished ?? []).slice(0, 2);
  const fillN = Math.max(0, SLOTS - liveMatches.length - kickingOff.length - finishedMatches.length);
  // pickUpcoming returns FUTURE fixtures (kickoff > now), so kicking-off matches are never
  // double-listed; still guard by team pair in case of clock skew.
  const upMatches = (up.data?.matches ? pickUpcoming(up.data.matches, fillN) : [])
    .filter((m: any) => !koKeys.has(`${m.home?.id}|${m.away?.id}`));
  const total = liveMatches.length + kickingOff.length + finishedMatches.length + upMatches.length;
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
        <div className="flex items-center gap-2">
          <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>World Cup 2026</div>
          <AdvanceModeToggle />
        </div>
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
          {kickingOff.map((m: any) => (
            <span key={'ko' + m.home?.id + m.away?.id} style={{ display: 'contents' }}><KickingOffCard m={m} /></span>
          ))}
          {finishedMatches.map((m, i) => (
            <span key={'ft' + i + m.home.id} style={{ display: 'contents' }}><FinishedCard m={m} /></span>
          ))}
          {upMatches.map((m) => (
            <span key={m.kickoff + m.home.id} style={{ display: 'contents' }}><MatchCard m={m} /></span>
          ))}
        </div>
      )}
    </div>
  );
}
