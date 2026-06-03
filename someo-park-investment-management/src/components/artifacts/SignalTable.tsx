import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import { getLatestSignals } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import PairBadge from '../PairBadge';

export default function SignalTable({ params }: { params?: any }) {
  const { t } = useTranslation();
  const [strategy, setStrategy] = useState(params?.strategy || 'mrpt');
  const { data, loading, error, refetch } = useApi(() => getLatestSignals(strategy), [strategy]);

  if (loading) return <LoadingState />;
  if (error && (strategy === 'ssrs' || strategy === 'aiss')) return <ErrorState message="SR/AISS signals require Express backend running. Start: npm run dev" onRetry={refetch} />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!data) return null;

  // ══════════════════════════════════════════════════════════════
  // AISS MODE: stock-level book (subsector is a grouping label only)
  // ══════════════════════════════════════════════════════════════
  if (strategy === 'aiss') {
    // Show ONLY the SELECTED (held) subsectors, each with its 4 stocks
    // (3 weighted tiers + reserve). Reserve / 0% rows are dimmed as FLAT.
    const sh: Record<string, any> = data.stock_holdings || {};
    const shareOf = (tk: string) => sh[tk]?.target_shares ?? sh[tk]?.shares ?? 0;
    // subsector-level action (HOLD = existing book, ENTER/OPEN = new this rebalance)
    const subAction: Record<string, string> = {};
    (data.signals || []).forEach((s: any) => { subAction[s.ticker] = s.action; });
    const mapAction = (a?: string): string | null => {
      if (!a) return null;
      const u = String(a).toUpperCase();
      if (u.includes('ENTER') || u.includes('OPEN') || u.includes('BUY')) return 'OPEN';
      if (u.includes('HOLD')) return 'HOLD';
      if (u.includes('EXIT') || u.includes('SELL') || u.includes('CLOSE')) return 'CLOSE';
      return u;
    };
    const universe: any[] = data.stock_universe || [];
    const heldSubs = universe.filter((g: any) => g.held);
    // grouped: keep each subsector's 4 stocks together (tiers then reserve)
    let rows: any[] = heldSubs.flatMap((g: any) => (g.stocks || []).map((s: any) => {
      const isFlat = s.tier_role === 'reserve' || (s.portfolio_weight || 0) <= 0.0001;
      return {
        ticker: s.ticker, subsector: g.subsector, tier_role: s.tier_role,
        weight: s.portfolio_weight || 0, shares: shareOf(s.ticker), price: s.price || 0,
        action: isFlat ? 'FLAT' : (mapAction(subAction[g.subsector]) || 'HOLD'),
      };
    }));
    if (!universe.length) {
      // fallback: held-only from stock_holdings (no reserve/flat available)
      rows = Object.entries(sh).map(([ticker, h]: any) => ({
        ticker, subsector: h.subsectors?.[0] || 'other', tier_role: '',
        weight: h.portfolio_weight || 0, shares: shareOf(ticker), price: h.price || h.last_price || 0,
        action: mapAction(subAction[h.subsectors?.[0]]) || 'HOLD',
      })).sort((a, b) => b.weight - a.weight);
    }
    return (
      <div className="flex flex-col h-full">
        <div className="flex items-center justify-between mb-4 shrink-0">
          <div className="text-sm font-medium text-[var(--text-primary)]">{t('aiss.signalsTitle')}</div>
          <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
            {['mrpt', 'mtfs', 'ssrs', 'aiss'].map(s => (
              <button key={s} onClick={() => setStrategy(s)} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === s ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>{s.toUpperCase()}</button>
            ))}
          </div>
        </div>
        <div className="text-[10px] text-[var(--text-muted)] mb-2 shrink-0">
          {t('ssrs.signalDate', { date: data.signal_date, regime: typeof data.regime === 'string' ? data.regime.toUpperCase() : (data.regime?.label || data.regime?.regime_label || '—').toUpperCase() })}
        </div>
        <div className="flex-1 overflow-y-auto">
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden">
            <table className="w-full text-left text-sm">
              <thead className="bg-[var(--bg-tertiary)] border-b border-[var(--border-subtle)] text-[10px] uppercase tracking-wider text-[var(--text-muted)]">
                <tr>
                  <th className="px-4 py-3 font-medium">{t('aiss.colStock')}</th>
                  <th className="px-4 py-3 font-medium">{t('aiss.colSubsector')}</th>
                  <th className="px-4 py-3 font-medium">{t('common.action')}</th>
                  <th className="px-4 py-3 font-medium text-right">{t('ssrs.weight')}</th>
                  <th className="px-4 py-3 font-medium text-right">{t('ssrs.shares')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {rows.map((r: any, idx: number) => {
                  const held = r.weight > 0.0001;
                  const actClass = r.action === 'OPEN' ? 'bg-[var(--success)]/10 text-[var(--success)]'
                    : r.action === 'CLOSE' ? 'bg-[var(--error)]/10 text-[var(--error)]'
                    : r.action === 'HOLD' ? 'bg-[var(--accent-primary)]/10 text-[var(--accent-primary)]'
                    : 'bg-[var(--text-muted)]/10 text-[var(--text-muted)]';
                  return (
                  <tr key={idx} className={`hover:bg-[var(--bg-secondary)] transition-colors ${held ? '' : 'opacity-40'}`}>
                    <td className="px-4 py-3">
                      <PairBadge pair={r.ticker} direction={held ? 'long' : null} strategy="aiss" compact details={{ weight: r.weight, shares: r.shares, lastPrice: r.price }} />
                      {r.tier_role === 'reserve' && <span className="ml-2 text-[9px] font-mono text-[var(--text-muted)] uppercase">reserve</span>}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-[var(--text-muted)]">{r.subsector}</td>
                    <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded text-[10px] font-bold tracking-wide ${actClass}`}>{r.action}</span></td>
                    <td className="px-4 py-3 font-mono text-xs text-right">{(r.weight * 100).toFixed(1)}%</td>
                    <td className="px-4 py-3 font-mono text-xs text-right">{held ? r.shares?.toLocaleString() : '0'}</td>
                  </tr>
                  );
                })}
                {rows.length === 0 && <tr><td colSpan={5} className="px-4 py-8 text-center text-[var(--text-muted)]">{t('common.noDataAvailable')}</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    );
  }

  // ══════════════════════════════════════════════════════════════
  // SR MODE: Sector ETF signals with V1/V2 selector
  // ══════════════════════════════════════════════════════════════
  if (strategy === 'ssrs') {
    const srSignals: any[] = data.signals || [];
    const activeETFs = srSignals.filter((s: any) => s.target_weight > 0.01);
    const flatETFs = srSignals.filter((s: any) => !s.target_weight || s.target_weight <= 0.01);
    return (
      <div className="flex flex-col h-full">
        <div className="flex items-center justify-between mb-4 shrink-0">
          <div className="text-sm font-medium text-[var(--text-primary)]">{t('ssrs.signalsTitle')}</div>
          <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
            {['mrpt', 'mtfs', 'ssrs', 'aiss'].map(s => (
              <button key={s} onClick={() => setStrategy(s)} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === s ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
                {s.toUpperCase()}
              </button>
            ))}
          </div>
        </div>
        <div className="text-[10px] text-[var(--text-muted)] mb-2 shrink-0">
          {t('ssrs.signalDate', { date: data.signal_date, regime: typeof data.regime === 'string' ? data.regime.toUpperCase() : (data.regime?.regime_label || '—').toUpperCase() })}
        </div>
        <div className="flex-1 overflow-y-auto">
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden">
            <table className="w-full text-left text-sm">
              <thead className="bg-[var(--bg-tertiary)] border-b border-[var(--border-subtle)] text-[10px] uppercase tracking-wider text-[var(--text-muted)]">
                <tr>
                  <th className="px-4 py-3 font-medium">{t('common.sector')}</th>
                  <th className="px-4 py-3 font-medium">{t('common.action')}</th>
                  <th className="px-4 py-3 font-medium text-right">{t('ssrs.weight')}</th>
                  <th className="px-4 py-3 font-medium text-right">{t('ssrs.score')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {activeETFs.map((sig: any, idx: number) => (
                  <tr key={idx} className="hover:bg-[var(--bg-secondary)] transition-colors">
                    <td className="px-4 py-3"><PairBadge pair={sig.ticker} direction={sig.target_weight > 0.01 ? 'long' : null} strategy="ssrs" compact
                      details={{ weight: sig.target_weight, shares: sig.current_shares || sig.target_shares, lastPrice: sig.price }} /></td>
                    <td className="px-4 py-3">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold tracking-wide ${
                        sig.action === 'HOLD' ? 'bg-[var(--accent-primary)]/10 text-[var(--accent-primary)]' :
                        sig.action?.includes('ENTER') || sig.action?.includes('BUY') ? 'bg-[var(--success)]/10 text-[var(--success)]' :
                        sig.action?.includes('EXIT') || sig.action?.includes('SELL') ? 'bg-[var(--error)]/10 text-[var(--error)]' :
                        'bg-[var(--text-muted)]/10 text-[var(--text-muted)]'
                      }`}>{sig.action}</span>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-right">{(sig.target_weight * 100).toFixed(1)}%</td>
                    <td className="px-4 py-3 font-mono text-xs text-right" style={{ color: sig.composite_score > 0 ? 'var(--success)' : 'var(--error)' }}>
                      {sig.composite_score?.toFixed(3)}
                    </td>
                  </tr>
                ))}
                {flatETFs.map((sig: any, idx: number) => (
                  <tr key={`flat-${idx}`} className="hover:bg-[var(--bg-secondary)] transition-colors opacity-40">
                    <td className="px-4 py-3"><PairBadge pair={sig.ticker} strategy="ssrs" compact /></td>
                    <td className="px-4 py-3"><span className="px-2 py-0.5 rounded text-[10px] font-bold tracking-wide bg-[var(--text-muted)]/10 text-[var(--text-muted)]">FLAT</span></td>
                    <td className="px-4 py-3 font-mono text-xs text-right">0%</td>
                    <td className="px-4 py-3 font-mono text-xs text-right">{sig.composite_score?.toFixed(3) || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    );
  }

  // ══════════════════════════════════════════════════════════════
  // MRPT/MTFS MODE — original code unchanged below
  // ══════════════════════════════════════════════════════════════
  const allSignals: any[] = data.signals || [];
  const activeSignals = data.active_signals || allSignals.filter((s: any) => s.action && s.action !== 'FLAT' && s.action !== 'HOLD');
  const flatSignals = data.flat_signals || allSignals.filter((s: any) => s.action === 'FLAT' || s.action === 'HOLD');
  const excludedPairs = data.excluded_pairs || [];

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)]">{t('signals.title', { strategy: strategy.toUpperCase() })}</div>
        <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
          {['mrpt', 'mtfs', 'ssrs', 'aiss'].map(s => (
            <button key={s} onClick={() => setStrategy(s)} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === s ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
              {s.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      <div className="text-[10px] text-[var(--text-muted)] mb-2 shrink-0">
        {t('signals.signalDate', { date: data.signal_date, time: data.generated_at })}
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden">
          <table className="w-full text-left text-sm">
            <thead className="bg-[var(--bg-tertiary)] border-b border-[var(--border-subtle)] text-[10px] uppercase tracking-wider text-[var(--text-muted)]">
              <tr>
                <th className="px-4 py-3 font-medium">{t('common.pair')}</th>
                <th className="px-4 py-3 font-medium">{t('common.action')}</th>
                <th className="px-4 py-3 font-medium">{t('signals.zScore')}</th>
                <th className="px-4 py-3 font-medium">{t('common.shares')}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-subtle)]">
              {activeSignals.map((sig: any, idx: number) => (
                <tr key={idx} className="hover:bg-[var(--bg-secondary)] transition-colors">
                  <td className="px-4 py-3">
                    <PairBadge
                      pair={sig.pair}
                      direction={sig.direction || (sig.action?.includes('LONG') ? 'long' : sig.action?.includes('SHORT') ? 'short' : undefined)}
                      strategy={strategy}
                      compact
                    />
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-[10px] font-bold tracking-wide ${
                      sig.action === 'MACRO_VETO' ? 'bg-[var(--warning)]/10 text-[var(--warning)]' :
                      sig.action === 'BLACKOUT' ? 'bg-purple-500/10 text-purple-400' :
                      sig.action?.includes('OPEN') ? 'bg-[var(--success)]/10 text-[var(--success)]' :
                      sig.action?.includes('CLOSE') ? 'bg-[var(--error)]/10 text-[var(--error)]' :
                      'bg-[var(--text-muted)]/10 text-[var(--text-muted)]'
                    }`}>
                      {sig.action === 'MACRO_VETO' ? `⊘ ${sig.original_action?.replace('_', ' ') ?? 'VETO'}` :
                       sig.action === 'BLACKOUT' ? '◉ BLACKOUT' : sig.action}
                    </span>
                    {sig.note && (sig.action === 'MACRO_VETO' || sig.action === 'BLACKOUT') && (
                      <div className="text-[9px] text-[var(--text-muted)] mt-0.5 truncate max-w-[200px]" title={sig.note}>{sig.note}</div>
                    )}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">{sig.z_score?.toFixed(2) ?? sig.momentum_spread?.toFixed(2) ?? 'N/A'}</td>
                  <td className="px-4 py-3 font-mono text-xs text-[var(--text-secondary)]">
                    {sig.s1_shares ?? sig.s1?.shares ?? '—'} / {sig.s2_shares ?? sig.s2?.shares ?? '—'}
                  </td>
                </tr>
              ))}
              {flatSignals.map((sig: any, idx: number) => (
                <tr key={`flat-${idx}`} className="hover:bg-[var(--bg-secondary)] transition-colors opacity-60">
                  <td className="px-4 py-3"><PairBadge pair={sig.pair} strategy={strategy} compact /></td>
                  <td className="px-4 py-3">
                    <span className="px-2 py-0.5 rounded text-[10px] font-bold tracking-wide bg-[var(--text-muted)]/10 text-[var(--text-muted)]">FLAT</span>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">{sig.z_score?.toFixed(2) ?? '—'}</td>
                  <td className="px-4 py-3 font-mono text-xs text-[var(--text-muted)]">—</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {excludedPairs.length > 0 && (
          <div className="mt-4">
            <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-2">{t('signals.excluded')}</div>
            <div className="space-y-1">
              {excludedPairs.map((ep: any, idx: number) => (
                <div key={idx} className="text-xs text-[var(--text-muted)] bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded px-3 py-2 flex items-center gap-2">
                  <PairBadge pair={ep.pair} strategy={strategy} compact noPopover /> <span>— {ep.exclusion_reason}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
