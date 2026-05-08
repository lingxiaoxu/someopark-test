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
// SR:        /api/sr/inventory
// ═══════════════════════════════════════════════════════════════════════
export const getInventory = async (strategy: string) => {
  if (strategy === 'sr') return fetchApi<any>('/api/sr/inventory');
  try {
    return await fetchApi<any>(`/api/inventory/${strategy}`);
  } catch {
    return fetchApi<any>(`/data/inventory_${strategy}.json`);
  }
};
export const getInventoryHistory = (strategy: string) =>
  strategy === 'sr'
    ? fetchApi<any[]>('/api/sr/inventory/history')
    : fetchApi<any[]>(`/api/inventory/history/${strategy}`);
export const getInventorySnapshot = (strategy: string, filename: string) =>
  strategy === 'sr'
    ? fetchApi<any>(`/api/sr/inventory/history/${filename}`)
    : fetchApi<any>(`/api/inventory/history/${strategy}/${filename}`);

// ═══════════════════════════════════════════════════════════════════════
// Signals
// ═══════════════════════════════════════════════════════════════════════
export const getLatestSignals = (strategy: string) =>
  strategy === 'sr'
    ? fetchApi<any>('/api/sr/signals/latest')
    : fetchApi<any>(`/api/signals/latest/${strategy}`);
export const getLatestCombinedSignals = () =>
  fetchApi<any>(`/api/signals/combined/latest`);

// ═══════════════════════════════════════════════════════════════════════
// Daily Report
// ═══════════════════════════════════════════════════════════════════════
export const getLatestDailyReport = (strategy?: string) =>
  strategy === 'sr'
    ? fetchApi<any>('/api/sr/daily-report/latest')
    : fetchApi<any>('/api/daily-report/latest');
export const getLatestDailyReportTxt = (strategy?: string) =>
  strategy === 'sr'
    ? fetchText('/api/sr/daily-report/latest/txt')
    : fetchText('/api/daily-report/latest/txt');

// ═══════════════════════════════════════════════════════════════════════
// Regime
// ═══════════════════════════════════════════════════════════════════════
export const getLatestRegime = (strategy?: string) =>
  strategy === 'sr'
    ? fetchApi<any>('/api/sr/regime/latest')
    : fetchApi<any>('/api/regime/latest');

// ═══════════════════════════════════════════════════════════════════════
// Walk-Forward
// ═══════════════════════════════════════════════════════════════════════
export const getWFSummary = (strategy: string) =>
  strategy === 'sr'
    ? fetchApi<any>('/api/sr/wf/summary')
    : fetchApi<any>(`/api/wf/summary/${strategy}`);
export const getOOSEquityCurve = (strategy: string) =>
  strategy === 'sr'
    ? fetchApi<any>('/api/sr/equity-curve')
    : fetchApi<any[]>(`/api/wf/equity-curve/${strategy}`);
export const getOOSPairSummary = (strategy: string) =>
  strategy === 'sr'
    ? fetchApi<any>('/api/sr/wf/param-oos')
    : fetchApi<any[]>(`/api/wf/pair-summary/${strategy}`);
export const getDSRLog = (strategy: string) =>
  strategy === 'sr'
    ? fetchApi<any>('/api/sr/wf/fold-grid')
    : fetchApi<any[]>(`/api/wf/dsr-log/${strategy}`);

// ═══════════════════════════════════════════════════════════════════════
// Pair Universe / Sector Universe
// ═══════════════════════════════════════════════════════════════════════
export const getPairUniverse = (strategy: string) =>
  strategy === 'sr'
    ? fetchApi<any>('/api/sr/sector-universe')
    : fetchApi<any>(`/api/pairs/${strategy}`);
export const getPairDb = (collection: string) =>
  fetchApi<any>(`/api/pairs/db/${collection}`);

// ═══════════════════════════════════════════════════════════════════════
// WF xlsx viewer / File Structure
// ═══════════════════════════════════════════════════════════════════════
export const getWFXlsxList = (strategy: string) =>
  strategy === 'sr'
    ? fetchApi<any>('/api/sr/files/list')
    : fetchApi<string[]>(`/api/wf/xlsx/list?strategy=${strategy}`);
export const getWFXlsxSheets = (strategy: string, relPath: string) =>
  strategy === 'sr'
    ? fetchApi<any>(`/api/sr/portfolio-history/${encodeURIComponent(relPath)}/sheets`)
    : fetchApi<any>(`/api/wf/xlsx/sheets?strategy=${strategy}&path=${encodeURIComponent(relPath)}`);
export const getWFXlsxSheet = (strategy: string, relPath: string, sheet: string) =>
  strategy === 'sr'
    ? fetchApi<any>(`/api/sr/portfolio-history/${encodeURIComponent(relPath)}/${encodeURIComponent(sheet)}`)
    : fetchApi<any>(`/api/wf/xlsx/sheet?strategy=${strategy}&path=${encodeURIComponent(relPath)}&sheet=${encodeURIComponent(sheet)}`);

// ═══════════════════════════════════════════════════════════════════════
// Diagnostic
// ═══════════════════════════════════════════════════════════════════════
export const getDiagnosticSheets = (strategy?: string) =>
  strategy === 'sr'
    ? fetchApi<any>('/api/sr/diagnostic/latest')
    : fetchApi<any>('/api/diagnostic/latest');
export const getDiagnosticSheet = (sheet: string, strategy?: string) =>
  strategy === 'sr'
    ? fetchApi<any>(`/api/sr/diagnostic/latest/${encodeURIComponent(sheet)}`)
    : fetchApi<any>(`/api/diagnostic/latest/${encodeURIComponent(sheet)}`);

// ═══════════════════════════════════════════════════════════════════════
// PnL Report / Tearsheet
// ═══════════════════════════════════════════════════════════════════════
export const getPnlReportList = (strategy?: string) =>
  strategy === 'sr'
    ? fetchApi<any[]>('/api/sr/tearsheet/list')
    : fetchApi<{ date: string; filename: string }[]>('/api/pnl-report');
export const getPnlReportUrl = (date?: string, strategy?: string) => {
  if (strategy === 'sr') {
    const p = date ? `/api/sr/tearsheet/${date}` : '/api/sr/tearsheet/list';
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
  strategy === 'sr'
    ? fetchApi<any[]>('/api/sr/portfolio-history/list')
    : fetchApi<any[]>('/api/monitor-history/list');
export const getMonitorHistorySheets = (filename: string, strategy?: string) =>
  strategy === 'sr'
    ? fetchApi<string[]>(`/api/sr/portfolio-history/${encodeURIComponent(filename)}/sheets`)
    : fetchApi<string[]>(`/api/monitor-history/${encodeURIComponent(filename)}/sheets`);
export const getMonitorHistorySheet = (filename: string, sheet: string, strategy?: string) =>
  strategy === 'sr'
    ? fetchApi<any>(`/api/sr/portfolio-history/${encodeURIComponent(filename)}/${encodeURIComponent(sheet)}`)
    : fetchApi<any>(`/api/monitor-history/${encodeURIComponent(filename)}/${encodeURIComponent(sheet)}`);

// ═══════════════════════════════════════════════════════════════════════
// SR-only: Smart Select + Strategy Performance + Params list
// ═══════════════════════════════════════════════════════════════════════
export const getSRSmartSelect = () =>
  fetchApi<any>('/api/sr/smart-select');
export const getSRStrategyPerformance = () =>
  fetchApi<any>('/api/sr/strategy-performance');
export const getSRParamsList = () =>
  fetchApi<string[]>('/api/sr/params/list');

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
