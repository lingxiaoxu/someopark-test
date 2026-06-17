/**
 * MatchCard — one upcoming World Cup match in Prediction Market mode.
 * Clarity-first: spells out the outcomes with team names ("Argentina win / Draw /
 * Algeria win") instead of H/D/A, and shows three labelled rows — OUR PREDICTION,
 * Kalshi price, Polymarket US price — so it's obvious what we predict vs what the
 * venues currently quote. Collapsed by default (headline pick + kickoff); click to
 * expand the full breakdown. Reads ops/upcoming_export.py output.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { tCountry } from '../../i18n/countries';

type ThreeWay = { home: number; draw: number; away: number };
type Q = { ask: number | null; bid: number | null; ask_c?: number | null; bid_c?: number | null; mid_c?: number | null };
type VenueQuote = { home?: Q; draw?: Q; away?: Q; devig?: ThreeWay | null } | null;

export type UpcomingMatch = {
  kickoff: string;
  et?: string | null;
  round?: string;
  home: { id: string | null; name: string; zh?: string };
  away: { id: string | null; name: string; zh?: string };
  model: (ThreeWay & { over_2_5?: number; btts?: number; cents?: ThreeWay }) | null;
  book_devig?: ThreeWay | null;
  kalshi?: VenueQuote;
  poly_us?: VenueQuote;
  edge?: { best?: { side: string; venue: string; net_edge: number; tradable: boolean } | null };
  tentative?: boolean;   // knockout tie whose teams aren't decided yet (placeholder pairing)
};

const pct = (v?: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`);
const px = (v?: number | null) => (v == null ? '—' : v.toFixed(2));
const cents = (v?: number | null) => (v == null ? '—' : `${Math.round(v)}¢`);

// A venue's three asks carry a vig, so they sum to MORE than 100¢ — the contract
// price is NOT the probability. This line makes that explicit: the overround (sum of
// the three asks) and the de-vigged implied probability (what the venue really thinks).
function VigNote({ q }: { q: { home?: { ask: number | null }; draw?: { ask: number | null }; away?: { ask: number | null }; devig?: { home: number; draw: number; away: number } | null } | null | undefined }) {
  if (!q) return null;
  const a = [q.home?.ask, q.draw?.ask, q.away?.ask];
  if (a.some((x) => x == null)) return null;
  const sum = (a as number[]).reduce((s, x) => s + x, 0);
  const dv = q.devig;
  return (
    <div style={{ fontSize: 9, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginTop: 1, marginLeft: 2 }}>
      ↳ {Math.round(sum * 100)}¢ (vig {((sum - 1) * 100).toFixed(1)}%)
      {dv ? ` · de-vig ${Math.round(dv.home * 100)}/${Math.round(dv.draw * 100)}/${Math.round(dv.away * 100)}%` : ''}
    </div>
  );
}

export default function MatchCard({ m }: { m: UpcomingMatch }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const home = tCountry(m.home.name), away = tCountry(m.away.name);
  const winH = `${home} ${t('prediction.win')}`, winA = `${away} ${t('prediction.win')}`, drawL = t('prediction.drawResult');
  const best = m.edge?.best;
  const edgeColor = best?.tradable ? 'var(--success)' : 'var(--text-muted)';
  const Chevron = open ? ChevronDown : ChevronRight;

  // Knockout tie whose teams aren't decided yet: show the placeholder bracket pairing
  // (e.g. "Winner Group A vs Runner-up Group B") with a TBD note, no prediction. It
  // upgrades to the real countries + model automatically once the teams are known.
  if (m.tentative || !m.model) {
    return (
      <div className="pair-card" style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 300, flex: '1 1 360px' }}>
        <div className="flex items-center justify-between">
          <span style={{ fontSize: 13, fontWeight: 700, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)', textTransform: 'uppercase', letterSpacing: '.03em' }}>
            {home} <span style={{ color: 'var(--text-muted)' }}>vs</span> {away}
          </span>
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{m.et || ''}</span>
        </div>
        <div style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', marginTop: 2 }}>
          {m.round ? m.round + ' · ' : ''}{t('prediction.tbdPairing')}
        </div>
      </div>
    );
  }

  // headline = the model's most likely outcome, spelled out
  const sides: [string, number][] = [[winH, m.model.home], [drawL, m.model.draw], [winA, m.model.away]];
  const top = sides.reduce((a, b) => (b[1] > a[1] ? b : a));

  // one labelled 3-way row (probabilities or venue prices), team names spelled out.
  // hc/dc/ac (optional) render the per-contract ¢ alongside in parentheses.
  const Line = ({ label, h, d, a, fmt, hc, dc, ac }: { label: string; h?: number | null; d?: number | null; a?: number | null; fmt: (v?: number | null) => string; hc?: number | null; dc?: number | null; ac?: number | null }) => {
    const withC = (v: number | null | undefined, c: number | null | undefined) => (c == null ? fmt(v) : `${fmt(v)} (${cents(c)})`);
    return (
      <div style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)', marginTop: 3, lineHeight: 1.5 }}>
        <span style={{ color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '.04em' }}>{label}</span><br />
        {winH} <b>{withC(h, hc)}</b> · {drawL} <b>{withC(d, dc)}</b> · {winA} <b>{withC(a, ac)}</b>
      </div>
    );
  };

  return (
    <div className="pair-card" style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 300, flex: '1 1 360px' }}>
      <button onClick={() => setOpen(o => !o)} style={{ background: 'transparent', border: 'none', padding: 0, cursor: 'pointer', textAlign: 'left', width: '100%' }}>
        <div className="flex items-center justify-between">
          <span style={{ fontSize: 13, fontWeight: 700, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)', textTransform: 'uppercase', letterSpacing: '.03em' }}>
            <Chevron className="inline w-3.5 h-3.5" style={{ marginRight: 4, verticalAlign: '-2px', color: 'var(--text-muted)' }} />
            {home} <span style={{ color: 'var(--text-muted)' }}>vs</span> {away}
          </span>
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{m.et || ''}</span>
        </div>
        {!open && (
          <div className="flex items-center justify-between" style={{ marginTop: 4 }}>
            <span style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}>
              {t('prediction.ourPrediction')}: <b style={{ color: 'var(--text-primary)' }}>{top[0]} {pct(top[1])}</b>
            </span>
            {best && (
              <span style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: edgeColor, fontWeight: 700 }}>
                {best.net_edge >= 0 ? '+' : ''}{(best.net_edge * 100).toFixed(1)}%{best.tradable ? ' ★' : ''}
              </span>
            )}
          </div>
        )}
      </button>

      {open && (
        <>
          <Line label={t('prediction.ourPrediction')} h={m.model.home} d={m.model.draw} a={m.model.away} fmt={pct}
            hc={m.model.cents?.home} dc={m.model.cents?.draw} ac={m.model.cents?.away} />
          {(m.model.over_2_5 != null || m.model.btts != null) && (
            <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
              O2.5 {pct(m.model.over_2_5)} · BTTS {pct(m.model.btts)}
            </div>
          )}
          {m.kalshi
            ? <><Line label={t('prediction.kalshiPrice')} h={m.kalshi.home?.ask} d={m.kalshi.draw?.ask} a={m.kalshi.away?.ask} fmt={px}
                hc={m.kalshi.home?.ask_c} dc={m.kalshi.draw?.ask_c} ac={m.kalshi.away?.ask_c} /><VigNote q={m.kalshi} /></>
            : <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginTop: 3 }}>{t('prediction.kalshiPrice')}: {t('prediction.notListed')}</div>}
          {m.poly_us
            ? <><Line label={t('prediction.polyPrice')} h={m.poly_us.home?.ask} d={m.poly_us.draw?.ask} a={m.poly_us.away?.ask} fmt={px}
                hc={m.poly_us.home?.ask_c} dc={m.poly_us.draw?.ask_c} ac={m.poly_us.away?.ask_c} /><VigNote q={m.poly_us} /></>
            : <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginTop: 3 }}>{t('prediction.polyPrice')}: {t('prediction.notListed')}</div>}
          {best && (
            <div style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: edgeColor, fontWeight: 700, marginTop: 4 }}>
              {t('prediction.colEdge')}: {best.venue}/{best.side} {best.net_edge >= 0 ? '+' : ''}{(best.net_edge * 100).toFixed(1)}%{best.tradable ? ' ★' : ''}
            </div>
          )}
        </>
      )}
    </div>
  );
}
