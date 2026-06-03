import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Clock, ChevronDown, ChevronRight, TrendingUp, TrendingDown } from 'lucide-react';
import { useApi } from '../../hooks/useApi';
import { getInventoryHistory, getInventorySnapshot } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import PairBadge from '../PairBadge';

function fmtNum(v: number | null | undefined, decimals = 2): string {
  if (v == null || isNaN(v)) return '—';
  const abs = Math.abs(v);
  const sign = v < 0 ? '-' : '';
  return sign + abs.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function calcDaysHeld(openDate: string, asOf?: string): number {
  if (!openDate) return 0;
  const end = asOf ? new Date(asOf).getTime() : Date.now();
  return Math.max(0, Math.floor((end - new Date(openDate).getTime()) / 86400000));
}

function PnlBadge({ pnl, pct }: { pnl?: number; pct?: number }) {
  if (pnl == null) return null;
  const positive = pnl >= 0;
  const color = positive ? 'var(--success)' : 'var(--error)';
  return (
    <div className="flex items-center gap-1.5">
      {positive ? <TrendingUp className="w-3 h-3" style={{ color }} /> : <TrendingDown className="w-3 h-3" style={{ color }} />}
      <span className="text-xs font-mono" style={{ color }}>${fmtNum(pnl)}</span>
      {pct != null && <span className="text-[10px] font-mono" style={{ color }}>({pct >= 0 ? '+' : ''}{pct.toFixed(2)}%)</span>}
    </div>
  );
}

// ══ SSRS Sector Holding Detail ══
function SectorDetail({ ticker, holding, asOf }: { ticker: string; holding: any; asOf?: string; key?: any }) {
  const { t } = useTranslation();
  const pnlPerShare = (holding.last_price || 0) - (holding.cost_basis || 0);
  const totalPnl = pnlPerShare * (holding.shares || 0);
  const pnlPct = holding.cost_basis ? (pnlPerShare / holding.cost_basis) * 100 : 0;
  const daysHeld = calcDaysHeld(holding.entry_date, asOf);

  return (
    <div className="bg-[var(--bg-secondary)] rounded-lg p-3 space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <PairBadge pair={ticker} direction="long" strategy="ssrs" compact
            details={{ weight: holding.weight, shares: holding.shares, costBasis: holding.cost_basis, lastPrice: holding.last_price, openDate: holding.entry_date, daysHeld: daysHeld, unrealizedPnl: totalPnl, unrealizedPnlPct: pnlPct }} />
          <span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded ${
            holding.action_today === 'ENTER' ? 'bg-green-500/10 text-green-400' :
            holding.action_today === 'EXIT' ? 'bg-[var(--error)]/10 text-[var(--error)]' :
            'bg-blue-500/10 text-blue-400'
          }`}>{holding.action_today || 'HOLD'}</span>
          <span className="text-[10px] text-[var(--text-muted)]">{(holding.weight * 100).toFixed(1)}% weight</span>
        </div>
        <PnlBadge pnl={totalPnl} pct={pnlPct} />
      </div>
      <div className="grid grid-cols-5 gap-2 text-[10px]">
        <div>
          <div className="text-[var(--text-muted)] uppercase">{t('ssrs.shares')}</div>
          <div className="text-[var(--text-primary)] font-mono">{(holding.shares || 0).toLocaleString()}</div>
        </div>
        <div>
          <div className="text-[var(--text-muted)] uppercase">{t('ssrs.costBasis')}</div>
          <div className="text-[var(--text-primary)] font-mono">${fmtNum(holding.cost_basis)}</div>
        </div>
        <div>
          <div className="text-[var(--text-muted)] uppercase">{t('ssrs.price')}</div>
          <div className="text-[var(--text-primary)] font-mono">${fmtNum(holding.last_price)}</div>
        </div>
        <div>
          <div className="text-[var(--text-muted)] uppercase">{t('ssrs.entry')}</div>
          <div className="text-[var(--text-primary)] font-mono">{holding.entry_date || '—'}</div>
        </div>
        <div>
          <div className="text-[var(--text-muted)] uppercase">{t('ssrs.daysHeld')}</div>
          <div className="text-[var(--text-primary)] font-mono">{daysHeld}d</div>
        </div>
      </div>
    </div>
  );
}

// ══ SSRS Snapshot Expanded View ══
function SsrsSnapshotDetail({ data }: { data: any }) {
  const { t } = useTranslation();
  const holdings = data.holdings || {};
  const activeHoldings = Object.entries(holdings).filter(([, h]: any) => (h as any).weight > 0.01);
  const inactiveETFs = Object.entries(holdings).filter(([, h]: any) => (h as any).weight <= 0.01);

  // Calculate total portfolio value & PnL
  const totalValue = activeHoldings.reduce((sum, [, h]: any) => sum + (h.shares || 0) * (h.last_price || 0), 0);
  const totalCost = activeHoldings.reduce((sum, [, h]: any) => sum + (h.shares || 0) * (h.cost_basis || 0), 0);
  const totalPnl = totalValue - totalCost;
  const totalPnlPct = totalCost > 0 ? (totalPnl / totalCost) * 100 : 0;

  return (
    <div className="space-y-3">
      {/* Summary row */}
      <div className="flex items-center gap-4 text-[11px] text-[var(--text-muted)] flex-wrap">
        <span>{t('inventoryHistory.asOf')} <span className="font-mono text-[var(--text-primary)]">{data.as_of}</span></span>
        <span>{t('inventoryHistory.capital')} <span className="font-mono text-[var(--text-primary)]">${Number(data.capital).toLocaleString()}</span></span>
        <span>{t('ssrs.cash')}: <span className="font-mono text-[var(--text-primary)]">{((data.cash_weight || 0) * 100).toFixed(1)}%</span></span>
        <span>{t('ssrs.regime')}: <span className="font-mono text-[var(--text-primary)]">{data.rebalance_history?.[data.rebalance_history.length - 1]?.regime || '—'}</span></span>
        {data.emergency_mode_active && <span className="text-[var(--error)] font-bold">{t('ssrs.emergencyMode')}</span>}
        <PnlBadge pnl={totalPnl} pct={totalPnlPct} />
      </div>

      {/* Active holdings */}
      {activeHoldings
        .sort(([, a]: any, [, b]: any) => (b as any).weight - (a as any).weight)
        .map(([ticker, h]: any) => (
          <SectorDetail key={ticker} ticker={ticker} holding={h} asOf={data.as_of} />
        ))}

      {/* Inactive ETFs */}
      {inactiveETFs.length > 0 && (
        <div className="text-[10px] text-[var(--text-muted)] pt-1 flex flex-wrap items-center gap-1">
          <span>{t('ssrs.notHeld')}:</span>
          {inactiveETFs.map(([k]) => (
            <span key={k}><PairBadge pair={k} strategy="ssrs" compact noPopover /></span>
          ))}
        </div>
      )}

      {/* Rebalance history */}
      {data.rebalance_history?.length > 0 && (
        <div className="text-[10px] text-[var(--text-muted)] pt-1 border-t border-[var(--border-subtle)]">
          <span className="uppercase tracking-wider">{t('ssrs.rebalanceHistory')}: </span>
          {data.rebalance_history.map((r: any, i: number) => (
            <span key={i} className="font-mono">
              {r.date} ({r.reason}){i < data.rebalance_history.length - 1 ? ' → ' : ''}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// AISS snapshot: stock-level grouped by subsector (subsector = grouping label only)
// ══ AISS subsector card: header = subsector (action/weight/total PnL);
//    each STOCK is a SectorDetail-style row with its own cost basis / price /
//    entry / days held / PnL (all stock-level). ══
function AissStockRow({ s, asOf }: { s: any; asOf?: string }) {
  const { t } = useTranslation();
  const pnlPerShare = (s.last_price || 0) - (s.cost_basis || 0);
  const totalPnl = pnlPerShare * (s.shares || 0);
  const pnlPct = s.cost_basis ? (pnlPerShare / s.cost_basis) * 100 : 0;
  const daysHeld = s.days_held ?? calcDaysHeld(s.entry_date, asOf);
  return (
    <div className="bg-[var(--bg-primary)] rounded-md p-2 space-y-1.5 border border-[var(--border-subtle)]">
      <div className="flex items-center justify-between">
        <PairBadge pair={s.ticker} direction="long" strategy="aiss" compact
          details={{ weight: s.weight, shares: s.shares, costBasis: s.cost_basis, lastPrice: s.last_price, openDate: s.entry_date, daysHeld, unrealizedPnl: totalPnl, unrealizedPnlPct: pnlPct }} />
        <PnlBadge pnl={totalPnl} pct={pnlPct} />
      </div>
      <div className="grid grid-cols-6 gap-2 text-[10px]">
        <div><div className="text-[var(--text-muted)] uppercase">{t('ssrs.shares')}</div><div className="text-[var(--text-primary)] font-mono">{(s.shares || 0).toLocaleString()}</div></div>
        <div><div className="text-[var(--text-muted)] uppercase">{t('ssrs.weight')}</div><div className="text-[var(--text-primary)] font-mono">{((s.weight || 0) * 100).toFixed(1)}%</div></div>
        <div><div className="text-[var(--text-muted)] uppercase">{t('ssrs.costBasis')}</div><div className="text-[var(--text-primary)] font-mono">${fmtNum(s.cost_basis)}</div></div>
        <div><div className="text-[var(--text-muted)] uppercase">{t('ssrs.price')}</div><div className="text-[var(--text-primary)] font-mono">${fmtNum(s.last_price)}</div></div>
        <div><div className="text-[var(--text-muted)] uppercase">{t('ssrs.entry')}</div><div className="text-[var(--text-primary)] font-mono">{s.entry_date || '—'}</div></div>
        <div><div className="text-[var(--text-muted)] uppercase">{t('ssrs.daysHeld')}</div><div className="text-[var(--text-primary)] font-mono">{daysHeld}d</div></div>
      </div>
    </div>
  );
}

function AissSubsectorCard({ sub, holding, stocks, asOf }: { sub: string; holding: any; stocks: any[]; asOf?: string }) {
  const act = holding.action_today || 'HOLD';
  // subsector total PnL = sum of per-stock PnL
  const totalPnl = stocks.reduce((acc: number, s: any) => acc + ((s.last_price || 0) - (s.cost_basis || 0)) * (s.shares || 0), 0);
  const totalCost = stocks.reduce((acc: number, s: any) => acc + (s.cost_basis || 0) * (s.shares || 0), 0);
  const pnlPct = totalCost > 0 ? (totalPnl / totalCost) * 100 : 0;
  return (
    <div className="bg-[var(--bg-secondary)] rounded-lg p-3 space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="font-mono text-[11px] font-bold uppercase tracking-wider text-[var(--text-primary)]">{sub}</span>
          <span className={`text-[10px] font-bold uppercase px-1.5 py-0.5 rounded ${
            act === 'ENTER' ? 'bg-green-500/10 text-green-400' :
            act === 'EXIT' ? 'bg-[var(--error)]/10 text-[var(--error)]' :
            'bg-blue-500/10 text-blue-400'
          }`}>{act}</span>
          <span className="text-[10px] text-[var(--text-muted)]">{((holding.weight || 0) * 100).toFixed(1)}% weight</span>
        </div>
        <PnlBadge pnl={totalPnl} pct={pnlPct} />
      </div>
      {/* each individual stock: full cost-basis / price / entry / days / PnL detail */}
      <div className="space-y-1.5">
        {stocks.map((s: any) => <AissStockRow key={s.ticker} s={s} asOf={asOf} />)}
      </div>
    </div>
  );
}

// ══ AISS Snapshot Expanded View (mirrors SSRS, stock-level inside subsector cards) ══
function AissSnapshotDetail({ data }: { data: any }) {
  const { t } = useTranslation();
  const holdings = data.holdings || {};                 // subsector → rich fields
  const view = data.stock_view || { subsectors: [] };
  const stocksBySub: Record<string, any[]> = {};
  (view.subsectors || []).forEach((g: any) => { stocksBySub[g.subsector] = g.stocks || []; });

  const active = Object.entries(holdings).filter(([, h]: any) => (h as any).weight > 0.01);
  const totalValue = active.reduce((s, [, h]: any) => s + (h.shares || 0) * (h.last_price || 0), 0);
  const totalCost = active.reduce((s, [, h]: any) => s + (h.shares || 0) * (h.cost_basis || 0), 0);
  const totalPnl = totalValue - totalCost;
  const totalPnlPct = totalCost > 0 ? (totalPnl / totalCost) * 100 : 0;

  return (
    <div className="space-y-3">
      {/* Summary row */}
      <div className="flex items-center gap-4 text-[11px] text-[var(--text-muted)] flex-wrap">
        <span>{t('inventoryHistory.asOf')} <span className="font-mono text-[var(--text-primary)]">{data.as_of}</span></span>
        <span>{t('inventoryHistory.capital')} <span className="font-mono text-[var(--text-primary)]">${Number(data.capital).toLocaleString()}</span></span>
        <span>{t('ssrs.cash')}: <span className="font-mono text-[var(--text-primary)]">{((data.cash_weight || 0) * 100).toFixed(1)}%</span></span>
        <span>{t('ssrs.regime')}: <span className="font-mono text-[var(--text-primary)]">{data.rebalance_history?.[data.rebalance_history.length - 1]?.regime || '—'}</span></span>
        <PnlBadge pnl={totalPnl} pct={totalPnlPct} />
      </div>

      {/* Held subsector cards (sorted by weight) */}
      {active
        .sort(([, a]: any, [, b]: any) => (b as any).weight - (a as any).weight)
        .map(([sub, h]: any) => (
          <AissSubsectorCard key={sub} sub={sub} holding={h} stocks={stocksBySub[sub] || []} asOf={data.as_of} />
        ))}
      {active.length === 0 && (
        <div className="text-[10px] text-[var(--text-muted)] py-4 text-center">{t('common.noDataAvailable')}</div>
      )}

      {/* Rebalance history */}
      {data.rebalance_history?.length > 0 && (
        <div className="text-[10px] text-[var(--text-muted)] pt-1 border-t border-[var(--border-subtle)]">
          <span className="uppercase tracking-wider">{t('ssrs.rebalanceHistory')}: </span>
          {data.rebalance_history.map((r: any, i: number) => (
            <span key={i} className="font-mono">{r.date} ({r.reason}){i < data.rebalance_history.length - 1 ? ' → ' : ''}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function PairDetail({ pair, pos, asOf }: { pair: string; pos: any; asOf?: string; key?: any }) {
  const { t } = useTranslation();
  const [showLog, setShowLog] = useState(false);
  const latestLog = pos.monitor_log?.length > 0 ? pos.monitor_log[pos.monitor_log.length - 1] : null;
  const daysHeld = calcDaysHeld(pos.open_date, asOf);

  return (
    <div className="bg-[var(--bg-secondary)] rounded-lg p-3 space-y-2">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <PairBadge
            pair={pair}
            direction={pos.direction}
            strategy={pos.strategy}
            compact
            details={{
              openDate: pos.open_date,
              s1Shares: pos.s1_shares,
              s2Shares: pos.s2_shares,
              s1Price: pos.open_s1_price,
              s2Price: pos.open_s2_price,
              hedgeRatio: pos.open_hedge_ratio,
              paramSet: pos.param_set,
              zScore: pos.open_signal?.z_score,
              unrealizedPnl: latestLog?.unrealized_pnl,
              unrealizedPnlPct: latestLog?.unrealized_pnl_pct,
            }}
          />
        </div>
        {latestLog && <PnlBadge pnl={latestLog.unrealized_pnl} pct={latestLog.unrealized_pnl_pct} />}
      </div>

      {/* Entry Details */}
      <div className="grid grid-cols-4 gap-2 text-[10px]">
        <div>
          <div className="text-[var(--text-muted)] uppercase">{t('inventory.openDate')}</div>
          <div className="text-[var(--text-primary)] font-mono">{pos.open_date}</div>
        </div>
        <div>
          <div className="text-[var(--text-muted)] uppercase">{t('inventory.daysHeld')}</div>
          <div className="text-[var(--text-primary)] font-mono">{daysHeld}d</div>
        </div>
        <div>
          <div className="text-[var(--text-muted)] uppercase">{t('inventory.sharesS1S2')}</div>
          <div className="text-[var(--text-primary)] font-mono">{pos.s1_shares} @ ${fmtNum(pos.open_s1_price)}</div>
        </div>
        <div>
          <div className="text-[var(--text-muted)] uppercase">{t('inventory.sharesS1S2')}</div>
          <div className="text-[var(--text-primary)] font-mono">{pos.s2_shares} @ ${fmtNum(pos.open_s2_price)}</div>
        </div>
      </div>

      {/* Entry Signal */}
      {pos.open_signal && (
        <div className="flex items-center gap-3 text-[10px]">
          <span className="text-[var(--text-muted)]">Entry Signal:</span>
          {pos.open_signal.z_score != null && <span className="font-mono text-[var(--text-primary)]">Z={pos.open_signal.z_score.toFixed(3)}</span>}
          {pos.open_signal.entry_threshold != null && <span className="font-mono text-[var(--text-muted)]">entry@{pos.open_signal.entry_threshold.toFixed(3)}</span>}
          {pos.open_signal.exit_threshold != null && <span className="font-mono text-[var(--text-muted)]">exit@{pos.open_signal.exit_threshold.toFixed(3)}</span>}
          {pos.open_hedge_ratio != null && <span className="font-mono text-[var(--text-muted)]">HR={pos.open_hedge_ratio.toFixed(3)}</span>}
        </div>
      )}

      {/* Latest action/note */}
      {latestLog && (
        <div className="flex items-center gap-2 text-[10px]">
          <span className={`font-bold uppercase px-1.5 py-0.5 rounded ${latestLog.action === 'HOLD' ? 'bg-blue-500/10 text-blue-400' : latestLog.action.includes('CLOSE') ? 'bg-[var(--error)]/10 text-[var(--error)]' : 'bg-[var(--text-muted)]/10 text-[var(--text-muted)]'}`}>
            {latestLog.action}
          </span>
          {latestLog.note && <span className="text-[var(--text-muted)] truncate" title={latestLog.note}>{latestLog.note}</span>}
        </div>
      )}

      {/* Monitor Log Toggle */}
      {pos.monitor_log?.length > 0 && (
        <div>
          <button onClick={() => setShowLog(!showLog)} className="text-[10px] text-[var(--accent-primary)] hover:underline flex items-center gap-1">
            {showLog ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            {t('inventoryHistory.monitorLog', { count: pos.monitor_log.length })}
          </button>
          {showLog && (
            <div className="mt-1.5 overflow-x-auto">
              <table className="w-full text-[10px]">
                <thead className="text-[var(--text-muted)] uppercase bg-[var(--bg-primary)]">
                  <tr>
                    <th className="px-2 py-1 text-left">{t('inventoryHistory.date')}</th>
                    <th className="px-2 py-1 text-left">{t('common.action')}</th>
                    <th className="px-2 py-1 text-right">{t('inventoryHistory.zMom')}</th>
                    <th className="px-2 py-1 text-right">{t('inventoryHistory.pnl')}</th>
                    <th className="px-2 py-1 text-right">{t('inventoryHistory.pnlPct')}</th>
                    <th className="px-2 py-1 text-left">{t('common.note')}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-subtle)]">
                  {pos.monitor_log.map((log: any, i: number) => (
                    <tr key={i} className="hover:bg-[var(--bg-primary)]">
                      <td className="px-2 py-1 font-mono">{log.date}</td>
                      <td className="px-2 py-1">
                        <span className={`font-bold ${log.action.includes('CLOSE') ? 'text-[var(--error)]' : 'text-[var(--text-primary)]'}`}>{log.action}</span>
                      </td>
                      <td className="px-2 py-1 text-right font-mono">
                        {log.z_score != null ? log.z_score.toFixed(3) : log.momentum_spread != null ? log.momentum_spread.toFixed(3) : '—'}
                      </td>
                      <td className="px-2 py-1 text-right font-mono" style={{ color: (log.unrealized_pnl ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>
                        ${fmtNum(log.unrealized_pnl)}
                      </td>
                      <td className="px-2 py-1 text-right font-mono" style={{ color: (log.unrealized_pnl_pct ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>
                        {log.unrealized_pnl_pct != null ? `${log.unrealized_pnl_pct >= 0 ? '+' : ''}${log.unrealized_pnl_pct.toFixed(2)}%` : '—'}
                      </td>
                      <td className="px-2 py-1 text-[var(--text-muted)] truncate max-w-[200px]" title={log.note}>{log.note || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function InventoryHistoryViewer({ params }: { params?: any }) {
  const { t } = useTranslation();
  const [strategy, setStrategy] = useState(params?.strategy || 'mrpt');
  const [expandedFile, setExpandedFile] = useState<string | null>(null);
  const [snapshotData, setSnapshotData] = useState<any>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);

  const { data: history, loading, error, refetch } = useApi(() => getInventoryHistory(strategy), [strategy]);

  const handleExpand = async (filename: string) => {
    if (expandedFile === filename) {
      setExpandedFile(null);
      return;
    }
    setExpandedFile(filename);
    setSnapshotLoading(true);
    try {
      const snap = await getInventorySnapshot(strategy, filename);
      setSnapshotData(snap);
    } catch {
      setSnapshotData(null);
    }
    setSnapshotLoading(false);
  };

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!history) return null;

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)]">{t('inventoryHistory.title')}</div>
        <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
          {['mrpt', 'mtfs', 'ssrs', 'aiss'].map(s => (
            <button key={s} onClick={() => { setStrategy(s); setExpandedFile(null); }} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === s ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
              {s.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto space-y-2">
        {(history as any[]).map((h: any, i: number) => (
          <div key={i} className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg">
            <div
              className="flex items-center justify-between px-4 py-3 cursor-pointer hover:bg-[var(--bg-secondary)] transition-colors"
              onClick={() => handleExpand(h.filename)}
            >
              <div className="flex items-center gap-3">
                {expandedFile === h.filename ? <ChevronDown className="w-3.5 h-3.5 text-[var(--text-muted)]" /> : <ChevronRight className="w-3.5 h-3.5 text-[var(--text-muted)]" />}
                <Clock className="w-3 h-3 text-[var(--text-muted)]" />
                <span className="text-xs font-mono text-[var(--text-primary)]">{h.timestamp?.replace('_', ' ')}</span>
              </div>
              <div className="flex items-center gap-4">
                <span className="text-xs text-[var(--text-secondary)]">{h.activePairs} {t('inventoryHistory.activePairs')}</span>
                <span className="text-xs text-[var(--text-muted)]">{(h.size / 1024).toFixed(1)} KB</span>
              </div>
            </div>
            {expandedFile === h.filename && (
              <div className="border-t border-[var(--border-subtle)] px-4 py-3">
                {snapshotLoading ? (
                  <div className="text-[var(--text-muted)] text-center py-4 text-xs">{t('inventoryHistory.loadingSnapshot')}</div>
                ) : snapshotData ? (
                  strategy === 'aiss' ? (
                    <AissSnapshotDetail data={snapshotData} />
                  ) : strategy === 'ssrs' ? (
                    <SsrsSnapshotDetail data={snapshotData} />
                  ) : (
                  <div className="space-y-3">
                    <div className="flex items-center gap-4 text-[11px] text-[var(--text-muted)]">
                      <span>{t('inventoryHistory.asOf')} <span className="font-mono text-[var(--text-primary)]">{snapshotData.as_of}</span></span>
                      <span>{t('inventoryHistory.capital')} <span className="font-mono text-[var(--text-primary)]">${Number(snapshotData.capital).toLocaleString()}</span></span>
                    </div>
                    {Object.entries(snapshotData.pairs || {})
                      .filter(([, p]: any) => (p as any).direction !== null)
                      .map(([key, p]: any) => (
                        <PairDetail key={key} pair={key} pos={p} asOf={snapshotData.as_of} />
                      ))}
                    {Object.entries(snapshotData.pairs || {})
                      .filter(([, p]: any) => (p as any).direction === null).length > 0 && (
                      <div className="text-[10px] text-[var(--text-muted)] pt-1 flex flex-wrap items-center gap-1">
                        <span>{t('common.flat')}:</span>
                        {Object.entries(snapshotData.pairs || {})
                          .filter(([, p]: any) => (p as any).direction === null)
                          .map(([k]) => <span key={k}><PairBadge pair={k} strategy={strategy} compact /></span>)}
                      </div>
                    )}
                  </div>
                  )
                ) : (
                  <div className="text-[var(--text-muted)] text-center text-xs">{t('inventoryHistory.failedSnapshot')}</div>
                )}
              </div>
            )}
          </div>
        ))}
        {(history as any[]).length === 0 && (
          <div className="text-sm text-[var(--text-muted)] text-center py-8">{t('inventoryHistory.noHistory')}</div>
        )}
      </div>
    </div>
  );
}
