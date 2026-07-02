import React, { useState, useMemo, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  AreaChart, Area, ReferenceLine,
} from 'recharts';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import { API_BASE, apiHeaders } from '../../lib/api';
import SizedChart from './SizedChart';


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
  // Master mode fields (optional — only in master_portfolio_performance.json)
  sr_equity?: number;
  aiss_equity?: number;
  master_equity?: number;
  sr_pnl?: number;
  aiss_pnl?: number;
  master_pnl?: number;
  sr_dd?: number;
  aiss_dd?: number;
  master_dd?: number;
  [key: string]: any;
}

const COLORS: Record<string, string> = {
  mrpt: '#2563eb',
  mtfs: '#f59e0b',
  sr: '#16a34a',
  aiss: '#a855f7',
  bdc: '#e11d48',
  combined: '#111',
  master: '#111',
  spy: '#9ca3af',
  smh: '#6b7280',
  soxx: '#0891b2',
  mags: '#c026d3',
};
const LABELS: Record<string, string> = {
  mrpt: 'MRPT', mtfs: 'MTFS', sr: 'SSRS', aiss: 'AISS', bdc: 'PC BDC',
  combined: 'COMBINED 4 AI ENABLED SYSTEMATIC STRATEGIES',
  master: 'MASTER AI PORTFOLIO WITH GLOBAL ALLOCATIONS',
  spy: 'SPY', smh: 'SMH', soxx: 'SOXX', mags: 'MAGS',
};
const STRAT_KEYS = ['mrpt', 'mtfs', 'sr', 'aiss', 'combined'];
const MASTER_KEYS = ['mrpt', 'mtfs', 'sr', 'aiss', 'bdc', 'master'];
const BENCHMARK_KEYS = ['spy', 'smh', 'soxx', 'mags'];  // dashed reference lines in Master mode

// Default start of the visible window when a view first opens. The picker's `min` still
// exposes the full history, so earlier dates remain selectable — this only sets the default.
const DEFAULT_START_DATE = '2025-11-11';


export default function StrategyPerformanceViewer({ params }: { params?: any }) {
  const { t } = useTranslation();

  // ALL hooks declared unconditionally at the top
  const [viewMode, setViewMode] = useState<'strategies' | 'master'>('strategies');
  const [stratData, setStratData] = useState<DayData[] | null>(null);
  const [masterData, setMasterData] = useState<DayData[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [masterLoading, setMasterLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeStrategies, setActiveStrategies] = useState<Set<string>>(new Set(['mrpt', 'mtfs', 'sr', 'aiss', 'combined']));
  const [startDate, setStartDate] = useState<string>('');
  const [endDate, setEndDate] = useState<string>('');

  // Load strategy_performance.json
  useEffect(() => {
    fetch(`${API_BASE}/data/strategy_performance.json`, { headers: apiHeaders() })
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then((d: DayData[]) => {
        setStratData(d);
        if (d.length > 0) {
          const first = d[0].date, last = d[d.length - 1].date;
          // Default the window start to DEFAULT_START_DATE, clamped into the available range.
          setStartDate(DEFAULT_START_DATE >= first && DEFAULT_START_DATE <= last ? DEFAULT_START_DATE : first);
          setEndDate(last);
        }
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  // Load master_portfolio_performance.json
  useEffect(() => {
    fetch(`${API_BASE}/data/master_portfolio_performance.json`, { headers: apiHeaders() })
      .then(r => { if (!r.ok) throw new Error(`master HTTP ${r.status}`); return r.json(); })
      .then((d: DayData[]) => setMasterData(d))
      .catch(() => {}) // silently fail — master mode just won't be available
      .finally(() => setMasterLoading(false));
  }, []);

  // Both modes use masterData (it has all fields: mrpt, mtfs, sr, aiss, combined, master)
  // Fall back to stratData only if masterData hasn't loaded yet
  const data = masterData || stratData;

  const toggle = (key: string) => {
    setActiveStrategies(prev => {
      const next = new Set(prev);
      if (next.has(key)) { if (next.size > 1) next.delete(key); }
      else next.add(key);
      return next;
    });
  };

  // Filter data by date range
  const filteredData = useMemo(() => {
    if (!data) return [];
    return data.filter(d => d.date >= startDate && d.date <= endDate);
  }, [data, startDate, endDate]);

  // Available date range from full data
  const dateRange = useMemo(() => {
    if (!data || data.length === 0) return { min: '', max: '' };
    return { min: data[0].date, max: data[data.length - 1].date };
  }, [data]);

  // Active keys based on mode
  const activeKeys = viewMode === 'master' ? MASTER_KEYS : STRAT_KEYS;
  // The "total" key (for right axis $ scale)
  const totalKey = viewMode === 'master' ? 'master' : 'combined';

  // Recompute drawdown relative to filtered window — dynamic for all keys
  const windowData = useMemo(() => {
    if (filteredData.length === 0) return [];
    const first = filteredData[0];
    const peaks: Record<string, number> = {};
    for (const k of activeKeys) peaks[k] = (first as any)[`${k}_equity`] || 0;
    // benchmarks get the same drawdown/PnL treatment as strategies (Master mode)
    for (const bm of BENCHMARK_KEYS) peaks[bm] = (first as any)[`${bm}_equity`] || 0;

    return filteredData.map((d: any, i: number) => {
      const row: any = { ...d, label: d.date.slice(5) };
      for (const k of activeKeys) {
        const eq = d[`${k}_equity`] || 0;
        const firstEq = (first as any)[`${k}_equity`] || 1;
        if (eq > peaks[k]) peaks[k] = eq;
        row[`${k}_ret`] = ((eq - firstEq) / firstEq) * 100;
        row[`${k}_dd_w`] = peaks[k] > 0 ? (eq - peaks[k]) / peaks[k] * 100 : 0;
        row[`${k}_dd_dollar`] = eq - peaks[k];
        if (i > 0) {
          const prevEq = (filteredData[i - 1] as any)[`${k}_equity`] || 1;
          row[`${k}_pnl_w`] = eq - prevEq;
          row[`${k}_pnl_pct`] = prevEq > 0 ? ((eq - prevEq) / prevEq) * 100 : 0;
        } else {
          row[`${k}_pnl_w`] = 0;
          row[`${k}_pnl_pct`] = 0;
        }
      }
      // Benchmarks: return %, drawdown, and daily PnL (same as strategies)
      for (const bm of BENCHMARK_KEYS) {
        const eq = d[`${bm}_equity`] || 0;
        const firstEq = (first as any)[`${bm}_equity`] || 1;
        if (firstEq > 0 && eq > 0) {
          if (eq > peaks[bm]) peaks[bm] = eq;
          row[`${bm}_ret`] = ((eq - firstEq) / firstEq) * 100;
          row[`${bm}_dd_w`] = peaks[bm] > 0 ? (eq - peaks[bm]) / peaks[bm] * 100 : 0;
          row[`${bm}_dd_dollar`] = eq - peaks[bm];
          if (i > 0) {
            const prevEq = (filteredData[i - 1] as any)[`${bm}_equity`] || 1;
            row[`${bm}_pnl_w`] = eq - prevEq;
            row[`${bm}_pnl_pct`] = prevEq > 0 ? ((eq - prevEq) / prevEq) * 100 : 0;
          } else {
            row[`${bm}_pnl_w`] = 0;
            row[`${bm}_pnl_pct`] = 0;
          }
        }
      }
      return row;
    });
  }, [filteredData, viewMode]);

  // Auto-fit Y domain for return %
  const [retYMin, retYMax] = useMemo(() => {
    if (windowData.length === 0) return [-3, 15];
    const vals: number[] = [];
    for (const d of windowData) {
      for (const k of activeKeys) {
        if (activeStrategies.has(k)) vals.push(d[`${k}_ret`] ?? 0);
      }
      // Include visible benchmarks in Y range
      if (viewMode === 'master') {
        for (const bm of BENCHMARK_KEYS) {
          if (activeStrategies.has(bm)) vals.push(d[`${bm}_ret`] ?? 0);
        }
      }
    }
    if (vals.length === 0) return [-3, 15];
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const range = max - min || 5;
    const pad = range * 0.15;
    return [Math.floor((min - pad) * 2) / 2, Math.ceil((max + pad) * 2) / 2];
  }, [windowData, activeStrategies, viewMode]);

  // Right axis: total equity $
  const firstRow0 = filteredData?.[0] as any;
  const totalEqStart = firstRow0 ? (firstRow0[`${totalKey}_equity`] || 1000000) : 1000000;
  const combEqMin = totalEqStart * (1 + retYMin / 100);
  const combEqMax = totalEqStart * (1 + retYMax / 100);

  // Current weight % for each component (from latest data)
  const weightPcts = useMemo(() => {
    if (filteredData.length === 0) return {} as Record<string, number>;
    const last = filteredData[filteredData.length - 1] as any;
    const total = last[`${totalKey}_equity`] || 1;
    const w: Record<string, number> = {};
    for (const k of activeKeys) {
      if (k !== totalKey) w[k] = Math.round((last[`${k}_equity`] || 0) / total * 100);
    }
    return w;
  }, [filteredData, viewMode]);

  // Scorecard stats (computed over filtered window) — dynamic keys
  const stats = useMemo(() => {
    if (filteredData.length === 0) return null;
    const last = filteredData[filteredData.length - 1] as any;
    const firstRow = filteredData[0] as any;
    const result: Record<string, any> = {};

    const statKeys = viewMode === 'master' ? [...activeKeys, ...BENCHMARK_KEYS] : activeKeys;
    for (const k of statKeys) {
      const startEq = firstRow[`${k}_equity`] || 1;
      const endEq = last[`${k}_equity`] || 0;
      const ret = ((endEq - startEq) / startEq) * 100;
      const maxDD = Math.min(...windowData.map((d: any) => d[`${k}_dd_w`] ?? 0));
      const dailyReturns = filteredData.map((d: any, i: number) => {
        if (i === 0) return 0;
        const prev = (filteredData[i - 1] as any)[`${k}_equity`] || 1;
        return ((d[`${k}_equity`] || 0) - prev) / prev;
      }).slice(1);
      const mean = dailyReturns.reduce((a: number, b: number) => a + b, 0) / dailyReturns.length;
      const std = Math.sqrt(dailyReturns.reduce((a: number, b: number) => a + (b - mean) ** 2, 0) / dailyReturns.length);
      const sharpe = std > 0 ? (mean / std) * Math.sqrt(252) : 0;
      const winDays = dailyReturns.filter((r: number) => r > 0).length;
      const winRate = dailyReturns.length > 0 ? (winDays / dailyReturns.length) * 100 : 0;
      result[k] = { totalReturn: ret, maxDD, sharpe, winRate, totalPnL: endEq - startEq };
    }
    return result;
  }, [filteredData, windowData, viewMode]);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} />;
  if (!data || !stats) return null;

  const fmtPct = (v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
  const fmtMoney = (v: number) => `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

  const inputStyle: React.CSSProperties = {
    padding: '3px 6px',
    fontSize: '10px',
    fontFamily: 'var(--font-mono)',
    border: '1px solid #ccc',
    background: '#fff',
    color: '#333',
    width: '110px',
  };

  return (
    <div className="flex flex-col gap-5" style={{ fontFamily: 'var(--font-mono)' }}>

      {/* Mode switch + Strategy toggles + date range */}
      <div className="flex items-center gap-2 shrink-0 flex-wrap">
        {/* STRATEGIES / MASTER mode toggle */}
        <div style={{ display: 'flex', border: '2px solid #111', marginRight: '6px' }}>
          {(['strategies', 'master'] as const).map(m => (
            <button key={m} onClick={() => {
              setViewMode(m);
              setActiveStrategies(new Set(m === 'strategies' ? STRAT_KEYS : MASTER_KEYS));
              // Keep the currently-selected date range so it carries over to the new view
              // (both modes share the same underlying data range; the chart below refreshes
              // automatically since filteredData depends on startDate/endDate).
            }} style={{
              padding: '4px 10px', fontSize: '10px', fontWeight: 700, letterSpacing: '.06em',
              background: viewMode === m ? '#111' : 'transparent',
              color: viewMode === m ? '#fff' : '#111',
              border: 'none', borderLeft: m === 'master' ? '1px solid #111' : 'none', cursor: 'pointer',
            }}>{m.toUpperCase()}</button>
          ))}
        </div>
        {/* Strategy toggles (dynamic based on mode) */}
        {(viewMode === 'master' ? MASTER_KEYS : STRAT_KEYS).map(key => (
          <button
            key={key}
            onClick={() => toggle(key)}
            style={{
              padding: '4px 10px',
              fontSize: '10px',
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
            {LABELS[key] || key.toUpperCase()}
          </button>
        ))}
        {viewMode === 'master' && BENCHMARK_KEYS.map(bm => (
          <button
            key={bm}
            onClick={() => toggle(bm)}
            style={{
              padding: '4px 10px',
              fontSize: '10px',
              fontWeight: 700,
              letterSpacing: '.06em',
              textTransform: 'uppercase',
              border: `2px dashed ${COLORS[bm]}`,
              background: activeStrategies.has(bm) ? COLORS[bm] : 'transparent',
              color: activeStrategies.has(bm) ? '#fff' : COLORS[bm],
              cursor: 'pointer',
              transition: 'all .15s',
            }}
          >
            {LABELS[bm]}
          </button>
        ))}
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: '6px' }}>
          <input
            type="date"
            value={startDate}
            min={dateRange.min}
            max={endDate}
            onChange={e => setStartDate(e.target.value)}
            style={inputStyle}
          />
          <span style={{ fontSize: '10px', color: '#999' }}>—</span>
          <input
            type="date"
            value={endDate}
            min={startDate}
            max={dateRange.max}
            onChange={e => setEndDate(e.target.value)}
            style={inputStyle}
          />
        </div>
      </div>

      {/* Scorecard (strategies + visible benchmarks in Master mode) */}
      {(() => { const cardKeys = [...activeKeys, ...(viewMode === 'master' ? BENCHMARK_KEYS : [])].filter(k => activeStrategies.has(k)); return (
      <div style={{ display: 'grid', gridTemplateColumns: `repeat(${Math.min(cardKeys.length, 5)}, 1fr)`, gap: '10px' }} className="shrink-0">
        {cardKeys.map(key => {
          const s = stats?.[key];
          if (!s) return null;
          return (
            <div key={key} style={{ background: '#fff', border: '2px solid #111', padding: '12px' }}>
              <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '.08em', textTransform: 'uppercase', color: COLORS[key], marginBottom: '8px', display: 'flex', justifyContent: 'space-between' }}>
                <span>{LABELS[key] || key.toUpperCase()}</span>
                {weightPcts[key] != null && <span style={{ fontSize: '9px', color: '#888', fontWeight: 500 }}>{weightPcts[key]}%</span>}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px', fontSize: '11px' }}>
                <div>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>RETURN</div>
                  <div style={{ fontWeight: 700, color: s.totalReturn >= 0 ? '#16a34a' : '#dc2626' }}>{fmtPct(s.totalReturn)}</div>
                </div>
                <div>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>SHARPE</div>
                  <div style={{ fontWeight: 700 }}>{s.sharpe.toFixed(2)}</div>
                </div>
                <div>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>MAX DD</div>
                  <div style={{ fontWeight: 700, color: '#dc2626' }}>{s.maxDD.toFixed(2)}%</div>
                </div>
                <div>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>WIN RATE</div>
                  <div style={{ fontWeight: 700 }}>{s.winRate.toFixed(0)}%</div>
                </div>
                <div style={{ gridColumn: 'span 2' }}>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>NET PnL</div>
                  <div style={{ fontWeight: 700, color: s.totalPnL >= 0 ? '#16a34a' : '#dc2626' }}>{fmtMoney(s.totalPnL)}</div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
      ); })()}

      {/* Equity Curve (% and $) */}
      <div style={{ background: '#fff', border: '2px solid #111', padding: '16px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', marginBottom: '12px' }}>
          {t('strategyPerf.equityCurveTitle')}
        </div>
        <SizedChart height={280}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={windowData} margin={{ top: 5, right: 5, left: 5, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" vertical={false} />
              <XAxis dataKey="label" fontSize={9} stroke="#999" tickLine={false} axisLine={false} minTickGap={50} />
              <YAxis
                yAxisId="ret"
                domain={[retYMin, retYMax]}
                fontSize={9} stroke="#999" tickLine={false} axisLine={false}
                tickFormatter={v => `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`}
              />
              <YAxis
                yAxisId="eq"
                orientation="right"
                domain={[combEqMin, combEqMax]}
                fontSize={9} stroke="#999" tickLine={false} axisLine={false}
                tickFormatter={v => `$${(v / 1000).toFixed(0)}k`}
                width={40}
              />
              <Tooltip
                contentStyle={{ fontFamily: 'var(--font-mono)', fontSize: '11px', border: '2px solid #111', borderRadius: 0 }}
                formatter={(val: number, name: string, props: { payload?: Record<string, number> }) => {
                  const strat = name.replace('_ret', '');
                  const eq = props.payload?.[`${strat}_equity`];
                  const eqStr = eq != null ? ` (${fmtMoney(eq)})` : '';
                  return [`${val >= 0 ? '+' : ''}${val.toFixed(2)}%${eqStr}`, LABELS[strat] || strat.toUpperCase()];
                }}
                labelFormatter={(label: string) => `Date: ${label}`}
              />
              <ReferenceLine yAxisId="ret" y={0} stroke="#ccc" strokeDasharray="4 4" />
              {activeKeys.filter(k => activeStrategies.has(k)).map(k => (
                <Line key={k} yAxisId="ret" type="monotone" dataKey={`${k}_ret`}
                  stroke={COLORS[k]} strokeWidth={k === totalKey ? 2.5 : 2} dot={false} name={`${k}_ret`} />
              ))}
              {viewMode === 'master' && BENCHMARK_KEYS.filter(bm => activeStrategies.has(bm)).map(bm => (
                <Line key={bm} yAxisId="ret" type="monotone" dataKey={`${bm}_ret`}
                  stroke={COLORS[bm]} strokeWidth={1.5} strokeDasharray="6 3" dot={false} name={`${bm}_ret`} />
              ))}
              <Line yAxisId="eq" type="monotone" dataKey={`${totalKey}_equity`} stroke="transparent" strokeWidth={0} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </SizedChart>
      </div>

      {/* Drawdown (% and $) */}
      <div style={{ background: '#fff', border: '2px solid #111', padding: '16px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', marginBottom: '12px' }}>
          {t('strategyPerf.drawdownTitle')}
        </div>
        <SizedChart height={180}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={windowData} margin={{ top: 5, right: 5, left: 5, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" vertical={false} />
              <XAxis dataKey="label" fontSize={9} stroke="#999" tickLine={false} axisLine={false} minTickGap={50} />
              <YAxis yAxisId="left" fontSize={9} stroke="#999" tickLine={false} axisLine={false} tickFormatter={v => `${v.toFixed(1)}%`} />
              <YAxis yAxisId="right" orientation="right" fontSize={9} stroke="#999" tickLine={false} axisLine={false}
                tickFormatter={v => `${fmtMoney(v)}`} />
              <Tooltip
                contentStyle={{ fontFamily: 'var(--font-mono)', fontSize: '11px', border: '2px solid #111', borderRadius: 0 }}
                formatter={(val: number, name: string, props: { payload?: Record<string, number> }) => {
                  if (name.endsWith('_dd_dollar')) return null;
                  const strat = name.replace('_dd_w', '');
                  const dollar = props.payload?.[`${strat}_dd_dollar`];
                  const dollarStr = dollar != null ? ` (${fmtMoney(dollar)})` : '';
                  return [`${val.toFixed(2)}%${dollarStr}`, LABELS[strat] || strat.toUpperCase()];
                }}
                labelFormatter={(label: string) => `Date: ${label}`}
              />
              <ReferenceLine yAxisId="left" y={0} stroke="#111" strokeWidth={1} />
              {activeKeys.filter(k => activeStrategies.has(k)).map(k => (
                <Area key={k} yAxisId="left" type="monotone" dataKey={`${k}_dd_w`}
                  stroke={COLORS[k]} fill={COLORS[k]} fillOpacity={k === totalKey ? 0.08 : 0.1}
                  strokeWidth={k === totalKey ? 2 : 1.5} dot={false} name={`${k}_dd_w`} />
              ))}
              {viewMode === 'master' && BENCHMARK_KEYS.filter(bm => activeStrategies.has(bm)).map(bm => (
                <Area key={bm} yAxisId="left" type="monotone" dataKey={`${bm}_dd_w`}
                  stroke={COLORS[bm]} strokeDasharray="5 4" fill="transparent"
                  strokeWidth={1.5} dot={false} name={`${bm}_dd_w`} />
              ))}
              <Area yAxisId="right" type="monotone" dataKey={`${totalKey}_dd_dollar`} stroke="transparent" fill="transparent" dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        </SizedChart>
      </div>

      {/* Daily P&L ($ and %) */}
      <div style={{ background: '#fff', border: '2px solid #111', padding: '16px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', marginBottom: '12px' }}>
          {t('strategyPerf.dailyPnLTitle')}
        </div>
        <SizedChart height={160}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={windowData} margin={{ top: 5, right: 5, left: 5, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" vertical={false} />
              <XAxis dataKey="label" fontSize={9} stroke="#999" tickLine={false} axisLine={false} minTickGap={50} />
              <YAxis yAxisId="left" fontSize={9} stroke="#999" tickLine={false} axisLine={false} tickFormatter={v => `$${(v / 1000).toFixed(0)}k`} />
              <YAxis yAxisId="right" orientation="right" fontSize={9} stroke="#999" tickLine={false} axisLine={false}
                tickFormatter={v => `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`} />
              <Tooltip
                contentStyle={{ fontFamily: 'var(--font-mono)', fontSize: '11px', border: '2px solid #111', borderRadius: 0 }}
                formatter={(val: number, name: string, props: { payload?: Record<string, number> }) => {
                  if (name.endsWith('_pnl_pct')) return null;
                  const strat = name.replace('_pnl_w', '');
                  const pct = props.payload?.[`${strat}_pnl_pct`];
                  const pctStr = pct != null ? ` (${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%)` : '';
                  return [`${fmtMoney(val)}${pctStr}`, LABELS[strat] || strat.toUpperCase()];
                }}
                labelFormatter={(label: string) => `Date: ${label}`}
              />
              <ReferenceLine yAxisId="left" y={0} stroke="#111" strokeWidth={1} />
              {activeKeys.filter(k => activeStrategies.has(k)).map(k => (
                <Area key={k} yAxisId="left" type="stepAfter" dataKey={`${k}_pnl_w`}
                  stroke={COLORS[k]} fill={COLORS[k]} fillOpacity={0.08}
                  strokeWidth={k === totalKey ? 1.5 : 1} dot={false} name={`${k}_pnl_w`} />
              ))}
              {viewMode === 'master' && BENCHMARK_KEYS.filter(bm => activeStrategies.has(bm)).map(bm => (
                <Area key={bm} yAxisId="left" type="stepAfter" dataKey={`${bm}_pnl_w`}
                  stroke={COLORS[bm]} strokeDasharray="5 4" fill="transparent"
                  strokeWidth={1} dot={false} name={`${bm}_pnl_w`} />
              ))}
              <Area yAxisId="right" type="stepAfter" dataKey={`${totalKey}_pnl_pct`} stroke="transparent" fill="transparent" dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        </SizedChart>
      </div>

      {/* Footer metadata */}
      <div style={{ fontSize: '9px', color: '#999', letterSpacing: '.04em', textTransform: 'uppercase' }}>
        {viewMode === 'strategies'
          ? `${filteredData.length} Trading Days · MRPT + MTFS + SSRS Strategies`
          : `${filteredData.length} Trading Days · Master AI Portfolio · Equal 1/3 (Strategies / SSRS / AISS)`
        }
      </div>
    </div>
  );
}
