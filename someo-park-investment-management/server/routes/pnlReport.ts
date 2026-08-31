import { Router } from 'express';
import fs from 'fs';
import path from 'path';
import { getBackendPath } from '../config.js';

const router = Router();

// Per-strategy PnL report dirs. All use the same pnl_report_YYYYMMDD.pdf naming.
// ssrs/aiss switched to portfolio_ledger reports on 2026-07-02.
// Legacy sources (switch back by pointing these entries at the old dirs /
// reverting api.ts to the /api/{strategy}/tearsheet endpoints):
//   ssrs: qlib tearsheets  qlib-main/sector_rotation/report/output/tearsheet_*.pdf
//   aiss: qlib tearsheets  qlib-main/semiconductor_strategy/report/output/tearsheet_*.pdf
const PNL_DIRS: Record<string, string> = {
  mrpt: 'trading_signals/pnl_reports',                                   // MRPT/MTFS pairs
  ssrs: 'qlib-main/sector_rotation/trading_signals/pnl_reports',        // portfolio_ledger
  aiss: 'qlib-main/semiconductor_strategy/trading_signals/pnl_reports', // portfolio_ledger
  aeus: 'qlib-main/electric_utilities_strategy/trading_signals/pnl_reports', // portfolio_ledger
};
const pnlDir = (strategy?: any): string =>
  PNL_DIRS[typeof strategy === 'string' ? strategy : 'mrpt'] || PNL_DIRS.mrpt;

// GET /api/pnl-report/latest — returns the latest PnL report PDF (by date in filename)
router.get('/latest', (req, res) => {
  try {
    const dir = getBackendPath(pnlDir(req.query.strategy));
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
    const filePath = getBackendPath(path.join(pnlDir(req.query.strategy), filename));

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
router.get('/', (req, res) => {
  try {
    const dir = getBackendPath(pnlDir(req.query.strategy));
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
