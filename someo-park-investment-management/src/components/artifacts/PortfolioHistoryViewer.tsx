import React, { useState, useEffect } from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import { getMonitorHistoryList, getMonitorHistorySheets, getMonitorHistorySheet } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';

const fmtAccounting = (v: number) => {
  const abs = Math.abs(v);
  const s = abs.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return v < 0 ? `-${s}` : s;
};

function GenericTable({ headers, rows }: { headers: string[]; rows: any[] }) {
  return (
    <div className="overflow-x-auto overflow-y-auto flex-1">
      <table className="w-full text-sm text-left">
        <thead className="text-[10px] text-[var(--text-muted)] uppercase bg-[var(--bg-secondary)] sticky top-0 z-10">
          <tr>
            {headers.map((h, i) => (
              <th key={i} className="px-3 py-2 font-medium whitespace-nowrap">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--border-subtle)] text-xs">
          {rows.map((row, i) => (
            <tr key={i} className="hover:bg-[var(--bg-secondary)] transition-colors">
              {headers.map((h, j) => {
                const val = row[h];
                const isNum = typeof val === 'number';
                return (
                  <td key={j} className={`px-3 py-2 font-mono whitespace-nowrap ${isNum && val < 0 ? 'text-[var(--error)]' : 'text-[var(--text-primary)]'}`}>
                    {isNum ? fmtAccounting(val) : String(val ?? '')}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function PortfolioHistoryViewer({ params }: { params?: any }) {
  const { t } = useTranslation();
  const { data: fileList, loading: loadingList, error: errorList, refetch } = useApi(() => getMonitorHistoryList(), []);
  const [selectedFile, setSelectedFile] = useState<string>('');
  const [sheets, setSheets] = useState<string[]>([]);
  const [activeSheet, setActiveSheet] = useState<string>('');
  const [sheetData, setSheetData] = useState<any>(null);
  const [sheetLoading, setSheetLoading] = useState(false);

  // Auto-select first file
  useEffect(() => {
    if (fileList && fileList.length > 0 && !selectedFile) {
      setSelectedFile(fileList[0].filename);
    }
  }, [fileList]);

  // Load sheets when file selected, skip empty Sheet1
  useEffect(() => {
    if (!selectedFile) return;
    getMonitorHistorySheets(selectedFile)
      .then(s => {
        const filtered = s.filter((n: string) => n.toLowerCase() !== 'sheet1');
        setSheets(filtered);
        if (filtered.length > 0) setActiveSheet(filtered[0]);
      })
      .catch(() => setSheets([]));
  }, [selectedFile]);

  // Load sheet data when sheet selected
  useEffect(() => {
    if (!selectedFile || !activeSheet) return;
    setSheetLoading(true);
    getMonitorHistorySheet(selectedFile, activeSheet)
      .then(setSheetData)
      .catch(() => setSheetData(null))
      .finally(() => setSheetLoading(false));
  }, [selectedFile, activeSheet]);

  if (loadingList) return <LoadingState />;
  if (errorList) return <ErrorState message={errorList} onRetry={refetch} />;
  if (!fileList || fileList.length === 0) return <div className="text-sm text-[var(--text-muted)] text-center py-8">{t('portfolioHistory.noFiles')}</div>;

  // Detect chart-able sheets
  const isChartSheet = /pnl_history|recorded_vars|equity_history/.test(activeSheet);

  // Transform data for chart
  const chartData = sheetData?.rows?.map((row: any) => {
    const dateCol = sheetData.headers.find((h: string) => /date/i.test(h)) || sheetData.headers[0];
    return { ...row, _date: row[dateCol] };
  }) || [];

  const numericHeaders = sheetData?.headers?.filter((h: string) => {
    if (/date/i.test(h)) return false;
    return sheetData.rows.some((r: any) => typeof r[h] === 'number');
  }) || [];

  return (
    <div className="flex flex-col h-full">
      {/* File selector */}
      <div className="flex items-center gap-2 mb-3 shrink-0">
        <span className="text-xs text-[var(--text-muted)]">{t('portfolioHistory.file')}</span>
        <select
          value={selectedFile}
          onChange={e => setSelectedFile(e.target.value)}
          className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md px-2 py-1.5 text-xs focus:outline-none focus:border-[var(--accent-primary)] text-[var(--text-primary)] max-w-[300px] truncate"
        >
          {fileList.map((f: any) => (
            <option key={f.filename} value={f.filename}>{f.pair} ({f.strategy}) - {f.timestamp}</option>
          ))}
        </select>
      </div>

      {/* Sheet tabs - show ALL sheets */}
      <div className="flex overflow-x-auto gap-1.5 mb-4 pb-2 border-b border-[var(--border-subtle)] shrink-0">
        {sheets.map(sheet => (
          <button
            key={sheet}
            onClick={() => setActiveSheet(sheet)}
            className={`px-2.5 py-1 text-[10px] font-medium rounded-md whitespace-nowrap transition-colors ${activeSheet === sheet ? 'bg-[var(--accent-primary)] text-white' : 'bg-[var(--bg-primary)] text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] border border-[var(--border-subtle)]'}`}
          >
            {sheet.replace(/_/g, ' ')}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="flex-1 min-h-0 bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-4">
        {sheetLoading ? (
          <LoadingState />
        ) : !sheetData ? (
          <div className="text-sm text-[var(--text-muted)] text-center py-8">{t('common.selectSheet')}</div>
        ) : isChartSheet && chartData.length > 0 ? (
          <div className="h-full flex flex-col">
            <div className="text-sm font-medium mb-4 text-[var(--text-primary)]">{activeSheet.replace(/_/g, ' ')}</div>
            <div className="flex-1 min-h-[300px]">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border-subtle)" vertical={false} />
                  <XAxis dataKey="_date" stroke="var(--text-muted)" fontSize={10} tickMargin={8} minTickGap={30} />
                  <YAxis stroke="var(--text-muted)" fontSize={10} />
                  <Tooltip
                    contentStyle={{ backgroundColor: 'var(--bg-primary)', borderColor: 'var(--border-subtle)', borderRadius: '8px', color: 'var(--text-primary)' }}
                  />
                  {activeSheet === 'recorded_vars' && (
                    <>
                      <ReferenceLine y={2} stroke="var(--error)" strokeDasharray="3 3" />
                      <ReferenceLine y={-2} stroke="var(--success)" strokeDasharray="3 3" />
                      <ReferenceLine y={0} stroke="var(--text-muted)" />
                    </>
                  )}
                  {numericHeaders.slice(0, 3).map((h: string, i: number) => (
                    <Line key={h} type="monotone" dataKey={h} stroke={['var(--accent-primary)', '#f59e0b', 'var(--success)'][i]} strokeWidth={1.5} dot={false} />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        ) : (
          <GenericTable headers={sheetData.headers} rows={sheetData.rows} />
        )}
      </div>
    </div>
  );
}
