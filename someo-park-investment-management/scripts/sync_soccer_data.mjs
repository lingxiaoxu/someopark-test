#!/usr/bin/env node
/**
 * sync_soccer_data.mjs — copy prediction_market_soccer outputs into public/data/soccer/.
 *
 * The club-soccer system writes JSON to prediction_market_soccer/data/output/.
 * The frontend (Club Soccer Market mode) reads them as static files from
 * public/data/soccer/ (dev) → dist/data/soccer/ (firebase). Whitelist-style copy,
 * mirroring sync_prediction_data.mjs. Read-only on the source; only writes into
 * public/data/soccer/ (its own namespace — the WC files in public/data/ and the
 * stock-strategy files are NEVER touched). Missing files are warned, not fatal
 * (the live exporters — upcoming/inplay/schedule — land in later phases).
 *
 *   node scripts/sync_soccer_data.mjs        (run before `npm run build`)
 */
import { existsSync, copyFileSync, mkdirSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(__dirname, '../../prediction_market_soccer/data/output');
const DST = resolve(__dirname, '../public/data/soccer');

// Exact files the frontend consumes (latest.json / model_run_* archives are
// deliberately NOT synced). Everything is copy-if-exists — the live/quality
// exporters land per backend phase and the views render empty states meanwhile.
const FILES = [
  'soccer_model.json',
  'season_odds.json',
  'upcoming.json',
  'inplay_live.json',
  'schedule.json',
  'calibration.json',
  'oos_report.json',
  'performance_report.json',
  'risk_report.json',
  'squad.json',
  'form.json',
  'team_styles.json',
  'xv_matches.json',
  'xv_champion.json',
  'bracket.json',
  'param_select_club.json',
  'milestone_marks.json',
  'backtest.json',
  'frontend_overview.json',
  'performance_report.pdf',
  'risk_report.pdf',
];

if (!existsSync(SRC)) {
  console.error(`[sync:soccer] source not found: ${SRC}`);
  console.error('[sync:soccer] run the prediction_market_soccer exporters first (run_model / season_odds_export / upcoming_export).');
  process.exit(1);
}
mkdirSync(DST, { recursive: true });

let copied = 0;
const missing = [];
for (const f of FILES) {
  const s = join(SRC, f);
  if (existsSync(s)) { copyFileSync(s, join(DST, f)); copied++; }
  else missing.push(f);
}

console.log(`[sync:soccer] copied ${copied}/${FILES.length} soccer files → public/data/soccer/`);
if (missing.length) console.log(`[sync:soccer] not yet generated (skipped): ${missing.join(', ')}`);
console.log(`[sync:soccer] public/data/soccer now has: ${readdirSync(DST).join(', ')}`);
