import React, { useState, useMemo } from 'react';
import { Filter } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import { getDSRLog } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import PairBadge from '../PairBadge';

export default function WFGridViewer({ params: viewParams }: { params?: any }) {
  const { t } = useTranslation();
  const [strategy, setStrategy] = useState(viewParams?.strategy || 'mrpt');
  const [selectedPair, setSelectedPair] = useState('ALL');
  const [selectedWindow, setSelectedWindow] = useState('ALL');
  const [selectedParamSet, setSelectedParamSet] = useState('ALL');

  const { data: rawData, loading, error, refetch } = useApi(() => getDSRLog(strategy), [strategy]);

  const pairKeys = useMemo(() => {
    if (!rawData) return ['ALL'];
    const unique = [...new Set(rawData.map((r: any) => r.pair_key))];
    return ['ALL', ...unique.sort()];
  }, [rawData]);

  const windowKeys = useMemo(() => {
    if (!rawData) return ['ALL'];
    const unique = [...new Set(rawData.map((r: any) => r.window_idx))].sort((a: number, b: number) => a - b);
    return ['ALL', ...unique.map(String)];
  }, [rawData]);

  const paramSetKeys = useMemo(() => {
    if (!rawData) return ['ALL'];
    const unique = [...new Set(rawData.map((r: any) => r.param_set))];
    return ['ALL', ...unique.sort()];
  }, [rawData]);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!rawData) return null;

  const filteredData = rawData.filter((r: any) => {
    if (selectedPair !== 'ALL' && r.pair_key !== selectedPair) return false;
    if (selectedWindow !== 'ALL' && String(r.window_idx) !== selectedWindow) return false;
    if (selectedParamSet !== 'ALL' && r.param_set !== selectedParamSet) return false;
    return true;
  });

  const resetFilters = () => { setSelectedPair('ALL'); setSelectedWindow('ALL'); setSelectedParamSet('ALL'); };

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)]">{t('wfGrid.title', { count: rawData.length })}</div>
        <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
          {['mrpt', 'mtfs'].map(s => (
            <button key={s} onClick={() => { setStrategy(s); resetFilters(); }} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === s ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
              {s.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-2 mb-3 shrink-0 flex-wrap">
        <Filter className="w-4 h-4 text-[var(--text-muted)]" />
        <select value={selectedPair} onChange={e => setSelectedPair(e.target.value)} className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md px-2 py-1.5 text-xs focus:outline-none focus:border-[var(--accent-primary)] text-[var(--text-primary)]">
          {pairKeys.map(p => <option key={p} value={p}>{p === 'ALL' ? t('wfGrid.pairAll') : p}</option>)}
        </select>
        <select value={selectedWindow} onChange={e => setSelectedWindow(e.target.value)} className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md px-2 py-1.5 text-xs focus:outline-none focus:border-[var(--accent-primary)] text-[var(--text-primary)]">
          {windowKeys.map(w => <option key={w} value={w}>{w === 'ALL' ? t('wfGrid.windowAll') : `W${w}`}</option>)}
        </select>
        <select value={selectedParamSet} onChange={e => setSelectedParamSet(e.target.value)} className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md px-2 py-1.5 text-xs focus:outline-none focus:border-[var(--accent-primary)] text-[var(--text-primary)]">
          {paramSetKeys.map(p => <option key={p} value={p}>{p === 'ALL' ? t('wfGrid.paramAll') : p}</option>)}
        </select>
        <span className="text-xs text-[var(--text-muted)]">{filteredData.length} {t('common.results')}</span>
      </div>

      <div className="flex-1 overflow-y-auto border border-[var(--border-subtle)] rounded-md bg-[var(--bg-primary)]">
        <table className="w-full text-sm text-left">
          <thead className="text-[10px] text-[var(--text-muted)] uppercase bg-[var(--bg-secondary)] sticky top-0 z-10">
            <tr>
              <th className="px-3 py-2 font-medium">{t('common.pair')}</th>
              <th className="px-3 py-2 font-medium">{t('inventory.paramSet')}</th>
              <th className="px-3 py-2 font-medium">{t('common.window')}</th>
              <th className="px-3 py-2 font-medium">{t('wfGrid.pairPnl')}</th>
              <th className="px-3 py-2 font-medium">{t('wfGrid.pairSharpe')}</th>
              <th className="px-3 py-2 font-medium">{t('wfGrid.dsrPval')}</th>
              <th className="px-3 py-2 font-medium">{t('common.trades')}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--border-subtle)] text-xs">
            {filteredData.map((row: any, i: number) => {
              const pnl = Number(row.pair_pnl) || 0;
              const sharpe = Number(row.pair_sharpe) || 0;
              const dsr = Number(row.dsr_pvalue) || 0;
              return (
                <tr key={i} className="hover:bg-[var(--bg-secondary)] transition-colors">
                  <td className="px-3 py-2"><PairBadge pair={row.pair_key} strategy={strategy} compact /></td>
                  <td className="px-3 py-2 text-[var(--text-secondary)]">{row.param_set}</td>
                  <td className="px-3 py-2 text-[var(--text-secondary)]">W{row.window_idx}</td>
                  <td className={`px-3 py-2 font-mono ${pnl >= 0 ? 'text-[var(--success)]' : 'text-[var(--error)]'}`}>${pnl.toLocaleString(undefined, { maximumFractionDigits: 0 })}</td>
                  <td className={`px-3 py-2 font-mono ${sharpe >= 1 ? 'text-[var(--success)]' : sharpe < 0 ? 'text-[var(--error)]' : 'text-[var(--text-primary)]'}`}>{sharpe.toFixed(2)}</td>
                  <td className={`px-3 py-2 font-mono ${dsr < 0.05 ? 'text-[var(--success)]' : 'text-[var(--text-secondary)]'}`}>{dsr.toFixed(4)}</td>
                  <td className="px-3 py-2 font-mono text-[var(--text-secondary)]">{row.n_trades}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
