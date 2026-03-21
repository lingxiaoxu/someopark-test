import { Router } from 'express';
import { readJsonFile, readTextFile, findLatestFile, listFiles, extractTimestamp, deduplicateLatest } from '../utils/fileUtils.js';
import { getBackendPath } from '../config.js';
import path from 'path';

const router = Router();

// GET /api/daily-report/latest
router.get('/latest', async (req, res) => {
  try {
    const dir = getBackendPath('trading_signals');
    const latestFile = await findLatestFile(dir, 'daily_report_[0-9]*.json');
    const data = await readJsonFile(latestFile);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/daily-report/latest/txt
router.get('/latest/txt', async (req, res) => {
  try {
    const dir = getBackendPath('trading_signals');
    const latestFile = await findLatestFile(dir, 'daily_report_[0-9]*.txt');
    const text = await readTextFile(latestFile);
    res.type('text/plain').send(text);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/daily-report/list
router.get('/list', async (req, res) => {
  try {
    const dir = getBackendPath('trading_signals');
    const allFiles = await listFiles(dir, 'daily_report_[0-9]*.json');
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
