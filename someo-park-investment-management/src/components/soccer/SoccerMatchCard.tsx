/**
 * SoccerMatchCard — one upcoming club-soccer match in Club Soccer Market mode.
 * Mirrors prediction/MatchCard.tsx's layout (collapsed headline pick → expanded
 * three-way model vs Kalshi vs Polymarket rows with ¢, de-vig note, decision line),
 * but is CAPABILITY-DRIVEN (§3.0): the 2-way "advance" lens renders ONLY when the
 * backend-computed `caps.advance` is true (never a league/round string test), and a
 * two-leg aggregate badge ("Agg 2-2") shows when caps.two_leg && caps.leg===2 &&
 * caps.agg. Reads prediction_market_soccer upcoming_export rows.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import ClubName from './ClubName';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useAdvanceMode } from '../prediction/AdvanceMode';
import { clubName, leagueLabel, stageLabel } from './soccerLabels';

// Re-exported for views that already import it from here; the definition now lives in
// soccerLabels so ClubName can use it without a cycle back through this file.
export { clubName };
import type { SoccerUpcomingMatch, SoccerVenueQuote } from '../../lib/soccerApi';

const pct = (v?: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`);
const px = (v?: number | null) => (v == null ? '—' : v.toFixed(2));
const cents = (v?: number | null) => (v == null ? '—' : `${Math.round(v)}¢`);

/** Club display name: zh when the UI language is Chinese AND the exporter provided
 * one (long-tail clubs may not have zh yet), else the API-Football English name. */

/** Kick-off shown in the reader's locale. The backend `et` string ("08-18 20:30 ET")
 *  is a US-desk convenience, not a translatable value — prefer the ISO kickoff and
 *  fall back to `et` only when there is no timestamp to format. */
function kickoffLabel(m: any): string {
  const iso = m?.kickoff;
  if (iso) {
    try {
      return new Date(iso).toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      });
    } catch { /* fall through to the backend string */ }
  }
  return m?.et || '';
}


// Overround + de-vigged implied probability line (3-way or 2-way advance book).
function VigNote({ q, twoWay }: { q: SoccerVenueQuote; twoWay?: boolean }) {
  const { t } = useTranslation();
  if (!q) return null;
  const a = twoWay ? [q.home?.ask, q.away?.ask] : [q.home?.ask, q.draw?.ask, q.away?.ask];
  if (a.some((x) => x == null)) return null;
  const sum = (a as number[]).reduce((s, x) => s + x, 0);
  const dv = q.devig as any;
  const dvStr = dv
    ? (twoWay
        ? ` · ${t('soccer.colDevig')} ${Math.round(dv.home * 100)}/${Math.round(dv.away * 100)}%`
        : ` · ${t('soccer.colDevig')} ${Math.round(dv.home * 100)}/${Math.round(dv.draw * 100)}/${Math.round(dv.away * 100)}%`)
    : '';
  return (
    <div style={{ fontSize: 9, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginTop: 1, marginLeft: 2 }}>
      ↳ {Math.round(sum * 100)}¢ ({t('soccer.vig')} {((sum - 1) * 100).toFixed(1)}%){dvStr}
    </div>
  );
}

export default function SoccerMatchCard({ m, showLeague = false }: { m: SoccerUpcomingMatch; showLeague?: boolean }) {
  const { t, i18n } = useTranslation();
  const { mode } = useAdvanceMode();
  const [open, setOpen] = useState(false);
  const lang = i18n.language || '';
  const home = clubName(m.home, lang, t), away = clubName(m.away, lang, t);

  // Capability-driven lens: the 2-way advance product ONLY when the user picked it
  // AND the backend says this tie carries an advance market (caps.advance) AND the
  // block is actually present. Pure-league matches auto-lock to regulation.
  const caps = m.caps || {};
  const adv = m.advance || null;
  const twoWay = mode === 'advance' && !!caps.advance && !!(adv && adv.model);
  // Two-leg tie, second leg with a known first-leg aggregate → badge (caps-driven).
  const aggBadge = caps.two_leg && caps.leg === 2 && caps.agg ? t('soccer.aggBadge', { agg: caps.agg }) : null;

  const winH = twoWay ? t('soccer.advancesLabel', { team: home }) : `${home} ${t('soccer.win')}`;
  const winA = twoWay ? t('soccer.advancesLabel', { team: away }) : `${away} ${t('soccer.win')}`;
  const drawL = t('soccer.drawResult');

  const vModel: any = twoWay ? adv!.model : m.model;
  const vKalshi = twoWay ? adv!.kalshi : m.kalshi;
  const vPoly = twoWay ? adv!.poly_us : m.poly_us;
  const best = (twoWay ? adv!.edge?.best : m.edge?.best) || null;
  const dec = (twoWay ? adv!.decision : m.decision) || null;

  const betting = dec ? !!dec.bet : !!(best?.tradable && best.net_edge > 0);
  const edgeView = (betting && dec && dec.side)
    ? { side: dec.side, venue: dec.venue ?? '', net_edge: dec.net_edge ?? 0 }
    : best ? { side: best.side, venue: best.venue, net_edge: best.net_edge } : null;
  const edgeColor = betting ? 'var(--success)' : 'var(--text-muted)';
  const Chevron = open ? ChevronDown : ChevronRight;

  const leagueTag = showLeague ? leagueLabel({ league: m.league ?? '', zh: m.league_zh }, lang, t) : '';
  const roundTag = stageLabel(m.round, t);

  // Tie whose pairing isn't decided yet (placeholder), or no model row: name-only card.
  if (m.tentative || !m.model) {
    return (
      <div className="pair-card" style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 300, flex: '1 1 360px' }}>
        <div className="flex items-center justify-between">
          <span style={{ fontSize: 13, fontWeight: 700, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)', textTransform: 'uppercase', letterSpacing: '.03em' }}>
            <ClubName club={m.home} /> <span style={{ color: 'var(--text-muted)' }}>{t('soccer.versus')}</span> <ClubName club={m.away} />
          </span>
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{kickoffLabel(m)}</span>
        </div>
        <div style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', marginTop: 2 }}>
          {leagueTag ? leagueTag + ' · ' : ''}{roundTag ? roundTag + ' · ' : ''}{t('soccer.tbdPairing')}
        </div>
      </div>
    );
  }

  // Headline = the model's most likely outcome under the active lens.
  const sides: [string, number][] = twoWay
    ? [[winH, vModel.home], [winA, vModel.away]]
    : [[winH, vModel.home], [drawL, vModel.draw], [winA, vModel.away]];
  const top = sides.reduce((a, b) => (b[1] > a[1] ? b : a));

  const sideLabelMap: Record<string, string> = twoWay
    ? { home: winH, away: winA }
    : { home: winH, draw: drawL, away: winA };
  const venueLabelMap: Record<string, string> = { kalshi: 'Kalshi', poly_us: 'Polymarket US', poly: 'Polymarket', poly_global: 'Polymarket' };

  // Decision line (simplified vs the WC advice prose): BET side @ ask, stake, edge / HOLD.
  const decisionLine = () => {
    if (dec && dec.bet && dec.side) {
      const bits = [
        `${venueLabelMap[dec.venue ?? ''] ?? dec.venue ?? ''} · ${sideLabelMap[dec.side] ?? dec.side} @ ${dec.price_cents != null ? Math.round(dec.price_cents) : '—'}¢`,
        dec.model_prob != null ? `${t('soccer.model')} ${Math.round(dec.model_prob * 100)}%` : '',
        dec.net_edge != null ? `${t('soccer.colEdge')} ${(dec.net_edge * 100).toFixed(1)}%` : '',
        dec.stake_usd != null ? new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(dec.stake_usd) : '',
      ].filter(Boolean);
      return `${t('soccer.bet')} · ${bits.join(' · ')}`;
    }
    if (best && best.tradable && best.net_edge > 0) {
      return `${t('soccer.edgeFound')} · ${venueLabelMap[best.venue] ?? best.venue}/${sideLabelMap[best.side] ?? best.side} +${(best.net_edge * 100).toFixed(1)}%`;
    }
    return t('soccer.hold');
  };

  // One labelled row (probabilities or venue asks); the draw column drops when twoWay.
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
            <ClubName club={m.home} /> <span style={{ color: 'var(--text-muted)' }}>{t('soccer.versus')}</span> <ClubName club={m.away} />
          </span>
          <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
            {twoWay ? <span style={{ color: 'var(--accent-primary)', marginRight: 6 }}>{t('soccer.modeAdvance')}</span> : null}{kickoffLabel(m)}
          </span>
        </div>
        {(leagueTag || roundTag || aggBadge) && (
          <div style={{ fontSize: 9, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)', marginTop: 2, display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
            {leagueTag && <span>{leagueTag}</span>}
            {roundTag && <span>{roundTag}</span>}
            {aggBadge && (
              <span style={{ padding: '0 5px', border: '1px solid var(--accent-primary)', color: 'var(--accent-primary)', fontWeight: 700, letterSpacing: '.04em' }}>
                {aggBadge}
              </span>
            )}
          </div>
        )}
        {!open && (
          <div className="flex items-center justify-between" style={{ marginTop: 4 }}>
            <span style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}>
              {t('soccer.ourPrediction')}: <b style={{ color: 'var(--text-primary)' }}>{top[0]} {pct(top[1])}</b>
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
            <span style={{ color: betting ? 'var(--success)' : 'var(--text-muted)', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.04em' }}>{t('soccer.decision')}</span>
            <span style={{ color: 'var(--text-primary)' }}> · {decisionLine()}</span>
          </div>
          <Line label={t('soccer.ourPrediction')} h={vModel.home} d={twoWay ? null : vModel.draw} a={vModel.away} fmt={pct}
            hc={vModel.cents?.home} dc={twoWay ? null : vModel.cents?.draw} ac={vModel.cents?.away} />
          {!twoWay && (m.model.over_2_5 != null || m.model.btts != null) && (
            <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
              {t('soccer.mktOver25')} {pct(m.model.over_2_5)} · {t('soccer.mktBtts')} {pct(m.model.btts)}
            </div>
          )}
          {vKalshi
            ? <><Line label={t('soccer.kalshiPrice')} h={vKalshi.home?.ask} d={twoWay ? null : vKalshi.draw?.ask} a={vKalshi.away?.ask} fmt={px}
                hc={vKalshi.home?.ask_c} dc={twoWay ? null : vKalshi.draw?.ask_c} ac={vKalshi.away?.ask_c} /><VigNote q={vKalshi} twoWay={twoWay} /></>
            : <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginTop: 3 }}>{t('soccer.kalshiPrice')}: {t('soccer.notListed')}</div>}
          {vPoly
            ? <><Line label={t('soccer.polyPrice')} h={vPoly.home?.ask} d={twoWay ? null : vPoly.draw?.ask} a={vPoly.away?.ask} fmt={px}
                hc={vPoly.home?.ask_c} dc={twoWay ? null : vPoly.draw?.ask_c} ac={vPoly.away?.ask_c} /><VigNote q={vPoly} twoWay={twoWay} /></>
            : <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginTop: 3 }}>{t('soccer.polyPrice')}: {t('soccer.notListed')}</div>}
          {edgeView && (
            <div style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: edgeColor, fontWeight: 700, marginTop: 4 }}>
              {t('soccer.colEdge')}: {venueLabelMap[edgeView.venue] ?? edgeView.venue}/{sideLabelMap[edgeView.side] ?? edgeView.side} {edgeView.net_edge >= 0 ? '+' : ''}{(edgeView.net_edge * 100).toFixed(1)}%{betting ? ' ★' : ''}
            </div>
          )}
        </>
      )}
    </div>
  );
}
