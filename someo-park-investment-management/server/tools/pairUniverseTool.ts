// server/tools/pairUniverseTool.ts
// Data source: server/routes/pairUniverse.ts

import { readJsonFile } from '../utils/fileUtils.js'
import { getBackendPath } from '../config.js'
import { getMongoDb } from '../utils/mongoClient.js'
import type { AgentTool } from './index.js'

export const pairUniverseTool: AgentTool = {
  definition: {
    name: 'get_pair_universe',
    description: `Get pair universe data. Two sources:
1. source="selected" (default): pairs currently active (from pair_universe_{strategy}.json)
2. source="coint"|"similar"|"pca": candidate pools from MongoDB pairs_day_select
For selected: returns list with s1, s2, hedge_ratio, coint_pvalue.
For candidates: returns latest snapshot with selected/unselected marked.`,
    input_schema: {
      type: 'object',
      properties: {
        strategy: {
          type: 'string',
          description: 'Strategy: "mrpt" or "mtfs"',
          enum: ['mrpt', 'mtfs']
        },
        source: {
          type: 'string',
          description: 'Data source (default: "selected")',
          enum: ['selected', 'coint', 'similar', 'pca']
        }
      },
      required: ['strategy']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ strategy, source = 'selected' }) {
    if (source === 'selected') {
      const filePath = getBackendPath(`pair_universe_${strategy}.json`)
      return readJsonFile(filePath)
    }

    const fieldMap: Record<string, string> = { coint: 'coint_pairs', similar: 'similar_pairs', pca: 'pca_pairs' }
    const field = fieldMap[source]
    if (!field) throw new Error(`Invalid source: ${source}`)

    const db = await getMongoDb('someopark')
    const doc = await db.collection('pairs_day_select')
      .find({}, { projection: { day: 1, [field]: 1 } })
      .sort({ day: -1 }).limit(1).toArray()
    if (doc.length === 0) return { day: null, pairs: [], total: 0 }

    let selectedSet = new Set<string>()
    try {
      const mrpt = await readJsonFile(getBackendPath('pair_universe_mrpt.json'))
      const mtfs = await readJsonFile(getBackendPath('pair_universe_mtfs.json'))
      ;[...mrpt, ...mtfs].forEach((p: any) => {
        selectedSet.add(`${p.s1}/${p.s2}`)
        selectedSet.add(`${p.s2}/${p.s1}`)
      })
    } catch { /* ignore if files missing */ }

    const pairs = (doc[0][field] || []).map((p: any) => {
      const s1 = Array.isArray(p) ? p[0] : (p.s1 || p[0])
      const s2 = Array.isArray(p) ? p[1] : (p.s2 || p[1])
      return { s1, s2, pair: `${s1}/${s2}`, selected: selectedSet.has(`${s1}/${s2}`) }
    })
    return { day: doc[0].day, source, pairs, total: pairs.length }
  }
}
