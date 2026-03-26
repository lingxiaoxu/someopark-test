import { Router } from 'express';
import fs from 'fs';
import path from 'path';
import { getBackendPath } from '../config.js';

const router = Router();

const PNL_DIR = 'trading_signals/pnl_reports';

// GET /api/pnl-report/latest — returns the latest PnL report PDF (by date in filename)
router.get('/latest', (_req, res) => {
  try {
    const dir = getBackendPath(PNL_DIR);
    if (!fs.existsSync(dir)) {
      return res.status(404).json({ error: 'PnL reports directory not found' });
    }

    const files = fs.readdirSync(dir)
      .filter(f => f.startsWith('pnl_report_') && f.endsWith('.pdf'))
      .sort()
      .reverse();

    if (files.length === 0) {
      return res.status(404).json({ error: 'No PnL report found' });
    }

    const latest = files[0];
    const filePath = path.join(dir, latest);
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', `inline; filename="${latest}"`);
    res.setHeader('X-Filename', latest);
    fs.createReadStream(filePath).pipe(res);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/pnl-report/:date — returns a specific date's PnL report (YYYYMMDD)
router.get('/:date', (req, res) => {
  try {
    const { date } = req.params;
    const filename = `pnl_report_${date}.pdf`;
    const filePath = getBackendPath(path.join(PNL_DIR, filename));

    if (!fs.existsSync(filePath)) {
      return res.status(404).json({ error: `PnL report for ${date} not found` });
    }

    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', `inline; filename="${filename}"`);
    res.setHeader('X-Filename', filename);
    fs.createReadStream(filePath).pipe(res);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/pnl-report/list — returns available report dates
router.get('/', (_req, res) => {
  try {
    const dir = getBackendPath(PNL_DIR);
    if (!fs.existsSync(dir)) {
      return res.json([]);
    }

    const files = fs.readdirSync(dir)
      .filter(f => f.startsWith('pnl_report_') && f.endsWith('.pdf'))
      .sort()
      .reverse()
      .map(f => {
        const match = f.match(/pnl_report_(\d{8})\.pdf/);
        return match ? { date: match[1], filename: f } : null;
      })
      .filter(Boolean);

    res.json(files);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

export default router;
