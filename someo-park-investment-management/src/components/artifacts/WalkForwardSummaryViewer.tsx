import React, { useState } from 'react';
import { Calendar } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import { getWFSummary } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import PairBadge from '../PairBadge';

export default function WalkForwardSummaryViewer({ params }: { params?: any }) {
  const { t } = useTranslation();
  const [strategy, setStrategy] = useState(params?.strategy || 'mrpt');
  const { data, loading, error, refetch } = useApi(() => getWFSummary(strategy), [strategy]);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!data) return null;

  const windows = data.windows || [];
  const oosStats = data.oos_stats || {};

  return (
    <div className="flex flex-col h-full space-y-4">
      <div className="flex items-center justify-between shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)]">{t('wfSummary.title', { count: windows.length })}</div>
        <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
          {['mrpt', 'mtfs'].map(s => (
            <button key={s} onClick={() => setStrategy(s)} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === s ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
              {s.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      {/* OOS Aggregate Stats */}
      <div className="grid grid-cols-3 gap-3 shrink-0">
        <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
          <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('wfSummary.oosTotalPnl')}</div>
          <div className={`text-sm font-mono ${oosStats.oos_total_pnl >= 0 ? 'text-[var(--success)]' : 'text-[var(--error)]'}`}>
            ${oosStats.oos_total_pnl?.toLocaleString(undefined, { maximumFractionDigits: 0 })}
          </div>
        </div>
        <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
          <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('wfSummary.oosSharpe')}</div>
          <div className={`text-sm font-mono ${oosStats.oos_sharpe >= 1 ? 'text-[var(--success)]' : oosStats.oos_sharpe < 0 ? 'text-[var(--error)]' : 'text-[var(--text-primary)]'}`}>
            {oosStats.oos_sharpe?.toFixed(2)}
          </div>
        </div>
        <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
          <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('wfSummary.maxDrawdown')}</div>
          <div className="text-sm font-mono text-[var(--error)]">
            {(oosStats.oos_max_dd_pct * 100)?.toFixed(2)}%
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto space-y-3">
        {windows.map((w: any) => (
          <div key={w.window_idx} className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-4">
            <div className="flex justify-between items-center mb-3">
              <div className="font-medium text-[var(--text-primary)]">{t('wfSummary.windowN', { n: w.window_idx })}</div>
              <div className="flex gap-3 text-xs">
                <span className={`font-mono ${w.oos_sharpe >= 1 ? 'text-[var(--success)]' : w.oos_sharpe < 0 ? 'text-[var(--error)]' : 'text-[var(--text-primary)]'}`}>
                  {t('wfSummary.sharpe', { value: w.oos_sharpe?.toFixed(2) })}
                </span>
                <span className={`font-mono ${w.oos_pnl >= 0 ? 'text-[var(--success)]' : 'text-[var(--error)]'}`}>
                  {t('wfSummary.pnl', { value: w.oos_pnl?.toLocaleString(undefined, { maximumFractionDigits: 0 }) })}
                </span>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-4 text-xs text-[var(--text-secondary)]">
              <div>
                <div className="flex items-center gap-1 mb-1 text-[var(--text-muted)]"><Calendar className="w-3 h-3"/> {t('wfSummary.trainPeriod')}</div>
                <div className="font-mono">{w.train_start} to {w.train_end}</div>
              </div>
              <div>
                <div className="flex items-center gap-1 mb-1 text-[var(--text-muted)]"><Calendar className="w-3 h-3"/> {t('wfSummary.testPeriod')}</div>
                <div className="font-mono">{w.test_start} to {w.test_end}</div>
              </div>
            </div>
            <div className="mt-3 pt-3 border-t border-[var(--border-subtle)] text-xs">
              <div className="flex items-center gap-1 flex-wrap">
                <span className="text-[var(--text-muted)]">{t('wfSummary.selectedPairs')}</span>
                <span className="text-[var(--accent-primary)]">{w.n_selected_pairs}</span>
                {w.selected_pairs && w.selected_pairs.slice(0, 5).map((p: any, pi: number) => (
                  <span key={pi}><PairBadge s1={p[0]} s2={p[1]} strategy={strategy} compact noPopover /></span>
                ))}
                {w.selected_pairs && w.selected_pairs.length > 5 && (
                  <span className="text-[var(--text-muted)]">{t('wfSummary.nMore', { count: w.selected_pairs.length - 5 })}</span>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
