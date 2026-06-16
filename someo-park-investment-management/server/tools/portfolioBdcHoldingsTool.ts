// server/tools/portfolioBdcHoldingsTool.ts
// Data source: public/data/bdc_lookthrough_latest.json
// Produced daily by the repo-root RunBDCLookThrough.py (the BDC look-through pipeline).
//
// Read-only, file-backed (NOT via pythonBridge) so it answers instantly and is not
// bound by the 60–120s execSync limit — the heavy per-deal modelling is precomputed
// offline; this tool just serves the latest aggregation.

import { readJsonFile } from '../utils/fileUtils.js'
import path from 'path'
import { fileURLToPath } from 'url'
import type { AgentTool } from './index.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

export const portfolioBdcHoldingsTool: AgentTool = {
  definition: {
    name: 'portfolio_bdc_holdings',
    description:
      "Look through the BDC sleeve (GBDC/TSLX/OBDC/BXSL/ARCC) to the underlying private-credit " +
      "loans disclosed in each BDC's SEC Schedule of Investments. Returns the sleeve-level " +
      "aggregation: top issuers (cross-BDC), sector exposure, weighted spread / all-in / IRR / " +
      "credit score, PIK share, non-accrual share, mark distribution, per-BDC summary, and " +
      "credit-quality early warnings. Use `view` to pick a slice. Data is the latest disclosed " +
      "quarter (BDC holdings are quarterly; the JSON carries each BDC's as-of date).",
    input_schema: {
      type: 'object',
      properties: {
        view: {
          type: 'string',
          enum: ['summary', 'top_issuers', 'sector_exposure', 'by_bdc', 'early_warning'],
          description: "Which slice to return (default 'summary' = everything except the long issuer list)."
        },
        top_n: { type: 'number', description: 'For top_issuers: how many issuers (default 25).' }
      },
      required: []
    }
  },
  isConcurrencySafe: () => true,
  isReadOnly: () => true,
  async execute({ view = 'summary', top_n }: { view?: string; top_n?: number }) {
    const filePath = path.resolve(__dirname, '..', '..', 'public', 'data', 'bdc_lookthrough_latest.json')
    let data: any
    try {
      data = await readJsonFile(filePath)
    } catch {
      return { error: 'bdc_lookthrough_latest.json not found — the BDC look-through pipeline has not run yet.' }
    }
    if (!data || typeof data !== 'object') return { error: 'malformed bdc_lookthrough_latest.json' }

    const head = {
      as_of: data.as_of, rates_date: data.rates_date,
      deal_count: data.deal_count, issuer_count: data.issuer_count,
      sleeve_alloc: data.sleeve_alloc
    }
    switch (view) {
      case 'top_issuers':
        return { ...head, top_issuers: (data.top_issuers || []).slice(0, top_n && top_n > 0 ? top_n : 25) }
      case 'sector_exposure':
        return { ...head, sector_exposure: data.sector_exposure }
      case 'by_bdc':
        return { ...head, by_bdc: data.by_bdc, manifest: data.manifest }
      case 'early_warning':
        return { ...head, early_warning: data.early_warning, mark_distribution: data.mark_distribution }
      default:
        return {
          ...head,
          weighted: data.weighted,
          mark_distribution: data.mark_distribution,
          sector_exposure: data.sector_exposure,
          by_bdc: data.by_bdc,
          early_warning: data.early_warning,
          top_issuers: (data.top_issuers || []).slice(0, 10)
        }
    }
  }
}
