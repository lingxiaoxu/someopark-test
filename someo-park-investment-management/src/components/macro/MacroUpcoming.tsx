/**
 * MacroUpcoming — the "近期数据 / Recent Data" card strip in Macro Markets mode.
 * Shows the next ~8 scheduled releases (mixed cadence) from macro_board.json's
 * next_releases[], sorted by scheduled time, with a live countdown, a cadence
 * chip and — when the board carries a decision for that series+period — a
 * status dot (open = green, pass = grey). Card click opens the family artifact
 * (fed → macro_fed, labor → macro_labor, inflation → macro_inflation).
 * Mirrors the PredictionUpcoming card chrome (pixel card + corner dots).
 */
import { useTranslation } from 'react-i18next';
import { useSetArtifact } from '../../contexts/ArtifactContext';
import { getMacroJson, familyArtifact, predSummary } from './macroApi';
import type { MacroBoard, MacroRelease } from './macroApi';
import { mono, useMacroPoll } from './primitives';

const SLOTS = 8;

function countdown(t: (k: string, o?: any) => string, iso: string): string {
  const ms = new Date(iso).getTime() - Date.now();
  if (isNaN(ms)) return '—';
  if (ms <= 0) return t('macro.countdownPast');
  const mins = Math.floor(ms / 60000);
  const d = Math.floor(mins / 1440);
  const h = Math.floor((mins % 1440) / 60);
  const m = mins % 60;
  const v = d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
  return t('macro.countdownIn', { v });
}

function ReleaseCard({ r, board }: { r: MacroRelease; board: MacroBoard | null }) {
  const { t } = useTranslation();
  const setArtifact = useSetArtifact();
  const entry = board?.series?.[r.series]?.entries?.find((e) => e.period === r.period);
  const decision = entry?.decision || null;
  const dotColor = decision ? (decision.kind === 'pass' ? 'var(--text-muted)' : 'var(--success)') : null;
  const type = familyArtifact(r.family);
  const section = r.series === 'KXFED' ? 'ladder' : r.series === 'KXFEDDECISION' ? 'decision' : undefined;
  const open = () => setArtifact({ type, title: t(`macro.${type.replace('macro_', '')}`), params: section ? { section } : undefined });
  const pred = entry?.pred?.dist ? predSummary(entry.pred.dist) : null;
  return (
    <div
      className="pair-card"
      onClick={open}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } }}
      style={{ display: 'flex', flexDirection: 'column', gap: 5, minWidth: 200, flex: '1 1 220px', cursor: 'pointer' }}
    >
      <div className="flex items-center justify-between">
        <span style={{ fontSize: 12, fontWeight: 700, ...mono, color: 'var(--text-primary)', textTransform: 'uppercase', letterSpacing: '.03em' }}>
          {dotColor && (
            <span title={decision?.kind === 'pass' ? t('macro.statusPass') : t('macro.statusOpen')}
              style={{ color: dotColor, marginRight: 6 }}>●</span>
          )}
          {r.series.replace(/^KX/, '')}
        </span>
        <span style={{ display: 'inline-block', padding: '1px 6px', fontSize: 9, fontWeight: 700, ...mono,
          letterSpacing: '.05em', textTransform: 'uppercase', border: '1px solid var(--border-subtle)',
          color: 'var(--text-muted)', borderRadius: 3 }}>
          {r.cadence.replace('_', ' ')}
        </span>
      </div>
      <div style={{ fontSize: 11, ...mono, color: 'var(--text-secondary)' }}>
        {t('macro.colPeriod')}: {r.period}
      </div>
      <div style={{ fontSize: 10, ...mono, color: 'var(--success)', fontWeight: 700 }}>
        {countdown(t, r.scheduled_ts)}
      </div>
      {pred && pred !== '—' && (
        <div style={{ fontSize: 10, ...mono, color: 'var(--text-muted)' }}>
          {t('macro.colModel')}: {pred}
        </div>
      )}
      {r.note && <div style={{ fontSize: 9.5, ...mono, color: 'var(--text-muted)' }}>{r.note}</div>}
    </div>
  );
}

export default function MacroUpcoming() {
  const { t } = useTranslation();
  const { data, loading } = useMacroPoll<MacroBoard>(() => getMacroJson('macro_board'), 60000);

  const releases = [...(data?.next_releases ?? [])]
    .sort((a, b) => String(a.scheduled_ts).localeCompare(String(b.scheduled_ts)))
    .slice(0, SLOTS);

  return (
    <div className="p-4 relative" style={{ background: 'var(--paper)', border: '3px solid var(--ink)', boxShadow: 'var(--shadow-pixel-sm)' }}>
      <div style={{ position: 'absolute', top: -2, left: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div style={{ position: 'absolute', top: -2, right: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div style={{ position: 'absolute', bottom: -2, left: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div style={{ position: 'absolute', bottom: -2, right: -2, width: 6, height: 6, background: 'var(--ink)' }} />
      <div className="flex items-center justify-between mb-3">
        <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.14em', textTransform: 'uppercase', color: 'var(--text-primary)', ...mono }}>
          {t('macro.upcomingTitle')} <span style={{ color: 'var(--success)' }}>({releases.length})</span>
        </div>
        <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono }}>Fed · CPI · Jobs · Kalshi</div>
      </div>
      {loading && !releases.length ? (
        <div className="text-xs py-2" style={{ color: 'var(--text-muted)' }}>{t('macro.loading')}</div>
      ) : !releases.length ? (
        <div className="text-xs py-2" style={{ color: 'var(--text-muted)' }}>{t('macro.noUpcoming')}</div>
      ) : (
        <div className="flex flex-wrap gap-2">
          {releases.map((r) => (
            <span key={r.series + r.period + r.scheduled_ts} style={{ display: 'contents' }}>
              <ReleaseCard r={r} board={data} />
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
