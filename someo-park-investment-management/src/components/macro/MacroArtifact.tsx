/**
 * MacroArtifact — renders the right-panel content for every `macro_*` artifact.
 * One dispatcher + a set of real data viewers, each fetching the static JSON
 * exported by the Kalshi macro paper-trading system into public/data/ (macro_board,
 * macro_fed, macro_divergence, macro_decisions, macro_performance, macro_oos,
 * macro_coverage, macro_risk). Mirrors the PredictionArtifact dispatch pattern;
 * styling uses CSS vars / .table & .card classes, so it inverts with the theme.
 */
import type { ComponentType, ReactNode } from 'react';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip } from 'recharts';
import { getMacroJson, analyzeMacro, predSummary, macroFileUrl, getMacroHealth, getMacroPricetrack } from './macroApi';
import type { MacroBoard, MacroDecision, MacroHealth, MacroPricetrack, MacroReportEntry } from './macroApi';
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
  return (
    <div className="flex items-center flex-wrap" style={{ gap: 8, marginBottom: 8 }}>
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
  const open: any[] = data?.open_by_series ?? [];
  const settled: any[] = data?.settled_by_series ?? [];
  const byOrigin: any[] = data?.settled_by_origin ?? [];
  const openKind: any[] = data?.open_by_kind ?? [];
  const track = pt?.track ?? [];
  return (
    <div>
      <Generated ts={data?.generated_at} />
      <KV rows={[
        [t('macro.bankroll'), <span style={mono}>{money(data?.bankroll_usd)}</span>],
        [t('macro.unrealized'), <span style={{ ...mono, color: (data?.unrealized_usd ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(data?.unrealized_usd)}</span>],
        [t('macro.mode'), <Chip color="var(--accent-primary)">{data?.mode ?? '—'}</Chip>],
      ]} />
      <SectionTitle>{t('macro.openBySeries')}</SectionTitle>
      <DataTable
        cols={[t('macro.colSeries'), t('macro.colOpenN'), t('macro.colStaked')]}
        rows={open.map((r) => [<span style={mono}>{shortSeries(r.series)}</span>, r.n, money(r.staked)])} />
      <SectionTitle>{t('macro.settledBySeries')}</SectionTitle>
      {settled.length ? (
        <DataTable
          cols={[t('macro.colSeries'), t('macro.colSettledN'), t('macro.colPnl')]}
          rows={settled.map((r) => {
            const pnl = r.realized ?? r.pnl_usd ?? r.pnl;
            return [<span style={mono}>{shortSeries(r.series)}</span>, r.n,
              <span style={{ ...mono, color: (pnl ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(pnl)}</span>];
          })} />
      ) : (
        <div className="text-xs py-2" style={{ color: 'var(--text-muted)', ...mono }}>{t('macro.noneSettled')}</div>
      )}
      {(byOrigin.length > 0 || openKind.length > 0) && (
        <>
          <SectionTitle>{t('macro.byStrategy')}</SectionTitle>
          <DataTable
            cols={[t('macro.colOrigin'), t('macro.colOpenN'), t('macro.colStaked'),
              t('macro.colSettledN'), t('macro.colPnl')]}
            rows={['open', 'arb', 'snipe'].map((k) => {
              const o = openKind.find((r) => r.kind === k);
              const s = byOrigin.find((r) => (r.origin ?? 'open') === k);
              if (!o && !s) return null;
              const pnl = s?.realized;
              return [
                <Chip color={kindColor(k)}>{t(`macro.origin_${k}`)}</Chip>,
                o?.n ?? 0, o?.staked != null ? money(o.staked) : '—',
                s?.n ?? 0,
                pnl != null
                  ? <span style={{ ...mono, color: pnl >= 0 ? 'var(--success)' : 'var(--error)' }}>{money(pnl)}</span>
                  : '—'];
            }).filter(Boolean) as any[]} />
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

// ── Calibration (OOS) ─────────────────────────────────────────────────────────
function CalibrationView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_oos');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const experiments: any[] = (data?.experiments ?? [])
    .filter((ex: any) => ex.name !== 'decision_replay');
  const decReplays: any[] = data?.decision_replays ?? [];
  const gates: any[] = data?.component_gates ?? [];
  return (
    <div>
      <Generated ts={data?.generated_at} />
      {decReplays.length > 0 && (
        <>
          <SectionTitle>{t('macro.decisionReplay')}</SectionTitle>
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
          <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, margin: '4px 0 10px' }}>
            {t('macro.decisionReplayNote')}
          </div>
        </>
      )}
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
        <div style={{ fontSize: 10.5, color: 'var(--text-muted)', ...mono, marginTop: 6 }}>
          {t('macro.gateNote')}: {data.gate_note}
        </div>
      )}
      <Ai view="macro_calibration" />
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

function CoverageView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_coverage');
  if (loading && !data) return <Loading />; if (error && !data) return <ErrorBox e={error} />;
  const cov: any[] = data?.coverage ?? [];
  const missed: any[] = data?.missed ?? [];
  const alerts: any[] = data?.alerts ?? [];
  const modelAlerts = alerts.filter((a) => MODEL_ALERT_SOURCES.has(a.source));
  const opsAlerts = alerts.filter((a) => !MODEL_ALERT_SOURCES.has(a.source));
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
      <SectionTitle>{t('macro.consistencyAlerts')}</SectionTitle>
      <AlertList items={modelAlerts} />
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
      <SectionTitle>{t('macro.limits')}</SectionTitle>
      <KV rows={Object.entries(limits).map(([k, v]) => [k, <span style={mono}>{money(v as number)}</span>] as [string, ReactNode])} />
      <SectionTitle>{t('macro.scenario')}</SectionTitle>
      <KV rows={Object.entries(scenario).map(([k, v]) =>
        [k, <span style={mono}>{typeof v === 'number' ? money(v) : String(v)}</span>] as [string, ReactNode])} />
      <SectionTitle>{t('macro.openExposure')}</SectionTitle>
      <DataTable
        cols={[t('macro.colSeries'), t('macro.colPeriod'), t('macro.colSize')]}
        rows={exposure.map((r) => [<span style={mono}>{shortSeries(r.series)}</span>, r.period, money(r.size_usd)])} />
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

function ReportsView() {
  const { t } = useTranslation();
  const { data, loading, error } = useMacro<any>('macro_reports');
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
  return (
    <div>
      <Generated ts={data?.generated_at} />
      <DataTable
        cols={[t('macro.colName'), t('macro.colKind'), t('macro.colTime'), '']}
        rows={reports.map((r) => [
          <span style={mono}>{r.name}</span>,
          <Chip color={r.kind === 'weekly' ? 'var(--accent-primary)' : 'var(--text-muted)'}>{r.kind ?? '—'}</Chip>,
          <span style={{ ...mono, fontSize: 10 }}>{dt(r.mtime)}</span>,
          <a href={macroFileUrl(r.url)} target="_blank" rel="noreferrer"
            style={{ ...mono, fontSize: 10.5, color: 'var(--accent-primary)', textDecoration: 'underline' }}>
            {t('macro.download')}
          </a>,
        ])} />
    </div>
  );
}

// ── dispatcher ────────────────────────────────────────────────────────────────
export const REGISTRY: Record<string, ComponentType<any>> = {
  macro_board: BoardView,
  macro_fed: FedView,
  macro_inflation: InflationView,
  macro_labor: LaborView,
  macro_energy: EnergyView,
  macro_divergence: DivergenceView,
  macro_decisions: DecisionsView,
  macro_performance: PerformanceView,
  macro_calibration: CalibrationView,
  macro_coverage: CoverageView,
  macro_risk: RiskView,
  macro_overview: OverviewView,
  macro_reports: ReportsView,
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
