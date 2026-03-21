import React from 'react';
import { useTranslation } from 'react-i18next';
import PairBadge from './PairBadge';

export const PairCard = ({ pair, zScore, pnl, status }: { pair: string, zScore: string, pnl: string, status: string }) => {
  const { t } = useTranslation();
  return (
    <div className="pair-card flex flex-col gap-2">
      <div className="flex justify-between items-center">
        <PairBadge pair={pair} compact noPopover />
        <span className={`text-[11px] px-2 py-0.5 rounded-full ${status === 'active' ? 'bg-[var(--accent-primary)]/20 text-[var(--accent-primary)]' : 'bg-[var(--bg-secondary)] text-[var(--text-muted)]'}`}>
          {status}
        </span>
      </div>
      <div className="flex justify-between text-sm">
        <span className="text-[var(--text-secondary)]">{t('financial.zScore')}</span>
        <span className="font-mono">{zScore}</span>
      </div>
      <div className="flex justify-between text-sm">
        <span className="text-[var(--text-secondary)]">{t('financial.pnl')}</span>
        <span className={`font-mono ${pnl.startsWith('+') ? 'text-[var(--success)]' : 'text-[var(--error)]'}`}>{pnl}</span>
      </div>
    </div>
  );
};

export const InventoryBox = ({ direction, shares, price, pnl, pair }: { direction: string, shares: string, price: string, pnl: string, pair: string, key?: any }) => {
  const { t } = useTranslation();
  return (
    <div className="inventory-box flex flex-col gap-2">
      <div className="flex justify-between items-center">
        <PairBadge pair={pair.replace(' / ', '/')} direction={direction as 'long' | 'short'} compact />
      </div>
      <div className="grid grid-cols-2 gap-2 text-sm mt-2">
        <div>
          <div className="text-[var(--text-muted)] text-xs">{t('common.shares')}</div>
          <div className="font-mono">{shares}</div>
        </div>
        <div>
          <div className="text-[var(--text-muted)] text-xs">{t('financial.openPrice')}</div>
          <div className="font-mono">{price}</div>
        </div>
        <div className="col-span-2 mt-1">
          <div className="text-[var(--text-muted)] text-xs">{t('financial.unrealizedPnl')}</div>
          <div className={`font-mono ${pnl.startsWith('+') ? 'text-[var(--success)]' : 'text-[var(--error)]'}`}>{pnl}</div>
        </div>
      </div>
    </div>
  );
};

export const WFBox = ({ windows }: { windows: Array<{sharpe: string, pnl: string}> }) => {
  const { t } = useTranslation();
  return (
    <div className="wf-box">
      {windows.map((w, i) => (
        <div key={i} className="bg-[var(--bg-tertiary)] p-3 rounded-md flex flex-col gap-1">
          <span className="text-xs text-[var(--text-muted)]">{t('financial.windowN', { n: i + 1 })}</span>
          <span className="font-mono text-sm">{t('financial.sr')} {w.sharpe}</span>
          <span className={`font-mono text-sm ${w.pnl.startsWith('+') ? 'text-[var(--success)]' : 'text-[var(--error)]'}`}>{w.pnl}</span>
        </div>
      ))}
    </div>
  );
};
