// In dev: empty (Vite proxy handles it). In prod: set VITE_API_URL to your tunnel/server URL.
const API_BASE = import.meta.env.VITE_API_URL || '';
const API_KEY = import.meta.env.VITE_API_KEY || '';

export function apiHeaders(): Record<string, string> {
  const h: Record<string, string> = {};
  if (API_KEY) h['x-api-key'] = API_KEY;
  if (API_BASE) h['ngrok-skip-browser-warning'] = '1';
  return h;
}

export { API_BASE };

async function fetchApi<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { headers: apiHeaders() });
  if (!res.ok) throw new Error(`API error: ${res.status} ${res.statusText}`);
  return res.json();
}

async function fetchText(path: string): Promise<string> {
  const res = await fetch(`${API_BASE}${path}`, { headers: apiHeaders() });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.text();
}

// ═══════════════════════════════════════════════════════════════════════
// Inventory
// MRPT/MTFS: /api/inventory/{strategy}
// SSRS:      /api/ssrs/inventory
// ═══════════════════════════════════════════════════════════════════════
// qlib-based strategies (SSRS sector rotation, AISS AI-semiconductor) share an
// identical route shape under /api/{strategy}/...; MRPT/MTFS use the legacy paths.
const QLIB = (s?: string): s is 'ssrs' | 'aiss' => s === 'ssrs' || s === 'aiss';

export const getInventory = async (strategy: string) => {
  if (QLIB(strategy)) return fetchApi<any>(`/api/${strategy}/inventory`);
  try {
    return await fetchApi<any>(`/api/inventory/${strategy}`);
  } catch {
    return fetchApi<any>(`/data/inventory_${strategy}.json`);
  }
};
export const getInventoryHistory = (strategy: string) =>
  QLIB(strategy)
    ? fetchApi<any[]>(`/api/${strategy}/inventory/history`)
    : fetchApi<any[]>(`/api/inventory/history/${strategy}`);
export const getInventorySnapshot = (strategy: string, filename: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/inventory/history/${filename}`)
    : fetchApi<any>(`/api/inventory/history/${strategy}/${filename}`);

// ═══════════════════════════════════════════════════════════════════════
// Signals
// ═══════════════════════════════════════════════════════════════════════
export const getLatestSignals = (strategy: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/signals/latest`)
    : fetchApi<any>(`/api/signals/latest/${strategy}`);
export const getLatestCombinedSignals = () =>
  fetchApi<any>(`/api/signals/combined/latest`);

// ═══════════════════════════════════════════════════════════════════════
// Daily Report
// ═══════════════════════════════════════════════════════════════════════
export const getLatestDailyReport = (strategy?: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/daily-report/latest`)
    : fetchApi<any>('/api/daily-report/latest');
export const getLatestDailyReportTxt = (strategy?: string) =>
  QLIB(strategy)
    ? fetchText(`/api/${strategy}/daily-report/latest/txt`)
    : fetchText('/api/daily-report/latest/txt');

// ═══════════════════════════════════════════════════════════════════════
// Regime
// ═══════════════════════════════════════════════════════════════════════
export const getLatestRegime = (strategy?: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/regime/latest`)
    : fetchApi<any>('/api/regime/latest');

// ═══════════════════════════════════════════════════════════════════════
// Walk-Forward
// ═══════════════════════════════════════════════════════════════════════
export const getWFSummary = (strategy: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/wf/summary`)
    : fetchApi<any>(`/api/wf/summary/${strategy}`);
export const getOOSEquityCurve = (strategy: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/equity-curve`)
    : fetchApi<any[]>(`/api/wf/equity-curve/${strategy}`);
export const getOOSPairSummary = (strategy: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/wf/param-oos`)
    : fetchApi<any[]>(`/api/wf/pair-summary/${strategy}`);
export const getDSRLog = (strategy: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/wf/fold-grid`)
    : fetchApi<any[]>(`/api/wf/dsr-log/${strategy}`);

// ═══════════════════════════════════════════════════════════════════════
// Pair Universe / Sector Universe / Stock Universe
// SSRS → sector-universe (ETFs); AISS → stock-universe (individual stocks)
// ═══════════════════════════════════════════════════════════════════════
export const getPairUniverse = (strategy: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>('/api/ssrs/sector-universe')
    : strategy === 'aiss'
    ? fetchApi<any>('/api/aiss/stock-universe')
    : fetchApi<any>(`/api/pairs/${strategy}`);
export const getPairDb = (collection: string) =>
  fetchApi<any>(`/api/pairs/db/${collection}`);

// ═══════════════════════════════════════════════════════════════════════
// WF xlsx viewer / File Structure
// ═══════════════════════════════════════════════════════════════════════
export const getWFXlsxList = (strategy: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/files/list`)
    : fetchApi<string[]>(`/api/wf/xlsx/list?strategy=${strategy}`);
export const getWFXlsxSheets = (strategy: string, relPath: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/portfolio-history/${encodeURIComponent(relPath)}/sheets`)
    : fetchApi<any>(`/api/wf/xlsx/sheets?strategy=${strategy}&path=${encodeURIComponent(relPath)}`);
export const getWFXlsxSheet = (strategy: string, relPath: string, sheet: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/portfolio-history/${encodeURIComponent(relPath)}/${encodeURIComponent(sheet)}`)
    : fetchApi<any>(`/api/wf/xlsx/sheet?strategy=${strategy}&path=${encodeURIComponent(relPath)}&sheet=${encodeURIComponent(sheet)}`);

// ═══════════════════════════════════════════════════════════════════════
// Diagnostic
// ═══════════════════════════════════════════════════════════════════════
export const getDiagnosticSheets = (strategy?: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/diagnostic/latest`)
    : fetchApi<any>('/api/diagnostic/latest');
export const getDiagnosticSheet = (sheet: string, strategy?: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/diagnostic/latest/${encodeURIComponent(sheet)}`)
    : fetchApi<any>(`/api/diagnostic/latest/${encodeURIComponent(sheet)}`);

// ═══════════════════════════════════════════════════════════════════════
// PnL Report / Tearsheet
// ═══════════════════════════════════════════════════════════════════════
export const getPnlReportList = (strategy?: string) =>
  QLIB(strategy)
    ? fetchApi<any[]>(`/api/${strategy}/tearsheet/list`)
    : fetchApi<{ date: string; filename: string }[]>('/api/pnl-report');
export const getPnlReportUrl = (date?: string, strategy?: string) => {
  if (QLIB(strategy)) {
    const p = date ? `/api/${strategy}/tearsheet/${date}` : `/api/${strategy}/tearsheet/list`;
    const keyParam = API_KEY ? `?key=${API_KEY}` : '';
    return `${API_BASE}${p}${keyParam}`;
  }
  const p = date ? `/api/pnl-report/${date}` : '/api/pnl-report/latest';
  const keyParam = API_KEY ? `?key=${API_KEY}` : '';
  return `${API_BASE}${p}${keyParam}`;
};

// ═══════════════════════════════════════════════════════════════════════
// Risk Management Report
// ═══════════════════════════════════════════════════════════════════════
export const getRiskReportList = () =>
  fetchApi<{ date: string; timestamp: string; filename: string }[]>('/api/risk-report');
export const getRiskReportUrl = (ts?: string) => {
  const p = ts ? `/api/risk-report/${ts}` : '/api/risk-report/latest';
  const keyParam = API_KEY ? `?key=${API_KEY}` : '';
  return `${API_BASE}${p}${keyParam}`;
};

// ═══════════════════════════════════════════════════════════════════════
// Monitor / Portfolio History
// ═══════════════════════════════════════════════════════════════════════
export const getMonitorHistoryList = (strategy?: string) =>
  QLIB(strategy)
    ? fetchApi<any[]>(`/api/${strategy}/portfolio-history/list`)
    : fetchApi<any[]>('/api/monitor-history/list');
export const getMonitorHistorySheets = (filename: string, strategy?: string) =>
  QLIB(strategy)
    ? fetchApi<string[]>(`/api/${strategy}/portfolio-history/${encodeURIComponent(filename)}/sheets`)
    : fetchApi<string[]>(`/api/monitor-history/${encodeURIComponent(filename)}/sheets`);
export const getMonitorHistorySheet = (filename: string, sheet: string, strategy?: string) =>
  QLIB(strategy)
    ? fetchApi<any>(`/api/${strategy}/portfolio-history/${encodeURIComponent(filename)}/${encodeURIComponent(sheet)}`)
    : fetchApi<any>(`/api/monitor-history/${encodeURIComponent(filename)}/${encodeURIComponent(sheet)}`);

// ═══════════════════════════════════════════════════════════════════════
// SSRS / AISS: Smart Select + Strategy Performance + Params list
// (strategy defaults to ssrs for backward compatibility)
// ═══════════════════════════════════════════════════════════════════════
export const getSRSmartSelect = (strategy: string = 'ssrs') =>
  fetchApi<any>(`/api/${strategy}/smart-select`);
export const getSRStrategyPerformance = (strategy: string = 'ssrs') =>
  fetchApi<any>(`/api/${strategy}/strategy-performance`);
export const getSRParamsList = (strategy: string = 'ssrs') =>
  fetchApi<string[]>(`/api/${strategy}/params/list`);

// === Someo Agent SSE streaming ===
export async function* callAgent(
  messages: any[],
  model: any,
  sessionId: string,
): AsyncGenerator<any> {
  const res = await fetch(`${API_BASE}/api/agent`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...apiHeaders() },
    body: JSON.stringify({ messages, model, sessionId }),
  })
  if (!res.ok) throw new Error(`Agent API error: ${res.status}`)

  const reader = res.body!.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const lines = buf.split('\n')
    buf = lines.pop() || ''
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try { yield JSON.parse(line.slice(6)) } catch { /* skip malformed */ }
      }
    }
  }
}

export async function answerAgentQuestion(sessionId: string, answer: string) {
  return fetch(`${API_BASE}/api/agent/answer`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...apiHeaders() },
    body: JSON.stringify({ sessionId, answer }),
  })
}

// ═══════════════════════════════════════════════════════════════════════
// Prediction Market (World Cup 2026) — static JSON synced from
// prediction_market/data/output/ into public/data/ (see scripts/sync_prediction_data.mjs).
// All read-only; same fetchApi convention as the other /data/*.json sources.
// ═══════════════════════════════════════════════════════════════════════
export const getWCOverview     = () => fetchApi<any>('/data/frontend_overview.json');
export const getWCChampion     = () => fetchApi<any>('/data/worldcup_model.json');
export const getWCDivergence   = () => fetchApi<any>('/data/xv_matches.json');
export const getWCChampionXV   = () => fetchApi<any>('/data/xv_champion.json');
export const getWCPerformance  = () => fetchApi<any>('/data/performance_report.json');
export const getWCRisk         = () => fetchApi<any>('/data/risk_report.json');
export const getWCCalibration  = () => fetchApi<any>('/data/oos_report.json');
export const getWCInplay       = () => fetchApi<any>('/data/inplay_signals.json');
export const getWCInplayLive   = () => fetchApi<any>('/data/inplay_live.json');
export const getWCBacktest     = () => fetchApi<any>('/data/backtest.json');
export const getWCSquad        = () => fetchApi<any>('/data/squad.json');
export const getWCUpcoming     = () => fetchApi<any>('/data/upcoming.json');
