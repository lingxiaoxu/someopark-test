/**
 * clubIndex — "which soccer artifacts does this club appear in?"
 *
 * The club mirror of countryIndex (TRANSFORM_PLAN 附录 C-31). Powers the ClubName
 * popover: clicking a club lists only the artifacts that ACTUALLY contain it, each a
 * link that scroll-focuses the club in the destination. Built once per session from
 * the same static JSON the artifact views read, then memoised.
 *
 * Two things differ from the World Cup version, and both come from the extra layer this
 * module has — a soccer module holding COMPETITIONS holding matches:
 *
 *   1. Identity is the canonical `club_id`, not a name. The World Cup could key on a
 *      country name because there are 48 of them and they are spelled one way; there
 *      are ~500 clubs here whose names differ per source and per language ("Inter" /
 *      "Internazionale" / "国际米兰"). Sources that carry only a name resolve through a
 *      name→id map built from the model, and a name that does not resolve is dropped
 *      rather than guessed — a wrong club on a popover is worse than a missing one.
 *
 *   2. Status is PER COMPETITION KIND. The World Cup has one ladder for everyone
 *      (group → R16 → QF → SF → final), so one RORDER answered "how far did they get".
 *      A club's season has no single such ladder: a league club is in a title race or a
 *      relegation fight, a Swiss-phase club is chasing a top-8 seed, a cup club is
 *      simply alive or out. Reusing the round ladder here would have labelled every
 *      Premier League club "group stage" forever.
 */
import { useEffect, useState } from 'react';

import {
  getSoccerModel, getSoccerSquad, getSoccerStyles, getSoccerForm, getSoccerUpcoming,
  getSoccerXvMatches, getSoccerSchedule, getSoccerBacktest, getSoccerPerformance,
  getSoccerInplay, getSoccerMilestones, getSoccerSeasonOdds,
} from './soccerApi';

export type ClubIndex = Map<string, Set<string>>;   // club_id → set of soccer_* types

/** Where a club stands in ITS OWN competition — the shape depends on the format. */
export type ClubStatus =
  // league / league_playoffs
  | { kind: 'title'; p: number }         // live in the title race
  | { kind: 'europe'; p: number }        // chasing continental qualification
  | { kind: 'mid' }                      // safe, nothing to play for at either end
  | { kind: 'relegationFight'; p: number }
  | { kind: 'relegated' }                // mathematically down
  // swiss league phase (UCL / UEL / UECL)
  | { kind: 'qualDirect'; p: number }    // on course for the direct-qualification cut
  | { kind: 'qualPlayoff'; p: number }   // on course for the playoff places
  | { kind: 'pendingDraw' }              // the phase has not been drawn yet
  // two-legged cup
  | { kind: 'cupAlive'; p: number }      // still in the bracket, with title odds
  | { kind: 'cupOut' };                  // eliminated

export interface ClubMeta {
  eloRank: number | null;
  league: string | null;                 // the club's home competition key
  kind: string | null;                   // that competition's format
  status: ClubStatus | null;
}
export type ClubMetaMap = Map<string, ClubMeta>;

// Thresholds for reading a season simulation as a STATE. Deliberately loose: the label
// answers "what is this club's season about", not "what will happen", so a 15% title
// chance is a title race and a 15% relegation chance is a relegation fight.
const P_IN_RACE = 0.15;
const P_EUROPE = 0.30;
const P_SETTLED = 0.9999;      // probability at which an outcome is arithmetically fixed
const P_DEAD = 0.0001;

function leagueStatus(o: any): ClubStatus | null {
  const champ = o?.p_champion, top = o?.p_top_n, rel = o?.p_relegation;
  if (champ == null && top == null && rel == null) return null;
  if (rel != null && rel >= P_SETTLED) return { kind: 'relegated' };
  if (champ != null && champ >= P_IN_RACE) return { kind: 'title', p: champ };
  if (rel != null && rel >= P_IN_RACE) return { kind: 'relegationFight', p: rel };
  if (top != null && top >= P_EUROPE) return { kind: 'europe', p: top };
  return { kind: 'mid' };
}

function swissStatus(o: any, state: string | null): ClubStatus | null {
  if (state === 'pending_draw') return { kind: 'pendingDraw' };
  const direct = o?.p_qual_direct, playoff = o?.p_qual_playoff;
  if (direct == null && playoff == null) return null;
  if (direct != null && direct >= P_EUROPE) return { kind: 'qualDirect', p: direct };
  if (playoff != null) return { kind: 'qualPlayoff', p: playoff };
  return null;
}

function cupStatus(o: any, state: string | null): ClubStatus | null {
  if (state === 'pending_bracket' || state === 'pending_draw') return { kind: 'pendingDraw' };
  const champ = o?.p_champion;
  if (champ == null) return null;
  // A cup club with zero title probability is not "unlikely" — it is knocked out. The
  // KO tree only carries clubs still in the bracket, so zero is elimination, and saying
  // "0% to win the cup" about a club already out reads as a forecast when it is a fact.
  return champ <= P_DEAD ? { kind: 'cupOut' } : { kind: 'cupAlive', p: champ };
}

function statusFor(kind: string | null, o: any, state: string | null): ClubStatus | null {
  if (kind === 'swiss_ucl') return swissStatus(o, state);
  if (kind === 'cup_two_leg') return cupStatus(o, state);
  return leagueStatus(o);
}

// ── presence extraction ──────────────────────────────────────────────────────
type Extract = { types: string[]; ids: any[] }[];

/** Club ids out of a list of match-like records, across the shapes our exports use. */
function matchIds(rows: any[]): any[] {
  const out: any[] = [];
  for (const m of rows || []) {
    for (const side of [m?.home, m?.away]) {
      if (side && typeof side === 'object') out.push(side.id ?? side.club_id ?? side.team_id);
      else if (typeof side === 'string') out.push(side);
    }
    if (m?.home_id) out.push(m.home_id);
    if (m?.away_id) out.push(m.away_id);
  }
  return out;
}

const SOURCES: { get: () => Promise<any>; extract: (d: any) => Extract }[] = [
  { get: getSoccerModel, extract: (d) => {
      const leagues = d?.leagues ?? [];
      return [
        { types: ['soccer_season_odds'], ids: leagues.flatMap((l: any) => (l.season_odds ?? []).map((r: any) => r.club_id)) },
        { types: ['soccer_league_table'], ids: leagues.flatMap((l: any) => (l.table ?? []).map((r: any) => r.club_id)) },
        { types: ['soccer_top_scorer'], ids: leagues.flatMap((l: any) => (l.top_scorer ?? []).map((r: any) => r.club_id ?? r?.club?.id)) },
        { types: ['soccer_predictions', 'soccer_match_pricing'], ids: leagues.flatMap((l: any) => matchIds(l.matches ?? [])) },
      ];
  } },
  { get: getSoccerSeasonOdds, extract: (d) => [
      { types: ['soccer_season_odds'], ids: (d?.leagues ?? []).flatMap((l: any) => (l.boards ?? []).flatMap((b: any) => (b.rows ?? []).map((r: any) => r.club_id))) },
  ] },
  { get: getSoccerSquad,   extract: (d) => [{ types: ['soccer_squad'],  ids: (d?.teams ?? []).map((t: any) => t.team_id) }] },
  { get: getSoccerStyles,  extract: (d) => [{ types: ['soccer_styles'], ids: (d?.teams ?? []).map((t: any) => t.team_id) }] },
  { get: getSoccerForm,    extract: (d) => [{ types: ['soccer_form'],   ids: (d?.teams ?? []).map((t: any) => t.team_id) }] },
  // upcoming.json backs BOTH Today's Predictions and Match Pricing.
  { get: getSoccerUpcoming,  extract: (d) => [{ types: ['soccer_predictions', 'soccer_match_pricing'], ids: matchIds(d?.matches ?? []) }] },
  { get: getSoccerXvMatches, extract: (d) => [{ types: ['soccer_divergence'], ids: matchIds(Array.isArray(d) ? d : d?.matches ?? []) }] },
  { get: getSoccerSchedule,  extract: (d) => [{ types: ['soccer_schedule'],   ids: matchIds(d?.matches ?? []) }] },
  { get: getSoccerBacktest,  extract: (d) => [{ types: ['soccer_backtest'],   ids: matchIds(d?.matches ?? []) }] },
  { get: getSoccerInplay,    extract: (d) => [{ types: ['soccer_inplay'],     ids: matchIds(d?.matches ?? []) }] },
  { get: getSoccerMilestones, extract: (d) => [{ types: ['soccer_pricetrack'], ids: matchIds(d?.matches ?? []) }] },
  { get: getSoccerPerformance, extract: (d) => [{ types: ['soccer_performance'], ids: matchIds(d?.bet_log ?? []) }] },
];

export interface ClubData { index: ClubIndex; meta: ClubMetaMap; byName: Map<string, string>; }
let _cache: Promise<ClubData> | null = null;

/** Lowercased, accent-folded name → club_id, for sources that carry no id. */
function nameKey(s: any): string {
  return String(s ?? '').normalize('NFKD').replace(/[̀-ͯ]/g, '').trim().toLowerCase();
}

function buildMeta(meta: ClubMetaMap, byName: Map<string, string>, model: any): void {
  for (const lg of model?.leagues ?? []) {
    const state = lg?.odds_state ?? null;
    for (const o of lg?.season_odds ?? []) {
      const id = o?.club_id;
      if (!id) continue;
      meta.set(id, {
        eloRank: o?.elo_rank ?? null,
        league: lg?.league ?? null,
        kind: lg?.kind ?? null,
        status: statusFor(lg?.kind ?? null, o, state),
      });
      for (const n of [o?.name, o?.zh]) { const k = nameKey(n); if (k) byName.set(k, id); }
    }
  }
}

async function buildData(): Promise<ClubData> {
  const index: ClubIndex = new Map();
  const meta: ClubMetaMap = new Map();
  const byName = new Map<string, string>();

  const results = await Promise.all(SOURCES.map(async (src) => {
    try { return { src, data: await src.get() }; }
    catch { return { src, data: null as any }; }
  }));

  // Meta first: it also builds the name→id map the presence pass needs.
  const model = results.find((r) => r.src.get === getSoccerModel)?.data;
  buildMeta(meta, byName, model);

  const add = (raw: any, types: string[]) => {
    if (raw == null) return;
    let id = typeof raw === 'string' ? raw : String(raw);
    if (!meta.has(id)) {
      const viaName = byName.get(nameKey(raw));
      if (!viaName) return;        // unresolvable → dropped, never guessed
      id = viaName;
    }
    let set = index.get(id);
    if (!set) index.set(id, (set = new Set()));
    for (const t of types) set.add(t);
  };
  for (const { src, data } of results) {
    if (!data) continue;
    for (const grp of src.extract(data)) for (const c of grp.ids) add(c, grp.types);
  }
  return { index, meta, byName };
}

/** Shared, memoised build (one fetch pass per session). */
export function loadClubData(): Promise<ClubData> {
  return (_cache ??= buildData());
}

function useClubData(): ClubData | null {
  const [d, setD] = useState<ClubData | null>(null);
  useEffect(() => {
    let alive = true;
    loadClubData().then((v) => { if (alive) setD(v); });
    return () => { alive = false; };
  }, []);
  return d;
}

/** Presence index (club_id → soccer_* types), null while loading. */
export function useSoccerIndex(): ClubIndex | null {
  return useClubData()?.index ?? null;
}

/** Per-club header metadata (Elo rank, home competition, season status), null while loading. */
export function useSoccerMeta(): ClubMetaMap | null {
  return useClubData()?.meta ?? null;
}
