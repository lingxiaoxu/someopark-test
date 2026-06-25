// server/tools/predictionMarketTool.ts
// World Cup 2026 prediction-market data (Kalshi + Polymarket).
// Data source: public/data/*.json — the SAME files the Prediction Market dashboard
// reads, produced by prediction_market/ops/* (champion sim, golden boot, live in-play,
// upcoming model+venue quotes, performance/bet-log, risk gate, backtest, calibration).

import { readJsonFile } from '../utils/fileUtils.js'
import path from 'path'
import { fileURLToPath } from 'url'
import type { AgentTool } from './index.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const dataDir = path.resolve(__dirname, '..', '..', 'public', 'data')

// view → file + short description of what it holds.
const VIEWS: Record<string, { file: string; about: string }> = {
  champion:    { file: 'worldcup_model.json',      about: 'champion odds (p_champion/final/sf), FIFA rank, rating per team; also golden_boot + group_matches' },
  golden_boot: { file: 'worldcup_model.json',      about: 'top-scorer probabilities (EA FC 26 talent + knockout depth + teammate split) + current goals scored' },
  predictions: { file: 'upcoming.json',            about: 'today/upcoming matches: model 3-way + O2.5/BTTS, de-vig book, real Kalshi/Poly US asks, edges, AND the decision (value pick + stake + the dual-口径 plan: entry now + planned smart-exit cash-out)' },
  schedule:    { file: 'upcoming.json',            about: 'fixture schedule with kickoff times (ET/PT) + recently finished matches (FT + score)' },
  inplay:      { file: 'inplay_live.json',         about: 'LIVE matches now: live 3-way, xG, remaining goals, + 15 in-play tactics (8 data-mined incl. the overshoot/smart-exit take-profit) + the OVER/UNDER totals market. Each opportunity carries an `intent` ("manage" = sell/lock a HELD position, "entry" = new value entry, "event" = red card / goal over-reaction), a CONFIDENCE tier (`confidence` high/med/low, from the validated effectiveness study) and a STAKING gate (`actionable` + `stake_usd`, else advisory — keeps every signal but only stakes the ones that clear the gate). Each live match also carries a `hedge` object — when we hold our pre-match DIRECTIONAL value bet, it gives the DRAW-protection: how many draw contracts to buy (break-even `b` = cost/(100−draw¢), and full hedge), a 3-state payoff matrix (home/draw/away win), `lead_state` (leading/level/behind), and our `entry_c`. The smart-money cross-validation only flags an entry when the book AND the model both beat the tradable venue price (venue lagging), agreeing with the model rather than chasing the book.' },
  performance: { file: 'performance_report.json',  about: 'accuracy (Brier vs uniform, calibrated), trade-grade gate, and the bet log in 3 modes: REALIZED (decision + smart-exit cash-out = the real strategy, e.g. 15W-7L/+486¢), HOLD-to-FT (reference), and ARGMAX (reference) — each with W-L + PnL' },
  reach_round: { file: 'reach_round.json',         about: 'reach-round (晋级盘): each of the 48 teams’ probability to reach each round (group-advance / R16 / QF / SF / final), model % vs Kalshi¢ (KXWCROUND) vs Poly¢ + edge; refreshed on every settle' },
  styles:      { file: 'team_styles.json',         about: 'team style taxonomy as a 48 teams × 10 styles MATRIX (styles: possession / direct / high-press / low-block / dominant-attack / clinical / high-volume / set-piece / balanced / contained). A team can play 1–2 styles — `teams[].styles` is a 1–2 element array, each {code,label,poss,rank} where rank is the team\'s within-style ranking by possession (cells do NOT sum to 100%). Built from a curated research prior blended with live API-Football metrics; refreshed weekly. Backward-compatible: each team also keeps the legacy single `style` (its primary) + `metrics` (possession/xG/directness…). Descriptive scouting aid.' },
  risk:        { file: 'risk_report.json',         about: 'pre-trade gates, venue balances (Kalshi/Poly), $1 order cap, API budget/health, calibration gate (also the Venues & Gates and API-Budget views)' },
  calibration: { file: 'oos_report.json',          about: 'out-of-sample reliability: Brier vs uniform, log-loss, predicted-vs-observed draw rate, goal-total bias — the directional calibration health check' },
  backtest:    { file: 'backtest.json',            about: 'model vs market vs uniform Brier on settled matches; blend curve; trade-grade verdict' },
  squad:       { file: 'squad.json',               about: 'squad strength z-scores (minutes-weighted club rating + attack); blended into the live model' },
  form:        { file: 'form.json',                about: 'recent-form index (time-weighted, friendly-discounted goal difference); blended into the live model' },
  params:      { file: 'param_sweep.json',         about: 'parameter sweep: the selected param set (min calibrated Brier) + the full grid (7 knobs incl. FC/squad/form weights), n_param_sets + n_settled' },
  divergence:  { file: 'xv_matches.json',          about: 'model 3-way vs the sharp bookmaker de-vig (where we diverge from the market)' },
  pricetrack:  { file: 'milestone_marks.json',     about: 'per-contract ¢ + probability at each match milestone (PRE/15\'/30\'/HT/60\'/75\'/FT) for home/draw/away from Kalshi+Polymarket, our pre-match pick + entry ¢, the mark-to-market (entry→FT), AND the smart-exit cash-out (when/at what ¢ we sold the over-reaction vs holding to settlement)' },
  overview:    { file: 'frontend_overview.json',   about: 'system overview: interfaces, modes, schedule, inputs/outputs, value, state-aware headline (the System Overview + Model Notes views)' },
}

export const predictionMarketTool: AgentTool = {
  definition: {
    name: 'get_prediction_market',
    description:
      'Read World Cup 2026 prediction-market data (the Kalshi + Polymarket trading system). This backs ' +
      'EVERY dashboard view, so use it for ANY question about: champion odds, the golden boot (top ' +
      'scorer), today\'s / upcoming match predictions + venue prices, the fixture schedule, LIVE in-play ' +
      'matches + 15 in-play tactics (incl. the smart-exit/overshoot take-profit) and totals markets, our ' +
      'accuracy / Brier / calibration (OOS reliability), the production bet log and P&L in 3 modes ' +
      '(REALIZED = decision + smart-exit cash-out, HOLD-to-FT, ARGMAX), the trade-grade gate, risk gates / ' +
      'venue balances / API budget, the backtest, squad strength, recent form, the reach-round (晋级盘) ' +
      'per-team advancement probabilities, the team style taxonomy (球队风格), model-vs-market divergence, ' +
      'the parameter sweep, or the system overview. Pick the `view` for the data you need. Probabilities are 0-1; venue prices are ' +
      'contract prices (≈ implied probability). Knockout matches have no draw (decided by extra time + ' +
      'penalty shootout); group matches do.',
    input_schema: {
      type: 'object',
      properties: {
        view: {
          type: 'string',
          enum: Object.keys(VIEWS),
          description:
            'Which dataset: ' +
            Object.entries(VIEWS).map(([k, v]) => `"${k}" = ${v.about}`).join('; '),
        },
        top: { type: 'number', description: 'For champion/golden_boot/predictions: only the top N rows (default all).' },
        team: { type: 'string', description: 'Optional filter — restrict champion/golden_boot/predictions/performance(bet log) to one team (name/id/Chinese).' },
      },
      required: ['view'],
    },
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ view, top, team }) {
    const spec = VIEWS[view]
    if (!spec) {
      return { error: `Unknown view "${view}". Valid: ${Object.keys(VIEWS).join(', ')}` }
    }
    let data: any
    try {
      data = await readJsonFile(path.resolve(dataDir, spec.file))
    } catch (e: any) {
      return { error: `${spec.file} not available yet (run the exporter + npm run sync:wc): ${e?.message || e}` }
    }

    const n = typeof top === 'number' && top > 0 ? top : undefined
    const hit = (s: any) => !team || matchesTeam(team, s)
    // Trim the big arrays so the model gets the relevant slice, not a wall of JSON.
    if (view === 'champion' && data?.champion) {
      let rows = data.champion.filter((c: any) => hit(c))
      return { meta: data.meta, champion: n ? rows.slice(0, n) : rows }
    }
    if (view === 'golden_boot' && data?.golden_boot) {
      let rows = data.golden_boot.filter((p: any) => hit(p))
      return { meta: data.meta, golden_boot: n ? rows.slice(0, n) : rows }
    }
    if (view === 'predictions') {
      let matches = (data?.matches ?? data) as any[]
      if (Array.isArray(matches) && team) matches = matches.filter((m) => matchesTeam(team, m.home) || matchesTeam(team, m.away))
      return { note: data?.note, matches: n && Array.isArray(matches) ? matches.slice(0, n) : matches }
    }
    if (view === 'inplay') {
      // No live match → say so explicitly (don't return a confusing empty result).
      if (!data || !data.n_live || !(data.matches || []).length) {
        return { n_live: 0, matches: [],
          message: 'No match is live right now, so there is no in-play arbitrage to show — ' +
            'in-play signals are only produced during a live match. Use view="predictions" for ' +
            'upcoming matches and their scheduled kickoff times.' }
      }
      return data
    }
    if (view === 'performance' && team && Array.isArray(data?.bet_log)) {
      // Filter the production bet log to this team's matches.
      return { ...data, bet_log: data.bet_log.filter((b: any) => matchesTeam(team, b) || matchesTeam(team, { name: b.home }) || matchesTeam(team, { name: b.away })) }
    }
    if (view === 'pricetrack') {
      let matches = (data?.matches ?? []) as any[]
      if (team) matches = matches.filter((m) => matchesTeam(team, m.home) || matchesTeam(team, m.away))
      return { milestones: data?.milestones, note: data?.note, matches: n ? matches.slice(0, n) : matches }
    }
    if (view === 'styles') {
      let rows = (data?.teams ?? []) as any[]
      if (team) rows = rows.filter((r) => matchesTeam(team, r))
      return { ts: data?.ts, n: rows.length, teams: n ? rows.slice(0, n) : rows }
    }
    if (view === 'reach_round') {
      // rounds[] each holds a per-team array — optionally filter to one team per round.
      let rounds = (data?.rounds ?? []) as any[]
      if (team) rounds = rounds.map((rd) => ({ ...rd, teams: (rd.teams ?? []).filter((t: any) => matchesTeam(team, t)) }))
      return { as_of: data?.as_of, note: data?.note, rounds }
    }
    return data
  },
}

// ── grounding for the NON-agent chat ───────────────────────────────────────────
// The plain chat path (nemo / any model, no real tool-calling) answers blind unless we
// hand it the data. When detectArtifacts() flags a wc_* artifact, we load the SAME
// trimmed data the panel shows and inject it so the model's prose matches the panel.
// wc_<type> artifact → VIEWS key (a few cards share one underlying file/view).
const WC_TYPE_TO_VIEW: Record<string, string> = {
  wc_champion: 'champion', wc_golden_boot: 'golden_boot', wc_predictions: 'predictions',
  wc_match_pricing: 'predictions', wc_schedule: 'schedule', wc_inplay: 'inplay',
  wc_performance: 'performance', wc_reach_round: 'reach_round', wc_styles: 'styles',
  wc_risk: 'risk', wc_venues: 'risk', wc_budget: 'risk', wc_calibration: 'calibration',
  wc_backtest: 'backtest', wc_squad: 'squad', wc_form: 'form', wc_params: 'params',
  wc_divergence: 'divergence', wc_pricetrack: 'pricetrack', wc_overview: 'overview',
  wc_methodology: 'overview',
}

/** Load the real data behind detected wc_* artifact types as a compact context block so
 *  a non-agent chat model answers from the same numbers the user sees on the panel.
 *  Hard-capped per view AND in total — some files (overview/risk/performance) carry the
 *  full bet_log and would otherwise inject 100s of KB. Summary fields sit at the TOP of
 *  these JSONs (headline / gates / top rows), so head-keep truncation preserves the
 *  numbers a user actually asks about. */
const _PER_VIEW_CAP = 8000
const _TOTAL_CAP = 24000
export async function predictionContextForArtifacts(
  types: string[], opts: { top?: number } = {},
): Promise<string> {
  const seen = new Set<string>()
  const blocks: string[] = []
  let total = 0
  for (const t of types) {
    const view = WC_TYPE_TO_VIEW[t]
    if (!view || seen.has(view)) continue
    seen.add(view)
    if (total >= _TOTAL_CAP) break
    try {
      const data = await predictionMarketTool.execute({ view, top: opts.top ?? 10 })
      if (!data || (data as any).error) continue
      let json = JSON.stringify(data)
      if (json.length > _PER_VIEW_CAP) json = json.slice(0, _PER_VIEW_CAP) + ' …[truncated]'
      const block = `### view="${view}" — ${VIEWS[view].about}\n${json}`
      blocks.push(block)
      total += block.length
    } catch { /* skip a view that fails to load; others still inject */ }
  }
  return blocks.join('\n\n')
}

// ── team / match resolution (accepts English name, team_id, or Chinese name) ──
function _norm(s: string): string {
  return (s || '').toString().toLowerCase().replace(/[\s'._-]/g, '')
}
function matchesTeam(query: string, row: any): boolean {
  if (!query || !row) return false
  const q = _norm(query)
  for (const k of ['team_id', 'name', 'zh', 'home_id', 'away_id', 'pick_team']) {
    const v = row[k]
    if (v && (_norm(v) === q || _norm(v).includes(q) || q.includes(_norm(v)))) return true
  }
  return false
}

async function _load(file: string): Promise<any> {
  try { return await readJsonFile(path.resolve(dataDir, file)) } catch { return null }
}
function _list(d: any): any[] {
  if (Array.isArray(d)) return d
  return d?.rows || d?.teams || d?.matches || []
}

export const predictionMarketTeamTool: AgentTool = {
  definition: {
    name: 'get_wc_team',
    description:
      'Deep-dive on ONE World Cup 2026 team across every model view: champion / advance ' +
      'probabilities + rating + FIFA rank, its golden-boot candidates, squad strength, recent form, ' +
      'and its upcoming + live matches (model 3-way, venue asks, edge). Accepts the team\'s English ' +
      'name, id, or Chinese name (e.g. "Argentina", "argentina", "阿根廷"). Mirrors get_pair_stats.',
    input_schema: {
      type: 'object',
      properties: { team: { type: 'string', description: 'Team name / id (English, id, or Chinese).' } },
      required: ['team'],
    },
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ team }) {
    const [wc, squad, form, upcoming, inplay] = await Promise.all([
      _load('worldcup_model.json'), _load('squad.json'), _load('form.json'),
      _load('upcoming.json'), _load('inplay_live.json'),
    ])
    const champion = (wc?.champion || []).find((c: any) => matchesTeam(team, c))
    if (!champion) return { error: `Team "${team}" not found in the 48-team field.` }
    return {
      team: champion.name, zh: champion.zh,
      champion,                                                   // p_champion/final/sf/qf/r16/advance, rating, fifa_rank
      golden_boot: (wc?.golden_boot || []).filter((p: any) => matchesTeam(team, p)),
      squad: _list(squad).find((s: any) => matchesTeam(team, s)) || null,
      form: _list(form).find((s: any) => matchesTeam(team, s)) || null,
      upcoming_matches: _list(upcoming).filter((m: any) => matchesTeam(team, m.home) || matchesTeam(team, m.away)),
      live_matches: (inplay?.matches || []).filter((m: any) => matchesTeam(team, m.home) || matchesTeam(team, m.away)),
    }
  },
}

export const predictionMarketMatchTool: AgentTool = {
  definition: {
    name: 'get_wc_match',
    description:
      'Deep-dive on ONE World Cup 2026 matchup: our model 3-way + O2.5/BTTS, the de-vig book, real ' +
      'Kalshi & Polymarket US asks, our edge per side, any cross-venue lock-arb, and — if the match is ' +
      'LIVE now — the live 3-way / xG / remaining goals and all in-play signals. Also each team\'s ' +
      'tournament advance odds for context. Give both teams (name/id/Chinese).',
    input_schema: {
      type: 'object',
      properties: {
        home: { type: 'string', description: 'One team (name/id/Chinese).' },
        away: { type: 'string', description: 'The other team (name/id/Chinese).' },
      },
      required: ['home', 'away'],
    },
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ home, away }) {
    const [wc, upcoming, inplay] = await Promise.all([
      _load('worldcup_model.json'), _load('upcoming.json'), _load('inplay_live.json'),
    ])
    const pairOf = (m: any) =>
      (matchesTeam(home, m.home) && matchesTeam(away, m.away)) ||
      (matchesTeam(home, m.away) && matchesTeam(away, m.home))
    const live = (inplay?.matches || []).find(pairOf)
    const pre = _list(upcoming).find(pairOf)
    if (!live && !pre) {
      return { error: `No scheduled/live match found for "${home}" vs "${away}". They may have already played or not be drawn together.` }
    }
    const champ = (t: string) => {
      const c = (wc?.champion || []).find((x: any) => matchesTeam(t, x))
      return c ? { name: c.name, p_advance_model: c.p_advance_model, p_champion: c.p_champion, fifa_rank: c.fifa_rank } : null
    }
    return {
      pre_match: pre || null,    // model 3-way + O2.5/BTTS + book + kalshi + poly + edge + lock_arb
      live: live || null,        // live model + xG + opportunities (only if in play now)
      live_note: live ? `LIVE now ${live.minute}' ${live.score} — ${live.opportunities?.length || 0} in-play signal(s).`
                      : 'Not in play right now — showing the pre-match model + venue prices. In-play arbitrage appears here only while the match is live.',
      context: { home: champ(home), away: champ(away) },
    }
  },
}

export const predictionMarketCompareTool: AgentTool = {
  definition: {
    name: 'compare_wc_teams',
    description:
      'Compare 2–6 World Cup 2026 teams side by side across the model: champion / advance odds, ' +
      'rating, FIFA rank, squad-strength z, recent-form z, and each team\'s leading golden-boot ' +
      'candidate. Mirrors compare_strategies. Accepts names / ids / Chinese names.',
    input_schema: {
      type: 'object',
      properties: {
        teams: { type: 'array', items: { type: 'string' },
          description: 'Teams to compare (2–6), by name / id / Chinese name.' },
      },
      required: ['teams'],
    },
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ teams }) {
    if (!Array.isArray(teams) || teams.length < 2) return { error: 'Provide 2–6 teams to compare.' }
    const [wc, squad, form] = await Promise.all([_load('worldcup_model.json'), _load('squad.json'), _load('form.json')])
    const rows = teams.slice(0, 6).map((q: string) => {
      const c = (wc?.champion || []).find((x: any) => matchesTeam(q, x))
      if (!c) return { query: q, error: 'not found' }
      const sq = _list(squad).find((s: any) => matchesTeam(q, s))
      const fm = _list(form).find((s: any) => matchesTeam(q, s))
      const boot = (wc?.golden_boot || []).filter((p: any) => matchesTeam(q, p))
        .sort((a: any, b: any) => b.p_golden_boot - a.p_golden_boot)[0]
      return {
        team: c.name, zh: c.zh, fifa_rank: c.fifa_rank, rating: c.rating,
        p_champion: c.p_champion, p_advance_model: c.p_advance_model,
        squad_z: sq?.score_z ?? null, form_z: fm?.form_z ?? null,
        top_scorer: boot ? { name: boot.name, p_golden_boot: boot.p_golden_boot } : null,
      }
    })
    return { teams: rows }
  },
}

export const predictionMarketTrackRecordTool: AgentTool = {
  definition: {
    name: 'get_wc_track_record',
    description:
      'The production betting track record as a TIME SERIES — flat 1u on our prediction every match ' +
      'since the opener, settled on the real result. Returns the running P&L curve (date → cumulative), ' +
      'each match (prediction / bet / result / odds / P&L), a summary (W-L, units, ROI), and a group-vs-' +
      'knockout breakdown. Optional filters: team, stage ("group"/"knockout"), since (YYYY-MM-DD).',
    input_schema: {
      type: 'object',
      properties: {
        team: { type: 'string', description: 'Only matches involving this team.' },
        stage: { type: 'string', enum: ['group', 'knockout'], description: 'Only this stage.' },
        since: { type: 'string', description: 'Only matches on/after this date (YYYY-MM-DD).' },
      },
      required: [],
    },
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ team, stage, since }) {
    const perf = await _load('performance_report.json')
    let log: any[] = perf?.bet_log || []
    if (!log.length) return { message: 'No settled matches yet — the track record starts once matches finish.' }
    if (team) log = log.filter((b) => matchesTeam(team, { name: b.home }) || matchesTeam(team, { name: b.away }) || matchesTeam(team, b))
    if (stage) log = log.filter((b) => b.stage === stage)
    if (since) log = log.filter((b) => (b.date || '') >= since)
    if (!log.length) return { message: `No matches in the track record for the given filter (team=${team}, stage=${stage}, since=${since}).` }

    // Recompute the running curve over the filtered slice.
    let cum = 0, wins = 0
    const pnl_curve = log.map((b) => { cum += b.pnl; wins += b.won ? 1 : 0
      return { date: b.date, match: `${b.home} ${b.score} ${b.away}`, stage: b.stage,
        pick: b.pick_team, result: b.result, won: b.won, odds: b.dec_odds, pnl: b.pnl, cum_pnl: Math.round(cum * 100) / 100 } })
    const byStage: Record<string, any> = {}
    for (const b of log) {
      const s = byStage[b.stage] ||= { matches: 0, wins: 0, pnl: 0 }
      s.matches++; s.wins += b.won ? 1 : 0; s.pnl = Math.round((s.pnl + b.pnl) * 100) / 100
    }
    return {
      summary: { matches: log.length, record: `${wins}W-${log.length - wins}L`,
        pnl_units: Math.round(cum * 100) / 100, roi: Math.round((cum / log.length) * 1000) / 1000,
        since: log[0].date, trade_grade: perf?.trade_grade },
      by_stage: byStage,
      pnl_curve,
    }
  },
}
