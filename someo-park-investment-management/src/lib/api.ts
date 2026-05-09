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
export const getInventory = async (strategy: string) => {
  if (strategy === 'ssrs') return fetchApi<any>('/api/ssrs/inventory');
  try {
    return await fetchApi<any>(`/api/inventory/${strategy}`);
  } catch {
    return fetchApi<any>(`/data/inventory_${strategy}.json`);
  }
};
export const getInventoryHistory = (strategy: string) =>
  strategy === 'ssrs'
    ? fetchApi<any[]>('/api/ssrs/inventory/history')
    : fetchApi<any[]>(`/api/inventory/history/${strategy}`);
export const getInventorySnapshot = (strategy: string, filename: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>(`/api/ssrs/inventory/history/${filename}`)
    : fetchApi<any>(`/api/inventory/history/${strategy}/${filename}`);

// ═══════════════════════════════════════════════════════════════════════
// Signals
// ═══════════════════════════════════════════════════════════════════════
export const getLatestSignals = (strategy: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>('/api/ssrs/signals/latest')
    : fetchApi<any>(`/api/signals/latest/${strategy}`);
export const getLatestCombinedSignals = () =>
  fetchApi<any>(`/api/signals/combined/latest`);

// ═══════════════════════════════════════════════════════════════════════
// Daily Report
// ═══════════════════════════════════════════════════════════════════════
export const getLatestDailyReport = (strategy?: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>('/api/ssrs/daily-report/latest')
    : fetchApi<any>('/api/daily-report/latest');
export const getLatestDailyReportTxt = (strategy?: string) =>
  strategy === 'ssrs'
    ? fetchText('/api/ssrs/daily-report/latest/txt')
    : fetchText('/api/daily-report/latest/txt');

// ═══════════════════════════════════════════════════════════════════════
// Regime
// ═══════════════════════════════════════════════════════════════════════
export const getLatestRegime = (strategy?: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>('/api/ssrs/regime/latest')
    : fetchApi<any>('/api/regime/latest');

// ═══════════════════════════════════════════════════════════════════════
// Walk-Forward
// ═══════════════════════════════════════════════════════════════════════
export const getWFSummary = (strategy: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>('/api/ssrs/wf/summary')
    : fetchApi<any>(`/api/wf/summary/${strategy}`);
export const getOOSEquityCurve = (strategy: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>('/api/ssrs/equity-curve')
    : fetchApi<any[]>(`/api/wf/equity-curve/${strategy}`);
export const getOOSPairSummary = (strategy: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>('/api/ssrs/wf/param-oos')
    : fetchApi<any[]>(`/api/wf/pair-summary/${strategy}`);
export const getDSRLog = (strategy: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>('/api/ssrs/wf/fold-grid')
    : fetchApi<any[]>(`/api/wf/dsr-log/${strategy}`);

// ═══════════════════════════════════════════════════════════════════════
// Pair Universe / Sector Universe
// ═══════════════════════════════════════════════════════════════════════
export const getPairUniverse = (strategy: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>('/api/ssrs/sector-universe')
    : fetchApi<any>(`/api/pairs/${strategy}`);
export const getPairDb = (collection: string) =>
  fetchApi<any>(`/api/pairs/db/${collection}`);

// ═══════════════════════════════════════════════════════════════════════
// WF xlsx viewer / File Structure
// ═══════════════════════════════════════════════════════════════════════
export const getWFXlsxList = (strategy: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>('/api/ssrs/files/list')
    : fetchApi<string[]>(`/api/wf/xlsx/list?strategy=${strategy}`);
export const getWFXlsxSheets = (strategy: string, relPath: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>(`/api/ssrs/portfolio-history/${encodeURIComponent(relPath)}/sheets`)
    : fetchApi<any>(`/api/wf/xlsx/sheets?strategy=${strategy}&path=${encodeURIComponent(relPath)}`);
export const getWFXlsxSheet = (strategy: string, relPath: string, sheet: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>(`/api/ssrs/portfolio-history/${encodeURIComponent(relPath)}/${encodeURIComponent(sheet)}`)
    : fetchApi<any>(`/api/wf/xlsx/sheet?strategy=${strategy}&path=${encodeURIComponent(relPath)}&sheet=${encodeURIComponent(sheet)}`);

// ═══════════════════════════════════════════════════════════════════════
// Diagnostic
// ═══════════════════════════════════════════════════════════════════════
export const getDiagnosticSheets = (strategy?: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>('/api/ssrs/diagnostic/latest')
    : fetchApi<any>('/api/diagnostic/latest');
export const getDiagnosticSheet = (sheet: string, strategy?: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>(`/api/ssrs/diagnostic/latest/${encodeURIComponent(sheet)}`)
    : fetchApi<any>(`/api/diagnostic/latest/${encodeURIComponent(sheet)}`);

// ═══════════════════════════════════════════════════════════════════════
// PnL Report / Tearsheet
// ═══════════════════════════════════════════════════════════════════════
export const getPnlReportList = (strategy?: string) =>
  strategy === 'ssrs'
    ? fetchApi<any[]>('/api/ssrs/tearsheet/list')
    : fetchApi<{ date: string; filename: string }[]>('/api/pnl-report');
export const getPnlReportUrl = (date?: string, strategy?: string) => {
  if (strategy === 'ssrs') {
    const p = date ? `/api/ssrs/tearsheet/${date}` : '/api/ssrs/tearsheet/list';
    const keyParam = API_KEY ? `?key=${API_KEY}` : '';
    return `${API_BASE}${p}${keyParam}`;
  }
  const p = date ? `/api/pnl-report/${date}` : '/api/pnl-report/latest';
  const keyParam = API_KEY ? `?key=${API_KEY}` : '';
  return `${API_BASE}${p}${keyParam}`;
};

// ═══════════════════════════════════════════════════════════════════════
// Monitor / Portfolio History
// ═══════════════════════════════════════════════════════════════════════
export const getMonitorHistoryList = (strategy?: string) =>
  strategy === 'ssrs'
    ? fetchApi<any[]>('/api/ssrs/portfolio-history/list')
    : fetchApi<any[]>('/api/monitor-history/list');
export const getMonitorHistorySheets = (filename: string, strategy?: string) =>
  strategy === 'ssrs'
    ? fetchApi<string[]>(`/api/ssrs/portfolio-history/${encodeURIComponent(filename)}/sheets`)
    : fetchApi<string[]>(`/api/monitor-history/${encodeURIComponent(filename)}/sheets`);
export const getMonitorHistorySheet = (filename: string, sheet: string, strategy?: string) =>
  strategy === 'ssrs'
    ? fetchApi<any>(`/api/ssrs/portfolio-history/${encodeURIComponent(filename)}/${encodeURIComponent(sheet)}`)
    : fetchApi<any>(`/api/monitor-history/${encodeURIComponent(filename)}/${encodeURIComponent(sheet)}`);

// ═══════════════════════════════════════════════════════════════════════
// SSRS-only: Smart Select + Strategy Performance + Params list
// ═══════════════════════════════════════════════════════════════════════
export const getSRSmartSelect = () =>
  fetchApi<any>('/api/ssrs/smart-select');
export const getSRStrategyPerformance = () =>
  fetchApi<any>('/api/ssrs/strategy-performance');
export const getSRParamsList = () =>
  fetchApi<string[]>('/api/ssrs/params/list');

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
