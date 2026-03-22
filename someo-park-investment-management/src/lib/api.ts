const API_BASE = '';  // Use Vite proxy, so relative paths work

async function fetchApi<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`API error: ${res.status} ${res.statusText}`);
  return res.json();
}

async function fetchText(path: string): Promise<string> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.text();
}

// Inventory — try live API first, fallback to static snapshot for Firebase hosting
export const getInventory = async (strategy: string) => {
  try {
    return await fetchApi<any>(`/api/inventory/${strategy}`);
  } catch {
    return fetchApi<any>(`/data/inventory_${strategy}.json`);
  }
};
export const getInventoryHistory = (strategy: string) =>
  fetchApi<any[]>(`/api/inventory/history/${strategy}`);
export const getInventorySnapshot = (strategy: string, filename: string) =>
  fetchApi<any>(`/api/inventory/history/${strategy}/${filename}`);

// Signals
export const getLatestSignals = (strategy: string) =>
  fetchApi<any>(`/api/signals/latest/${strategy}`);
export const getLatestCombinedSignals = () =>
  fetchApi<any>(`/api/signals/combined/latest`);

// Daily Report
export const getLatestDailyReport = () =>
  fetchApi<any>(`/api/daily-report/latest`);
export const getLatestDailyReportTxt = () =>
  fetchText(`/api/daily-report/latest/txt`);

// Regime
export const getLatestRegime = () =>
  fetchApi<any>(`/api/regime/latest`);

// Walk-Forward
export const getWFSummary = (strategy: string) =>
  fetchApi<any>(`/api/wf/summary/${strategy}`);
export const getOOSEquityCurve = (strategy: string) =>
  fetchApi<any[]>(`/api/wf/equity-curve/${strategy}`);
export const getOOSPairSummary = (strategy: string) =>
  fetchApi<any[]>(`/api/wf/pair-summary/${strategy}`);
export const getDSRLog = (strategy: string) =>
  fetchApi<any[]>(`/api/wf/dsr-log/${strategy}`);

// Pair Universe
export const getPairUniverse = (strategy: string) =>
  fetchApi<any>(`/api/pairs/${strategy}`);
export const getPairDb = (collection: string) =>
  fetchApi<any>(`/api/pairs/db/${collection}`);

// WF xlsx viewer
export const getWFXlsxList = (strategy: string) =>
  fetchApi<string[]>(`/api/wf/xlsx/list?strategy=${strategy}`);
export const getWFXlsxSheets = (strategy: string, relPath: string) =>
  fetchApi<any>(`/api/wf/xlsx/sheets?strategy=${strategy}&path=${encodeURIComponent(relPath)}`);
export const getWFXlsxSheet = (strategy: string, relPath: string, sheet: string) =>
  fetchApi<any>(`/api/wf/xlsx/sheet?strategy=${strategy}&path=${encodeURIComponent(relPath)}&sheet=${encodeURIComponent(sheet)}`);

// Diagnostic
export const getDiagnosticSheets = () =>
  fetchApi<any>(`/api/diagnostic/latest`);
export const getDiagnosticSheet = (sheet: string) =>
  fetchApi<any>(`/api/diagnostic/latest/${encodeURIComponent(sheet)}`);

// Monitor / Portfolio History
export const getMonitorHistoryList = () =>
  fetchApi<any[]>(`/api/monitor-history/list`);
export const getMonitorHistorySheets = (filename: string) =>
  fetchApi<string[]>(`/api/monitor-history/${encodeURIComponent(filename)}/sheets`);
export const getMonitorHistorySheet = (filename: string, sheet: string) =>
  fetchApi<any>(`/api/monitor-history/${encodeURIComponent(filename)}/${encodeURIComponent(sheet)}`);
