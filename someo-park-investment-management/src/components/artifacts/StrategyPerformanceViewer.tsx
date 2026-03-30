import React, { useState, useMemo, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
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


export default function StrategyPerformanceViewer() {
  const { t } = useTranslation();
  const [data, setData] = useState<DayData[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeStrategies, setActiveStrategies] = useState<Set<string>>(new Set(['mrpt', 'mtfs', 'combined']));
  const [startDate, setStartDate] = useState<string>('');
  const [endDate, setEndDate] = useState<string>('');

  useEffect(() => {
    fetch(`${API_BASE}/data/strategy_performance.json`, { headers: apiHeaders() })
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then((d: DayData[]) => {
        setData(d);
        if (d.length > 0) {
          setStartDate(d[0].date);
          setEndDate(d[d.length - 1].date);
        }
      })
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

  // Recompute drawdown relative to filtered window
  const windowData = useMemo(() => {
    if (filteredData.length === 0) return [];
    const first = filteredData[0];

    // Track peaks within the window for DD
    let mrptPeak = first.mrpt_equity;
    let mtfsPeak = first.mtfs_equity;
    let combPeak = first.combined_equity;

    return filteredData.map((d, i) => {
      if (d.mrpt_equity > mrptPeak) mrptPeak = d.mrpt_equity;
      if (d.mtfs_equity > mtfsPeak) mtfsPeak = d.mtfs_equity;
      if (d.combined_equity > combPeak) combPeak = d.combined_equity;

      const prevEq = i > 0 ? filteredData[i - 1].combined_equity : d.combined_equity;
      return {
        ...d,
        mrpt_ret: ((d.mrpt_equity - first.mrpt_equity) / first.mrpt_equity) * 100,
        mtfs_ret: ((d.mtfs_equity - first.mtfs_equity) / first.mtfs_equity) * 100,
        combined_ret: ((d.combined_equity - first.combined_equity) / first.combined_equity) * 100,
        // Drawdown recomputed for this window
        mrpt_dd_w: (d.mrpt_equity - mrptPeak) / mrptPeak * 100,
        mtfs_dd_w: (d.mtfs_equity - mtfsPeak) / mtfsPeak * 100,
        combined_dd_w: (d.combined_equity - combPeak) / combPeak * 100,
        // DD $ from peak
        mrpt_dd_dollar: d.mrpt_equity - mrptPeak,
        mtfs_dd_dollar: d.mtfs_equity - mtfsPeak,
        combined_dd_dollar: d.combined_equity - combPeak,
        // PnL
        mrpt_pnl_w: i > 0 ? d.mrpt_equity - filteredData[i - 1].mrpt_equity : 0,
        mtfs_pnl_w: i > 0 ? d.mtfs_equity - filteredData[i - 1].mtfs_equity : 0,
        combined_pnl_w: i > 0 ? d.combined_equity - filteredData[i - 1].combined_equity : 0,
        // PnL %
        mrpt_pnl_pct: i > 0 ? ((d.mrpt_equity - filteredData[i - 1].mrpt_equity) / filteredData[i - 1].mrpt_equity) * 100 : 0,
        mtfs_pnl_pct: i > 0 ? ((d.mtfs_equity - filteredData[i - 1].mtfs_equity) / filteredData[i - 1].mtfs_equity) * 100 : 0,
        combined_pnl_pct: i > 0 ? (d.combined_pnl / prevEq) * 100 : 0,
        combined_dd_eq: d.combined_equity,
        label: d.date.slice(5), // MM-DD
      };
    });
  }, [filteredData]);

  // Auto-fit Y domain for return %
  const [retYMin, retYMax] = useMemo(() => {
    if (windowData.length === 0) return [-3, 15];
    const vals: number[] = [];
    for (const d of windowData) {
      if (activeStrategies.has('mrpt')) vals.push(d.mrpt_ret);
      if (activeStrategies.has('mtfs')) vals.push(d.mtfs_ret);
      if (activeStrategies.has('combined')) vals.push(d.combined_ret);
    }
    if (vals.length === 0) return [-3, 15];
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const range = max - min || 5;
    const pad = range * 0.15;
    return [Math.floor((min - pad) * 2) / 2, Math.ceil((max + pad) * 2) / 2];
  }, [windowData, activeStrategies]);

  // Right axis: combined equity $
  const firstRow0 = filteredData?.[0];
  const combEqMin = firstRow0 ? firstRow0.combined_equity * (1 + retYMin / 100) : 880000;
  const combEqMax = firstRow0 ? firstRow0.combined_equity * (1 + retYMax / 100) : 1020000;

  // Current weight ratio (from latest data point in window)
  const weightPcts = useMemo(() => {
    if (filteredData.length === 0) return { mrpt: 50, mtfs: 50 };
    const last = filteredData[filteredData.length - 1];
    const total = last.mrpt_equity + last.mtfs_equity;
    const mrpt = Math.round(last.mrpt_equity / total * 100);
    return { mrpt, mtfs: 100 - mrpt };
  }, [filteredData]);

  // Scorecard stats (computed over filtered window)
  const stats = useMemo(() => {
    if (filteredData.length === 0) return null;
    const last = filteredData[filteredData.length - 1];
    const firstRow = filteredData[0];

    const calcStats = (eqKey: keyof DayData, ddKey: string) => {
      const startEq = firstRow[eqKey] as number;
      const ret = ((last[eqKey] as number) - startEq) / startEq * 100;
      // Use window-recomputed DD
      const maxDD = Math.min(...windowData.map(d => (d as Record<string, number>)[ddKey] ?? 0));
      const dailyReturns = filteredData.map((d, i) => {
        if (i === 0) return 0;
        const prev = filteredData[i - 1][eqKey] as number;
        return ((d[eqKey] as number) - prev) / prev;
      }).slice(1);
      const mean = dailyReturns.reduce((a, b) => a + b, 0) / dailyReturns.length;
      const std = Math.sqrt(dailyReturns.reduce((a, b) => a + (b - mean) ** 2, 0) / dailyReturns.length);
      const sharpe = std > 0 ? (mean / std) * Math.sqrt(252) : 0;
      const winDays = dailyReturns.filter(r => r > 0).length;
      const winRate = dailyReturns.length > 0 ? (winDays / dailyReturns.length) * 100 : 0;
      return {
        totalReturn: ret,
        maxDD,
        sharpe,
        winRate,
        finalEquity: last[eqKey] as number,
        totalPnL: (last[eqKey] as number) - startEq,
      };
    };

    return {
      mrpt: calcStats('mrpt_equity', 'mrpt_dd_w'),
      mtfs: calcStats('mtfs_equity', 'mtfs_dd_w'),
      combined: calcStats('combined_equity', 'combined_dd_w'),
    };
  }, [filteredData, windowData]);

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

      {/* Strategy toggles + date range */}
      <div className="flex items-center gap-2 shrink-0 flex-wrap">
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
            {key === 'combined' ? `${t('strategyPerf.combined')} (${weightPcts.mrpt}/${weightPcts.mtfs})` : t(`strategyPerf.${key}`)}
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

      {/* Scorecard */}
      <div className="grid grid-cols-3 gap-3 shrink-0">
        {(['mrpt', 'mtfs', 'combined'] as const).filter(k => activeStrategies.has(k)).map(key => {
          const s = stats[key];
          return (
            <div key={key} style={{ background: '#fff', border: '2px solid #111', padding: '12px' }}>
              <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '.08em', textTransform: 'uppercase', color: COLORS[key], marginBottom: '8px' }}>
                {t(`strategyPerf.${key}`)}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px', fontSize: '11px' }}>
                <div>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>{t('strategyPerf.return')}</div>
                  <div style={{ fontWeight: 700, color: s.totalReturn >= 0 ? '#16a34a' : '#dc2626' }}>{fmtPct(s.totalReturn)}</div>
                </div>
                <div>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>{t('strategyPerf.sharpe')}</div>
                  <div style={{ fontWeight: 700 }}>{s.sharpe.toFixed(2)}</div>
                </div>
                <div>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>{t('strategyPerf.maxDD')}</div>
                  <div style={{ fontWeight: 700, color: '#dc2626' }}>{s.maxDD.toFixed(2)}%</div>
                </div>
                <div>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>{t('strategyPerf.winRate')}</div>
                  <div style={{ fontWeight: 700 }}>{s.winRate.toFixed(0)}%</div>
                </div>
                <div style={{ gridColumn: 'span 2' }}>
                  <div style={{ color: '#888', fontSize: '9px', textTransform: 'uppercase' }}>{t('strategyPerf.netPnL')}</div>
                  <div style={{ fontWeight: 700, color: s.totalPnL >= 0 ? '#16a34a' : '#dc2626' }}>{fmtMoney(s.totalPnL)}</div>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Equity Curve (% and $) */}
      <div style={{ background: '#fff', border: '2px solid #111', padding: '16px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', marginBottom: '12px' }}>
          {t('strategyPerf.equityCurveTitle')}
        </div>
        <div style={{ height: 280 }}>
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
                  const label = strat.toUpperCase();
                  const eq = props.payload?.[`${strat}_equity`];
                  const eqStr = eq != null ? ` (${fmtMoney(eq)})` : '';
                  return [`${val >= 0 ? '+' : ''}${val.toFixed(2)}%${eqStr}`, label];
                }}
                labelFormatter={(label: string) => `${t('strategyPerf.date')}: ${label}`}
              />
              <ReferenceLine yAxisId="ret" y={0} stroke="#ccc" strokeDasharray="4 4" />
              {activeStrategies.has('mrpt') && <Line yAxisId="ret" type="monotone" dataKey="mrpt_ret" stroke={COLORS.mrpt} strokeWidth={2} dot={false} name="mrpt_ret" />}
              {activeStrategies.has('mtfs') && <Line yAxisId="ret" type="monotone" dataKey="mtfs_ret" stroke={COLORS.mtfs} strokeWidth={2} dot={false} name="mtfs_ret" />}
              {activeStrategies.has('combined') && <Line yAxisId="ret" type="monotone" dataKey="combined_ret" stroke={COLORS.combined} strokeWidth={2.5} dot={false} name="combined_ret" />}
              <Line yAxisId="eq" type="monotone" dataKey="combined_equity" stroke="transparent" strokeWidth={0} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Drawdown (% and $) */}
      <div style={{ background: '#fff', border: '2px solid #111', padding: '16px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', marginBottom: '12px' }}>
          {t('strategyPerf.drawdownTitle')}
        </div>
        <div style={{ height: 180 }}>
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
                  if (name === 'combined_dd_dollar') return null;
                  const strat = name.replace('_dd_w', '');
                  const label = strat.toUpperCase();
                  const dollar = props.payload?.[`${strat}_dd_dollar`];
                  const dollarStr = dollar != null ? ` (${fmtMoney(dollar)})` : '';
                  return [`${val.toFixed(2)}%${dollarStr}`, label];
                }}
                labelFormatter={(label: string) => `${t('strategyPerf.date')}: ${label}`}
              />
              <ReferenceLine yAxisId="left" y={0} stroke="#111" strokeWidth={1} />
              {activeStrategies.has('mrpt') && <Area yAxisId="left" type="monotone" dataKey="mrpt_dd_w" stroke={COLORS.mrpt} fill={COLORS.mrpt} fillOpacity={0.1} strokeWidth={1.5} dot={false} name="mrpt_dd_w" />}
              {activeStrategies.has('mtfs') && <Area yAxisId="left" type="monotone" dataKey="mtfs_dd_w" stroke={COLORS.mtfs} fill={COLORS.mtfs} fillOpacity={0.1} strokeWidth={1.5} dot={false} name="mtfs_dd_w" />}
              {activeStrategies.has('combined') && <Area yAxisId="left" type="monotone" dataKey="combined_dd_w" stroke={COLORS.combined} fill={COLORS.combined} fillOpacity={0.08} strokeWidth={2} dot={false} name="combined_dd_w" />}
              <Area yAxisId="right" type="monotone" dataKey="combined_dd_dollar" stroke="transparent" fill="transparent" dot={false} name="combined_dd_dollar" />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Daily P&L ($ and %) */}
      <div style={{ background: '#fff', border: '2px solid #111', padding: '16px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', marginBottom: '12px' }}>
          {t('strategyPerf.dailyPnLTitle')}
        </div>
        <div style={{ height: 160 }}>
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
                  if (name === 'combined_pnl_pct') return null;
                  const strat = name.replace('_pnl_w', '');
                  const label = strat.toUpperCase();
                  const pct = props.payload?.[`${strat}_pnl_pct`];
                  const pctStr = pct != null ? ` (${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%)` : '';
                  return [`${fmtMoney(val)}${pctStr}`, label];
                }}
                labelFormatter={(label: string) => `${t('strategyPerf.date')}: ${label}`}
              />
              <ReferenceLine yAxisId="left" y={0} stroke="#111" strokeWidth={1} />
              {activeStrategies.has('combined') && <Area yAxisId="left" type="stepAfter" dataKey="combined_pnl_w" stroke={COLORS.combined} fill={COLORS.combined} fillOpacity={0.08} strokeWidth={1.5} dot={false} name="combined_pnl_w" />}
              {activeStrategies.has('mrpt') && <Area yAxisId="left" type="stepAfter" dataKey="mrpt_pnl_w" stroke={COLORS.mrpt} fill={COLORS.mrpt} fillOpacity={0.08} strokeWidth={1} dot={false} name="mrpt_pnl_w" />}
              {activeStrategies.has('mtfs') && <Area yAxisId="left" type="stepAfter" dataKey="mtfs_pnl_w" stroke={COLORS.mtfs} fill={COLORS.mtfs} fillOpacity={0.08} strokeWidth={1} dot={false} name="mtfs_pnl_w" />}
              <Area yAxisId="right" type="stepAfter" dataKey="combined_pnl_pct" stroke="transparent" fill="transparent" dot={false} name="combined_pnl_pct" />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Footer metadata */}
      <div style={{ fontSize: '9px', color: '#999', letterSpacing: '.04em', textTransform: 'uppercase' }}>
        {t('strategyPerf.footer', { days: filteredData.length, mrptPct: weightPcts.mrpt, mtfsPct: weightPcts.mtfs })}
      </div>
    </div>
  );
}
