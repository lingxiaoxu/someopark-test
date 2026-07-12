import React, { useState, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { ExternalLink } from 'lucide-react';

import { useSetArtifact } from '../../contexts/ArtifactContext';
import { usePredictionFocus } from '../../contexts/PredictionFocusContext';
import { tCountry, countryKey } from '../../i18n/countries';
import { usePredictionIndex, usePredictionMeta } from '../../lib/countryIndex';
import { PREDICTION_ITEMS } from './PredictionArtifactGrid';

const ITEM_BY_TYPE = Object.fromEntries(PREDICTION_ITEMS.map((i) => [i.type, i]));
// Stable display order = the grid order.
const ORDER = PREDICTION_ITEMS.map((i) => i.type);

/**
 * A clickable country name. Renders like plain `tCountry(code)` text but:
 *   1. is the scroll/highlight ANCHOR for cross-artifact focus (`data-country`), and
 *   2. on click opens a small popover listing every OTHER prediction artifact the
 *      country appears in — each a direct link that navigates there and scroll-focuses
 *      the country. Mirrors the stock PairBadge popover.
 *
 * Drop-in for `tCountry(x)` at country render sites: `<CountryName code={x} />`.
 */
export default function CountryName({ code, bold }: { code?: string | null; bold?: boolean }) {
  const { t } = useTranslation();
  const key = countryKey(code);
  const setActiveArtifact = useSetArtifact();
  const { selfType } = usePredictionFocus();
  const index = usePredictionIndex();
  const meta = usePredictionMeta();

  const [open, setOpen] = useState(false);
  const [style, setStyle] = useState<React.CSSProperties>({});
  const anchorRef = useRef<HTMLSpanElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (anchorRef.current?.contains(e.target as Node)) return;
      if (popRef.current?.contains(e.target as Node)) return;
      setOpen(false);
    };
    const onLeave = () => setOpen(false);
    document.addEventListener('mousedown', onDown);
    window.addEventListener('scroll', onLeave, true);
    window.addEventListener('resize', onLeave);
    return () => {
      document.removeEventListener('mousedown', onDown);
      window.removeEventListener('scroll', onLeave, true);
      window.removeEventListener('resize', onLeave);
    };
  }, [open]);

  if (!key) return <>{tCountry(code)}</>;

  const targets = index
    ? ORDER.filter((tp) => tp !== selfType && index.get(key)?.has(tp))
    : [];

  const onClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!open && anchorRef.current) {
      const r = anchorRef.current.getBoundingClientRect();
      const alignRight = window.innerWidth - r.left < 260;
      setStyle({
        position: 'fixed', top: r.bottom + 4,
        left: alignRight ? Math.max(8, r.right - 240) : r.left,
        zIndex: 9999, width: 240,
      });
    }
    setOpen((v) => !v);
  };

  const go = (type: string) => {
    const item = ITEM_BY_TYPE[type];
    setActiveArtifact({
      type,
      title: item ? t(`prediction.${item.i18nKey}`) : type,
      params: { focusCountry: key, focusNonce: Date.now() },
    });
    setOpen(false);
  };

  // Header extras: FIFA seeding + tournament progress (localised, colour-coded).
  const m = meta?.get(key) ?? null;
  const fifaText = m?.fifaRank != null ? `FIFA #${m.fifaRank}` : '';
  const roundLbl = (r: string) => t('prediction.wcRound.' + r, { defaultValue: r });
  let statusText = '';
  let statusColor = '#c9c9d0';
  const s = m?.status;
  if (s?.kind === 'reached') { statusText = t('prediction.wcStatus.reached', { round: roundLbl(s.round) }); statusColor = '#19e08a'; }
  else if (s?.kind === 'out') { statusText = t('prediction.wcStatus.out', { round: roundLbl(s.round) }); statusColor = '#ff5a5a'; }
  else if (s?.kind === 'outGroup') { statusText = t('prediction.wcStatus.outGroup'); statusColor = '#ff5a5a'; }
  else if (s?.kind === 'group') { statusText = t('prediction.wcStatus.group'); statusColor = '#c9c9d0'; }

  return (
    <span ref={anchorRef} data-country={key} style={{ display: 'inline-flex', alignItems: 'center' }}>
      <button
        onClick={onClick}
        title={t('prediction.countryNavHint', { defaultValue: '' })}
        style={{
          font: 'inherit', color: 'inherit', background: 'none', border: 'none', padding: 0,
          cursor: 'pointer', textDecoration: 'underline', textDecorationStyle: 'dotted',
          textUnderlineOffset: 2, textDecorationColor: 'var(--text-muted)',
          fontWeight: bold ? 700 : undefined,
        }}
      >
        {tCountry(code)}
      </button>

      {open && createPortal(
        // Explicit dark palette (NOT theme vars): this popover portals to document.body,
        // where CSS-var inheritance from [data-mode="prediction"] is unreliable and left the
        // list text washed out. CountryName only ever renders inside prediction (dark)
        // artifacts, so a fixed high-contrast dark card is always correct.
        <div
          ref={popRef}
          className="overflow-hidden"
          style={{ ...style, background: '#17171a', border: '1px solid #3a3a40', borderRadius: 8, boxShadow: '0 8px 24px rgba(0,0,0,0.45)' }}
        >
          <div style={{ padding: '8px 12px', background: '#0c0c0e', borderBottom: '1px solid #3a3a40' }}>
            <div className="flex items-baseline gap-2">
              <span style={{ fontSize: 13, fontWeight: 700, color: '#ffffff' }}>{tCountry(code)}</span>
              {fifaText && <span style={{ fontSize: 10, fontWeight: 600, color: '#c9c9d0' }}>{fifaText}</span>}
            </div>
            {statusText && <div style={{ fontSize: 11, fontWeight: 700, color: statusColor, marginTop: 1 }}>{statusText}</div>}
          </div>
          <div style={{ padding: '6px 12px 2px', fontSize: 9, letterSpacing: '.06em', textTransform: 'uppercase', color: '#9a9aa2' }}>
            {t('prediction.countryAppearsIn')}
          </div>
          <div className="max-h-[300px] overflow-y-auto" style={{ paddingBottom: 4 }}>
            {!index ? (
              <div style={{ padding: '6px 12px', fontSize: 11, color: '#c9c9d0' }}>…</div>
            ) : targets.length === 0 ? (
              <div style={{ padding: '6px 12px', fontSize: 11, color: '#c9c9d0' }}>{t('prediction.countryNoOther')}</div>
            ) : targets.map((type) => {
              const item = ITEM_BY_TYPE[type];
              const Icon = item?.Icon ?? ExternalLink;
              return (
                <button
                  key={type}
                  onClick={() => go(type)}
                  className="w-full flex items-center gap-2 text-left"
                  style={{ padding: '6px 12px', fontSize: 12, fontWeight: 600, color: '#f2f2f4', background: 'transparent', border: 'none', cursor: 'pointer', transition: 'background .1s' }}
                  onMouseEnter={(e) => { e.currentTarget.style.background = '#26262c'; }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
                >
                  <Icon className="w-3 h-3" style={{ color: '#c2c2c9', opacity: 1 }} />
                  <span>{item ? t(`prediction.${item.i18nKey}`) : type}</span>
                </button>
              );
            })}
          </div>
        </div>,
        document.body,
      )}
    </span>
  );
}
