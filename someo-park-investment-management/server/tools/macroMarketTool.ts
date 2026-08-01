// server/tools/macroMarketTool.ts
// Macro prediction-market data (Kalshi macro paper-trading system: Fed / CPI / PCE /
// jobless claims / payrolls / U3). Data source: public/data/macro_*.json — the SAME
// files the Macro Markets dashboard reads. Mirrors predictionMarketTool's grounding
// approach: detected macro_* artifact types → compact JSON context blocks so the
// non-agent chat answers from the same numbers the user sees on the panel.

import { readJsonFile } from '../utils/fileUtils.js'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const dataDir = path.resolve(__dirname, '..', '..', 'public', 'data')

// The 14 macro artifact types (mirrors MACRO_ITEMS in the frontend grid).
export const MACRO_ARTIFACT_TYPES = [
  'macro_board', 'macro_fed', 'macro_inflation', 'macro_labor', 'macro_energy',
  'macro_divergence', 'macro_decisions', 'macro_performance', 'macro_calibration',
  'macro_coverage', 'macro_risk', 'macro_overview', 'macro_reports',
  'macro_walkforward', 'macro_bets',
] as const

// EN + ZH keyword dictionary for mode-scoped artifact detection (macro mode only).
export const MACRO_KEYWORD_PATTERNS: Array<{ type: string; title: string; keywords: string[] }> = [
  { type: 'macro_board',       title: 'Macro Board',        keywords: ['macro board', 'board', 'next release', 'release schedule', 'upcoming data', 'data calendar', '宏观看板', '看板', '数据日历', '下次发布', '近期数据'] },
  { type: 'macro_fed',         title: 'Fed Meetings',       keywords: ['fed', 'fomc', 'rate decision', 'rate cut', 'rate hike', 'interest rate', 'powell', '美联储', '联储', '议息', '加息', '降息', '利率决议'] },
  { type: 'macro_inflation',   title: 'Inflation',          keywords: ['cpi', 'core cpi', 'pce', 'inflation', 'price index', '通胀', '物价', '核心通胀', '消费者价格'] },
  { type: 'macro_labor',       title: 'Labor Market',       keywords: ['jobless', 'claims', 'initial claims', 'nfp', 'payrolls', 'nonfarm', 'non-farm', 'unemployment', 'u3', 'labor market', 'jobs report', '初请', '失业金', '非农', '就业', '失业率', '劳动力市场'] },
  { type: 'macro_energy',      title: 'Energy',             keywords: ['energy', 'oil price', 'gas price', 'gasoline', 'wti', 'brent', '能源', '油价', '汽油', '原油'] },
  { type: 'macro_divergence',  title: 'Model vs Market',    keywords: ['divergence', 'model vs market', 'gap', 'mispricing', '分歧', '模型vs市场', '模型 vs 市场', '错价', '偏离'] },
  { type: 'macro_decisions',   title: 'Decisions & Marks',  keywords: ['decision', 'decisions', 'trade log', 'marks', 'positions log', '决策', '交易记录', '决策日志', '盯市'] },
  { type: 'macro_performance', title: 'Performance',        keywords: ['performance', 'bankroll', 'pnl', 'p&l', 'unrealized', 'track record', '表现', '盈亏', '资金', '战绩', '未实现'] },
  { type: 'macro_calibration', title: 'Calibration (OOS)',  keywords: ['calibration', 'brier', 'oos', 'out of sample', 'out-of-sample', 'backtest', '校准', '样本外', '回测'] },
  { type: 'macro_coverage',    title: 'Coverage',           keywords: ['coverage', 'covered', 'missed', 'alerts', 'watchdog', '覆盖', '覆盖率', '漏报', '告警'] },
  { type: 'macro_risk',        title: 'Risk Limits',        keywords: ['risk', 'limits', 'exposure', 'scenario', 'max loss', '风险', '风控', '敞口', '限额', '最大损失'] },
  { type: 'macro_overview',    title: 'System Overview',    keywords: ['overview', 'how it works', 'system overview', 'methodology', 'what is this', '总览', '概览', '系统说明', '方法论', '原理'] },
  { type: 'macro_reports',     title: 'Reports',            keywords: ['report', 'reports', 'download', 'pdf', '报告', '下载'] },
  { type: 'macro_walkforward', title: 'Walk-Forward Lab',   keywords: ['walk-forward', 'walkforward', 'wf lab', 'bet log', 'bet history', 'ml selector', 'smart switch', 'favourite line', 'edge line', 'entry lead', '走前', '实验室', 'ML 线', 'ML选注', '智能切换', '大热线', '边际线', '三线', 'bet 历史', '逐注', '30天前上线', '入场提前'] },
  { type: 'macro_bets',        title: "Today's Bets",       keywords: ['today\'s bets', 'todays bets', 'what are we betting', 'current bets', 'open bets', 'next bets', 'bet plan', '今日下注', '下什么', '下注计划', '当前下注', '在场的注', '要下的注', '接下来的bet', '下一个bet'] },
]

// artifact type → the macro_*.json file(s) that back it. Family views (inflation /
// labor / energy) read the board like the frontend does; overview gets a small
// multi-file digest.
const TYPE_TO_FILES: Record<string, string[]> = {
  macro_board:       ['macro_board.json'],
  macro_fed:         ['macro_fed.json'],
  macro_inflation:   ['macro_board.json'],
  macro_labor:       ['macro_board.json'],
  macro_energy:      ['macro_board.json'],
  macro_divergence:  ['macro_divergence.json'],
  macro_decisions:   ['macro_decisions.json'],
  macro_performance: ['macro_performance.json'],
  macro_calibration: ['macro_oos.json'],
  macro_coverage:    ['macro_coverage.json'],
  macro_risk:        ['macro_risk.json'],
  macro_overview:    ['macro_board.json', 'macro_performance.json', 'macro_oos.json'],
  macro_reports:     ['macro_reports.json'],
  macro_walkforward: ['macro_walkforward.json'],
  macro_bets:        ['macro_bets.json'],
}

const ABOUT: Record<string, string> = {
  macro_board:       'next scheduled releases + per-series latest prediction (gmix mu/sigma, empirical quantiles, categorical probs) and latest decision (kind/net_edge/size/note)',
  macro_fed:         'FOMC meeting probabilities per bucket (C26/C25/H0/H25/H26 = cut 26+bp / cut 25bp / hold / hike 25bp / hike 26+bp) + model inputs (core_yoy, du12, rule vs market blend)',
  macro_inflation:   'inflation series (KXCPI, KXCPICORE, KXCPIYOY, KXCPICOREYOY, KXPCECORE) predictions + decisions from the board',
  macro_labor:       'labor series (KXJOBLESSCLAIMS initial claims, KXPAYROLLS nonfarm payrolls, KXU3 unemployment rate) predictions + decisions from the board',
  macro_energy:      'energy series (KXWTIW WTI weekly, KXNATGASW Henry Hub weekly, KXAAAGASW AAA gasoline) — GBM-on-futures model with EIA storage-surprise tilt + AAA daily anchor; predictions + decisions from the board',
  macro_divergence:  'model-vs-market divergence ranking (gap_norm per series+period; > 0.15 is notable)',
  macro_decisions:   'the decision log (enter/exit/pass with fair/ask/net_edge/size/note) + latest mark-to-market per open position',
  macro_performance: 'the betting track record (hybrid rule): track.history = record through 2026-07-31, track.live = production bets from the 2026-07-31 rules go-live (settled + open with unrealized marks), track.combined = the headline W-L/PnL/ROI; plus paper bankroll and mode',
  macro_calibration: 'out-of-sample experiments: Brier model vs market per horizon + the real-money gate note (model must beat market before real money)',
  macro_coverage:    'series × period coverage states (predicted/scheduled), missed list, watchdog alerts',
  macro_risk:        'risk limits (per event/family/cluster/gross), scenario max loss, open exposure per series+period',
  macro_overview:    'system digest: board + performance + OOS gate',
  macro_reports:     'daily + weekly PDF reports (board snapshot, open positions & marks, alerts, weekly adds calibration deciles + gates); the panel lists the newest renderer-fixed PDFs',
  macro_bets:        'the direct "what are we betting" view: open_bets = placed unsettled paper bets with latest unrealized mark; stances = every market inside the 7-day entry window with today\'s decision (bet placed with structure/fair/cost, or PASS with the gate reason); upcoming = releases in the next 14 days. Cadence: full re-decision daily 05:00, book re-check every 15 min',
  macro_walkforward: 'the live 30d track record (production started 30d ago, strict PIT — each day used only that day\'s information) rebuilt daily at 16:00 UTC. daily.streams = hybrid (live rule) / edge (value) / argmax (defer-to-market favourite: max-fair pick, bet only when fair<=cost). ml = three-line walk-forward comparison on the same dataset: top-level windows (last30/last60/all) = ML selector (expanding-window logistic, per-event weights, 0.10-0.90 price window); .baseline = defer-to-market favourite replica; .blend = smart switch that per event follows whichever line has the better trailing settled record (strictly pre-entry). Adoption rule: a challenger must beat the baseline on BOTH windows; blend currently does but the sample is thin and PnL concentrates in few equal-risk bets, so it is displayed as candidate, NOT live. sweep = one full PIT walk-forward per entry lead (1/3/5/7d) + per-series coverage',
}

// Family-filter the big board file so inflation/labor/energy grounding stays compact.
const FAMILY_OF_TYPE: Record<string, string> = {
  macro_inflation: 'inflation', macro_labor: 'labor', macro_energy: 'energy',
}

function slimBoard(board: any, family?: string): any {
  if (!board) return board
  const series: Record<string, any> = {}
  for (const [name, s] of Object.entries<any>(board.series ?? {})) {
    if (family && s?.family !== family) continue
    // Drop the 200-point empirical quantile arrays → keep p5/p50/p95 only.
    series[name] = {
      ...s,
      entries: (s.entries ?? []).map((e: any) => {
        const dist = e?.pred?.dist
        let slim = dist
        if (dist?.kind === 'empirical' && Array.isArray(dist.quantiles) && dist.quantiles.length) {
          const q = dist.quantiles
          const at = (i: number) => q[Math.min(i, q.length - 1)]
          slim = { kind: 'empirical', p5: at(10), p50: at(100), p95: at(190), n: dist.n }
        }
        return { ...e, pred: e.pred ? { ...e.pred, dist: slim } : e.pred }
      }),
    }
  }
  return {
    generated_at: board.generated_at,
    next_releases: family
      ? (board.next_releases ?? []).filter((r: any) => r.family === family)
      : board.next_releases,
    series,
  }
}

function slimFile(file: string, data: any, type: string): any {
  if (file === 'macro_board.json') return slimBoard(data, FAMILY_OF_TYPE[type])
  if (file === 'macro_walkforward.json') {
    // trades arrays + per-lead curves blow the cap — keep window summaries,
    // the last 8 trades per stream, and the three-line (baseline/ml/blend) table
    const win = (w: any) => w && ({ n_trades: w.n_trades, won: w.won,
      win_rate: w.win_rate, staked: w.staked, realized: w.realized, roi: w.roi })
    const line = (l: any) => l && ({ last30: win(l.last30), last60: win(l.last60), all: win(l.all) })
    const streams: Record<string, any> = {}
    for (const [k, v] of Object.entries<any>(data?.daily?.streams ?? {})) {
      streams[k] = { ...win(v), trades_tail: (v?.trades ?? []).slice(-5) }
    }
    const cov = Object.entries<any>(data?.sweep?.coverage ?? {})
    // ml FIRST — the per-view cap truncates from the tail, and the three-line
    // comparison is the part the user asks about most
    return {
      generated_at: data?.generated_at,
      ml: data?.ml ? { note: data.ml.note, ml: line(data.ml),
        baseline: line(data.ml.baseline), blend: line(data.ml.blend) } : undefined,
      daily: data?.daily ? { days: data.daily.days, streams } : undefined,
      sweep: data?.sweep ? {
        days: data.sweep.days,
        leads: Object.fromEntries(Object.entries<any>(data.sweep.leads ?? {})
          .map(([k, v]) => [k, win(v)])),
        coverage_testable: cov.filter(([, v]) => v?.testable).length,
        coverage_total: cov.length,
        coverage_events: Object.fromEntries(cov.map(([k, v]) => [k, v?.events_in_window])),
      } : undefined,
    }
  }
  if (file === 'macro_decisions.json') {
    return {
      generated_at: data?.generated_at,
      decisions: (data?.decisions ?? []).slice(0, 25),
      latest_marks: data?.latest_marks ?? [],
    }
  }
  return data
}

const _PER_VIEW_CAP = 8000
const _TOTAL_CAP = 24000

/** Load the real data behind detected macro_* artifact types as a compact grounding
 *  block (headed so the model treats it as authoritative). Mirrors
 *  predictionContextForArtifacts: per-view + total caps, silent skip on failure. */
export async function macroContextForArtifacts(types: string[]): Promise<string> {
  const blocks: string[] = []
  const seen = new Set<string>()
  let total = 0
  for (const t of types) {
    const files = TYPE_TO_FILES[t]
    if (!files || seen.has(t)) continue
    seen.add(t)
    if (total >= _TOTAL_CAP) break
    for (const file of files) {
      try {
        const raw = await readJsonFile(path.resolve(dataDir, file))
        if (!raw) continue
        let json = JSON.stringify(slimFile(file, raw, t))
        if (json.length > _PER_VIEW_CAP) json = json.slice(0, _PER_VIEW_CAP) + ' …[truncated]'
        const block = `### view="${t}" (${file}) — ${ABOUT[t] ?? ''}\n${json}`
        blocks.push(block)
        total += block.length
        if (total >= _TOTAL_CAP) break
      } catch { /* skip a file that fails to load; others still inject */ }
    }
  }
  if (!blocks.length) return ''
  return '## Macro prediction-market data (authoritative — cite these numbers)\n\n' + blocks.join('\n\n')
}
