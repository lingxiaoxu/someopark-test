// VolumePredictionViewer — 成交量预测(Volume Prediction)每日产出面板
// 数据链:launchd vp.shadowdaily(17:33 ET)→ VolumePrediction/outputs/ →
// server /api/vp/daily/:strategy(只读)→ 这里。
//
// 版式(2026-09-01 重设计):
//   ① 顶部 6 卡对五策略**取并集、口径一致**:数据新鲜至 / 实际服务模型 / 模型漂移 /
//      建议日期 / AUM(策略组合口径 = 实时净值面板同源)/ Capitulation 触发
//   ② 公司行为块:server 把 adapter 的文本告警解析成结构化条目(退市/改名 → 槽位
//      → 已处理方式);被解释掉的 "no VP data" 冗余行不再重复出现
//   ③ 每策略三段,段标题即段内容:
//        pairs(MRPT/MTFS): 活跃持仓清算力 → 入场候选 → 模型 AB
//        SSRS:            持仓 ETF 清算力与冲击成本 → 未来事件 → 模型 AB
//        AISS/AEUS:       持仓清算力 → Capitulation 监测 → 模型 AB
// 逐票 13,000 行预测留在磁盘上不进前端。
import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import { getVolumePrediction } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import PairBadge from '../PairBadge';

const STRATS = ['mrpt', 'mtfs', 'ssrs', 'aiss', 'aeus'];

// serving mix 模型短名:去 baselines. 前缀、去 _YYYYMMDD 冻结日;lgbm 族统称 lgbm
const shortModel = (m: string) => {
  const base = m.replace(/^baselines\./, '').replace(/_\d{8}$/, '');
  return base.startsWith('lgbm') ? 'lgbm' : base;
};
// 固定 rnn → ma5 → lgbm 展示序(其余排后),缺席的常规三员补 0 —— lgbm 近期
// 实际 0 行,显式画出来才说明"RNN 全覆盖 + lgbm 只是守卫"这件事
const MIX_ORDER = ['rnn', 'ma5', 'lgbm'];
function normalizeMix(mix: { model: string; n: number }[]) {
  const rows = mix.map(x => ({ ...x, short: shortModel(x.model) }));
  for (const want of MIX_ORDER) {
    if (!rows.some(r => r.short === want || r.short.startsWith(want))) {
      rows.push({ model: want, n: 0, short: want });
    }
  }
  const rank = (s: string) => {
    const i = MIX_ORDER.findIndex(w => s === w || s.startsWith(w));
    return i < 0 ? MIX_ORDER.length : i;
  };
  return rows.sort((a, b) => rank(a.short) - rank(b.short));
}

const fmtAdv = (v: number | null | undefined) =>
  v == null ? '—' : v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : `${Math.round(v / 1e3)}K`;
// DTL(days-to-liquidate)极小值显示下限,避免一排 0.00 假装没信息
const fmtDtl = (v: number | null | undefined) =>
  v == null ? '—' : v === 0 ? '0' : v < 0.01 ? '<0.01' : v.toFixed(2);
const fmtUsd = (v: number | null | undefined) =>
  v == null ? '—' : `$${Math.round(v).toLocaleString()}`;

function Card({ label, children, tone, title }: {
  label: string; children: React.ReactNode; tone?: 'ok' | 'warn'; title?: string;
}) {
  return (
    <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3" title={title}>
      <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{label}</div>
      <div className={`text-sm font-mono ${tone === 'warn' ? 'text-amber-600'
        : tone === 'ok' ? 'text-emerald-600' : 'text-[var(--text-primary)]'}`}>{children}</div>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div className="font-mono text-xs font-bold uppercase tracking-wider text-[var(--text-primary)] mb-2 px-1">
      {children}
    </div>
  );
}

type CorpAction = {
  type: 'delisted' | 'renamed'; ticker: string; name?: string; date?: string;
  current?: string; slots: { pair: string; active: boolean }[]; successor: string[];
};

export default function VolumePredictionViewer({ params }: { params?: any }) {
  const { t } = useTranslation();
  const [strategy, setStrategy] = useState(
    STRATS.includes(params?.strategy) ? params.strategy : 'mtfs');
  const { data, loading, error, refetch } = useApi(() => getVolumePrediction(strategy), [strategy]);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!data) return null;

  const { health, advice, ab } = data;
  const mix = normalizeMix(data.serving_mix || []);
  const inv: Record<string, any> = data.inv || {};
  const sig: Record<string, any> = data.sig || {};
  const aumOfficial: { date: string; value: number } | null = data.aum_official ?? null;
  const corpActions: CorpAction[] = data.corp_actions || [];
  const warnings: string[] = data.warnings || [];
  const capSummary: { triggered: string[]; total: number } | null = data.capitulation_summary ?? null;

  const pairsMode = strategy === 'mrpt' || strategy === 'mtfs';
  const stockMode = strategy === 'aiss' || strategy === 'aeus';
  const stale = !!health?.stale;

  // pairs:advice.positions 覆盖 inventory 全量条目(含已平),只留有 DTL 的活仓
  const livePositions = pairsMode
    ? (advice?.positions || []).filter((p: any) => (p.s1_dtl || 0) > 0 || (p.s2_dtl || 0) > 0)
    : [];
  // 候选按两腿最小 ADV 升序 —— 流动性最紧的入场候选才值得看,只取前 6
  const candidates = pairsMode
    ? [...(advice?.candidates || [])]
        .map((c: any) => ({ ...c, minAdv: Math.min(...Object.values(c.adv_forecast || {}) as number[]) }))
        .sort((a: any, b: any) => a.minAdv - b.minAdv)
    : [];
  const shownCandidates = candidates.slice(0, 6);

  // ssrs:未来 5 日只挑有事的日子;全空则一句话带过
  const events = (advice?.upcoming_events || []).filter((e: any) =>
    e.early_close || e.triple_witching || e.double_witching || e.russell_rebalance || e.n_earnings > 0);

  // aiss/aeus:capitulation 面板按 |eta_z| 降序,异常票排前
  const capitulation = [...(advice?.capitulation || [])]
    .sort((a: any, b: any) => Math.abs(b.eta_z || 0) - Math.abs(a.eta_z || 0));

  const rowCls = 'bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-3';

  return (
    <div className="flex flex-col h-full">
      {/* 头部:全称标题 + 策略选择器(与 InventoryViewer 同款) */}
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)]">
          {t('vp.title', { strategy: strategy.toUpperCase() })}
        </div>
        <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
          {STRATS.map(s => (
            <button key={s} onClick={() => setStrategy(s)}
              className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === s
                ? 'bg-[var(--accent-primary)] text-white'
                : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
              {s.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      {/* ① 顶部 6 卡:五策略同一套(并集),没有数据的格子画 — 而不是整格消失 */}
      <div className="grid grid-cols-3 gap-3 mb-4 shrink-0">
        <Card label={t('vp.freshThrough')} tone={stale ? 'warn' : 'ok'}>
          {health?.fresh_through ?? '—'}{stale
            ? ` ⚠ ${t('vp.staleDays', { n: health?.staleness_days ?? '?' })}` : ' ✓'}
        </Card>
        <Card label={t('vp.servingMix')}
          title={t('vp.servingMixTitle', {
            prod: health?.production ?? '—', rnn: health?.model_version ?? '—',
            n: health?.coverage_n?.toLocaleString() ?? '—' })}>
          {mix.length
            ? mix.map(m => `${m.short} ${m.n.toLocaleString()}`).join(' · ')
            : health?.production ?? '—'}
        </Card>
        <Card label={t('vp.modelDrift')} tone={health?.refreeze_due ? 'warn' : undefined}>
          {health?.model_drift_tradedays ?? '—'} {t('vp.tradedays')}
          {health?.refreeze_due ? ` · ${t('vp.refreezeDue')}` : ''}
        </Card>
        <Card label={t('vp.adviceDate')} tone={advice ? undefined : 'warn'}>
          {advice?.date ?? '—'}
          {!advice && <span className="text-[10px] ml-1">{t('vp.noAdvice')}</span>}
        </Card>
        <Card label={t('vp.aum')} title={t('vp.aumTitle')}>
          {fmtUsd(aumOfficial?.value)}
          {aumOfficial?.date && (
            <span className="text-[10px] text-[var(--text-muted)] ml-1.5">
              {t('vp.aumAsOf', { date: aumOfficial.date })}
            </span>
          )}
        </Card>
        <Card label={t('vp.capitulationCount')} title={t('vp.capitulationTitle')}
          tone={capSummary ? (capSummary.triggered.length > 0 ? 'warn' : 'ok') : undefined}>
          {capSummary
            ? <>
                {capSummary.triggered.length} / {capSummary.total}
                {capSummary.triggered.length > 0 && (
                  <span className="text-red-500 font-bold ml-1.5">{capSummary.triggered.join(' ')}</span>
                )}
              </>
            : <span className="text-[var(--text-muted)]" title={t('vp.capNA')}>—</span>}
        </Card>
      </div>

      {/* ② 公司行为:结构化、已处理 → 中性底色;解释不了的残余告警才用琥珀色 */}
      {corpActions.length > 0 && (
        <div className="mb-3 shrink-0 bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-2 text-xs font-mono">
          <div className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] mb-1">{t('vp.corpActions')}</div>
          {corpActions.map(ca => (
            <div key={`${ca.type}:${ca.ticker}`} className="py-0.5 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <span className={`px-1.5 py-0.5 rounded-sm border text-[10px] ${ca.type === 'delisted'
                ? 'border-red-400 text-red-500' : 'border-amber-400 text-amber-600'}`}>
                {ca.type === 'delisted'
                  ? t('vp.caDelisted', { date: ca.date ?? '?' })
                  : t('vp.caRenamed', { current: ca.current ?? '?' })}
              </span>
              <span className="font-bold text-[var(--text-primary)]">{ca.ticker}</span>
              {ca.name && <span className="text-[var(--text-muted)]">({ca.name})</span>}
              {ca.slots.length > 0 && (
                <span className="text-[var(--text-secondary)]">
                  {t('vp.caSlots')}: {ca.slots.map(s =>
                    `${s.pair}${s.active ? '' : `(${t('vp.caEmptySlot')})`}`).join(', ')}
                </span>
              )}
              <span className="text-[var(--text-muted)]">
                · {ca.type === 'delisted' ? t('vp.caHandledStale') : t('vp.caHandledForward')}
              </span>
              {ca.successor.length > 0 && (
                <span className="text-amber-600">· {t('vp.caSuccessor')}: {ca.successor.join(', ')}</span>
              )}
            </div>
          ))}
        </div>
      )}
      {warnings.length > 0 && (
        <div className="mb-4 shrink-0 border border-amber-500 text-amber-600 rounded-lg p-2 text-xs font-mono">
          <div className="text-[10px] uppercase tracking-wider mb-1">{t('vp.otherWarnings')}</div>
          {warnings.map((w, i) => <div key={i}>⚠ {w}</div>)}
        </div>
      )}

      {/* ③ 三段正文(按策略族) */}
      <div className="flex-1 overflow-y-auto space-y-4">
        {/* ══ pairs(MRPT/MTFS):活仓 DTL → 流动性最紧的入场候选 ══ */}
        {pairsMode && (
          <div>
            <SectionTitle>{t('vp.livePositions', { n: livePositions.length })}</SectionTitle>
            <div className="space-y-2">
              {livePositions.map((p: any) => {
                const d = inv[p.pair];   // server 从 inventory join 的持仓明细
                return (
                <div key={p.pair} className={rowCls}>
                  <div className="flex items-center justify-between mb-2">
                    {/* 统一配对容器:方向/两腿明细与 InventoryViewer pairs 模式同款 */}
                    <PairBadge pair={p.pair} strategy={strategy} compact
                      direction={d?.direction}
                      details={d ? {
                        openDate: d.open_date, daysHeld: d.days_held,
                        s1Shares: d.s1_shares, s2Shares: d.s2_shares,
                        s1Price: d.s1_price, s2Price: d.s2_price,
                        hedgeRatio: d.hedge_ratio, paramSet: d.param_set,
                      } : undefined} />
                    <span className="inline-flex items-center gap-1.5">
                      {/* advice 是 D-1 口径:当日盘中已平的对如实标出 */}
                      {d?.closed && (
                        <span className="font-mono text-[10px] px-1.5 py-0.5 border border-[var(--border-subtle)] text-[var(--text-muted)] rounded-sm">
                          {t('vp.closedToday')}
                        </span>
                      )}
                      <span className="font-mono text-[10px] px-1.5 py-0.5 border border-[var(--accent-primary)] text-[var(--accent-primary)] rounded-sm"
                        title={t('vp.bottleneckTitle')}>
                        {t('vp.bottleneck')} {p.bottleneck}
                      </span>
                    </span>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    {[[p.s1, p.s1_dtl], [p.s2, p.s2_dtl]].map(([tk, dtl]: any) => (
                      <div key={tk}>
                        <span className="text-[var(--text-muted)]">{tk}</span><br />
                        <span className="font-mono">
                          {t('vp.dtl')} {fmtDtl(dtl)} · ADV {fmtAdv(p.adv_forecast?.[tk])}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
                );
              })}
              {livePositions.length === 0 && (
                <div className="text-center text-[var(--text-muted)] text-xs py-4">{t('vp.noLive')}</div>
              )}
            </div>
            <div className="mt-4">
              <SectionTitle>{t('vp.candidates', { shown: shownCandidates.length, n: candidates.length })}</SectionTitle>
              <div className="flex flex-wrap gap-2">
                {shownCandidates.map((c: any) => {
                  const pk = `${c.s1}/${c.s2}`;
                  const sg = sig[pk];      // 当天 signals ∪ excluded join
                  const held = inv[pk];    // 候选同时是持仓对(含今日已平)→ 用持仓明细
                  // 方向优先级:持仓方向 → 信号方向 → action 字面 → 信号符号推
                  // **预期入场方向**(MRPT 均值回归 z>0 触发即 short;MTFS 动量
                  // mom>0 触发即 long,与 DailySignal 实际 action 规则一致)。
                  // excluded 对不标方向。
                  const dir = held?.direction
                    ?? (sg?.excluded ? undefined
                      : sg?.direction
                      ?? (sg?.action?.includes('LONG') ? 'long'
                        : sg?.action?.includes('SHORT') ? 'short'
                        : typeof sg?.z_score === 'number' ? (sg.z_score > 0 ? 'short' : 'long')
                        : typeof sg?.momentum_spread === 'number'
                          ? (sg.momentum_spread > 0 ? 'long' : 'short') : undefined));
                  const details = held
                    ? { openDate: held.open_date, daysHeld: held.days_held,
                        s1Shares: held.s1_shares, s2Shares: held.s2_shares,
                        s1Price: held.s1_price, s2Price: held.s2_price,
                        hedgeRatio: held.hedge_ratio, paramSet: held.param_set }
                    : sg && !sg.excluded
                      ? { zScore: sg.z_score, momentumSpread: sg.momentum_spread,
                          s1Price: sg.s1_price, s2Price: sg.s2_price } : undefined;
                  const advTitle = Object.entries(c.adv_forecast || {})
                    .map(([k, v]) => `${k} ADV ${fmtAdv(v as number)}`).join(' · ');
                  const inactive = sg?.excluded || (!sg && !held);   // 已排除/未选用同档淡显
                  return (
                  <span key={pk}
                    className={`inline-flex items-center gap-1${inactive ? ' opacity-60' : ''}`}
                    title={sg?.excluded ? `${advTitle} · ${sg.reason}` : advTitle}>
                    <PairBadge s1={c.s1} s2={c.s2} strategy={strategy} compact
                      direction={dir} details={details} />
                    <span className="font-mono text-[10px] text-[var(--text-muted)]">
                      {fmtAdv(c.minAdv)}
                      {sg?.excluded ? ` · ${t('vp.excluded')}`
                        /* MTFS 逐期重组对冲腿:universe 组合不在今日 WF 选用集 =
                           无信号也未被排除,如实标"未选用"而不是留白装适配 */
                        : (!sg && !held) ? ` · ${t('vp.notSelected')}` : ''}
                    </span>
                  </span>
                  );
                })}
                {candidates.length === 0 && (
                  <span className="text-[var(--text-muted)] text-xs">{t('vp.noCandidates')}</span>
                )}
              </div>
            </div>
          </div>
        )}

        {/* ══ SSRS:持仓 ETF 清算力与冲击成本 → 未来事件 ══ */}
        {strategy === 'ssrs' && (
          <div>
            <SectionTitle>{t('vp.etfHoldings', { n: (advice?.etfs || []).length })}</SectionTitle>
            {advice?.liquidity_trend_vs_ma5 != null && (
              <div className={`text-[10px] font-mono px-1 mb-2 ${advice.liquidity_trend_vs_ma5 < -0.2
                ? 'text-amber-600' : 'text-[var(--text-muted)]'}`}>
                {t('vp.liqTrend')}: {(advice.liquidity_trend_vs_ma5 * 100).toFixed(1)}%
              </div>
            )}
            <div className="space-y-2">
              {(advice?.etfs || []).map((e: any) => (
                <div key={e.etf} className={`${rowCls} flex items-center justify-between`}>
                  {/* SSRS 单票模式(与 SignalTable 同款:weight 上徽章,popup 出股数/权重/价格) */}
                  <PairBadge pair={e.etf} direction="long" strategy="ssrs" compact
                    details={{ weight: inv[e.etf]?.weight, shares: e.shares,
                      lastPrice: inv[e.etf]?.last_price, costBasis: inv[e.etf]?.cost_basis,
                      openDate: inv[e.etf]?.open_date, daysHeld: inv[e.etf]?.days_held }} />
                  <span className="font-mono text-xs text-[var(--text-secondary)]">
                    {e.shares?.toLocaleString()} {t('vp.shares')} · ADV {fmtAdv(e.adv_forecast)}
                  </span>
                  <span className="font-mono text-xs">
                    {t('vp.dtl')} {fmtDtl(e.dtl)}
                    <span className="text-[var(--text-muted)]"> · </span>
                    <span title={t('vp.impactTitle')}>${(e.impact_per_100k ?? 0).toFixed(2)}/100K</span>
                  </span>
                </div>
              ))}
              {(advice?.etfs || []).length === 0 && (
                <div className="text-center text-[var(--text-muted)] text-xs py-4">{t('vp.noLive')}</div>
              )}
            </div>
            <div className="mt-4">
              <SectionTitle>{t('vp.eventsWindow')}</SectionTitle>
              {events.length === 0
                ? <div className="text-[var(--text-muted)] text-xs px-1">{t('vp.noEvents')}</div>
                : events.map((e: any) => (
                  <div key={e.date} className="text-xs font-mono px-1 py-0.5">
                    {e.date} — {[
                      e.early_close && t('vp.earlyClose'),
                      e.triple_witching && t('vp.tripleWitching'),
                      e.double_witching && t('vp.doubleWitching'),
                      e.russell_rebalance && t('vp.russell'),
                      e.n_earnings > 0 && t('vp.nEarnings', { n: e.n_earnings }),
                    ].filter(Boolean).join(' · ')}
                  </div>
                ))}
            </div>
          </div>
        )}

        {/* ══ AISS/AEUS:持仓清算力 → capitulation 监测(η-z 异常票排前) ══ */}
        {stockMode && (
          <div>
            <SectionTitle>{t('vp.holdings', { n: (advice?.holdings || []).length })}</SectionTitle>
            <div className="space-y-2">
              {(advice?.holdings || []).map((h: any) => (
                <div key={h.ticker} className={`${rowCls} flex items-center justify-between`}>
                  {/* AISS/AEUS 个股模式(与 SignalTable 同款:weight/shares/price 全传) */}
                  <PairBadge pair={h.ticker} direction="long" strategy={strategy} compact
                    details={{ weight: inv[h.ticker]?.weight, shares: h.shares,
                      lastPrice: inv[h.ticker]?.last_price, costBasis: inv[h.ticker]?.cost_basis,
                      openDate: inv[h.ticker]?.open_date, daysHeld: inv[h.ticker]?.days_held }} />
                  <span className="font-mono text-xs text-[var(--text-secondary)]">
                    {h.shares?.toLocaleString()} {t('vp.shares')} · ADV {fmtAdv(h.adv_forecast)}
                  </span>
                  <span className="font-mono text-xs">{t('vp.dtl')} {fmtDtl(h.dtl)}</span>
                </div>
              ))}
              {(advice?.holdings || []).length === 0 && (
                <div className="text-center text-[var(--text-muted)] text-xs py-4">{t('vp.noLive')}</div>
              )}
            </div>
            <div className="mt-4">
              <SectionTitle>{t('vp.capitulation')}</SectionTitle>
              <div className="flex flex-wrap gap-2">
                {capitulation.map((c: any) => (
                  <span key={c.symbol} className="inline-flex items-center gap-1"
                    title={`η-z ${(c.eta_z ?? 0).toFixed(2)} · 5d ${((c.ret_5d ?? 0) * 100).toFixed(1)}%`}>
                    <PairBadge pair={c.symbol} strategy={strategy} compact
                      direction={inv[c.symbol] ? 'long' : null}
                      details={inv[c.symbol] ? { weight: inv[c.symbol].weight,
                        shares: inv[c.symbol].shares, lastPrice: inv[c.symbol].last_price } : undefined} />
                    <span className={`font-mono text-[10px] ${c.capitulation
                      ? 'text-red-500 font-bold' : 'text-[var(--text-muted)]'}`}>
                      {(c.eta_z ?? 0) >= 0 ? '+' : ''}{(c.eta_z ?? 0).toFixed(1)}σ
                    </span>
                  </span>
                ))}
                {capitulation.length === 0 && (
                  <span className="text-[var(--text-muted)] text-xs">{t('vp.capNA')}</span>
                )}
              </div>
            </div>
          </div>
        )}

        {/* ══ 模型 AB(全策略共用第三段):blend3 vs production,持仓票滞后 MAPE ══ */}
        {ab?.length > 0 && (
          <div>
            <SectionTitle>{t('vp.abTitle')}</SectionTitle>
            <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg overflow-hidden">
              <table className="w-full text-xs font-mono">
                <thead>
                  <tr className="text-[10px] text-[var(--text-muted)] uppercase">
                    <th className="text-left px-3 py-1.5">{t('vp.abDate')}</th>
                    <th className="text-right px-3 py-1.5">blend3</th>
                    <th className="text-right px-3 py-1.5">prod</th>
                    <th className="text-center px-3 py-1.5">{t('vp.abWinner')}</th>
                    <th className="text-right px-3 py-1.5">{t('vp.abHeld')}</th>
                  </tr>
                </thead>
                <tbody>
                  {ab.map((r: any) => {
                    const win = r.blend3_held_mape < r.prod_held_mape;
                    const tie = r.blend3_held_mape === r.prod_held_mape;
                    return (
                      <tr key={r.pred_date} className="border-t border-[var(--border-subtle)]">
                        {/* 滞后口径画进日期列:预测日 → 实际结算日 */}
                        <td className="px-3 py-1.5">{r.pred_date?.slice(5)} → {r.actual_date?.slice(5)}</td>
                        <td className={`text-right px-3 py-1.5 ${!tie && win ? 'text-emerald-600 font-bold' : ''}`}>
                          {r.blend3_held_mape?.toFixed(1)}%
                        </td>
                        <td className={`text-right px-3 py-1.5 ${!tie && !win ? 'text-emerald-600 font-bold' : ''}`}>
                          {r.prod_held_mape?.toFixed(1)}%
                        </td>
                        {/* 平局显式画 = —— RNN 全覆盖后两路由持仓票重合,绿色不触发是常态 */}
                        <td className={`text-center px-3 py-1.5 ${tie
                          ? 'text-[var(--text-muted)]' : 'text-emerald-600 font-bold'}`}
                          title={tie ? t('vp.abTieTitle') : undefined}>
                          {tie ? '=' : win ? 'blend3' : 'prod'}
                        </td>
                        <td className="text-right px-3 py-1.5 text-[var(--text-muted)]">{r.n_held}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <div className="text-[10px] text-[var(--text-muted)] px-3 py-1.5 border-t border-[var(--border-subtle)]">
                {t('vp.abNote')}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
