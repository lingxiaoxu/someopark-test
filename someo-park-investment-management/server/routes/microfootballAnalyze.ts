import { Router, Request, Response } from 'express'
import { generateText, LanguageModel } from 'ai'
import { getModelClient, LLMModel } from '../utils/models.js'
import path from 'path'
import { fileURLToPath } from 'url'
import { readFileSync, existsSync } from 'fs'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const INDEX = path.join(__dirname, '..', '..', 'public', 'data', 'microfootball_index.json')

const router = Router()

// The LOCAL nemotron model — identical descriptor to src/lib/models.json's default chat model,
// resolved by getModelClient to createOpenAI({ baseURL: OLLAMA_BASE_URL, apiKey:'ollama' }).chat(id),
// exactly as server/routes/chat.ts does. The model runs on box A (ed9f) via OLLAMA_BASE_URL.
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

// POST /api/microfootball/analyze  { matchup_id, sim_id?, lang? }
// On-demand only — the local model is slow; the frontend calls one request at a time.
router.post('/analyze', async (req: Request, res: Response) => {
  try {
    const { matchup_id, sim_id, lang } = req.body || {}
    if (!matchup_id) return res.status(400).json({ error: 'matchup_id required' })
    const m = loadMatchup(matchup_id)
    if (!m) return res.status(404).json({ error: `matchup not found: ${matchup_id}` })

    const langLine = `Answer in ${LANGS[lang as string] || 'English'}.`
    let prompts
    if (sim_id) {
      const sim = (m.sims || []).find((s: any) => s.sim_id === sim_id)
      if (!sim) return res.status(404).json({ error: `sim not found: ${sim_id}` })
      prompts = simPrompts(m, sim, langLine)
    } else {
      prompts = aggregatePrompts(m, langLine)
    }

    // Identical call shape to server/routes/chat.ts:219 (the default /v1 generateText path).
    const modelClient = getModelClient(NEMO, {})
    const { text } = await generateText({
      model: modelClient as LanguageModel,
      system: prompts.system,
      messages: [{ role: 'user', content: prompts.user }],
      maxOutputTokens: 1024,
    })

    res.json({ analysis: sanitize(text), matchup_id, sim_id: sim_id || null, model: NEMO.id })
  } catch (error: any) {
    console.error('microfootball analyze error:', error?.message || error)
    res.status(500).json({ error: error?.message || 'analysis failed' })
  }
})

export default router
