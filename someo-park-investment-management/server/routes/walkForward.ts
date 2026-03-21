import { Router } from 'express';
import path from 'path';
import fs from 'fs/promises';
import { readJsonFile, findLatestFile } from '../utils/fileUtils.js';
import { parseCsvFile } from '../utils/csvParser.js';
import { listXlsxSheets, parseXlsxSheet } from '../utils/xlsxParser.js';
import { getBackendPath } from '../config.js';

const router = Router();

function getWfDir(strategy: string): string {
  return strategy === 'mtfs'
    ? getBackendPath('historical_runs/walk_forward_mtfs')
    : getBackendPath('historical_runs/walk_forward');
}

// GET /api/wf/summary/mrpt
router.get('/summary/:strategy', async (req, res) => {
  try {
    const { strategy } = req.params;
    if (!['mrpt', 'mtfs'].includes(strategy)) {
      return res.status(400).json({ error: 'Invalid strategy' });
    }
    const dir = getWfDir(strategy);
    const latestFile = await findLatestFile(dir, 'walk_forward_summary_*.json');
    const data = await readJsonFile(latestFile);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/wf/equity-curve/mrpt
router.get('/equity-curve/:strategy', async (req, res) => {
  try {
    const { strategy } = req.params;
    if (!['mrpt', 'mtfs'].includes(strategy)) {
      return res.status(400).json({ error: 'Invalid strategy' });
    }
    const dir = getWfDir(strategy);
    const latestFile = await findLatestFile(dir, 'oos_equity_curve_*.csv');
    const data = await parseCsvFile(latestFile);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/wf/pair-summary/mrpt
router.get('/pair-summary/:strategy', async (req, res) => {
  try {
    const { strategy } = req.params;
    if (!['mrpt', 'mtfs'].includes(strategy)) {
      return res.status(400).json({ error: 'Invalid strategy' });
    }
    const dir = getWfDir(strategy);
    const latestFile = await findLatestFile(dir, 'oos_pair_summary_*.csv');
    const data = await parseCsvFile(latestFile);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/wf/dsr-log/mrpt
router.get('/dsr-log/:strategy', async (req, res) => {
  try {
    const { strategy } = req.params;
    if (!['mrpt', 'mtfs'].includes(strategy)) {
      return res.status(400).json({ error: 'Invalid strategy' });
    }
    const dir = getWfDir(strategy);
    const latestFile = await findLatestFile(dir, 'dsr_selection_log_*.csv');
    const data = await parseCsvFile(latestFile);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/wf/xlsx/sheets?strategy=mrpt&path=window06.../historical_runs/portfolio_history_....xlsx
router.get('/xlsx/sheets', async (req, res) => {
  try {
    const { strategy, path: relPath } = req.query;
    if (!strategy || !relPath) {
      return res.status(400).json({ error: 'strategy and path required' });
    }
    if (!['mrpt', 'mtfs'].includes(String(strategy))) {
      return res.status(400).json({ error: 'Invalid strategy' });
    }
    const dir = getWfDir(String(strategy));
    const filePath = path.resolve(dir, String(relPath));
    // Security: ensure resolved path is under wf dir
    if (!filePath.startsWith(dir)) {
      return res.status(403).json({ error: 'Path traversal detected' });
    }
    await fs.access(filePath);
    const sheets = await listXlsxSheets(filePath);
    res.json({ file: path.basename(filePath), sheets });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/wf/xlsx/sheet?strategy=mrpt&path=...&sheet=equity_history
router.get('/xlsx/sheet', async (req, res) => {
  try {
    const { strategy, path: relPath, sheet } = req.query;
    if (!strategy || !relPath || !sheet) {
      return res.status(400).json({ error: 'strategy, path, and sheet required' });
    }
    if (!['mrpt', 'mtfs'].includes(String(strategy))) {
      return res.status(400).json({ error: 'Invalid strategy' });
    }
    const dir = getWfDir(String(strategy));
    const filePath = path.resolve(dir, String(relPath));
    if (!filePath.startsWith(dir)) {
      return res.status(403).json({ error: 'Path traversal detected' });
    }
    const data = await parseXlsxSheet(filePath, decodeURIComponent(String(sheet)));
    res.json(data);
  } catch (err: any) {
    if (err.message.includes('not found')) {
      return res.status(404).json({ error: err.message });
    }
    res.status(500).json({ error: err.message });
  }
});

// GET /api/wf/xlsx/list?strategy=mrpt — list xlsx files in wf directory tree
router.get('/xlsx/list', async (req, res) => {
  try {
    const { strategy } = req.query;
    if (!strategy || !['mrpt', 'mtfs'].includes(String(strategy))) {
      return res.status(400).json({ error: 'Valid strategy required' });
    }
    const dir = getWfDir(String(strategy));

    async function findXlsx(base: string, prefix: string): Promise<string[]> {
      const results: string[] = [];
      try {
        const entries = await fs.readdir(base, { withFileTypes: true });
        for (const entry of entries) {
          const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
          if (entry.isDirectory()) {
            results.push(...await findXlsx(path.join(base, entry.name), rel));
          } else if (entry.name.endsWith('.xlsx')) {
            results.push(rel);
          }
        }
      } catch { /* skip inaccessible dirs */ }
      return results;
    }

    const files = await findXlsx(dir, '');
    res.json(files);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

export default router;
