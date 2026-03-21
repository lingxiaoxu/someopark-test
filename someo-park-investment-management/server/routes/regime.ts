import { Router } from 'express';
import { readJsonFile, findLatestFile } from '../utils/fileUtils.js';
import { getBackendPath } from '../config.js';

const router = Router();

// GET /api/regime/latest — extract regime field from daily report
router.get('/latest', async (req, res) => {
  try {
    const dir = getBackendPath('trading_signals');
    const latestFile = await findLatestFile(dir, 'daily_report_[0-9]*.json');
    const data = await readJsonFile(latestFile);
    res.json(data.regime);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

export default router;
