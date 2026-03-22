import 'dotenv/config';
import express from 'express';
import cors from 'cors';
import { API_PORT } from './config.js';

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

const app = express();
app.use(cors());
app.use(express.json({ limit: '10mb' }));

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
app.use('/api/publish', publishRoutes);

// Health check
app.get('/api/health', (_req, res) => {
  res.json({ status: 'ok', time: new Date().toISOString() });
});

app.listen(API_PORT, () => {
  console.log(`API server running on http://localhost:${API_PORT}`);
});
