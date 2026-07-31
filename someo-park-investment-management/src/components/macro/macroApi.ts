/**
 * macroApi — data access for the Macro Markets (Kalshi macro paper-trading) family.
 * Mirrors the prediction family's conventions: static JSON under /data/*.json with
 * cache:'no-store' (files are regenerated continuously), same header pattern as
 * lib/api.ts (x-api-key + ngrok skip). New file — lib/api.ts itself is untouched.
 */
import { API_BASE, apiHeaders } from '../../lib/api';

/** Fetch one macro_*.json data file (name without extension, e.g. "macro_board"). */
export async function getMacroJson<T = any>(name: string): Promise<T> {
  const res = await fetch(`${API_BASE}/data/${name}.json`, { headers: apiHeaders(), cache: 'no-store' });
  if (!res.ok) throw new Error(`API error: ${res.status} ${res.statusText}`);
  return res.json();
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
 *  gmix → "mu ± sigma"; empirical → "p50 [p5, p95]" (quantile idx 10/100/190);
 *  categorical → top-2 probs. */
export function predSummary(dist: any): string {
  if (!dist || !dist.kind) return '—';
  if (dist.kind === 'gmix' && Array.isArray(dist.comps) && dist.comps.length) {
    // comps: [[weight, mu, sigma], ...] → mixture mean ± mixture stdev
    let mu = 0, m2 = 0;
    for (const [w, m, s] of dist.comps) { mu += w * m; m2 += w * (s * s + m * m); }
    const sigma = Math.sqrt(Math.max(0, m2 - mu * mu));
    return `${mu.toFixed(3)} ± ${sigma.toFixed(3)}`;
  }
  if (dist.kind === 'empirical' && Array.isArray(dist.quantiles) && dist.quantiles.length) {
    const q = dist.quantiles;
    const at = (i: number) => q[Math.min(i, q.length - 1)];
    return `${at(100)} [${at(10)}, ${at(190)}]`;
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
