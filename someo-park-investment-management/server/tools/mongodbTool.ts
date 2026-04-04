// server/tools/mongodbTool.ts
// Data source: server/utils/mongoClient.ts

import { getMongoDb } from '../utils/mongoClient.js'
import type { AgentTool } from './index.js'

export const mongodbTool: AgentTool = {
  definition: {
    name: 'query_mongodb',
    description: 'Query MongoDB database (someopark). Available collections: pairs_day_select (fields: coint_pairs, similar_pairs, pca_pairs, day). Use for historical pair universe data.',
    input_schema: {
      type: 'object',
      properties: {
        collection: { type: 'string', description: 'Collection name, e.g. "pairs_day_select"' },
        filter: { type: 'string', description: 'JSON filter string, e.g. "{}" or "{\\"day\\": \\"2026-04-01\\"}"' },
        projection: { type: 'string', description: 'JSON projection, e.g. "{\\"coint_pairs\\": 1}"' },
        limit: { type: 'number', description: 'Max documents (default 10)' },
        sort: { type: 'string', description: 'JSON sort, e.g. "{\\"day\\": -1}" for newest first' }
      },
      required: ['collection']
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ collection, filter = '{}', projection, limit = 10, sort }) {
    const db = await getMongoDb('someopark')
    let q = db.collection(collection).find(JSON.parse(filter))
    if (projection) q = q.project(JSON.parse(projection))
    if (sort) q = q.sort(JSON.parse(sort))
    return q.limit(limit).toArray()
  }
}
