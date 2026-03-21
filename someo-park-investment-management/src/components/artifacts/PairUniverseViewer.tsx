import React, { useState } from 'react';
import { Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import { getPairUniverse, getPairDb } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import PairBadge from '../PairBadge';

export default function PairUniverseViewer({ params }: { params?: any }) {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState('selected');
  const [searchTerm, setSearchTerm] = useState('');
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortAsc, setSortAsc] = useState(true);

  const toggleSort = (key: string) => {
    if (sortKey === key) {
      if (!sortAsc) { setSortKey(null); setSortAsc(true); } // 3rd click resets
      else setSortAsc(false);
    } else { setSortKey(key); setSortAsc(true); }
  };
  const sortArrow = (key: string) => sortKey === key ? (sortAsc ? ' ↑' : ' ↓') : '';

  // Selected pairs from JSON
  const { data: mrptPairs, loading: loadingMrpt } = useApi(() => getPairUniverse('mrpt'), []);
  const { data: mtfsPairs, loading: loadingMtfs } = useApi(() => getPairUniverse('mtfs'), []);

  // DB pairs (loaded when tab clicked)
  const { data: cointData, loading: loadingCoint, error: errorCoint, refetch: refetchCoint } = useApi(() => getPairDb('coint'), []);
  const { data: similarData, loading: loadingSimilar } = useApi(() => getPairDb('similar'), []);
  const { data: pcaData, loading: loadingPca } = useApi(() => getPairDb('pca'), []);

  const isLoading = activeTab === 'selected' ? (loadingMrpt || loadingMtfs) :
    activeTab === 'coint' ? loadingCoint :
    activeTab === 'similar' ? loadingSimilar : loadingPca;

  if (isLoading) return <LoadingState />;

  // Build current tab data
  let currentPairs: any[] = [];
  if (activeTab === 'selected') {
    const mrpt = (mrptPairs || []).map((p: any) => ({ ...p, pair: `${p.s1}/${p.s2}`, strategy: 'MRPT', selected: true }));
    const mtfs = (mtfsPairs || []).map((p: any) => ({ ...p, pair: `${p.s1}/${p.s2}`, strategy: 'MTFS', selected: true }));
    currentPairs = [...mrpt, ...mtfs];
  } else if (activeTab === 'coint') {
    currentPairs = cointData?.pairs || [];
  } else if (activeTab === 'similar') {
    currentPairs = similarData?.pairs || [];
  } else {
    currentPairs = pcaData?.pairs || [];
  }

  let filteredPairs = currentPairs.filter((p: any) =>
    (p.pair || `${p.s1}/${p.s2}`).toLowerCase().includes(searchTerm.toLowerCase())
  );
  if (sortKey) {
    filteredPairs = [...filteredPairs].sort((a: any, b: any) => {
      if (sortKey === 'pair') {
        const va = (a.pair || `${a.s1}/${a.s2}`).toLowerCase();
        const vb = (b.pair || `${b.s1}/${b.s2}`).toLowerCase();
        return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
      }
      if (sortKey === 'selected') {
        return sortAsc ? (a.selected ? -1 : 1) : (a.selected ? 1 : -1);
      }
      return 0;
    });
  }

  const tabs = [
    { id: 'selected', label: t('pairUniverse.selected', { count: (mrptPairs || []).length + (mtfsPairs || []).length }) },
    { id: 'coint', label: t('pairUniverse.cointegrated', { count: cointData?.total || '...' }) },
    { id: 'similar', label: t('pairUniverse.similar', { count: similarData?.total || '...' }) },
    { id: 'pca', label: t('pairUniverse.pca', { count: pcaData?.total || '...' }) },
  ];

  return (
    <div className="flex flex-col h-full">
      <div className="flex gap-2 mb-4 overflow-x-auto shrink-0">
        {tabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`px-3 py-1.5 text-xs font-medium rounded-md whitespace-nowrap transition-colors ${activeTab === tab.id ? 'bg-[var(--accent-primary)] text-white' : 'bg-[var(--bg-primary)] text-[var(--text-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)]'}`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="relative mb-4 shrink-0">
        <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-[var(--text-muted)]" />
        <input
          type="text"
          placeholder={t('pairUniverse.searchPairs')}
          value={searchTerm}
          onChange={e => setSearchTerm(e.target.value)}
          className="w-full bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md pl-9 pr-3 py-2 text-sm focus:outline-none focus:border-[var(--accent-primary)] transition-colors text-[var(--text-primary)]"
        />
      </div>

      <div className="flex-1 overflow-y-auto border border-[var(--border-subtle)] rounded-md bg-[var(--bg-primary)]">
        <table className="w-full text-sm text-left">
          <thead className="text-[10px] text-[var(--text-muted)] uppercase bg-[var(--bg-secondary)] sticky top-0 z-10">
            <tr>
              <th className="px-4 py-3 font-medium cursor-pointer hover:text-[var(--text-primary)]" onClick={() => toggleSort('pair')}>{t('common.pair')}{sortArrow('pair')}</th>
              {activeTab === 'selected' && <th className="px-4 py-3 font-medium">{t('common.strategy')}</th>}
              {activeTab === 'selected' && <th className="px-4 py-3 font-medium">{t('common.sector')}</th>}
              <th className="px-4 py-3 font-medium cursor-pointer hover:text-[var(--text-primary)]" onClick={() => toggleSort('selected')}>{t('common.status')}{sortArrow('selected')}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--border-subtle)]">
            {filteredPairs.map((p: any, i: number) => (
              <tr key={i} className={`hover:bg-[var(--bg-secondary)] transition-colors ${p.selected ? 'bg-[var(--accent-primary)]/5' : ''}`}>
                <td className="px-4 py-3"><PairBadge s1={p.s1} s2={p.s2} strategy={p.strategy?.toLowerCase()} compact /></td>
                {activeTab === 'selected' && <td className="px-4 py-3 text-[var(--text-secondary)] text-xs">{p.strategy}</td>}
                {activeTab === 'selected' && <td className="px-4 py-3 text-[var(--text-secondary)] text-xs">{p.sector || '—'}</td>}
                <td className="px-4 py-3">
                  {p.selected ? (
                    <span className="px-2 py-1 text-[10px] font-medium bg-[var(--success)]/10 text-[var(--success)] rounded border border-[var(--success)]/20">{t('common.selected')}</span>
                  ) : (
                    <span className="px-2 py-1 text-[10px] font-medium bg-[var(--bg-tertiary)] text-[var(--text-muted)] rounded border border-[var(--border-subtle)]">{t('common.available')}</span>
                  )}
                </td>
              </tr>
            ))}
            {filteredPairs.length === 0 && (
              <tr>
                <td colSpan={4} className="px-4 py-8 text-center text-[var(--text-muted)] text-sm">
                  {searchTerm ? `No pairs found matching "${searchTerm}"` : t('common.noDataAvailable')}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
