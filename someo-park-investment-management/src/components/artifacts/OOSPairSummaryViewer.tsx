import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import { getOOSPairSummary } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import PairBadge from '../PairBadge';

export default function OOSPairSummaryViewer({ params }: { params?: any }) {
  const { t } = useTranslation();
  const [strategy, setStrategy] = useState(params?.strategy || 'mrpt');
  const [sortKey, setSortKey] = useState<string>('OOS_PnL');
  const [sortAsc, setSortAsc] = useState(false);

  const { data: rawPairs, loading, error, refetch } = useApi(() => getOOSPairSummary(strategy), [strategy]);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!rawPairs) return null;

  const pairs = [...rawPairs].sort((a: any, b: any) => {
    const va = Number(a[sortKey]) || 0;
    const vb = Number(b[sortKey]) || 0;
    return sortAsc ? va - vb : vb - va;
  });

  const handleSort = (key: string) => {
    if (sortKey === key) setSortAsc(!sortAsc);
    else { setSortKey(key); setSortAsc(false); }
  };

  const fmtPnl = (v: number) => v >= 0 ? `+$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}` : `-$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)]">{t('oosPairSummary.title')}</div>
        <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
          {['mrpt', 'mtfs'].map(s => (
            <button key={s} onClick={() => setStrategy(s)} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === s ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
              {s.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto border border-[var(--border-subtle)] rounded-md bg-[var(--bg-primary)]">
        <table className="w-full text-sm text-left whitespace-nowrap">
          <thead className="text-[10px] text-[var(--text-muted)] uppercase bg-[var(--bg-secondary)] sticky top-0 z-10">
            <tr>
              <th className="px-3 py-2 font-medium">{t('common.pair')}</th>
              <th className="px-3 py-2 font-medium cursor-pointer hover:text-[var(--text-primary)]" onClick={() => handleSort('OOS_PnL')}>{t('oosPairSummary.oosPnl')} {sortKey === 'OOS_PnL' ? (sortAsc ? '↑' : '↓') : ''}</th>
              <th className="px-3 py-2 font-medium cursor-pointer hover:text-[var(--text-primary)]" onClick={() => handleSort('Sharpe')}>{t('oosPairSummary.sharpe')} {sortKey === 'Sharpe' ? (sortAsc ? '↑' : '↓') : ''}</th>
              <th className="px-3 py-2 font-medium">{t('oosPairSummary.maxDdPct')}</th>
              <th className="px-3 py-2 font-medium">{t('oosPairSummary.winRate')}</th>
              <th className="px-3 py-2 font-medium">{t('common.trades')}</th>
              <th className="px-3 py-2 font-medium">{t('common.days')}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--border-subtle)] text-xs">
            {pairs.map((p: any, i: number) => {
              const pnl = Number(p.OOS_PnL) || 0;
              const sharpe = Number(p.Sharpe) || 0;
              const maxDdPct = Number(p.MaxDD_pct) || 0;
              const winRate = Number(p.WinRate) || 0;
              return (
                <tr key={i} className="hover:bg-[var(--bg-secondary)] transition-colors">
                  <td className="px-3 py-2"><PairBadge pair={p.Pair} strategy={strategy} compact /></td>
                  <td className={`px-3 py-2 font-mono ${pnl >= 0 ? 'text-[var(--success)]' : 'text-[var(--error)]'}`}>{fmtPnl(pnl)}</td>
                  <td className={`px-3 py-2 font-mono ${sharpe >= 1 ? 'text-[var(--success)]' : sharpe < 0 ? 'text-[var(--error)]' : 'text-[var(--text-primary)]'}`}>{sharpe.toFixed(2)}</td>
                  <td className="px-3 py-2 font-mono text-[var(--error)]">{(maxDdPct * 100).toFixed(2)}%</td>
                  <td className="px-3 py-2 font-mono text-[var(--text-secondary)]">{(winRate * 100).toFixed(0)}%</td>
                  <td className="px-3 py-2 font-mono text-[var(--text-secondary)]">{p.N_Trades}</td>
                  <td className="px-3 py-2 font-mono text-[var(--text-secondary)]">{p.N_Days}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
