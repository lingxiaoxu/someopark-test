import React, { useState, useEffect, useMemo, useCallback } from 'react';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import { API_BASE, apiHeaders } from '../../lib/api';

// 实时净值看板(controller M7)。数据:controller/output 经 /api/controller-nav。
// UI 借鉴 StrategyPerformanceViewer(同配色/布局语言);历史日频看官方 viewer,
// 本看板 = 永远今天的分钟级(分工互补,plan §4.3)。

const COLORS: Record<string, string> = {
  MRPT: '#2563eb', MTFS: '#f59e0b', SSRS: '#16a34a', AISS: '#a855f7',
  BDC: '#e11d48', PORTFOLIO: '#111',
};
const FREQS = [
  { key: '1m', minutes: 1 }, { key: '5m', minutes: 5 },
  { key: '15m', minutes: 15 }, { key: '60m', minutes: 60 },
];
const POLL_MS = 45_000;

interface NavNode {
  node_id: string; display_name: string; kind: string; value: number;
  parent_id?: string | null; positions_as_of?: string | null;
}
interface NavLatest {
  ts: string; structure_hash: string; stale: boolean; market: string;
  feed_delay_min: number | null; missing: string[]; nodes: NavNode[];
}

export default function RealtimeNavViewer({ params }: { params?: any }) {
  const [latest, setLatest] = useState<NavLatest | null>(null);
  const [streamRows, setStreamRows] = useState<any[]>([]);
  const [reconcile, setReconcile] = useState<any>(null);
  const [freq, setFreq] = useState('1m');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [structHash, setStructHash] = useState<string>('');
  const [structFlash, setStructFlash] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const poll = useCallback(async () => {
    try {
      const [l, s, r] = await Promise.all([
        fetch(`${API_BASE}/api/controller-nav/latest`, { headers: apiHeaders() }),
        fetch(`${API_BASE}/api/controller-nav/stream`, { headers: apiHeaders() }),
        fetch(`${API_BASE}/api/controller-nav/reconcile`, { headers: apiHeaders() }),
      ]);
      if (!l.ok) throw new Error(`controller not running (HTTP ${l.status})`);
      const lj: NavLatest = await l.json();
      setLatest(lj);
      if (s.ok) setStreamRows((await s.json()).rows || []);
      if (r.ok) setReconcile(await r.json());
      setError(null);
      // 持仓变化即时提示(structure_hash 变 → 闪烁标记,plan §4.3)
      setStructHash(prev => {
        if (prev && prev !== lj.structure_hash) {
          setStructFlash(true);
          setTimeout(() => setStructFlash(false), 8000);
        }
        return lj.structure_hash;
      });
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => clearInterval(id);
  }, [poll]);

  const strategies = useMemo(
    () => (latest?.nodes || []).filter(n => n.kind === 'strategy'), [latest]);
  const portfolio = useMemo(
    () => (latest?.nodes || []).find(n => n.kind === 'portfolio'), [latest]);
  const childrenOf = useCallback((pid: string) =>
    (latest?.nodes || []).filter(n => n.parent_id === pid && n.kind !== 'strategy'),
    [latest]);

  // 当日曲线:按频率子采样;基准=每节点当日首笔(日内 % 自洽口径)
  const chart = useMemo(() => {
    if (!streamRows.length) return { data: [] as any[], names: [] as string[] };
    const step = FREQS.find(f => f.key === freq)!.minutes;
    const wanted = new Set(['PORTFOLIO', ...strategies.map(s => s.display_name)]);
    const byTs: Record<string, any> = {};
    const first: Record<string, number> = {};
    for (const r of streamRows) {
      const name = r.display_name as string;
      if (!wanted.has(name)) continue;
      const t = new Date(r.ts as string);
      if (step > 1 && t.getUTCMinutes() % step !== 0) continue;
      const label = r.ts.slice(11, 16);
      if (first[name] === undefined) first[name] = r.value as number;
      (byTs[label] ||= { label })[name] = ((r.value as number) / first[name] - 1) * 100;
    }
    return { data: Object.values(byTs), names: [...wanted] };
  }, [streamRows, freq, strategies]);

  const dayPct = (name: string, value: number) => {
    const f = streamRows.find(r => r.display_name === name);
    return f ? ((value / (f.value as number)) - 1) * 100 : null;
  };

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={`实时净值:${error}(controller 未运行?)`} />;
  if (!latest) return <ErrorState message="no data" />;

  const verdictColor = reconcile?.verdict === 'ok' ? '#16a34a'
    : reconcile?.verdict === 'breach' ? '#e11d48' : '#999';

  return (
    <div className="h-full flex flex-col gap-3 p-1 overflow-auto">
      {/* 头部:PORTFOLIO 大数字 + 状态徽标 */}
      <div className="flex items-end gap-4 flex-wrap shrink-0">
        <div>
          <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: '.08em', color: '#666' }}>
            PORTFOLIO(实时 · {latest.ts.slice(11, 19)} UTC)
          </div>
          <div style={{ fontSize: 30, fontWeight: 800 }}>
            ${portfolio?.value.toLocaleString(undefined, { maximumFractionDigits: 0 })}
            {portfolio && dayPct('PORTFOLIO', portfolio.value) !== null && (
              <span style={{ fontSize: 15, marginLeft: 10,
                color: dayPct('PORTFOLIO', portfolio.value)! >= 0 ? '#16a34a' : '#e11d48' }}>
                {dayPct('PORTFOLIO', portfolio.value)! >= 0 ? '+' : ''}
                {dayPct('PORTFOLIO', portfolio.value)!.toFixed(2)}% 日内
              </span>
            )}
          </div>
        </div>
        <div className="flex gap-2 flex-wrap" style={{ fontSize: 10, fontWeight: 700 }}>
          <span style={{ padding: '3px 8px', border: '1px solid #ccc' }}>
            market: {latest.market}
          </span>
          {latest.feed_delay_min != null && (
            <span style={{ padding: '3px 8px', border: '1px solid #ccc' }}>
              feed delay {latest.feed_delay_min.toFixed(1)}m
            </span>
          )}
          {latest.stale && (
            <span style={{ padding: '3px 8px', background: '#fef3c7', border: '1px solid #f59e0b' }}>
              STALE
            </span>
          )}
          <span style={{ padding: '3px 8px', border: `1px solid ${verdictColor}`, color: verdictColor }}>
            vs official EOD: {reconcile?.verdict ?? '—'}
          </span>
          {structFlash && (
            <span style={{ padding: '3px 8px', background: '#dbeafe', border: '1px solid #2563eb' }}>
              持仓已更新 · 结构 {latest.structure_hash.slice(0, 8)}
            </span>
          )}
        </div>
        {/* 频率选择 */}
        <div style={{ marginLeft: 'auto', display: 'flex', border: '2px solid #111' }}>
          {FREQS.map(f => (
            <button key={f.key} onClick={() => setFreq(f.key)} style={{
              padding: '4px 10px', fontSize: 10, fontWeight: 700, cursor: 'pointer',
              background: freq === f.key ? '#111' : 'transparent',
              color: freq === f.key ? '#fff' : '#111', border: 'none',
            }}>{f.key}</button>
          ))}
        </div>
      </div>

      {/* Scorecard:策略卡(点开 = 层级展开中间层) */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(170px,1fr))', gap: 10 }}
        className="shrink-0">
        {strategies.map(s => {
          const pct = dayPct(s.display_name, s.value);
          const kids = childrenOf(s.node_id);
          const open = expanded.has(s.node_id);
          return (
            <div key={s.node_id}
              onClick={() => kids.length && setExpanded(prev => {
                const n = new Set(prev);
                n.has(s.node_id) ? n.delete(s.node_id) : n.add(s.node_id);
                return n;
              })}
              style={{ border: `2px solid ${COLORS[s.display_name] || '#111'}`,
                       padding: '8px 10px', cursor: kids.length ? 'pointer' : 'default' }}>
              <div style={{ fontSize: 10, fontWeight: 800, color: COLORS[s.display_name] }}>
                {s.display_name}{kids.length ? (open ? ' ▾' : ' ▸') : ''}
              </div>
              <div style={{ fontSize: 17, fontWeight: 700 }}>
                ${s.value.toLocaleString(undefined, { maximumFractionDigits: 0 })}
              </div>
              <div style={{ fontSize: 10.5 }}>
                {pct !== null && (
                  <span style={{ color: pct >= 0 ? '#16a34a' : '#e11d48', fontWeight: 700 }}>
                    {pct >= 0 ? '+' : ''}{pct.toFixed(2)}% 日内
                  </span>
                )}
                <span style={{ color: '#999', marginLeft: 6 }}>
                  as of {s.positions_as_of ?? '—'}
                </span>
              </div>
              {open && kids.map(k => (
                <div key={k.node_id} style={{ fontSize: 10.5, display: 'flex',
                  justifyContent: 'space-between', borderTop: '1px dashed #ddd',
                  padding: '3px 0' }}>
                  <span>└ {k.display_name}</span>
                  <span style={{ fontWeight: 700 }}>
                    ${k.value.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                  </span>
                </div>
              ))}
            </div>
          );
        })}
      </div>

      {/* 当日日内曲线(% vs 当日首笔;策略 + PORTFOLIO) */}
      <div className="flex-1 min-h-[260px]">
        <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.08em', color: '#666', marginBottom: 4 }}>
          当日日内收益 %({freq} 采样 · 基准=当日首笔 · 历史日频请看 Strategy Performance)
        </div>
        {chart.data.length < 2 ? (
          <div style={{ color: '#999', fontSize: 12, padding: 20 }}>
            当日数据不足两笔(controller interval 运行中会持续累积)
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="90%">
            <LineChart data={chart.data}>
              <CartesianGrid strokeDasharray="2 4" stroke="#eee" />
              <XAxis dataKey="label" tick={{ fontSize: 10 }} />
              <YAxis tick={{ fontSize: 10 }} tickFormatter={(v: number) => `${v.toFixed(2)}%`} />
              <Tooltip formatter={(v: any) => `${Number(v).toFixed(3)}%`} />
              {chart.names.map(n => (
                <Line key={n} dataKey={n} dot={false} strokeWidth={n === 'PORTFOLIO' ? 2.4 : 1.4}
                  stroke={COLORS[n] || '#888'} isAnimationActive={false} connectNulls />
              ))}
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
