import { Router } from 'express';
import path from 'path';
import { readJsonFile, listFiles, extractTimestamp, getFileStats } from '../utils/fileUtils.js';
import { getBackendPath } from '../config.js';

const router = Router();

// GET /api/inventory/history/mrpt — list all history snapshots
// (must be before /:strategy to avoid matching "history" as strategy)
router.get('/history/:strategy', async (req, res) => {
  try {
    const { strategy } = req.params;
    if (!['mrpt', 'mtfs'].includes(strategy)) {
      return res.status(400).json({ error: 'Invalid strategy' });
    }
    const dir = getBackendPath('inventory_history');
    const files = await listFiles(dir, `inventory_${strategy}_*.json`);
    const result = await Promise.all(files.map(async (f) => {
      const stats = await getFileStats(f);
      const data = await readJsonFile(f);
      const activePairs = Object.values(data.pairs || {})
        .filter((p: any) => p.direction !== null).length;
      return {
        filename: path.basename(f),
        timestamp: extractTimestamp(f),
        size: stats.size,
        activePairs,
      };
    }));
    res.json(result);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/inventory/history/mrpt/inventory_mrpt_20260321_123653.json
router.get('/history/:strategy/:filename', async (req, res) => {
  try {
    const filename = path.basename(req.params.filename); // sanitize
    const filePath = getBackendPath(`inventory_history/${filename}`);
    const data = await readJsonFile(filePath);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/inventory/mrpt  or  /api/inventory/mtfs
router.get('/:strategy', async (req, res) => {
  try {
    const { strategy } = req.params;
    if (!['mrpt', 'mtfs'].includes(strategy)) {
      return res.status(400).json({ error: 'Invalid strategy. Use mrpt or mtfs.' });
    }
    const filePath = getBackendPath(`inventory_${strategy}.json`);
    const data = await readJsonFile(filePath);
    res.json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

export default router;
