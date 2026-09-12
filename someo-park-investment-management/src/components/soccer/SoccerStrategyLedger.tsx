import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import ClubName from './ClubName';
import { fmtDate, fmtDateTime, leagueLabel } from './soccerLabels';

export type StrategyLedger = {
  schema_version: number; ledger_id: string; as_of: string;
  records: any[];
  summary: { n_matches: number; n_pre: number; n_inplay: number; n_legs: number;
    pre: any; inplay: any; combined: any };
};

export function hasStrategyLedger(value: any): value is StrategyLedger {
  return value?.schema_version === 1 && typeof value.ledger_id === 'string'
    && !!value.ledger_id && !!value.as_of && Array.isArray(value.records)
    && !!value.summary?.pre && !!value.summary?.inplay && !!value.summary?.combined;
}

const finite = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n);
/** Ledger money retains the paper ledger's 0.1-cent precision; quotes stay per-contract. */
export function ledgerMoney(n: unknown, language: string): string {
  return finite(n) ? new Intl.NumberFormat(language, { style: 'currency', currency: 'USD',
    minimumFractionDigits: 3, maximumFractionDigits: 3 }).format(n) : '—';
}
export function ledgerQuote(n: unknown, language: string): string {
  return finite(n) ? `${new Intl.NumberFormat(language, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(n)}¢` : '—';
}
export function ledgerOutcome(cents: unknown): 'profit' | 'loss' | 'flat' | 'unknown' {
  return !finite(cents) ? 'unknown' : cents > 0 ? 'profit' : cents < 0 ? 'loss' : 'flat';
}
const outcomeColor = (outcome: string) => outcome === 'profit' ? 'var(--success)'
  : outcome === 'loss' ? 'var(--error)' : 'var(--text-muted)';
const centsUsd = (n: unknown) => finite(n) ? n / 100 : null;

export function LedgerPnl({ cents, usd }: { cents?: unknown; usd?: unknown }) {
  const { i18n } = useTranslation();
  const value = usd !== undefined ? usd : centsUsd(cents);
  return <span data-pnl-outcome={ledgerOutcome(value)} style={{ color: outcomeColor(ledgerOutcome(value)), whiteSpace: 'nowrap' }}>
    {ledgerMoney(value, i18n.language)}
  </span>;
}

export function StrategyLedgerUnavailable() {
  const { t } = useTranslation();
  return <div role="status" style={{ padding: '12px 0', color: 'var(--warning, #a86b00)' }}>{t('soccer.strategyLedger.unavailable')}</div>;
}

/** Same identity, denominator, outcomes and totals in both live UI projections. */
export function SoccerStrategyLedgerSummary({ ledger }: { ledger: StrategyLedger }) {
  const { t, i18n } = useTranslation();
  const key = (name: string) => t(`soccer.strategyLedger.${name}`);
  const s = ledger.summary;
  return <section data-strategy-ledger={ledger.ledger_id} style={{ fontFamily: 'var(--font-mono)', marginBottom: 12 }}>
    <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 5 }}>{key('title')}</div>
    <div style={{ fontSize: 12, marginBottom: 8 }}>
      {key('totalPnl')}: <b><LedgerPnl usd={s.combined.pnl_usd} /></b>
      {' · '}{t('soccer.strategyLedger.population', { matches: s.n_matches, legs: s.n_legs })}
    </div>
    <div style={{ overflowX: 'auto' }}><table className="table">
      <thead><tr>{['track', 'legs', 'profit', 'loss', 'flat', 'unknown', 'pnl'].map(k => <th key={k} scope="col">{key(k)}</th>)}</tr></thead>
      <tbody>{(['combined', 'pre', 'inplay'] as const).map(track => {
        const row = s[track];
        return <tr key={track} data-ledger-summary-track={track}>
          <th scope="row">{key(track)}</th><td>{row.n ?? '—'}</td><td>{row.profit ?? '—'}</td>
          <td>{row.loss ?? '—'}</td><td>{row.flat ?? '—'}</td><td>{row.unknown ?? '—'}</td>
          <td><LedgerPnl usd={row.pnl_usd} /></td>
        </tr>;
      })}</tbody>
    </table></div>
    <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 5 }}>{key('outcomeNote')} {key('units')}</div>
    <div style={{ fontSize: 9, color: 'var(--text-muted)', marginTop: 4, overflowWrap: 'anywhere' }}>
      {key('sameSource')} · {key('snapshot')}: {fmtDateTime(ledger.as_of, i18n.language)} · {key('ledgerId')}: <code>{ledger.ledger_id}</code>
    </div>
  </section>;
}

/** A record is never filtered by the availability of an accompanying market quote. */
export function SoccerStrategyRecord({ record: b, children }: { record: any; children?: ReactNode }) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language;
  const key = (name: string) => t(`soccer.strategyLedger.${name}`);
  const tracks = [
    { id: 'pre', entered: b.bet === true, side: b.pick, team: b.pick_team,
      stake: b.stake_usd, entry: b.entry_cents, mark: t('soccer.milestone.PRE'),
      exit: b.smart_exit, settle: b.settle_cents, pnl: b.realized_pnl_cents },
    { id: 'inplay', entered: !!b.inplay_side, side: b.inplay_side, team: b.inplay_side_team,
      stake: b.inplay_stake_usd, entry: b.inplay_entry_cents, mark: b.inplay_milestone,
      exit: b.inplay_exit, settle: typeof b.inplay_won === 'boolean' ? (b.inplay_won ? 100 : 0) : null,
      pnl: b.inplay_pnl_cents },
  ];
  const team = (side: string, fallback: any) => side === 'draw' ? t('soccer.drawResult')
    : <ClubName club={{ id: side === 'home' ? b.home_id : b.away_id, name: fallback }} />;
  return <article className="card" data-strategy-record={b.fixture_id} style={{ marginBottom: 12, fontFamily: 'var(--font-mono)' }}>
    <div style={{ fontWeight: 700, fontSize: 12, marginBottom: 4 }}>
      <ClubName club={{ id: b.home_id, name: b.home }} /> {b.score ?? ''} <ClubName club={{ id: b.away_id, name: b.away }} />
    </div>
    <div style={{ color: 'var(--text-muted)', fontSize: 10, marginBottom: 6 }}>
      {fmtDate(b.date, lang)} · {leagueLabel({ league: b.league ?? '' }, lang, t)}
    </div>
    <div style={{ overflowX: 'auto' }}><table className="table">
      <thead><tr>{['track', 'side', 'stake', 'entry', 'exit', 'outcome', 'pnl'].map(k => <th key={k} scope="col">{key(k)}</th>)}</tr></thead>
      <tbody>{tracks.map(row => <tr key={row.id} data-strategy-leg={`${b.fixture_id}:${row.id}`}>
        <th scope="row">{key(row.id)}</th>
        {row.entered ? <>
          <td>{team(row.side, row.team)}</td><td>{ledgerMoney(row.stake, lang)}</td>
          <td>{row.mark ?? '—'} · {ledgerQuote(row.entry, lang)}</td>
          <td>{row.exit ? `${row.exit.sold_min ?? '—'}′ · ${ledgerQuote(row.exit.sold_c, lang)}`
            : `${key('settled')} · ${ledgerQuote(row.settle, lang)}`}</td>
          <td data-strategy-outcome={ledgerOutcome(row.pnl)} style={{ color: outcomeColor(ledgerOutcome(row.pnl)) }}>{key(ledgerOutcome(row.pnl))}</td>
          <td><LedgerPnl cents={row.pnl} /></td>
        </> : <td colSpan={6} style={{ color: 'var(--text-muted)' }}>{key('noEntry')}</td>}
      </tr>)}</tbody>
    </table></div>
    <div data-ledger-cumulative={b.fixture_id} style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 14px', fontSize: 10, marginTop: 6 }}>
      <span>{key('preCum')}: <LedgerPnl cents={b.pre_cum_pnl_cents} /></span>
      <span>{key('inplayCum')}: <LedgerPnl cents={b.inplay_cum_pnl_cents} /></span>
      <span>{key('combinedCum')}: <b><LedgerPnl cents={b.combined_cum_pnl_cents} /></b></span>
    </div>
    {children}
  </article>;
}
