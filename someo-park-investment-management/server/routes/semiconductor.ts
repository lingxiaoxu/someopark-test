/**
 * AI Semiconductor Strategy (AISS) API routes — /api/aiss/*
 *
 * Twin of the SSRS (sector rotation) routes; reads from
 * qlib-main/semiconductor_strategy/ and historical_runs/semiconductor_strategy/.
 *
 * IMPORTANT: AISS trades individual STOCKS grouped into subsectors. The
 * subsector layer is NOT tradable, so holdings/universe endpoints always
 * expose stock-level positions (from inventory_aiss.json -> stock_holdings),
 * with the subsector only used as a grouping label + weight.
 */
import { Router } from 'express';
import path from 'path';
import fs from 'fs/promises';
import { getBackendPath } from '../config.js';
import { readJsonFile, listFiles, extractTimestamp, findLatestFile } from '../utils/fileUtils.js';
import { parseCsvFile } from '../utils/csvParser.js';

const router = Router();

// ── Path helpers ────────────────────────────────────────────────────
const AISS_DIR = () => getBackendPath('qlib-main/semiconductor_strategy');
const AISS_SIGNALS = () => path.join(AISS_DIR(), 'trading_signals');
const AISS_BACKTEST = () => path.join(AISS_DIR(), 'backtest_results');
const AISS_HISTORY = () => getBackendPath('historical_runs/semiconductor_strategy');
const AISS_REPORT = () => path.join(AISS_DIR(), 'report/output');
const AISS_INV_HISTORY = () => path.join(AISS_DIR(), 'inventory_history');

async function getLatestFile(dir: string, pattern: string): Promise<string | null> {
  try {
    const files = await listFiles(dir, pattern);   // listFiles sorts DESC (newest first)
    return files.length > 0 ? files[0] : null;
  } catch { return null; }
}

async function readJsonSafe(filePath: string): Promise<any> {
  try { return await readJsonFile(filePath); }
  catch { return null; }
}

// Build the stock-level view from an inventory object: subsector groups
// (weight from holdings) + member stocks (from stock_holdings).
function buildStockView(data: any) {
  const holdings = data?.holdings || {};            // subsector -> {weight, ...}
  const stockHoldings = data?.stock_holdings || {}; // ticker -> {subsectors, shares, ...}
  const groups: Record<string, any> = {};
  for (const [sub, info] of Object.entries(holdings) as any) {
    groups[sub] = {
      subsector: sub,
      weight: info.weight || 0,
      days_held: info.days_held || 0,
      action_today: info.action_today || '',
      stocks: [],
    };
  }
  const stocks: any[] = [];
  for (const [ticker, info] of Object.entries(stockHoldings) as any) {
    const sub = (info.subsectors && info.subsectors[0]) || 'other';
    const subH = (data?.holdings || {})[sub] || {};
    const row = {
      ticker,
      subsector: sub,
      weight: info.portfolio_weight || 0,
      shares: info.shares || 0,
      last_price: info.last_price || 0,
      target_value: info.target_value || 0,
      // per-stock cost tracking; legacy snapshots lacking it inherit entry/days from
      // the subsector and use the stock's own price as a neutral cost fallback
      // (real per-stock cost is populated by the daily signal going forward).
      cost_basis: info.cost_basis ?? info.last_price ?? 0,
      entry_date: info.entry_date ?? subH.entry_date ?? '',
      days_held: info.days_held ?? subH.days_held ?? 0,
      action_today: info.action_today ?? subH.action_today ?? 'HOLD',
    };
    stocks.push(row);
    if (!groups[sub]) groups[sub] = { subsector: sub, weight: 0, stocks: [] };
    groups[sub].stocks.push(row);
  }
  // sort stocks within each group by weight desc; groups by weight desc
  const subsectors = Object.values(groups)
    .map((g: any) => ({ ...g, stocks: g.stocks.sort((a: any, b: any) => b.weight - a.weight) }))
    .sort((a: any, b: any) => b.weight - a.weight);
  stocks.sort((a, b) => b.weight - a.weight);
  return { subsectors, stocks };
}

// ══════════════════════════════════════════════════════════════════════
//  Inventory
// ══════════════════════════════════════════════════════════════════════

// GET /api/aiss/inventory — full inventory + derived stock-level view
router.get('/inventory', async (_req, res) => {
  try {
    const filePath = path.join(AISS_DIR(), 'inventory_aiss.json');
    const data = await readJsonFile(filePath);
    const view = buildStockView(data);
    res.json({ ...data, stock_view: view });
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// GET /api/aiss/inventory/history
router.get('/inventory/history', async (_req, res) => {
  try {
    const files = await listFiles(AISS_INV_HISTORY(), 'inventory_aiss_*.json');
    const result = await Promise.all(files.map(async (f) => {
      const data = await readJsonSafe(f);
      const stats = await fs.stat(f).catch(() => null);
      const stockHoldings = data?.stock_holdings || {};
      const activeCount = Object.keys(stockHoldings).length;
      return {
        filename: path.basename(f),
        timestamp: extractTimestamp(f),
        as_of: data?.as_of || '',
        activePairs: activeCount,   // # stocks held (keeps field name for viewer reuse)
        size: stats?.size || 0,
      };
    }));
    res.json(result);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/aiss/inventory/history/:filename
router.get('/inventory/history/:filename', async (req, res) => {
  try {
    const filename = path.basename(req.params.filename);
    const filePath = path.join(AISS_INV_HISTORY(), filename);
    const data = await readJsonFile(filePath);
    res.json({ ...data, stock_view: buildStockView(data) });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  Signals & Daily Report
// ══════════════════════════════════════════════════════════════════════

// GET /api/aiss/signals/latest
router.get('/signals/latest', async (_req, res) => {
  try {
    const latest = await getLatestFile(AISS_SIGNALS(), 'aiss_daily_report_*.json');
    if (!latest) return res.json({ available: false });
    const data = await readJsonFile(latest);
    res.json({
      available: true,
      signal_date: data.signal_date,
      regime: data.regime,                          // full regime object (label + indicators)
      signals: data.signals || [],
      smart_select: data.smart_select || null,
      stock_holdings: data.stock_holdings || {},    // tradable individual stocks (target_shares)
      stock_trades: data.stock_trades || {},        // per-stock actions
      stock_breakdown: data.stock_breakdown || {},
      stock_universe: data.stock_universe || [],    // ALL 8 subsectors × 4 stocks (incl 0%/reserve)
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/aiss/daily-report/latest
router.get('/daily-report/latest', async (_req, res) => {
  try {
    const latest = await getLatestFile(AISS_SIGNALS(), 'aiss_daily_report_*.json');
    if (!latest) return res.json({ available: false });
    const data = await readJsonFile(latest);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/aiss/daily-report/latest/txt
router.get('/daily-report/latest/txt', async (_req, res) => {
  try {
    const latest = await getLatestFile(AISS_SIGNALS(), 'aiss_daily_report_*.txt');
    if (!latest) return res.type('text').send('No AISS daily report available');
    const text = await fs.readFile(latest, 'utf-8');
    res.type('text').send(text);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  Regime
// ══════════════════════════════════════════════════════════════════════

// GET /api/aiss/regime/latest
router.get('/regime/latest', async (_req, res) => {
  try {
    const latest = await getLatestFile(AISS_SIGNALS(), 'aiss_daily_report_*.json');
    if (!latest) return res.json({ available: false });
    const data = await readJsonFile(latest);
    res.json(data.regime || { available: false });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  Stock Universe (stock-level; subsector is only a grouping label)
// ══════════════════════════════════════════════════════════════════════

// GET /api/aiss/stock-universe — FULL tradable universe (all 8 subsectors × 4
// stocks incl. unselected/reserve at 0%), sourced from the daily report's
// stock_universe block; falls back to the held-only inventory view if absent.
router.get('/stock-universe', async (_req, res) => {
  try {
    const report = await (async () => {
      const latest = await getLatestFile(AISS_SIGNALS(), 'aiss_daily_report_*.json');
      return latest ? await readJsonSafe(latest) : null;
    })();
    const inv = await readJsonSafe(path.join(AISS_DIR(), 'inventory_aiss.json')) || {};

    if (report?.stock_universe?.length) {
      // map daily-report shape → viewer shape (subsectors with weight + member stocks)
      const subsectors = report.stock_universe.map((g: any) => ({
        subsector: g.subsector,
        display: g.display || g.subsector,
        weight: g.subsector_weight || 0,
        held: !!g.held,
        composite_score: g.composite_score ?? null,
        stocks: (g.stocks || []).map((s: any) => ({
          ticker: s.ticker,
          tier_role: s.tier_role,
          weight: s.portfolio_weight || 0,        // portfolio-level weight
          within_weight: s.within_weight || 0,    // within-subsector tier weight
          last_price: s.price || 0,
          shares: inv.stock_holdings?.[s.ticker]?.shares || 0,
          target_value: inv.stock_holdings?.[s.ticker]?.target_value || 0,
          held: (inv.stock_holdings?.[s.ticker]?.shares || 0) !== 0,
        })),
      }));
      const stocks = subsectors.flatMap((g: any) => g.stocks);
      return res.json({
        available: true,
        param_set: inv.param_set || '',
        signal_version: inv.signal_version || 'v1',
        updated_at: report.signal_date || inv.last_daily_update || inv.as_of || '',
        source: 'daily_report',
        n_stocks: stocks.length,
        n_subsectors: subsectors.length,
        n_held_subsectors: subsectors.filter((s: any) => s.held).length,
        subsectors,
        stocks,
      });
    }

    // fallback: held-only view from inventory
    const view = buildStockView(inv);
    res.json({
      available: true,
      param_set: inv.param_set || '',
      signal_version: inv.signal_version || 'v1',
      updated_at: inv.last_daily_update || inv.as_of || '',
      source: 'inventory',
      n_stocks: view.stocks.length,
      n_subsectors: view.subsectors.length,
      n_held_subsectors: view.subsectors.filter((s: any) => (s.weight || 0) > 0.001).length,
      subsectors: view.subsectors,
      stocks: view.stocks,
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  Walk-Forward
// ══════════════════════════════════════════════════════════════════════

// GET /api/aiss/wf/summary
router.get('/wf/summary', async (_req, res) => {
  try {
    const filePath = path.join(AISS_BACKTEST(), 'wf_fold_detail.json');
    const data = await readJsonFile(filePath);
    // universe counts (subsectors + distinct stocks) from the latest daily report
    let n_subsectors = 8, n_stocks = 0;
    try {
      const latest = await getLatestFile(AISS_SIGNALS(), 'aiss_daily_report_*.json');
      const rep = latest ? await readJsonSafe(latest) : null;
      const su = rep?.stock_universe || [];
      if (su.length) {
        n_subsectors = su.length;
        n_stocks = new Set(su.flatMap((g: any) => (g.stocks || []).map((s: any) => s.ticker))).size;
      }
    } catch { /* keep defaults */ }
    res.json({
      available: true,
      mode: data.mode,
      n_folds: data.n_folds,
      n_param_sets: data.n_param_sets,
      n_subsectors,
      n_stocks,
      mean_wfe: data.mean_wfe,
      dsr_aggregate: data.dsr_aggregate,
      synthetic_metrics: data.synthetic_metrics,
      param_oos_stats: data.param_oos_stats,
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// GET /api/aiss/wf/fold-grid
router.get('/wf/fold-grid', async (_req, res) => {
  try {
    const filePath = path.join(AISS_BACKTEST(), 'wf_fold_detail.json');
    const data = await readJsonFile(filePath);
    const folds = (data.folds || []).map((f: any) => ({
      fold_id: f.fold_id,
      is_start: f.is_start,
      is_end: f.is_end,
      oos_start: f.oos_start,
      oos_end: f.oos_end,
      selected: f.selected,
      method: f.method,
      is_sharpe: f.is_sharpe,
      oos_sharpe: f.oos_metrics?.sharpe ?? null,
      wfe: f.wfe,
      oos_regime: f.oos_regime,
    }));
    res.json({ available: true, folds });
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// GET /api/aiss/wf/param-oos
router.get('/wf/param-oos', async (_req, res) => {
  try {
    const batchFile = await findLatestFile(AISS_BACKTEST(), 'aiss_batch_summary_*.csv');
    const rows = await parseCsvFile(batchFile);
    let regimeData: Record<string, any> = {};
    try {
      const regimePath = path.join(AISS_BACKTEST(), 'param_oos_by_regime.json');
      regimeData = await readJsonFile(regimePath) as Record<string, any>;
    } catch { /* ignore if missing */ }
    res.json({ available: true, rows, regimeData });
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// GET /api/aiss/equity-curve?param=X&version=v1
router.get('/equity-curve', async (req, res) => {
  try {
    const selPath = path.join(AISS_DIR(), 'selected_param_set.json');
    const sel = await readJsonSafe(selPath);
    const param = (req.query.param as string) || sel?.param_set || 'balanced_four';
    const ver = (req.query.version as string) || sel?.signal_version || 'v1';

    let files = await listFiles(AISS_HISTORY(), `aiss_portfolio_${param}_${ver}_IS-OOS_*.xlsx`);
    if (files.length === 0) {
      files = await listFiles(AISS_HISTORY(), `aiss_portfolio_${param}_*.xlsx`);
    }
    if (files.length === 0) {
      return res.json({ available: false, message: `No portfolio file for ${param}` });
    }
    const { parseXlsxSheet } = await import('../utils/xlsxParser.js');
    const latestFile = files[files.length - 1];
    const sheetData = await parseXlsxSheet(latestFile, 'equity_history');
    const allRows = sheetData.rows.map((r: any) => ({
      date: r.Date || r.date || r[0],
      value: r.Net_Equity || r.net_equity || r.value || r[1],
    }));

    let oosStart: string | null = null;
    try {
      const wfPath = path.join(AISS_BACKTEST(), `wf_fold_detail_${ver}.json`);
      const wfData = await readJsonSafe(wfPath) || await readJsonSafe(path.join(AISS_BACKTEST(), 'wf_fold_detail.json'));
      if (wfData?.folds?.length) {
        const oosStarts = wfData.folds.map((f: any) => f.oos_start).filter(Boolean).sort();
        oosStart = oosStarts[0]?.slice(0, 10) || null;
      }
    } catch { /* skip */ }

    let equityCurve = allRows;
    if (oosStart) {
      const oosRows = allRows.filter((r: any) => r.date >= oosStart);
      if (oosRows.length > 0) {
        const baseValue = oosRows[0].value;
        equityCurve = oosRows.map((r: any) => ({
          ...r,
          value_rebased: baseValue ? (r.value / baseValue) * 1000000 : r.value,
        }));
      }
    }

    res.json({ available: true, param, version: ver, file: path.basename(latestFile), oos_start: oosStart, data: equityCurve });
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  Portfolio History Excel
// ══════════════════════════════════════════════════════════════════════

const PORTFOLIO_RE = /aiss_portfolio_(.+)_(v[12])_(IS(?:-OOS)?)_(batch|select|tearsheet|backtest)_(\d{8}_\d{6})\.xlsx/;

// GET /api/aiss/portfolio-history/list
router.get('/portfolio-history/list', async (_req, res) => {
  try {
    const files = await listFiles(AISS_HISTORY(), 'aiss_portfolio_*.xlsx');
    const result = files.map(f => {
      const name = path.basename(f);
      const match = name.match(PORTFOLIO_RE);
      return {
        filename: name,
        param: match?.[1] || '',
        version: match?.[2] || '',
        span: match?.[3] || '',
        mode: match?.[4] || '',
        timestamp: match?.[5] || '',
      };
    });
    res.json(result);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/aiss/portfolio-history/:filename/sheets
router.get('/portfolio-history/:filename/sheets', async (req, res) => {
  try {
    const filename = path.basename(req.params.filename);
    const filePath = path.join(AISS_HISTORY(), filename);
    const { listXlsxSheets } = await import('../utils/xlsxParser.js');
    const sheets = await listXlsxSheets(filePath);
    res.json(sheets);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/aiss/portfolio-history/:filename/:sheet
router.get('/portfolio-history/:filename/:sheet', async (req, res) => {
  try {
    const filename = path.basename(req.params.filename);
    const filePath = path.join(AISS_HISTORY(), filename);
    const { parseXlsxSheet } = await import('../utils/xlsxParser.js');
    const data = await parseXlsxSheet(filePath, req.params.sheet);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  WF Diagnostic
// ══════════════════════════════════════════════════════════════════════

// GET /api/aiss/diagnostic/latest
router.get('/diagnostic/latest', async (_req, res) => {
  try {
    const files = await listFiles(AISS_HISTORY(), 'wf_diagnostic_aiss_*.xlsx');
    if (files.length === 0) return res.json({ available: false });
    const latest = files[files.length - 1];
    const { listXlsxSheets } = await import('../utils/xlsxParser.js');
    const sheets = await listXlsxSheets(latest);
    res.json({ available: true, filename: path.basename(latest), sheets });
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// GET /api/aiss/diagnostic/latest/:sheet
router.get('/diagnostic/latest/:sheet', async (req, res) => {
  try {
    const files = await listFiles(AISS_HISTORY(), 'wf_diagnostic_aiss_*.xlsx');
    if (files.length === 0) return res.json({ available: false });
    const latest = files[files.length - 1];
    const { parseXlsxSheet } = await import('../utils/xlsxParser.js');
    const data = await parseXlsxSheet(latest, req.params.sheet);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  Tearsheet PDF
// ══════════════════════════════════════════════════════════════════════

// GET /api/aiss/tearsheet/list
router.get('/tearsheet/list', async (_req, res) => {
  try {
    const files = await listFiles(AISS_REPORT(), '*.pdf');
    const result = files.map(f => ({
      filename: path.basename(f),
      timestamp: extractTimestamp(f) || path.basename(f),
    }));
    res.json(result);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/aiss/tearsheet/:filename
router.get('/tearsheet/:filename', async (req, res) => {
  try {
    const filename = path.basename(req.params.filename);
    const filePath = path.join(AISS_REPORT(), filename);
    res.sendFile(filePath);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  Smart Select
// ══════════════════════════════════════════════════════════════════════

// GET /api/aiss/smart-select
router.get('/smart-select', async (_req, res) => {
  try {
    const latest = await getLatestFile(AISS_SIGNALS(), 'aiss_daily_report_*.json');
    const sel = await readJsonSafe(path.join(AISS_DIR(), 'selected_param_set.json'));

    const report = latest ? await readJsonSafe(latest) : null;
    const smartSelect = report?.smart_select || {};

    res.json({
      available: true,
      ...smartSelect,
      switch_history: sel?.switch_history || [],
      top_candidates: sel?.top_candidates || [],
      health: sel?.health || {},
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  File Structure (for WFStructureViewer)
// ══════════════════════════════════════════════════════════════════════

// GET /api/aiss/files/list
router.get('/files/list', async (_req, res) => {
  try {
    const files = await listFiles(AISS_HISTORY(), '*.xlsx');
    const grouped: Record<string, any[]> = {};
    for (const f of files) {
      const name = path.basename(f);
      const match = name.match(PORTFOLIO_RE);
      if (match) {
        const param = match[1];
        if (!grouped[param]) grouped[param] = [];
        grouped[param].push({
          filename: name,
          version: match[2],
          span: match[3],
          mode: match[4],
          timestamp: match[5],
        });
      } else {
        const key = '_diagnostics';
        if (!grouped[key]) grouped[key] = [];
        grouped[key].push({ filename: name });
      }
    }
    res.json({ available: true, groups: grouped });
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  Strategy Performance (V1 vs V2)
// ══════════════════════════════════════════════════════════════════════

// GET /api/aiss/strategy-performance
router.get('/strategy-performance', async (_req, res) => {
  try {
    const sel = await readJsonSafe(path.join(AISS_DIR(), 'selected_param_set.json'));
    const param = (sel?.param_set) || 'balanced_four';

    const v1Files = await listFiles(AISS_HISTORY(), `aiss_portfolio_${param}_v1_*.xlsx`);
    const v2Files = await listFiles(AISS_HISTORY(), `aiss_portfolio_${param}_v2_*.xlsx`);

    const result: any = { available: true, param };

    if (v1Files.length > 0) {
      const { parseXlsxSheet } = await import('../utils/xlsxParser.js');
      const data = await parseXlsxSheet(v1Files[v1Files.length - 1], 'equity_history');
      result.v1 = data.rows.map((r: any) => ({ date: r[0], value: r[1] }));
    }
    if (v2Files.length > 0) {
      const { parseXlsxSheet } = await import('../utils/xlsxParser.js');
      const data = await parseXlsxSheet(v2Files[v2Files.length - 1], 'equity_history');
      result.v2 = data.rows.map((r: any) => ({ date: r[0], value: r[1] }));
    }

    res.json(result);
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// GET /api/aiss/params/list — list available param sets
router.get('/params/list', async (_req, res) => {
  try {
    const topPath = path.join(AISS_BACKTEST(), 'top_candidates.json');
    const data = await readJsonSafe(topPath);
    if (data?.top) {
      res.json(data.top.map((c: any) => c.name || c.param_set));
    } else {
      res.json([]);
    }
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

export default router;
