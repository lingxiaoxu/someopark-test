/**
 * countryIndex — "which prediction artifacts does this country appear in?"
 *
 * Powers the CountryName popover: clicking a country lists only the artifacts that
 * ACTUALLY contain it (dynamic presence), each a direct link that scroll-focuses the
 * country in the destination. Built once per session from the same static JSON blobs
 * the artifact views read, then memoised.
 *
 * Presence is derived per dataset with explicit extractors (so e.g. worldcup_model
 * feeds both wc_champion via .champion[].name and wc_golden_boot via .golden_boot[].team),
 * falling back to a recursive country-string sweep for irregular shapes (schedule).
 */
import { useEffect, useState } from 'react';

import { COUNTRY_NAMES, countryKey } from '../i18n/countries';
import {
  getWCChampion, getWCReachRound, getWCSquad, getWCStyles, getWCForm,
  getWCUpcoming, getWCDivergence, getWCSchedule, getWCBacktest,
  getWCInplayLive, getWCMicrofootball, getWCMilestoneMarks, getWCPerformance,
} from './api';

export type CountryIndex = Map<string, Set<string>>;   // countryKey → set of wc_* types

// Tournament progress + seeding shown in the popover header.
export type CountryStatus =
  | { kind: 'reached'; round: string }   // confirmed reached `round`, still alive
  | { kind: 'out'; round: string }       // reached `round` then eliminated there
  | { kind: 'outGroup' }                 // eliminated in the group stage
  | { kind: 'group' };                   // still in the group stage
export interface CountryMeta { fifaRank: number | null; status: CountryStatus | null; }
export type CountryMetaMap = Map<string, CountryMeta>;

// Round order, deepest last. `advance` = made the knockout bracket (Round of 32).
const RORDER = ['advance', 'r16', 'qf', 'sf', 'final'];

// Reach-round model_pct is a probability: 1 = confirmed reached, 0 = confirmed out.
function statusFor(byRound: Record<string, number | undefined>): CountryStatus | null {
  const adv = byRound['advance'];
  if (adv == null) return null;
  if (adv <= 0.0001) return { kind: 'outGroup' };
  let reached: string | null = null;
  for (const k of RORDER) { const v = byRound[k]; if (v != null && v >= 0.9999) reached = k; }
  if (!reached) return { kind: 'group' };
  if (reached === 'final') return { kind: 'reached', round: 'final' };
  const next = RORDER[RORDER.indexOf(reached) + 1];
  const nv = byRound[next];
  if (nv != null && nv <= 0.0001) return { kind: 'out', round: reached };
  return { kind: 'reached', round: reached };
}

// Pull home/away names out of a list of match-like records under several known shapes.
function matchNames(rows: any[]): string[] {
  const out: string[] = [];
  for (const m of rows || []) {
    const h = m?.home?.name ?? m?.home ?? m?.home_name;
    const a = m?.away?.name ?? m?.away ?? m?.away_name;
    if (typeof h === 'string') out.push(h);
    if (typeof a === 'string') out.push(a);
  }
  return out;
}

// Recursively collect every string in a JSON tree that is a known country name.
function sweepCountries(node: any, acc: Set<string>, depth = 0): void {
  if (node == null || depth > 8) return;
  if (typeof node === 'string') { if (COUNTRY_NAMES.has(node.trim())) acc.add(node.trim()); return; }
  if (Array.isArray(node)) { for (const v of node) sweepCountries(v, acc, depth + 1); return; }
  if (typeof node === 'object') { for (const k in node) sweepCountries(node[k], acc, depth + 1); }
}

// Each source: a reader + an extractor mapping the raw JSON to {types, names} groups.
type Extract = { types: string[]; names: string[] }[];
const SOURCES: { get: () => Promise<any>; extract: (d: any) => Extract }[] = [
  { get: getWCChampion,     extract: (d) => [
      { types: ['wc_champion'],    names: (d?.champion ?? []).map((c: any) => c?.name) },
      { types: ['wc_golden_boot'], names: (d?.golden_boot ?? []).map((p: any) => p?.team) },
  ] },
  { get: getWCReachRound,   extract: (d) => [
      { types: ['wc_reach_round'], names: (d?.rounds ?? []).flatMap((r: any) => (r?.teams ?? []).map((t: any) => t?.name)) },
  ] },
  { get: getWCSquad,        extract: (d) => [{ types: ['wc_squad'],  names: (d?.teams ?? []).map((t: any) => t?.name) }] },
  { get: getWCStyles,       extract: (d) => [{ types: ['wc_styles'], names: (d?.teams ?? []).map((t: any) => t?.name) }] },
  { get: getWCForm,         extract: (d) => [{ types: ['wc_form'],   names: (d?.teams ?? []).map((t: any) => t?.name) }] },
  // upcoming.json backs BOTH Today's Predictions and Match Pricing.
  { get: getWCUpcoming,     extract: (d) => [{ types: ['wc_predictions', 'wc_match_pricing'], names: matchNames(d?.matches ?? []) }] },
  { get: getWCDivergence,   extract: (d) => [{ types: ['wc_divergence'], names: matchNames(Array.isArray(d) ? d : d?.matches ?? []) }] },
  { get: getWCBacktest,     extract: (d) => [{ types: ['wc_backtest'],   names: matchNames(d?.matches ?? []) }] },
  // performance bet log: home/away matchup + the side we bet / model pick.
  { get: getWCPerformance,  extract: (d) => [{ types: ['wc_performance'], names: [
      ...matchNames(d?.bet_log ?? []),
      ...(d?.bet_log ?? []).flatMap((b: any) => [b?.pick_team, b?.model_pick_team]),
  ] }] },
  { get: getWCInplayLive,   extract: (d) => [{ types: ['wc_inplay'],     names: matchNames(d?.matches ?? []) }] },
  { get: getWCMilestoneMarks, extract: (d) => [{ types: ['wc_pricetrack'], names: matchNames(d?.matches ?? []) }] },
  { get: getWCMicrofootball, extract: (d) => [{ types: ['wc_microfootball'], names: matchNames(d?.matchups ?? []) }] },
  // schedule.json shape varies (group fixtures + knockout bracket) → recursive sweep.
  { get: getWCSchedule,     extract: (d) => { const s = new Set<string>(); sweepCountries(d, s); return [{ types: ['wc_schedule'], names: [...s] }]; } },
];

export interface CountryData { index: CountryIndex; meta: CountryMetaMap; }
let _cache: Promise<CountryData> | null = null;

function buildMeta(meta: CountryMetaMap, champ: any, reach: any): void {
  const fifa: Record<string, number | null> = {};
  for (const c of champ?.champion ?? []) if (c?.name) fifa[countryKey(c.name)] = c.fifa_rank ?? null;
  const byTeam: Record<string, Record<string, number>> = {};
  for (const r of reach?.rounds ?? []) for (const t of r.teams ?? []) {
    const k = countryKey(t?.name);
    if (k) (byTeam[k] ??= {})[r.key] = t.model_pct;
  }
  const keys = new Set([...Object.keys(fifa), ...Object.keys(byTeam)]);
  for (const k of keys) {
    if (!COUNTRY_NAMES.has(k)) continue;
    meta.set(k, { fifaRank: fifa[k] ?? null, status: byTeam[k] ? statusFor(byTeam[k]) : null });
  }
}

async function buildData(): Promise<CountryData> {
  const index: CountryIndex = new Map();
  const meta: CountryMetaMap = new Map();
  const add = (name: any, types: string[]) => {
    const key = countryKey(name);
    if (!key || !COUNTRY_NAMES.has(key)) return;
    let set = index.get(key);
    if (!set) index.set(key, (set = new Set()));
    for (const t of types) set.add(t);
  };
  // Fetch all sources concurrently; a missing/empty blob (e.g. no live match) just
  // contributes nothing rather than failing the whole index.
  const results = await Promise.all(SOURCES.map(async (src) => {
    try { return { src, data: await src.get() }; }
    catch { return { src, data: null as any }; }
  }));
  for (const { src, data } of results) {
    if (!data) continue;
    for (const grp of src.extract(data)) for (const n of grp.names) add(n, grp.types);
  }
  // Header metadata: FIFA rank from the champion model, progress from reach-round.
  const champ = results.find((r) => r.src.get === getWCChampion)?.data;
  const reach = results.find((r) => r.src.get === getWCReachRound)?.data;
  buildMeta(meta, champ, reach);
  return { index, meta };
}

/** Shared, memoised build (one fetch pass per session). */
export function loadCountryData(): Promise<CountryData> {
  return (_cache ??= buildData());
}

function useCountryData(): CountryData | null {
  const [d, setD] = useState<CountryData | null>(null);
  useEffect(() => {
    let alive = true;
    loadCountryData().then((v) => { if (alive) setD(v); });
    return () => { alive = false; };
  }, []);
  return d;
}

/** Presence index (countryKey → wc_* types), null while loading. */
export function usePredictionIndex(): CountryIndex | null {
  return useCountryData()?.index ?? null;
}

/** Per-country header metadata (FIFA rank + tournament progress), null while loading. */
export function usePredictionMeta(): CountryMetaMap | null {
  return useCountryData()?.meta ?? null;
}
