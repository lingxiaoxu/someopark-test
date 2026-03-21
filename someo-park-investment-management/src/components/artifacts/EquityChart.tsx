import React, { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { useApi } from '../../hooks/useApi';
import { getOOSEquityCurve, getWFSummary } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';

export default function EquityChart({ params }: { params?: any }) {
  const { t } = useTranslation();
  const [strategy, setStrategy] = useState(params?.strategy || 'mrpt');
  const { data: chartData, loading, error, refetch } = useApi(() => getOOSEquityCurve(strategy), [strategy]);
  const { data: wfSummary } = useApi(() => getWFSummary(strategy), [strategy]);

  const startEquity = 500000;

  // Add return % to each data point
  const enrichedData = useMemo(() => {
    if (!chartData) return [];
    return chartData.map((d: any) => {
      const eq = d.Equity_Chained || d.OOS_Equity_Chained || d.Equity || startEquity;
      return { ...d, _equity: eq, _returnPct: ((eq - startEquity) / startEquity) * 100 };
    });
  }, [chartData]);

  // Auto-fit Y domain
  const [yMin, yMax] = useMemo(() => {
    if (enrichedData.length === 0) return [0, 1];
    const vals = enrichedData.map((d: any) => d._equity);
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const pad = (max - min) * 0.08 || 5000;
    return [Math.floor((min - pad) / 1000) * 1000, Math.ceil((max + pad) / 1000) * 1000];
  }, [enrichedData]);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!enrichedData || enrichedData.length === 0) return null;

  const oosStats = wfSummary?.oos_stats || {};
  const endEquity = enrichedData[enrichedData.length - 1]?._equity || startEquity;
  const totalReturn = ((endEquity - startEquity) / startEquity * 100).toFixed(2);

  return (
    <div className="flex flex-col gap-4 h-full">
      <div className="flex items-center justify-between shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)]">{t('equity.title')}</div>
        <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
          {['mrpt', 'mtfs'].map(s => (
            <button key={s} onClick={() => setStrategy(s)} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === s ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
              {s.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-3 shrink-0">
        <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-3">
          <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('equity.totalReturn')}</div>
          <div className={`text-lg font-semibold ${Number(totalReturn) >= 0 ? 'text-[var(--success)]' : 'text-[var(--error)]'}`}>
            {Number(totalReturn) >= 0 ? '+' : ''}{totalReturn}%
          </div>
        </div>
        <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-3">
          <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('equity.sharpeRatio')}</div>
          <div className="text-lg font-semibold text-[var(--text-primary)]">{oosStats.oos_sharpe?.toFixed(2) ?? '—'}</div>
        </div>
        <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-3">
          <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('equity.maxDrawdown')}</div>
          <div className="text-lg font-semibold text-[var(--error)]">{oosStats.oos_max_dd_pct != null ? `${(oosStats.oos_max_dd_pct * 100).toFixed(2)}%` : '—'}</div>
        </div>
      </div>

      <div className="flex-1 bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-4 min-h-[300px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={enrichedData} margin={{ top: 5, right: 50, left: -10, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border-subtle)" vertical={false} />
            <XAxis dataKey="Date" stroke="var(--text-muted)" fontSize={10} tickLine={false} axisLine={false} minTickGap={40} />
            <YAxis
              yAxisId="left"
              domain={[yMin, yMax]}
              stroke="var(--text-muted)" fontSize={10} tickLine={false} axisLine={false}
              tickFormatter={(val) => `$${(val/1000).toFixed(0)}k`}
            />
            <YAxis
              yAxisId="right"
              orientation="right"
              domain={[((yMin - startEquity) / startEquity) * 100, ((yMax - startEquity) / startEquity) * 100]}
              stroke="var(--text-muted)" fontSize={10} tickLine={false} axisLine={false}
              tickFormatter={(val) => `${val >= 0 ? '+' : ''}${val.toFixed(1)}%`}
            />
            <Tooltip
              contentStyle={{ backgroundColor: 'var(--bg-tertiary)', borderColor: 'var(--border-subtle)', borderRadius: '8px', fontSize: '12px' }}
              itemStyle={{ color: 'var(--text-primary)' }}
              formatter={(val: number, name: string) => {
                if (name === 'Equity') return [`$${val.toLocaleString(undefined, { maximumFractionDigits: 0 })}`, t('equity.equity')];
                return [`${val >= 0 ? '+' : ''}${val.toFixed(2)}%`, t('equity.return')];
              }}
            />
            <Line yAxisId="left" type="monotone" dataKey="_equity" stroke="var(--accent-primary)" strokeWidth={2} dot={false} activeDot={{ r: 4 }} name="Equity" />
            <Line yAxisId="right" type="monotone" dataKey="_returnPct" stroke="transparent" strokeWidth={0} dot={false} name="Return" />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
