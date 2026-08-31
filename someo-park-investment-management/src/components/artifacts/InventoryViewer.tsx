import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { subsectorName } from '../../i18n/subsectors';
import { useApi } from '../../hooks/useApi';
import { getInventory } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import PairBadge from '../PairBadge';

export default function InventoryViewer({ params }: { params?: any }) {
  const { t } = useTranslation();
  const [strategy, setStrategy] = useState(params?.strategy || 'mrpt');
  const { data, loading, error, refetch } = useApi(() => getInventory(strategy), [strategy]);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!data) return null;

  // ══ AISS/AEUS MODE — stock-level holdings grouped by subsector (subsector = label only) ══
  if (strategy === 'aiss' || strategy === 'aeus') {
    const subWeights = data.holdings || {};           // subsector -> {weight}
    const sh = data.stock_holdings || {};             // ticker -> {subsectors, shares, ...}
    const groups: Record<string, any[]> = {};
    Object.entries(sh).forEach(([ticker, h]: any) => {
      const sub = (h.subsectors && h.subsectors[0]) || 'other';
      (groups[sub] = groups[sub] || []).push({ ticker, ...h });
    });
    const orderedSubs = Object.keys(groups).sort((a, b) => (subWeights[b]?.weight || 0) - (subWeights[a]?.weight || 0));
    return (
      <div className="flex flex-col h-full">
        <div className="flex items-center justify-between mb-4 shrink-0">
          <div className="text-sm font-medium text-[var(--text-primary)]">{t('inventory.title', { strategy: strategy.toUpperCase() })}</div>
          <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
            {['mrpt', 'mtfs', 'ssrs', 'aiss', 'aeus'].map(s => (
              <button key={s} onClick={() => setStrategy(s)} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === s ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>{s.toUpperCase()}</button>
            ))}
          </div>
        </div>
        <div className="grid grid-cols-2 gap-4 mb-4 shrink-0">
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
            <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('inventory.asOfDate')}</div>
            <div className="text-sm font-mono text-[var(--text-primary)]">{data.as_of}</div>
          </div>
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
            <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('inventory.baseCapital')}</div>
            <div className="text-sm font-mono text-[var(--text-primary)]">${Number(data.capital).toLocaleString()}</div>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto space-y-4">
          {orderedSubs.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-center py-16">
              <div className="text-sm text-[var(--text-muted)]">{t('inventory.noHoldings', '暂无持仓')}</div>
              {data.note && <div className="text-xs text-[var(--text-muted)] mt-2 font-mono">{data.note}</div>}
            </div>
          )}
          {orderedSubs.map(sub => (
            <div key={sub}>
              <div className="flex items-center justify-between mb-2 px-1">
                <span className="font-mono text-xs font-bold uppercase tracking-wider text-[var(--text-primary)]">{subsectorName(sub, t)}</span>
                <span className="font-mono text-xs font-bold text-[var(--accent-primary)]">{((subWeights[sub]?.weight || 0) * 100).toFixed(1)}% {t(`${strategy}.subsectorLabel`)}</span>
              </div>
              <div className="space-y-2">
                {groups[sub].sort((a, b) => (b.portfolio_weight || 0) - (a.portfolio_weight || 0)).map((h: any) => (
                  <div key={h.ticker} className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-3">
                    <div className="flex items-center justify-between mb-2">
                      <PairBadge pair={h.ticker} direction="long" strategy={strategy} compact
                        details={{ weight: h.portfolio_weight, shares: h.shares, lastPrice: h.last_price }} />
                      <span className="font-mono text-xs text-[var(--text-secondary)]">{((h.portfolio_weight || 0) * 100).toFixed(1)}%</span>
                    </div>
                    <div className="grid grid-cols-3 gap-2 text-xs">
                      <div><span className="text-[var(--text-muted)]">{t('ssrs.shares')}</span><br/><span className="font-mono">{h.shares?.toLocaleString()}</span></div>
                      <div><span className="text-[var(--text-muted)]">{t('ssrs.price')}</span><br/><span className="font-mono">${h.last_price?.toFixed(2)}</span></div>
                      <div><span className="text-[var(--text-muted)]">{t(`${strategy}.colValue`)}</span><br/><span className="font-mono">${Math.round(h.target_value || 0).toLocaleString()}</span></div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
          {orderedSubs.length === 0 && <div className="text-center text-[var(--text-muted)] py-8">{t('ssrs.noPositions')}</div>}
        </div>
      </div>
    );
  }

  // ══ SR MODE ══
  if (strategy === 'ssrs') {
    const holdings = Object.entries(data.holdings || {}).filter(([, h]: any) => (h as any).weight > 0.01);
    return (
      <div className="flex flex-col h-full">
        <div className="flex items-center justify-between mb-4 shrink-0">
          <div className="text-sm font-medium text-[var(--text-primary)]">{t('ssrs.inventoryTitle')}</div>
          <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
            {['mrpt', 'mtfs', 'ssrs', 'aiss', 'aeus'].map(s => (
              <button key={s} onClick={() => setStrategy(s)} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === s ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>{s.toUpperCase()}</button>
            ))}
          </div>
        </div>
        <div className="grid grid-cols-2 gap-4 mb-4 shrink-0">
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
            <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('inventory.asOfDate')}</div>
            <div className="text-sm font-mono text-[var(--text-primary)]">{data.as_of}</div>
          </div>
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
            <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('inventory.baseCapital')}</div>
            <div className="text-sm font-mono text-[var(--text-primary)]">${Number(data.capital).toLocaleString()}</div>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto space-y-3">
          {holdings.map(([ticker, h]: any) => (
            <div key={ticker} className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-4">
              <div className="flex items-center justify-between mb-2">
                <PairBadge pair={ticker} direction="long" strategy="ssrs" compact
                  details={{ weight: h.weight, shares: h.shares, costBasis: h.cost_basis, lastPrice: h.last_price, openDate: h.entry_date, daysHeld: h.days_held }} />
              </div>
              <div className="grid grid-cols-3 gap-2 text-xs">
                <div><span className="text-[var(--text-muted)]">{t('ssrs.weight')}</span><br/><span className="font-mono">{(h.weight * 100).toFixed(1)}%</span></div>
                <div><span className="text-[var(--text-muted)]">{t('ssrs.shares')}</span><br/><span className="font-mono">{h.shares?.toLocaleString()}</span></div>
                <div><span className="text-[var(--text-muted)]">{t('ssrs.price')}</span><br/><span className="font-mono">${h.last_price?.toFixed(2)}</span></div>
                <div><span className="text-[var(--text-muted)]">{t('ssrs.costBasis')}</span><br/><span className="font-mono">${h.cost_basis?.toFixed(2)}</span></div>
                <div><span className="text-[var(--text-muted)]">{t('ssrs.entry')}</span><br/><span className="font-mono">{h.entry_date}</span></div>
                <div><span className="text-[var(--text-muted)]">{t('ssrs.daysHeld')}</span><br/><span className="font-mono">{h.days_held}</span></div>
              </div>
            </div>
          ))}
          {holdings.length === 0 && <div className="text-center text-[var(--text-muted)] py-8">{t('ssrs.noPositions')}</div>}
        </div>
      </div>
    );
  }

  // ══ MRPT/MTFS MODE — unchanged ══
  const activePairs = Object.entries(data.pairs || {}).filter(([, p]: any) => (p as any).direction !== null);

  const calcDaysHeld = (openDate: string) => {
    if (!openDate) return 0;
    const diff = Date.now() - new Date(openDate).getTime();
    return Math.max(0, Math.floor(diff / 86400000));
  };

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)]">{t('inventory.title', { strategy: strategy.toUpperCase() })}</div>
        <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
          {['mrpt', 'mtfs', 'ssrs', 'aiss', 'aeus'].map(s => (
            <button key={s} onClick={() => setStrategy(s)} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === s ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
              {s.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-4 shrink-0">
        <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
          <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('inventory.asOfDate')}</div>
          <div className="text-sm font-mono text-[var(--text-primary)]">{data.as_of}</div>
        </div>
        <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
          <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('inventory.baseCapital')}</div>
          <div className="text-sm font-mono text-[var(--text-primary)]">${Number(data.capital).toLocaleString()}</div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto space-y-3">
        {activePairs.length === 0 && (
          <div className="text-sm text-[var(--text-muted)] text-center py-8">{t('inventory.noActivePositions')}</div>
        )}
        {activePairs.map(([pairKey, pos]: any) => (
          <div key={pairKey} className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-4">
            <div className="flex justify-between items-center mb-3 pb-2 border-b border-[var(--border-subtle)]">
              <PairBadge
                pair={pairKey}
                direction={pos.direction}
                strategy={strategy}
                details={{
                  openDate: pos.open_date,
                  daysHeld: calcDaysHeld(pos.open_date),
                  s1Shares: pos.s1_shares,
                  s2Shares: pos.s2_shares,
                  s1Price: pos.open_s1_price,
                  s2Price: pos.open_s2_price,
                  hedgeRatio: pos.open_hedge_ratio,
                  paramSet: pos.param_set,
                  zScore: pos.open_signal?.z_score,
                  momentumSpread: pos.open_signal?.momentum_spread,
                }}
              />
            </div>
            <div className="grid grid-cols-2 gap-y-3 gap-x-4 text-xs">
              <div>
                <span className="text-[var(--text-muted)] block mb-0.5">{t('inventory.sharesS1S2')}</span>
                <span className="font-mono text-[var(--text-primary)]">{pos.s1_shares} / {pos.s2_shares}</span>
              </div>
              <div>
                <span className="text-[var(--text-muted)] block mb-0.5">{t('inventory.openDate')}</span>
                <span className="font-mono text-[var(--text-primary)]">{pos.open_date}</span>
              </div>
              <div>
                <span className="text-[var(--text-muted)] block mb-0.5">{t('inventory.openPriceS1S2')}</span>
                <span className="font-mono text-[var(--text-primary)]">${pos.open_s1_price?.toFixed(2)} / ${pos.open_s2_price?.toFixed(2)}</span>
              </div>
              <div>
                <span className="text-[var(--text-muted)] block mb-0.5">{t('inventory.daysHeld')}</span>
                <span className="font-mono text-[var(--text-primary)]">{calcDaysHeld(pos.open_date)}</span>
              </div>
              <div>
                <span className="text-[var(--text-muted)] block mb-0.5">{t('inventory.paramSet')}</span>
                <span className="text-[var(--text-secondary)]">{pos.param_set}</span>
              </div>
              <div>
                <span className="text-[var(--text-muted)] block mb-0.5">{t('inventory.zScoreEntry')}</span>
                <span className="font-mono text-[var(--text-primary)]">{pos.open_signal?.z_score?.toFixed(2) ?? 'N/A'}</span>
              </div>
              {pos.wf_source && (
                <div className="col-span-2">
                  <span className="text-[var(--text-muted)] block mb-0.5">{t('inventory.wfSource')}</span>
                  <span className="text-[var(--text-secondary)]">{pos.wf_source.default_window} ({pos.wf_source.wf_dir})</span>
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
