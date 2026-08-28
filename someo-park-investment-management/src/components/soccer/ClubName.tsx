import { useEffect, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent } from 'react';
import { createPortal } from 'react-dom';
import { ExternalLink } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { useSetArtifact } from '../../contexts/ArtifactContext';
import { useSoccerFocus } from '../../contexts/SoccerFocusContext';
import { useSoccerIndex, useSoccerMeta, type ClubStatus } from '../../lib/clubIndex';
import { SOCCER_ITEMS } from './SoccerArtifactGrid';
import { clubName, leagueLabel } from './soccerLabels';

const ORDER = SOCCER_ITEMS.map((i) => i.type);
const ITEM_BY_TYPE = Object.fromEntries(SOCCER_ITEMS.map((i) => [i.type, i]));

/** The club's season in one line, phrased for ITS OWN competition format. */
function statusLine(
  s: ClubStatus | null | undefined,
  t: (k: string, o?: any) => string,
): { text: string; color: string } | null {
  if (!s) return null;
  const pct = (p: number) => `${Math.round(p * 100)}%`;
  switch (s.kind) {
    case 'title':          return { text: t('soccer.seasonStatus.title', { p: pct(s.p) }), color: '#19e08a' };
    case 'europe':         return { text: t('soccer.seasonStatus.europe', { p: pct(s.p) }), color: '#4ea8ff' };
    case 'mid':            return { text: t('soccer.seasonStatus.mid'), color: '#c9c9d0' };
    case 'relegationFight':return { text: t('soccer.seasonStatus.relegationFight', { p: pct(s.p) }), color: '#ffb020' };
    case 'relegated':      return { text: t('soccer.seasonStatus.relegated'), color: '#ff5a5a' };
    case 'qualDirect':     return { text: t('soccer.seasonStatus.qualDirect', { p: pct(s.p) }), color: '#19e08a' };
    case 'qualPlayoff':    return { text: t('soccer.seasonStatus.qualPlayoff', { p: pct(s.p) }), color: '#4ea8ff' };
    case 'pendingDraw':    return { text: t('soccer.seasonStatus.pendingDraw'), color: '#c9c9d0' };
    case 'cupAlive':       return { text: t('soccer.seasonStatus.cupAlive', { p: pct(s.p) }), color: '#19e08a' };
    case 'cupOut':         return { text: t('soccer.seasonStatus.cupOut'), color: '#ff5a5a' };
    default:               return null;
  }
}

/**
 * A clickable club name — the soccer mirror of CountryName (附录 C-31). It:
 *   1. is the scroll/highlight ANCHOR for cross-artifact focus (`data-club`), and
 *   2. on click opens a popover listing every OTHER soccer artifact this club appears
 *      in, each a link that navigates there and scroll-focuses the club.
 *
 * The header carries what a CLUB is identified by — its Elo rank, its competition, and
 * where its season stands — rather than the World Cup's single round ladder, which has
 * no meaning for a league club.
 *
 * Drop-in for `clubName(c, lang, t)`: `<ClubName club={c} />`.
 */
export default function ClubName({
  club, bold,
}: { club?: { id?: string; club_id?: string; team_id?: string; name?: string; zh?: string } | null; bold?: boolean }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  const setActiveArtifact = useSetArtifact();
  const { selfType } = useSoccerFocus();
  const index = useSoccerIndex();
  const meta = useSoccerMeta();

  const [open, setOpen] = useState(false);
  const [style, setStyle] = useState<CSSProperties>({});
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
    // Close on PAGE scroll (the fixed position would go stale) — but not on a scroll
    // inside the popover's own list, or its contents could never be scrolled.
    const onScroll = (e: Event) => {
      if (popRef.current && e.target instanceof Node && popRef.current.contains(e.target)) return;
      setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    window.addEventListener('scroll', onScroll, true);
    window.addEventListener('resize', onLeave);
    return () => {
      document.removeEventListener('mousedown', onDown);
      window.removeEventListener('scroll', onScroll, true);
      window.removeEventListener('resize', onLeave);
    };
  }, [open]);

  const label = clubName(club, lang, t);
  const id = club?.club_id || club?.team_id || club?.id || '';
  // No canonical id → plain text. A popover keyed on a name we could not resolve would
  // link to the wrong club, which is worse than not offering the link.
  if (!id) return <>{label}</>;

  const targets = index ? ORDER.filter((tp) => tp !== selfType && index.get(id)?.has(tp)) : [];

  const onClick = (e: ReactMouseEvent) => {
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
      title: item ? t(`soccer.${item.i18nKey}`) : type,
      params: { focusClub: id, focusNonce: Date.now() },
    });
    setOpen(false);
  };

  const m = meta?.get(id) ?? null;
  const eloText = m?.eloRank != null ? `Elo #${m.eloRank}` : '';
  const compText = m?.league ? leagueLabel({ league: m.league }, lang, t) : '';
  const st = statusLine(m?.status, t);

  return (
    <span ref={anchorRef} data-club={id} style={{ display: 'inline-flex', alignItems: 'center' }}>
      <button
        onClick={onClick}
        title={t('soccer.clubNavHint', { defaultValue: '' })}
        style={{
          font: 'inherit', color: 'inherit', background: 'none', border: 'none', padding: 0,
          cursor: 'pointer', textDecoration: 'underline', textDecorationStyle: 'dotted',
          textUnderlineOffset: 2, textDecorationColor: 'var(--text-muted)',
          fontWeight: bold ? 700 : undefined,
        }}
      >
        {label}
      </button>

      {open && createPortal(
        // Explicit dark palette (NOT theme vars), same reason as CountryName: this
        // portals to document.body where CSS-var inheritance from the artifact shell is
        // unreliable, and soccer artifacts are always rendered dark.
        <div
          ref={popRef}
          className="overflow-hidden"
          style={{ ...style, background: '#17171a', border: '1px solid #3a3a40', borderRadius: 8, boxShadow: '0 8px 24px rgba(0,0,0,0.45)' }}
        >
          <div style={{ padding: '8px 12px', background: '#0c0c0e', borderBottom: '1px solid #3a3a40' }}>
            <div className="flex items-baseline gap-2">
              <span style={{ fontSize: 13, fontWeight: 700, color: '#ffffff' }}>{label}</span>
              {eloText && <span style={{ fontSize: 10, fontWeight: 600, color: '#c9c9d0' }}>{eloText}</span>}
            </div>
            {compText && <div style={{ fontSize: 10, color: '#9a9aa2', marginTop: 1 }}>{compText}</div>}
            {st && <div style={{ fontSize: 11, fontWeight: 700, color: st.color, marginTop: 1 }}>{st.text}</div>}
          </div>
          <div style={{ padding: '6px 12px 2px', fontSize: 9, letterSpacing: '.06em', textTransform: 'uppercase', color: '#9a9aa2' }}>
            {t('soccer.clubAppearsIn')}
          </div>
          <div className="max-h-[300px] overflow-y-auto" style={{ paddingBottom: 4 }}>
            {!index ? (
              <div style={{ padding: '6px 12px', fontSize: 11, color: '#c9c9d0' }}>…</div>
            ) : targets.length === 0 ? (
              <div style={{ padding: '6px 12px', fontSize: 11, color: '#c9c9d0' }}>{t('soccer.clubNoOther')}</div>
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
                  <span>{item ? t(`soccer.${item.i18nKey}`) : type}</span>
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
