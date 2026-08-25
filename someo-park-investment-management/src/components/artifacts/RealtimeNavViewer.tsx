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
// 账本口径 → 官方口径是**两族**变换(2026-08-19 逐日实测确认),股数只有一族该缩放:
//   乘性族 SSRS/AISS/BDC —— official = ledger × k(AISS k=2.68155435 连续 55 天、
//     SSRS k=0.9950436 连续 71 天、BDC k=1)。k 同时作用于金额**和股数**:官方那本
//     账真的持有 ledger×k 股(KLAC 账本 1,678 → 官方 4,500),所以显示股数必须乘 k,
//     否则 shares × price ≠ 同一行显示的金额 —— 这正是本次修的病。
//   加性族 MRPT/MTFS —— official = ledger − C(C 恒定到 $0.01,105 天),且官方日 P&L
//     与账本日 P&L 逐美元相等 ⇒ 敞口 1:1。**股数绝不能缩放**:MTFS 的 k(t) 实测在
//     0.32–0.50 之间漂,按它缩放会让面板一手不交易也天天变股数,并把真实敞口低报
//     2.6 倍、与已发布的 P&L 序列自相矛盾。它们的显示口径另案处理(加一行资金基准差)。
const MULTIPLICATIVE = new Set(['SSRS', 'AISS', 'BDC']);
const ADDITIVE = new Set(['MRPT', 'MTFS']);
// pairs 的 QC 镜像倍数按**开仓时刻**归队(2026-08-19 三队列),服务端已按
// (pair, open_date) 解析好,前端只取现成倍数,不在这里重实现队列判定。
interface Cohort { cohort: string; m: number }
interface MirrorState {
  scalars: Record<string, number>;
  capital_base?: Record<string, number>;          // 加性族 C:official = ledger − C
  cohorts?: Record<string, Record<string, Cohort>>;
  scaled_frozen?: boolean;
  rolloff?: {
    frozen_at: string; measured_on: string; k_equity: number;
    qc_equity?: number; panel_official_total?: number;
  } | null;
}
const COHORT_COLOR: Record<string, string> = {
  L: '#b45309', S: '#7c3aed', F: '#16a34a',
};
const COHORTS = ['L', 'S', 'F'] as const;
// QC exporter 用 Python int(round(x)) = banker 舍入到偶数;JS Math.round 是 half-up,
// 恰好 .5 时会与 QC 差 1 股。共用常数就要共用舍入,否则"面板=QC"不成立。
const bankersRound = (x: number) => {
  const f = Math.floor(x), d = x - f;
  return d > 0.5 ? f + 1 : d < 0.5 ? f : (f % 2 === 0 ? f : f + 1);
};
const usd = (v: number) =>
  `${v < 0 ? '−' : ''}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

// A2 + K 行——把"展开项加总 ≠ 卡片主数字"这件事画出来。
// 加性族 MRPT/MTFS:账本口径 → −C 落官方,附 K 三队列;乘性族 SSRS/AISS/BDC
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
// K 行是**信息行**(不参与上面的加总): QC 因队列倍数没镜像到的那部分持仓市值。
// L(m=0)与 S(m=k)都是有限寿命的退场队列 —— 两队清空后 QC 只剩 F(m=1),
// 净值层的差就此定格为常数,届时由 ops/rolloff.py 实测冻结、记到 QC 侧现金上。
function waterfall(s: NavNode, kids: NavNode[],
                   cohorts: Record<string, Cohort> | undefined,
                   mirror: MirrorState | null, t: (k: string, o?: any) => string,
                   mul?: { k: number }) {
  const st = OFFICIAL_KEY[s.display_name];
  const C = mirror?.capital_base?.[st];
  // 乘性族(mul 给定 k)整段用**官方口径**(×k)——与上方展开行显示的金额同刻度,
  // 底行直接等于卡片主数字;加性族 scale=1(账本口径),经 −C 落到官方。
  const scale = mul ? mul.k : 1;
  // 多空拆腿:逐腿按符号归边,绝不 net。kid 无 holdings 时退回 kid.value 归边;
  // 无 kids(SSRS/BDC 直接持股)取策略节点自己的 holdings。
  let Lmv = 0, Smv = 0;
  const addLeg = (v: number) => { if (v >= 0) Lmv += v; else Smv += -v; };
  if (kids.length) {
    for (const k of kids) {
      const hs = k.holdings || [];
      if (hs.length === 0) { addLeg(k.value * scale); continue; }
      for (const h of hs) addLeg(h.value * scale);
    }
  } else {
    for (const h of s.holdings || []) addLeg(h.value * scale);
  }
  const E = s.value * scale;                    // add: 账本 equity;mul: 官方净值
  const shortDue = 0.02 * Smv;                  // 102% 抵押中超出卖出所得的 2%
  const restrictedCash = 1.02 * Smv;            // PB 扣押的空头抵押
  const marginLoan = Math.max(0, Lmv + shortDue - E);
  const freeCash = Math.max(0, E - Lmv - shortDue);
  const gross = Lmv + Smv;
  // 乘性族 official = ledger×k ⇒ gross 与 E 同乘 k,杠杆两口径恒等
  const levLedger = E > 0 ? gross / E : null;
  const levOfficial = mul ? levLedger
    : (C !== undefined && E - C > 0 ? gross / (E - C) : null);
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
  // QC 未镜像的持仓市值:逐腿 (账本股数 − QC 股数) × 现价,按队列分档
  const byCohort: Record<string, number> = { L: 0, S: 0, F: 0 };
  let unknown = false;
  for (const k of kids) {
    const mul = cohorts?.[k.display_name];
    if (!mul) { unknown = true; continue; }
    for (const h of k.holdings || []) {
      if (!h.shares) continue;
      const px = h.value / h.shares;
      const qcSh = mul.m === 0 ? 0 : bankersRound(h.shares * mul.m);
      byCohort[mul.cohort] += (h.shares - qcSh) * px;
    }
  }
  const gap = byCohort.L + byCohort.S + byCohort.F;
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
      {!mul && <div style={{ fontSize: 9.5, marginTop: 4, padding: '3px 0',
        borderTop: '1px dotted #999', color: '#666' }}
        title={t('realtimeNav.wfKTitle')}>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontWeight: 700 }}>
          <span>{t('realtimeNav.wfK')}{unknown ? ` ${t('realtimeNav.wfKUnknown')}` : ''}</span>
          <span>{usd(gap)}</span>
        </div>
        {COHORTS.map(c => {
          const n = kids.filter(k => cohorts?.[k.display_name]?.cohort === c).length;
          return (
            <div key={c} style={{ display: 'flex', justifyContent: 'space-between',
              paddingLeft: 10, color: COHORT_COLOR[c] }}>
              <span>{c} · {t(`realtimeNav.cohort${c}`)} · {t('realtimeNav.wfPairs', { n })}</span>
              <span>{usd(byCohort[c])}</span>
            </div>
          );
        })}
        {mirror && mirror.scaled_frozen === false && (
          <div style={{ color: '#b45309', fontWeight: 700, paddingLeft: 10 }}>
            ⚠︎ {t('realtimeNav.wfNoScaledFreeze')}
          </div>
        )}
      </div>}
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
    // 0% 垂直居中:对称 Y 域(上=正收益,下=负收益),平线时最小 ±0.5%
    let lim = 0.5;
    for (const row of data) {
      for (const k of Object.keys(row)) {
        if (k !== 'label' && k !== 'pf_usd') lim = Math.max(lim, Math.abs(row[k]));
      }
    }
    lim = Math.ceil(lim * 1.15 * 100) / 100;
    return { data, names: [...wanted], lim, offSum };
  }, [chartStream, freq, strategies, prevByName, official, mirror]);

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
  // A3(2026-08-19):加性族 MRPT/MTFS 走**加法**,不能套百分比。official = ledger − C
  //   是恒等式,官方日 P&L 与账本日 P&L 逐美元相等 ⇒ live = ledger_live − C。
  //   旧式 off.value×(1+账本涨跌%) 把账本的百分比乘在小 2.65 倍的官方本金上,等于把
  //   当天的美元盈亏也缩小 2.65 倍 —— 8/19 实测 MTFS 主数字多报 $38,292($334,727 vs
  //   $296,435),且与展开项加总(见 waterfall)自相矛盾。EOD 重锚才盖住,日内一直错。
  //   C 取不到就返回 null(不静默退回错公式):卡片退到账本口径并把"官方锚定"标红。
  // A5(2026-08-20):乘性族同样**直接套恒等式** live = ledger × k,不再用
  //   off.value×(1+日内%)。旧式在两处出错:
  //   (1) 官方 EOD 每天只在 09:40 pipeline 跑完后前移一天,而 day_return 每天 ET 零点
  //       归零 —— 收盘到次日 09:40 这一段 (1+0) 把主数字钉死在**前天**的 EOD 上。
  //       8/20 00:49 实测:AISS 报 $2,909,113,真值 ledger 1,050,563.84 × 2.68155435
  //       = $2,817,144,单腿多报 $91,969,头部合计多报 $81,263。
  //   (2) 下面展开行的 scale 取 oa.live/s.value,旧式下 = 2.7691 ≠ k = 2.68155,
  //       而股数是按 k 换算的 ⇒ 股数 × 现价 ≠ 显示金额(用户早先提的自相矛盾)。
  //       改成恒等式后 scale 恒等于 k,两者天然一致。
  //   实测依据:day_return 与原始账本比在乘性族上逐日相等(8/18、8/19 差 0.0000pp,
  //   记账台阶只有 MTFS 有),所以 ledger×k 是恒等式而非近似。k 取不到返回 null。
  const officialAnchor = (name: string, value: number) => {
    const off = official?.[OFFICIAL_KEY[name]];
    if (!off) return null;
    if (ADDITIVE.has(name)) {
      const C = mirror?.capital_base?.[OFFICIAL_KEY[name]];
      return typeof C === 'number' ? { ...off, live: value - C } : null;
    }
    const k = mirror?.scalars?.[OFFICIAL_KEY[name]];
    return typeof k === 'number' && k > 0 ? { ...off, live: value * k } : null;
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
                {mirror?.rolloff && (
                  // 退场日之后:L/S 两队清空、QC 逐票收敛 ⇒ 两边持仓相同,净值差
                  // 定格成常数 K,记到 QC 一侧当现金。K 是**账户级**的一个数(QC 是
                  // 单一混合账户,没有分策略子账),所以放在组合层,不放策略卡里。
                  <div style={{ fontSize: 10, color: '#16a34a', fontWeight: 700 }}
                    title={t('realtimeNav.rolloffTitle', {
                      qc: usd(mirror.rolloff.qc_equity ?? 0),
                      panel: usd(mirror.rolloff.panel_official_total ?? 0) })}>
                    ✓ {t('realtimeNav.rolloffDone', {
                      k: usd(mirror.rolloff.k_equity),
                      date: mirror.rolloff.measured_on })}
                  </div>
                )}
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
          // 日内 % 与卡片主数字同口径(与顶部组合卡的算法一致):官方口径下加性族的 %
          // 天然是账本 % 的 ~2.65 倍(内生杠杆),已发布的 performance 曲线就是这个刻度。
          // 乘性族两者恒等,所以统一成一个公式没有副作用。
          const oaPct = officialAnchor(s.display_name, s.value);
          const pct = oaPct && oaPct.value ? (oaPct.live / oaPct.value - 1) * 100
            : dayPct(s.display_name, s.value);
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
                  const sh = (typeof kf === 'number' && kf > 0)
                    ? bankersRound(h.shares * kf) : h.shares;
                  // A4:加性族显示"账本股数 → QC 股数",QC 侧用与 exporter 同一套
                  // banker 舍入;L 队列恒 0 股(go-live 冻结,有机退场中)。
                  // 乘性族(全额缩放镜像 ×k)无队列:QC 股数 = 显示股数本身
                  // (面板已按 ×k+banker 舍入换算),标签把镜像关系显式画出来。
                  const qc = mul ? (mul.m === 0 ? 0 : bankersRound(h.shares * mul.m))
                    : (scalable && !missingK ? sh : null);
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
                      {!mul && qc !== null && (   /* 乘性族镜像徽章,同 pair 行 F 徽章样式 */
                        <span style={{ marginLeft: 5, fontSize: 9,
                          color: COHORT_COLOR.F,
                          border: `1px solid ${COHORT_COLOR.F}`, padding: '0 3px' }}
                          title={t('realtimeNav.qcScaledTitle', { k: kf })}>
                          {/* 3 位小数:SSRS k=0.995 用 2 位会显示成 ×1.00,与 BDC 真 ×1 混淆 */}
                          {t('realtimeNav.qcScaledShort',
                            { k: kf === 1 ? '1' : String(+(kf as number).toFixed(3)) })}
                        </span>
                      )}
                    </span>
                    <span>${(h.value * scale).toLocaleString(undefined, { maximumFractionDigits: 0 })}</span>
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
                          {mul && (                            /* A4:每对挂 QC 队列徽标 */
                            <span style={{ marginLeft: 5, fontSize: 9,
                              color: COHORT_COLOR[mul.cohort],
                              border: `1px solid ${COHORT_COLOR[mul.cohort]}`,
                              padding: '0 3px' }}
                              /* 徽标挤在对名后面,只放最短的辨识词;完整说法进 tooltip */
                              title={`${t(`realtimeNav.cohort${mul.cohort}`)} · m=${mul.m}`}>
                              {mul.cohort} {t(`realtimeNav.cohort${mul.cohort}Short`)}
                            </span>
                          )}
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
