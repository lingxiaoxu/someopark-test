import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
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
          {['mrpt', 'mtfs'].map(s => (
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
