import { Router } from 'express';
import path from 'path';
import { findLatestFile, listFiles, extractTimestamp, deduplicateLatest } from '../utils/fileUtils.js';
import { listXlsxSheets, parseXlsxSheet } from '../utils/xlsxParser.js';
import { getBackendPath } from '../config.js';

const router = Router();

// GET /api/diagnostic/latest — list all sheets in latest diagnostic xlsx
router.get('/latest', async (req, res) => {
  try {
    const dir = getBackendPath('historical_runs');
    const latestFile = await findLatestFile(dir, 'wf_diagnostic_*.xlsx');
    const sheets = await listXlsxSheets(latestFile);
    res.json({ file: path.basename(latestFile), sheets });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/diagnostic/latest/:sheet — read a specific sheet
router.get('/latest/:sheet', async (req, res) => {
  try {
    const { sheet } = req.params;
    const dir = getBackendPath('historical_runs');
    const latestFile = await findLatestFile(dir, 'wf_diagnostic_*.xlsx');
    const data = await parseXlsxSheet(latestFile, decodeURIComponent(sheet));
    res.json(data);
  } catch (err: any) {
    if (err.message.includes('not found')) {
      return res.status(404).json({ error: err.message });
    }
    res.status(500).json({ error: err.message });
  }
});

// GET /api/diagnostic/list — list all diagnostic xlsx files
router.get('/list', async (req, res) => {
  try {
    const dir = getBackendPath('historical_runs');
    const allFiles = await listFiles(dir, 'wf_diagnostic_*.xlsx');
    const files = deduplicateLatest(allFiles);
    const result = files.map(f => ({
      filename: path.basename(f),
      timestamp: extractTimestamp(f),
    }));
    res.json(result);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

export default router;
