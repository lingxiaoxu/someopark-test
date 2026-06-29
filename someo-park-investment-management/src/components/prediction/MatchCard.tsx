/**
 * MatchCard — one upcoming World Cup match in Prediction Market mode.
 * Clarity-first: spells out the outcomes with team names ("Argentina win / Draw /
 * Algeria win") instead of H/D/A, and shows three labelled rows — OUR PREDICTION,
 * Kalshi price, Polymarket US price — so it's obvious what we predict vs what the
 * venues currently quote. Collapsed by default (headline pick + kickoff); click to
 * expand the full breakdown. Reads ops/upcoming_export.py output.
 *
 * Knockout matches carry a parallel 2-way "advance" block (m.advance). When the shared
 * AdvanceMode is 'advance' AND this is a knockout tie, the WHOLE card (model / quotes /
 * edge / decision) renders from that 2-way block (home/away, no draw). Group matches
 * have no advance block, so they always render the 3-way regulation view (auto-lock).
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { tCountry } from '../../i18n/countries';
import { useAdvanceMode } from './AdvanceMode';

type ThreeWay = { home: number; draw: number; away: number };
type Q = { ask: number | null; bid: number | null; ask_c?: number | null; bid_c?: number | null; mid_c?: number | null };
type VenueQuote = { home?: Q; draw?: Q; away?: Q; devig?: ThreeWay | { home: number; away: number } | null } | null;
type Decision = {
  bet: boolean; side?: string | null; venue?: string | null;
  price_cents?: number | null; model_prob?: number | null; net_edge?: number | null;
  stake_usd?: number; capped_notional_usd?: number; confidence_k?: number | null;
} | null;

// 2-way "who advances" block (knockout only) — same fields as the 3-way set, two sides.
type AdvanceBlock = {
  model: { home: number; away: number; cents?: { home: number; away: number } } | null;
  kalshi?: VenueQuote;
  poly_us?: VenueQuote;
  edge?: { best?: { side: string; venue: string; net_edge: number; tradable: boolean } | null };
  decision?: Decision;
} | null;

export type UpcomingMatch = {
  kickoff: string;
  et?: string | null;
  round?: string;
  home: { id: string | null; name: string; zh?: string };
  away: { id: string | null; name: string; zh?: string };
  model: (ThreeWay & { over_2_5?: number; btts?: number; cents?: ThreeWay; p_home_advance?: number }) | null;
  knockout?: boolean;   // knockout tie: also carries the 2-way advance market
  form?: { home?: number | null; away?: number | null } | null;
  book_devig?: ThreeWay | null;
  kalshi?: VenueQuote;
  poly_us?: VenueQuote;
  edge?: { best?: { side: string; venue: string; net_edge: number; tradable: boolean } | null };
  decision?: Decision;
  advance?: AdvanceBlock;   // 2-way knockout product (null for group / undecided ties)
  tentative?: boolean;   // knockout tie whose teams aren't decided yet (placeholder pairing)
};

const pct = (v?: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`);
const px = (v?: number | null) => (v == null ? '—' : v.toFixed(2));
const cents = (v?: number | null) => (v == null ? '—' : `${Math.round(v)}¢`);

// A venue's asks carry a vig, so they sum to MORE than 100¢ — the contract price is NOT
// the probability. This line makes that explicit: the overround (sum of the asks) and the
// de-vigged implied probability. Works for the 3-way (home/draw/away) and the 2-way
// advance book (home/away, no draw).
function VigNote({ q, twoWay }: { q: VenueQuote; twoWay?: boolean }) {
  if (!q) return null;
  const a = twoWay ? [q.home?.ask, q.away?.ask] : [q.home?.ask, q.draw?.ask, q.away?.ask];
  if (a.some((x) => x == null)) return null;
  const sum = (a as number[]).reduce((s, x) => s + x, 0);
  const dv = q.devig as any;
  const dvStr = dv
    ? (twoWay
        ? ` · de-vig ${Math.round(dv.home * 100)}/${Math.round(dv.away * 100)}%`
        : ` · de-vig ${Math.round(dv.home * 100)}/${Math.round(dv.draw * 100)}/${Math.round(dv.away * 100)}%`)
    : '';
  return (
    <div style={{ fontSize: 9, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginTop: 1, marginLeft: 2 }}>
      ↳ {Math.round(sum * 100)}¢ (vig {((sum - 1) * 100).toFixed(1)}%){dvStr}
    </div>
  );
}

export default function MatchCard({ m }: { m: UpcomingMatch }) {
  const { t, i18n } = useTranslation();
  const { mode } = useAdvanceMode();
  const [open, setOpen] = useState(false);
  const home = tCountry(m.home.name), away = tCountry(m.away.name);

  // Resolve the active "lens": the 2-way advance product only when the user picked it AND
  // this is a knockout tie that actually has an advance block (else regulation auto-locks).
  const adv = m.advance || null;
  const twoWay = mode === 'advance' && !!m.knockout && !!(adv && adv.model);

  // Outcome labels for the active lens.
  const winH = twoWay ? t('prediction.advancesLabel', { team: home }) : `${home} ${t('prediction.win')}`;
  const winA = twoWay ? t('prediction.advancesLabel', { team: away }) : `${away} ${t('prediction.win')}`;
  const drawL = t('prediction.drawResult');

  // View-model: pick the 3-way or 2-way source for every field the card renders.
  const vModel: any = twoWay ? adv!.model : m.model;
  const vKalshi = twoWay ? adv!.kalshi : m.kalshi;
  const vPoly = twoWay ? adv!.poly_us : m.poly_us;
  const best = (twoWay ? adv!.edge?.best : m.edge?.best) || null;
  const dec = (twoWay ? adv!.decision : m.decision) || null;

  // Single source of truth for "are we betting this match" + the edge to show: the
  // decision model (dec). Falls back to the edge finder when no decision.
  const betting = dec ? !!dec.bet : !!(best?.tradable && best.net_edge > 0);
  const edgeView = (betting && dec && dec.side)
    ? { side: dec.side, venue: dec.venue ?? '', net_edge: dec.net_edge ?? 0 }
    : best ? { side: best.side, venue: best.venue, net_edge: best.net_edge } : null;
  const edgeColor = betting ? 'var(--success)' : 'var(--text-muted)';
  const Chevron = open ? ChevronDown : ChevronRight;

  // Knockout tie whose teams aren't decided yet: placeholder bracket pairing, no prediction.
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

  // headline = the model's most likely outcome under the active lens, spelled out.
  const sides: [string, number][] = twoWay
    ? [[winH, vModel.home], [winA, vModel.away]]
    : [[winH, vModel.home], [drawL, vModel.draw], [winA, vModel.away]];
  const top = sides.reduce((a, b) => (b[1] > a[1] ? b : a));

  const sideLabelMap: Record<string, string> = twoWay
    ? { home: winH, away: winA }
    : { home: winH, draw: drawL, away: winA };
  const venueLabelMap: Record<string, string> = { kalshi: 'Kalshi', poly_us: 'Polymarket US', poly: 'Polymarket US' };
  const lang = i18n.language || '';
  const sep = (lang.startsWith('zh') || lang.startsWith('ja')) ? '' : ' ';
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
    if (dec && !dec.bet) return t('prediction.adviceHold') + sep + inplay;
    let side: 'home' | 'draw' | 'away' | null = null;
    let venue = '', c2: number | string = '—', model: number | string = '—', edge: string = '—', stakeClause = '';
    if (dec && dec.bet && dec.side) {
      side = dec.side as 'home' | 'draw' | 'away';
      venue = dec.venue ?? '';
      c2 = dec.price_cents != null ? Math.round(dec.price_cents) : '—';
      model = dec.model_prob != null ? Math.round(dec.model_prob * 100) : '—';
      edge = dec.net_edge != null ? (dec.net_edge * 100).toFixed(1) : '—';
      if (dec.stake_usd != null) stakeClause = t('prediction.adviceStakeClause', { stake: dec.stake_usd.toFixed(2) });
    } else if (best && best.tradable && best.net_edge > 0) {
      side = best.side as 'home' | 'draw' | 'away';
      venue = best.venue;
      const q = best.venue === 'kalshi' ? vKalshi : vPoly;
      const c = (q as any)?.[side]?.ask_c ?? (q as any)?.[side]?.mid_c ?? null;
      c2 = c != null ? Math.round(c) : '—';
      const mp = (vModel as any)[side];
      model = mp != null ? Math.round(mp * 100) : '—';
      edge = (best.net_edge * 100).toFixed(1);
    } else {
      return t('prediction.adviceHold') + sep + inplay;
    }
    return t('prediction.adviceBuy', {
      venue: venueLabelMap[venue] ?? venue,
      side: sideLabelMap[side] ?? side,
      cents: c2, model, edge, form: formClause(side),
    }) + (stakeClause ? sep + stakeClause : '') + sep
      + (twoWay ? t('prediction.adviceSettleAdvance') : t('prediction.adviceCashout'));
  };

  // one labelled row (probabilities or venue prices), team names spelled out. When
  // twoWay, the draw column is omitted (home / away only).
  const Line = ({ label, h, d, a, fmt, hc, dc, ac }: { label: string; h?: number | null; d?: number | null; a?: number | null; fmt: (v?: number | null) => string; hc?: number | null; dc?: number | null; ac?: number | null }) => {
    const withC = (v: number | null | undefined, c: number | null | undefined) => (c == null ? fmt(v) : `${fmt(v)} (${cents(c)})`);
    return (
      <div style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)', marginTop: 3, lineHeight: 1.5 }}>
        <span style={{ color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '.04em' }}>{label}</span><br />
        {winH} <b>{withC(h, hc)}</b>{twoWay ? '' : <> · {drawL} <b>{withC(d, dc)}</b></>} · {winA} <b>{withC(a, ac)}</b>
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
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
            {twoWay ? <span style={{ color: 'var(--accent-primary)', marginRight: 6 }}>{t('prediction.modeAdvance')}</span> : null}{m.et || ''}
          </span>
        </div>
        {!open && (
          <div className="flex items-center justify-between" style={{ marginTop: 4 }}>
            <span style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}>
              {t('prediction.ourPrediction')}: <b style={{ color: 'var(--text-primary)' }}>{top[0]} {pct(top[1])}</b>
            </span>
            {edgeView && (
              <span style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: edgeColor, fontWeight: 700 }}>
                {edgeView.net_edge >= 0 ? '+' : ''}{(edgeView.net_edge * 100).toFixed(1)}%{betting ? ' ★' : ''}
              </span>
            )}
          </div>
        )}
      </button>

      {open && (
        <>
          <div style={{ marginTop: 4, padding: '6px 8px', border: `1px solid ${betting ? 'var(--success)' : 'var(--border-subtle)'}`, background: 'var(--bg-tertiary)', fontSize: 11, fontFamily: 'var(--font-mono)', lineHeight: 1.5 }}>
            <span style={{ color: betting ? 'var(--success)' : 'var(--text-muted)', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.04em' }}>{t('prediction.adviceLabel')}</span>
            <span style={{ color: 'var(--text-primary)' }}> · {buildAdvice()}</span>
          </div>
          {dec && dec.bet && dec.side && (
            <div style={{ marginTop: 4, fontSize: 10, fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)', lineHeight: 1.5, display: 'flex', flexDirection: 'column', gap: 1 }}>
              <div>{t('prediction.planEntry')}: <b style={{ color: 'var(--text-primary)' }}>{sideLabelMap[dec.side] ?? dec.side}</b> @ {dec.price_cents != null ? Math.round(dec.price_cents) : '—'}¢{dec.stake_usd != null ? <> · ${dec.stake_usd.toFixed(2)}</> : null}</div>
              <div>{t('prediction.planExit')}: {twoWay ? t('prediction.planExitAdvance') : t('prediction.planExitDesc')}</div>
              <div style={{ color: 'var(--text-muted)' }}>{t('prediction.planWhy', { edge: dec.net_edge != null ? (dec.net_edge * 100).toFixed(1) : '—' })}</div>
            </div>
          )}
          <Line label={t('prediction.ourPrediction')} h={vModel.home} d={twoWay ? null : vModel.draw} a={vModel.away} fmt={pct}
            hc={vModel.cents?.home} dc={twoWay ? null : vModel.cents?.draw} ac={vModel.cents?.away} />
          {!twoWay && (m.model.over_2_5 != null || m.model.btts != null) && (
            <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
              O2.5 {pct(m.model.over_2_5)} · BTTS {pct(m.model.btts)}
            </div>
          )}
          {vKalshi
            ? <><Line label={t('prediction.kalshiPrice')} h={vKalshi.home?.ask} d={twoWay ? null : vKalshi.draw?.ask} a={vKalshi.away?.ask} fmt={px}
                hc={vKalshi.home?.ask_c} dc={twoWay ? null : vKalshi.draw?.ask_c} ac={vKalshi.away?.ask_c} /><VigNote q={vKalshi} twoWay={twoWay} /></>
            : <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginTop: 3 }}>{t('prediction.kalshiPrice')}: {t('prediction.notListed')}</div>}
          {vPoly
            ? <><Line label={t('prediction.polyPrice')} h={vPoly.home?.ask} d={twoWay ? null : vPoly.draw?.ask} a={vPoly.away?.ask} fmt={px}
                hc={vPoly.home?.ask_c} dc={twoWay ? null : vPoly.draw?.ask_c} ac={vPoly.away?.ask_c} /><VigNote q={vPoly} twoWay={twoWay} /></>
            : <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginTop: 3 }}>{t('prediction.polyPrice')}: {t('prediction.notListed')}</div>}
          {edgeView && (
            <div style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: edgeColor, fontWeight: 700, marginTop: 4 }}>
              {t('prediction.colEdge')}: {edgeView.venue}/{edgeView.side} {edgeView.net_edge >= 0 ? '+' : ''}{(edgeView.net_edge * 100).toFixed(1)}%{betting ? ' ★' : ''}
            </div>
          )}
        </>
      )}
    </div>
  );
}
