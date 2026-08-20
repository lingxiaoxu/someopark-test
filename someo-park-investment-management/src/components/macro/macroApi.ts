/**
 * macroApi — data access for the Macro Markets (Kalshi macro paper-trading) family.
 * Mirrors the prediction family's conventions: static JSON under /data/*.json with
 * cache:'no-store' (files are regenerated continuously), same header pattern as
 * lib/api.ts (x-api-key + ngrok skip). New file — lib/api.ts itself is untouched.
 */
import { API_BASE, apiHeaders } from '../../lib/api';

/** Fetch one macro_*.json data file (name without extension, e.g. "macro_board").
 * Defensive parse: Python's json module can emit bare NaN/Infinity, which strict
 * JSON.parse rejects — sanitize to null first so one bad float never blanks a
 * whole view (the exporter also sanitizes; this is the second belt). */
export async function getMacroJson<T = any>(name: string): Promise<T> {
  const res = await fetch(`${API_BASE}/data/${name}.json`, { headers: apiHeaders(), cache: 'no-store' });
  if (!res.ok) throw new Error(`API error: ${res.status} ${res.statusText}`);
  const text = await res.text();
  return JSON.parse(text.replace(/(?<![\w"'-])-?(?:NaN|Infinity)(?![\w"])/g, 'null'));
}

/** Absolute URL for a server-relative macro data file (e.g. report PDFs under /data/). */
export const macroFileUrl = (path: string) => `${API_BASE}${path}`;

// ── typed getters for the newer macro exports ────────────────────────────────
export type MacroHealth = {
  ts?: string;
  sources?: Record<string, any>;
  series?: Record<string, { status?: string; notes?: string[] }>;
  flags?: any[];
};
export const getMacroHealth = () => getMacroJson<MacroHealth>('macro_health');

export type MacroReportEntry = { name: string; url: string; mtime?: string; kind?: string };
export type MacroReports = { generated_at?: string; reports?: MacroReportEntry[] };
export const getMacroReports = () => getMacroJson<MacroReports>('macro_reports');

export type MacroPricetrack = {
  generated_at?: string;
  track?: { ts: string; pnl_usd: number; n_legs?: number }[];
};
export const getMacroPricetrack = () => getMacroJson<MacroPricetrack>('macro_pricetrack');

/** research/live_replay — the walk-forward re-run over the live window, reconciled
 *  trade-by-trade against the live ledger. `bucket` is the charge: STRUCTURAL:* is a
 *  documented harness limit, DISAGREED:* is the two rules genuinely differing, and
 *  UNEXPLAINED is the only one that raises an alert. */
export type MacroReplayLeg = {
  series: string; period: string; day?: string; desc?: string;
  staked?: number | null; realized?: number | null; won?: boolean;
  bucket?: string; detail?: string; n_live_on_key?: number;
};
export type MacroLiveReplay = {
  generated_at?: string;
  latest_ts?: string;
  latest?: {
    window_start?: string; window_end?: string; days?: number; generated_at?: string;
    replay?: { n_trades?: number; won?: number; staked?: number; realized?: number; roi?: number | null };
    live?: { n_trades?: number; n_open?: number; won?: number; staked?: number; realized?: number };
    reconciliation?: {
      n_replay?: number; n_live?: number; n_matched?: number;
      n_replay_only?: number; n_live_only?: number; n_unexplained?: number;
      replay_only_by_cause?: Record<string, number>;
      live_only_by_cause?: Record<string, number>;
      matched?: MacroReplayLeg[]; replay_only?: MacroReplayLeg[]; live_only?: MacroReplayLeg[];
      unexplained?: MacroReplayLeg[]; verdict?: string; note?: string;
    };
    opportunity?: {
      n_pass?: number; infra_share?: number | null;
      by_bucket?: Record<string, number>; by_reason?: Record<string, number>;
      counterfactual?: { n_infra_blocked_events?: number; n_replay_traded?: number;
        replay_realized?: number; replay_staked?: number; caveat?: string };
    };
  };
  history?: { window_end?: string; days?: number; replay_realized?: number | null;
    live_realized?: number | null; n_matched?: number; n_replay_only?: number;
    n_live_only?: number; n_unexplained?: number; infra_share?: number | null }[];
};
export const getMacroLiveReplay = () => getMacroJson<MacroLiveReplay>('macro_livereplay');

/** On-demand local-model analysis of one macro view (slow; call one at a time). */
export const analyzeMacro = (view: string, lang: string) =>
  fetch(`${API_BASE}/api/macro/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...apiHeaders() },
    body: JSON.stringify({ view, lang }),
  }).then(async (r) => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json() as Promise<{ analysis: string; cached?: boolean }>;
  });

// ── shared macro-board helpers (used by MacroUpcoming + several artifact views) ──

export type MacroRelease = {
  series: string; family: string; cadence: string; period: string;
  scheduled_ts: string; note?: string;
};
export type MacroDecision = {
  ts_utc?: string; kind?: string; fair?: number | null; ask?: number | null;
  net_edge?: number | null; size_usd?: number | null; note?: string;
};
export type MacroEntry = {
  period: string;
  pred?: { asof?: string; model?: string; dist?: any };
  decision?: MacroDecision | null;
};
export type MacroBoard = {
  generated_at?: string;
  next_releases?: MacroRelease[];
  series?: Record<string, { family: string; cadence: string; structure: string; entries: MacroEntry[] }>;
};

/** Compact human summary of a prediction distribution:
 *  gmix / empirical → "p50 [p5, p95]"; categorical → top-2 probs. */
/** Compact number: at most 4 decimals, trailing zeros stripped (display-wide rule). */
export const nice = (v: any): string => {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return String(Number(n.toFixed(4)));
};

/** Standard normal CDF (A&S 7.1.26 erf, |err| < 1.5e-7 — display precision). */
function normCdf(z: number): number {
  const s = z < 0 ? -1 : 1, x = Math.abs(z) / Math.SQRT2;
  const t = 1 / (1 + 0.3275911 * x);
  const y = 1 - ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t
    - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x);
  return 0.5 * (1 + s * y);
}

/** Quantile of a Gaussian mixture, by bisection on its CDF.
 *  A scale mixture (all comps share a mean) has a stdev dominated by its fat-tail
 *  component, so "mu ± sd" describes a region the forecast doesn't believe in —
 *  KXPAYROLLS reads 74,967 ± 74,431 (±99%) while carrying 80% of its mass inside
 *  ±73,884. A quantile band states the same distribution without implying symmetry
 *  of mass, and matches how the empirical branch below reports. */
function gmixQuantile(comps: any[], p: number): number {
  let lo = Infinity, hi = -Infinity;
  for (const [, m, s] of comps) {
    lo = Math.min(lo, m - 12 * Math.abs(s));
    hi = Math.max(hi, m + 12 * Math.abs(s));
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return NaN;
  const wsum = comps.reduce((a, c) => a + c[0], 0) || 1;
  const cdf = (x: number) => comps.reduce(
    (a, [w, m, s]) => a + (w / wsum) * (s > 0 ? normCdf((x - m) / s) : (x >= m ? 1 : 0)), 0);
  for (let i = 0; i < 80; i++) {
    const mid = (lo + hi) / 2;
    if (cdf(mid) < p) lo = mid; else hi = mid;
  }
  return (lo + hi) / 2;
}

export function predSummary(dist: any): string {
  if (!dist || !dist.kind) return '—';
  if (dist.kind === 'gmix' && Array.isArray(dist.comps) && dist.comps.length) {
    // comps: [[weight, mu, sigma], ...]
    const q = (p: number) => gmixQuantile(dist.comps, p);
    return `${nice(q(0.5))} [${nice(q(0.05))}, ${nice(q(0.95))}]`;
  }
  if (dist.kind === 'empirical' && Array.isArray(dist.quantiles) && dist.quantiles.length) {
    const q = dist.quantiles.filter((v: any) => Number.isFinite(v)) as number[];
    if (!q.length) return '—';
    // quantiles[] is np.linspace(0, 1, N) (model/common.py Empirical.to_json), so
    // index i is the i/(N-1) quantile and the percentile MUST come off the array's
    // own length. This used to hardcode 10/100/190 for N=201; the models moved to
    // N=1001 and `Math.min` clamped instead of throwing, so every energy/rates card
    // silently printed its 10th percentile as the median (WTI was $4.51/bbl low and
    // U3's band 3x too narrow) for as long as _EMP_QUANTILES has been 1001.
    const at = (p: number) => {
      const pos = p * (q.length - 1), lo = Math.floor(pos), hi = Math.ceil(pos);
      return q[lo] + (q[hi] - q[lo]) * (pos - lo);
    };
    return `${nice(at(0.5))} [${nice(at(0.05))}, ${nice(at(0.95))}]`;
  }
  if (dist.kind === 'categorical' && dist.probs) {
    const top = Object.entries(dist.probs as Record<string, number>)
      .sort((a, b) => b[1] - a[1]).slice(0, 2);
    return top.map(([k, v]) => `${k} ${(v * 100).toFixed(1)}%`).join(' · ');
  }
  return '—';
}

/** family → the artifact type its releases open. */
export function familyArtifact(family?: string): string {
  switch (family) {
    case 'fed': return 'macro_fed';
    case 'labor': return 'macro_labor';
    case 'inflation': return 'macro_inflation';
    case 'energy': return 'macro_energy';
    default: return 'macro_board';
  }
}
