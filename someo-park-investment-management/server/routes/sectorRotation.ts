/**
 * Sector Rotation (SR) API routes — /api/sr/*
 *
 * Completely independent from MRPT/MTFS routes.
 * Reads from qlib-main/sector_rotation/ directory structure.
 */
import { Router } from 'express';
import path from 'path';
import fs from 'fs/promises';
import { getBackendPath } from '../config.js';
import { readJsonFile, listFiles, extractTimestamp, findLatestFile } from '../utils/fileUtils.js';
import { parseCsvFile } from '../utils/csvParser.js';

const router = Router();

// ── Path helpers ────────────────────────────────────────────────────
const SR_DIR = () => getBackendPath('qlib-main/sector_rotation');
const SR_SIGNALS = () => path.join(SR_DIR(), 'trading_signals');
const SR_BACKTEST = () => path.join(SR_DIR(), 'backtest_results');
const SR_HISTORY = () => getBackendPath('historical_runs/sector_rotation');
const SR_REPORT = () => path.join(SR_DIR(), 'report/output');
const SR_INV_HISTORY = () => path.join(SR_DIR(), 'inventory_history');

async function getLatestFile(dir: string, pattern: string): Promise<string | null> {
  try {
    const files = await listFiles(dir, pattern);
    return files.length > 0 ? files[files.length - 1] : null;
  } catch { return null; }
}

async function readJsonSafe(filePath: string): Promise<any> {
  try { return await readJsonFile(filePath); }
  catch { return null; }
}

// ══════════════════════════════════════════════════════════════════════
//  Inventory
// ══════════════════════════════════════════════════════════════════════

// GET /api/sr/inventory
router.get('/inventory', async (_req, res) => {
  try {
    const filePath = path.join(SR_DIR(), 'inventory_sector_rotation.json');
    const data = await readJsonFile(filePath);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// GET /api/sr/inventory/history
router.get('/inventory/history', async (_req, res) => {
  try {
    const files = await listFiles(SR_INV_HISTORY(), 'inventory_sector_rotation_*.json');
    const result = await Promise.all(files.map(async (f) => {
      const data = await readJsonSafe(f);
      const stats = await fs.stat(f).catch(() => null);
      const holdings = data?.holdings || {};
      const activeCount = Object.values(holdings).filter((h: any) => (h.weight || 0) > 0.01).length;
      return {
        filename: path.basename(f),
        timestamp: extractTimestamp(f),
        as_of: data?.as_of || '',
        activePairs: activeCount,
        size: stats?.size || 0,
      };
    }));
    res.json(result);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/sr/inventory/history/:filename
router.get('/inventory/history/:filename', async (req, res) => {
  try {
    const filename = path.basename(req.params.filename);
    const filePath = path.join(SR_INV_HISTORY(), filename);
    const data = await readJsonFile(filePath);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  Signals & Daily Report
// ══════════════════════════════════════════════════════════════════════

// GET /api/sr/signals/latest
router.get('/signals/latest', async (_req, res) => {
  try {
    const latest = await getLatestFile(SR_SIGNALS(), 'sr_daily_report_*.json');
    if (!latest) return res.json({ available: false });
    const data = await readJsonFile(latest);
    res.json({
      available: true,
      signal_date: data.signal_date,
      regime: data.regime?.label,
      signals: data.signals || [],
      smart_select: data.smart_select || null,
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/sr/daily-report/latest
router.get('/daily-report/latest', async (_req, res) => {
  try {
    const latest = await getLatestFile(SR_SIGNALS(), 'sr_daily_report_*.json');
    if (!latest) return res.json({ available: false });
    const data = await readJsonFile(latest);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/sr/daily-report/latest/txt
router.get('/daily-report/latest/txt', async (_req, res) => {
  try {
    const latest = await getLatestFile(SR_SIGNALS(), 'sr_daily_report_*.txt');
    if (!latest) return res.type('text').send('No SR daily report available');
    const text = await fs.readFile(latest, 'utf-8');
    res.type('text').send(text);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  Regime
// ══════════════════════════════════════════════════════════════════════

// GET /api/sr/regime/latest
router.get('/regime/latest', async (_req, res) => {
  try {
    const latest = await getLatestFile(SR_SIGNALS(), 'sr_daily_report_*.json');
    if (!latest) return res.json({ available: false });
    const data = await readJsonFile(latest);
    res.json(data.regime || { available: false });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  Sector Universe (replaces Pair Universe for SR)
// ══════════════════════════════════════════════════════════════════════

// GET /api/sr/sector-universe
router.get('/sector-universe', async (_req, res) => {
  try {
    const filePath = path.join(SR_DIR(), 'inventory_sector_rotation.json');
    const data = await readJsonFile(filePath);
    const holdings = data.holdings || {};
    // All 11 GICS sector ETFs — show all, mark held ones
    const ALL_ETFS = ['XLE','XLB','XLI','XLY','XLP','XLV','XLF','XLK','XLC','XLU','XLRE'];
    const sectors = ALL_ETFS.map(ticker => {
      const info = holdings[ticker] || {};
      return {
        ticker,
        weight: info.weight || 0,
        entry_date: info.entry_date || '',
        cost_basis: info.cost_basis || 0,
        shares: info.shares || 0,
        last_price: info.last_price || 0,
        days_held: info.days_held || 0,
        held: (info.weight || 0) > 0.01,
      };
    });
    res.json({
      available: true,
      param_set: data.param_set || '',
      signal_version: data.signal_version || 'v1',
      n_positions: sectors.filter(s => s.held).length,
      sectors,
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  Walk-Forward
// ══════════════════════════════════════════════════════════════════════

// GET /api/sr/wf/summary
router.get('/wf/summary', async (_req, res) => {
  try {
    const filePath = path.join(SR_BACKTEST(), 'wf_fold_detail.json');
    const data = await readJsonFile(filePath);
    res.json({
      available: true,
      mode: data.mode,
      n_folds: data.n_folds,
      n_param_sets: data.n_param_sets,
      mean_wfe: data.mean_wfe,
      dsr_aggregate: data.dsr_aggregate,
      synthetic_metrics: data.synthetic_metrics,
      param_oos_stats: data.param_oos_stats,
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// GET /api/sr/wf/fold-grid
router.get('/wf/fold-grid', async (_req, res) => {
  try {
    const filePath = path.join(SR_BACKTEST(), 'wf_fold_detail.json');
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

// GET /api/sr/wf/param-oos
router.get('/wf/param-oos', async (_req, res) => {
  try {
    // Return batch summary CSV as array (matches MRPT/MTFS pair-summary format)
    const batchFile = await findLatestFile(SR_BACKTEST(), 'sr_batch_summary_*.csv');
    const rows = await parseCsvFile(batchFile);
    // Also include regime data for the regime columns
    let regimeData: Record<string, any> = {};
    try {
      const regimePath = path.join(SR_BACKTEST(), 'param_oos_by_regime.json');
      regimeData = await readJsonFile(regimePath) as Record<string, any>;
    } catch { /* ignore if missing */ }
    res.json({ available: true, rows, regimeData });
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// GET /api/sr/equity-curve?param=X
router.get('/equity-curve', async (req, res) => {
  try {
    // Read from selected_param_set.json to get current param + version
    const selPath = path.join(SR_DIR(), 'selected_param_set.json');
    const sel = await readJsonSafe(selPath);
    const param = (req.query.param as string) || sel?.param_set || 'tight_beta_tracker';
    const ver = (req.query.version as string) || sel?.signal_version || 'v1';

    // Prefer IS-OOS (walk-forward validated) over IS-only
    let files = await listFiles(SR_HISTORY(), `sr_portfolio_${param}_${ver}_IS-OOS_*.xlsx`);
    if (files.length === 0) {
      files = await listFiles(SR_HISTORY(), `sr_portfolio_${param}_*.xlsx`);
    }
    if (files.length === 0) {
      return res.json({ available: false, message: `No portfolio file for ${param}` });
    }
    // Read equity from the latest portfolio xlsx (sheet: equity_history)
    const { parseXlsxSheet } = await import('../utils/xlsxParser.js');
    const latestFile = files[files.length - 1];
    const sheetData = await parseXlsxSheet(latestFile, 'equity_history');
    const allRows = sheetData.rows.map((r: any) => ({
      date: r.Date || r.date || r[0],
      value: r.Net_Equity || r.net_equity || r.value || r[1],
    }));

    // For OOS-only: find earliest OOS start from wf_fold_detail
    let oosStart: string | null = null;
    try {
      const wfPath = path.join(SR_BACKTEST(), `wf_fold_detail_${ver}.json`);
      const wfData = await readJsonSafe(wfPath) || await readJsonSafe(path.join(SR_BACKTEST(), 'wf_fold_detail.json'));
      if (wfData?.folds?.length) {
        const oosStarts = wfData.folds.map((f: any) => f.oos_start).filter(Boolean).sort();
        oosStart = oosStarts[0]?.slice(0, 10) || null;
      }
    } catch { /* skip */ }

    // Filter to OOS period only; re-base equity to start at first OOS value
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

// GET /api/sr/portfolio-history/list
router.get('/portfolio-history/list', async (_req, res) => {
  try {
    const files = await listFiles(SR_HISTORY(), 'sr_portfolio_*.xlsx');
    const result = files.map(f => {
      const name = path.basename(f);
      // Parse: sr_portfolio_{param}_{version}_{span}_{mode}_{ts}.xlsx
      const match = name.match(/sr_portfolio_(.+)_(v[12])_(IS(?:-OOS)?)_(batch|select|tearsheet|backtest)_(\d{8}_\d{6})\.xlsx/);
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

// GET /api/sr/portfolio-history/:filename/sheets
router.get('/portfolio-history/:filename/sheets', async (req, res) => {
  try {
    const filename = path.basename(req.params.filename);
    const filePath = path.join(SR_HISTORY(), filename);
    const { listXlsxSheets } = await import('../utils/xlsxParser.js');
    const sheets = await listXlsxSheets(filePath);
    res.json(sheets);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/sr/portfolio-history/:filename/:sheet
router.get('/portfolio-history/:filename/:sheet', async (req, res) => {
  try {
    const filename = path.basename(req.params.filename);
    const filePath = path.join(SR_HISTORY(), filename);
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

// GET /api/sr/diagnostic/latest
router.get('/diagnostic/latest', async (_req, res) => {
  try {
    const files = await listFiles(SR_HISTORY(), 'wf_diagnostic_sr_*.xlsx');
    if (files.length === 0) return res.json({ available: false });
    const latest = files[files.length - 1];
    const { listXlsxSheets } = await import('../utils/xlsxParser.js');
    const sheets = await listXlsxSheets(latest);
    res.json({ available: true, filename: path.basename(latest), sheets });
  } catch (err: any) {
    res.status(500).json({ error: err.message, available: false });
  }
});

// GET /api/sr/diagnostic/latest/:sheet
router.get('/diagnostic/latest/:sheet', async (req, res) => {
  try {
    const files = await listFiles(SR_HISTORY(), 'wf_diagnostic_sr_*.xlsx');
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

// GET /api/sr/tearsheet/list
router.get('/tearsheet/list', async (_req, res) => {
  try {
    const files = await listFiles(SR_REPORT(), '*.pdf');
    const result = files.map(f => ({
      filename: path.basename(f),
      timestamp: extractTimestamp(f) || path.basename(f),
    }));
    res.json(result);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/sr/tearsheet/:filename
router.get('/tearsheet/:filename', async (req, res) => {
  try {
    const filename = path.basename(req.params.filename);
    const filePath = path.join(SR_REPORT(), filename);
    res.sendFile(filePath);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// ══════════════════════════════════════════════════════════════════════
//  Smart Select
// ══════════════════════════════════════════════════════════════════════

// GET /api/sr/smart-select
router.get('/smart-select', async (_req, res) => {
  try {
    // Get from latest daily report
    const latest = await getLatestFile(SR_SIGNALS(), 'sr_daily_report_*.json');
    const sel = await readJsonSafe(path.join(SR_DIR(), 'selected_param_set.json'));

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

// GET /api/sr/files/list
router.get('/files/list', async (_req, res) => {
  try {
    const files = await listFiles(SR_HISTORY(), '*.xlsx');
    // Group by param_set
    const grouped: Record<string, any[]> = {};
    for (const f of files) {
      const name = path.basename(f);
      const match = name.match(/sr_portfolio_(.+)_(v[12])_(IS(?:-OOS)?)_(batch|select|tearsheet|backtest)_(\d{8}_\d{6})\.xlsx/);
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
        // WF diagnostic or monitor files
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

// GET /api/sr/strategy-performance
router.get('/strategy-performance', async (_req, res) => {
  try {
    const sel = await readJsonSafe(path.join(SR_DIR(), 'selected_param_set.json'));
    const param = (sel?.param_set) || 'tight_beta_tracker';

    // Find V1 and V2 files for same param
    const v1Files = await listFiles(SR_HISTORY(), `sr_portfolio_${param}_v1_*.xlsx`);
    const v2Files = await listFiles(SR_HISTORY(), `sr_portfolio_${param}_v2_*.xlsx`);

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

// GET /api/sr/params/list — list available param sets
router.get('/params/list', async (_req, res) => {
  try {
    const topPath = path.join(SR_BACKTEST(), 'top_candidates.json');
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
