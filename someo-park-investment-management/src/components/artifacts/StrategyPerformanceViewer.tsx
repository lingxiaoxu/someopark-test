import React, { useState, useMemo, useEffect } from 'react';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  AreaChart, Area, ReferenceLine,
} from 'recharts';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import { API_BASE, apiHeaders } from '../../lib/api';

interface DayData {
  date: string;
  mrpt_equity: number;
  mtfs_equity: number;
  combined_equity: number;
  mrpt_pnl: number;
  mtfs_pnl: number;
  combined_pnl: number;
  mrpt_dd: number;
  mtfs_dd: number;
  combined_dd: number;
}

const COLORS = {
  mrpt: '#2563eb',
  mtfs: '#f59e0b',
  combined: '#111',
};

const STRATEGY_LABELS: Record<string, string> = {
  mrpt: 'MRPT',
  mtfs: 'MTFS',
  combined: 'Combined (70/30)',
};

export default function StrategyPerformanceViewer() {
  const [data, setData] = useState<DayData[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeStrategies, setActiveStrategies] = useState<Set<string>>(new Set(['mrpt', 'mtfs', 'combined']));

  useEffect(() => {
    fetch(`${API_BASE}/data/strategy_performance.json`, { headers: apiHeaders() })
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(d => setData(d))
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  const toggle = (key: string) => {
    setActiveStrategies(prev => {
      const next = new Set(prev);
      if (next.has(key)) { if (next.size > 1) next.delete(key); }
      else next.add(key);
      return next;
    });
  };

  // Compute enriched chart data with return %
  const chartData = useMemo(() => {
    if (!data) return [];
    const start = 1000000;
    return data.map(d => ({
      ...d,
      mrpt_ret: ((d.mrpt_equity - start) / start) * 100,
      mtfs_ret: ((d.mtfs_equity - start) / start) * 100,
      combined_ret: ((d.combined_equity - start) / start) * 100,
      label: d.date.slice(5), // MM-DD
    }));
  }, [data]);

  // Auto-fit Y domain for equity chart based on active strategies
  const [eqYMin, eqYMax] = useMemo(() => {
    if (chartData.length === 0) return [950000, 1200000];
    const vals: number[] = [];
    for (const d of chartData) {
      if (activeStrategies.has('mrpt')) vals.push(d.mrpt_equity);
      if (activeStrategies.has('mtfs')) vals.push(d.mtfs_equity);
      if (activeStrategies.has('combined')) vals.push(d.combined_equity);
    }
    if (vals.length === 0) return [950000, 1200000];
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const range = max - min || 10000;
    const pad = range * 0.1;
    return [Math.floor((min - pad) / 5000) * 5000, Math.ceil((max + pad) / 5000) * 5000];
  }, [chartData, activeStrategies]);

  // Auto-fit return % axis (derived from equity domain)
  const retYMin = ((eqYMin - 1000000) / 1000000) * 100;
  const retYMax = ((eqYMax - 1000000) / 1000000) * 100;

  // Scorecard stats
  const stats = useMemo(() => {
    if (!data || data.length === 0) return null;
    const last = data[data.length - 1];
    const start = 1000000;

    const calcStats = (eqKey: keyof DayData, ddKey: keyof DayData, pnlKey: keyof DayData) => {
      const ret = ((last[eqKey] as number) - start) / start * 100;
      const maxDD = Math.min(...data.map(d => d[ddKey] as number));
      const dailyReturns = data.map((d, i) => {
        if (i === 0) return 0;
        const prev = data[i - 1][eqKey] as number;
        return ((d[eqKey] as number) - prev) / prev;
      }).slice(1);
      const mean = dailyReturns.reduce((a, b) => a + b, 0) / dailyReturns.length;
      const std = Math.sqrt(dailyReturns.reduce((a, b) => a + (b - mean) ** 2, 0) / dailyReturns.length);
      const sharpe = std > 0 ? (mean / std) * Math.sqrt(252) : 0;
      const winDays = dailyReturns.filter(r => r > 0).length;
      const winRate = (winDays / dailyReturns.length) * 100;
      return {
        totalReturn: ret,
        maxDD,
        sharpe,
        winRate,
        finalEquity: last[eqKey] as number,
        totalPnL: (last[eqKey] as number) - start,
      };
    };

    return {
      mrpt: calcStats('mrpt_equity', 'mrpt_dd', 'mrpt_pnl'),
      mtfs: calcStats('mtfs_equity', 'mtfs_dd', 'mtfs_pnl'),
      combined: calcStats('combined_equity', 'combined_dd', 'combined_pnl'),
    };
  }, [data]);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} />;
  if (!data || !stats) return null;

  const fmtPct = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
  const fmtMoney = (v: number) => `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

  return (
    <div className="flex flex-col gap-5" style={{ fontFamily: 'var(--font-mono)' }}>

      {/* Strategy toggles */}
      <div className="flex items-center gap-2 shrink-0">
        {(['mrpt', 'mtfs', 'combined'] as const).map(key => (
          <button
            key={key}
            onClick={() => toggle(key)}
            style={{
              padding: '4px 12px',
              fontSize: '11px',
              fontWeight: 700,
              letterSpacing: '.06em',
              textTransform: 'uppercase',
              border: `2px solid ${COLORS[key]}`,
              background: activeStrategies.has(key) ? COLORS[key] : 'transparent',
              color: activeStrategies.has(key) ? '#fff' : COLORS[key],
              cursor: 'pointer',
              transition: 'all .15s',
            }}
          >
            {STRATEGY_LABELS[key]}
          </button>
        ))}
      </div>

      {/* Scorecard */}
      <div className="grid grid-cols-3 gap-3 shrink-0">
        {(['mrpt', 'mtfs', 'combined'] as const).filter(k => activeStrategies.has(k)).map(key => {
          const s = stats[key];
          return (
            <div key={key} style={{ background: '#fff', border: '2px solid #111', padding: '12px' }}>
              <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '.08em', textTransform: 'uppercase', color: COLORS[key], marginBottom: '8px' }}>
                {STRATEGY_LABELS[key]}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px', fontSize: '11px' }}>
                <div>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>Return</div>
                  <div style={{ fontWeight: 700, color: s.totalReturn >= 0 ? '#16a34a' : '#dc2626' }}>{fmtPct(s.totalReturn)}</div>
                </div>
                <div>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>Sharpe</div>
                  <div style={{ fontWeight: 700 }}>{s.sharpe.toFixed(2)}</div>
                </div>
                <div>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>Max DD</div>
                  <div style={{ fontWeight: 700, color: '#dc2626' }}>{s.maxDD.toFixed(2)}%</div>
                </div>
                <div>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>Win Rate</div>
                  <div style={{ fontWeight: 700 }}>{s.winRate.toFixed(0)}%</div>
                </div>
                <div style={{ gridColumn: 'span 2' }}>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>Net P&L</div>
                  <div style={{ fontWeight: 700, color: s.totalPnL >= 0 ? '#16a34a' : '#dc2626' }}>{fmtMoney(s.totalPnL)}</div>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Equity Curve */}
      <div style={{ background: '#fff', border: '2px solid #111', padding: '16px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', marginBottom: '12px' }}>
          Equity Curve — Nov 2025 to Mar 2026
        </div>
        <div style={{ height: 280 }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 5, right: 50, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" vertical={false} />
              <XAxis dataKey="label" fontSize={9} stroke="#999" tickLine={false} axisLine={false} minTickGap={50} />
              <YAxis
                yAxisId="left"
                domain={[eqYMin, eqYMax]}
                fontSize={9} stroke="#999" tickLine={false} axisLine={false}
                tickFormatter={v => `$${(v / 1000).toFixed(0)}k`}
              />
              <YAxis
                yAxisId="right"
                orientation="right"
                domain={[retYMin, retYMax]}
                fontSize={9} stroke="#999" tickLine={false} axisLine={false}
                tickFormatter={v => `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`}
              />
              <Tooltip
                contentStyle={{ fontFamily: 'var(--font-mono)', fontSize: '11px', border: '2px solid #111', borderRadius: 0 }}
                formatter={(val: number, name: string) => {
                  if (name.includes('ret')) return [`${val >= 0 ? '+' : ''}${val.toFixed(2)}%`, name.replace('_ret', '').toUpperCase() + ' Return'];
                  return [fmtMoney(val), name.replace('_equity', '').toUpperCase() + ' Equity'];
                }}
                labelFormatter={(label: string) => `Date: ${label}`}
              />
              <ReferenceLine yAxisId="left" y={1000000} stroke="#ccc" strokeDasharray="4 4" />
              {activeStrategies.has('mrpt') && <Line yAxisId="left" type="monotone" dataKey="mrpt_equity" stroke={COLORS.mrpt} strokeWidth={2} dot={false} name="mrpt_equity" />}
              {activeStrategies.has('mtfs') && <Line yAxisId="left" type="monotone" dataKey="mtfs_equity" stroke={COLORS.mtfs} strokeWidth={2} dot={false} name="mtfs_equity" />}
              {activeStrategies.has('combined') && <Line yAxisId="left" type="monotone" dataKey="combined_equity" stroke={COLORS.combined} strokeWidth={2.5} dot={false} name="combined_equity" />}
              {/* Invisible lines for right axis return % */}
              {activeStrategies.has('mrpt') && <Line yAxisId="right" type="monotone" dataKey="mrpt_ret" stroke="transparent" strokeWidth={0} dot={false} name="mrpt_ret" />}
              {activeStrategies.has('mtfs') && <Line yAxisId="right" type="monotone" dataKey="mtfs_ret" stroke="transparent" strokeWidth={0} dot={false} name="mtfs_ret" />}
              {activeStrategies.has('combined') && <Line yAxisId="right" type="monotone" dataKey="combined_ret" stroke="transparent" strokeWidth={0} dot={false} name="combined_ret" />}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Drawdown Chart */}
      <div style={{ background: '#fff', border: '2px solid #111', padding: '16px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', marginBottom: '12px' }}>
          Drawdown (%)
        </div>
        <div style={{ height: 180 }}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={chartData} margin={{ top: 5, right: 20, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" vertical={false} />
              <XAxis dataKey="label" fontSize={9} stroke="#999" tickLine={false} axisLine={false} minTickGap={50} />
              <YAxis fontSize={9} stroke="#999" tickLine={false} axisLine={false} tickFormatter={v => `${v.toFixed(1)}%`} />
              <Tooltip
                contentStyle={{ fontFamily: 'var(--font-mono)', fontSize: '11px', border: '2px solid #111', borderRadius: 0 }}
                formatter={(val: number, name: string) => [`${val.toFixed(2)}%`, name.replace('_dd', '').toUpperCase()]}
                labelFormatter={(label: string) => `Date: ${label}`}
              />
              <ReferenceLine y={0} stroke="#111" strokeWidth={1} />
              {activeStrategies.has('mrpt') && <Area type="monotone" dataKey="mrpt_dd" stroke={COLORS.mrpt} fill={COLORS.mrpt} fillOpacity={0.1} strokeWidth={1.5} dot={false} name="mrpt_dd" />}
              {activeStrategies.has('mtfs') && <Area type="monotone" dataKey="mtfs_dd" stroke={COLORS.mtfs} fill={COLORS.mtfs} fillOpacity={0.1} strokeWidth={1.5} dot={false} name="mtfs_dd" />}
              {activeStrategies.has('combined') && <Area type="monotone" dataKey="combined_dd" stroke={COLORS.combined} fill={COLORS.combined} fillOpacity={0.08} strokeWidth={2} dot={false} name="combined_dd" />}
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Daily P&L Bar-like via line */}
      <div style={{ background: '#fff', border: '2px solid #111', padding: '16px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', marginBottom: '12px' }}>
          Daily P&L
        </div>
        <div style={{ height: 160 }}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={chartData} margin={{ top: 5, right: 20, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" vertical={false} />
              <XAxis dataKey="label" fontSize={9} stroke="#999" tickLine={false} axisLine={false} minTickGap={50} />
              <YAxis fontSize={9} stroke="#999" tickLine={false} axisLine={false} tickFormatter={v => `$${(v / 1000).toFixed(0)}k`} />
              <Tooltip
                contentStyle={{ fontFamily: 'var(--font-mono)', fontSize: '11px', border: '2px solid #111', borderRadius: 0 }}
                formatter={(val: number, name: string) => [fmtMoney(val), name.replace('_pnl', '').toUpperCase()]}
                labelFormatter={(label: string) => `Date: ${label}`}
              />
              <ReferenceLine y={0} stroke="#111" strokeWidth={1} />
              {activeStrategies.has('combined') && <Area type="stepAfter" dataKey="combined_pnl" stroke={COLORS.combined} fill={COLORS.combined} fillOpacity={0.08} strokeWidth={1.5} dot={false} name="combined_pnl" />}
              {activeStrategies.has('mrpt') && <Area type="stepAfter" dataKey="mrpt_pnl" stroke={COLORS.mrpt} fill={COLORS.mrpt} fillOpacity={0.08} strokeWidth={1} dot={false} name="mrpt_pnl" />}
              {activeStrategies.has('mtfs') && <Area type="stepAfter" dataKey="mtfs_pnl" stroke={COLORS.mtfs} fill={COLORS.mtfs} fillOpacity={0.08} strokeWidth={1} dot={false} name="mtfs_pnl" />}
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Footer metadata */}
      <div style={{ fontSize: '9px', color: '#999', letterSpacing: '.04em', textTransform: 'uppercase' }}>
        Inception: Nov 11, 2025 &middot; {data.length} Trading Days &middot; $1M Initial Capital &middot; 70/30 MRPT/MTFS Weighting
      </div>
    </div>
  );
}
