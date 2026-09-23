import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  ReferenceLine,
} from 'recharts';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import { API_BASE, apiHeaders } from '../../lib/api';
import {
  ADDITIVE, MULTIPLICATIVE, OFFICIAL_KEY, buildRealtimeNavPanel, capitalPresentation,
  dayPercent, formatNavMoney, formatNavPercent, holdingsPresentation,
  officialAnchor as panelOfficialAnchor, previousByName,
  type Holding, type NavNode, type NavLatest, type Cohort, type MirrorState,
} from '../../../shared/realtimeNav';

// 实时净值看板(controller M7)。数据:controller/output 经 /api/controller-nav。
// UI 借鉴 StrategyPerformanceViewer(同配色/布局语言);历史日频看官方 viewer,
// 本看板 = 永远今天的分钟级(分工互补,plan §4.3)。

const COLORS: Record<string, string> = {
  MRPT: '#2563eb', MTFS: '#f59e0b', SSRS: '#16a34a', AISS: '#a855f7', AEUS: '#0ea5e9',
  BDC: '#e11d48', PORTFOLIO: '#111',
};
// 悬浮框固定显示顺序(用户令):策略在前,PORTFOLIO 殿后
const TOOLTIP_ORDER = ['MRPT', 'MTFS', 'SSRS', 'AISS', 'AEUS', 'BDC', 'PORTFOLIO'];
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

const COHORT_COLOR: Record<string, string> = {
  L: '#b45309', S: '#7c3aed', F: '#16a34a',
};
const usd = (v: number) =>
  `${v < 0 ? '−' : ''}$${formatNavMoney(Math.abs(v))}`;

// 资金构成——把"展开项加总 ≠ 卡片主数字"这件事画出来。
// 加性族 MRPT/MTFS:账本口径 → −C 落官方;乘性族 SSRS/AISS/BDC
// (mul 传 k):整段直接官方口径(×k,与展开行金额同刻度),底行=卡片主数字,
// 无 −C/K 段(全额缩放镜像,无退场队列)。
// 多空组合占用的资金不能净额化(2026-08-24 用户指正:132k 多 − 56k 空 ≠ 76k 占用)。
// 资金构成与 RiskManager._balance_sheet_one 同式(prime-broker market-neutral,
// 文档 .claude/plan/strategies-plan/RISK_MANAGEMENT_MODULE_PLAN.md §3):
//   L = Σmax(腿市值,0)   S = Σ|min(腿市值,0)|
//   受限现金 = 1.02×S(PB 对空头卖出所得扣 102% 抵押,不可动用)
//   融资负债 M = max(0, L + 0.02×S − E)   自由现金 = max(0, E − L − 0.02×S)
//   恒等式: 自由现金 + 受限现金 + L − S − M ≡ E(账本 equity,日内唯一可算口径)
// 官方口径杠杆另差一个 C: gross/(E−C),tooltip 里同时给出。
// 旧 K/L/S/F 退场队列仅移除展示；底层镜像数据与冻结系数保持原样。
function waterfall(s: NavNode, kids: NavNode[],
                   cohorts: Record<string, Cohort> | undefined,
                   mirror: MirrorState | null, t: (k: string, o?: any) => string,
                   mul?: { k: number }) {
  const { C, Lmv, Smv, E, restrictedCash, marginLoan, freeCash,
    gross, levLedger, levOfficial } =
    capitalPresentation(s, kids, cohorts, mirror, mul);
  const row = (label: string, v: number | null, opt?: {
    bold?: boolean; color?: string; top?: string; title?: string }) => (
    <div style={{ fontSize: 10.5, display: 'flex', justifyContent: 'space-between',
      padding: '3px 0', borderTop: opt?.top ?? '1px dashed #ddd',
      color: opt?.color ?? '#555', fontWeight: opt?.bold ? 700 : undefined }}
      title={opt?.title}>
      <span>{label}</span>
      <span>{v === null ? '—' : usd(v)}</span>
    </div>
  );
  return (
    <div style={{ marginTop: 3 }}>
      {row(t('realtimeNav.wfLong'), Lmv, { top: '1px solid #111' })}
      {row(t('realtimeNav.wfShort'), -Smv, { title: t('realtimeNav.wfShortTitle') })}
      {row(t('realtimeNav.wfRestricted'), restrictedCash,
        { title: t('realtimeNav.wfRestrictedTitle') })}
      {row(t('realtimeNav.wfFreeCash'), freeCash, { title: t('realtimeNav.wfFreeCashTitle') })}
      {row(t('realtimeNav.wfMarginLoan'), -marginLoan,
        { color: marginLoan > 0 ? '#b45309' : '#555',
          title: t('realtimeNav.wfMarginLoanTitle') })}
      {mul
        ? row(t('realtimeNav.wfNet'), E,
            { bold: true, top: '1px solid #111',
              title: t('realtimeNav.wfNetMulTitle',
                { ledger: usd(s.value), k: String(mul.k) }) })
        : (<>
            {row(t('realtimeNav.wfLedgerEq'), E,
              { top: '1px solid #111', title: t('realtimeNav.wfLedgerEqTitle') })}
            {row(`${t('realtimeNav.wfCapBase')}${C === undefined ? ' (n/a)' : ''}`,
              C === undefined ? null : -C,
              { color: C === undefined ? '#b45309' : '#555',
                title: t('realtimeNav.wfCapBaseTitle') })}
            {row(t('realtimeNav.wfNet'), C === undefined ? null : s.value - C,
              { bold: true, top: '1px solid #111' })}
          </>)}
      {row(t('realtimeNav.wfGross'), gross,
        { color: '#888',
          title: mul
            ? t('realtimeNav.wfGrossTitleMul', {
                lev: levLedger === null ? '—' : levLedger.toFixed(2) })
            : t('realtimeNav.wfGrossTitle', {
                lev: levLedger === null ? '—' : levLedger.toFixed(2),
                levOff: levOfficial === null ? '—' : levOfficial.toFixed(2) }) })}
    </div>
  );
}

export default function RealtimeNavViewer({ params }: { params?: any }) {
  const { t } = useTranslation();
  const [latest, setLatest] = useState<NavLatest | null>(null);
  const [streamRows, setStreamRows] = useState<any[]>([]);          // 今天的流(日内基准用)
  const [chartStream, setChartStream] = useState<{ date: string | null, rows: any[], isToday: boolean }>(
    { date: null, rows: [], isToday: true });                       // 图表用:闭市回看最近时段
  const [reconcile, setReconcile] = useState<any>(null);
  const [prevClose, setPrevClose] = useState<{ date: string | null, values: Record<string, number> } | null>(null);
  const [official, setOfficial] = useState<Record<string, { date: string, value: number }> | null>(null);
  const [mirror, setMirror] = useState<MirrorState | null>(null);
  const [freq, setFreq] = useState('1m');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [activeLines, setActiveLines] = useState<Set<string>>(new Set(TOOLTIP_ORDER));
  const [structHash, setStructHash] = useState<string>('');
  const [structFlash, setStructFlash] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const poll = useCallback(async () => {
    try {
      const [l, s, r, p, o, k] = await Promise.all([
        fetch(`${API_BASE}/api/controller-nav/latest`, { headers: apiHeaders() }),
        fetch(`${API_BASE}/api/controller-nav/stream`, { headers: apiHeaders() }),
        fetch(`${API_BASE}/api/controller-nav/reconcile`, { headers: apiHeaders() }),
        fetch(`${API_BASE}/api/controller-nav/prev-close`, { headers: apiHeaders() }),
        fetch(`${API_BASE}/api/controller-nav/official`, { headers: apiHeaders() }),
        fetch(`${API_BASE}/api/controller-nav/scalars`, { headers: apiHeaders() }),
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
      // 拿不到就置 null —— 不静默退回账本股数当官方口径用,叶子行会自己标黄喊出来
      const kj = k.ok ? await k.json() : null;
      setMirror(kj?.scalars ? (kj as MirrorState) : null);
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
  const prevByName = useMemo(() => previousByName(latest, prevClose), [prevClose, latest]);
  const anchored = Object.keys(prevByName).length > 0;

  const chart = useMemo(() => {
    const rows = chartStream.rows;
    if (!rows.length) return { data: [] as any[], names: [] as string[] };
    const step = FREQS.find(f => f.key === freq)!.minutes;
    const wanted = new Set(['PORTFOLIO', ...strategies.map(s => s.display_name)]);
    const byTs: Record<string, any> = {};
    // 昨收锚只对"今天"成立;回看历史时段用该时段首笔
    const base: Record<string, number> = chartStream.isToday ? { ...prevByName } : {};
    // 官方口径换算(2026-08-19,与卡片主数字同口径)。两族的做法必须不同:
    //   乘性族 SSRS/AISS/BDC:official = ledger × k ⇒ 两口径 % 恒等,沿用后端链式
    //     衔接的 day_return(记账台阶已剔除)。
    //   加性族 MRPT/MTFS:official = ledger − C(实测逐分对到分:8/19 开盘账本
    //     939,463.88 − C 585,005.91 = 354,457.97 = 官方 8/18 EOD)。这一族**不能**用
    //     day_return:已发布的 mtfs_pnl 恰等于账本净值的**原始**日内差额(8/18:
    //     −7,839.87),而 day_return 把其中一部分当记账台阶剔掉了(同日 −1.84%
    //     ≈ −17,398)。要和已发布曲线对得上,就得用原始净值比:(v−C)/(v0−C)−1。
    const names = strategies.map(s => s.display_name);
    const offBase: Record<string, number> = {};   // 官方口径的当期基准(分母)
    const capC: Record<string, number> = {};
    let addOk = true;
    for (const n of names) {
      const o = official?.[OFFICIAL_KEY[n]];
      if (!o?.value) continue;
      if (ADDITIVE.has(n)) {
        const C = mirror?.capital_base?.[OFFICIAL_KEY[n]];
        if (typeof C !== 'number') { addOk = false; continue; }
        capC[n] = C;
        // 今天:基准 = 昨收账本 − C(与官方 EOD 逐分相等);回看历史时段:用该时段首笔
        const lp = chartStream.isToday ? prevByName[n] : undefined;
        offBase[n] = lp !== undefined ? lp - C : NaN;   // NaN → 见下面首笔兜底
      } else {
        // A5:$ 轴与组合权重的分母也走恒等式 ledger×k,不用已发布的官方 EOD ——
        // 后者只在每天 09:40 pipeline 跑完才前移一天,收盘到次日 09:40 这段是**前天**的值
        // (8/20 实测五腿合计因此虚高 $81,263)。回看历史时段拿不到昨收,才退回 o.value。
        const k = mirror?.scalars?.[OFFICIAL_KEY[n]];
        const lp = chartStream.isToday ? prevByName[n] : undefined;
        offBase[n] = (typeof k === 'number' && k > 0 && lp !== undefined)
          ? lp * k : o.value;
      }
    }
    for (const r of rows) {
      const name = r.display_name as string;
      if (!wanted.has(name)) continue;
      const t = new Date(r.ts as string);
      if (step > 1 && t.getUTCMinutes() % step !== 0) continue;
      const label = ET_HM.format(t);
      let pctV: number;
      if (ADDITIVE.has(name)) {
        const C = capC[name];
        if (C === undefined) continue;               // 拿不到 C 就整条线不画
        const off = (r.value as number) - C;
        if (!Number.isFinite(offBase[name])) offBase[name] = off;   // 首笔兜底
        pctV = (off / offBase[name] - 1) * 100;
      } else {
        const dr = r.day_return;
        if (dr !== undefined && dr !== null && dr !== '') {
          pctV = Number(dr) * 100;             // 后端链式衔接后的日内收益
        } else {
          if (base[name] === undefined) base[name] = r.value as number;
          pctV = ((r.value as number) / base[name] - 1) * 100;
        }
      }
      if (name === 'PORTFOLIO') {
        (byTs[label] ||= { label }).pf_ledger = pctV;   // 组合线下面按官方权重重算
      } else {
        (byTs[label] ||= { label })[name] = pctV;
      }
    }
    const data = Object.values(byTs);
    // 右 $ 轴(与 SPV 同款双轴)与组合线的口径分母:各腿官方口径基准之和
    const canOff = addOk && names.length > 0
      && names.every(n => Number.isFinite(offBase[n]) && offBase[n]);
    const offSum = canOff ? names.reduce((a, n) => a + offBase[n], 0) : null;
    // 组合 % 必须由各腿的官方 % 按**官方本金**加权重算 —— 后端发布的 PORTFOLIO
    // day_return 是账本权重的,两口径策略权重不同,直接拿来用会和顶部卡片对不上。
    // 某一刻凑不齐五腿就留空(线上开个口子),绝不混口径填一个假点。
    for (const row of data) {
      if (canOff && names.every(n => row[n] !== undefined)) {
        row.PORTFOLIO = names.reduce((a, n) => a + row[n] * offBase[n], 0) / offSum!;
        row.pf_usd = offSum! * (1 + row.PORTFOLIO / 100);
      } else if (!canOff) {
        row.PORTFOLIO = row.pf_ledger;         // 官方锚缺失 → 如实退回账本口径组合线
      }
      delete row.pf_ledger;
    }
    return { data, names: [...wanted], offSum };
  }, [chartStream, freq, strategies, prevByName, official, mirror]);

  // 与 SPV 同款:仅按可见曲线自动缩放;保留 0% 对称域与原有留白。
  const chartLim = useMemo(() => {
    let lim = 0.5;
    const visibleNames = chart.names.filter(name => activeLines.has(name));
    for (const row of chart.data) {
      for (const name of visibleNames) {
        const value = row[name];
        if (Number.isFinite(value)) lim = Math.max(lim, Math.abs(value));
      }
    }
    return Math.ceil(lim * 1.15 * 100) / 100;
  }, [chart, activeLines]);

  // 展示规则与聊天 NAV 共用，保留面板原有的基准与回退行为。
  const dayPct = (name: string, value: number) =>
    dayPercent(name, value, latest, prevByName, streamRows);
  const officialAnchor = (name: string, value: number) =>
    panelOfficialAnchor(name, value, official, mirror);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={t('realtimeNav.errNotRunning', { err: error })} />;
  if (!latest) return <ErrorState message="no data" />;

  // 对账时效闸门(2026-08-27):verdict 本身不带年龄,8/14 那份 ok 在面板上绿了 13 天
  // (reconcile_eod.py 当时根本没有调度)。服务端给出 age_bdays 后,超 1 个交易日的
  // 裁决一律降级 —— 旧结论不许冒充今天。但 breach 不降级:陈旧可以让绿灯失效,
  // 不能让红灯闭嘴。
  const reconStale = reconcile?.stale === true;
  const reconAge = reconcile?.age_bdays ?? '?';
  const verdictColor = reconcile?.verdict === 'breach' ? '#e11d48'
    : reconStale ? '#999'
    : reconcile?.verdict === 'ok' ? '#16a34a'
    : reconcile?.verdict === 'partial' ? '#b45309' : '#999';

  const panel = buildRealtimeNavPanel({ latest, official, mirror, prevClose,
    streamRows, reconcile, nowMs: Date.now() });
  const feedDead = panel.feed.dead;
  const feedLagging = panel.feed.lagging;
  const ageTxt = panel.feed.ageText;
  const delayMin = panel.feed.delayMin;
  const priceTs = new Date(panel.feed.priceTs);

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
            const pct = panel.portfolio.day_return_pct;
            const states = panel.quality.states;
            const qc: { label: string, state: 'pass' | 'fail' | 'pending' }[] = [
              { label: t('realtimeNav.qcDual'), state: states.dual_engine_match },
              // 心跳:循环 1m 一跳(闭市平移也跳),超时=进程已死或卡住
              { label: feedDead ? t('realtimeNav.qcHeartbeatDead', { age: ageTxt })
                  : t('realtimeNav.qcHeartbeat'),
                state: states.heartbeat },
              { label: t('realtimeNav.qcFresh'), state: states.price_fresh },
              { label: latest.missing?.length
                  ? t('realtimeNav.qcQuotesMissing', { n: latest.missing.length })
                  : t('realtimeNav.qcQuotes'),
                state: states.full_book_quotes },
              { label: reconStale ? t('realtimeNav.qcReconStale', { n: reconAge })
                  : t('realtimeNav.qcRecon'),
                state: states.reconcile },
              { label: t('realtimeNav.qcAnchor'), state: states.official_anchor },
              // 持仓文件半更新窗(inventory→account 约 1 分钟)是每日常规:
              // 短窗琥珀"同步中",超 10 分钟才是真异常升级红色
              { label: t('realtimeNav.qcStruct'),
                state: states.structure_sync },
            ];
            // 沿用既有历史 QC 标记,移至同一详情行;不把冻结记录当成每日新裁决。
            if (mirror?.rolloff) qc.push({ label: t('realtimeNav.rolloffDone'), state: 'pass' });
            const allPass = panel.quality.allPass;
            const anyFail = panel.quality.anyFail;
            // 沿用共享检查的严重程度:待确认/短暂延迟为警告,实质失败为异常。
            const status = allPass ? 'pass' : anyFail ? 'fail' : 'pending';
            const statusColor = allPass ? '#16a34a' : anyFail ? '#e11d48' : '#b45309';
            const statusLabel = t(allPass ? 'realtimeNav.statusNormal'
              : anyFail ? 'realtimeNav.statusAbnormal' : 'realtimeNav.statusWarning');
            return (
              <>
                <div style={{ fontSize: 30, fontWeight: 800 }}
                  title={t('realtimeNav.mainTitle')}>
                  ${panel.portfolio.display_value}
                  {pct !== null && (
                    <span style={{ fontSize: 15, marginLeft: 10,
                      color: pct >= 0 ? '#16a34a' : '#e11d48' }}>
                      {formatNavPercent(pct)}{' '}
                      {anchored && prevClose?.date
                        ? t('realtimeNav.vsPrevClose', { date: `${prevClose.date.slice(4, 6)}/${prevClose.date.slice(6, 8)}` })
                        : t('realtimeNav.intraday')}
                    </span>
                  )}
                </div>
                <details data-nav-quality={status} className="group"
                  style={{ marginTop: 3, fontSize: 10.5, fontWeight: 700 }}>
                  <summary className="[&::-webkit-details-marker]:hidden"
                    style={{ display: 'flex', alignItems: 'center', gap: 6,
                      width: 'fit-content', cursor: 'pointer', listStyle: 'none', color: statusColor }}>
                    <span aria-hidden="true" style={{ width: 8, height: 8, borderRadius: '50%',
                      background: statusColor, flexShrink: 0 }} />
                    <span>{statusLabel}</span>
                    <span style={{ color: '#777', fontSize: 10, marginLeft: 3 }}>
                      {t('realtimeNav.qualityDetails')}
                      <span aria-hidden="true" className="inline-block transition-transform group-open:rotate-90"
                        style={{ marginLeft: 3 }}>▸</span>
                    </span>
                  </summary>
                  <div style={{ marginTop: 5, color: statusColor, lineHeight: 1.6 }}
                    title={latest.rebuild_error
                      ? `${t('realtimeNav.qcStructTitle')} — ${latest.rebuild_error}`
                      : t('realtimeNav.qcTitle')}>
                    {allPass ? `✓ ${t('realtimeNav.qcAllPass')} · `
                      : anyFail ? `✗ ${t('realtimeNav.qcFail')} · ` : `◷ ${t('realtimeNav.qcPending')} · `}
                    {qc.map(c => `${c.state === 'pass' ? '✓' : c.state === 'fail' ? '✗' : '◷'}${c.label}`).join(' ')}
                  </div>
                </details>
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
            {reconcile?.date ? ` @${String(reconcile.date).slice(5)}` : ''}
            {reconStale ? ` · ${t('realtimeNav.reconStaleTag', { n: reconAge })}` : ''}
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
          const display = panel.strategies.find(card => card.node_id === s.node_id)!;
          const pct = display.day_return_pct;
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
                        ? t(ADDITIVE.has(s.display_name)
                          ? 'realtimeNav.cardOfficialTitleAdd'
                          : 'realtimeNav.cardOfficialTitle', { date: oa.date })
                        : t('realtimeNav.cardNoOfficial')}>
                      ${display.display_value}
                    </div>
                    <div style={{ fontSize: 10.5 }}>
                      {pct !== null && (
                        <span style={{ color: pct >= 0 ? '#16a34a' : '#e11d48', fontWeight: 700 }}>
                          {formatNavPercent(pct)} {t('realtimeNav.intraday')}
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
                const scalars = mirror?.scalars;
                const oa = officialAnchor(s.display_name, s.value);
                // 乘性族(SSRS/AISS/BDC)股数按 go-live 冻结常数换算到官方口径,
                // 与 QC 缩放镜像同常数同舍入 → 面板股数 = QC 持股。金额一律不动,
                // 卡片主数字与合计仍锚在官方 EOD(performance json)上,总额不变。
                const scalable = MULTIPLICATIVE.has(s.display_name);
                // A1(加性族 MRPT/MTFS):**取消金额等比缩放**。加性族 official=ledger−C,
                // 敞口 1:1 —— 缩放会把 HPE 2,647 股显示成 $53,005(真实 $140,635),
                // 股数与金额自相矛盾。加性族的口径差改由下面的 −C 行显式画出(A2)。
                const additive = ADDITIVE.has(s.display_name);
                const scale = additive ? 1 : (oa && s.value ? oa.live / s.value : 1);
                const kf = scalable ? scalars?.[OFFICIAL_KEY[s.display_name]] : undefined;
                const missingK = scalable && !(typeof kf === 'number' && kf > 0);
                const cohorts = additive
                  ? mirror?.cohorts?.[OFFICIAL_KEY[s.display_name]] : undefined;
                const leafRow = (h: Holding, indent: boolean, mul?: Cohort) => {
                  const displayHolding = holdingsPresentation(h, s, official, mirror, mul);
                  const sh = displayHolding.shares;
                  const qc = displayHolding.qc_shares;
                  return (
                  <div key={h.id} style={{ fontSize: 10, display: 'flex',
                    justifyContent: 'space-between', padding: '2px 0',
                    paddingLeft: indent ? 14 : 0, color: '#555' }}>
                    <span>{h.name}
                      <span
                        title={missingK ? `⚠ scalar n/a · ledger ${h.shares.toLocaleString()}`
                          : scalable ? `ledger ${h.shares.toLocaleString()} × ${kf}`
                            : `ledger ${h.shares.toLocaleString()} × 1`}
                        style={{ color: missingK ? '#b45309'
                          : sh < 0 ? '#e11d48' : '#999', marginLeft: 5,
                          fontWeight: missingK ? 700 : undefined }}>
                        {missingK ? '⚠︎' : ''}{sh > 0 ? '+' : ''}{sh.toLocaleString()}
                      </span>
                      {qc !== null && (
                        <span title={mul
                            ? `QC m=${mul!.m} · ${t(`realtimeNav.cohort${mul!.cohort}`)}`
                            : t('realtimeNav.qcScaledTitle', { k: kf })}
                          style={{ color: mul ? COHORT_COLOR[mul!.cohort] : COHORT_COLOR.F,
                            marginLeft: 6 }}>
                          → QC {qc.toLocaleString()}
                        </span>
                      )}
                    </span>
                    <span>${displayHolding.display_value}</span>
                  </div>
                  );
                };
                if (kids.length) {
                  const rows = kids.map(k => {
                    const mul = cohorts?.[k.display_name];
                    // 配对行不显示净额(多空腿资金占用不能 net,与策略层瀑布同理):
                    // 有空头腿时右侧改为 多/空 两腿并示,占用资金(多头 + 空头抵押 2%,
                    // RiskManager 同式)与净值进 tooltip。纯多头行保持单数字。
                    let pL = 0, pS = 0;
                    for (const h of k.holdings || []) {
                      const v = h.value * scale;
                      if (v >= 0) pL += v; else pS += -v;
                    }
                    return (
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
                        <span>└ {k.display_name}{k.holdings?.length ? (expanded.has(k.node_id) ? ' ▾' : ' ▸') : ''}
                        </span>
                        {pS > 0 ? (
                          <span style={{ textAlign: 'right', lineHeight: 1.25 }}
                            title={t('realtimeNav.pairLegsTitle', {
                              occ: usd(pL + 0.02 * pS), long: usd(pL),
                              coll: usd(0.02 * pS), net: usd(pL - pS) })}>
                            <span style={{ fontWeight: 700, display: 'block' }}>
                              {t('realtimeNav.pairLong')} {usd(pL)}
                            </span>
                            <span style={{ color: '#e11d48', display: 'block' }}>
                              {t('realtimeNav.pairShort')} {usd(-pS)}
                            </span>
                          </span>
                        ) : (
                          <span style={{ fontWeight: 700 }}>
                            ${(k.value * scale).toLocaleString(undefined, { maximumFractionDigits: 0 })}
                          </span>
                        )}
                      </div>
                      {expanded.has(k.node_id) && k.holdings?.map(h => leafRow(h, true, mul))}
                    </div>
                    );
                  });
                  return (<>
                    {rows}
                    {additive && waterfall(s, kids, cohorts, mirror, t)}
                    {scalable && !missingK &&
                      waterfall(s, kids, undefined, mirror, t, { k: kf as number })}
                  </>);
                }
                if (s.holdings?.length) {                        // 直接持股(SSRS/BDC):股票级明细
                  return (
                    <>
                      <div style={{ borderTop: '1px dashed #ddd', marginTop: 2 }}>
                        {s.holdings.map(h => leafRow(h, false))}
                      </div>
                      {scalable && !missingK &&
                        waterfall(s, [], undefined, mirror, t, { k: kf as number })}
                    </>
                  );
                }
                return (                                          // 空仓(MRPT 0 对):全现金
                  <>
                    <div style={{ fontSize: 10.5, color: '#999', borderTop: '1px dashed #ddd', padding: '3px 0' }}>
                      {t('realtimeNav.flatCash')}
                    </div>
                    {additive && waterfall(s, [], cohorts, mirror, t)}
                  </>
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
                <YAxis yAxisId="ret" domain={[-chartLim, chartLim]}
                  fontSize={9} stroke="#999" tickLine={false} axisLine={false}
                  tickFormatter={(v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`} />
                {chart.offSum && (
                  <YAxis yAxisId="eq" orientation="right"
                    domain={[chart.offSum * (1 - chartLim / 100),
                             chart.offSum * (1 + chartLim / 100)]}
                    allowDataOverflow
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
