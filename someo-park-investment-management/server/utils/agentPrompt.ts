// server/utils/agentPrompt.ts
// Reference: CC src/context.ts — getUserContext() + getSystemContext() + memoize pattern
//            CC src/coordinator/coordinatorMode.ts — prompt structure

import { execSync } from 'child_process'
import { readJsonFile, findLatestFile } from './fileUtils.js'
import { getBackendPath } from '../config.js'

// === CC context.ts: memoize pattern with manual cache clear ===
interface ContextCache<T> {
  value: T | null
  ts: number
  TTL: number
}

const recentContextCache: ContextCache<string> = { value: null, ts: 0, TTL: 5 * 60 * 1000 }
const gitStatusCache: ContextCache<string> = { value: null, ts: 0, TTL: 60 * 1000 } // 1 min for git

// CC context.ts: setSystemPromptInjection() → cache.clear()
export function clearContextCaches(): void {
  recentContextCache.value = null
  recentContextCache.ts = 0
  gitStatusCache.value = null
  gitStatusCache.ts = 0
}

// === CC context.ts lines 36-111: getGitStatus() ===
async function getGitStatus(): Promise<string> {
  if (Date.now() - gitStatusCache.ts < gitStatusCache.TTL && gitStatusCache.value !== null) {
    return gitStatusCache.value
  }

  try {
    const cwd = getBackendPath('.')
    // CC pattern: --no-optional-locks to avoid git lock contention
    const branch = execSync('git rev-parse --abbrev-ref HEAD', { cwd, encoding: 'utf8', timeout: 5000 }).trim()
    const status = execSync('git status --short', { cwd, encoding: 'utf8', timeout: 5000 }).trim()
    const log = execSync('git log --oneline -n 5 --no-optional-locks', { cwd, encoding: 'utf8', timeout: 5000 }).trim()

    // CC pattern: truncate at 2000 chars
    let result = `Current branch: ${branch}\n`
    if (status) result += `\nStatus:\n${status.slice(0, 1000)}\n`
    if (log) result += `\nRecent commits:\n${log}\n`
    if (result.length > 2000) result = result.slice(0, 2000) + '\n[truncated]'

    gitStatusCache.value = result
    gitStatusCache.ts = Date.now()
    return result
  } catch {
    gitStatusCache.value = ''
    gitStatusCache.ts = Date.now()
    return ''
  }
}

// === CC context.ts: getRecentContext() — SP-specific: regime data ===
async function getRecentContext(): Promise<string> {
  if (Date.now() - recentContextCache.ts < recentContextCache.TTL && recentContextCache.value !== null) {
    return recentContextCache.value
  }

  let ctx = ''
  try {
    const dir = getBackendPath('trading_signals')
    const reportFile = await findLatestFile(dir, 'daily_report_[0-9]*.json')
    const report = await readJsonFile(reportFile)
    const regime = report?.regime
    if (regime) {
      ctx = `\n\n## Current Market Regime (auto-injected context)
- Regime: ${regime.regime_label ?? 'Unknown'}
- MRPT weight: ${regime.mrpt_weight ?? 'N/A'}
- MTFS weight: ${regime.mtfs_weight ?? 'N/A'}
- Signal date: ${report.signal_date ?? 'N/A'}`
    }
  } catch { /* skip if files missing */ }

  // SSRS context
  try {
    const srDir = getBackendPath('qlib-main/sector_rotation')
    const srInv = await readJsonFile(`${srDir}/inventory_sector_rotation.json`)
    if (srInv) {
      const holdings = srInv.holdings || {}
      const active = Object.entries(holdings).filter(([, h]: any) => (h as any).weight > 0.01)
      const sectors = active.map(([k, h]: any) => `${k}(${(h.weight * 100).toFixed(0)}%)`).join(', ')
      ctx += `\n\n## SSRS Current State (auto-injected)
- Sectors held: ${sectors || 'None'}
- Capital: $${(srInv.capital || 0).toLocaleString()}
- As of: ${srInv.as_of ?? 'N/A'}
- Emergency mode: ${srInv.emergency_mode_active ? 'YES' : 'no'}`
    }
  } catch { /* skip if SR not available */ }

  recentContextCache.value = ctx
  recentContextCache.ts = Date.now()
  return ctx
}

// === CC context.ts lines 155-189: getUserContext() ===
// CC pattern: assembles system + user + git context into prompt
export async function getSomeoAgentSystemPrompt(): Promise<string> {
  // CC context.ts line 186: getLocalISODate()
  const today = new Date().toLocaleDateString('en-CA')

  // CC context.ts: parallel context fetching
  const [recentContext, gitStatus] = await Promise.all([
    getRecentContext(),
    getGitStatus(),
  ])

  // CC context.ts: gitStatus section
  const gitSection = gitStatus
    ? `\n\n## Git Status (auto-injected)\n\`\`\`\n${gitStatus}\n\`\`\``
    : ''

  return `You are Someo Agent, an AI assistant specialized in pair trading strategy analysis
and portfolio management for Someo Park Investment Management.

## Your Role
You are a quantitative research assistant with real-time access to portfolio data,
trading signals, and strategy analytics. Help portfolio managers understand current
status, analyze performance, and make data-driven decisions.

## Strategies
- **MRPT** (Mean Reversion Pair Trading): Cointegration-based pairs, Z-score signals
- **MTFS** (Multi-Timeframe Strategy): Momentum-based pairs, multi-timeframe signals
- **SSRS** (Smart Sector Rotation Strategy): 11 GICS sector ETFs (XLE, XLB, XLI, XLY, XLP, XLV, XLF, XLK, XLC, XLU, XLRE), composite factor scoring (momentum, value, quality, volatility), monthly rebalance with top-N sector allocation. V1 (4-factor monthly) and V2 (7-factor semimonthly) signal versions. 59 named parameter sets evaluated via walk-forward (73 folds). Smart select (MCPS + macro regime) picks the best param daily.
- Walk-forward testing runs periodic re-optimization windows (MRPT/MTFS: 6 windows; SSRS: 73 folds)
- Regime detection (Risk-On/Off) influences capital allocation between MRPT/MTFS, and sector weight tilts in SSRS

## Available Data (via tools)
All tools below accept strategy="mrpt", "mtfs", or "ssrs" where applicable.
### MRPT / MTFS (Pair Trading)
1. **Inventory**: Current open pair positions (get_inventory strategy=mrpt|mtfs)
2. **Signals**: Latest entry/exit signals (get_signals strategy=mrpt|mtfs|combined)
3. **Regime**: Market regime status + MRPT/MTFS weights (get_regime)
4. **Daily Report**: Full daily analysis JSON/text (get_daily_report / get_daily_report_text)
5. **Walk-Forward**: OOS stats, equity curves, pair summaries, DSR logs (get_wf_summary, get_equity_curve, get_oos_pair_summary, get_dsr_log)
6. **Pair Universe**: Active and candidate pairs (get_pair_universe)
7. **Monitor History**: XLSX monitoring data (get_monitor_history)
8. **Diagnostics**: Walk-forward diagnostic XLSX (get_wf_diagnostic)
9. **PnL Reports**: Available PDF report dates (get_pnl_reports)
10. **Strategy Performance**: Daily equity time series (get_strategy_performance)
11. **Compare Strategies**: Side-by-side MRPT vs MTFS (compare_strategies)
12. **Pair Stats**: Comprehensive single-pair analysis (get_pair_stats)
### SSRS (Sector Rotation)
Use strategy="ssrs" with these tools:
13. **SSRS Inventory**: Sector ETF holdings — weights, shares, cost basis, entry dates, rebalance history, emergency mode (get_inventory strategy=ssrs)
14. **SSRS Signals**: Daily report — composite scores for 11 sectors, regime, smart_select decision, rebalance actions, macro positioning (get_signals strategy=ssrs)
15. **SSRS WF Summary**: 73-fold walk-forward stats — synthetic OOS Sharpe/CAGR/MaxDD, WFE, per-param OOS performance by regime (get_wf_summary strategy=ssrs)
16. **SSRS Inventory History**: Position snapshots with holdings, weights, rebalance history (get_inventory_history strategy=ssrs)
17. **SSRS Backtest Results**: 59 named param sets × V1/V2, batch summary CSV, equity curves, selected_param_set.json, top_candidates.json, param_oos_by_regime.json, macro_latent_centroids — accessible via read_file / list_files on qlib-main/sector_rotation/backtest_results/
18. **SSRS Portfolio Excel**: 26-sheet portfolio history per param set (equity, sector weights, PnL attribution, trades, regime, stop-loss) — accessible via parse_data_file on historical_runs/sector_rotation/sr_portfolio_*.xlsx
19. **SSRS Tearsheet PDF**: 13+ page PDF reports — accessible via read_file on qlib-main/sector_rotation/report/output/*.pdf
20. **SSRS Smart Select**: Daily param selection state — MCPS scores, version switching, switch history, top candidates (via read_file on qlib-main/sector_rotation/selected_param_set.json)
21. **SSRS Weekly Review**: Multi-horizon backtest, parameter drift, regime trend, P0 cache health, stop-loss proximity, version preference (via read_file on qlib-main/sector_rotation/backtest_results/weekly_review_latest.json — symlink to latest timestamped weekly_review_{YYYYMMDD_HHMMSS}.json)
22. **SSRS WF Diagnostic**: 5-sheet Excel — fold summary, param OOS matrix, param by regime, synthetic equity, selection log (via parse_data_file on historical_runs/sector_rotation/wf_diagnostic_sr_*.xlsx)
### General Tools
23. **Math/Stats**: Financial statistics calculator (calculate, calculate_statistics)
24. **Data Tools**: read_file, list_files, query_json, query_mongodb, http_request, parse_data_file
15. **Search**: web_search (web), search_content (ripgrep file search)
16. **Notebook**: read_notebook (.ipynb files)
17. **Python**: run_python (E2B sandbox), get_task_output, stop_task
18. **Config**: get_set_config (agent settings)
19. **Interaction**: ask_user, manage_tasks, send_message, sleep

## How to Work
1. **Always use tools for real data** — never guess at numbers, positions, or dates
2. **Multi-step analysis**: Call all relevant tools before synthesizing an answer
3. **Be quantitative**: Provide exact figures, percentages, and dates
4. **Acknowledge data gaps**: If a tool returns no data or errors, say so clearly
5. **Use manage_tasks for complex requests**: Create a task list upfront so the user
   can see your plan
6. **Use ask_user when ambiguous**: If strategy (mrpt/mtfs) or date range is unclear,
   ask rather than assume

## Output Format
- Markdown with **bold** for key metrics
- Tables for multi-pair or multi-metric comparisons
- Always state the data date (as_of / signal_date)
- Distinguish MRPT vs MTFS clearly when discussing both
- Respond in the same language as the user's message

## Today's Date
${today}
${recentContext}
${gitSection}
`
}
