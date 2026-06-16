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
  golden_boot: { file: 'worldcup_model.json',      about: 'top-scorer probabilities (EA FC 26 talent + knockout depth + teammate split)' },
  predictions: { file: 'upcoming.json',            about: 'upcoming matches: model 3-way + O2.5/BTTS, de-vig book, real Kalshi/Poly US asks, edges' },
  inplay:      { file: 'inplay_live.json',         about: 'LIVE matches now: live 3-way, xG, remaining goals, in-play arb/value/tactic signals' },
  performance: { file: 'performance_report.json',  about: 'accuracy (Brier vs uniform, calibrated), trade-grade gate, and the production bet log (per-match prediction/bet/result/PnL)' },
  risk:        { file: 'risk_report.json',         about: 'pre-trade gates, venue balances, $1 order cap, API budget, calibration gate' },
  backtest:    { file: 'backtest.json',            about: 'model vs market vs uniform Brier on settled matches; blend curve; trade-grade verdict' },
  squad:       { file: 'squad.json',               about: 'squad strength z-scores (minutes-weighted club rating + attack)' },
  form:        { file: 'form.json',                about: 'recent-form index (time-weighted, friendly-discounted goal difference)' },
  params:      { file: 'param_sweep.json',         about: 'parameter sweep: which param set was selected (min Brier) and the grid of alternatives' },
  divergence:  { file: 'xv_matches.json',          about: 'model 3-way vs the sharp bookmaker de-vig (where we diverge from the market)' },
  overview:    { file: 'frontend_overview.json',   about: 'system overview: interfaces, modes, schedule, inputs/outputs, value, state-aware headline' },
}

export const predictionMarketTool: AgentTool = {
  definition: {
    name: 'get_prediction_market',
    description:
      'Read World Cup 2026 prediction-market data (the Kalshi + Polymarket trading system). ' +
      'Use this for ANY question about: who will win the World Cup / champion odds, the golden boot ' +
      '(top scorer), today\'s / upcoming match predictions and venue prices, LIVE in-play matches and ' +
      'in-play arbitrage signals, our prediction accuracy / Brier / calibration, the production bet log ' +
      'and P&L, the trade-grade gate, risk gates, the backtest, squad strength, recent form, or the ' +
      'parameter sweep. Pick the `view` for the data you need. Probabilities are 0-1; venue prices are ' +
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
    if (view === 'performance' && team && Array.isArray(data?.bet_log)) {
      // Filter the production bet log to this team's matches.
      return { ...data, bet_log: data.bet_log.filter((b: any) => matchesTeam(team, b) || matchesTeam(team, { name: b.home }) || matchesTeam(team, { name: b.away })) }
    }
    return data
  },
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
      context: { home: champ(home), away: champ(away) },
    }
  },
}
