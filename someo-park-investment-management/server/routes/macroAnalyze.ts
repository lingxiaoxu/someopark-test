import { Router, Request, Response } from 'express'
import { generateText, LanguageModel } from 'ai'
import { getModelClient, LLMModel } from '../utils/models.js'
import { createHash } from 'crypto'
import { macroContextForArtifacts, MACRO_ARTIFACT_TYPES } from '../tools/macroMarketTool.js'

// On-demand LOCAL-nemotron analysis of one macro view. Mirrors
// server/routes/microfootballAnalyze.ts: same model descriptor, same sanitize
// helper, one slow call at a time (requests are serialized server-side), and a
// content-hashed cache so unchanged data doesn't re-run the slow model. The cache
// here is in-memory (macro data is tiny + regenerates daily) instead of the
// microfootball ssh cache on ed9f.

const router = Router()

// Identical descriptor to microfootballAnalyze / src/lib/models.json's default chat model.
const NEMO_DISPLAY = 'Someo Park Local Model 120B'  // customer-facing name
const NEMO: LLMModel = {
  id: 'nemotron-3-super:120b',
  name: 'Someo Park Local Model 120B',
  provider: 'Ollama',
  providerId: 'ollama',
}

const LANGS: Record<string, string> = { en: 'English', zh: 'Chinese', ja: 'Japanese', fr: 'French', es: 'Spanish' }

// Strip nemotron <think>…</think> reasoning leakage (copied from microfootballAnalyze/chat.ts).
function sanitize(s: string): string {
  if (!s) return s
  let t = s.replace(/<think>[\s\S]*?<\/think>/gi, '')
  const close = t.lastIndexOf('</think>')
  if (close !== -1) t = t.slice(close + '</think>'.length)
  return t.trim() || s.trim()
}

// In-memory cache: view+lang → { content_hash, analysis, generated_at }.
const TTL_MS = 6 * 60 * 60 * 1000
const cache = new Map<string, { content_hash: string; analysis: string; generated_at: number }>()

// ONE in-flight model call at a time — the local model is slow; serialize requests.
let inflight: Promise<void> = Promise.resolve()

const VALID_VIEWS = new Set<string>(MACRO_ARTIFACT_TYPES as readonly string[])

function prompts(view: string, ctx: string, langLine: string) {
  const system =
    'You are a professional macroeconomic markets analyst for a Kalshi macro prediction-market ' +
    'paper-trading system (Fed decisions, CPI/PCE inflation, jobless claims, payrolls, unemployment). ' +
    'You are given the authoritative data block behind ONE dashboard view. Write ONE concise, ' +
    'plain-prose summary (a short paragraph or a few tight bullet points): the headline numbers, ' +
    'what the model currently expects, any open decisions/edges worth noting, and — if present — ' +
    'whether the out-of-sample gate keeps the system in paper mode. Use ONLY the numbers provided; ' +
    `do not invent figures, and say so if a value is absent. ${langLine}`
  const user = `Dashboard view: ${view}\n\n${ctx}`
  return { system, user }
}

// POST /api/macro/analyze  { view, lang? }
// On-demand only — the local model is slow; the frontend calls one request at a time.
router.post('/analyze', async (req: Request, res: Response) => {
  try {
    const { view, lang } = req.body || {}
    if (!view) return res.status(400).json({ error: 'view required' })
    if (!VALID_VIEWS.has(view)) return res.status(400).json({ error: `unknown view: ${view}` })

    const ctx = await macroContextForArtifacts([view])
    if (!ctx) return res.status(404).json({ error: `no data available for view: ${view}` })

    const langKey = LANGS[lang as string] ? (lang as string) : 'en'
    const langLine = `Answer in ${LANGS[langKey]}.`
    const contentHash = createHash('sha256').update(ctx + '|' + langKey).digest('hex').slice(0, 16)
    const cacheKey = `${view}__${langKey}`

    // Cache hit: content unchanged AND fresh → skip the slow model.
    const hit = cache.get(cacheKey)
    if (hit && hit.content_hash === contentHash && Date.now() - hit.generated_at < TTL_MS) {
      return res.json({ analysis: hit.analysis, view, model: NEMO_DISPLAY, cached: true })
    }

    // Miss → call the model (identical shape to microfootballAnalyze), serialized.
    const p = prompts(view, ctx, langLine)
    let analysis = ''
    const run = inflight.then(async () => {
      const modelClient = getModelClient(NEMO, {})
      const { text } = await generateText({
        model: modelClient as LanguageModel,
        system: p.system,
        messages: [{ role: 'user', content: p.user }],
        maxOutputTokens: 1024,
      })
      analysis = sanitize(text)
    })
    inflight = run.catch(() => {})   // the queue survives a failed call
    await run

    cache.set(cacheKey, { content_hash: contentHash, analysis, generated_at: Date.now() })
    res.json({ analysis, view, model: NEMO_DISPLAY, cached: false })
  } catch (error: any) {
    console.error('macro analyze error:', error?.message || error)
    res.status(500).json({ error: error?.message || 'analysis failed' })
  }
})

export default router
