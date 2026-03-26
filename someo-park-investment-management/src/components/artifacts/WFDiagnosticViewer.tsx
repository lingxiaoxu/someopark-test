import React, { useState, useEffect, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import { getDiagnosticSheets, getDiagnosticSheet } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import PairBadge from '../PairBadge';

// Detect pair-like cell: header contains "pair" and value looks like "TICKER/TICKER"
const PAIR_RE = /^[A-Z]{1,5}\/[A-Z]{1,5}$/;
function isPairCell(header: string, val: any): boolean {
  if (typeof val !== 'string') return false;
  const h = header.toLowerCase();
  if (!h.includes('pair')) return false;
  // Single pair or comma-separated pairs
  return val.split(/,\s*/).every(p => PAIR_RE.test(p.trim()));
}

function renderPairCell(val: string) {
  const parts = val.split(/,\s*/);
  if (parts.length === 1) return <PairBadge pair={val.trim()} compact />;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {parts.map((p, i) => <span key={i}><PairBadge pair={p.trim()} compact /></span>)}
    </span>
  );
}

function correlationColor(value: number): string {
  if (value >= 0) {
    const g = Math.round(150 * value);
    return `rgb(${30}, ${g + 40}, ${30})`;
  } else {
    const r = Math.round(150 * Math.abs(value));
    return `rgb(${r + 40}, ${30}, ${30})`;
  }
}

// Headers whose values should always render as integers (no decimals)
const INTEGER_HEADERS = new Set(['window', 'n_pairs', 'n_selected', 'rank', 'count', 'num_pairs', 'num_selected', 'window_idx']);

function isIntegerColumn(header: string): boolean {
  const h = header.toLowerCase();
  return INTEGER_HEADERS.has(h) || h.startsWith('n_');
}

function isPnlColumn(header: string): boolean {
  const h = header.toLowerCase();
  return h.endsWith('_pnl') || h === 'oos_pnl' || h === 'is_pnl' || h === 'pnl';
}

function isDateColumn(header: string): boolean {
  const h = header.toLowerCase();
  return h === 'start' || h === 'end' || h === 'test_start' || h === 'test_end' || h === 'date' || h.endsWith('_date');
}

// Convert Excel serial date number or raw number to YYYY-MM-DD
function formatDateValue(val: any): string {
  if (val == null) return '';
  const s = String(val);
  if (s.includes('-') && s.length >= 8) return s; // already formatted
  const num = Number(val);
  if (!isNaN(num) && num > 1900 && num < 2200) {
    // Looks like a year (e.g. 2025.00, 2026.00) — format as integer year
    if (Number.isInteger(num) || Math.abs(num - Math.round(num)) < 0.001) return Math.round(num).toString();
  }
  if (!isNaN(num) && num > 40000 && num < 60000) {
    // Excel serial date
    const d = new Date((num - 25569) * 86400000);
    return d.toISOString().slice(0, 10);
  }
  return s;
}

function fmtPnl(val: number): string {
  const abs = Math.abs(val);
  const formatted = abs.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return val < 0 ? `-$${formatted}` : `$${formatted}`;
}

function fmtCell(val: any, header?: string): string {
  if (val == null) return '';
  if (header && isDateColumn(header)) return formatDateValue(val);
  if (typeof val === 'number') {
    if (header && isIntegerColumn(header)) return Math.round(val).toString();
    if (header && isPnlColumn(header)) return fmtPnl(val);
    const abs = Math.abs(val);
    if (abs >= 1000) return val.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (abs < 0.01 && abs > 0) return val.toFixed(4);
    return val.toFixed(2);
  }
  return String(val);
}

function isSectionRow(row: any, headers: string[]): boolean {
  const firstVal = String(row[headers[0]] || '');
  return /^[═━─]{3,}|^[═]{2,}/.test(firstVal) || (firstVal.includes('═══') && headers.slice(1).every(h => !row[h]));
}

function getSectionTitle(row: any, headers: string[]): string {
  const val = String(row[headers[0]] || '');
  return val.replace(/[═━─]/g, '').trim();
}

// GenericTable with section separators, negative coloring, and window filter
function GenericTable({ headers, rows, sheetName }: { headers: string[]; rows: any[]; sheetName?: string }) {
  // Detect WINDOW column for filtering
  const windowHeader = headers.find(h => h.toLowerCase() === 'window' || h.toLowerCase() === 'window_idx');
  const windowValues = useMemo(() => {
    if (!windowHeader) return [];
    const raw = [...new Set(rows.map(r => r[windowHeader]).filter(v => v != null))];
    // Check if values are numeric or string
    const allNumeric = raw.every(v => !isNaN(Number(v)));
    if (allNumeric) {
      return raw.map(v => Math.round(Number(v))).sort((a, b) => a - b);
    }
    // String window values — sort naturally
    return raw.map(v => String(v)).sort();
  }, [rows, windowHeader]);
  const [selectedWindow, setSelectedWindow] = useState<string | number | null>(null);

  const filteredRows = useMemo(() => {
    if (selectedWindow === null || !windowHeader) return rows;
    if (typeof selectedWindow === 'number') {
      return rows.filter(r => Math.round(Number(r[windowHeader])) === selectedWindow);
    }
    return rows.filter(r => String(r[windowHeader]) === selectedWindow);
  }, [rows, selectedWindow, windowHeader]);

  const sections = useMemo(() => {
    const result: { title?: string; rows: any[] }[] = [];
    let current: { title?: string; rows: any[] } = { rows: [] };

    for (const row of filteredRows) {
      if (isSectionRow(row, headers)) {
        if (current.rows.length > 0 || current.title) result.push(current);
        current = { title: getSectionTitle(row, headers), rows: [] };
      } else {
        current.rows.push(row);
      }
    }
    if (current.rows.length > 0 || current.title) result.push(current);
    return result;
  }, [headers, filteredRows]);

  const hasSections = sections.some(s => s.title);

  const windowFilter = windowValues.length > 1 && (
    <div className="flex items-center flex-wrap gap-1.5 mb-3">
      <span className="text-[10px] text-[var(--text-muted)] font-medium mr-1">Window:</span>
      <button
        onClick={() => setSelectedWindow(null)}
        className={`px-2 py-0.5 text-[10px] font-medium rounded transition-colors ${selectedWindow === null ? 'bg-[var(--accent-primary)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]'}`}
      >All</button>
      {windowValues.map(w => (
        <button
          key={w}
          onClick={() => setSelectedWindow(w)}
          className={`px-2 py-0.5 text-[10px] font-medium rounded transition-colors ${selectedWindow === w ? 'bg-[var(--accent-primary)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]'}`}
        >{w}</button>
      ))}
    </div>
  );

  if (hasSections) {
    return (
      <div className="space-y-4">
        {windowFilter}
        {sections.map((section, si) => {
          // Detect macro snapshot section embedded in Executive Summary
          const isMacroSnapshot = section.title && (section.title.includes('宏观环境快照') || section.title.includes('daily_report'));
          if (isMacroSnapshot && section.rows.length > 0) {
            // Convert rows to single-column format for DailyReportSnapshotTable
            const fakeRows = section.rows.map(r => {
              // The text is in the first non-empty field
              const val = Object.values(r).find(v => v != null && String(v).trim()) || '';
              return { col: String(val) };
            });
            return (
              <div key={si}>
                <div className="px-3 py-2 bg-[var(--accent-primary)]/10 border border-[var(--accent-primary)]/20 rounded-t-lg text-xs font-medium text-[var(--accent-primary)]">
                  {section.title}
                </div>
                <DailyReportSnapshotTable rows={fakeRows} />
              </div>
            );
          }

          return (
            <div key={si}>
              {section.title && (
                <div className="px-3 py-2 bg-[var(--accent-primary)]/10 border border-[var(--accent-primary)]/20 rounded-t-lg text-xs font-medium text-[var(--accent-primary)]">
                  {section.title}
                </div>
              )}
              {section.rows.length > 0 && (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm text-left">
                    <thead className="text-[10px] text-[var(--text-muted)] uppercase bg-[var(--bg-secondary)] sticky top-0 z-10">
                      <tr>
                        {headers.map((h, i) => (
                          <th key={i} className="px-3 py-2 font-medium whitespace-nowrap">{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[var(--border-subtle)] text-xs">
                      {section.rows.map((row, ri) => (
                        <tr key={ri} className="hover:bg-[var(--bg-secondary)] transition-colors">
                          {headers.map((h, j) => {
                            const val = row[h];
                            const isDate = isDateColumn(h);
                            const num = typeof val === 'number' ? val : parseFloat(val);
                            const isNum = !isDate && !isNaN(num) && typeof val !== 'boolean' && val !== '' && val != null;
                            const isNeg = isNum && num < 0 && !isDateColumn(h);
                            const isPair = isPairCell(h, val);
                            return (
                              <td key={j} className={`px-3 py-2 ${isNum && !isDate ? 'font-mono' : ''} whitespace-nowrap`}
                                style={{ color: isNeg ? 'var(--error)' : 'var(--text-primary)' }}>
                                {isPair ? renderPairCell(String(val)) : isDate ? formatDateValue(val) : isNum ? fmtCell(num, h) : String(val ?? '')}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div>
      {windowFilter}
      <div className="overflow-x-auto">
        <table className="w-full text-sm text-left">
          <thead className="text-[10px] text-[var(--text-muted)] uppercase bg-[var(--bg-secondary)] sticky top-0 z-10">
            <tr>
              {headers.map((h, i) => (
                <th key={i} className="px-3 py-2 font-medium whitespace-nowrap">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--border-subtle)] text-xs">
            {filteredRows.map((row, i) => (
              <tr key={i} className="hover:bg-[var(--bg-secondary)] transition-colors">
                {headers.map((h, j) => {
                  const val = row[h];
                  const isDate = isDateColumn(h);
                  const num = typeof val === 'number' ? val : parseFloat(val);
                  const isNum = !isDate && !isNaN(num) && typeof val !== 'boolean' && val !== '' && val != null;
                  const isNeg = isNum && num < 0;
                  const isPair = isPairCell(h, val);
                  return (
                    <td key={j} className={`px-3 py-2 ${isNum && !isDate ? 'font-mono' : ''} whitespace-nowrap`}
                      style={{ color: isNeg ? 'var(--error)' : 'var(--text-primary)' }}>
                      {isPair ? renderPairCell(String(val)) : isDate ? formatDateValue(val) : isNum ? fmtCell(num, h) : String(val ?? '')}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CorrelationHeatmap({ headers, rows }: { headers: string[]; rows: any[] }) {
  const labels = rows.map(r => r[headers[0]] || '');
  const numHeaders = headers.slice(1);

  return (
    <div className="overflow-x-auto">
      <div className="inline-grid gap-[1px]" style={{ gridTemplateColumns: `80px repeat(${numHeaders.length}, 32px)` }}>
        <div />
        {numHeaders.map((h, i) => (
          <div key={i} className="text-[8px] text-[var(--text-muted)] text-center truncate" title={h}>{h.slice(0, 4)}</div>
        ))}
        {rows.map((row, ri) => (
          <React.Fragment key={ri}>
            <div className="text-[9px] text-[var(--text-muted)] truncate pr-1 flex items-center" title={labels[ri]}>{labels[ri]}</div>
            {numHeaders.map((h, ci) => {
              const val = typeof row[h] === 'number' ? row[h] : parseFloat(row[h]);
              return (
                <div
                  key={ci}
                  className="w-8 h-8 rounded-sm flex items-center justify-center text-[7px] text-white/80 cursor-default"
                  style={{ backgroundColor: isNaN(val) ? 'var(--bg-tertiary)' : correlationColor(val) }}
                  title={`${labels[ri]} × ${h}: ${isNaN(val) ? 'N/A' : val.toFixed(3)}`}
                >
                  {isNaN(val) ? '' : val.toFixed(1)}
                </div>
              );
            })}
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

// Split a value token that may contain two merged numbers like "-0.5033 +0.2043"
function splitMergedValues(token: string): string[] {
  const m = token.match(/^([-+]?\d[\d.]*[%万k]*(?:\([^)]+\))?)\s+([-+]?\d[\d.]*[%万k]*(?:\([^)]+\))?)$/);
  if (m) return [m[1], m[2]];
  return [token];
}

// Parse a data line from fixed-width macro indicator text
function parseIndicatorLine(line: string): string[] | null {
  const trimmed = line.trim();
  if (!trimmed || /^-{5,}/.test(trimmed) || /^[═]{3,}/.test(trimmed)) return null;

  // Split: indicator name is everything up to the first numeric value after 2+ spaces
  const m = trimmed.match(/^(.+?)\s{2,}([-+]?\d.*)$/);
  if (!m) {
    // Short lines like "VIX z-score   +1.41   偏高" or "EFFR年变化   -0.69%   降息"
    const m2 = trimmed.match(/^(.+?)\s{2,}([-+]?\d\S*)\s{2,}(.+)$/);
    if (m2) return [m2[1].trim(), m2[2], '', '', '', '', '', '', '', m2[3].trim()];
    const m3 = trimmed.match(/^(.+?)\s{2,}([-+]?\d\S*)$/);
    if (m3) return [m3[1].trim(), m3[2]];
    return null;
  }

  const name = m[1].trim();
  const rest = m[2];

  // Tokenize the rest: split by 3+ spaces, then 2+ spaces, then detect merged number pairs
  const coarseTokens = rest.split(/\s{3,}/);
  const tokens: string[] = [];
  for (const ct of coarseTokens) {
    const sub = ct.trim().split(/\s{2,}/);
    for (const s of sub) {
      const sv = s.trim();
      if (!sv) continue;
      tokens.push(...splitMergedValues(sv));
    }
  }

  // Short indicator lines (e.g. "VIX z-score  +1.41  偏高") have only value + interpretation.
  // The interpretation (last non-numeric token) should go in the last column (解读), not column 2.
  if (tokens.length === 2) {
    const lastToken = tokens[1];
    const isNumeric = /^[-+]?\d/.test(lastToken);
    if (!isNumeric) {
      // value + interpretation → pad middle columns so interpretation lands in col 9 (index 9 = 解读)
      return [name, tokens[0], '', '', '', '', '', '', '', lastToken];
    }
  }

  return [name, ...tokens];
}

// Parse the Daily_Report_Snapshot single-column fixed-width text into a proper table
function DailyReportSnapshotTable({ rows }: { rows: any[] }) {
  const parsed = useMemo(() => {
    const lines = rows.map(r => String(Object.values(r)[0] || ''));

    // Find header line and separator
    const headerIdx = lines.findIndex(l => l.includes('当前值') || l.includes('指标'));
    const sepIdx = lines.findIndex(l => /^\s*-{10,}/.test(l));
    const dataStartIdx = Math.max(headerIdx + 1, sepIdx + 1, 1);
    const sourceLine = (lines.find(l => l.includes('来源')) || '').trim();

    // Parse header
    let colHeaders: string[] = [];
    if (headerIdx >= 0) {
      colHeaders = lines[headerIdx].trim().split(/\s{2,}/).filter(Boolean);
    }
    if (colHeaders.length < 2) {
      colHeaders = ['指标', '当前值(日期)', '前值(日期)', '变化', '频', '30obs均', 'vs30', '90obs均', 'vs90', '解读'];
    }

    // Parse data lines using regex tokenization
    const dataLines: string[][] = [];
    for (let i = dataStartIdx; i < lines.length; i++) {
      const parsed = parseIndicatorLine(lines[i]);
      if (parsed && parsed.length >= 2) dataLines.push(parsed);
    }

    return { sourceLine, colHeaders, dataLines };
  }, [rows]);

  return (
    <div className="space-y-2">
      {parsed.sourceLine && (
        <div className="text-[10px] text-[var(--text-muted)] mb-2">{parsed.sourceLine}</div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-xs text-left">
          <thead className="text-[10px] text-[var(--text-muted)] uppercase bg-[var(--bg-secondary)] sticky top-0 z-10">
            <tr>
              {parsed.colHeaders.map((h, i) => (
                <th key={i} className="px-2 py-1.5 font-medium whitespace-nowrap">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--border-subtle)]">
            {parsed.dataLines.map((parts, i) => (
              <tr key={i} className="hover:bg-[var(--bg-secondary)] transition-colors">
                {parsed.colHeaders.map((_, j) => {
                  const val = parts[j] || '';
                  const isNeg = val.startsWith('-') && /[0-9]/.test(val);
                  return (
                    <td key={j} className={`px-2 py-1.5 whitespace-nowrap ${j > 0 ? 'font-mono' : ''}`}
                      style={{ color: isNeg ? 'var(--error)' : 'var(--text-primary)' }}>
                      {val}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function WFDiagnosticViewer() {
  const { t } = useTranslation();
  const { data: sheetsInfo, loading: loadingSheets, error: errorSheets, refetch } = useApi(() => getDiagnosticSheets(), []);
  const [activeTab, setActiveTab] = useState<string>('');
  const [sheetData, setSheetData] = useState<any>(null);
  const [sheetLoading, setSheetLoading] = useState(false);

  const tabs = (sheetsInfo?.sheets || []).map((s: any) => s.name).filter((n: string) => n.toLowerCase() !== 'sheet1');

  useEffect(() => {
    if (tabs.length > 0 && !activeTab) {
      setActiveTab(tabs[0]);
    }
  }, [tabs]);

  useEffect(() => {
    if (!activeTab) return;
    setSheetLoading(true);
    getDiagnosticSheet(activeTab)
      .then(setSheetData)
      .catch(() => setSheetData(null))
      .finally(() => setSheetLoading(false));
  }, [activeTab]);

  if (loadingSheets) return <LoadingState />;
  if (errorSheets) return <ErrorState message={errorSheets} onRetry={refetch} />;

  const isCorrSheet = activeTab.includes('Cross_Corr') || activeTab.includes('Corr_Shift');
  const isDailySnapshot = activeTab === 'Daily_Report_Snapshot';

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between mb-3 shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)]">{t('wfDiagnostic.title', { file: sheetsInfo?.file })}</div>
      </div>

      <div className="flex flex-wrap gap-1.5 mb-4 shrink-0">
        {tabs.map((tab: string) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-2 py-1 text-[10px] font-medium rounded-md transition-colors ${activeTab === tab ? 'bg-[var(--accent-primary)] text-white' : 'bg-[var(--bg-primary)] text-[var(--text-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-secondary)]'}`}
          >
            {tab.replace(/_/g, ' ')}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-4">
        {sheetLoading ? (
          <LoadingState />
        ) : sheetData ? (
          isDailySnapshot ? (
            <DailyReportSnapshotTable rows={sheetData.rows} />
          ) : isCorrSheet ? (
            <CorrelationHeatmap headers={sheetData.headers} rows={sheetData.rows} />
          ) : (
            <GenericTable headers={sheetData.headers} rows={sheetData.rows} sheetName={activeTab} />
          )
        ) : (
          <div className="text-sm text-[var(--text-muted)] text-center py-8">{t('common.selectSheet')}</div>
        )}
      </div>
    </div>
  );
}
