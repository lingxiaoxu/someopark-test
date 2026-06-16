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
      },
      required: ['view'],
    },
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ view, top }) {
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
    // Trim the big arrays so the model gets the relevant slice, not a wall of JSON.
    if (view === 'champion' && data?.champion) {
      return { meta: data.meta, champion: n ? data.champion.slice(0, n) : data.champion }
    }
    if (view === 'golden_boot' && data?.golden_boot) {
      return { meta: data.meta, golden_boot: n ? data.golden_boot.slice(0, n) : data.golden_boot }
    }
    if (view === 'predictions') {
      const matches = data?.matches ?? data
      return { note: data?.note, matches: n && Array.isArray(matches) ? matches.slice(0, n) : matches }
    }
    return data
  },
}
