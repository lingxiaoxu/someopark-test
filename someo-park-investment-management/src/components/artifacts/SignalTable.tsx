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
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!data) return null;

  // Signal file uses flat `signals` array with action field, or legacy active_signals/flat_signals
  const allSignals: any[] = data.signals || [];
  const activeSignals = data.active_signals || allSignals.filter((s: any) => s.action && s.action !== 'FLAT' && s.action !== 'HOLD');
  const flatSignals = data.flat_signals || allSignals.filter((s: any) => s.action === 'FLAT' || s.action === 'HOLD');
  const excludedPairs = data.excluded_pairs || [];

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)]">{t('signals.title', { strategy: strategy.toUpperCase() })}</div>
        <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
          {['mrpt', 'mtfs'].map(s => (
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
                      sig.action?.includes('OPEN') ? 'bg-[var(--success)]/10 text-[var(--success)]' :
                      sig.action?.includes('CLOSE') ? 'bg-[var(--error)]/10 text-[var(--error)]' :
                      'bg-[var(--text-muted)]/10 text-[var(--text-muted)]'
                    }`}>
                      {sig.action === 'MACRO_VETO' ? `⊘ ${sig.original_action?.replace('_', ' ') ?? 'VETO'}` : sig.action}
                    </span>
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
