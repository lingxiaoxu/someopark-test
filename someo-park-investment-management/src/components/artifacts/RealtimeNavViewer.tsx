import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
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
// 悬浮框固定显示顺序(用户令):策略在前,PORTFOLIO 殿后
const TOOLTIP_ORDER = ['MRPT', 'MTFS', 'SSRS', 'AISS', 'BDC', 'PORTFOLIO'];
const tooltipRank = (k: string) => {
  const i = TOOLTIP_ORDER.indexOf(k);
  return i === -1 ? TOOLTIP_ORDER.length : i;
};
// 与 StrategyPerformanceViewer 同款:钉在绘图区左上角的紧凑悬浮框
// (margin.left=5 + 左轴 ~60px → x=70 恰好让开刻度标签)
const TOOLTIP_POS = { x: 70, y: 8 };

function CompactTooltip({ active, payload, label, renderValue }: any) {
  if (!active || !Array.isArray(payload) || payload.length === 0) return null;
  const seen = new Set<string>();
  const rows = payload
    .map((p: any) => ({ key: String(p.name ?? p.dataKey ?? ''), p }))
    .filter(({ key }: any) => COLORS[key] !== undefined)
    .filter(({ key }: any) => { if (seen.has(key)) return false; seen.add(key); return true; })
    .sort((a: any, b: any) => tooltipRank(a.key) - tooltipRank(b.key));
  if (rows.length === 0) return null;
  return (
    <div style={{
      fontFamily: 'var(--font-mono)', fontSize: '8px', lineHeight: 1.5,
      background: 'rgba(255,255,255,0.94)', border: '1px solid #111',
      padding: '3px 5px', pointerEvents: 'none',
    }}>
      <div style={{ fontWeight: 700, color: '#111', marginBottom: 1 }}>{label}</div>
      {rows.map(({ key, p }: any) => (
        <div key={key} style={{ display: 'flex', gap: 6, whiteSpace: 'nowrap' }}>
          <span style={{ color: COLORS[key], fontWeight: 700 }}>{key}</span>
          <span style={{ marginLeft: 'auto', color: '#333' }}>{renderValue(p)}</span>
        </div>
      ))}
    </div>
  );
}
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
  holdings?: Holding[] | null; day_return?: number | null;
}
interface NavLatest {
  ts: string; structure_hash: string; stale: boolean; market: string;
  feed_delay_min: number | null; missing: string[]; nodes: NavNode[];
  last_rebuild_ts?: string | null; structure_diff?: string[];
  corp_actions?: Record<string, string>; rebuild_error?: string | null;
  rebuild_error_age_s?: number | null;
}
// 官方口径映射(与 reconcile_eod._ANCHORS 一致;display_name → official key)
const OFFICIAL_KEY: Record<string, string> = {
  MRPT: 'mrpt', MTFS: 'mtfs', SSRS: 'ssrs', AISS: 'aiss', BDC: 'bdc',
};

export default function RealtimeNavViewer({ params }: { params?: any }) {
  const { t } = useTranslation();
  const [latest, setLatest] = useState<NavLatest | null>(null);
  const [streamRows, setStreamRows] = useState<any[]>([]);          // 今天的流(日内基准用)
  const [chartStream, setChartStream] = useState<{ date: string | null, rows: any[], isToday: boolean }>(
    { date: null, rows: [], isToday: true });                       // 图表用:闭市回看最近时段
  const [reconcile, setReconcile] = useState<any>(null);
  const [prevClose, setPrevClose] = useState<{ date: string | null, values: Record<string, number> } | null>(null);
  const [official, setOfficial] = useState<Record<string, { date: string, value: number }> | null>(null);
  const [freq, setFreq] = useState('1m');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [activeLines, setActiveLines] = useState<Set<string>>(new Set(TOOLTIP_ORDER));
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
      const dr = r.day_return;
      let pctV: number;
      if (dr !== undefined && dr !== null && dr !== '') {
        pctV = Number(dr) * 100;               // 后端链式衔接后的日内收益
      } else {
        if (base[name] === undefined) base[name] = r.value as number;
        pctV = ((r.value as number) / base[name] - 1) * 100;
      }
      (byTs[label] ||= { label })[name] = pctV;
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
    // 右 $ 轴(与 SPV 同款双轴):PORTFOLIO % 的官方口径 $ 等值(纯轴换算,数据不变)
    const offSum = official && Object.keys(OFFICIAL_KEY)
      .every(k => official[OFFICIAL_KEY[k]])
      ? Object.values(OFFICIAL_KEY).reduce((a, k) => a + official![k].value, 0)
      : null;
    if (offSum) {
      for (const row of data) {
        if (row.PORTFOLIO !== undefined) {
          row.pf_usd = offSum * (1 + row.PORTFOLIO / 100);
        }
      }
    }
    return { data, names: [...wanted], lim, offSum };
  }, [chartStream, freq, strategies, prevByName, official]);

  // 日内 %:优先用后端发布的 day_return(跨结构变化已链式衔接,记账台阶已剔除);
  // 老数据无此字段时退回客户端基准计算
  const dayPct = (name: string, value: number) => {
    const node = (latest?.nodes || []).find(n => n.display_name === name);
    if (node && node.day_return !== undefined && node.day_return !== null) {
      return node.day_return * 100;
    }
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
  if (error) return <ErrorState message={t('realtimeNav.errNotRunning', { err: error })} />;
  if (!latest) return <ErrorState message="no data" />;

  const verdictColor = reconcile?.verdict === 'ok' ? '#16a34a'
    : reconcile?.verdict === 'breach' ? '#e11d48'
    : reconcile?.verdict === 'partial' ? '#b45309' : '#999';

  // 心跳闸门(2026-08-14):常驻循环 1 分钟一跳,闭市也跳(平移续写)——所以
  // tick 年龄是与行情无关的存活信号。8/13 循环被一次 DNS 失败打挂后,面板仍把
  // 11 小时前的死数据标成"实时",要用户自己发现 —— 陈旧必须自己喊出来。
  const feedAgeS = Math.max(0, (Date.now() - new Date(latest.ts).getTime()) / 1000);
  const feedDead = feedAgeS > 600;
  const feedLagging = feedAgeS > 180;
  const ageTxt = feedAgeS >= 3600 ? `${Math.floor(feedAgeS / 3600)}h${Math.floor((feedAgeS % 3600) / 60)}m`
    : `${Math.floor(feedAgeS / 60)}m`;
  // 行情延迟如实标注(2026-08-14):订阅是 15 分钟延迟行情,feed_delay_min 一直
  // 如实在报却从没进过 UI ——"实时 · {tick 时刻}"把新鲜度多报了 15 分钟。
  // 主时间戳改为**价格时点**(ts − delay),延迟量写明;心跳仍看 tick 时刻。
  const delayMin = latest.feed_delay_min ?? 0;
  const priceTs = new Date(new Date(latest.ts).getTime() - delayMin * 60000);

  return (
    <div className="h-full flex flex-col gap-3 p-1 overflow-auto">
      {/* 头部:PORTFOLIO 大数字 + 状态徽标 */}
      <div className="flex items-end gap-4 flex-wrap shrink-0">
        <div>
          <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: '.08em',
                        color: feedDead ? '#e11d48' : feedLagging ? '#b45309' : '#666' }}
            title={feedDead ? t('realtimeNav.feedFrozenTitle') : undefined}>
            {feedDead
              ? t('realtimeNav.portfolioFrozen', {
                  time: ET_HMS.format(new Date(latest.ts)), age: ageTxt })
              : delayMin >= 1
                ? t('realtimeNav.portfolioDelayed', {
                    time: ET_HMS.format(priceTs), delay: Math.round(delayMin) })
                : t('realtimeNav.portfolioLive', {
                    time: ET_HMS.format(new Date(latest.ts)) })}
          </div>
          {(() => {
            // 主数字 = 官方口径(与 StrategyPerformanceViewer 同刻度):
            // Σ 各策略官方 EOD × (1+日内收益);官方数据不可用才退回账本合计。
            const parts = strategies.map(s => officialAnchor(s.display_name, s.value));
            const allOfficial = parts.length > 0 && parts.every(Boolean);
            const main = allOfficial
              ? parts.reduce((a, p) => a + p!.live, 0)
              : portfolio?.value ?? 0;
            // % 与 $ 同权重:官方口径合计的日内变化(账本权重的 PF day_return
            // 会因两口径策略权重不同而对不上主数字)
            const offSum = allOfficial ? parts.reduce((a, p) => a + p!.value, 0) : 0;
            const pct = allOfficial && offSum
              ? (main / offSum - 1) * 100
              : portfolio ? dayPct('PORTFOLIO', portfolio.value) : null;
            // Quality check 汇总(用户令:不显示账本明细,只报各 check 是否通过)
            // 双引擎对拍:nav_latest 只在两引擎逐节点对拍通过后才发布,能读到即通过
            const rec = reconcile?.verdict;
            const qc: { label: string, state: 'pass' | 'fail' | 'pending' }[] = [
              { label: t('realtimeNav.qcDual'), state: 'pass' },
              // 心跳:循环 1m 一跳(闭市平移也跳),超时=进程已死或卡住
              { label: feedDead ? t('realtimeNav.qcHeartbeatDead', { age: ageTxt })
                  : t('realtimeNav.qcHeartbeat'),
                state: feedDead ? 'fail' : feedLagging ? 'pending' : 'pass' },
              { label: t('realtimeNav.qcFresh'), state: latest.stale ? 'fail' : 'pass' },
              { label: latest.missing?.length
                  ? t('realtimeNav.qcQuotesMissing', { n: latest.missing.length })
                  : t('realtimeNav.qcQuotes'),
                state: latest.missing?.length ? 'fail' : 'pass' },
              { label: t('realtimeNav.qcRecon'),
                state: rec === 'ok' ? 'pass' : rec === 'breach' ? 'fail' : 'pending' },
              { label: t('realtimeNav.qcAnchor'), state: allOfficial ? 'pass' : 'fail' },
              // 持仓文件半更新窗(inventory→account 约 1 分钟)是每日常规:
              // 短窗琥珀"同步中",超 10 分钟才是真异常升级红色
              { label: t('realtimeNav.qcStruct'),
                state: !latest.rebuild_error ? 'pass'
                  : (latest.rebuild_error_age_s ?? 0) < 600 ? 'pending' : 'fail' },
            ];
            const allPass = qc.every(c => c.state === 'pass');
            const anyFail = qc.some(c => c.state === 'fail');
            return (
              <>
                <div style={{ fontSize: 30, fontWeight: 800 }}
                  title={t('realtimeNav.mainTitle')}>
                  ${main.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                  {pct !== null && (
                    <span style={{ fontSize: 15, marginLeft: 10,
                      color: pct >= 0 ? '#16a34a' : '#e11d48' }}>
                      {pct >= 0 ? '+' : ''}{pct.toFixed(2)}%{' '}
                      {anchored && prevClose?.date
                        ? t('realtimeNav.vsPrevClose', { date: `${prevClose.date.slice(4, 6)}/${prevClose.date.slice(6, 8)}` })
                        : t('realtimeNav.intraday')}
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 10.5, fontWeight: 700,
                  color: allPass ? '#16a34a' : anyFail ? '#e11d48' : '#b45309' }}
                  title={latest.rebuild_error
                    ? `${t('realtimeNav.qcStructTitle')} — ${latest.rebuild_error}`
                    : t('realtimeNav.qcTitle')}>
                  {allPass ? `✓ ${t('realtimeNav.qcAllPass')} · `
                    : anyFail ? `✗ ${t('realtimeNav.qcFail')} · ` : `◷ ${t('realtimeNav.qcPending')} · `}
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
            title={t('realtimeNav.reconTitle')}>
            {t('realtimeNav.reconLabel')}: {reconcile?.verdict ?? '—'}
          </span>
          {Object.keys(latest.corp_actions || {}).length > 0 && (
            <span style={{ padding: '3px 8px', background: '#ffedd5', border: '1px solid #ea580c' }}
              title={t('realtimeNav.splitTitle')}>
              SPLIT: {Object.entries(latest.corp_actions!).map(([tk, r]) => `${tk} ${r}`).join(' · ')}
            </span>
          )}
          {structFlash && (
            <span style={{ padding: '3px 8px', background: '#dbeafe', border: '1px solid #2563eb' }}
              title={latest.last_rebuild_ts ? t('realtimeNav.rebuiltAt', { time: ET_HMS.format(new Date(latest.last_rebuild_ts)) }) : undefined}>
              {t('realtimeNav.structUpdated')}{latest.structure_diff?.length
                ? ` · ${latest.structure_diff.join('; ')}`
                : ` · ${t('realtimeNav.structLabel')} ${latest.structure_hash.slice(0, 8)}`}
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
                  <span title={t('realtimeNav.splitTitle')}
                    style={{ color: '#ea580c', marginLeft: 4 }}>⚠︎{t('realtimeNav.splitTag')}</span>
                )}
              </div>
              {(() => {
                const oa = officialAnchor(s.display_name, s.value);
                return (
                  <>
                    <div style={{ fontSize: 17, fontWeight: 700 }}
                      title={oa
                        ? t('realtimeNav.cardOfficialTitle', { date: oa.date })
                        : t('realtimeNav.cardNoOfficial')}>
                      ${(oa ? oa.live : s.value).toLocaleString(undefined, { maximumFractionDigits: 0 })}
                    </div>
                    <div style={{ fontSize: 10.5 }}>
                      {pct !== null && (
                        <span style={{ color: pct >= 0 ? '#16a34a' : '#e11d48', fontWeight: 700 }}>
                          {pct >= 0 ? '+' : ''}{pct.toFixed(2)}% {t('realtimeNav.intraday')}
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
                    {t('realtimeNav.flatCash')}
                  </div>
                );
              })()}
            </div>
          );
        })}
      </div>

      {/* 当日日内曲线 — UI 与 StrategyPerformanceViewer 的 Equity Curve 同款
          (容器/标题/legend chips/双 Y 轴/线型/CompactTooltip);功能与数据不变 */}
      <div className="flex-1 min-h-[234px] max-h-[54vh]"
        style={{ background: '#fff', border: '2px solid #111', padding: '16px',
                 display: 'flex', flexDirection: 'column' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '.06em',
                      textTransform: 'uppercase', marginBottom: '4px' }}
          title={chartStream.isToday
            ? t('realtimeNav.chartToday', { freq,
                base: anchored ? t('realtimeNav.chartBasePrev') : t('realtimeNav.chartBaseFirst') })
            : t('realtimeNav.chartLookback', {
                date: chartStream.date ? `${chartStream.date.slice(4, 6)}/${chartStream.date.slice(6, 8)}` : '—' })}>
          {t('realtimeNav.chartHeader')}
          {!chartStream.isToday && chartStream.date && (
            <span style={{ color: '#999', marginLeft: 8 }}>
              {chartStream.date.slice(4, 6)}/{chartStream.date.slice(6, 8)}
            </span>
          )}
        </div>
        {/* legend chips = 显隐开关(与 SPV 同款交互:实心=显示,空心=隐藏) */}
        <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap', marginBottom: '10px' }}>
          {TOOLTIP_ORDER.map(k => (
            <button key={k}
              onClick={() => setActiveLines(prev => {
                const n = new Set(prev);
                n.has(k) ? n.delete(k) : n.add(k);
                return n;
              })}
              style={{
                padding: '4px 10px', fontSize: '10px', fontWeight: 700,
                letterSpacing: '.06em', textTransform: 'uppercase',
                border: `2px solid ${COLORS[k]}`,
                background: activeLines.has(k) ? COLORS[k] : 'transparent',
                color: activeLines.has(k) ? '#fff' : COLORS[k],
                cursor: 'pointer', transition: 'all .15s',
              }}>{k}</button>
          ))}
        </div>
        {chart.data.length < 2 ? (
          <div style={{ color: '#999', fontSize: 12, padding: 20 }}>
            {t('realtimeNav.chartEmpty')}
          </div>
        ) : (
          <div style={{ flex: 1, minHeight: 0 }}>
            <ResponsiveContainer width="100%" height="100%"
              initialDimension={{ width: 300, height: 200 }}>
              <LineChart data={chart.data} margin={{ top: 5, right: 5, left: 5, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" vertical={false} />
                <XAxis dataKey="label" fontSize={9} stroke="#999"
                  tickLine={false} axisLine={false} minTickGap={50} />
                <YAxis yAxisId="ret" domain={[-chart.lim, chart.lim]}
                  fontSize={9} stroke="#999" tickLine={false} axisLine={false}
                  tickFormatter={(v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`} />
                {chart.offSum && (
                  <YAxis yAxisId="eq" orientation="right"
                    domain={[chart.offSum * (1 - chart.lim / 100),
                             chart.offSum * (1 + chart.lim / 100)]}
                    fontSize={9} stroke="#999" tickLine={false} axisLine={false}
                    tickFormatter={(v: number) => `$${(v / 1000).toFixed(0)}k`}
                    width={40} />
                )}
                <Tooltip
                  position={TOOLTIP_POS}
                  content={
                    <CompactTooltip
                      renderValue={(p: any) => {
                        const pct = Number(p.value);
                        const base = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
                        if (p.dataKey === 'PORTFOLIO' && chart.offSum) {
                          const usd = chart.offSum * (1 + pct / 100);
                          return `${base} ($${Math.round(usd).toLocaleString()})`;
                        }
                        return base;
                      }}
                    />
                  }
                />
                <ReferenceLine yAxisId="ret" y={0} stroke="#ccc" strokeDasharray="4 4" />
                {chart.names.filter(n => activeLines.has(n)).map(n => (
                  <Line key={n} yAxisId="ret" type="monotone" dataKey={n}
                    dot={chart.data.length <= 10
                      ? { r: 2.5, strokeWidth: 0, fill: COLORS[n] || '#888' } : false}
                    strokeWidth={n === 'PORTFOLIO' ? 2.5 : 2}
                    stroke={COLORS[n] || '#888'} isAnimationActive={false}
                    connectNulls name={n} />
                ))}
                {chart.offSum && (
                  <Line yAxisId="eq" type="monotone" dataKey="pf_usd"
                    stroke="transparent" strokeWidth={0} dot={false}
                    isAnimationActive={false} />
                )}
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </div>
    </div>
  );
}
