import { Router } from 'express';
import { readJsonFile, findLatestFile, listFiles, extractTimestamp, deduplicateLatest } from '../utils/fileUtils.js';
import { getBackendPath } from '../config.js';
import path from 'path';

const router = Router();

// GET /api/signals/latest/mrpt
router.get('/latest/:strategy', async (req, res) => {
  try {
    const { strategy } = req.params;
    if (!['mrpt', 'mtfs'].includes(strategy)) {
      return res.status(400).json({ error: 'Invalid strategy' });
    }
    const dir = getBackendPath('trading_signals');
    const latestFile = await findLatestFile(dir, `${strategy}_signals_*.json`);
    const data = await readJsonFile(latestFile);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/signals/combined/latest
router.get('/combined/latest', async (req, res) => {
  try {
    const dir = getBackendPath('trading_signals');
    const latestFile = await findLatestFile(dir, 'combined_signals_*.json');
    const data = await readJsonFile(latestFile);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/signals/list
router.get('/list', async (req, res) => {
  try {
    const dir = getBackendPath('trading_signals');
    const allFiles = await listFiles(dir, '*_signals_*.json');
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
