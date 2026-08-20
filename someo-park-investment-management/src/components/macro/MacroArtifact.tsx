/**
 * MacroArtifact — renders the right-panel content for every `macro_*` artifact.
 * One dispatcher + a set of real data viewers, each fetching the static JSON
 * exported by the Kalshi macro paper-trading system into public/data/ (macro_board,
 * macro_fed, macro_divergence, macro_decisions, macro_performance, macro_oos,
 * macro_coverage, macro_risk). Mirrors the PredictionArtifact dispatch pattern;
 * styling uses CSS vars / .table & .card classes, so it inverts with the theme.
 */
import type { ComponentType, CSSProperties, ReactNode } from 'react';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip } from 'recharts';
import { getMacroJson, analyzeMacro, predSummary, macroFileUrl, getMacroHealth, getMacroPricetrack } from './macroApi';
import type { MacroBoard, MacroDecision, MacroHealth, MacroLiveReplay, MacroPricetrack, MacroReplayLeg, MacroReportEntry } from './macroApi';
import {
  mono, fmt, pct, money, Loading, ErrorBox, KV, DataTable, Chip, TabButton, Generated,
  AiResult, useMacroPoll, type AiState,
} from './primitives';
import { MACRO_ITEMS, groupOfType } from './MacroArtifactGrid';
import { useSetArtifact } from '../../contexts/ArtifactContext';

// ── shared hooks / helpers ────────────────────────────────────────────────────
const useMacro = <T = any,>(name: string) => useMacroPoll<T>(() => getMacroJson<T>(name), 60000);

function useAi(view: string) {
  const { i18n } = useTranslation();
  const [state, setState] = useState<AiState>({ loading: false });
  const run = async () => {
    setState({ loading: true });
    try {
      const r = await analyzeMacro(view, i18n.language);
      setState({ loading: false, text: r.analysis, cached: r.cached });
    } catch (e: any) {
      setState({ loading: false, error: String(e?.message || e) });
    }
  };
  return { state, run };
}

function Ai({ view }: { view: string }) {
  const { t } = useTranslation();
  const { state, run } = useAi(view);
  return <AiResult state={state} onRun={run} label={t('macro.aiAnalyze')} />;
}

const trunc = (s?: string | null, n = 44) => {
  const v = String(s ?? '');
  return v.length > n ? v.slice(0, n - 1) + '…' : (v || '—');
};
const dt = (iso?: string | null) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? '—' : d.toISOString().slice(0, 16).replace('T', ' ');
};
const shortSeries = (s: string) => s.replace(/^KX/, '');

const KIND_COLOR: Record<string, string> = {
  pass: 'var(--text-muted)',
  open: 'var(--success)',
  argmax: 'var(--accent-primary)',    // WC-hybrid favourite leg
  arb: 'var(--accent-primary)',       // §24-A riskless arbitrage
  snipe: 'var(--warning)',            // §24-B post-print sniper
  exit: 'var(--text-secondary)',
  settle_note: 'var(--text-secondary)',
};
const kindColor = (k?: string) => KIND_COLOR[k ?? ''] ?? 'var(--success)';

function decisionChip(t: (k: string) => string, d?: MacroDecision | null): ReactNode {
  if (!d || !d.kind) return <span style={{ color: 'var(--text-muted)' }}>—</span>;
  const isPass = d.kind === 'pass';
  return <Chip color={kindColor(d.kind)}>{isPass ? t('macro.statusPass') : d.kind}</Chip>;
}

function countdownStr(t: (k: string, o?: any) => string, iso: string): string {
  const ms = new Date(iso).getTime() - Date.now();
  if (isNaN(ms)) return '—';
  if (ms <= 0) return t('macro.countdownPast');
  const mins = Math.floor(ms / 60000);
  const d = Math.floor(mins / 1440), h = Math.floor((mins % 1440) / 60), m = mins % 60;
  return t('macro.countdownIn', { v: d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m` });
}

function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: '.08em', textTransform: 'uppercase',
      color: 'var(--text-primary)', ...mono, margin: '12px 0 6px' }}>
      {children}
    </div>
  );
}

// ── System health strip (macro_health.json red/yellow/green lights) ──────────
const healthColor = (st?: string) =>
  st === 'green' ? 'var(--success)' : st === 'yellow' ? 'var(--warning)'
    : st === 'red' ? 'var(--error)' : 'var(--text-muted)';

function HealthStrip() {
  const { t } = useTranslation();
  const { data } = useMacroPoll<MacroHealth>(getMacroHealth, 60000);
  if (!data) return null;
  const series: NonNullable<MacroHealth['series']> = data.series ?? {};
  const flags: any[] = data.flags ?? [];
  const sources: Record<string, any> = data.sources ?? {};
  return (
    <div style={{ marginBottom: 8 }}>
      <div className="flex items-center flex-wrap" style={{ gap: 8 }}>
        <span style={{ fontSize: 10, fontWeight: 700, ...mono, textTransform: 'uppercase',
          letterSpacing: '.06em', color: 'var(--text-primary)' }}>
          {t('macro.systemHealth')}
        </span>
        {Object.entries(series).map(([name, s]) => (
          <span key={name} title={(s.notes ?? []).join('; ') || (s.status ?? '')}
            style={{ fontSize: 9.5, ...mono, color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>
            <span style={{ color: healthColor(s.status) }}>●</span> {shortSeries(name)}
          </span>
        ))}
        <Chip color={flags.length ? 'var(--error)' : 'var(--text-muted)'}>
          {t('macro.healthFlags', { n: flags.length })}
        </Chip>
      </div>
      {Object.keys(sources).length > 0 && (
        <div className="flex items-center flex-wrap" style={{ gap: 8, marginTop: 4 }}>
          <span style={{ fontSize: 10, fontWeight: 700, ...mono, textTransform: 'uppercase',
            letterSpacing: '.06em', color: 'var(--text-muted)' }}>
            {t('macro.dataFeeds')}
          </span>
          {Object.entries(sources).map(([sid, v]) => {
            const age = v?.age_days ?? v?.age_hours;
            const stale = age == null || (v?.age_days != null && v.age_days > 9)
              || (v?.age_hours != null && v.age_hours > 26);
            const label = sid.replace(/_WEEKLY$/, '').replace(/_/g, ' ').toLowerCase();
            return (
              <span key={sid}
                title={`${sid}: ${v?.latest_kt ?? ''}`}
                style={{ fontSize: 9.5, ...mono, whiteSpace: 'nowrap',
                  color: stale ? 'var(--warning)' : 'var(--text-secondary)' }}>
                <span style={{ color: stale ? 'var(--warning)' : 'var(--success)' }}>●</span>
                {' '}{label}{age != null ? ` ${age}${v?.age_days != null ? 'd' : 'h'}` : ''}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Board ─────────────────────────────────────────────────────────────────────
function BoardView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<MacroBoard>('macro_board');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const releases = [...(data?.next_releases ?? [])]
    .sort((a, b) => String(a.scheduled_ts).localeCompare(String(b.scheduled_ts)));
  const series: NonNullable<MacroBoard['series']> = data?.series ?? {};
  return (
    <div>
      <Generated ts={data?.generated_at} />
      <HealthStrip />
      <SectionTitle>{t('macro.nextReleases')}</SectionTitle>
      <DataTable
        cols={[t('macro.colSeries'), t('macro.colPeriod'), t('macro.colTime'), t('macro.colCountdown')]}
        rows={releases.map((r) => [
          <span style={mono}>{shortSeries(r.series)}</span>, r.period, dt(r.scheduled_ts),
          <span style={{ color: 'var(--success)', ...mono }}>{countdownStr(t, r.scheduled_ts)}</span>,
        ])} />
      <SectionTitle>{t('macro.seriesEntries')}</SectionTitle>
      {Object.entries(series).map(([name, s]) => (
        <div key={name} className="card" style={{ marginBottom: 10, padding: '8px 10px' }}>
          <div style={{ fontSize: 11, fontWeight: 700, ...mono, color: 'var(--text-primary)', marginBottom: 4 }}>
            {shortSeries(name)} <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>· {s.family} · {s.cadence}</span>
          </div>
          <DataTable
            cols={[t('macro.colPeriod'), t('macro.colModel'), t('macro.colDecision'), t('macro.colEdge'), t('macro.colSize'), t('macro.colNote')]}
            rows={(s.entries ?? []).map((e) => [
              e.period,
              <span style={mono}>{predSummary(e.pred?.dist)}</span>,
              decisionChip(t, e.decision),
              e.decision?.net_edge != null ? fmt(e.decision.net_edge, 3) : '—',
              e.decision?.size_usd ? money(e.decision.size_usd) : '—',
              <span style={{ color: 'var(--text-muted)' }}>{trunc(e.decision?.note, 32)}</span>,
            ])} />
        </div>
      ))}
      <Ai view="macro_board" />
    </div>
  );
}

// ── Fed ───────────────────────────────────────────────────────────────────────
const FED_BUCKETS = ['C26', 'C25', 'H0', 'H25', 'H26'];

function ProbBars({ probs, keys }: { probs: Record<string, number>; keys?: string[] }) {
  return (
    <div style={{ marginBottom: 8 }}>
      {(keys ?? FED_BUCKETS).map((k) => {
        const p = probs?.[k] ?? 0;
        return (
          <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3 }}>
            <span style={{ width: 34, fontSize: 10, ...mono, color: 'var(--text-secondary)' }}>{k}</span>
            <div style={{ flex: 1, height: 12, background: 'var(--bg-tertiary)', borderRadius: 2, overflow: 'hidden' }}>
              <div style={{ width: `${Math.min(100, p * 100)}%`, height: '100%', background: 'var(--accent-primary)' }} />
            </div>
            <span style={{ width: 52, textAlign: 'right', fontSize: 10, ...mono, color: 'var(--text-primary)' }}>{pct(p)}</span>
          </div>
        );
      })}
    </div>
  );
}

/** Empirical quantile array (201 pts of a discrete dist) → level probabilities. */
function quantileLevels(qs: number[] | undefined): Record<string, number> {
  const finite = (qs ?? []).filter((v) => Number.isFinite(v));
  if (!finite.length) return {};
  const counts: Record<string, number> = {};
  finite.forEach((v) => { const k = v.toFixed(2); counts[k] = (counts[k] ?? 0) + 1; });
  const out: Record<string, number> = {};
  Object.keys(counts).sort((a, b) => Number(a) - Number(b))
    .forEach((k) => { out[k] = counts[k] / finite.length; });
  return out;
}

function FedView({ params }: { params?: any }) {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_fed');
  const { data: board } = useMacro<any>('macro_board');
  const decRef = useRef<HTMLDivElement>(null);
  const ladRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const tgt = params?.section === 'ladder' ? ladRef.current
      : params?.section === 'decision' ? decRef.current : null;
    if (tgt) setTimeout(() => tgt.scrollIntoView({ behavior: 'smooth', block: 'start' }), 150);
  }, [params?.section, data, board]);
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const meetings: any[] = data?.meetings ?? [];
  const kxfed: any[] = board?.series?.KXFED?.entries ?? [];
  const focus = params?.section;
  const hl = (on: boolean) => (on ? { outline: '2px solid var(--accent-primary)', outlineOffset: 2, borderRadius: 4 } : {});
  return (
    <div>
      <Generated ts={data?.generated_at} />
      <div ref={decRef} style={hl(focus === 'decision')}>
      <SectionTitle>{t('macro.meetingsTitle')}</SectionTitle>
      {meetings.map((m) => (
        <div key={m.period} className="card" style={{ marginBottom: 10, padding: '8px 10px' }}>
          <div style={{ fontSize: 12, fontWeight: 700, ...mono, color: 'var(--text-primary)', marginBottom: 6 }}>
            {m.period} <span style={{ color: 'var(--text-muted)', fontWeight: 400, fontSize: 10 }}>· {dt(m.asof)}</span>
          </div>
          <ProbBars probs={m.probs} />
          <SectionTitle>{t('macro.inputs')}</SectionTitle>
          <KV rows={[
            ['core_yoy', <span style={mono}>{fmt(m.inputs?.core_yoy, 2)}</span>],
            ['du12', <span style={mono}>{fmt(m.inputs?.du12, 2)}</span>],
            [t('macro.mode'), <span style={mono}>{m.inputs?.mode ?? '—'}</span>],
          ]} />
          <DataTable
            cols={['', ...FED_BUCKETS]}
            rows={[
              [t('macro.ruleRow'), ...FED_BUCKETS.map((k) => pct(m.inputs?.rule?.[k]))],
              [t('macro.marketRow'), ...FED_BUCKETS.map((k) => pct(m.inputs?.market?.[k]))],
              [t('macro.modelRow'), ...FED_BUCKETS.map((k) => pct(m.probs?.[k]))],
            ]} />
        </div>
      ))}
      </div>
      {kxfed.length > 0 && (
        <div ref={ladRef} style={hl(focus === 'ladder')}>
          <SectionTitle>{t('macro.rateLadderTitle')}</SectionTitle>
          {kxfed.map((e) => {
            const levels = e.pred?.dist?.kind === 'empirical'
              ? quantileLevels(e.pred.dist.quantiles) : {};
            const ks = Object.keys(levels);
            if (!ks.length) return null;
            return (
              <div key={e.period} className="card" style={{ marginBottom: 10, padding: '8px 10px' }}>
                <div style={{ fontSize: 12, fontWeight: 700, ...mono, color: 'var(--text-primary)', marginBottom: 6 }}>
                  {e.period} <span style={{ color: 'var(--text-muted)', fontWeight: 400, fontSize: 10 }}>· KXFED · {dt(e.pred?.asof)}</span>
                </div>
                <ProbBars probs={levels} keys={ks} />
              </div>
            );
          })}
        </div>
      )}
      <Ai view="macro_fed" />
    </div>
  );
}

// ── family views (inflation / labor / energy) ────────────────────────────────
function FamilyCards({ seriesNames }: { seriesNames: string[] }) {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<MacroBoard>('macro_board');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const series = data?.series ?? {};
  const hits = seriesNames.filter((n) => series[n]);
  if (!hits.length) {
    return (
      <div>
        <Generated ts={data?.generated_at} />
        <div className="text-xs py-2" style={{ color: 'var(--text-muted)', ...mono }}>{t('macro.noUpcoming')}</div>
      </div>
    );
  }
  return (
    <div>
    <Generated ts={data?.generated_at} />
    <div className="flex flex-wrap gap-2">
      {hits.flatMap((name) =>
        (series[name].entries ?? []).map((e) => (
          <div key={name + e.period} className="pair-card"
            style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 190, flex: '1 1 210px' }}>
            <div className="flex items-center justify-between">
              <span style={{ fontSize: 11, fontWeight: 700, ...mono, color: 'var(--text-primary)' }}>
                {shortSeries(name)} · {e.period}
              </span>
              {decisionChip(t, e.decision)}
            </div>
            <div style={{ fontSize: 10.5, ...mono, color: 'var(--text-secondary)' }}>
              {t('macro.colModel')}: {predSummary(e.pred?.dist)}
            </div>
            {e.decision?.net_edge != null && (
              <div style={{ fontSize: 10, ...mono, color: 'var(--text-muted)' }}>
                {t('macro.colEdge')}: {fmt(e.decision.net_edge, 3)}
              </div>
            )}
            {e.decision?.note && (
              <div style={{ fontSize: 9.5, ...mono, color: 'var(--text-muted)' }}>{trunc(e.decision.note, 40)}</div>
            )}
          </div>
        )))}
    </div>
    </div>
  );
}

const INFLATION_SERIES = ['KXCPI', 'KXCPICORE', 'KXCPIYOY', 'KXCPICOREYOY', 'KXPCECORE'];
const LABOR_SERIES = ['KXJOBLESSCLAIMS', 'KXPAYROLLS', 'KXU3'];

function InflationView() {
  return (<div><FamilyCards seriesNames={INFLATION_SERIES} /><Ai view="macro_inflation" /></div>);
}

function LaborView() {
  return (<div><FamilyCards seriesNames={LABOR_SERIES} /><Ai view="macro_labor" /></div>);
}

function EnergyView() {
  const { data } = useMacro<MacroBoard>('macro_board');
  // Energy models (KXWTIW / KXNATGASW / KXAAAGASW) are live — render whatever
  // energy-family series the board carries.
  const allSeries: NonNullable<MacroBoard['series']> = data?.series ?? {};
  const energyNames = Object.entries(allSeries)
    .filter(([, s]) => s.family === 'energy').map(([n]) => n);
  return (
    <div>
      <FamilyCards seriesNames={energyNames} />
      <Ai view="macro_energy" />
    </div>
  );
}

// ── Divergence ────────────────────────────────────────────────────────────────
function DivergenceView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_divergence');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const rows: any[] = data?.rows ?? [];
  return (
    <div>
      <Generated ts={data?.generated_at} />
      <DataTable
        cols={[t('macro.colSeries'), t('macro.colPeriod'), t('macro.colGap'), t('macro.colLegs')]}
        rows={rows.map((r) => [
          <span style={mono}>{shortSeries(r.series)}</span>, r.period,
          <span style={{ color: r.gap_norm > 0.15 ? 'var(--success)' : 'var(--text-muted)', fontWeight: r.gap_norm > 0.15 ? 700 : 400, ...mono }}>
            {fmt(r.gap_norm, 3)}
          </span>,
          r.n_legs,
        ])} />
      <Ai view="macro_divergence" />
    </div>
  );
}

// ── Decisions ─────────────────────────────────────────────────────────────────
const DECISIONS_CAP = 60;

function DecisionsView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_decisions');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const total = (data?.decisions ?? []).length;
  const decisions: any[] = (data?.decisions ?? []).slice(0, DECISIONS_CAP);
  const marks: any[] = data?.latest_marks ?? [];
  return (
    <div>
      <Generated ts={data?.generated_at} />
      <SectionTitle>{t('macro.decisions')}</SectionTitle>
      <div style={{ overflowX: 'auto' }}>
        <DataTable
          cols={[t('macro.colTime'), t('macro.colSeries'), t('macro.colPeriod'), t('macro.colKind'),
            t('macro.colFair'), t('macro.colAsk'), t('macro.colEdge'), t('macro.colSize'), t('macro.colNote')]}
          rows={decisions.map((d) => [
            <span style={{ ...mono, fontSize: 10 }}>{dt(d.ts_utc)}</span>,
            <span style={mono}>{shortSeries(d.series)}</span>, d.period,
            <Chip color={kindColor(d.kind)}>{d.kind}</Chip>,
            d.fair != null ? fmt(d.fair, 3) : '—', d.ask != null ? fmt(d.ask, 3) : '—',
            d.net_edge != null ? fmt(d.net_edge, 3) : '—',
            d.size_usd ? money(d.size_usd) : '—',
            <span style={{ color: 'var(--text-muted)' }}>{trunc(d.note, 30)}</span>,
          ])} />
      </div>
      {total > DECISIONS_CAP && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginTop: 4 }}>
          {t('macro.truncatedNote', { shown: DECISIONS_CAP, total })}
        </div>
      )}
      <SectionTitle>{t('macro.latestMarks')}</SectionTitle>
      {marks.length ? (
        <DataTable
          cols={['#', t('macro.colTicker'), t('macro.colMid'), t('macro.colPnl')]}
          rows={marks.map((m) => [
            m.decision_id,
            <span style={{ ...mono, fontSize: 10 }}>{m.ticker}</span>,
            fmt(m.mid, 3),
            <span style={{ color: (m.pnl_usd ?? 0) >= 0 ? 'var(--success)' : 'var(--error)', ...mono }}>{money(m.pnl_usd)}</span>,
          ])} />
      ) : (
        <div className="text-xs py-2" style={{ color: 'var(--text-muted)', ...mono }}>—</div>
      )}
      <Ai view="macro_decisions" />
    </div>
  );
}

// ── Performance ───────────────────────────────────────────────────────────────
function PerformanceView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_performance');
  const { data: pt } = useMacroPoll<MacroPricetrack>(getMacroPricetrack, 60000);
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const tr = data?.track;
  const hist = tr?.history;
  const liveS = tr?.live?.settled;
  const liveO = tr?.live?.open;
  const comb = tr?.combined;
  const track = pt?.track ?? [];
  const settledLog: any[] = [...(hist?.trades ?? []), ...(liveS?.trades ?? [])]
    .sort((a, b) => String(a.day).localeCompare(String(b.day)));
  let cum = 0;
  const withCum = settledLog.map((x) => { cum += (x.realized ?? 0); return { ...x, cum }; });
  return (
    <div>
      <Generated ts={data?.generated_at} />
      <KV rows={[
        [t('macro.bankroll'), <span style={mono}>{money(data?.bankroll_usd)}</span>],
        [t('macro.unrealized'), <span style={{ ...mono, color: (data?.unrealized_usd ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(data?.unrealized_usd)}</span>],
        [t('macro.mode'), <Chip color="var(--accent-primary)">{data?.mode ?? '—'}</Chip>],
      ]} />
      {comb && (
        <>
          <SectionTitle>{t('macro.trackRecord')}</SectionTitle>
          <div style={{ fontSize: 12, ...mono, marginBottom: 6 }}>
            <b>{comb.won}W-{comb.n_trades - comb.won}L</b>
            {' · '}
            <span style={{ color: (comb.realized ?? 0) >= 0 ? 'var(--success)' : 'var(--error)', fontWeight: 700 }}>
              {money(comb.realized)} ({pct(comb.roi)})
            </span>
            {' · '}
            <span style={{ color: 'var(--text-muted)' }}>{comb.n_trades} {t('macro.wfBets')}</span>
          </div>
          <DataTable
            cols={[t('macro.colSpan'), t('macro.colTrades'), 'W-L', t('macro.winRate'),
              t('macro.colPnl'), 'ROI']}
            rows={[
              hist && [
                // the two halves of this table are not the same kind of evidence — the
                // first is a simulation of the rule, the second is the ledger it filled.
                // Without the chip a reader takes the combined row as one track record.
                <span><Chip color="var(--text-muted)">{t('macro.trackBacktest')}</Chip>{' '}
                  <span style={mono}>{hist.span?.[0]?.slice(5)}–{hist.span?.[1]?.slice(5)}</span></span>,
                hist.n_trades, `${hist.won}W-${hist.n_trades - hist.won}L`, pct(hist.win_rate),
                <span style={{ ...mono, color: (hist.realized ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(hist.realized)}</span>,
                <span style={{ ...mono, color: (hist.roi ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{pct(hist.roi)}</span>,
              ],
              liveS && [
                <span><Chip color="var(--accent-primary)">{t('macro.trackLive')}</Chip>{' '}
                  <span style={mono}>{tr.cutover?.slice(5)}–</span></span>,
                liveS.n_trades,
                `${liveS.won}W-${liveS.n_trades - liveS.won}L`,
                liveS.n_trades ? pct(liveS.won / liveS.n_trades) : '—',
                <span style={{ ...mono, color: (liveS.realized ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(liveS.realized)}</span>,
                liveS.roi != null ? pct(liveS.roi) : '—',
              ],
              liveO && [
                <span style={{ ...mono, color: 'var(--text-muted)' }}>{t('macro.trackOpen')}</span>,
                liveO.n, '—', '—',
                <span style={{ ...mono, color: (liveO.unrealized ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(liveO.unrealized)}</span>,
                '—',
              ],
            ].filter(Boolean) as any[]} />
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>
            {t('macro.trackNote', { cutover: tr.cutover })}
          </div>
          {withCum.length > 0 && (
            <>
              <SectionTitle>{t('macro.wfBetLog')}</SectionTitle>
              <div style={{ overflowX: 'auto' }}>
                <DataTable
                  cols={[t('macro.wfEntryDay'), t('macro.colSeries'), t('macro.wfBetCol'),
                    t('macro.colStaked'), t('macro.colVerdict'), t('macro.colPnl'), 'Cum$']}
                  rows={withCum.slice().reverse().map((x: any) => [
                    <span style={{ ...mono, fontSize: 10 }}>{String(x.day ?? '').slice(5)}</span>,
                    <span style={mono}>{shortSeries(x.series)}</span>,
                    <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>{trunc(x.desc, 28)}</span>,
                    money(x.staked),
                    <span style={{ color: x.won ? 'var(--success)' : 'var(--error)', fontWeight: 700 }}>
                      {x.won ? t('macro.betWon') : t('macro.betLost')}
                    </span>,
                    <span style={{ ...mono, color: (x.realized ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(x.realized)}</span>,
                    <span style={{ ...mono, color: x.cum >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(x.cum)}</span>,
                  ])} />
              </div>
            </>
          )}
        </>
      )}
      {track.length > 0 && (
        <>
          <SectionTitle>{t('macro.priceTrack')}</SectionTitle>
          <div style={{ width: '100%', height: 160 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={track} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <XAxis dataKey="ts" hide />
                <YAxis width={46} tick={{ fontSize: 9, fontFamily: 'var(--font-mono)' }} stroke="var(--text-muted)" />
                <Tooltip
                  formatter={(v: any) => [money(Number(v)), 'PnL']}
                  labelFormatter={(l: any) => dt(String(l))}
                  contentStyle={{ background: 'var(--bg-secondary)', border: '1px solid var(--border-subtle)',
                    fontSize: 10, fontFamily: 'var(--font-mono)' }} />
                <Line type="monotone" dataKey="pnl_usd" stroke="var(--accent-primary)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
      <Ai view="macro_performance" />
    </div>
  );
}

// ── Calibration (OOS) — §21.4 split: adoption gates ONLY. Decision replay and
// Brier scoring live in their own artifacts (macro_replay / macro_scoring);
// the 30d walkforward block was a duplicate of the Walk-Forward Lab and is gone.
function CalibrationView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_oos');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const gates: any[] = data?.component_gates ?? [];
  return (
    <div>
      <Generated ts={data?.generated_at} />
      <div style={{ fontSize: 10.5, color: 'var(--text-muted)', ...mono, margin: '2px 0 8px', lineHeight: 1.6 }}>
        {t('macro.calibPipeline')}
      </div>
      {gates.length > 0 && (
        <>
          <SectionTitle>{t('macro.componentGates')}</SectionTitle>
          {gates.map((g, i) => {
            let m: any = {};
            try { m = JSON.parse(g.metrics_json || '{}'); } catch { /* keep empty */ }
            const real = m.real === true || m.pass === true;
            return (
              <div key={i} className="card" style={{ marginBottom: 8, padding: '8px 10px' }}>
                <div className="flex items-center justify-between" style={{ marginBottom: 4 }}>
                  <span style={{ fontSize: 11, fontWeight: 700, ...mono, color: 'var(--text-primary)' }}>
                    {g.name} <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>· {shortSeries(g.series || '')} · {g.window}</span>
                  </span>
                  <Chip color={real ? 'var(--success)' : 'var(--error)'}>
                    {real ? t('macro.gateReal') : t('macro.gatePaper')}
                  </Chip>
                </div>
                {g.name === 'series_gate' && Array.isArray(m.reasons) && m.reasons.length > 0 && (
                  <ul style={{ paddingLeft: 14, fontSize: 10, color: 'var(--text-muted)', ...mono, marginBottom: 0 }}>
                    {m.reasons.map((r: string, j: number) => <li key={j}>{r}</li>)}
                  </ul>
                )}
              </div>
            );
          })}
        </>
      )}
      {data?.gate_note && (
        <div style={{ fontSize: 10.5, color: 'var(--text-muted)', ...mono, margin: '0 0 8px' }}>
          {t('macro.gateNote')}: {data.gate_note}
        </div>
      )}
      <Ai view="macro_calibration" />
    </div>
  );
}

// ── Decision replay (§21.4 split from calibration) ───────────────────────────
function ReplayView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_oos');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const decReplays: any[] = data?.decision_replays ?? [];
  return (
    <div>
      <Generated ts={data?.generated_at} />
      <SectionTitle>{t('macro.decisionReplay')}</SectionTitle>
      {decReplays.length ? (
        <DataTable
          cols={[t('macro.colSeries'), t('macro.colTrades'), t('macro.colStaked'),
            t('macro.colPnl'), 'ROI', 'edge_capture']}
          rows={decReplays.map((r) => [
            <span style={mono}>{shortSeries(r.series || '')}</span>,
            r.n_trades ?? '—',
            r.staked != null ? money(r.staked) : '—',
            r.realized != null
              ? <span style={{ ...mono, color: r.realized >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(r.realized)}</span>
              : '—',
            r.roi != null
              ? <span style={{ ...mono, color: r.roi >= 0 ? 'var(--success)' : 'var(--error)' }}>{pct(r.roi)}</span>
              : '—',
            r.edge_capture != null ? fmt(r.edge_capture, 2) : '—',
          ])} />
      ) : <div className="text-xs py-1" style={{ color: 'var(--text-muted)', ...mono }}>—</div>}
      <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, margin: '4px 0 10px' }}>
        {t('macro.decisionReplayNote')}
      </div>
      {/* AI context comes from the same macro_oos payload; the server whitelists
          only the parent view names, so the split keeps the parent's. */}
      <Ai view="macro_calibration" />
    </div>
  );
}

// ── Brier scoring replays (§21.4 split from calibration) ─────────────────────
function ScoringView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_oos');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const experiments: any[] = (data?.experiments ?? [])
    .filter((ex: any) => ex.name !== 'decision_replay');
  return (
    <div>
      <Generated ts={data?.generated_at} />
      {experiments.length > 0 && <SectionTitle>{t('macro.scoringReplays')}</SectionTitle>}
      {experiments.map((ex, i) => {
        let metrics: Record<string, number> = {};
        try { metrics = JSON.parse(ex.metrics_json || '{}'); } catch { /* keep empty */ }
        // horizons: keys look like "brier_model-24h" / "brier_market-1h"
        const horizons = [...new Set(Object.keys(metrics)
          .filter((k) => k.startsWith('brier_model-'))
          .map((k) => k.slice('brier_model-'.length)))];
        const extras: string[] = [];
        if (metrics['crps-24h'] != null) extras.push(`CRPS 24h ${fmt(metrics['crps-24h'], 1)}`);
        if (metrics['crps-1h'] != null) extras.push(`CRPS 1h ${fmt(metrics['crps-1h'], 1)}`);
        if (metrics.skipped != null && metrics.skipped > 0) extras.push(`${t('macro.skipped')}: ${metrics.skipped}`);
        return (
          <div key={i} className="card" style={{ marginBottom: 10, padding: '8px 10px' }}>
            <div style={{ fontSize: 11, fontWeight: 700, ...mono, color: 'var(--text-primary)', marginBottom: 4 }}>
              {ex.name} <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>· {shortSeries(ex.series || '')} · {ex.window} · n={metrics.n ?? '—'}</span>
            </div>
            <DataTable
              cols={[t('macro.colHorizon'), 'Brier (model)', 'Brier (market)', t('macro.colVerdict')]}
              rows={horizons.map((h) => {
                const bm = metrics[`brier_model-${h}`];
                const bk = metrics[`brier_market-${h}`];
                const ns = metrics[`n_scored-${h}`];
                const behind = bm != null && bk != null && bm > bk;
                return [ns != null ? `${h} (n=${ns})` : h, fmt(bm, 4), fmt(bk, 4),
                  <Chip color={behind ? 'var(--error)' : 'var(--success)'}>
                    {behind ? t('macro.behindMarket') : t('macro.beatsMarket')}
                  </Chip>];
              })} />
            {extras.length > 0 && (
              <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginTop: 4 }}>{extras.join(' · ')}</div>
            )}
          </div>
        );
      })}
      <Ai view="macro_calibration" />
    </div>
  );
}

// ── Bets: the direct "what are we betting" view ──────────────────────────────
function BetsView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_bets');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const open: any[] = data?.open_bets ?? [];
  const stances: any[] = data?.stances ?? [];
  const upcoming: any[] = data?.upcoming ?? [];
  const unreal = open.reduce((s, b) => s + (b.unrealized ?? 0), 0);
  return (
    <div>
      <Generated ts={data?.generated_at} />
      <SectionTitle>{t('macro.betsToday')}</SectionTitle>
      {stances.length ? (
        <div style={{ overflowX: 'auto' }}>
          <DataTable
            cols={[t('macro.colSeries'), t('macro.colPeriod'), t('macro.colDtc'),
              t('macro.colAction'), t('macro.wfBetCol'), 'fair', 'cost',
              t('macro.colReason')]}
            rows={stances.map((s) => {
              const d = s.decision;
              const isBet = d && d.kind !== 'pass';
              return [
                <span style={mono}>{shortSeries(s.series)}</span>,
                <span style={{ ...mono, fontSize: 10 }}>{s.period}</span>,
                <span style={mono}>{s.days_to_close}d</span>,
                d ? <Chip color={kindColor(d.kind)}>{d.kind === 'pass' ? t('macro.statusPass') : d.kind}</Chip>
                  : <span style={{ color: 'var(--text-muted)' }}>—</span>,
                <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>{trunc(d?.desc, 26)}</span>,
                d?.fair != null ? fmt(d.fair, 2) : '—',
                d?.cost != null ? fmt(d.cost, 2) : '—',
                <span style={{ color: isBet ? 'var(--success)' : 'var(--text-muted)', fontSize: 10 }}>
                  {isBet ? money(d.size_usd) : trunc(d?.note, 30)}
                </span>,
              ];
            })} />
        </div>
      ) : <div style={{ fontSize: 11, color: 'var(--text-muted)', ...mono }}>—</div>}
      <SectionTitle>
        {t('macro.betsOpen')} · {open.length} ·{' '}
        <span style={{ color: unreal >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(unreal)}</span>
      </SectionTitle>
      <div style={{ overflowX: 'auto' }}>
        <DataTable
          cols={[t('macro.wfEntryDay'), t('macro.colSeries'), t('macro.colPeriod'),
            t('macro.colAction'), t('macro.wfBetCol'), t('macro.colStaked'),
            t('macro.colUnrealized')]}
          rows={open.map((b) => [
            <span style={{ ...mono, fontSize: 10 }}>{String(b.ts ?? '').slice(5, 10)}</span>,
            <span style={mono}>{shortSeries(b.series)}</span>,
            <span style={{ ...mono, fontSize: 10 }}>{b.period}</span>,
            <Chip color={kindColor(b.kind)}>{b.kind}</Chip>,
            <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>{trunc(b.desc, 28)}</span>,
            money(b.size_usd),
            b.unrealized != null
              ? <span style={{ ...mono, color: b.unrealized >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(b.unrealized)}</span>
              : '—',
          ])} />
      </div>
      <SectionTitle>{t('macro.betsUpcoming')}</SectionTitle>
      <DataTable
        cols={[t('macro.colSeries'), t('macro.colPeriod'), t('macro.colTime'), t('macro.colCountdown')]}
        rows={upcoming.map((u) => [
          <span style={mono}>{shortSeries(u.series)}</span>, u.period, dt(u.scheduled_ts),
          <span style={{ color: 'var(--success)', ...mono }}>{u.days}d</span>,
        ])} />
      <div style={{ fontSize: 10.5, color: 'var(--text-muted)', ...mono, marginTop: 6 }}>
        {t('macro.betsNote')}
      </div>
      <Ai view="macro_bets" />
    </div>
  );
}

// ── Coverage ──────────────────────────────────────────────────────────────────
const STATE_COLOR: Record<string, string> = {
  predicted: 'var(--success)',
  passed: 'var(--success)',
  scheduled: 'var(--text-muted)',
  decided: 'var(--accent-primary)',
  open: 'var(--accent-primary)',
  reconciled: 'var(--accent-primary)',
  missed: 'var(--error)',
};

/** Consistency/model-quality alert sources; everything else counts as operational. */
const MODEL_ALERT_SOURCES = new Set([
  'consistency', 'health', 'dfm_gate', 'drift', 'attribution',
  'capture_gate', 'statement_risk', 'circuit_breaker',
]);

function AlertList({ items }: { items: any[] }) {
  if (!items.length) return <div className="text-xs py-1" style={{ color: 'var(--text-muted)', ...mono }}>—</div>;
  return (
    <ul style={{ paddingLeft: 16, fontSize: 11, ...mono }}>
      {items.map((a, i) => (
        <li key={i} style={{ marginBottom: 3,
          color: a.level === 'error' ? 'var(--error)'
            : a.level === 'warn' ? 'var(--warning)' : 'var(--text-secondary)' }}>
          <Chip color={a.level === 'error' ? 'var(--error)'
            : a.level === 'warn' ? 'var(--warning)' : 'var(--text-muted)'}>{a.source ?? '—'}</Chip>
          {' '}[{a.level}] {a.message} <span style={{ color: 'var(--text-muted)' }}>· {dt(a.ts)}</span>
        </li>
      ))}
    </ul>
  );
}

// §21.4 split: the coverage matrix and the alert feeds used to share one artifact;
// scheduling coverage stays here, every alert list moved to macro_alerts.
function CoverageView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_coverage');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const cov: any[] = data?.coverage ?? [];
  const missed: any[] = data?.missed ?? [];
  const seriesList = [...new Set(cov.map((r) => r.series))];
  const periods = [...new Set(cov.map((r) => r.period))].sort();
  const stateOf = (s: string, p: string) => cov.find((r) => r.series === s && r.period === p)?.state;
  return (
    <div>
      <Generated ts={data?.generated_at} />
      <SectionTitle>{t('macro.coverage')}</SectionTitle>
      <div style={{ overflowX: 'auto' }}>
        <table className="table">
          <thead><tr>
            <th style={{ textAlign: 'left' }}>{t('macro.colSeries')}</th>
            {periods.map((p) => <th key={p} style={{ textAlign: 'center', fontSize: 9 }}>{p}</th>)}
          </tr></thead>
          <tbody>
            {seriesList.map((s) => (
              <tr key={s}>
                <td style={{ ...mono, whiteSpace: 'nowrap' }}>{shortSeries(s)}</td>
                {periods.map((p) => {
                  const st = stateOf(s, p);
                  return (
                    <td key={p} style={{ textAlign: 'center' }}>
                      {st ? <Chip color={STATE_COLOR[st] ?? 'var(--text-muted)'}>{st.slice(0, 4)}</Chip>
                          : <span style={{ color: 'var(--text-muted)' }}>·</span>}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <SectionTitle>{t('macro.missed')}</SectionTitle>
      {missed.length ? (
        <ul style={{ paddingLeft: 16, fontSize: 11, color: 'var(--error)', ...mono }}>
          {missed.map((m, i) => <li key={i}>{typeof m === 'string' ? m : `${m.series ?? ''} ${m.period ?? ''} ${m.note ?? ''}`}</li>)}
        </ul>
      ) : (
        <div className="text-xs py-1" style={{ color: 'var(--text-muted)', ...mono }}>—</div>
      )}
      <Ai view="macro_coverage" />
    </div>
  );
}

// ── Alert center (§21.4 split from coverage) ─────────────────────────────────
function AlertsView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_coverage');
  const [showInfo, setShowInfo] = useState(false);
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const alerts: any[] = data?.alerts ?? [];
  const modelAll = alerts.filter((a) => MODEL_ALERT_SOURCES.has(a.source));
  // error/warn always visible; info-level consistency sweeps collapse behind a
  // toggle — they are routine instrumentation, not findings
  const modelAlerts = modelAll.filter((a) => a.level !== 'info');
  const modelInfo = modelAll.filter((a) => a.level === 'info');
  const opsAlerts = alerts.filter((a) => !MODEL_ALERT_SOURCES.has(a.source));
  return (
    <div>
      <Generated ts={data?.generated_at} />
      <SectionTitle>{t('macro.consistencyAlerts')}</SectionTitle>
      <AlertList items={modelAlerts} />
      {modelInfo.length > 0 && (
        <>
          <button onClick={() => setShowInfo(!showInfo)}
            style={{ fontSize: 10.5, ...mono, color: 'var(--text-muted)', background: 'transparent',
              border: '1px solid var(--border-subtle)', borderRadius: 4, padding: '2px 8px',
              cursor: 'pointer', margin: '2px 0 6px' }}>
            {showInfo ? '▾' : '▸'} {t('macro.infoSweeps', { n: modelInfo.length })}
          </button>
          {showInfo && <AlertList items={modelInfo} />}
        </>
      )}
      <SectionTitle>{t('macro.opsAlerts')}</SectionTitle>
      <AlertList items={opsAlerts} />
      <Ai view="macro_coverage" />
    </div>
  );
}

// ── Risk ──────────────────────────────────────────────────────────────────────
function RiskView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_risk');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const limits = data?.limits ?? {};
  const scenario = data?.scenario ?? {};
  const exposure: any[] = data?.open_exposure ?? [];
  return (
    <div>
      <Generated ts={data?.generated_at} />
      <SectionTitle>{t('macro.openExposure')}</SectionTitle>
      <DataTable
        cols={[t('macro.colSeries'), t('macro.colPeriod'), t('macro.colSize')]}
        rows={exposure.map((r) => [<span style={mono}>{shortSeries(r.series)}</span>, r.period, money(r.size_usd)])} />
      <SectionTitle>{t('macro.scenario')}</SectionTitle>
      <KV rows={Object.entries(scenario).map(([k, v]) =>
        [k, <span style={mono}>{typeof v === 'number' ? money(v) : String(v)}</span>] as [string, ReactNode])} />
      <SectionTitle>{t('macro.limits')}</SectionTitle>
      <KV rows={Object.entries(limits).map(([k, v]) => [k, <span style={mono}>{money(v as number)}</span>] as [string, ReactNode])} />
      <div style={{ fontSize: 10.5, color: 'var(--text-muted)', ...mono, marginTop: 6 }}>
        {t('macro.riskEnforceNote')}
      </div>
      <Ai view="macro_risk" />
    </div>
  );
}

// ── Overview / Reports ────────────────────────────────────────────────────────
function OverviewView() {
  const { t } = useTranslation();
  return (
    <div>
      {[1, 2, 3, 4].map((i) => (
        <p key={i} style={{ fontSize: 12, lineHeight: 1.7, color: 'var(--text-secondary)', ...mono, marginBottom: 10 }}>
          {t(`macro.overviewText${i}`)}
        </p>
      ))}
      <Ai view="macro_overview" />
    </div>
  );
}

// EXACT copy of the WC Pdfs kit (prediction/PredictionArtifact.tsx::Pdfs) — same
// tab styling, open↗ link, per-mount cache-buster, full-height framed iframe.
// Only difference: the report list is dynamic (macro generates a PDF per day/week).
function ReportsView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_reports');
  const [active, setActive] = useState<string | null>(null);
  // Cache-buster: PDFs are served with a fixed URL over the tunnel and browsers
  // cache them hard — a per-mount version token forces a fresh fetch per open.
  const [v] = useState(() => Date.now());
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const reports: MacroReportEntry[] = data?.reports ?? [];
  if (!reports.length) {
    return (
      <div>
        <Generated ts={data?.generated_at} />
        <div className="card" style={{ padding: '14px 12px', textAlign: 'center' }}>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', ...mono }}>{t('macro.noReports')}</div>
        </div>
      </div>
    );
  }
  const label = (r: MacroReportEntry) => {
    const m = r.name.match(/(\d{4})(\d{2})(\d{2})/);
    const d = m ? `${m[2]}-${m[3]}` : '';
    return `${r.kind === 'weekly' ? t('macro.pdfWeekly') : t('macro.pdfDaily')} ${d}`;
  };
  const cur = reports.find((r) => r.url === active) ?? reports[0];
  const url = `${macroFileUrl(cur.url)}?v=${v}`;
  const tab = (on: boolean): CSSProperties => ({
    padding: '6px 14px', border: '2px solid var(--ink)', cursor: 'pointer', ...mono,
    fontSize: 12, fontWeight: 700,
    background: on ? 'var(--ink)' : 'var(--paper)',
    color: on ? 'var(--paper)' : 'var(--ink)', marginRight: 8, marginBottom: 6,
  });
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Generated ts={data?.generated_at} />
      <div className="flex items-center justify-between" style={{ marginBottom: 10 }}>
        <div style={{ display: 'flex', flexWrap: 'wrap' }}>
          {reports.map((r) => (
            <button key={r.url} style={tab(r.url === cur.url)}
              onClick={() => setActive(r.url)}>{label(r)}</button>
          ))}
        </div>
        <a href={url} target="_blank" rel="noreferrer"
          style={{ ...mono, fontSize: 11, color: 'var(--text-muted)',
            textDecoration: 'underline', whiteSpace: 'nowrap' }}>
          {t('macro.open')} ↗
        </a>
      </div>
      {/* Fill to the bottom edge: viewport-relative min-height so the viewer is
          tall regardless of the flex chain through the overflow-auto panel. */}
      <div style={{ flex: 1, minHeight: 'calc(100vh - 150px)',
        border: '2px solid var(--ink)', background: '#fff' }}>
        <iframe key={cur.url} src={url} title={label(cur)}
          style={{ width: '100%', height: '100%',
            minHeight: 'calc(100vh - 150px)', border: 'none' }} />
      </div>
    </div>
  );
}

// ── dispatcher ────────────────────────────────────────────────────────────────
// ── Walk-Forward Lab (dedicated smart artifact, WC-style) ────────────────────
const LEAD_COLORS: Record<string, string> = {
  '1d': 'var(--error)', '3d': 'var(--warning)',
  '5d': 'var(--text-secondary)', '7d': 'var(--success)',
};

// §21.4 split: rolling as-if-live runs + stream bet log stay here; the entry-lead
// sweep experiment (lead compare / curves / coverage) moved to macro_sweep.
function WalkforwardView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_walkforward');
  const [stream, setStream] = useState('hybrid');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const daily = data?.daily;
  return (
    <div>
      <Generated ts={data?.generated_at ?? data?.sweep?.generated_at} />
      {daily && (
        <div style={{ fontSize: 11, ...mono, margin: '2px 0 8px', color: 'var(--accent-primary)', fontWeight: 700 }}>
          {t('macro.wfAsIfLive', { days: daily.days })}
        </div>
      )}
      <KV rows={[
        [t('macro.wfWindow'), <span style={mono}>{daily?.days ?? '—'}d</span>],
      ]} />
      {data?.ml && (data.ml.last30?.n_trades != null) && (() => {
        // baseline rows = the LIVE-scope favourite stream (identical numbers to
        // the bet-log Favourite tab): last30 from the 30d run, last60 from the
        // 60d run. ML/blend run on the selector dataset (fewer entry points).
        const streamWin = (run: any, w: string) => {
          const s = run?.streams?.argmax;
          if (s?.n_trades == null) return null;
          const won = s.won ?? Math.round((s.win_rate ?? 0) * s.n_trades);
          return { ...s, won, window: w };
        };
        const argmaxRows = [streamWin(data.daily, 'last30'), streamWin(data.daily60, 'last60')]
          .filter(Boolean) as any[];
        const rows: any[] = [];
        argmaxRows.forEach((v) => rows.push([
          <span style={mono}>{t('macro.stream_argmax')}</span>,
          <span style={mono}>{v.window}</span>, v.n_trades,
          `${v.won}W-${v.n_trades - v.won}L`, pct(v.win_rate),
          <span style={{ ...mono, color: (v.realized ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(v.realized)}</span>,
          <span style={{ ...mono, color: (v.roi ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{pct(v.roi)}</span>,
        ]));
        const lines: [string, any][] = [
          ['stream_ml', data.ml], ['stream_blend', data.ml.blend]];
        lines.forEach(([label, ln]) => (['last30', 'last60'] as const).forEach((w) => {
          const v = ln?.[w];
          if (v?.n_trades == null) return;
          rows.push([
            <span style={mono}>{t(`macro.${label}`)}</span>,
            <span style={mono}>{w}</span>, v.n_trades,
            `${v.won}W-${v.n_trades - v.won}L`, pct(v.win_rate),
            <span style={{ ...mono, color: (v.realized ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(v.realized)}</span>,
            <span style={{ ...mono, color: (v.roi ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{pct(v.roi)}</span>,
          ]);
        }));
        return (
          <>
            <SectionTitle>{t('macro.mlTitle')}</SectionTitle>
            <DataTable
              cols={[t('macro.colLine'), t('macro.wfWindow'), t('macro.colTrades'),
                'W-L', t('macro.winRate'), t('macro.colPnl'), 'ROI']}
              rows={rows} />
            <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, margin: '2px 0 6px' }}>
              {t('macro.mlNote')}
            </div>
          </>
        );
      })()}
      {daily?.streams && (() => {
        const streams: Record<string, any> = { ...daily.streams };
        if (data?.ml?.last30?.trades?.length) streams.ml = data.ml.last30;
        if (data?.ml?.blend?.last30?.trades?.length) streams.blend = data.ml.blend.last30;
        const names = ['hybrid', 'edge', 'argmax', 'ml', 'blend'].filter((k) => streams[k]);
        return (
          <div className="flex items-center" style={{ margin: '10px 0 2px' }}>
            {names.map((k) => (
              <button key={k} onClick={() => setStream(k)}
                style={{ padding: '3px 10px', fontSize: 11, fontFamily: 'var(--font-mono)',
                  fontWeight: stream === k ? 700 : 400,
                  color: stream === k ? 'var(--text-primary)' : 'var(--text-muted)',
                  background: stream === k ? 'var(--bg-tertiary)' : 'transparent',
                  border: `1px solid ${stream === k ? 'var(--accent-primary)' : 'var(--border-subtle)'}`,
                  borderRadius: 4, cursor: 'pointer', marginRight: 6 }}>
                {t(`macro.stream_${k}`)}
              </button>
            ))}
          </div>
        );
      })()}
      {(() => {
        const sdata = stream === 'ml' ? data?.ml?.last30
          : stream === 'blend' ? data?.ml?.blend?.last30 : daily?.streams?.[stream];
        const tradeList: any[] = sdata?.trades ?? daily?.trades ?? [];
        if (!tradeList.length) return null;
        return (() => {
        // WC BetLog pattern: headline "<W-L> · <PnL> · <context>" + full log with
        // per-bet W/L chips and a cumulative column.
        const trades: any[] = tradeList;
        const wins = trades.filter((x) => x.won).length;
        let cum = 0;
        const withCum = trades.map((x) => { cum += x.realized; return { ...x, cum }; });
        return (
          <>
            <SectionTitle>{t('macro.wfBetLog')}</SectionTitle>
            <div style={{ fontSize: 11, ...mono, marginBottom: 6 }}>
              <b>{wins}W-{trades.length - wins}L</b>
              {' · '}
              <span style={{ color: cum >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(cum)}</span>
              {' · '}
              <span style={{ color: 'var(--text-muted)' }}>
                {trades.length} {t('macro.wfBets')} / {daily.days}d
              </span>
            </div>
            <div style={{ overflowX: 'auto' }}>
              <DataTable
                cols={[t('macro.wfEntryDay'), t('macro.colSeries'), t('macro.wfBetCol'),
                  t('macro.wfLead'), t('macro.colStaked'), t('macro.colVerdict'),
                  t('macro.colPnl'), 'Cum$']}
                rows={withCum.slice().reverse().map((tr: any) => [
                  <span style={{ ...mono, fontSize: 10 }}>{tr.day?.slice(5)}</span>,
                  <span style={mono}>{shortSeries(tr.series)}</span>,
                  <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>{trunc(tr.desc, 30)}</span>,
                  <span style={{ ...mono, fontSize: 10 }}>{tr.lead_days}d</span>,
                  money(tr.staked),
                  <span style={{ color: tr.won ? 'var(--success)' : 'var(--error)', fontWeight: 700 }}>
                    {tr.won ? t('macro.betWon') : t('macro.betLost')}
                  </span>,
                  <span style={{ ...mono, color: tr.realized >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(tr.realized)}</span>,
                  <span style={{ ...mono, color: tr.cum >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(tr.cum)}</span>,
                ])} />
            </div>
          </>
        );
        })();
      })()}
      <Ai view="macro_walkforward" />
    </div>
  );
}

// ── Entry-lead sweep lab (§21.4 split from the Walk-Forward Lab) ─────────────
function SweepView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_walkforward');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const sweep = data?.sweep;
  const leads: [string, any][] = Object.entries(sweep?.leads ?? {});
  const cov: [string, any][] = Object.entries(sweep?.coverage ?? {});
  const testable = cov.filter(([, v]) => v.testable).length;
  // merge lead curves into one chart series: [{day, '1d': pnl, '3d': pnl, ...}]
  const chart: Record<string, any>[] = [];
  const byDay: Record<string, any> = {};
  leads.forEach(([lk, lv]) => (lv.curve ?? []).forEach((p: any) => {
    byDay[p.day] = byDay[p.day] ?? { day: p.day };
    byDay[p.day][lk] = p.pnl;
  }));
  Object.keys(byDay).sort().forEach((d) => chart.push(byDay[d]));
  const bestLead = leads.length
    ? leads.reduce((a, b) => ((b[1].roi ?? -9) > (a[1].roi ?? -9) ? b : a))[0] : null;
  return (
    <div>
      <Generated ts={sweep?.generated_at ?? data?.generated_at} />
      <KV rows={[
        [t('macro.wfWindow'), <span style={mono}>{sweep?.days ?? '—'}d</span>],
        [t('macro.wfCoverage'), <span style={mono}>{testable}/{cov.length} {t('macro.wfSeriesTestable')}</span>],
        [t('macro.wfBestLead'), bestLead
          ? <Chip color={LEAD_COLORS[bestLead] ?? 'var(--success)'}>{bestLead}</Chip> : '—'],
      ]} />
      <SectionTitle>{t('macro.wfLeadCompare')}</SectionTitle>
      <DataTable
        cols={[t('macro.wfLead'), t('macro.colTrades'), t('macro.winRate'),
          t('macro.colPnl'), 'ROI']}
        rows={leads.map(([lk, lv]) => [
          <Chip color={LEAD_COLORS[lk] ?? 'var(--text-muted)'}>{lk}</Chip>,
          lv.n_trades ?? '—', pct(lv.win_rate),
          <span style={{ ...mono, color: (lv.realized ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(lv.realized)}</span>,
          <span style={{ ...mono, color: (lv.roi ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{pct(lv.roi)}</span>,
        ])} />
      {chart.length > 1 && (
        <>
          <SectionTitle>{t('macro.wfCurves')}</SectionTitle>
          <div style={{ width: '100%', height: 180 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chart} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <XAxis dataKey="day" hide />
                <YAxis width={46} tick={{ fontSize: 9, fontFamily: 'var(--font-mono)' }} stroke="var(--text-muted)" />
                <Tooltip contentStyle={{ background: 'var(--bg-secondary)',
                  border: '1px solid var(--border-subtle)', fontSize: 10,
                  fontFamily: 'var(--font-mono)' }} />
                {leads.map(([lk]) => (
                  <Line key={lk} type="monotone" dataKey={lk} connectNulls
                    stroke={LEAD_COLORS[lk] ?? 'var(--accent-primary)'}
                    strokeWidth={1.5} dot={false} isAnimationActive={false} />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
      <SectionTitle>{t('macro.wfCoverageTitle')}</SectionTitle>
      <DataTable
        cols={[t('macro.colSeries'), t('macro.wfEvents'), '']}
        rows={cov.sort((a, b) => b[1].events_in_window - a[1].events_in_window)
          .map(([k, v]) => [
            <span style={mono}>{shortSeries(k)}</span>, v.events_in_window,
            v.testable
              ? <Chip color="var(--success)">{t('macro.wfTestable')}</Chip>
              : <Chip color="var(--text-muted)">{t('macro.wfNoData')}</Chip>,
          ])} />
      <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginTop: 6 }}>
        {t('macro.wfLabNote')}
      </div>
      <Ai view="macro_walkforward" />
    </div>
  );
}

// ── macro_livereplay: replay-vs-live divergence accounting (research/live_replay) ──
//
// The panel's job is to stop anyone reading it as a scoreboard. The two paths CANNOT
// match — the replay looks once a day at a candle close, takes at most one trade per
// event, and carries no breaker/depth/staleness gate — so the honest question is not
// "who won" but "is every difference charged to a known limit". Hence: the verdict and
// the unexplained count lead, the P&L table is explicitly labelled not-a-comparison, and
// each leg carries the bucket it was charged to.

/** UNEXPLAINED is the only alarm; DISAGREED is worth reading; STRUCTURAL is expected. */
const bucketColor = (b?: string) =>
  b?.startsWith('UNEXPLAINED') ? 'var(--error)'
    : b?.startsWith('DISAGREED') ? 'var(--warning)' : 'var(--text-muted)';
/** "STRUCTURAL:no_reentry" -> "no reentry" — the prefix is carried by the chip colour. */
const bucketLabel = (b?: string) => (b ?? '—').split(':').pop()!.replace(/_/g, ' ');

function LiveReplayView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<MacroLiveReplay>('macro_livereplay');
  const [tab, setTab] = useState<'matched' | 'replay_only' | 'live_only'>('live_only');
  if (loading && !data) return <Loading />;
  if (error && !data) return <ErrorBox e={error} />;
  const L = data?.latest;
  if (!L) return <div className="text-xs py-3" style={{ color: 'var(--text-muted)', ...mono }}>{t('macro.lrNoRun')}</div>;
  const rec = L.reconciliation ?? {};
  const opp = L.opportunity ?? {};
  const rep = L.replay ?? {}; const liv = L.live ?? {};
  const aligned = !rec.n_unexplained;
  const legs: MacroReplayLeg[] =
    (tab === 'matched' ? rec.matched : tab === 'replay_only' ? rec.replay_only : rec.live_only) ?? [];
  // Both sides' charges in one table: the buckets are disjoint by construction
  // (a leg is replay-only or live-only, never both), so a plain merge is safe.
  const causes: [string, number][] = Object.entries<number>({
    ...(rec.replay_only_by_cause ?? {}), ...(rec.live_only_by_cause ?? {}),
  }).sort((a, b) => b[1] - a[1]);
  const hist = data?.history ?? [];
  const wl = (n?: number, w?: number) => (n == null ? '—' : `${w ?? 0}W-${n - (w ?? 0)}L`);

  return (
    <div>
      <Generated ts={L.generated_at ?? data?.latest_ts} />
      <KV rows={[
        [t('macro.lrWindow'), <span style={mono}>{L.window_start} → {L.window_end} ({L.days}d)</span>],
        [t('macro.lrVerdict'),
          <Chip color={aligned ? 'var(--success)' : 'var(--error)'}>
            {aligned ? t('macro.lrAligned') : t('macro.lrReview')}
          </Chip>],
        [t('macro.lrUnexplained'),
          <span style={{ ...mono, fontWeight: 700, color: aligned ? 'var(--text-muted)' : 'var(--error)' }}>
            {rec.n_unexplained ?? '—'}
          </span>],
      ]} />

      {/* the two paths, side by side — deliberately NOT framed as a contest */}
      <SectionTitle>{t('macro.lrTracks')}</SectionTitle>
      <DataTable
        cols={['', t('macro.colTrades'), 'W-L', t('macro.colStaked'), t('macro.colPnl'), 'ROI']}
        rows={[
          [<Chip color="var(--text-muted)">{t('macro.lrReplay')}</Chip>,
            rep.n_trades ?? '—', wl(rep.n_trades, rep.won), money(rep.staked),
            <span style={{ ...mono, color: (rep.realized ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(rep.realized)}</span>,
            <span style={{ ...mono, color: (rep.roi ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{pct(rep.roi)}</span>],
          [<Chip color="var(--accent-primary)">{t('macro.lrLive')}</Chip>,
            liv.n_trades ?? '—', wl(liv.n_trades, liv.won), money(liv.staked),
            <span style={{ ...mono, color: (liv.realized ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(liv.realized)}</span>,
            liv.staked ? <span style={{ ...mono, color: (liv.realized ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{pct((liv.realized ?? 0) / liv.staked)}</span> : '—'],
        ]} />
      <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginTop: 6, lineHeight: 1.5 }}>
        {t('macro.lrNotAContest')}
      </div>

      {/* where the difference went */}
      <SectionTitle>{t('macro.lrAccounting')}</SectionTitle>
      <div style={{ display: 'flex', gap: 6, marginBottom: 8 }}>
        {([['matched', rec.n_matched], ['replay_only', rec.n_replay_only],
           ['live_only', rec.n_live_only]] as const).map(([k, n]) => (
          // TabButton's props do not include `key` (same as GroupTabs above), so the
          // key rides on a display:contents wrapper that adds no box of its own.
          <span key={k} style={{ display: 'contents' }}>
            <TabButton active={tab === k} onClick={() => setTab(k as typeof tab)}
              label={`${t(`macro.lr_${k}`)} ${n ?? 0}`} />
          </span>
        ))}
      </div>
      {legs.length > 0 ? (
        <div style={{ overflowX: 'auto' }}>
          <DataTable
            cols={[t('macro.wfEntryDay'), t('macro.colSeries'), t('macro.wfBetCol'),
              t('macro.colStaked'), t('macro.colPnl'), t('macro.lrCharge')]}
            rows={legs.map((x) => [
              <span style={{ ...mono, fontSize: 10 }}>{String(x.day ?? '').slice(5)}</span>,
              <span style={mono}>{shortSeries(x.series)}</span>,
              <span style={{ color: 'var(--text-muted)', fontSize: 10 }} title={x.detail ?? ''}>{trunc(x.desc, 26)}</span>,
              money(x.staked),
              <span style={{ ...mono, color: (x.realized ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(x.realized)}</span>,
              x.bucket
                ? <span title={x.detail ?? ''}><Chip color={bucketColor(x.bucket)}>{bucketLabel(x.bucket)}</Chip></span>
                : <span style={{ color: 'var(--text-muted)' }}>—</span>,
            ])} />
        </div>
      ) : (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', ...mono }}>{t('macro.lrNoLegs')}</div>
      )}
      {causes.length > 0 && (
        <DataTable
          cols={[t('macro.lrCharge'), t('macro.colTrades')]}
          rows={causes.map(([b, n]) => [
            <Chip color={bucketColor(b)}>{bucketLabel(b)}</Chip>, n,
          ])} />
      )}

      {/* the second half: what never became a decision at all */}
      <SectionTitle>{t('macro.lrOpportunity')}</SectionTitle>
      <KV rows={[
        [t('macro.lrNPass'), <span style={mono}>{opp.n_pass ?? '—'}</span>],
        [t('macro.lrInfraShare'),
          <span style={{ ...mono, fontWeight: 700, color: (opp.infra_share ?? 0) > 0.2 ? 'var(--warning)' : 'var(--text-muted)' }}>
            {pct(opp.infra_share)}
          </span>],
      ]} />
      {opp.by_reason && (
        <DataTable
          cols={[t('macro.lrReason'), t('macro.colTrades')]}
          rows={Object.entries<number>(opp.by_reason).sort((a, b) => b[1] - a[1]).map(([r, n]) => [
            <span style={{ ...mono, fontSize: 10 }}>{r.replace(/_/g, ' ')}</span>, n,
          ])} />
      )}

      {/* the trail — one point per daily run; a verdict that flips is the signal */}
      {hist.length > 1 && (
        <>
          <SectionTitle>{t('macro.lrTrail')}</SectionTitle>
          <div style={{ width: '100%', height: 150 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={hist} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                <XAxis dataKey="window_end" tick={{ fontSize: 9, fontFamily: 'var(--font-mono)' }} stroke="var(--text-muted)" />
                <YAxis width={46} tick={{ fontSize: 9, fontFamily: 'var(--font-mono)' }} stroke="var(--text-muted)" />
                <Tooltip
                  formatter={(v: any, n: any) => [money(Number(v)), String(n) === 'replay_realized' ? t('macro.lrReplay') : t('macro.lrLive')]}
                  contentStyle={{ background: 'var(--bg-secondary)', border: '1px solid var(--border-subtle)', fontSize: 10, fontFamily: 'var(--font-mono)' }} />
                <Line type="monotone" dataKey="replay_realized" stroke="var(--text-muted)" strokeWidth={1.5} dot={false} connectNulls isAnimationActive={false} />
                <Line type="monotone" dataKey="live_realized" stroke="var(--accent-primary)" strokeWidth={1.5} dot={false} connectNulls isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </>
      )}

      <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginTop: 8, lineHeight: 1.5 }}>
        {t('macro.lrNote')}
      </div>
      <Ai view="macro_livereplay" />
    </div>
  );
}

export const REGISTRY: Record<string, ComponentType<any>> = {
  macro_bets: BetsView,
  macro_board: BoardView,
  macro_fed: FedView,
  macro_inflation: InflationView,
  macro_labor: LaborView,
  macro_energy: EnergyView,
  macro_divergence: DivergenceView,
  macro_decisions: DecisionsView,
  macro_performance: PerformanceView,
  macro_calibration: CalibrationView,
  macro_replay: ReplayView,
  macro_scoring: ScoringView,
  macro_coverage: CoverageView,
  macro_alerts: AlertsView,
  macro_risk: RiskView,
  macro_overview: OverviewView,
  macro_reports: ReportsView,
  macro_walkforward: WalkforwardView,
  macro_sweep: SweepView,
  macro_livereplay: LiveReplayView,
};

const KEY_BY_TYPE: Record<string, string> = Object.fromEntries(MACRO_ITEMS.map((i) => [i.type, i.i18nKey]));

/** §21.3 second-level tab bar: sibling artifacts of the current type's group. */
function GroupTabs({ current }: { current: string }) {
  const { t } = useTranslation();
  const setArtifact = useSetArtifact();
  const group = groupOfType(current);
  if (!group || group.types.length < 2) return null;
  return (
    <div style={{ marginBottom: 8 }}>
      {group.types.map((tp) => {
        const label = t(`macro.${KEY_BY_TYPE[tp]}`);
        return (
          <span key={tp} style={{ display: 'contents' }}>
            <TabButton active={tp === current}
              onClick={() => { if (tp !== current) setArtifact({ type: tp, title: label }); }}
              label={label} />
          </span>
        );
      })}
    </div>
  );
}

export default function MacroArtifact({ artifact }: { artifact: { type: string; title?: string; params?: any } }) {
  const { t } = useTranslation();
  const View = REGISTRY[artifact.type];
  if (!View) return <div className="text-xs py-3" style={{ color: 'var(--text-muted)', ...mono }}>Unknown artifact: {artifact.type}</div>;
  const key = KEY_BY_TYPE[artifact.type];
  return (
    <div>
      {key && (
        <div className="flex items-center justify-between" style={{ marginBottom: 6, minHeight: 22 }}>
          <div style={{ fontSize: 13, fontWeight: 700, letterSpacing: '.1em', textTransform: 'uppercase', color: 'var(--text-primary)', ...mono }}>
            {t(`macro.${key}`)}
          </div>
        </div>
      )}
      <GroupTabs current={artifact.type} />
      <View params={artifact.params} />
    </div>
  );
}

export const isMacroArtifact = (type?: string) => !!type && type.startsWith('macro_');
