import { Router } from 'express';
import path from 'path';
import { listFiles, extractTimestamp, deduplicateLatest } from '../utils/fileUtils.js';
import { listXlsxSheets, parseXlsxSheet } from '../utils/xlsxParser.js';
import { getBackendPath } from '../config.js';

const router = Router();

// GET /api/monitor-history/list
router.get('/list', async (req, res) => {
  try {
    const dir = getBackendPath('trading_signals/monitor_history');
    const allFiles = await listFiles(dir, 'monitor_*.xlsx');
    const files = deduplicateLatest(allFiles);
    const result = files.map(f => {
      const basename = path.basename(f, '.xlsx');
      const parts = basename.split('_');
      // monitor_mrpt_AWK_FOX_20260321_123649
      const strategy = parts[1];
      const s1 = parts[2];
      const s2 = parts[3];
      const timestamp = parts.length >= 6 ? `${parts[4]}_${parts[5]}` : extractTimestamp(f);
      return { filename: path.basename(f), strategy, pair: `${s1}/${s2}`, timestamp };
    });
    res.json(result.sort((a, b) => b.timestamp.localeCompare(a.timestamp)));
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/monitor-history/:filename/sheets
router.get('/:filename/sheets', async (req, res) => {
  try {
    const filename = path.basename(req.params.filename); // sanitize
    const filePath = getBackendPath(`trading_signals/monitor_history/${filename}`);
    const sheets = await listXlsxSheets(filePath);
    res.json(sheets.map(s => s.name));
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/monitor-history/:filename/:sheet
router.get('/:filename/:sheet', async (req, res) => {
  try {
    const filename = path.basename(req.params.filename); // sanitize
    const filePath = getBackendPath(`trading_signals/monitor_history/${filename}`);
    const data = await parseXlsxSheet(filePath, decodeURIComponent(req.params.sheet));
    res.json(data);
  } catch (err: any) {
    if (err.message.includes('not found')) {
      return res.status(404).json({ error: err.message });
    }
    res.status(500).json({ error: err.message });
  }
});

export default router;
