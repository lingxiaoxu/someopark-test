import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import path from 'path';
import { fileURLToPath } from 'url';
import { API_PORT } from './config.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

import inventoryRoutes from './routes/inventory.js';
import signalsRoutes from './routes/signals.js';
import dailyReportRoutes from './routes/dailyReport.js';
import regimeRoutes from './routes/regime.js';
import walkForwardRoutes from './routes/walkForward.js';
import pairUniverseRoutes from './routes/pairUniverse.js';
import diagnosticRoutes from './routes/diagnostic.js';
import monitorHistoryRoutes from './routes/monitorHistory.js';
import chatRoutes from './routes/chat.js';
import morphChatRoutes from './routes/morphChat.js';
import sandboxRoutes from './routes/sandbox.js';
import publishRoutes from './routes/publish.js';
import controllerNavRoutes from './routes/controllerNav.js';
import pnlReportRoutes from './routes/pnlReport.js';
import riskReportRoutes from './routes/riskReport.js';
import agentRoutes from './routes/agent.js';
import sectorRotationRoutes from './routes/sectorRotation.js';
import semiconductorRoutes from './routes/semiconductor.js';
import microfootballAnalyzeRoutes from './routes/microfootballAnalyze.js';
import macroAnalyzeRoutes from './routes/macroAnalyze.js';
import { registerAllTools } from './tools/index.js';

const app = express();
app.use(cors());
app.use(express.json({ limit: '10mb' }));

// Serve static data files (strategy_performance.json, etc.)
app.use('/data', express.static(path.join(__dirname, '..', 'public', 'data')));

// Heavy MicroFootball-sim assets (replay.gif + 10MB trajectory.jsonl per sim). These live in
// server/sim_assets/ — OUTSIDE public/ — so `vite build` never copies them into dist/Firebase;
// they are served over the same cloudflared tunnel as /data. No auth (like /data).
app.use('/sim', express.static(path.join(__dirname, 'sim_assets')));

// API key guard — only enforced when SP_API_KEY env is set (i.e. when exposed via ngrok)
const SP_API_KEY = process.env.SP_API_KEY;
if (SP_API_KEY) {
  app.use('/api', (req, res, next) => {
    const key = req.headers['x-api-key'] || req.query.key;
    if (key === SP_API_KEY) return next();
    res.status(401).json({ error: 'Unauthorized' });
  });
  console.log('API key protection enabled');
}

// Routes
app.use('/api/inventory', inventoryRoutes);
app.use('/api/signals', signalsRoutes);
app.use('/api/daily-report', dailyReportRoutes);
app.use('/api/regime', regimeRoutes);
app.use('/api/wf', walkForwardRoutes);
app.use('/api/pairs', pairUniverseRoutes);
app.use('/api/diagnostic', diagnosticRoutes);
app.use('/api/monitor-history', monitorHistoryRoutes);
app.use('/api/chat', chatRoutes);
app.use('/api/morph-chat', morphChatRoutes);
app.use('/api/sandbox', sandboxRoutes);
app.use('/api/controller-nav', controllerNavRoutes);
app.use('/api/publish', publishRoutes);
app.use('/api/pnl-report', pnlReportRoutes);
app.use('/api/risk-report', riskReportRoutes);
app.use('/api/ssrs', sectorRotationRoutes);
app.use('/api/aiss', semiconductorRoutes);
app.use('/api/agent', agentRoutes);
app.use('/api/microfootball', microfootballAnalyzeRoutes);
app.use('/api/macro', macroAnalyzeRoutes);

// Register Someo Agent tools
registerAllTools();

// Health check
app.get('/api/health', (_req, res) => {
  res.json({ status: 'ok', time: new Date().toISOString() });
});

app.listen(API_PORT, () => {
  console.log(`API server running on http://localhost:${API_PORT}`);
});
