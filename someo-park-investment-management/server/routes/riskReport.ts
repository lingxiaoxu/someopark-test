import { Router } from 'express';
import fs from 'fs';
import path from 'path';
import { getBackendPath } from '../config.js';

const router = Router();

const RISK_DIR = 'trading_signals/risk_management';
// risk_report_YYYYMMDD_HHMMSS.pdf
const RISK_RE = /^risk_report_(\d{8})_(\d{6})\.pdf$/;

// GET /api/risk-report/latest — latest risk report PDF (by timestamp in filename)
router.get('/latest', (_req, res) => {
  try {
    const dir = getBackendPath(RISK_DIR);
    if (!fs.existsSync(dir)) {
      return res.status(404).json({ error: 'Risk reports directory not found' });
    }

    const files = fs.readdirSync(dir)
      .filter(f => RISK_RE.test(f))
      .sort()
      .reverse();

    if (files.length === 0) {
      return res.status(404).json({ error: 'No risk report found' });
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

// GET /api/risk-report/:ts — specific report by timestamp (YYYYMMDD_HHMMSS)
router.get('/:ts', (req, res) => {
  try {
    const { ts } = req.params;
    // guard against path traversal — only accept the exact timestamp shape
    if (!/^\d{8}_\d{6}$/.test(ts)) {
      return res.status(400).json({ error: 'Invalid timestamp format' });
    }
    const filename = `risk_report_${ts}.pdf`;
    const filePath = getBackendPath(path.join(RISK_DIR, filename));

    if (!fs.existsSync(filePath)) {
      return res.status(404).json({ error: `Risk report for ${ts} not found` });
    }

    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', `inline; filename="${filename}"`);
    res.setHeader('X-Filename', filename);
    fs.createReadStream(filePath).pipe(res);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/risk-report — list available reports (newest first)
router.get('/', (_req, res) => {
  try {
    const dir = getBackendPath(RISK_DIR);
    if (!fs.existsSync(dir)) {
      return res.json([]);
    }

    const files = fs.readdirSync(dir)
      .map(f => {
        const m = f.match(RISK_RE);
        return m ? { date: m[1], timestamp: `${m[1]}_${m[2]}`, filename: f } : null;
      })
      .filter(Boolean)
      .sort((a: any, b: any) => (a.timestamp < b.timestamp ? 1 : -1));  // newest first

    res.json(files);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

export default router;
