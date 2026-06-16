#!/usr/bin/env node
/**
 * sync_prediction_data.mjs — copy prediction_market outputs into public/data/.
 *
 * The prediction_market system writes JSON/PDF to prediction_market/data/output/.
 * The frontend (Prediction Market mode) reads them as static files from
 * public/data/ (dev) → dist/data/ (firebase). This script copies the consumed
 * artifacts across. Read-only on the source; only writes into public/data/.
 * It NEVER touches the stock-strategy data files already in public/data/.
 *
 *   node scripts/sync_prediction_data.mjs        (run before `npm run build`)
 */
import { existsSync, copyFileSync, mkdirSync, readdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(__dirname, '../../prediction_market/data/output');
const DST = resolve(__dirname, '../public/data');

// Exact files the frontend consumes (single-file artifacts).
const FILES = [
  'frontend_overview.json',
  'worldcup_model.json',
  'xv_matches.json',
  'xv_champion.json',
  'upcoming.json',
  'inplay_signals.json',
  'inplay_live.json',
  'backtest.json',
  'performance_report.json',
  'risk_report.json',
  'oos_report.json',
  'performance_report.pdf',
  'risk_report.pdf',
];

if (!existsSync(SRC)) {
  console.error(`[sync] source not found: ${SRC}`);
  console.error('[sync] run the prediction_market exporters first (frontend_export / upcoming_export / *_report --pdf).');
  process.exit(1);
}
mkdirSync(DST, { recursive: true });

let copied = 0, missing = [];
for (const f of FILES) {
  const s = join(SRC, f);
  if (existsSync(s)) { copyFileSync(s, join(DST, f)); copied++; }
  else missing.push(f);
}

// worldcup_model.json also lives at the source root via run_model --emit-frontend;
// prefer the output/ copy, but fall back so the champion artifact never 404s.
if (missing.includes('worldcup_model.json')) {
  const alt = resolve(__dirname, '../public/data/worldcup_model.json');
  if (existsSync(alt)) missing = missing.filter((m) => m !== 'worldcup_model.json');
}

console.log(`[sync] copied ${copied}/${FILES.length} prediction files → public/data/`);
if (missing.length) console.log(`[sync] not yet generated (skipped): ${missing.join(', ')}`);
console.log(`[sync] public/data now has: ${readdirSync(DST).filter((f) => /worldcup|xv_|upcoming|frontend_overview|inplay|performance_report|risk_report|oos_/.test(f)).join(', ')}`);
