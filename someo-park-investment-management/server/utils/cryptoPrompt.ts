// Crypto-only additions to the conversational prompt. The domain-neutral coding
// prompt in prompt.ts is deliberately not imported or changed here.
import models from '../../src/lib/models.json'
import type { LLMModel } from './models.js'
import type { ArtifactTrigger } from './artifactDetector.js'
import { cryptoContextForArtifacts } from '../tools/cryptoMarketTool.js'
import { resolveCryptoScope } from '../tools/cryptoTriggers.js'

export function cryptoModelIdentity(model: Pick<LLMModel, 'id' | 'providerId'>): string {
  const known = models.find(m => m.id === model?.id && m.providerId === model?.providerId)
  const id = typeof model?.id === 'string' && /^[a-z0-9._:/-]{1,120}$/i.test(model.id)
    ? model.id : 'unverified'
  const provider = ['ollama', 'anthropic'].includes(model?.providerId) ? model.providerId : 'unverified'
  return `\n\n## Current chat model (request routing metadata)
Catalog display name for this model: ${JSON.stringify(known?.name ?? 'Not listed in the built-in model catalog')}.
Routed model ID: ${JSON.stringify(id)}. Provider adapter: ${JSON.stringify(provider)}.
When asked which model you use, distinguish the application's display name from the underlying
model ID above. A Someo Park display name is an alias, not evidence that Someo Park trained the
foundation model. Do not identify the chat model from football simulations or trading strategies.
This metadata describes this request's model selection, not an independent audit of the model server.`
}

export function cryptoModulePrompt(agent = false): string {
  return `\n\n## Crypto prediction-market knowledge — current application mode
The user is in the Crypto Prediction Markets module, with its own FAVE / PFME strategy selector.
In this module Kalshi, binary markets, positions, performance and health refer to CRYPTO contracts;
do not substitute World Cup, club soccer, macro or stock-strategy records for missing crypto data.
- FAVE (Favorite Value Entry, internal W7): 15-minute binary contracts; enter the favorite side
  in the registered price/time band and hold to official settlement. Use the supplied parameters
  and ledger version for the actual current rules.
- PFME (Passive Favorite Market Entry, internal W8): passive favorite-side entry with risk exits.
  The exported paper result is the tilted execution candidate. The paired control arm is a
  separate experiment and is NOT exported here; never use Demo P&L as its result or add the arms.
- Strategy evaluation uses the PAPER ledger as the primary basis. Kalshi DEMO is an independent
  execution mirror; partial/unknown fills and costs stay partial/unknown. Prod quotes are READ-ONLY
  market data, not Prod positions or realized P&L. Prod trading is not connected in this data contract.

The data views mirror the dashboard: performance (收益与回撤), orders (订单与成交), positions
(当前持仓), settlements (结算账本), execution (执行质量), markets (二元市场), rules (策略规则与验证),
health (运行与数据健康). Every view is scoped to FAVE and/or PFME. Orders, positions and settlement
rows currently come from Demo; paper performance has its own independent curve and sample counts.
Always keep source timestamps, ledger scope, fees, unresolved records and version caveats. A fresh
snapshot does not make old source data fresh. Recent gaps or 429 events are not proof of current
downtime; a source-hash warning must not be dismissed because the strategy has positive P&L.
${agent
    ? `Use get_crypto_market for the appropriate view, get_crypto_contract for an EXACT contract ticker,
and get_crypto_track_record for paginated paper or Demo result curves (Demo settlement rows use
get_crypto_market view=settlements). These are read-only skills,
not order tools. Fetch the relevant data before making numeric claims; state any missing scope plainly.`
    : `The relevant crypto data is INJECTED BELOW from the SAME snapshot the panel reads. Answer ONLY
from it in ordinary prose; do not emit tool calls, JSON or <think> tags because they do not execute
in normal chat. If a needed value or the paired control is absent, say it cannot be verified from
this source and name the relevant view rather than inventing figures.`}`
}

export async function cryptoChatGrounding(
  text: string, artifacts: ArtifactTrigger[], model: Pick<LLMModel, 'id' | 'providerId'>,
  agent = false,
): Promise<string> {
  const instructions = cryptoModulePrompt(agent) + cryptoModelIdentity(model)
  try {
    const context = await cryptoContextForArtifacts(
      artifacts.map(a => a.type).filter(t => t.startsWith('crypto_')),
      resolveCryptoScope(text),
    )
    return instructions + '\n\n' + context
  } catch {
    // No source substitution: crypto remains the active module even if its
    // data is unreadable, rather than falling back into another module's facts.
    return instructions + '\n\nCrypto data is unavailable. State that current figures could not be verified; do not invent zero positions, P&L or a healthy runtime.'
  }
}
