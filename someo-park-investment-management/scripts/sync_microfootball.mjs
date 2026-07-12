#!/usr/bin/env node
/**
 * sync_microfootball.mjs — pull the AI football-sim records from DGX box B (ec7f) and
 * build the frontend index for the "MicroFootball Sim" prediction artifact.
 *
 * The 20 sim records (10× Brazil_vs_Japan, 10× Cote d'Ivoire_vs_Norway) live on ec7f at
 * ~/mirofootball/games/. ec7f is only reachable via a double hop:
 *   Mac --(Tailscale, nvsync.key)--> ed9f (100.76.95.43) --(cable, ed9f shared key)--> ec7f (169.254.229.3)
 * so we stream a tar through the nested ssh. Heavy assets (replay.gif + 10MB trajectory.jsonl)
 * land OUTSIDE public/ — in server/sim_assets/microfootball/ — served by the Express `/sim`
 * static mount over the tunnel, so `vite build` never copies them into dist/Firebase.
 *
 * Phase A  stream-pull the full records into server/sim_assets/microfootball/
 * Phase A2 rename matchup dirs to URL-safe slugs (the source "Cote d'Ivoire_vs_Norway" has a
 *          space + apostrophe — slugging avoids URL-encoding pain in the <img>/canvas src)
 * Phase B  walk the records, read the small JSON/summary, compute the 10-sim aggregate, and
 *          write the small public/data/microfootball_index.json (the only file the tunnel /data
 *          path serves; the GIFs/trajectories are referenced by /sim/... URLs in it).
 *
 *   node scripts/sync_microfootball.mjs
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync, renameSync, rmSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { homedir } from 'node:os';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ASSETS = resolve(__dirname, '../server/sim_assets/microfootball');   // heavy assets (outside public/)
const INDEX = resolve(__dirname, '../public/data/microfootball_index.json'); // small, tunnel-served via /data

const KEY = join(homedir(), 'Library/Application Support/NVIDIA/Sync/config/nvsync.key');
const ED9F = 'someoparkdgx25@100.76.95.43';     // box A (Tailscale)
const EC7F = 'someoparkdgx25@169.254.229.3';    // box B (cable, reached from ed9f)
const SRC_DIR = '~/mirofootball/games';
// GENERIC: we surface EVERY matchup found under games/ — no hard-coded list. New matchups added
// on the box (e.g. tomorrow's batch) appear automatically on the next sync, zero code change.

const slug = (s) => s.toLowerCase().replace(/['’`]/g, '').replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');

// ── Phase A: stream-pull the records (full, incl. gif + trajectory) via the nested ssh tar ──
// Tar `.` with --exclude=Mexico_vs_Ecuador so the apostrophe dir is captured WITHOUT having to
// quote it across three shell layers (validated). Drop into a clean ASSETS dir.
if (!existsSync(KEY)) { console.error(`[mf-sync] ssh key not found: ${KEY}`); process.exit(1); }
rmSync(ASSETS, { recursive: true, force: true });
mkdirSync(ASSETS, { recursive: true });

const remote = `cd ${SRC_DIR} && tar cf - .`;   // all matchup dirs
const cmd = `ssh -o BatchMode=yes -i "${KEY}" ${ED9F} "ssh -o BatchMode=yes ${EC7F} '${remote}'" | tar xf - -C "${ASSETS}"`;
console.log('[mf-sync] pulling sim records from ec7f (via ed9f) — this transfers ~200MB, please wait…');
try {
  execFileSync('bash', ['-c', cmd], { stdio: ['ignore', 'inherit', 'inherit'], maxBuffer: 1 << 30 });
} catch (e) {
  console.error(`[mf-sync] pull failed: ${e.message}`);
  console.error('[mf-sync] check: ed9f reachable on Tailscale, ec7f reachable from ed9f over the cable.');
  process.exit(1);
}

// ── Phase A2: rename EVERY matchup dir to a URL-safe slug (handles names with spaces/apostrophes) ──
const present = readdirSync(ASSETS, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name);
const dirSlug = {};   // original dir name → slug
for (const name of present) {
  const s = slug(name);
  dirSlug[name] = s;
  if (s !== name) renameSync(join(ASSETS, name), join(ASSETS, s));
}

// ── Phase B: read each record, compute the aggregate, write the index ──
const readJson = (p) => JSON.parse(readFileSync(p, 'utf8'));
const tryRead = (p) => (existsSync(p) ? readFileSync(p, 'utf8') : '');
const round1 = (x) => Math.round(x * 10) / 10;
const mean = (arr) => (arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0);

const matchups = [];
for (const [name, s] of Object.entries(dirSlug)) {
  const base = join(ASSETS, s);
  const entries = readdirSync(base, { withFileTypes: true });
  const simDirs = entries.filter((d) => d.isDirectory()).map((d) => d.name).sort();
  // Only index a dir that actually holds sim records (skips any stray non-matchup folder).
  if (!simDirs.some((sd) => existsSync(join(base, sd, 'stats.json')))) { rmSync(base, { recursive: true, force: true }); continue; }
  // Only index a COMPLETED batch: the generator writes a `_summary_<N>games.txt` marker into the
  // matchup dir when the whole batch finishes. A dir without it is still generating (e.g. 2/10
  // sims done) — hide it from the frontend entirely; the next sync after completion picks it up.
  if (!entries.some((e) => e.isFile() && /^_summary_.*games.*\.txt$/i.test(e.name))) {
    console.log(`[mf-sync] ${name}: batch in progress (no _summary marker) — skipped from index`);
    continue;
  }

  let homeName = name.split('_vs_')[0], awayName = name.split('_vs_')[1];
  const sims = [];
  for (const sd of simDirs) {
    const dir = join(base, sd);
    let stats, state;
    try { stats = readJson(join(dir, 'stats.json')); state = readJson(join(dir, 'state.json')); }
    catch { console.warn(`[mf-sync] skip malformed record: ${s}/${sd}`); continue; }
    homeName = state.home_name || homeName;
    awayName = state.away_name || awayName;
    const cfg = existsSync(join(dir, 'match_config.json')) ? readJson(join(dir, 'match_config.json')) : {};
    const sh = stats.score?.[0] ?? state.score_home ?? 0;
    const sa = stats.score?.[1] ?? state.score_away ?? 0;
    const result = sh > sa ? 'H' : sh < sa ? 'A' : 'D';
    const summary = tryRead(join(dir, 'summary.txt')).split('\n')[0].trim().replace(/\s*\d+\s*拍\s*\/\s*/, '');
    const urlBase = `/sim/microfootball/${s}/${encodeURIComponent(sd)}`;
    sims.push({
      sim_id: sd,
      gif_url: `${urlBase}/replay.gif`,
      traj_url: `${urlBase}/trajectory.jsonl`,
      score: { home: sh, away: sa },
      result,
      summary,
      reasoning: state.config_reasoning || '',
      stats: { home: stats.home, away: stats.away },
      tactics: { home: cfg.home || null, away: cfg.away || null },
      narrative_seed: cfg.narrative_seed || '',
    });
  }

  const n = sims.length;
  const rec = { home_wins: 0, draws: 0, away_wins: 0 };
  for (const m of sims) rec[m.result === 'H' ? 'home_wins' : m.result === 'A' ? 'away_wins' : 'draws']++;
  const dist = {};
  for (const m of sims) { const k = `${m.score.home}-${m.score.away}`; dist[k] = (dist[k] || 0) + 1; }
  const aggregate = {
    record: rec,
    win_pct: n ? { home: round1((rec.home_wins / n) * 100) / 100, draw: round1((rec.draws / n) * 100) / 100, away: round1((rec.away_wins / n) * 100) / 100 } : { home: 0, draw: 0, away: 0 },
    avg_possession: { home: round1(mean(sims.map((m) => m.stats.home.possession_pct))), away: round1(mean(sims.map((m) => m.stats.away.possession_pct))) },
    avg_xg: { home: round1(mean(sims.map((m) => m.stats.home.xg))), away: round1(mean(sims.map((m) => m.stats.away.xg))) },
    avg_shots: { home: round1(mean(sims.map((m) => m.stats.home.shots))), away: round1(mean(sims.map((m) => m.stats.away.shots))) },
    avg_score: { home: round1(mean(sims.map((m) => m.score.home))), away: round1(mean(sims.map((m) => m.score.away))) },
    score_distribution: Object.entries(dist).map(([score, count]) => ({ score, count })).sort((a, b) => b.count - a.count),
  };
  matchups.push({ id: s, dir: s, home_name: homeName, away_name: awayName, n_sims: n, sims, aggregate });
}

matchups.sort((a, b) => b.n_sims - a.n_sims || a.home_name.localeCompare(b.home_name));   // complete matchups first
mkdirSync(dirname(INDEX), { recursive: true });
writeFileSync(INDEX, JSON.stringify({ generated_at: new Date().toISOString(), matchups }, null, 2), 'utf8');

const gifCount = matchups.reduce((a, m) => a + m.sims.length, 0);
const bytes = (() => { let t = 0; const walk = (d) => readdirSync(d, { withFileTypes: true }).forEach((e) => { const p = join(d, e.name); e.isDirectory() ? walk(p) : (t += statSync(p).size); }); existsSync(ASSETS) && walk(ASSETS); return t; })();
console.log(`[mf-sync] ${matchups.map((m) => `${m.home_name}-${m.away_name}:${m.n_sims}`).join('  ')}`);
console.log(`[mf-sync] assets ${(bytes / 1e6).toFixed(0)}MB in ${ASSETS}  (${gifCount} sims) ; index → ${INDEX}`);

// ── Phase C: DFM amplification (extract → production → public/data/dfm_index.json) ──
// The diffusion amplifier (dfm/football/) turns each matchup's 10-15 sims into 5000
// synthetic matches (W/D/L, scorelines, 16-channel stat quantiles, cards — engine-faithful
// + real-anchored views). Runs after every sync so NEW matchups get their DFM snapshot
// automatically (~15 min of training/generation; `--skip-dfm` for a quick asset-only sync).
// Official timestamped outputs stay in dfm/football/production_runs/ (audit-kept); the
// frontend only receives the small per-matchup summary index.
const DFM_DIR = resolve(__dirname, '../../dfm/football');
const DFM_INDEX = resolve(__dirname, '../public/data/dfm_index.json');
if (process.argv.includes('--skip-dfm')) {
  console.log('[mf-sync] --skip-dfm: DFM amplification skipped (dfm_index.json unchanged)');
} else {
  console.log('[mf-sync] DFM amplification: extract → production (trains + generates, ~15 min)…');
  try {
    execFileSync('bash', ['-c',
      `cd "${DFM_DIR}" && conda run -n someopark_run --no-capture-output bash -c "python extract.py && python production.py"`],
      { stdio: ['ignore', 'inherit', 'inherit'], maxBuffer: 1 << 30 });
  } catch (e) {
    console.error(`[mf-sync] DFM pipeline failed: ${e.message} — frontend keeps the previous dfm_index.json`);
    process.exit(1);
  }
  const runsDir = join(DFM_DIR, 'production_runs');
  const manifests = readdirSync(runsDir).filter((f) => /^manifest_\d{8}_\d{6}\.json$/.test(f)).sort();
  const manifest = readJson(join(runsDir, manifests.at(-1)));
  const entries = {};
  for (const f of manifest.files) {
    const doc = readJson(join(runsDir, f));
    entries[doc.matchup] = {
      home: doc.home, away: doc.away, ts: doc.ts,
      n_source_sims: doc.n_source_sims, n_samples: doc.n_samples,
      engine_faithful: { ...doc.engine_faithful, events_sample: undefined },
      real_anchored: { ...doc.real_anchored, events_sample: undefined },
    };
  }
  writeFileSync(DFM_INDEX, JSON.stringify({
    generated_at: new Date().toISOString(), ts: manifest.ts,
    corpus_sha1: manifest.corpus.sha1, matchups: entries,
  }, null, 1), 'utf8');
  console.log(`[mf-sync] DFM index → ${DFM_INDEX} (${Object.keys(entries).length} matchups, snapshot ${manifest.ts})`);
}
