import { useTranslation } from 'react-i18next';
import { fmtDateTime } from './soccerLabels';
import { hasStrategyLedger, ledgerMoney } from './SoccerStrategyLedger';

/** Paper reconstructions and venue receipts have different evidentiary meaning. */
export default function SoccerPerformanceEvidence({ data }: { data: any }) {
  const { t, i18n } = useTranslation();
  const key = (name: string) => t(`soccer.performanceEvidence.${name}`);
  const money = (n: unknown) => ledgerMoney(n, i18n.language);
  const evidence = data.evidence_summary;
  const labels: Record<string, string> = {
    legacy_live_marked_paper: 'legacyLive', posthoc_candlestick_paper: 'posthoc', forward_observed_paper: 'forward',
  };
  const cells = { padding: '6px 8px', borderBottom: '1px solid var(--border-subtle)', textAlign: 'left' as const };
  return <section style={{ marginTop: 8, marginBottom: 12, fontFamily: 'var(--font-mono)', fontSize: 10, lineHeight: 1.6, color: 'var(--text-muted)' }}>
    <div>{key('paper')} · {key('pitUnverified')}</div>
    <details style={{ marginTop: 4 }}><summary style={{ cursor: 'pointer' }}>{key('title')}</summary>
    {!evidence ? <div>{key('summaryMissing')}</div> : <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead><tr>{['tier', 'count', 'preGross', 'liveGross'].map(k => <th key={k} scope="col" style={cells}>{key(k)}</th>)}</tr></thead>
        <tbody>{(evidence.tiers ?? []).map((tier: any) => <tr key={tier.level}>
          <td style={cells}>{labels[tier.level] ? key(labels[tier.level]) : key('summaryMissing')}</td>
          <td style={cells}>{tier.n_pre ?? '—'} / {tier.n_inplay ?? '—'}</td>
          <td style={cells}>{money(tier.pre_gross_usd)}</td><td style={cells}>{money(tier.inplay_gross_usd)}</td>
        </tr>)}</tbody>
      </table>
    </div>}
    <p style={{ margin: '5px 0' }}>{key('modelLimit')}</p>
    </details>
  </section>;
}

/** Actual demo fills audit execution of a subset; they are not a model-performance denominator. */
export function SoccerDemoExecutionSubset({ data }: { data: any }) {
  const { t, i18n } = useTranslation();
  const key = (name: string) => t(`soccer.performanceEvidence.${name}`);
  const ledgerKey = (name: string) => t(`soccer.strategyLedger.${name}`);
  const money = (n: unknown) => ledgerMoney(n, i18n.language);
  const demo = data?.demo_execution;
  const ledger = hasStrategyLedger(data?.strategy_ledger) ? data.strategy_ledger : null;
  const coverage = demo?.coverage;
  const verified = coverage?.state === 'ok' && coverage?.matching_basis === 'fixture_id_track_side';
  const ratio = verified && typeof coverage.fill_coverage_ratio === 'number'
    ? new Intl.NumberFormat(i18n.language, { style: 'percent', maximumFractionDigits: 1 }).format(coverage.fill_coverage_ratio) : '—';
  const cells = { padding: '6px 8px', borderBottom: '1px solid var(--border-subtle)', textAlign: 'left' as const };
  return <section data-demo-execution-subset="true" style={{ marginTop: 16, fontFamily: 'var(--font-mono)', fontSize: 11, lineHeight: 1.6 }}>
    <b>{ledgerKey('demoTitle')}</b>
    <div style={{ color: 'var(--text-muted)', margin: '5px 0' }}>{ledgerKey('demoNote')}</div>
    <div>{t('soccer.strategyLedger.demoCounts', { filled: demo?.n_filled ?? '—', legs: ledger?.summary.n_legs ?? '—' })}</div>
    {verified ? <>
      <div>{t('soccer.strategyLedger.matchedCoverage', { matched: coverage.n_filled_model_legs, legs: coverage.n_model_legs, ratio, fixtures: coverage.n_filled_model_unique_fixtures })}</div>
      <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>{t('soccer.strategyLedger.outsideLedger', { count: coverage.n_filled_outside_model_or_side_mismatch })}</div>
    </> : <div style={{ fontSize: 10, color: 'var(--text-muted)' }}>{ledgerKey('coverageUnavailable')}</div>}
    <div style={{ fontSize: 9, color: 'var(--text-muted)', marginBottom: 6 }}>{ledgerKey('snapshot')}: {fmtDateTime(data?.as_of, i18n.language) || '—'}</div>
    {!demo ? <div>{key('demoMissing')}</div> : <>
      <div>{key('fills')}: {demo.n_filled ?? '—'} · {key('closed')}: {demo.n_closed ?? '—'}</div>
      <div>{key('gross')}: {money(demo.gross_pnl_usd)} · {key('fees')}: {money(demo.fees_usd)}</div>
      <div>{key(demo.net_is_estimate ? 'netEstimated' : 'net')}: <b>{demo.fee_status === 'complete' ? money(demo.net_pnl_usd) : '—'}</b></div>
      <div style={{ color: 'var(--text-muted)' }}>{key(demo.fee_status !== 'complete' ? 'feesIncomplete' : demo.net_is_estimate ? 'feeEstimated' : 'feeComplete')}</div>
      {demo.by_track && <div style={{ overflowX: 'auto' }}><table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead><tr>{['tier', 'closed', 'record', 'gross', 'fees', 'net'].map(k => <th key={k} scope="col" style={cells}>{key(k)}</th>)}</tr></thead>
        <tbody>{(['pre', 'inplay'] as const).map(track => {
          const row = demo.by_track[track];
          if (!row) return null;
          return <tr key={track}><td style={cells}>{key(track === 'pre' ? 'preTrack' : 'inplayTrack')}</td>
            <td style={cells}>{row.n_closed ?? '—'}</td><td style={cells}>{row.wins ?? '—'} / {row.losses ?? '—'}</td>
            <td style={cells}>{money(row.gross_pnl_usd)}</td><td style={cells}>{money(row.fees_usd)}</td>
            <td style={cells}>{row.fee_status === 'complete' ? money(row.net_pnl_usd) : '—'}{row.net_is_estimate ? ' ≈' : ''}</td></tr>;
        })}</tbody>
      </table></div>}
    </>}
  </section>;
}
