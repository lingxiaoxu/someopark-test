import { Router, Request, Response } from 'express'
import { generateText, LanguageModel } from 'ai'
import { getModelClient, LLMModel } from '../utils/models.js'
import path from 'path'
import { fileURLToPath } from 'url'
import { readFileSync, existsSync } from 'fs'
import { createHash } from 'crypto'
import { spawn } from 'child_process'
import { homedir } from 'os'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const INDEX = path.join(__dirname, '..', '..', 'public', 'data', 'microfootball_index.json')
const DFM_INDEX = path.join(__dirname, '..', '..', 'public', 'data', 'dfm_index.json')

// ── On-box analysis cache (lives on ed9f, the nemotron host, NOT the Mac) ──────────
// A generated analysis is cached on ed9f at ~/mirofootball/ai_cache/. A request reuses the
// cached text (skipping the slow model) when: the cache is < 7 days old AND the content hasn't
// changed (the hash covers the matchup's n sims + aggregate, or the single sim's stats — so it
// invalidates automatically if the 10 sims change in count or content). Best-effort: if ed9f is
// unreachable the read/write silently no-op and we fall back to a fresh model call.
const KEY = path.join(homedir(), 'Library/Application Support/NVIDIA/Sync/config/nvsync.key')
const ED9F = 'someoparkdgx25@100.76.95.43'
const CACHE_DIR = '~/mirofootball/ai_cache'
const TTL_MS = 7 * 24 * 60 * 60 * 1000

function ssh(remoteCmd: string, input?: string): Promise<{ code: number; out: string }> {
  return new Promise((resolve) => {
    const p = spawn('ssh', ['-i', KEY, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', ED9F, remoteCmd])
    let out = ''
    p.stdout.on('data', (d) => (out += d))
    p.on('error', () => resolve({ code: 1, out: '' }))
    p.on('close', (code) => resolve({ code: code ?? 1, out }))
    if (input != null) { p.stdin.write(input); p.stdin.end() } else { p.stdin.end() }
  })
}
const safe = (s: string) => (s || '').replace(/[^a-zA-Z0-9_.-]/g, '_')
async function readCache(file: string): Promise<any | null> {
  const r = await ssh(`cat ${CACHE_DIR}/${file} 2>/dev/null`)
  if (r.code !== 0 || !r.out.trim()) return null
  try { return JSON.parse(r.out) } catch { return null }
}
async function writeCache(file: string, obj: any): Promise<void> {
  await ssh(`mkdir -p ${CACHE_DIR} && cat > ${CACHE_DIR}/${file}`, JSON.stringify(obj))
}

const router = Router()

// The LOCAL nemotron model — identical descriptor to src/lib/models.json's default chat model,
// resolved by getModelClient to createOpenAI({ baseURL: OLLAMA_BASE_URL, apiKey:'ollama' }).chat(id),
// exactly as server/routes/chat.ts does. The model runs on box A (ed9f) via OLLAMA_BASE_URL.
const NEMO_DISPLAY = 'Someo Park Local Model 120B'  // customer-facing name
const NEMO: LLMModel = {
  id: 'nemotron-3-super:120b',
  name: 'Someo Park Local Model 120B',
  provider: 'Ollama',
  providerId: 'ollama',
}

const LANGS: Record<string, string> = { en: 'English', zh: 'Chinese', ja: 'Japanese', fr: 'French', es: 'Spanish' }

// Strip nemotron <think>…</think> reasoning leakage (copied from chat.ts sanitizeChatText).
function sanitize(s: string): string {
  if (!s) return s
  let t = s.replace(/<think>[\s\S]*?<\/think>/gi, '')
  const close = t.lastIndexOf('</think>')
  if (close !== -1) t = t.slice(close + '</think>'.length)
  return t.trim() || s.trim()
}

function loadMatchup(id: string): any | null {
  if (!existsSync(INDEX)) return null
  const doc = JSON.parse(readFileSync(INDEX, 'utf8'))
  return (doc.matchups || []).find((m: any) => m.id === id) || null
}

// DFM diffusion-amplification snapshot for a matchup (dfm_index.json is a {matchups:{id:{…}}} map).
function loadDfm(id: string): any | null {
  if (!existsSync(DFM_INDEX)) return null
  const doc = JSON.parse(readFileSync(DFM_INDEX, 'utf8'))
  return (doc.matchups || {})[id] || null
}

const pct = (x: number) => `${Math.round((x || 0) * 100)}%`

function aggregatePrompts(m: any, langLine: string) {
  const a = m.aggregate, H = m.home_name, A = m.away_name
  const dist = (a.score_distribution || []).slice(0, 6).map((d: any) => `${d.score}×${d.count}`).join(', ')
  const system =
    `You are a professional football match analyst. You are given the AGGREGATE of ${m.n_sims} ` +
    `independent AI match simulations for one fixture (win/draw/loss counts, implied probabilities, ` +
    `average xG, possession, shots, and the score distribution). Write ONE concise paragraph: the most ` +
    `likely outcome and its implied probability, the expected scoreline, and the single most important ` +
    `tactical reason. Use ONLY the numbers provided; do not invent statistics. ${langLine}`
  const user =
    `Fixture: ${H} (home) vs ${A} (away)\n` +
    `Simulations: ${m.n_sims}\n` +
    `Record (${H} win / draw / ${A} win): ${a.record.home_wins} / ${a.record.draws} / ${a.record.away_wins}\n` +
    `Implied win probability: ${H} ${pct(a.win_pct.home)}, draw ${pct(a.win_pct.draw)}, ${A} ${pct(a.win_pct.away)}\n` +
    `Average xG: ${H} ${a.avg_xg.home}, ${A} ${a.avg_xg.away}\n` +
    `Average possession: ${H} ${a.avg_possession.home}%, ${A} ${a.avg_possession.away}%\n` +
    `Average shots: ${H} ${a.avg_shots.home}, ${A} ${a.avg_shots.away}\n` +
    `Average scoreline: ${H} ${a.avg_score.home} - ${a.avg_score.away} ${A}\n` +
    `Score distribution (score×count): ${dist}`
  return { system, user }
}

function simPrompts(m: any, sim: any, langLine: string) {
  const H = m.home_name, A = m.away_name, sh = sim.stats.home, sa = sim.stats.away
  const tac = (t: any) => t ? `directness ${t.directness}, press ${t.press_intensity}, tempo ${t.tempo}, note "${t.tactical_note || ''}"` : 'n/a'
  const system =
    `You are a professional football analyst writing a brief post-match report for ONE simulated match. ` +
    `Given the final score, possession, shots, xG and each side's tactical setup, write ONE concise paragraph: ` +
    `who controlled the game, whether the result matched the run of play (compare xG to goals), and the key ` +
    `tactical factor. Use ONLY the numbers provided. ${langLine}`
  const user =
    `Match: ${H} ${sim.score.home} - ${sim.score.away} ${A} (simulation, 90-minute equivalent)\n` +
    `Possession: ${H} ${sh.possession_pct}%, ${A} ${sa.possession_pct}%\n` +
    `Shots (on-target%): ${H} ${sh.shots} (${sh.shots_on_target_pct}%), ${A} ${sa.shots} (${sa.shots_on_target_pct}%)\n` +
    `xG: ${H} ${sh.xg}, ${A} ${sa.xg}\n` +
    `Passes (completion%): ${H} ${sh.passes} (${sh.pass_completion_pct}%), ${A} ${sa.passes} (${sa.pass_completion_pct}%)\n` +
    `${H} tactics: ${tac(sim.tactics?.home)}\n` +
    `${A} tactics: ${tac(sim.tactics?.away)}\n` +
    (sim.reasoning ? `Pre-match setup: ${sim.reasoning}` : '')
  return { system, user }
}

// DFM diffusion-amplification analysis: summarise the 5000-sample distribution (one of the two
// views — real_anchored / engine_faithful). `d` is the dfm_index matchup entry; `view` is d[mode].
function dfmPrompts(d: any, view: any, mode: string, langLine: string) {
  const H = d.home, A = d.away
  const p50 = (ch: string, side: 'home' | 'away', mul = 1, fix = 1) => {
    const s = view.stats?.[ch]?.[side]
    return s ? (s.p50 * mul).toFixed(fix) : 'n/a'
  }
  const dist = (view.scoreline_top10 || []).slice(0, 6).map((l: any) => `${l.score} ${pct(l.pct)}`).join(', ')
  const viewName = mode === 'engine_faithful'
    ? 'engine-faithful (the amplified distribution of the source sims as-is)'
    : 'real-anchored (rescaled to tournament-level intensity + calibrated cards — for real-match prediction)'
  const system =
    `You are a professional football match analyst. You are given a DIFFUSION-MODEL amplification ` +
    `of ${d.n_source_sims} AI match simulations into ${d.n_samples} synthetic matches for one fixture ` +
    `(the ${viewName} view). You get the win/draw/loss probabilities, expected scoreline, the score ` +
    `distribution, and median per-team possession, shots, corners, xG and cards. Write ONE concise ` +
    `paragraph: the most likely outcome and its probability, the expected scoreline, and the single ` +
    `most important statistical edge driving it. Use ONLY the numbers provided; do not invent stats. ${langLine}`
  const user =
    `Fixture: ${H} (home) vs ${A} (away)\n` +
    `Diffusion samples: ${d.n_samples} (from ${d.n_source_sims} sims) — ${viewName}\n` +
    `Win probability: ${H} ${pct(view.wdl.home)}, draw ${pct(view.wdl.draw)}, ${A} ${pct(view.wdl.away)}\n` +
    `Expected scoreline: ${H} ${(view.avg_goals || '').replace('-', ' - ')} ${A}\n` +
    `Top scorelines: ${dist}\n` +
    `Median possession: ${H} ${p50('poss_share', 'home', 100, 0)}%, ${A} ${p50('poss_share', 'away', 100, 0)}%\n` +
    `Median shots: ${H} ${p50('shots', 'home', 1, 0)}, ${A} ${p50('shots', 'away', 1, 0)}\n` +
    `Median corners: ${H} ${p50('corners', 'home', 1, 0)}, ${A} ${p50('corners', 'away', 1, 0)}\n` +
    `Median xG: ${H} ${p50('xg', 'home')}, ${A} ${p50('xg', 'away')}\n` +
    `Cards per match: ${view.cards_per_match?.yellow} yellow, ${view.cards_per_match?.red} red`
  return { system, user }
}

// POST /api/microfootball/analyze  { matchup_id, sim_id?, lang?, mode?, dfm_mode? }
// On-demand only — the local model is slow; the frontend calls one request at a time.
router.post('/analyze', async (req: Request, res: Response) => {
  try {
    const { matchup_id, sim_id, lang, mode, dfm_mode } = req.body || {}
    if (!matchup_id) return res.status(400).json({ error: 'matchup_id required' })

    const langLine = `Answer in ${LANGS[lang as string] || 'English'}.`
    let prompts, hashInput: string, slot: string
    if (mode === 'dfm') {
      // DFM diffusion-amplification analysis (reads dfm_index.json, not the sim index).
      const d = loadDfm(matchup_id)
      if (!d) return res.status(404).json({ error: `dfm matchup not found: ${matchup_id}` })
      const dm = dfm_mode === 'engine_faithful' ? 'engine_faithful' : 'real_anchored'
      const view = d[dm] || d.real_anchored
      prompts = dfmPrompts(d, view, dm, langLine)
      // content-hash the snapshot so a NEW DFM run (or view toggle) regenerates; else the cache holds.
      hashInput = JSON.stringify({ ts: d.ts, mode: dm, wdl: view.wdl, avg_goals: view.avg_goals,
        top: (view.scoreline_top10 || []).slice(0, 6), cards: view.cards_per_match })
      slot = `dfm_${dm}`
    } else {
      const m = loadMatchup(matchup_id)
      if (!m) return res.status(404).json({ error: `matchup not found: ${matchup_id}` })
      if (sim_id) {
        const sim = (m.sims || []).find((s: any) => s.sim_id === sim_id)
        if (!sim) return res.status(404).json({ error: `sim not found: ${sim_id}` })
        prompts = simPrompts(m, sim, langLine)
        hashInput = JSON.stringify({ sim: sim.sim_id, score: sim.score, stats: sim.stats })
        slot = safe(sim_id)
      } else {
        prompts = aggregatePrompts(m, langLine)
        hashInput = JSON.stringify({ n: m.n_sims, agg: m.aggregate })   // changes if the n sims change
        slot = 'agg'
      }
    }
    const contentHash = createHash('sha256').update(hashInput).digest('hex').slice(0, 16)
    const file = `${safe(matchup_id)}__${slot}__${safe(lang || 'en')}.json`

    // Cache hit (on ed9f): < 7 days old AND content unchanged → skip the (slow) model.
    const cached = await readCache(file)
    if (cached && cached.content_hash === contentHash && cached.generated_at &&
        (Date.now() - new Date(cached.generated_at).getTime()) < TTL_MS) {
      return res.json({ analysis: cached.analysis, matchup_id, sim_id: sim_id || null, model: NEMO_DISPLAY, cached: true })
    }

    // Miss → call the model (identical shape to chat.ts:219), then cache on ed9f (best-effort).
    const modelClient = getModelClient(NEMO, {})
    const { text } = await generateText({
      model: modelClient as LanguageModel,
      system: prompts.system,
      messages: [{ role: 'user', content: prompts.user }],
      maxOutputTokens: 1024,
    })
    const analysis = sanitize(text)
    writeCache(file, { matchup_id, sim_id: sim_id || null, lang: lang || 'en', content_hash: contentHash, analysis, generated_at: new Date().toISOString() }).catch(() => {})

    res.json({ analysis, matchup_id, sim_id: sim_id || null, model: NEMO_DISPLAY, cached: false })
  } catch (error: any) {
    console.error('microfootball analyze error:', error?.message || error)
    res.status(500).json({ error: error?.message || 'analysis failed' })
  }
})

export default router
