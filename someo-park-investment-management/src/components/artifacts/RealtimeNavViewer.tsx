import React, { useState, useEffect, useMemo, useCallback } from 'react';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  ReferenceLine,
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
// 全部时间显示 ET(与 NYSE 交易时段一致);Intl 实例复用(逐行 format 才够快)
const ET_HM = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false });
const ET_HMS = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });

interface Holding { id: string; name: string; shares: number; value: number }
interface NavNode {
  node_id: string; display_name: string; kind: string; value: number;
  parent_id?: string | null; positions_as_of?: string | null; corp_action?: boolean;
  holdings?: Holding[] | null;
}
interface NavLatest {
  ts: string; structure_hash: string; stale: boolean; market: string;
  feed_delay_min: number | null; missing: string[]; nodes: NavNode[];
  last_rebuild_ts?: string | null; structure_diff?: string[];
  corp_actions?: Record<string, string>;
}
// 官方口径映射(与 reconcile_eod._ANCHORS 一致;display_name → official key)
const OFFICIAL_KEY: Record<string, string> = {
  MRPT: 'mrpt', MTFS: 'mtfs', SSRS: 'ssrs', AISS: 'aiss', BDC: 'bdc',
};

export default function RealtimeNavViewer({ params }: { params?: any }) {
  const [latest, setLatest] = useState<NavLatest | null>(null);
  const [streamRows, setStreamRows] = useState<any[]>([]);          // 今天的流(日内基准用)
  const [chartStream, setChartStream] = useState<{ date: string | null, rows: any[], isToday: boolean }>(
    { date: null, rows: [], isToday: true });                       // 图表用:闭市回看最近时段
  const [reconcile, setReconcile] = useState<any>(null);
  const [prevClose, setPrevClose] = useState<{ date: string | null, values: Record<string, number> } | null>(null);
  const [official, setOfficial] = useState<Record<string, { date: string, value: number }> | null>(null);
  const [freq, setFreq] = useState('1m');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [structHash, setStructHash] = useState<string>('');
  const [structFlash, setStructFlash] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const poll = useCallback(async () => {
    try {
      const [l, s, r, p, o] = await Promise.all([
        fetch(`${API_BASE}/api/controller-nav/latest`, { headers: apiHeaders() }),
        fetch(`${API_BASE}/api/controller-nav/stream`, { headers: apiHeaders() }),
        fetch(`${API_BASE}/api/controller-nav/reconcile`, { headers: apiHeaders() }),
        fetch(`${API_BASE}/api/controller-nav/prev-close`, { headers: apiHeaders() }),
        fetch(`${API_BASE}/api/controller-nav/official`, { headers: apiHeaders() }),
      ]);
      if (!l.ok) throw new Error(`controller not running (HTTP ${l.status})`);
      const lj: NavLatest = await l.json();
      setLatest(lj);
      if (s.ok) {
        const sj = await s.json();
        const rows = sj.rows || [];
        setStreamRows(rows);
        // 判据=不同时间戳数(闭市日可能只有强制 tick 的一两笔,画不成线)
        const nTicks = new Set(rows.map((r: any) => r.ts)).size;
        if (nTicks >= 2) {
          setChartStream({ date: sj.date, rows, isToday: true });
        } else {
          // 今天没有(足够的)tick(闭市)→ 回看最近一个有数据的交易时段
          const lt = await fetch(`${API_BASE}/api/controller-nav/stream?date=latest`,
            { headers: apiHeaders() });
          if (lt.ok) {
            const lj2 = await lt.json();
            setChartStream({ date: lj2.date, rows: lj2.rows || [],
              isToday: lj2.date === sj.date });
          }
        }
      }
      if (r.ok) setReconcile(await r.json());
      if (p.ok) setPrevClose(await p.json());
      if (o.ok) setOfficial(await o.json());
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

  // 开盘锚(plan §4.3 吻合契约):日内 % 基准 = 前一交易日 controller 收盘
  // (16:00 ET 截断,与官方 EOD 同时点);无昨收(首日)才退回当日首笔,如实标注。
  const prevByName = useMemo(() => {
    const m: Record<string, number> = {};
    if (!prevClose?.values || !latest) return m;
    for (const n of latest.nodes) {
      const v = prevClose.values[n.node_id];
      if (v !== undefined) m[n.display_name] = v;
    }
    return m;
  }, [prevClose, latest]);
  const anchored = Object.keys(prevByName).length > 0;

  const chart = useMemo(() => {
    const rows = chartStream.rows;
    if (!rows.length) return { data: [] as any[], names: [] as string[] };
    const step = FREQS.find(f => f.key === freq)!.minutes;
    const wanted = new Set(['PORTFOLIO', ...strategies.map(s => s.display_name)]);
    const byTs: Record<string, any> = {};
    // 昨收锚只对"今天"成立;回看历史时段用该时段首笔
    const base: Record<string, number> = chartStream.isToday ? { ...prevByName } : {};
    for (const r of rows) {
      const name = r.display_name as string;
      if (!wanted.has(name)) continue;
      const t = new Date(r.ts as string);
      if (step > 1 && t.getUTCMinutes() % step !== 0) continue;
      const label = ET_HM.format(t);
      if (base[name] === undefined) base[name] = r.value as number;
      (byTs[label] ||= { label })[name] = ((r.value as number) / base[name] - 1) * 100;
    }
    const data = Object.values(byTs);
    // 0% 垂直居中:对称 Y 域(上=正收益,下=负收益),平线时最小 ±0.5%
    let lim = 0.5;
    for (const row of data) {
      for (const k of Object.keys(row)) {
        if (k !== 'label') lim = Math.max(lim, Math.abs(row[k]));
      }
    }
    lim = Math.ceil(lim * 1.15 * 100) / 100;
    return { data, names: [...wanted], lim };
  }, [chartStream, freq, strategies, prevByName]);

  const dayPct = (name: string, value: number) => {
    const base = prevByName[name]
      ?? (streamRows.find(r => r.display_name === name)?.value as number | undefined);
    return base ? (value / base - 1) * 100 : null;
  };
  // 官方口径换算(主展示口径,plan §4.3.3:与 StrategyPerformanceViewer 同刻度)
  // 账本口径与官方口径持仓相同 → 日内收益同源;绝对值 = 官方 EOD × (1+r)。
  // 闭市/无日内基准时 r=0(= 官方 EOD 本身,与官方曲线严格一致)。
  const officialAnchor = (name: string, value: number) => {
    const off = official?.[OFFICIAL_KEY[name]];
    if (!off) return null;
    const pct = dayPct(name, value);
    return { ...off, live: off.value * (1 + (pct ?? 0) / 100) };
  };

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={`实时净值:${error}(controller 未运行?)`} />;
  if (!latest) return <ErrorState message="no data" />;

  const verdictColor = reconcile?.verdict === 'ok' ? '#16a34a'
    : reconcile?.verdict === 'breach' ? '#e11d48'
    : reconcile?.verdict === 'partial' ? '#b45309' : '#999';

  return (
    <div className="h-full flex flex-col gap-3 p-1 overflow-auto">
      {/* 头部:PORTFOLIO 大数字 + 状态徽标 */}
      <div className="flex items-end gap-4 flex-wrap shrink-0">
        <div>
          <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: '.08em', color: '#666' }}>
            PORTFOLIO(实时 · {ET_HMS.format(new Date(latest.ts))} ET)
          </div>
          {(() => {
            // 主数字 = 官方口径(与 StrategyPerformanceViewer 同刻度):
            // Σ 各策略官方 EOD × (1+日内收益);官方数据不可用才退回账本合计。
            const parts = strategies.map(s => officialAnchor(s.display_name, s.value));
            const allOfficial = parts.length > 0 && parts.every(Boolean);
            const main = allOfficial
              ? parts.reduce((a, p) => a + p!.live, 0)
              : portfolio?.value ?? 0;
            const pct = portfolio ? dayPct('PORTFOLIO', portfolio.value) : null;
            // Quality check 汇总(用户令:不显示账本明细,只报各 check 是否通过)
            // 双引擎对拍:nav_latest 只在两引擎逐节点对拍通过后才发布,能读到即通过
            const rec = reconcile?.verdict;
            const qc: { label: string, state: 'pass' | 'fail' | 'pending' }[] = [
              { label: '双引擎对拍', state: 'pass' },
              { label: '价格新鲜', state: latest.stale ? 'fail' : 'pass' },
              { label: `全书报价${latest.missing?.length ? `缺${latest.missing.length}` : ''}`,
                state: latest.missing?.length ? 'fail' : 'pass' },
              { label: '持仓级对账',
                state: rec === 'ok' ? 'pass' : rec === 'breach' ? 'fail' : 'pending' },
              { label: '官方口径锚', state: allOfficial ? 'pass' : 'fail' },
            ];
            const allPass = qc.every(c => c.state === 'pass');
            const anyFail = qc.some(c => c.state === 'fail');
            return (
              <>
                <div style={{ fontSize: 30, fontWeight: 800 }}
                  title="官方口径:Σ 各策略官方 EOD × (1+日内收益)">
                  ${main.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                  {pct !== null && (
                    <span style={{ fontSize: 15, marginLeft: 10,
                      color: pct >= 0 ? '#16a34a' : '#e11d48' }}>
                      {pct >= 0 ? '+' : ''}{pct.toFixed(2)}%
                      {anchored ? ` vs 昨收${prevClose?.date ? ` ${prevClose.date.slice(4, 6)}/${prevClose.date.slice(6, 8)}` : ''}` : ' 日内'}
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 10.5, fontWeight: 700,
                  color: allPass ? '#16a34a' : anyFail ? '#e11d48' : '#b45309' }}
                  title="双引擎对拍=拍平/树两引擎逐节点一致才发布;持仓级对账=golden 持仓股数×独立官方收盘价重算(不依赖 ratio);pending=数据积累中">
                  {allPass ? '✓ Quality checks 全部通过 · '
                    : anyFail ? '✗ Quality check 未过 · ' : '◷ Quality checks · '}
                  {qc.map(c => `${c.state === 'pass' ? '✓' : c.state === 'fail' ? '✗' : '◷'}${c.label}`).join(' ')}
                </div>
              </>
            );
          })()}
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
          <span style={{ padding: '3px 8px', border: `1px solid ${verdictColor}`, color: verdictColor }}
            title="持仓级对账:golden 持仓股数 × 独立官方收盘价逐仓重算 vs controller 收盘(不依赖 ratio;官方 json 仅信息展示)">
            对账: {reconcile?.verdict ?? '—'}
          </span>
          {Object.keys(latest.corp_actions || {}).length > 0 && (
            <span style={{ padding: '3px 8px', background: '#ffedd5', border: '1px solid #ea580c' }}
              title="当日 split 生效:价格已是 split 后,持仓文件待各自 pipeline 调整(shares 以持仓文件为准)">
              SPLIT: {Object.entries(latest.corp_actions!).map(([t, r]) => `${t} ${r}`).join(' · ')}
            </span>
          )}
          {structFlash && (
            <span style={{ padding: '3px 8px', background: '#dbeafe', border: '1px solid #2563eb' }}
              title={latest.last_rebuild_ts ? `重建于 ${ET_HMS.format(new Date(latest.last_rebuild_ts))} ET` : undefined}>
              持仓已更新{latest.structure_diff?.length
                ? ` · ${latest.structure_diff.join(';')}`
                : ` · 结构 ${latest.structure_hash.slice(0, 8)}`}
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
              onClick={() => setExpanded(prev => {
                const n = new Set(prev);
                n.has(s.node_id) ? n.delete(s.node_id) : n.add(s.node_id);
                return n;
              })}
              style={{ border: `2px solid ${COLORS[s.display_name] || '#111'}`,
                       padding: '8px 10px', cursor: 'pointer' }}>
              <div style={{ fontSize: 10, fontWeight: 800, color: COLORS[s.display_name] }}>
                {s.display_name}{open ? ' ▾' : ' ▸'}
                {s.corp_action && (
                  <span title="当日 split 生效(见头部 SPLIT 徽标)"
                    style={{ color: '#ea580c', marginLeft: 4 }}>⚠︎split</span>
                )}
              </div>
              {(() => {
                const oa = officialAnchor(s.display_name, s.value);
                return (
                  <>
                    <div style={{ fontSize: 17, fontWeight: 700 }}
                      title={oa
                        ? `官方口径(官方 EOD ${oa.date} × 日内收益,与 Strategy Performance 同刻度)`
                        : '⚠ 官方 EOD 不可用,显示内部口径'}>
                      ${(oa ? oa.live : s.value).toLocaleString(undefined, { maximumFractionDigits: 0 })}
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
                  </>
                );
              })()}
              {open && (() => {
                const oa = officialAnchor(s.display_name, s.value);
                const scale = oa && s.value ? oa.live / s.value : 1;   // 子层等比换算,与卡片主数字同口径
                const leafRow = (h: Holding, indent: boolean) => (
                  <div key={h.id} style={{ fontSize: 10, display: 'flex',
                    justifyContent: 'space-between', padding: '2px 0',
                    paddingLeft: indent ? 14 : 0, color: '#555' }}>
                    <span>{h.name}
                      <span style={{ color: h.shares < 0 ? '#e11d48' : '#999', marginLeft: 5 }}>
                        {h.shares > 0 ? '+' : ''}{h.shares.toLocaleString()}
                      </span>
                    </span>
                    <span>${(h.value * scale).toLocaleString(undefined, { maximumFractionDigits: 0 })}</span>
                  </div>
                );
                if (kids.length) {
                  return kids.map(k => (
                    <div key={k.node_id}>
                      <div onClick={e => {                      // 二级展开:pair→腿 / subsector→成分股
                          e.stopPropagation();
                          if (k.holdings?.length) setExpanded(prev => {
                            const n = new Set(prev);
                            n.has(k.node_id) ? n.delete(k.node_id) : n.add(k.node_id);
                            return n;
                          });
                        }}
                        style={{ fontSize: 10.5, display: 'flex',
                          justifyContent: 'space-between', borderTop: '1px dashed #ddd',
                          padding: '3px 0', cursor: k.holdings?.length ? 'pointer' : 'default' }}>
                        <span>└ {k.display_name}{k.holdings?.length ? (expanded.has(k.node_id) ? ' ▾' : ' ▸') : ''}</span>
                        <span style={{ fontWeight: 700 }}>
                          ${(k.value * scale).toLocaleString(undefined, { maximumFractionDigits: 0 })}
                        </span>
                      </div>
                      {expanded.has(k.node_id) && k.holdings?.map(h => leafRow(h, true))}
                    </div>
                  ));
                }
                if (s.holdings?.length) {                        // 直接持股(SSRS/BDC):股票级明细
                  return (
                    <div style={{ borderTop: '1px dashed #ddd', marginTop: 2 }}>
                      {s.holdings.map(h => leafRow(h, false))}
                    </div>
                  );
                }
                return (                                          // 空仓(MRPT 0 对):全现金
                  <div style={{ fontSize: 10.5, color: '#999', borderTop: '1px dashed #ddd', padding: '3px 0' }}>
                    空仓 · 全现金
                  </div>
                );
              })()}
            </div>
          );
        })}
      </div>

      {/* 当日日内曲线(% vs 当日首笔;策略 + PORTFOLIO) */}
      <div className="flex-1 min-h-[260px]">
        <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.08em', color: '#666', marginBottom: 4 }}>
          {chartStream.isToday
            ? `当日日内收益 %(${freq} 采样 · 基准=${anchored ? '昨收(开盘锚)' : '当日首笔'} · 历史日频请看 Strategy Performance)`
            : `最近交易时段日内收益 %(${chartStream.date ? `${chartStream.date.slice(4, 6)}/${chartStream.date.slice(6, 8)}` : '—'} 回看 · 当前闭市,开盘后自动切回当日)`}
        </div>
        {chart.data.length < 2 ? (
          <div style={{ color: '#999', fontSize: 12, padding: 20 }}>
            暂无可绘制的时段数据(闭市 tick 跳过;开盘后 1 分钟一笔持续累积)
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="90%">
            <LineChart data={chart.data}>
              <CartesianGrid strokeDasharray="2 4" stroke="#eee" />
              <XAxis dataKey="label" tick={{ fontSize: 10 }} />
              <YAxis tick={{ fontSize: 10 }} domain={[-chart.lim, chart.lim]}
                tickFormatter={(v: number) => `${v.toFixed(2)}%`} />
              <ReferenceLine y={0} stroke="#bbb" strokeDasharray="4 4" />
              <Tooltip formatter={(v: any) => `${Number(v).toFixed(3)}%`} />
              {chart.names.map(n => (
                <Line key={n} dataKey={n}
                  dot={chart.data.length <= 10 ? { r: 2.5, strokeWidth: 0, fill: COLORS[n] || '#888' } : false}
                  strokeWidth={n === 'PORTFOLIO' ? 2.4 : 1.4}
                  stroke={COLORS[n] || '#888'} isAnimationActive={false} connectNulls />
              ))}
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
