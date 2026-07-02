import { Router } from 'express';
import fs from 'fs';
import path from 'path';
import { getBackendPath } from '../config.js';

const router = Router();

// Per-strategy risk report sources.
// ssrs/aiss switched to portfolio_ledger reports on 2026-07-02
// (risk_report_YYYYMMDD.pdf, date-only naming). Legacy source for switchback:
//   both previously had NO risk reports in this viewer (pairs-only);
//   pairs source unchanged: trading_signals/risk_management (YYYYMMDD_HHMMSS).
const RISK_SOURCES: Record<string, { dir: string; re: RegExp }> = {
  mrpt: { dir: 'trading_signals/risk_management',
          re: /^risk_report_(\d{8})_(\d{6})\.pdf$/ },
  ssrs: { dir: 'qlib-main/sector_rotation/trading_signals/risk_management',
          re: /^risk_report_(\d{8})\.pdf$/ },
  aiss: { dir: 'qlib-main/semiconductor_strategy/trading_signals/risk_management',
          re: /^risk_report_(\d{8})\.pdf$/ },
};
const riskSource = (strategy?: any) =>
  RISK_SOURCES[typeof strategy === 'string' ? strategy : 'mrpt'] || RISK_SOURCES.mrpt;
// file id = YYYYMMDD_HHMMSS (pairs) or YYYYMMDD (ledger)
const ID_RE = /^\d{8}(_\d{6})?$/;

// GET /api/risk-report/latest — latest risk report PDF (by timestamp in filename)
router.get('/latest', (req, res) => {
  try {
    const { dir: srcDir, re: srcRe } = riskSource(req.query.strategy);
    const dir = getBackendPath(srcDir);
    if (!fs.existsSync(dir)) {
      return res.status(404).json({ error: 'Risk reports directory not found' });
    }

    const files = fs.readdirSync(dir)
      .filter(f => srcRe.test(f))
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
    // guard against path traversal — only accept the exact id shape
    if (!ID_RE.test(ts)) {
      return res.status(400).json({ error: 'Invalid timestamp format' });
    }
    const filename = `risk_report_${ts}.pdf`;
    const filePath = getBackendPath(path.join(riskSource(req.query.strategy).dir, filename));

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
router.get('/', (req, res) => {
  try {
    const { dir: srcDir, re: srcRe } = riskSource(req.query.strategy);
    const dir = getBackendPath(srcDir);
    if (!fs.existsSync(dir)) {
      return res.json([]);
    }

    const files = fs.readdirSync(dir)
      .map(f => {
        const m = f.match(srcRe);
        // ledger files are date-only → id (timestamp field) = YYYYMMDD
        return m ? { date: m[1], timestamp: m[2] ? `${m[1]}_${m[2]}` : m[1], filename: f } : null;
      })
      .filter(Boolean)
      .sort((a: any, b: any) => (a.timestamp < b.timestamp ? 1 : -1));  // newest first

    res.json(files);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

export default router;
