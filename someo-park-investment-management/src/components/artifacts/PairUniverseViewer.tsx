import React, { useState } from 'react';
import { Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { subsectorName } from '../../i18n/subsectors';
import { useApi } from '../../hooks/useApi';
import { getPairUniverse, getPairDb } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import PairBadge from '../PairBadge';

/** Format an ISO timestamp or date string to compact local display */
/** Format to date-only YYYY-MM-DD (no hours/minutes) */
function fmtDate(raw: string | undefined | null): string {
  if (!raw) return '';
  try {
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw;
    const d = new Date(raw);
    if (isNaN(d.getTime())) return raw;
    return d.toLocaleDateString('en-CA'); // YYYY-MM-DD
  } catch { return raw; }
}

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

  // Selected pairs from JSON (new format: { pairs, updated_at })
  const { data: mrptData, loading: loadingMrpt } = useApi(() => getPairUniverse('mrpt'), []);
  const { data: mtfsData, loading: loadingMtfs } = useApi(() => getPairUniverse('mtfs'), []);
  const mrptPairs = mrptData?.pairs ?? (Array.isArray(mrptData) ? mrptData : []);
  const mtfsPairs = mtfsData?.pairs ?? (Array.isArray(mtfsData) ? mtfsData : []);
  // SR: sector ETF holdings (always loaded for Sector ETF tab)
  const { data: srSectors, loading: loadingSR } = useApi(() => getPairUniverse('ssrs'), []);
  // AISS: stock-level holdings grouped by subsector (subsector is grouping only, not tradable)
  const { data: aissStocks, loading: loadingAiss } = useApi(() => getPairUniverse('aiss'), []);

  // DB pairs (loaded when tab clicked)
  const { data: cointData, loading: loadingCoint, error: errorCoint, refetch: refetchCoint } = useApi(() => getPairDb('coint'), []);
  const { data: similarData, loading: loadingSimilar } = useApi(() => getPairDb('similar'), []);
  const { data: pcaData, loading: loadingPca } = useApi(() => getPairDb('pca'), []);

  const isLoading = activeTab === 'sector_etf' ? loadingSR :
    activeTab === 'aiss_stock' ? loadingAiss :
    activeTab === 'selected' ? (loadingMrpt || loadingMtfs || loadingSR) :
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

  // Count selected/total for each tab
  const selCount = mrptPairs.length + mtfsPairs.length;
  const cointSelCount = (cointData?.pairs || []).filter((p: any) => p.selected).length;
  const similarSelCount = (similarData?.pairs || []).filter((p: any) => p.selected).length;
  const pcaSelCount = (pcaData?.pairs || []).filter((p: any) => p.selected).length;
  const heldCount = (srSectors?.sectors || []).filter((s: any) => s.held).length;
  const totalEtfs = (srSectors?.sectors || []).length || 11;

  // Resolve updated_at for selected tab: use the older of mrpt/mtfs
  const selectedUpdatedAt = (() => {
    const a = mrptData?.updated_at;
    const b = mtfsData?.updated_at;
    if (!a && !b) return '';
    if (!a) return b;
    if (!b) return a;
    return a < b ? a : b;
  })();

  const tabMeta: Record<string, string> = {
    selected: fmtDate(selectedUpdatedAt),
    coint: fmtDate(cointData?.day),
    similar: fmtDate(similarData?.day),
    pca: fmtDate(pcaData?.day),
    sector_etf: fmtDate(srSectors?.updated_at),
    aiss_stock: fmtDate(aissStocks?.updated_at),
  };

  const tabs = [
    { id: 'selected', label: t('pairUniverse.tabLabel', { count: selCount }) },
    { id: 'coint', label: t('pairUniverse.cointTab', { selected: cointSelCount, total: cointData?.total || '...' }) },
    { id: 'similar', label: t('pairUniverse.similarTab', { selected: similarSelCount, total: similarData?.total || '...' }) },
    { id: 'pca', label: t('pairUniverse.pcaTab', { selected: pcaSelCount, total: pcaData?.total || '...' }) },
    { id: 'sector_etf', label: t('pairUniverse.sectorEtfTab', { selected: heldCount, total: totalEtfs }) },
    { id: 'aiss_stock', label: t('aiss.stocksTab', { count: aissStocks?.n_stocks || 0 }) },
  ];

  return (
    <div className="flex flex-col h-full">
      <div className="flex gap-2 mb-2 overflow-x-auto shrink-0">
        {tabs.map((tab: any) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`px-3 py-1.5 text-xs font-medium rounded-md whitespace-nowrap transition-colors ${
              activeTab === tab.id ? 'bg-[var(--accent-primary)] text-white' :
              'bg-[var(--bg-primary)] text-[var(--text-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)]'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {tabMeta[activeTab] && (
        <div className="text-[10px] text-[var(--text-muted)] mb-3 shrink-0 font-mono">
          {t('pairUniverse.updatedAt')} {tabMeta[activeTab]}
        </div>
      )}

      {activeTab === 'aiss_stock' ? (
        /* ── AISS stock content: subsector group header + tradable stock rows ── */
        <>
          {aissStocks?.param_set && (
            <div className="text-xs text-[var(--text-muted)] mb-3 shrink-0">
              {t('ssrs.paramVersion', { param: aissStocks.param_set, version: aissStocks?.signal_version || 'v1' })}
            </div>
          )}
          <div className="flex-1 overflow-y-auto border border-[var(--border-subtle)] rounded-md bg-[var(--bg-primary)]">
            <table className="w-full text-sm text-left">
              <thead className="text-[10px] text-[var(--text-muted)] uppercase bg-[var(--bg-secondary)] sticky top-0 z-10">
                <tr>
                  <th className="px-4 py-3 font-medium">{t('aiss.colStock')}</th>
                  <th className="px-4 py-3 font-medium text-right">{t('ssrs.shares')}</th>
                  <th className="px-4 py-3 font-medium text-right">{t('ssrs.weight')}</th>
                  <th className="px-4 py-3 font-medium text-right">{t('aiss.colLastPrice')}</th>
                  <th className="px-4 py-3 font-medium text-right">{t('aiss.colMarketValue')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {(aissStocks?.subsectors || []).map((grp: any) => [
                  /* subsector group header (grouping label + weight; NOT a tradable row) */
                  <tr key={`grp-${grp.subsector}`} className="bg-[var(--bg-secondary)]">
                    <td className="px-4 py-2 font-mono text-xs font-bold text-[var(--text-primary)]" colSpan={2}>
                      {subsectorName(grp.subsector, t) || grp.display}
                      {!grp.held && <span className="ml-2 px-1.5 py-0.5 text-[9px] font-medium bg-[var(--bg-tertiary)] text-[var(--text-muted)] rounded border border-[var(--border-subtle)] uppercase tracking-wider">{t('common.available')}</span>}
                    </td>
                    <td className={`px-4 py-2 text-right font-mono text-xs font-bold ${grp.held ? 'text-[var(--accent-primary)]' : 'text-[var(--text-muted)]'}`} colSpan={3}>
                      {(grp.weight * 100).toFixed(1)}% {t('aiss.subsectorLabel')}
                    </td>
                  </tr>,
                  /* tradable individual stocks within the subsector (reserve / 0% dimmed) */
                  ...(grp.stocks || []).map((s: any) => (
                    <tr key={`${grp.subsector}-${s.ticker}`} className={`hover:bg-[var(--bg-secondary)] ${s.weight > 0.0001 ? '' : 'opacity-45'}`}>
                      <td className="px-4 py-3 pl-8">
                        <PairBadge pair={s.ticker} direction={s.weight > 0.0001 ? 'long' : null} strategy="aiss" compact details={{ weight: s.weight, shares: s.shares }} />
                        {s.tier_role === 'reserve' && <span className="ml-2 text-[9px] font-mono text-[var(--text-muted)] uppercase">reserve</span>}
                      </td>
                      <td className="px-4 py-3 text-right font-mono">{s.shares || '—'}</td>
                      <td className="px-4 py-3 text-right font-mono">{(s.weight * 100).toFixed(1)}%</td>
                      <td className="px-4 py-3 text-right font-mono">${s.last_price?.toFixed(2)}</td>
                      <td className="px-4 py-3 text-right font-mono">{s.target_value ? '$' + Math.round(s.target_value).toLocaleString() : '—'}</td>
                    </tr>
                  ))
                ])}
                {(!aissStocks?.subsectors || aissStocks.subsectors.length === 0) && (
                  <tr><td colSpan={5} className="px-4 py-8 text-center text-[var(--text-muted)] text-sm">{t('common.noDataAvailable')}</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      ) : activeTab === 'sector_etf' ? (
        /* ── Sector ETF tab content ── */
        <>
          {srSectors?.param_set && (
            <div className="text-xs text-[var(--text-muted)] mb-3 shrink-0">
              {t('ssrs.paramVersion', { param: srSectors.param_set, version: srSectors?.signal_version || 'v1' })}
            </div>
          )}
          <div className="flex-1 overflow-y-auto border border-[var(--border-subtle)] rounded-md bg-[var(--bg-primary)]">
            <table className="w-full text-sm text-left">
              <thead className="text-[10px] text-[var(--text-muted)] uppercase bg-[var(--bg-secondary)] sticky top-0 z-10">
                <tr>
                  <th className="px-4 py-3 font-medium">{t('ssrs.etf')}</th>
                  <th className="px-4 py-3 font-medium text-right">{t('ssrs.weight')}</th>
                  <th className="px-4 py-3 font-medium">{t('ssrs.entryDate')}</th>
                  <th className="px-4 py-3 font-medium text-right">{t('ssrs.costBasis')}</th>
                  <th className="px-4 py-3 font-medium">{t('common.status')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border-subtle)]">
                {(srSectors?.sectors || []).map((s: any) => (
                  <tr key={s.ticker} className={`hover:bg-[var(--bg-secondary)] ${s.held ? 'bg-[var(--accent-primary)]/5' : 'opacity-50'}`}>
                    <td className="px-4 py-3"><PairBadge pair={s.ticker} direction={s.held ? 'long' : null} strategy="ssrs" compact details={s.held ? { weight: s.weight, shares: s.shares, costBasis: s.cost_basis, openDate: s.entry_date } : undefined} /></td>
                    <td className="px-4 py-3 text-right font-mono">{s.held ? (s.weight * 100).toFixed(1) + '%' : '—'}</td>
                    <td className="px-4 py-3 text-[var(--text-secondary)] text-xs">{s.held ? (s.entry_date || '—') : '—'}</td>
                    <td className="px-4 py-3 text-right font-mono">{s.held ? '$' + (s.cost_basis?.toFixed(2) || '—') : '—'}</td>
                    <td className="px-4 py-3">
                      {s.held ? (
                        <span className="px-2 py-1 text-[10px] font-medium bg-[var(--success)]/10 text-[var(--success)] rounded border border-[var(--success)]/20">{t('common.long')}</span>
                      ) : (
                        <span className="px-2 py-1 text-[10px] font-medium bg-[var(--bg-tertiary)] text-[var(--text-muted)] rounded border border-[var(--border-subtle)]">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        /* ── Pair tabs content (Selected/Coint/Similar/PCA) ── */
        <>
          <div className="relative mb-4 shrink-0">
            <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-[var(--text-muted)]" />
            <input type="text" placeholder={t('pairUniverse.searchPairs')} value={searchTerm}
              onChange={e => setSearchTerm(e.target.value)}
              className="w-full bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md pl-9 pr-3 py-2 text-sm focus:outline-none focus:border-[var(--accent-primary)] transition-colors text-[var(--text-primary)]" />
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
                  <tr><td colSpan={4} className="px-4 py-8 text-center text-[var(--text-muted)] text-sm">
                    {searchTerm ? `No pairs found matching "${searchTerm}"` : t('common.noDataAvailable')}
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
