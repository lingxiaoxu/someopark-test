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
  model: (ThreeWay & { over_2_5?: number; btts?: number; cents?: ThreeWay; p_home_advance?: number }) | null;
  knockout?: boolean;   // knockout tie: NO draw — bet/headline settle on who ADVANCES
  form?: { home?: number | null; away?: number | null } | null;
  book_devig?: ThreeWay | null;
  kalshi?: VenueQuote;
  poly_us?: VenueQuote;
  edge?: { best?: { side: string; venue: string; net_edge: number; tradable: boolean } | null };
  decision?: {
    bet: boolean; side?: string | null; venue?: string | null;
    price_cents?: number | null; model_prob?: number | null; net_edge?: number | null;
    stake_usd?: number; capped_notional_usd?: number; confidence_k?: number | null;
  } | null;
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
  const { t, i18n } = useTranslation();
  const [open, setOpen] = useState(false);
  const home = tCountry(m.home.name), away = tCountry(m.away.name);
  // Per-match market settles on the 90-min 3-way for both stages — a draw is valid even
  // in knockout (the separate "advance/reach-round" product is team-level, elsewhere).
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

  // headline = the model's most likely 90-min outcome, spelled out (draw included).
  const sides: [string, number][] = [[winH, m.model.home], [drawL, m.model.draw], [winA, m.model.away]];
  const top = sides.reduce((a, b) => (b[1] > a[1] ? b : a));

  // Plain-language "how to trade" line, generated from the edge data so a first-time
  // user knows what to do at a glance — what to buy, why, and where to watch in-play —
  // instead of decoding "edge: kalshi/draw +8.7%". Tradable+positive → a concrete BUY;
  // otherwise → sit out. Either way, point to the in-play view once the match starts.
  const sideLabelMap: Record<string, string> = { home: winH, draw: drawL, away: winA };
  const venueLabelMap: Record<string, string> = { kalshi: 'Kalshi', poly_us: 'Polymarket US', poly: 'Polymarket US' };
  // CJK languages don't put a space between full sentences; Latin ones do.
  const lang = i18n.language || '';
  const sep = (lang.startsWith('zh') || lang.startsWith('ja')) ? '' : ' ';
  // Recent-form clause woven into the "why", picked by which side we recommend:
  //   home → home team's form, away → away team's form, draw → both teams' closeness.
  // Strong form reinforces the pick; weak form is flagged as a risk; near-neutral is
  // omitted so the line stays clean. z-index is the same form feature the model uses.
  const STRONG_Z = 0.5, WEAK_Z = -0.5;
  const formClause = (side: 'home' | 'draw' | 'away'): string => {
    const f = m.form;
    if (!f) return '';
    if (side === 'draw') {
      const zh = f.home, za = f.away;
      if (zh != null && za != null && Math.abs(zh - za) <= 0.4) return t('prediction.formDrawClose');
      return '';
    }
    const z = side === 'home' ? f.home : f.away;
    const team = side === 'home' ? home : away;
    if (z == null) return '';
    if (z >= STRONG_Z) return t('prediction.formStrong', { team });
    if (z <= WEAK_Z) return t('prediction.formWeak', { team });
    return '';
  };
  const buildAdvice = (): string => {
    const inplay = t('prediction.adviceInplay');
    const dec = m.decision;
    // The production decision model (decide()) takes precedence — same brain as the bet
    // log + match_signals: it picks the value side, sizes the stake, and says "no bet"
    // when there's no safe edge (incl. the longshot bar / discipline gate).
    if (dec && !dec.bet) return t('prediction.adviceHold') + sep + inplay;
    let side: 'home' | 'draw' | 'away' | null = null;
    let venue = '', cents: number | string = '—', model: number | string = '—', edge: string = '—', stakeClause = '';
    if (dec && dec.bet && dec.side) {
      side = dec.side as 'home' | 'draw' | 'away';
      venue = dec.venue ?? '';
      cents = dec.price_cents != null ? Math.round(dec.price_cents) : '—';
      model = dec.model_prob != null ? Math.round(dec.model_prob * 100) : '—';
      edge = dec.net_edge != null ? (dec.net_edge * 100).toFixed(1) : '—';
      if (dec.stake_usd != null) stakeClause = t('prediction.adviceStakeClause', { stake: dec.stake_usd.toFixed(2) });
    } else if (best && best.tradable && best.net_edge > 0) {
      side = best.side as 'home' | 'draw' | 'away';
      venue = best.venue;
      const q = best.venue === 'kalshi' ? m.kalshi : m.poly_us;
      const c = q?.[side]?.ask_c ?? q?.[side]?.mid_c ?? null;
      cents = c != null ? Math.round(c) : '—';
      const mp = (m.model as ThreeWay)[side];
      model = mp != null ? Math.round(mp * 100) : '—';
      edge = (best.net_edge * 100).toFixed(1);
    } else {
      return t('prediction.adviceHold') + sep + inplay;
    }
    return t('prediction.adviceBuy', {
      venue: venueLabelMap[venue] ?? venue,
      side: sideLabelMap[side] ?? side,
      cents, model, edge, form: formClause(side),
    }) + (stakeClause ? sep + stakeClause : '') + sep + inplay;
  };

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
          <div style={{ marginTop: 4, padding: '6px 8px', border: `1px solid ${best?.tradable ? 'var(--success)' : 'var(--border-subtle)'}`, background: 'var(--bg-tertiary)', fontSize: 11, fontFamily: 'var(--font-mono)', lineHeight: 1.5 }}>
            <span style={{ color: best?.tradable ? 'var(--success)' : 'var(--text-muted)', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.04em' }}>{t('prediction.adviceLabel')}</span>
            <span style={{ color: 'var(--text-primary)' }}> · {buildAdvice()}</span>
          </div>
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
