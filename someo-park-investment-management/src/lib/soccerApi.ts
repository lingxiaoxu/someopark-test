/**
 * soccerApi — data access for the Club Soccer Prediction Market family.
 * Mirrors the macro family's convention (separate fetcher module — lib/api.ts is
 * untouched): static JSON under /data/soccer/*.json produced by
 * prediction_market_soccer exporters + scripts/sync_soccer_data.mjs, fetched with
 * cache:'no-store' (files are regenerated continuously). Live files (upcoming /
 * inplay / schedule) also take a ?_=Date.now() cache-buster, same as the WC getters.
 */
import { API_BASE, apiHeaders } from './api';

/** Fetch one /data/soccer/<file> JSON. Defensive parse: Python's json module can
 * emit bare NaN/Infinity, which strict JSON.parse rejects — sanitize to null first
 * so one bad float never blanks a whole view (macroApi has the same belt). */
async function getSoccerJson<T = any>(file: string, bust = false): Promise<T> {
  const q = bust ? `?_=${Date.now()}` : '';
  const res = await fetch(`${API_BASE}/data/soccer/${file}${q}`, { headers: apiHeaders(), cache: 'no-store' });
  if (!res.ok) throw new Error(`API error: ${res.status} ${res.statusText}`);
  const text = await res.text();
  return JSON.parse(text.replace(/(?<![\w"'-])-?(?:NaN|Infinity)(?![\w"])/g, 'null'));
}

// ── shared shapes (loose on purpose — the backend contract is the truth) ──────

export type SoccerLeagueKind = 'league' | 'league_playoffs' | 'swiss_ucl' | 'cup_two_leg';

/** Backend-computed market-capability object per match (§3.0). The frontend reads
 * ONLY this (never `if (league === 'ucl')` style special-casing). */
export type SoccerCaps = {
  stage?: string | null;
  advance?: boolean;
  two_leg?: boolean;
  leg?: number | null;
  agg?: string | null;
  et_then_pens?: boolean;
  neutral?: boolean;
};

export type SoccerClubRef = { id: string | null; name: string; zh?: string };

export type SoccerTableRow = {
  club_id: string; name: string; zh?: string;
  pts: number; gd: number; gf: number; played: number;
  /** Zoned competitions (Argentina Apertura/Clausura = two 15-club zones). */
  zone?: string | null;
};

export type SoccerSeasonOddsRow = {
  club_id: string; name: string; zh?: string; logo?: string | null;
  elo_rank?: number | null;
  p_champion?: number | null; p_top_n?: number | null; p_relegation?: number | null;
  p_last?: number | null; p_qual_direct?: number | null; p_qual_playoff?: number | null;
  e_points?: number | null; e_rank?: number | null; rating?: number | null;
  kalshi_champ_c?: number | null; poly_champ_c?: number | null;
};

export type SoccerModelLeague = {
  league: string; name: string; zh?: string; kind: SoccerLeagueKind;
  n_teams: number; n_remaining: number;
  top_n?: number; releg_direct?: number; releg_playoff?: number;
  /** 'ok' | 'pending_draw' | 'pending_bracket' — when not 'ok' every season-odds
   *  probability is null (unknown), which is NOT the same as 0%. */
  odds_state?: 'ok' | 'pending_draw' | 'pending_bracket';
  zones?: string[] | null;
  table?: SoccerTableRow[];
  season_odds?: SoccerSeasonOddsRow[];
  matches?: {
    home_id: string; home: string; away_id: string; away: string;
    p_home: number; p_draw: number; p_away: number;
    p_over_2_5?: number | null; p_btts?: number | null;
    knockout?: boolean; p_home_advance?: number | null;
  }[];
};

export type SoccerModel = {
  meta?: { run_ts?: string; code_version?: string; n_sims?: number; model_notes?: string[] };
  leagues?: SoccerModelLeague[];
};

export type SoccerBoardRow = {
  club_id: string; name: string; zh?: string; logo?: string | null;
  model_pct?: number | null; model_c?: number | null;
  kalshi_c?: number | null; poly_c?: number | null; edge_vs_kalshi?: number | null;
};

export type SoccerSeasonOdds = {
  as_of?: string; note?: string;
  leagues?: {
    league: string; name: string; zh?: string; kind: SoccerLeagueKind;
    boards?: { family: string; label?: string; kalshi_series?: string | null; rows?: SoccerBoardRow[] }[];
  }[];
};

export type SoccerVenueSide = {
  ask?: number | null; bid?: number | null;
  ask_c?: number | null; bid_c?: number | null; mid_c?: number | null;
};
export type SoccerVenueQuote = {
  home?: SoccerVenueSide; draw?: SoccerVenueSide; away?: SoccerVenueSide;
  devig?: { home: number; draw?: number; away: number } | null;
} | null;

export type SoccerDecision = {
  bet: boolean; side?: string | null; venue?: string | null;
  price_cents?: number | null; model_prob?: number | null; net_edge?: number | null;
  stake_usd?: number | null; count?: number | null; capped_notional_usd?: number | null;
  confidence_k?: number | null; knockout?: boolean;
} | null;

export type SoccerAdvanceBlock = {
  model: { home: number; away: number; cents?: { home: number; away: number } } | null;
  kalshi?: SoccerVenueQuote;
  poly_us?: SoccerVenueQuote;
  edge?: { best?: { side: string; venue: string; ask?: number | null; net_edge: number; tradable: boolean } | null } | null;
  decision?: SoccerDecision;
  lock_arb?: any;
} | null;

export type SoccerUpcomingMatch = {
  fixture_id?: number | string;
  league?: string; league_zh?: string;
  kickoff: string; et?: string | null; et_date?: string | null; round?: string;
  status?: string; tentative?: boolean;
  home: SoccerClubRef; away: SoccerClubRef;
  model: {
    home: number; draw: number; away: number;
    over_2_5?: number | null; btts?: number | null;
    cents?: { home: number; draw: number; away: number };
  } | null;
  knockout?: boolean;
  caps?: SoccerCaps;
  motivation?: any;
  form?: { home?: number | null; away?: number | null } | null;
  book_devig?: { home: number; draw: number; away: number } | null;
  kalshi?: SoccerVenueQuote;
  poly_us?: SoccerVenueQuote;
  edge?: { vs_book?: any; vs_kalshi?: any; vs_poly_us?: any; best?: { side: string; venue: string; ask?: number | null; net_edge: number; tradable: boolean } | null } | null;
  decision?: SoccerDecision;
  lock_arb?: any;
  advance?: SoccerAdvanceBlock;
};

export type SoccerUpcoming = {
  as_of?: string; n?: number; note?: string;
  matches?: SoccerUpcomingMatch[];
  recent_finished?: {
    league?: string; league_zh?: string;
    home: SoccerClubRef; away: SoccerClubRef;
    score?: string; status?: string; finished?: boolean; result?: string;
    kickoff?: string; et?: string | null; round?: string;
  }[];
};

export type SoccerInplay = { ts?: string; n_live?: number; matches?: any[] };
export type SoccerSchedule = { matches?: any[] };

// ── fetchers ─────────────────────────────────────────────────────────────────
export const getSoccerModel = () => getSoccerJson<SoccerModel>('soccer_model.json');
export const getSoccerSeasonOdds = () => getSoccerJson<SoccerSeasonOdds>('season_odds.json');
export const getSoccerUpcoming = () => getSoccerJson<SoccerUpcoming>('upcoming.json', true);
export const getSoccerInplay = () => getSoccerJson<SoccerInplay>('inplay_live.json', true);
export const getSoccerSchedule = () => getSoccerJson<SoccerSchedule>('schedule.json', true);

// ── quality / intel / market surfaces (附录 C parity — files land per backend
// phase; every consumer renders a clean empty state until its file exists) ─────
export const getSoccerSquad = () => getSoccerJson<any>('squad.json');
export const getSoccerStyles = () => getSoccerJson<any>('team_styles.json');
export const getSoccerForm = () => getSoccerJson<any>('form.json');
export const getSoccerXvMatches = () => getSoccerJson<any>('xv_matches.json', true);
export const getSoccerXvChampion = () => getSoccerJson<any>('xv_champion.json', true);
export const getSoccerMilestones = () => getSoccerJson<any>('milestone_marks.json', true);
export const getSoccerPerformance = () => getSoccerJson<any>('performance_report.json');
export const getSoccerOos = () => getSoccerJson<any>('oos_report.json');
export const getSoccerBacktest = () => getSoccerJson<any>('backtest.json');
// The 180-set global sweep was retired (it fitted one merged-prior model that
// production never prices with). Its replacement selects per competition on a
// time split — same card, honest source.
export const getSoccerParams = () => getSoccerJson<any>('param_select_club.json');
export const getSoccerRisk = () => getSoccerJson<any>('risk_report.json');
export const getSoccerOverview = () => getSoccerJson<any>('frontend_overview.json', true);

/** Absolute URL for a server-relative soccer data file (e.g. the report PDFs). */
export const soccerFileUrl = (file: string) => `${API_BASE}/data/soccer/${file}`;
