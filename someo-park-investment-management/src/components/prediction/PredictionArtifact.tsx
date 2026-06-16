/**
 * PredictionArtifact — renders the right-panel content for every World Cup `wc_*`
 * artifact. One dispatcher + a set of real data viewers (no stubs), each fetching
 * the static JSON synced from prediction_market/data/output/ via getWC*.
 * Styling uses CSS vars / the .table & .card classes, so it inverts with the theme.
 */
import type { CSSProperties, ReactNode, ReactElement } from 'react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import {
  getWCChampion, getWCDivergence, getWCUpcoming, getWCPerformance,
  getWCRisk, getWCCalibration, getWCInplay, getWCOverview,
} from '../../lib/api';
import { PREDICTION_ITEMS } from './PredictionArtifactGrid';
import { tCountry } from '../../i18n/countries';

// ── shared primitives ─────────────────────────────────────────────────────────
const pct = (v?: number | null, d = 1) => (v == null || isNaN(v) ? '—' : `${(v * 100).toFixed(d)}%`);
const num = (v?: number | null, d = 3) => (v == null || isNaN(v) ? '—' : v.toFixed(d));
const money = (v: any) => (typeof v === 'number' ? `$${v.toFixed(2)}` : String(v ?? '—'));

const mono: CSSProperties = { fontFamily: 'var(--font-mono)' };

function Loading() { return <div className="text-xs py-3" style={{ color: 'var(--text-muted)', ...mono }}>Loading…</div>; }
function ErrorBox({ e }: { e: string }) {
  return <div className="text-xs py-3" style={{ color: 'var(--error)', ...mono }}>Failed to load: {e}. Run the exporter + <code>npm run sync:wc</code>.</div>;
}
// Heading is rendered (translated) by the dispatcher; Title keeps only the sub line.
function Title({ children, sub }: { children?: ReactNode; sub?: string }) {
  void children;
  return sub ? (
    <div className="mb-3" style={{ fontSize: 10, color: 'var(--text-muted)', ...mono }}>{sub}</div>
  ) : null;
}
function KV({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <table className="table" style={{ marginBottom: 12 }}>
      <tbody>
        {rows.map(([k, v], i) => (
          <tr key={i}><td style={{ fontWeight: 700, width: '50%' }}>{k}</td><td style={{ textAlign: 'right' }}>{v}</td></tr>
        ))}
      </tbody>
    </table>
  );
}
function DataTable({ cols, rows }: { cols: string[]; rows: ReactNode[][] }) {
  return (
    <table className="table">
      <thead><tr>{cols.map((c, i) => <th key={i} style={{ textAlign: i === 0 ? 'left' : 'right' }}>{c}</th>)}</tr></thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>{r.map((cell, j) => <td key={j} style={{ textAlign: j === 0 ? 'left' : 'right' }}>{cell}</td>)}</tr>
        ))}
      </tbody>
    </table>
  );
}
function Notes({ items }: { items?: string[] }) {
  if (!items?.length) return null;
  return (
    <ul style={{ marginTop: 10, paddingLeft: 16, fontSize: 11, color: 'var(--text-muted)', ...mono }}>
      {items.map((n, i) => <li key={i} style={{ marginBottom: 4 }}>{n}</li>)}
    </ul>
  );
}

// ── viewers ───────────────────────────────────────────────────────────────────
function ChampionOdds() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCChampion(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const champ = (data?.champion ?? []).slice(0, 16);
  return (
    <div>
      <Title sub={`Monte-Carlo · ${data?.meta?.n_sims?.toLocaleString?.() ?? ''} sims · prior ${data?.meta?.prior_as_of ?? ''}`}>Champion Odds</Title>
      <DataTable cols={[tr('prediction.team'), 'Grp', 'Champ', 'Final', 'SF', 'Rating']}
        rows={champ.map((c: any) => [tCountry(c.name), c.group, pct(c.p_champion), pct(c.p_final), pct(c.p_sf), num(c.rating, 3)])} />
    </div>
  );
}

function GoldenBoot() {
  const { data, loading, error } = useApi<any>(() => getWCChampion(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const gb = (data?.golden_boot ?? []).slice(0, 16);
  return (
    <div>
      <Title sub="Player goal-scoring (nested in tournament sim)">Golden Boot</Title>
      <DataTable cols={['Player', 'P(boot)', 'E[goals]']}
        rows={gb.map((p: any) => [p.name, pct(p.p_golden_boot), num(p.e_goals, 2)])} />
    </div>
  );
}

function Methodology() {
  const { data, loading, error } = useApi<any>(() => getWCChampion(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  return (
    <div>
      <Title sub={`${data?.meta?.code_version ?? ''}`}>Model Notes / Methodology</Title>
      <Notes items={data?.meta?.model_notes} />
    </div>
  );
}

function Divergence() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any[]>(() => getWCDivergence(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const rows = (data ?? []);
  return (
    <div>
      <Title sub="Model 3-way vs sharp bookmaker de-vig; edge = model − book">Model vs Market</Title>
      <DataTable cols={[tr('prediction.match'), 'Model H/D/A', 'Book H/D/A', 'Best edge']}
        rows={rows.map((m: any) => {
          const e = m.edge_vs_book || {};
          const best = Math.max(Math.abs(e.home ?? 0), Math.abs(e.draw ?? 0), Math.abs(e.away ?? 0));
          const side = best === Math.abs(e.home ?? 0) ? 'H' : best === Math.abs(e.draw ?? 0) ? 'D' : 'A';
          const val = side === 'H' ? e.home : side === 'D' ? e.draw : e.away;
          return [
            `${tCountry(m.home)} v ${tCountry(m.away)}`,
            `${pct(m.model?.home, 0)}/${pct(m.model?.draw, 0)}/${pct(m.model?.away, 0)}`,
            `${pct(m.book_devig?.home, 0)}/${pct(m.book_devig?.draw, 0)}/${pct(m.book_devig?.away, 0)}`,
            <span style={{ color: Math.abs(val) >= 0.05 ? 'var(--success)' : 'var(--text-muted)' }}>{side} {val >= 0 ? '+' : ''}{pct(val, 1)}</span>,
          ];
        })} />
    </div>
  );
}

function Predictions() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCUpcoming(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const ms = data?.matches ?? [];
  return (
    <div>
      <Title sub="Model 3-way + O2.5 / BTTS for upcoming fixtures">Today's Predictions</Title>
      <DataTable cols={[tr('prediction.match'), 'ET', 'H', 'D', 'A', 'O2.5']}
        rows={ms.map((m: any) => [`${tCountry(m.home?.name)} v ${tCountry(m.away?.name)}`, m.et ?? '', pct(m.model?.home, 0), pct(m.model?.draw, 0), pct(m.model?.away, 0), pct(m.model?.over_2_5, 0)])} />
    </div>
  );
}

function MatchPricing() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCUpcoming(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const ms = data?.matches ?? [];
  return (
    <div>
      <Title sub="3-way fair price + real venue asks (Kalshi / Poly US)">Match Pricing</Title>
      {ms.map((m: any, i: number) => (
        <div key={i} className="card" style={{ marginBottom: 10 }}>
          <div style={{ fontWeight: 700, fontSize: 12, ...mono, marginBottom: 6 }}>{tCountry(m.home?.name)} vs {tCountry(m.away?.name)} <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>{m.et}</span></div>
          <DataTable cols={['', tr('prediction.home'), tr('prediction.draw'), tr('prediction.away')]}
            rows={[
              [tr('prediction.model'), pct(m.model?.home, 0), pct(m.model?.draw, 0), pct(m.model?.away, 0)],
              [tr('prediction.kalshiAsk'), num(m.kalshi?.home?.ask, 2), num(m.kalshi?.draw?.ask, 2), num(m.kalshi?.away?.ask, 2)],
              [tr('prediction.polyUsAsk'), num(m.poly_us?.home?.ask, 2), num(m.poly_us?.draw?.ask, 2), num(m.poly_us?.away?.ask, 2)],
            ]} />
        </div>
      ))}
    </div>
  );
}

function Schedule() {
  const { t: tr } = useTranslation();
  const { data, loading, error } = useApi<any>(() => getWCUpcoming(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const ms = data?.matches ?? [];
  return (
    <div>
      <Title sub="Kickoffs in US Eastern (from upcoming.json)">Schedule</Title>
      <DataTable cols={['ET kickoff', 'Round', tr('prediction.match')]}
        rows={ms.map((m: any) => [m.et ?? m.kickoff, m.round ?? '', `${tCountry(m.home?.name)} v ${tCountry(m.away?.name)}`])} />
    </div>
  );
}

function InPlay() {
  const { data, loading, error } = useApi<any>(() => getWCInplay(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const sigs = data?.signals ?? [];
  return (
    <div>
      <Title sub={`${data?.n_live ?? 0} live · per-minute lock-arb / relative-value / tactics`}>In-Play Arbitrage</Title>
      {sigs.length ? (
        <DataTable cols={['Match', 'Min', 'Kind', 'Side', 'Edge', 'Action']}
          rows={sigs.map((s: any) => [s.match, s.minute, s.kind, s.side, num(s.edge, 3), s.action])} />
      ) : <div className="text-xs py-2" style={{ color: 'var(--text-muted)', ...mono }}>No live matches right now — signals appear here per minute during games.</div>}
    </div>
  );
}

function PerformanceCard() {
  const { data, loading, error } = useApi<any>(() => getWCPerformance(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const grade = data?.brier <= data?.brier_uniform ? 'PASS' : 'BLOCK (not trade-grade)';
  return (
    <div>
      <Title sub="Accuracy on settled matches + paper calibration P&L">Accuracy & P&L</Title>
      <KV rows={[
        ['Settled matches', data?.n_settled],
        ['Brier (lower=better)', `${num(data?.brier, 4)} vs uniform ${num(data?.brier_uniform, 4)}`],
        ['Log-loss', num(data?.log_loss, 4)],
        ['Favourite hit-rate', pct(data?.favourite_hit_rate, 0)],
        ['Calibration P&L', `${num(data?.calibration_pnl, 2)}u (${num(data?.calibration_pnl_per_bet, 3)}u/bet)`],
        ['Trade grade', <span style={{ color: grade === 'PASS' ? 'var(--success)' : 'var(--error)', fontWeight: 700 }}>{grade}</span>],
      ]} />
      <Notes items={data?.notes} />
    </div>
  );
}

function RiskCard() {
  const { data, loading, error } = useApi<any>(() => getWCRisk(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const g = data?.gates ?? {}, b = data?.venue_balances ?? {}, ab = data?.api_budget ?? {};
  return (
    <div>
      <Title sub="Pre-trade guard rails — read-only">Risk Report</Title>
      <KV rows={[
        ['Kalshi env', g.kalshi_env],
        ['Kalshi trading', String(g.kalshi_trading_enabled)],
        ['Poly US trading', String(g.pmus_trading_enabled)],
        ['Order cap', money(g.hard_order_cap_usd)],
        ['Kalshi demo', money(b.kalshi_demo_usd)],
        ['Poly US', money(b.polymarket_us_usd)],
        ['Kalshi prod', String(b.kalshi_prod_usd)],
        ['API budget', `${ab.used}/${ab.cap} (${pct(ab.pct, 0)})`],
        ['Calibration', <span style={{ color: 'var(--error)' }}>{data?.calibration_gate?.status}</span>],
      ]} />
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.1em', margin: '8px 0 4px', ...mono, color: 'var(--text-primary)' }}>Blocked / guard rails</div>
      <ul style={{ paddingLeft: 16, fontSize: 11, color: 'var(--error)', ...mono }}>
        {(data?.blocked_summary ?? []).map((x: string, i: number) => <li key={i} style={{ marginBottom: 3 }}>{x}</li>)}
      </ul>
    </div>
  );
}

function Calibration() {
  const { data, loading, error } = useApi<any>(() => getWCCalibration(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  return (
    <div>
      <Title sub="Out-of-sample reliability (the trade-grade gate)">Calibration (OOS)</Title>
      <KV rows={[
        ['Matches', data?.n_matches],
        ['Brier', `${num(data?.brier, 4)} (uniform ${num(data?.brier_uniform, 4)})`],
        ['Log-loss', num(data?.log_loss, 4)],
        ['Favourite hit-rate', pct(data?.favourite_hit_rate, 0)],
        ['Pred vs obs draw', `${pct(data?.pred_draw_rate, 0)} vs ${pct(data?.obs_draw_rate, 0)}`],
        ['Avg goals pred/obs', `${num(data?.pred_avg_total_goals, 2)} / ${num(data?.obs_avg_total_goals, 2)}`],
      ]} />
      <Notes items={Array.isArray(data?.notes) ? data.notes : data?.notes ? [data.notes] : []} />
    </div>
  );
}

function OverviewCard() {
  const { data, loading, error } = useApi<any>(() => getWCOverview(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  return (
    <div>
      <Title sub={data?.as_of ? `as of ${data.as_of}` : undefined}>System Overview</Title>
      <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 10, ...mono }}>{data?.headline}</div>
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.1em', margin: '8px 0 4px', ...mono }}>Interfaces</div>
      <DataTable cols={['Cat', 'Command', 'Purpose']} rows={(data?.interfaces ?? []).map((i: any) => [i.category, i.command?.replace('python -m prediction_market.', ''), i.purpose])} />
      <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.1em', margin: '10px 0 4px', ...mono }}>Schedule</div>
      <DataTable cols={['When', 'Runs', 'Freq']} rows={(data?.schedule ?? []).map((s: any) => [s.when, s.runs, s.frequency])} />
    </div>
  );
}

function Venues() {
  const { data, loading, error } = useApi<any>(() => getWCRisk(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const g = data?.gates ?? {}, b = data?.venue_balances ?? {};
  return (
    <div>
      <Title sub="Execution venues, balances & trading gates">Venues & Gates</Title>
      <DataTable cols={['Venue', 'Role', 'Balance', 'Trading']}
        rows={[
          ['Kalshi (demo)', 'execute', money(b.kalshi_demo_usd), String(g.kalshi_trading_enabled)],
          ['Polymarket US', 'execute', money(b.polymarket_us_usd), String(g.pmus_trading_enabled)],
          ['Kalshi (prod)', 'real money', String(b.kalshi_prod_usd), 'gated'],
        ]} />
      <div style={{ fontSize: 11, color: 'var(--text-muted)', ...mono }}>Executable: {(g.executable_venues ?? []).join(', ')}</div>
    </div>
  );
}

function Budget() {
  const { data, loading, error } = useApi<any>(() => getWCRisk(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  const ab = data?.api_budget ?? {};
  const frac = Math.min(1, (ab.used ?? 0) / (ab.cap ?? 1));
  return (
    <div>
      <Title sub="API-Football monthly request budget">API Budget / Health</Title>
      <KV rows={[['Used', ab.used], ['Cap', ab.cap], ['Utilisation', pct(ab.pct, 0)]]} />
      <div style={{ height: 10, background: 'var(--bg-tertiary)', border: '1px solid var(--border-subtle)' }}>
        <div style={{ width: `${frac * 100}%`, height: '100%', background: frac > 0.8 ? 'var(--error)' : 'var(--success)' }} />
      </div>
    </div>
  );
}

function ValueCard() {
  const { data, loading, error } = useApi<any>(() => getWCOverview(), []);
  if (loading) return <Loading />; if (error) return <ErrorBox e={error} />;
  return (
    <div>
      <Title sub="What this system delivers + how to see it">Value & How to See</Title>
      <ul style={{ paddingLeft: 16, fontSize: 12, color: 'var(--text-secondary)', ...mono }}>
        {(data?.value ?? []).map((v: string, i: number) => <li key={i} style={{ marginBottom: 6 }}>{v}</li>)}
      </ul>
    </div>
  );
}

function Pdfs() {
  const link: CSSProperties = { display: 'inline-block', padding: '8px 14px', border: '2px solid var(--ink)', background: 'var(--paper)', color: 'var(--ink)', textDecoration: 'none', fontWeight: 700, ...mono, fontSize: 12, marginRight: 10, marginBottom: 10, boxShadow: 'var(--shadow-pixel-sm)' };
  return (
    <div>
      <Title sub="Institutional-style PDF reports">Download Reports</Title>
      <a href="/data/performance_report.pdf" download style={link}>Performance & P&L (PDF)</a>
      <a href="/data/risk_report.pdf" download style={link}>Risk Report (PDF)</a>
    </div>
  );
}

// ── dispatcher ────────────────────────────────────────────────────────────────
const REGISTRY: Record<string, () => ReactElement> = {
  wc_champion: ChampionOdds,
  wc_golden_boot: GoldenBoot,
  wc_methodology: Methodology,
  wc_divergence: Divergence,
  wc_predictions: Predictions,
  wc_match_pricing: MatchPricing,
  wc_schedule: Schedule,
  wc_inplay: InPlay,
  wc_performance: PerformanceCard,
  wc_risk: RiskCard,
  wc_calibration: Calibration,
  wc_overview: OverviewCard,
  wc_venues: Venues,
  wc_budget: Budget,
  wc_value: ValueCard,
  wc_pdfs: Pdfs,
};

const KEY_BY_TYPE: Record<string, string> = Object.fromEntries(PREDICTION_ITEMS.map(i => [i.type, i.i18nKey]));

export default function PredictionArtifact({ type }: { type: string }) {
  const { t } = useTranslation();
  const View = REGISTRY[type];
  if (!View) return <div className="text-xs py-3" style={{ color: 'var(--text-muted)', ...mono }}>Unknown artifact: {type}</div>;
  const key = KEY_BY_TYPE[type];
  return (
    <div>
      {key && <div style={{ fontSize: 13, fontWeight: 700, letterSpacing: '.1em', textTransform: 'uppercase', color: 'var(--text-primary)', marginBottom: 6, ...mono }}>{t(`prediction.${key}`)}</div>}
      <View />
    </div>
  );
}

export const isPredictionArtifact = (type?: string) => !!type && type.startsWith('wc_');
