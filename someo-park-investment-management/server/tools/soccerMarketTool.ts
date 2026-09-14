// server/tools/soccerMarketTool.ts
// Club Soccer prediction-market data (12 competitions: EPL / La Liga / Serie A /
// Bundesliga / Ligue 1 / UCL / UEL / UECL / Libertadores / Sudamericana / Brasileirão /
// Liga Profesional). Data source: public/data/soccer/*.json — the SAME files the Club
// Soccer dashboard reads, produced by prediction_market_soccer/ops/* and synced by
// scripts/sync_soccer_data.mjs.
//
// Structurally a sibling of predictionMarketTool (five isomorphic tools + a grounding
// loader, 附录 C-32) with ONE addition the World Cup never needed: every view takes a
// `league`. The soccer system is a league→match hierarchy, so an unscoped answer would
// mean 12 competitions of rows — the filter is what keeps a reply about the Premier
// League from being 80% Conference League.

import { readJsonFile } from '../utils/fileUtils.js'
import path from 'path'
import { fileURLToPath } from 'url'
import type { AgentTool } from './index.js'
import {
  SOCCER_LEAGUES, SOCCER_LEAGUE_IDS, resolveSoccerClub, soccerClubMeta, soccerLeagueDef,
} from './soccerTriggers.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const dataDir = path.resolve(__dirname, '..', '..', 'public', 'data', 'soccer')

// view → file + what it holds. Mirrors predictionMarketTool's VIEWS.
function snapshotMeta(data: any) {
  return { as_of: data?.as_of, ts: data?.ts, source_as_of: data?.source_as_of,
    data_status: data?.data_status, operations: data?.operations, sources: data?.sources,
    scan_error: data?.scan_error }
}

const VIEWS: Record<string, { file: string; about: string }> = {
  season_odds:     { file: 'soccer_model.json',          about: 'the season board per competition: model p_champion / p_top_n (Europe places) / p_relegation / expected points+rank from the season Monte-Carlo, PLUS the tradable boards from season_odds.json (model¢ vs Kalshi¢ vs Polymarket¢ and our edge, per champion / top_n / relegation family). `odds_state` is "pending_draw" for a competition whose bracket is not drawn yet — every probability is then null, which means UNKNOWN, not 0%' },
  league_table:    { file: 'soccer_model.json',          about: 'the current standings (points / goal difference / goals for / played, and `zone` for zoned competitions like the Argentine Apertura); the live table the season Monte-Carlo starts from' },
  top_scorer:      { file: 'soccer_model.json',          about: 'top-scorer (金靴/得点王) race per league: p_top_scorer, expected goals for the season, goals+appearances so far, scoring rate vs the EA FC 26 talent prior, and the player\'s club. Only league competitions run a top-scorer race' },
  predictions:     { file: 'upcoming.json',              about: 'upcoming + today\'s matches: our model 3-way (home/draw/away) + over-2.5 + BTTS, the de-vig book, real Kalshi / Polymarket US asks, edge per side, and the DECISION (bet or pass, side, venue, price, stake). Matches with `caps.advance` also carry an `advance` block = the 2-way "who advances" market (two-legged ties and knockout rounds, including extra time + penalties); `caps.agg` is the aggregate score after the first leg' },
  match_pricing:   { file: 'upcoming.json',              about: 'same data as `predictions` — use this view name when the question is about how ONE specific fixture is priced' },
  schedule:        { file: 'schedule.json',              about: 'the fixture calendar (7 days back, 30 days forward): kickoff time (UTC + ET), round, venue, status, and for finished matches the score, result and scorers' },
  inplay:          { file: 'inplay_live.json',           about: 'LIVE matches right now: live 3-way, xG, score, red cards, venue prices, in-play opportunities (entry / manage / event) and the draw-protection `hedge` for a held pre-match position. Empty when nothing is being played' },
  inplay_advance:  { file: 'inplay_live_advance.json',   about: 'LIVE two-legged / knockout ties, 2-way "WHO ADVANCES" (no draw; regulation → extra time → penalty shootout), with the advance-market prices and a 2-state hedge. Empty when no such tie is live' },
  form:            { file: 'form.json',                  about: 'recent-form index per club (time-weighted goal difference, form_z), the input the match model blends' },
  squad:           { file: 'squad.json',                 about: 'squad quality per club: minutes-weighted player rating with a league-strength adjustment, goals-against per 90, EA FC 26 talent overall, and the club\'s top players this season' },
  styles:          { file: 'team_styles.json',           about: 'the style taxonomy: 10 style codes (possession / direct / high-press / low-block / dominant-attack / clinical / high-volume / set-piece / balanced / contained); each club carries 1–2 styles plus raw metrics (possession, xG, directness). Descriptive scouting aid, not a prediction' },
  divergence:      { file: 'xv_matches.json',            about: 'per-match model-vs-market divergence: our 3-way against the de-vigged Kalshi / Polymarket / bookmaker line, with the biggest-disagreement side' },
  champion_divergence: { file: 'xv_champion.json',       about: 'season-champion model-vs-market divergence: p_champion against the Shin de-vigged Kalshi champion book, per competition' },
  performance:     { file: 'performance_report.json',    about: 'the canonical full-model SMART TIMING ledger: pre-match plus in-play positions with recorded smart exits, position P&L before fees and three cumulative curves. The dashboard, price track and PnL PDF use identical records. Historical PIT evidence is disclosed separately; Kalshi demo fills are only an execution subset. Probability diagnostics are not strategy win rates. For every ledger row use get_soccer_track_record' },
  calibration:     { file: 'oos_report.json',            about: 'out-of-sample reliability: Brier + CI, log-loss, predicted vs observed draw and home rates, predicted vs observed goal totals — the directional health check' },
  backtest:        { file: 'backtest.json',              about: 'model vs market vs uniform Brier over every settled match, the trade-grade verdict, the blend curve, and the most recent settled matches with the model\'s pick and probability' },
  params:          { file: 'param_select_club.json',     about: 'the parameter selection: three candidate knob sets (the club default, the World Cup values, and a refit) scored out-of-sample on a held-out split, with the winner, whether it was adopted, and the per-competition Brier of the test split. A candidate is only adopted when it beats the incumbent out-of-sample, so `adopted: false` means the incumbent held — not that the search failed' },
  bracket:         { file: 'bracket.json',                about: 'the drawn knockout brackets: each cup round with its ties, both legs, aggregate score where a first leg has been played, and each side\'s probability of advancing. Only rounds that have actually been DRAWN appear — an undrawn round is absent rather than shown with placeholder teams' },
  pricetrack:      { file: 'milestone_marks.json',       about: 'the same canonical smart-timing strategy records as performance and the PnL PDF, enriched with observed market quotes at PRE / 15\' / 30\' / HT / 60\' / 75\' / FT. Quotes are per-contract cents; realized P&L and the PRE/in-play/combined cumulative totals are position cents. price_only_matches have no recorded strategy position and are outside the model ledger' },
  calibration_gate:{ file: 'calibration.json',           about: 'the calibration mapping actually applied: pooled temperature + draw boost, and the PER-COMPETITION gate (§3.5). A league with fewer than 30 settled matches is `cold_start` and falls back to the pooled mapping ("applies":"pooled")' },
  risk:            { file: 'risk_report.json',           about: 'pre-trade gates, venue balances (Kalshi demo/prod, Polymarket US), the $1 hard order cap, open exposure, API request budget, the calibration gate and the kill switch' },
  overview:        { file: 'frontend_overview.json',     about: 'system overview: headline gate state, calibration summary, the 12 competitions with team counts / matches remaining / current leader, the Kalshi series in play, and the model notes' },
}

const VIEW_NAMES = Object.keys(VIEWS)

// ── loading + shaping helpers ────────────────────────────────────────────────
async function _load(file: string): Promise<any> {
  try { return await readJsonFile(path.resolve(dataDir, file)) } catch { return null }
}

/** Resolve whatever the model passed as `league` — an id, an English/中文/日本語 name,
 *  or nothing — to a league id. Returns null for "all competitions". */
/** Lower-case and strip diacritics, so "Primera División" and "Brasileirão" match the
 *  unaccented spellings people and venues actually type. CJK is unaffected by NFD. */
function fold(value: string): string {
  return value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').trim().toLowerCase()
}

function resolveLeague(q?: string | null): string | null {
  if (!q) return null
  const s = fold(q.toString())
  if (SOCCER_LEAGUE_IDS.includes(s)) return s
  for (const lg of SOCCER_LEAGUES) {
    if (fold(lg.label) === s) return lg.id
    if (lg.aliases.some((a) => fold(a) === s)) return lg.id
  }
  // Substring fallback, longest alias wins, and a tie between two competitions answers
  // nothing. 'primera division' (laliga) and 'serie a' (seriea) are names other countries
  // use too, so first-match-wins silently returned La Liga for "Argentina Primera Division"
  // and Italy's Serie A for "Brazilian Serie A" — wrong tables, wrong odds, no error shown.
  // Longest-match lets the specific spelling win; refusing a tie turns a wrong answer into
  // an explicit "Unknown competition", which the caller can see.
  let best: { id: string; len: number } | null = null
  let ambiguous = false
  for (const lg of SOCCER_LEAGUES) {
    for (const alias of lg.aliases) {
      const a = fold(alias)
      if (!a || !s.includes(a)) continue
      if (!best || a.length > best.len) {
        best = { id: lg.id, len: a.length }
        ambiguous = false
      } else if (a.length === best.len && lg.id !== best.id) {
        ambiguous = true
      }
    }
  }
  return best && !ambiguous ? best.id : null
}

/** Club query → the set of club_ids it can mean (a query may be ambiguous, e.g. two
 *  clubs called "Nacional"); empty set means "no filter". */
function clubIds(q?: string | null): Set<string> {
  return new Set(q ? resolveSoccerClub(q.toString()) : [])
}

const inClub = (ids: Set<string>, ...candidates: any[]) =>
  ids.size === 0 || candidates.some((c) => c && ids.has(String(c)))

/** Row budget. A named competition can afford a full table; "all 12" cannot. */
const budget = (top: number | undefined, league: string | null, wide: number, narrow: number) =>
  typeof top === 'number' && top > 0 ? top : (league ? wide : narrow)

function leagueSlice(data: any, league: string | null): any[] {
  const rows = (data?.leagues ?? []) as any[]
  return league ? rows.filter((l) => l.league === league) : rows
}

const matchClub = (m: any, ids: Set<string>) =>
  inClub(ids, m?.home?.id, m?.away?.id, m?.home_id, m?.away_id)

// Aggregate only recorded smart-timing position returns. Never substitute the
// terminal winner or HOLD P&L, and never rewrite stored cumulative columns.
export function smartTimingScopeSummary(records: any[]) {
  const track = (key: string, eligible: (row: any) => boolean) => {
    const legs = records.filter(eligible)
    const values = legs.map((row) => row[key]).filter((v) => typeof v === 'number' && Number.isFinite(v))
    const cents = values.reduce((sum, v) => sum + Math.round(v * 10), 0) / 10
    return { n_legs: legs.length, unknown: legs.length - values.length, profit: values.filter((v) => v > 0).length,
      loss: values.filter((v) => v < 0).length, flat: values.filter((v) => v === 0).length,
      pnl_cents: values.length === legs.length ? cents : null, pnl_usd: values.length === legs.length ? cents / 100 : null }
  }
  const pre = track('realized_pnl_cents', (row) => !!row.bet)
  const inplay = track('inplay_pnl_cents', (row) => !!row.inplay_side)
  const cents = pre.pnl_cents == null || inplay.pnl_cents == null ? null : Math.round((pre.pnl_cents + inplay.pnl_cents) * 10) / 10
  return { n_matches: records.length, n_legs: pre.n_legs + inplay.n_legs, pre, inplay,
    combined: { n_legs: pre.n_legs + inplay.n_legs, profit: pre.profit + inplay.profit,
      loss: pre.loss + inplay.loss, flat: pre.flat + inplay.flat, unknown: pre.unknown + inplay.unknown,
      pnl_cents: cents, pnl_usd: cents == null ? null : cents / 100 } }
}

function ledgerMeta(data: any) {
  const ledger = data.strategy_ledger
  return { ledger_id: ledger?.ledger_id, ledger_as_of: ledger?.as_of,
    full_ledger_summary: ledger?.summary, pnl_basis: ledger?.pnl_basis ?? data.pnl_basis,
    cumulative_scope: 'Stored cumulative columns always refer to the full canonical ledger, including when rows are filtered.' }
}

// ── tool 1: the view reader (mirrors get_prediction_market) ──────────────────
export const soccerMarketTool: AgentTool = {
  definition: {
    name: 'get_soccer_market',
    description:
      'Read Club Soccer prediction-market data — the 12 competitions we price: Premier League, ' +
      'La Liga, Serie A, Bundesliga, Ligue 1, UEFA Champions League, Europa League, Conference ' +
      'League, Copa Libertadores, Copa Sudamericana, Brasileirão and the Argentine Liga ' +
      'Profesional. This backs EVERY dashboard view, so use it for ANY club-football question: ' +
      'the league table (积分榜/順位表), title / top-four / relegation odds, the top-scorer race ' +
      '(金靴/得点王), how a specific fixture is priced, today\'s predictions and value bets, live ' +
      'in-play matches, recent form, squad strength, playing styles, model-vs-market divergence, ' +
      'our accuracy / Brier / calibration, the bet log and P&L, venue balances and risk gates, or ' +
      'the system overview. ALWAYS pass `league` when the question is about one competition — ' +
      'otherwise you get a thin slice of all twelve. Probabilities are 0-1; ¢ are contract cents ' +
      '(a binary contract settles 100¢, so ¢ ≈ implied probability, but venue prices carry vig — ' +
      'the venue\'s real implied probability is the DE-VIGGED price). Note this is CLUB football; ' +
      'World Cup / national-team questions belong to get_prediction_market instead.',
    input_schema: {
      type: 'object',
      properties: {
        view: {
          type: 'string',
          enum: VIEW_NAMES,
          description: 'Which dataset: ' +
            Object.entries(VIEWS).map(([k, v]) => `"${k}" = ${v.about}`).join('; '),
        },
        league: {
          type: 'string',
          description: 'Restrict to one competition. Accepts the id (' + SOCCER_LEAGUE_IDS.join('/') +
            ') or a name in any UI language ("Premier League", "英超", "セリエA", "Ligue des champions").',
        },
        club: { type: 'string', description: 'Optional filter — one club, by English / 中文 / 日本語 name or club_id ("Arsenal", "阿森纳", "アーセナル", "arsenal").' },
        top: { type: 'number', description: 'Rows per competition (default: a wider slice when `league` is given, a narrow one when it is not).' },
      },
      required: ['view'],
    },
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ view, league, club, top }) {
    const spec = VIEWS[view]
    if (!spec) return { error: `Unknown view "${view}". Valid: ${VIEW_NAMES.join(', ')}` }

    const lg = resolveLeague(league)
    if (league && !lg) {
      return { error: `Unknown competition "${league}". Valid ids: ${SOCCER_LEAGUE_IDS.join(', ')}.` }
    }
    const ids = clubIds(club)
    if (club && ids.size === 0) return { error: `Club "${club}" not found in the 12 competitions we cover.` }

    const data = await _load(spec.file)
    if (!data) {
      return view === 'params'
        ? { available: false, message: 'The parameter sweep has not been generated yet — it is deliberately withheld for the first weeks of the season (sample discipline), so the model runs on hand-set defaults. Nothing is wrong.' }
        : { error: `${spec.file} not available yet (run the soccer exporter + npm run sync:soccer).` }
    }
    const scope = { league: lg, club: club ? [...ids] : undefined }

    // ── soccer_model.json: three different slices of the same per-league object ──
    if (view === 'season_odds') {
      const n = budget(top, lg, 30, 8)
      const boards = await _load('season_odds.json')
      const byId = new Map(leagueSlice(boards, lg).map((l: any) => [l.league, l]))
      return {
        ...snapshotMeta(data), meta: data.meta, scope,
        leagues: leagueSlice(data, lg).map((l: any) => ({
          league: l.league, name: l.name, zh: l.zh, kind: l.kind, odds_state: l.odds_state,
          odds_family_states: l.odds_family_states, availability_reason: l.availability_reason,
          coverage: l.coverage, odds_notes: l.odds_notes, odds_notes_i18n: l.odds_notes_i18n, source_as_of: l.source_as_of,
          top_n: l.top_n, releg_direct: l.releg_direct, n_remaining: l.n_remaining,
          model: (l.season_odds ?? []).filter((r: any) => inClub(ids, r.club_id)).slice(0, n),
          // venue boards: model¢ vs Kalshi¢ vs Poly¢ + edge, per family
          boards: (byId.get(l.league)?.boards ?? []).map((b: any) => ({
            family: b.family, label: b.label, kalshi_series: b.kalshi_series,
            state: b.state, availability_reason: b.availability_reason,
            rows: (b.rows ?? []).filter((r: any) => inClub(ids, r.club_id)).slice(0, n),
          })),
        })),
      }
    }
    if (view === 'league_table') {
      const n = budget(top, lg, 30, 10)
      return {
        ...snapshotMeta(data), meta: data.meta, scope,
        leagues: leagueSlice(data, lg).map((l: any) => ({
          league: l.league, name: l.name, zh: l.zh, kind: l.kind, zones: l.zones,
          n_teams: l.n_teams, n_remaining: l.n_remaining,
          table: (l.table ?? []).filter((r: any) => inClub(ids, r.club_id)).slice(0, n),
        })),
      }
    }
    if (view === 'top_scorer') {
      const n = budget(top, lg, 15, 5)
      const rows = leagueSlice(data, lg)
        .map((l: any) => ({
          league: l.league, name: l.name, zh: l.zh,
          top_scorer: (l.top_scorer ?? []).filter((p: any) => inClub(ids, p.club_id)).slice(0, n),
        }))
        .filter((l: any) => l.top_scorer.length)
      return {
        ...snapshotMeta(data), meta: data.meta, scope, leagues: rows,
        note: rows.length ? undefined : 'No top-scorer race for this scope — cup and Swiss-format competitions do not run one.',
      }
    }

    // ── per-match files ──
    if (view === 'predictions' || view === 'match_pricing') {
      const n = budget(top, lg, 20, 8)
      let matches = (data.matches ?? []) as any[]
      if (lg) matches = matches.filter((m) => m.league === lg)
      if (ids.size) matches = matches.filter((m) => matchClub(m, ids))
      return { ...snapshotMeta(data), as_of: data.as_of, note: data.note, scope, n_total: matches.length, matches: matches.slice(0, n) }
    }
    if (view === 'schedule') {
      const n = budget(top, lg, 40, 15)
      let matches = (data.matches ?? []) as any[]
      if (lg) matches = matches.filter((m) => m.league === lg)
      if (ids.size) matches = matches.filter((m) => matchClub(m, ids))
      return { ...snapshotMeta(data), as_of: data.as_of, window: data.window, scope, n_total: matches.length, matches: matches.slice(0, n) }
    }
    if (view === 'inplay' || view === 'inplay_advance') {
      let matches = (data.matches ?? []) as any[]
      // inplay_live_advance.json rows carry no `league` field — filter those by club only.
      if (lg) matches = matches.filter((m) => m.league === lg)
      if (ids.size) matches = matches.filter((m) => matchClub(m, ids))
      if (!matches.length) {
        return { ...snapshotMeta(data), ts: data.ts, n_live: 0, matches: [], scope,
          message: data.scan_error || !data.ts || Date.now() - Date.parse(data.ts) > 900000 || ['degraded', 'unavailable'].includes(data.data_status?.state)
            ? 'The in-play snapshot is incomplete or unavailable; no reliable conclusion about live matches or opportunities can be drawn.'
            : view === 'inplay'
            ? 'No match is live in this scope right now, so there are no in-play signals — they only exist while a match is being played. Use view="predictions" for upcoming fixtures and kickoff times.'
            : 'No two-legged / knockout tie is live in this scope right now, so there is no "who advances" in-play view. Upcoming ties carry an `advance` block in view="predictions".' }
      }
      return { ...snapshotMeta(data), ts: data.ts, n_live: matches.length, scope, matches }
    }
    if (view === 'divergence') {
      const n = budget(top, lg, 20, 10)
      let matches = (data.matches ?? []) as any[]
      if (lg) matches = matches.filter((m) => m.league === lg)
      if (ids.size) matches = matches.filter((m) => matchClub(m, ids))
      return { ...snapshotMeta(data), as_of: data.as_of, note: data.note, scope, n_total: matches.length, matches: matches.slice(0, n) }
    }
    if (view === 'champion_divergence') {
      const n = budget(top, lg, 25, 8)
      return {
        ...snapshotMeta(data), as_of: data.as_of, note: data.note, scope,
        leagues: leagueSlice(data, lg).map((l: any) => ({
          ...l, rows: (l.rows ?? []).filter((r: any) => inClub(ids, r.club_id)).slice(0, n),
        })),
      }
    }
    if (view === 'pricetrack') {
      if (!data.strategy_ledger?.records) return { ...snapshotMeta(data), message: 'The canonical smart-timing ledger is unavailable.' }
      const n = budget(top, lg, 15, 8)
      let matches = (data.matches ?? []) as any[]
      if (lg) matches = matches.filter((m) => (m.strategy_record?.league ?? m.league) === lg)
      if (ids.size) matches = matches.filter((m) => matchClub(m, ids))
      return { ...snapshotMeta(data), ...ledgerMeta(data), milestones: data.milestones, note: data.note, scope,
        scope_summary: smartTimingScopeSummary(matches.map((m) => m.strategy_record).filter(Boolean)),
        n_total: matches.length, matches: matches.slice(-n),
        full_price_only_count: (data.price_only_matches ?? []).length }
    }
    if (view === 'backtest') {
      // The per-match settled list is 635 rows of noise next to the headline metrics.
      const n = budget(top, lg, 25, 10)
      let matches = (data.matches ?? []) as any[]
      if (lg) matches = matches.filter((m) => m.league === lg)
      return { ...data, scope, n_matches: matches.length, matches: matches.slice(-n).reverse() }
    }

    // ── per-club files ──
    if (view === 'form' || view === 'squad' || view === 'styles') {
      const n = budget(top, lg, 30, 12)
      let teams = (data.teams ?? []) as any[]
      if (lg) teams = teams.filter((t) => t.league === lg || (t.leagues ?? []).includes(lg))
      if (ids.size) teams = teams.filter((t) => inClub(ids, t.team_id))
      const head: any = { ts: data.ts, note: data.note, note_key: data.note_key, scope, n_total: teams.length }
      if (view === 'styles') head.styles = data.styles
      return { ...head, teams: teams.slice(0, n) }
    }

    // ── whole-system files: no league dimension, return as-is (bet log trimmed) ──
    if (view === 'performance') {
      const n = budget(top, lg, 15, 15)
      if (!data.strategy_ledger?.records) return { ...snapshotMeta(data), message: 'The canonical smart-timing ledger is unavailable.' }
      let log = data.strategy_ledger.records as any[]
      if (lg) log = log.filter((b) => b.league === lg)
      if (ids.size) log = log.filter((b) => matchClub(b, ids))
      return { ...snapshotMeta(data), ...ledgerMeta(data), scope, scope_summary: smartTimingScopeSummary(log),
        n_records: log.length, records: log.slice(-n), evidence_summary: data.evidence_summary,
        probability_diagnostics: { brier: data.brier, calibrated_brier: data.calibrated_brier,
          model_pred_accuracy: data.model_pred_accuracy, trade_grade: data.trade_grade },
        demo_execution_is_subset: true }
    }
    return { ...data, scope }
  },
}

// ── tool 2: one club across every view (mirrors get_wc_team) ─────────────────
export const soccerClubTool: AgentTool = {
  definition: {
    name: 'get_soccer_club',
    description:
      'Deep-dive on ONE club across every model view: its league table position, season odds ' +
      '(title / top-four / relegation, expected points), recent form, squad strength, playing ' +
      'style, its top-scorer candidates, and its upcoming + live matches with model prices, venue ' +
      'asks, edge and the bet decision. Accepts the club\'s English name, 中文 name, 日本語 name or ' +
      'club_id ("Arsenal", "阿森纳", "アーセナル", "arsenal"). Use this for "how is X doing", ' +
      '"X\'s form", "X\'s squad", "when does X play". Mirrors get_wc_team / get_pair_stats.',
    input_schema: {
      type: 'object',
      properties: { club: { type: 'string', description: 'Club name (English / 中文 / 日本語) or club_id.' } },
      required: ['club'],
    },
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ club }) {
    const ids = clubIds(club)
    if (!ids.size) return { error: `Club "${club}" not found in the 12 competitions we cover.` }
    if (ids.size > 1) {
      return { ambiguous: [...ids].map((id) => soccerClubMeta(id)),
        message: `"${club}" matches ${ids.size} clubs — ask again with one of these club_ids.` }
    }
    const id = [...ids][0]
    const meta = soccerClubMeta(id)
    const [model, boards, squad, form, styles, upcoming, inplay] = await Promise.all([
      _load('soccer_model.json'), _load('season_odds.json'), _load('squad.json'),
      _load('form.json'), _load('team_styles.json'), _load('upcoming.json'), _load('inplay_live.json'),
    ])

    // A club can appear in several competitions at once (domestic league + a European
    // or South-American cup), so collect its row from every one of them.
    const competitions: any[] = []
    for (const l of model?.leagues ?? []) {
      const table = (l.table ?? []).find((r: any) => r.club_id === id)
      const odds = (l.season_odds ?? []).find((r: any) => r.club_id === id)
      const scorers = (l.top_scorer ?? []).filter((p: any) => p.club_id === id)
      if (!table && !odds && !scorers.length) continue
      const boardRows = ((boards?.leagues ?? []).find((b: any) => b.league === l.league)?.boards ?? [])
        .map((b: any) => ({ family: b.family, row: (b.rows ?? []).find((r: any) => r.club_id === id) }))
        .filter((b: any) => b.row)
      competitions.push({
        league: l.league, name: l.name, zh: l.zh, kind: l.kind, odds_state: l.odds_state,
          odds_family_states: l.odds_family_states, availability_reason: l.availability_reason,
          coverage: l.coverage, odds_notes: l.odds_notes, odds_notes_i18n: l.odds_notes_i18n, source_as_of: l.source_as_of,
        table_row: table ?? null, season_odds: odds ?? null,
        venue_boards: boardRows, top_scorer_candidates: scorers,
      })
    }
    if (!competitions.length && !meta) return { error: `Club "${club}" not found.` }

    const of = (d: any) => (d?.teams ?? []).find((t: any) => t.team_id === id) ?? null
    const mine = (m: any) => m?.home?.id === id || m?.away?.id === id
    return {
      club: meta?.name ?? id, club_id: id, league: meta?.league,
      competitions,
      form: of(form), squad: of(squad), style: of(styles),
      upcoming_matches: (upcoming?.matches ?? []).filter(mine),
      live_matches: (inplay?.matches ?? []).filter(mine),
    }
  },
}

// ── tool 3: one fixture (mirrors get_wc_match) ───────────────────────────────
export const soccerMatchTool: AgentTool = {
  definition: {
    name: 'get_soccer_match',
    description:
      'Deep-dive on ONE club fixture: our model 3-way + over-2.5 + BTTS, the de-vig book, real ' +
      'Kalshi and Polymarket US asks, edge per side, the bet decision (side / venue / price / ' +
      'stake, or why we pass), any cross-venue lock-arb, the `caps` (two-legged tie? aggregate ' +
      'after leg 1? extra time and penalties?) and the 2-way "who advances" block when the tie has ' +
      'one — plus, if it is being played right now, the live 3-way, xG, score and in-play signals. ' +
      'Also each club\'s season context. Give both clubs by name (English / 中文 / 日本語) or id.',
    input_schema: {
      type: 'object',
      properties: {
        home: { type: 'string', description: 'One club (name in any language, or club_id).' },
        away: { type: 'string', description: 'The other club (name in any language, or club_id).' },
      },
      required: ['home', 'away'],
    },
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ home, away }) {
    const h = clubIds(home), a = clubIds(away)
    if (!h.size) return { error: `Club "${home}" not found.` }
    if (!a.size) return { error: `Club "${away}" not found.` }
    const [upcoming, inplay, advance, schedule, model] = await Promise.all([
      _load('upcoming.json'), _load('inplay_live.json'), _load('inplay_live_advance.json'),
      _load('schedule.json'), _load('soccer_model.json'),
    ])
    // Order-insensitive: the user rarely knows which side is at home.
    const isPair = (m: any) =>
      (inClub(h, m?.home?.id) && inClub(a, m?.away?.id)) ||
      (inClub(h, m?.away?.id) && inClub(a, m?.home?.id))

    const live = (inplay?.matches ?? []).find(isPair) ?? null
    const pre = (upcoming?.matches ?? []).find(isPair) ?? null
    const past = (schedule?.matches ?? []).filter((m: any) => isPair(m) && m.finished)
    if (!live && !pre) {
      return { error: `No scheduled or live fixture found for "${home}" vs "${away}".`,
        recently_played: past.slice(-3),
        hint: 'They may not be drawn against each other in the 30-day window we publish. Use get_soccer_club for either club\'s next fixtures.' }
    }
    const seasonOf = (ids: Set<string>) => {
      const out: any[] = []
      for (const l of model?.leagues ?? []) {
        const row = (l.season_odds ?? []).find((r: any) => inClub(ids, r.club_id))
        if (row) out.push({ league: l.league, name: l.name, p_champion: row.p_champion, p_top_n: row.p_top_n, p_relegation: row.p_relegation, e_points: row.e_points, rating: row.rating })
      }
      return out
    }
    return {
      pre_match: pre,          // model + book + kalshi + poly_us + edge + decision + caps + advance
      live,                    // live model + xG + opportunities + hedge (only while in play)
      live_advance: live ? (advance?.matches ?? []).find(isPair) ?? null : null,
      live_note: live
        ? `LIVE now ${live.minute}' ${live.score} — ${live.opportunities?.length ?? 0} in-play signal(s).`
        : 'Not in play right now — this is the pre-match model and venue prices. In-play signals appear here only while the match is being played.',
      previous_meetings: past.slice(-3),
      context: { home: seasonOf(h), away: seasonOf(a) },
    }
  },
}

// ── tool 4: side-by-side (mirrors compare_wc_teams) ──────────────────────────
export const soccerCompareTool: AgentTool = {
  definition: {
    name: 'compare_soccer_clubs',
    description:
      'Compare 2–6 clubs side by side: current table position (points / GD / played), season odds ' +
      '(title / top-four / relegation, expected points and finishing rank), model rating, Elo rank, ' +
      'squad-strength z, recent-form z and primary playing style. Accepts names in any UI language ' +
      'or club_ids. Mirrors compare_wc_teams / compare_strategies.',
    input_schema: {
      type: 'object',
      properties: {
        clubs: { type: 'array', items: { type: 'string' }, description: 'Clubs to compare (2–6), by name or club_id.' },
      },
      required: ['clubs'],
    },
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ clubs }) {
    if (!Array.isArray(clubs) || clubs.length < 2) return { error: 'Provide 2–6 clubs to compare.' }
    const [model, squad, form, styles] = await Promise.all([
      _load('soccer_model.json'), _load('squad.json'), _load('form.json'), _load('team_styles.json'),
    ])
    const rows = clubs.slice(0, 6).map((q: string) => {
      const ids = clubIds(q)
      if (!ids.size) return { query: q, error: 'not found' }
      const id = [...ids][0]
      // Prefer the club's domestic league row — a cup row has no table or relegation.
      let best: any = null
      for (const l of model?.leagues ?? []) {
        const odds = (l.season_odds ?? []).find((r: any) => r.club_id === id)
        const table = (l.table ?? []).find((r: any) => r.club_id === id)
        if (!odds && !table) continue
        const cand = { league: l.league, kind: l.kind, odds, table }
        if (!best || (best.kind !== 'league' && cand.kind === 'league')) best = cand
      }
      const pick = (d: any) => (d?.teams ?? []).find((t: any) => t.team_id === id)
      return {
        club: soccerClubMeta(id)?.name ?? id, club_id: id, league: best?.league ?? soccerClubMeta(id)?.league,
        table: best?.table ?? null,
        p_champion: best?.odds?.p_champion ?? null, p_top_n: best?.odds?.p_top_n ?? null,
        p_relegation: best?.odds?.p_relegation ?? null, e_points: best?.odds?.e_points ?? null,
        e_rank: best?.odds?.e_rank ?? null, rating: best?.odds?.rating ?? null,
        elo_rank: best?.odds?.elo_rank ?? null,
        squad_z: pick(squad)?.score_z ?? null, form_z: pick(form)?.form_z ?? null,
        style: pick(styles)?.style ?? null,
      }
    })
    return { clubs: rows }
  },
}

// ── tool 5: the track record as a time series (mirrors get_wc_track_record) ──
export const soccerTrackRecordTool: AgentTool = {
  definition: {
    name: 'get_soccer_track_record',
    description:
      'The canonical Club Soccer SMART TIMING ledger, identical to Accuracy/PnL, Price Track and the PnL PDF: ' +
      'every recorded PRE and in-play position with entry/exit quotes, stake, realized position P&L before fees ' +
      'and the original PRE/in-play/combined cumulative columns. Summary counts profit/loss/flat from realized ' +
      'returns, not terminal match winners. Demo fills are a separate execution subset. Optional filters: league, club, since (YYYY-MM-DD), stage ' +
      '("league" or "knockout"). Use for "how are our predictions doing", "are we profitable", ' +
      '"show the P&L curve".',
    input_schema: {
      type: 'object',
      properties: {
        league: { type: 'string', description: 'Only this competition (league id or translated name).' },
        club: { type: 'string', description: 'Only bets on matches involving this club.' },
        stage: { type: 'string', description: 'Only this stage ("league" / "knockout" / a cup round name).' },
        since: { type: 'string', description: 'Only bets on/after this date (YYYY-MM-DD).' },
      },
      required: [],
    },
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ club, league, stage, since }) {
    const perf = await _load('performance_report.json')
    let log: any[] = perf?.strategy_ledger?.records ?? []
    if (!perf) return { data_status: { state: 'unavailable', issues: [{ code: 'source_unavailable' }] }, message: 'The performance snapshot could not be loaded.' }
    if (!perf.strategy_ledger?.records) return { ...snapshotMeta(perf), message: 'The canonical smart-timing ledger is unavailable.' }
    const ids = clubIds(club)
    if (club && !ids.size) return { error: `Club "${club}" not found.` }
    const lg = resolveLeague(league)
    if (league && !lg) return { error: `Competition "${league}" not found.` }
    if (lg) log = log.filter((b) => b.league === lg)
    if (ids.size) log = log.filter((b) => matchClub(b, ids))
    if (stage) {
      const requested = String(stage).toLowerCase()
      log = log.filter((b) => requested === 'league'
        ? ['group', 'league'].includes((b.stage ?? '').toLowerCase())
        : (b.stage ?? '').toLowerCase() === requested)
    }
    if (since) log = log.filter((b) => (b.date ?? '') >= since)
    return {
      ...snapshotMeta(perf), ...ledgerMeta(perf),
      scope: { club, league: lg, stage, since }, scope_summary: smartTimingScopeSummary(log),
      evidence_summary: perf.evidence_summary,
      records: log,
      note: 'Use realized_pnl_cents for PRE and inplay_pnl_cents for in-play. Quote cents are per contract; P&L cents are for the recorded position. Legacy HOLD/argmax fields retained in each immutable source row are not the strategy return. Historical rows are not established as strict PIT. Demo account P&L does not represent this full model ledger.',
    }
  },
}

// ── grounding for the NON-agent chat ─────────────────────────────────────────
// Same contract as predictionContextForArtifacts / macroContextForArtifacts: the plain
// chat path has no real tool-calling, so when detectArtifacts() flags a soccer_* card we
// load the SAME slice the panel shows and inject it, and the model's prose matches the
// numbers on screen. The detector's league/club scoping is passed straight through —
// without it a Premier League question would be grounded on 12 competitions at once.
const SOCCER_TYPE_TO_VIEW: Record<string, string> = {
  soccer_season_odds: 'season_odds', soccer_league_table: 'league_table',
  soccer_top_scorer: 'top_scorer', soccer_squad: 'squad', soccer_styles: 'styles',
  soccer_form: 'form', soccer_match_pricing: 'predictions', soccer_predictions: 'predictions',
  soccer_divergence: 'divergence', soccer_schedule: 'schedule', soccer_inplay: 'inplay',
  soccer_pricetrack: 'pricetrack', soccer_performance: 'performance',
  soccer_calibration: 'calibration', soccer_backtest: 'backtest', soccer_params: 'params',
  soccer_overview: 'overview', soccer_model_notes: 'overview', soccer_venues: 'risk',
  soccer_pdfs: 'performance', soccer_bracket: 'bracket',
  soccer_risk: 'risk', soccer_budget: 'risk', soccer_methodology: 'overview',
}

const _PER_VIEW_CAP = 8000
const _TOTAL_CAP = 24000

export async function soccerContextForArtifacts(
  types: string[], params: { league?: string; clubs?: string[] } = {},
): Promise<string> {
  const league = params.league
  // One club scopes cleanly; several would need one call per club, so scope by league.
  const club = params.clubs?.length === 1 ? params.clubs[0] : undefined
  const seen = new Set<string>()
  const blocks: string[] = []
  let total = 0
  for (const t of types) {
    const view = SOCCER_TYPE_TO_VIEW[t]
    if (!view || seen.has(view)) continue
    seen.add(view)
    if (total >= _TOTAL_CAP) break
    try {
      const data = await soccerMarketTool.execute({ view, league, club })
      if (!data || (data as any).error) { blocks.push(`### view="${view}" — data unavailable; do not infer no matches or invent values.`); continue }
      let json = JSON.stringify(data)
      if (json.length > _PER_VIEW_CAP) json = json.slice(0, _PER_VIEW_CAP) + ' …[truncated]'
      const block = `### view="${view}" — ${VIEWS[view].about}\n${json}`
      blocks.push(block)
      total += block.length
    } catch { blocks.push(`### view="${view}" — data unavailable; do not infer no matches.`) }
  }
  if (!blocks.length) return ''
  const scope = league ? ` (scoped to ${soccerLeagueDef(league)?.label ?? league})` : ''
  return `## Club Soccer prediction-market data${scope} — stored snapshots; these are the ` +
    'numbers on the panel. Check source_as_of, as_of, data_status and operations before calling them current. A fresh browser fetch does not refresh the source. Missing model or quote data is unavailable, not no matches or no market. Champion odds must refer to winning the complete tournament, never substitute league-phase first place. Answer ONLY from the snapshots, do not invent figures, ' +
    'and say so when a value is absent (a null probability means UNKNOWN — e.g. a competition ' +
    'whose bracket is not drawn yet — never 0%).\n\n' + blocks.join('\n\n')
}
