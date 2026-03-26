import React, { useState, useEffect, useMemo } from 'react';
import { Folder, FileText, FileSpreadsheet, FileJson, Image as ImageIcon, ChevronRight, ChevronDown, Info, Database, FileCode, File, LayoutTemplate, Activity, Settings, Calendar, BarChart2, Play } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useApi } from '../../hooks/useApi';
import { getWFSummary, getWFXlsxList, getWFXlsxSheets, getWFXlsxSheet } from '../../lib/api';
import LoadingState from '../LoadingState';
import PairBadge from '../PairBadge';
import ErrorState from '../ErrorState';

type FileNode = {
  name: string;
  type: 'folder' | 'excel' | 'csv' | 'json' | 'png' | 'txt' | 'log';
  description?: string;
  children?: FileNode[];
  content?: any;
};

const excelSheets = [
  { name: 'Sheet1', desc: 'Summary' },
  { name: 'acc_daily_pnl_history', desc: 'Accumulated daily PnL' },
  { name: 'acc_interest_history', desc: 'Accumulated interest' },
  { name: 'asset_history', desc: 'Total assets' },
  { name: 'equity_history', desc: 'NAV curve' },
  { name: 'interest_expense_history', desc: 'Interest expense' },
  { name: 'liability_history', desc: 'Liabilities' },
  { name: 'value_history', desc: 'Portfolio value' },
  { name: 'asset_cash_history', desc: 'Cash assets' },
  { name: 'liability_loan_history', desc: 'Loan liabilities' },
  { name: 'price_history', desc: 'Position prices' },
  { name: 'share_history', desc: 'Position shares' },
  { name: 'percentage_history', desc: 'Position %' },
  { name: 'cost_basis_history', desc: 'Cost basis' },
  { name: 'hedge_history', desc: 'Hedge ratio' },
  { name: 'asset_securities_history', desc: 'Security assets' },
  { name: 'liability_securities_history', desc: 'Short positions' },
  { name: 'share_history_by_pair', desc: 'Shares by pair' },
  { name: 'cost_basis_by_pair', desc: 'Cost by pair' },
  { name: 'finished_trades_pnl', desc: 'Completed trade PnL' },
  { name: 'finished_trades_pnl_by_pair', desc: 'PnL by pair' },
  { name: 'total_cost_history', desc: 'Total cost' },
  { name: 'total_cost_history_by_pair', desc: 'Cost by pair' },
  { name: 'percentage_history_by_pair', desc: 'Position % by pair' },
  { name: 'daily_pnl_history', desc: 'Daily PnL' },
  { name: 'acc_security_pnl_history', desc: 'Accumulated security PnL' },
  { name: 'acc_sec_pnl_by_pair', desc: 'Security PnL by pair' },
  { name: 'acc_pair_trade_pnl_history', desc: 'Accumulated pair trade PnL' },
  { name: 'dod_security_pnl_history', desc: 'Day-over-day security PnL' },
  { name: 'dod_pair_trade_pnl_history', desc: 'Day-over-day pair trade PnL' },
  { name: 'statistical_test_history', desc: 'Statistical tests (z-score etc.)' },
  { name: 'recorded_vars', desc: 'Strategy recorded variables' },
  { name: 'stop_loss_history', desc: 'Stop loss events' },
  { name: 'pair_trade_history', desc: 'Pair trade open/close records' },
  { name: 'max_drawdown_history', desc: 'Max drawdown records' }
];

const mrptParams = "aggressive, balanced_plus, conservative, conservative_no_leverage, deep_dislocation, deep_entry_quick_exit, default, fast_reentry, fast_signal, fast_signal_tight_stop, flash_hold, high_entry, high_leverage, high_turnover, high_vol_specialist, long_z_short_v, low_entry, low_vol_specialist, medium_signal_high_leverage, no_leverage, patient_hold, quick_exit, short_z_long_v, slow_reentry, slow_signal, stable_signal_quick_exit, static_threshold, symmetric_exit, tight_stop, vol_adaptive, vol_agnostic, vol_gated";

const mtfsParams = "aggressive, balanced, beta_neutral, beta_neutral_long_term, conservative, crash_defensive, crash_tolerant, default, entry_filter_kalman, entry_threshold_strong, fast_rebalance, fast_strict, kalman_aggressive, kalman_hedge, let_profits_run, long_term_kalman, long_term_tilt, monthly_aligned_windows, no_reversal_protection, no_skip_month, raw_momentum, raw_momentum_kalman, sensitive_reversal, short_term_beta_neutral, short_term_tilt, slow_rebalance, trend_leverage, uniform_weights, vol_sized_conservative, vol_weighted_sizing, weekly_aligned_windows";

const mrptCsvCols = "z_back, v_back, base_entry_z, base_exit_z, entry_volatility_factor, exit_volatility_factor, amplifier, volatility_stop_loss_multiplier, max_holding_period, cooling_off_period";

const mtfsCsvCols = "momentum_windows, momentum_weights, skip_days, use_vams, use_llt, sma_short, sma_long, require_trend_confirmation, entry_momentum_threshold, exit_momentum_decay_threshold, reversal_sma_lookback, momentum_decay_short/long_window, exit_on_reversal, exit_on_momentum_decay, target_annual_vol, vol_scale_window, max_vol_scale_factor, crash_vol_percentile, crash_scale_factor, amplifier, use_vol_weighted_sizing, hedge_method, hedge_lag, volatility_stop_loss_multiplier, max_holding_period, cooling_off_period, pair_stop_loss_pct, rebalance_frequency, mean_back, std_back, v_back";

const fileSystem: FileNode[] = [
  {
    name: 'walk_forward',
    type: 'folder',
    description: 'MRPT (Mean Reversion Pair Trading) Walk-Forward root directory',
    children: [
      { name: 'walk_forward_summary_20260321.json', type: 'json', description: 'Main config + all window OOS performance summary' },
      { name: 'dsr_selection_log_20260321.csv', type: 'csv', description: 'DSR selection log for each pair x param_set' },
      { name: 'oos_equity_curve_20260321.csv', type: 'csv', description: '6-window chained daily equity curve' },
      { name: 'oos_pair_summary_20260321.csv', type: 'csv', description: 'Per-pair OOS aggregated performance' },
      { name: 'oos_report_20260321.txt', type: 'txt', description: 'Human-readable text report' },
      {
        name: 'window06_2024-02-15_2026-02-12',
        type: 'folder',
        description: 'Rolling window 6 (IS training + OOS test)',
        children: [
          { name: 'selected_pairs.json', type: 'json', description: 'Selected pairs for this window' },
          {
            name: 'historical_runs',
            type: 'folder',
            description: 'OOS test results (real performance using selected optimal strategy)',
            children: [
              { name: 'portfolio_history_wf_test_window06_2026-02-13_2026-03-20_1.xlsx', type: 'excel', description: 'OOS period real performance' }
            ]
          },
          {
            name: 'charts',
            type: 'folder',
            description: 'OOS test charts',
            children: [
              {
                name: 'wf_test_window06_2026-02-13_2026-03-20_1',
                type: 'folder',
                children: [
                  { name: 'individual_stocks.png', type: 'png' },
                  { name: 'pair_trades.png', type: 'png' },
                  { name: 'portfolio_history.png', type: 'png' },
                  { name: 'z_scores_and_pnl.png', type: 'png', description: 'MRPT specific chart' }
                ]
              }
            ]
          },
          {
            name: 'logs',
            type: 'folder',
            description: 'OOS test logs',
            children: [
              { name: 'run_wf_test_window06_1.log', type: 'log' }
            ]
          },
          {
            name: 'wf_window06_2024-02-15_2026-02-12',
            type: 'folder',
            description: 'IS Grid Search results (in-sample training)',
            children: [
              { name: 'grid_config.json', type: 'json', description: 'Grid search config' },
              { name: 'strategy_summary_20260321.csv', type: 'csv', description: 'Grid search summary table', content: { cols: mrptCsvCols, params: mrptParams } },
              {
                name: 'historical_runs',
                type: 'folder',
                description: 'Per param_set backtest Excel (64 files)',
                children: [
                  { name: 'portfolio_history_all15_aggressive_aggressive_1.xlsx', type: 'excel' },
                  { name: 'portfolio_history_all15_balanced_plus_balanced_plus_1.xlsx', type: 'excel' },
                  { name: '... (62 more files)', type: 'txt' }
                ]
              },
              {
                name: 'charts',
                type: 'folder',
                description: 'Per param_set charts (64 subdirectories)',
                children: [
                   { name: 'all15_aggressive_1', type: 'folder', children: [] }
                ]
              },
              {
                name: 'logs',
                type: 'folder',
                description: 'Per param_set run logs (64 files)',
                children: [
                  { name: 'run_all15_aggressive_1.log', type: 'log' }
                ]
              }
            ]
          }
        ]
      }
    ]
  },
  {
    name: 'walk_forward_mtfs',
    type: 'folder',
    description: 'MTFS (Multi-Timeframe Momentum Strategy) Walk-Forward root directory',
    children: [
      { name: 'walk_forward_summary_20260321.json', type: 'json' },
      { name: 'dsr_selection_log_20260321.csv', type: 'csv' },
      { name: 'oos_equity_curve_20260321.csv', type: 'csv' },
      { name: 'oos_pair_summary_20260321.csv', type: 'csv' },
      { name: 'oos_report_20260321.txt', type: 'txt' },
      {
        name: 'window01_2024-01-30_2025-07-29',
        type: 'folder',
        children: [
          { name: 'selected_pairs.json', type: 'json' },
          {
            name: 'historical_runs',
            type: 'folder',
            children: [
              { name: 'portfolio_history_MTFS_wf_test_window01_2025-07-30_2025-09-05_1.xlsx', type: 'excel' }
            ]
          },
          {
            name: 'charts',
            type: 'folder',
            children: [
              {
                name: 'wf_test_window01_2025-07-30_2025-09-05_1',
                type: 'folder',
                children: [
                  { name: 'individual_stocks.png', type: 'png' },
                  { name: 'pair_trades.png', type: 'png' },
                  { name: 'portfolio_history.png', type: 'png' },
                  { name: 'momentum_scores_and_pnl.png', type: 'png', description: 'MTFS specific chart' },
                  { name: 'vams_window_decomposition.png', type: 'png', description: 'MTFS specific chart' }
                ]
              }
            ]
          },
          {
            name: 'logs',
            type: 'folder',
            children: [
              { name: 'run_wf_test_window01_1.log', type: 'log' }
            ]
          },
          {
            name: 'wf_window01_2024-01-30_2025-07-29',
            type: 'folder',
            children: [
              { name: 'grid_config.json', type: 'json' },
              { name: 'mtfs_strategy_summary_20260321.csv', type: 'csv', content: { cols: mtfsCsvCols, params: mtfsParams } },
              {
                name: 'historical_runs',
                type: 'folder',
                children: [
                  { name: 'portfolio_history_MTFS_all15_aggressive_aggressive_1.xlsx', type: 'excel' },
                  { name: 'portfolio_history_MTFS_all15_balanced_balanced_1.xlsx', type: 'excel' },
                  { name: '... (60 more files)', type: 'txt' }
                ]
              }
            ]
          }
        ]
      }
    ]
  }
];

// Build a relative path from root to a node
function getNodePath(node: FileNode, root: FileNode[]): string | null {
  function search(nodes: FileNode[], prefix: string): string | null {
    for (const n of nodes) {
      const p = prefix ? `${prefix}/${n.name}` : n.name;
      if (n === node) return p;
      if (n.children) {
        const found = search(n.children, p);
        if (found) return found;
      }
    }
    return null;
  }
  return search(root, '');
}

function strategyFromPath(fullPath: string): string {
  return fullPath.startsWith('walk_forward_mtfs') ? 'mtfs' : 'mrpt';
}

// Strip the root folder prefix to get relative path within the wf dir
function relPathInWfDir(fullPath: string): string {
  const parts = fullPath.split('/');
  return parts.slice(1).join('/'); // remove "walk_forward" or "walk_forward_mtfs"
}

const getIcon = (type: string) => {
  switch (type) {
    case 'folder': return <Folder className="w-4 h-4 text-blue-400" fill="currentColor" fillOpacity={0.2} />;
    case 'excel': return <FileSpreadsheet className="w-4 h-4 text-green-500" />;
    case 'csv': return <Database className="w-4 h-4 text-emerald-500" />;
    case 'json': return <FileJson className="w-4 h-4 text-yellow-500" />;
    case 'png': return <ImageIcon className="w-4 h-4 text-purple-500" />;
    case 'log': return <FileCode className="w-4 h-4 text-gray-400" />;
    default: return <FileText className="w-4 h-4 text-gray-400" />;
  }
};

// Detect date-like strings: "2026-02-13", "2026-02-13T00:00:00.000Z", etc.
function isDateString(val: any): boolean {
  if (typeof val !== 'string') return false;
  return /^\d{4}-\d{2}-\d{2}/.test(val);
}

function fmtAccounting(val: any): string {
  if (val == null) return '';
  if (isDateString(val)) return String(val).slice(0, 10);
  const num = typeof val === 'number' ? val : parseFloat(String(val));
  if (isNaN(num)) return String(val);
  return num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function isNumericCell(val: any): boolean {
  if (val == null || val === '' || typeof val === 'boolean') return false;
  if (isDateString(val)) return false;
  if (typeof val === 'number') return true;
  return !isNaN(parseFloat(val));
}

// Detect if a column should display as percentage based on sheet name or column header
function isPctColumn(sheetName: string, colHeader: string): boolean {
  const lower = colHeader.toLowerCase();
  const sheetLower = sheetName.toLowerCase();
  return lower.includes('percent') || lower.includes('pct') || lower.includes('%')
    || sheetLower.includes('percent') || sheetLower.includes('pct');
}

function fmtPercent(val: any): string {
  if (val == null) return '';
  const num = typeof val === 'number' ? val : parseFloat(String(val));
  if (isNaN(num)) return String(val);
  return num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + '%';
}

// Detect if a column contains pair values (header contains "pair" and value looks like "TICKER/TICKER")
const PAIR_RE_WFS = /^[A-Z]{1,5}\/[A-Z]{1,5}$/;
function isPairColumn(colHeader: string): boolean {
  return colHeader.toLowerCase().includes('pair');
}
function isPairValue(val: any): boolean {
  if (typeof val !== 'string') return false;
  return val.split(/,\s*/).every(p => PAIR_RE_WFS.test(p.trim()));
}

// Inline xlsx sheet viewer component
function InlineXlsxViewer({ strategy, relPath }: { strategy: string; relPath: string }) {
  const { t } = useTranslation();
  const [sheetsInfo, setSheetsInfo] = useState<any>(null);
  const [activeSheet, setActiveSheet] = useState('');
  const [sheetData, setSheetData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [sheetLoading, setSheetLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    setLoading(true);
    setError('');
    getWFXlsxSheets(strategy, relPath)
      .then(info => {
        setSheetsInfo(info);
        const tabs = (info.sheets || []).map((s: any) => s.name).filter((n: string) => n.toLowerCase() !== 'sheet1');
        if (tabs.length > 0) setActiveSheet(tabs[0]);
      })
      .catch(err => setError(err.message))
      .finally(() => setLoading(false));
  }, [strategy, relPath]);

  useEffect(() => {
    if (!activeSheet) return;
    setSheetLoading(true);
    getWFXlsxSheet(strategy, relPath, activeSheet)
      .then(setSheetData)
      .catch(() => setSheetData(null))
      .finally(() => setSheetLoading(false));
  }, [activeSheet, strategy, relPath]);

  if (loading) return <LoadingState />;
  if (error) return <div className="text-xs text-[var(--error)] p-4">{error}</div>;
  if (!sheetsInfo) return null;

  const tabs = (sheetsInfo.sheets || []).map((s: any) => s.name).filter((n: string) => n.toLowerCase() !== 'sheet1');

  return (
    <div className="space-y-3">
      <div className="text-[11px] text-[var(--text-muted)]">{sheetsInfo.file} — {tabs.length} {t('wfStructure.sheets')}</div>
      <div className="flex flex-wrap gap-1">
        {tabs.map((tab: string) => (
          <button
            key={tab}
            onClick={() => setActiveSheet(tab)}
            className={`px-2 py-0.5 text-[10px] font-medium rounded transition-colors ${activeSheet === tab ? 'bg-[var(--accent-primary)] text-white' : 'bg-[var(--bg-secondary)] text-[var(--text-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)]'}`}
          >
            {tab.replace(/_/g, ' ')}
          </button>
        ))}
      </div>
      <div className="overflow-auto max-h-[400px] border border-[var(--border-subtle)] rounded-lg">
        {sheetLoading ? (
          <div className="p-4 text-xs text-[var(--text-muted)] text-center">{t('wfStructure.loadingSheet')}</div>
        ) : sheetData ? (
          <table className="w-full text-xs text-left">
            <thead className="text-[10px] text-[var(--text-muted)] uppercase bg-[var(--bg-secondary)] sticky top-0 z-10">
              <tr>
                {(sheetData.headers || []).map((h: string, i: number) => (
                  <th key={i} className="px-2 py-1.5 font-medium whitespace-nowrap">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-subtle)]">
              {(sheetData.rows || []).slice(0, 200).map((row: any, ri: number) => (
                <tr key={ri} className="hover:bg-[var(--bg-secondary)] transition-colors">
                  {(sheetData.headers || []).map((h: string, ci: number) => {
                    const val = row[h];
                    const isDate = isDateString(val);
                    const isNum = isNumericCell(val);
                    const isPct = isNum && isPctColumn(activeSheet, h);
                    const isPair = isPairColumn(h) && isPairValue(val);
                    const num = isNum ? (typeof val === 'number' ? val : parseFloat(val)) : 0;
                    return (
                      <td key={ci} className={`px-2 py-1 whitespace-nowrap ${isNum || isDate ? 'font-mono' : ''}`}
                        style={{ color: isNum && num < 0 ? 'var(--error)' : 'var(--text-primary)' }}>
                        {isPair ? (
                          String(val).includes(',')
                            ? <span className="inline-flex flex-wrap gap-1">{String(val).split(/,\s*/).map((p, pi) => <span key={pi}><PairBadge pair={p.trim()} strategy={strategy.toLowerCase()} compact /></span>)}</span>
                            : <PairBadge pair={String(val)} strategy={strategy.toLowerCase()} compact />
                        ) : isDate ? String(val).slice(0, 10) : isPct ? fmtPercent(val) : isNum ? fmtAccounting(val) : String(val ?? '')}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="p-4 text-xs text-[var(--text-muted)] text-center">{t('wfStructure.selectASheet')}</div>
        )}
      </div>
    </div>
  );
}

// Resolves xlsx file path from strategy + window + phase + paramSet, then renders InlineXlsxViewer
function ResolvedInlineXlsxViewer({ strategy, window: win, phase, paramSet }: { strategy: string; window: string; phase: string; paramSet: string }) {
  const [resolvedPath, setResolvedPath] = useState<string | null>(null);
  const [resolving, setResolving] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    setResolving(true);
    setError('');
    setResolvedPath(null);
    const strat = strategy.toLowerCase();
    getWFXlsxList(strat)
      .then((files: string[]) => {
        const windowNum = win.replace('Window ', 'window').replace(' ', '');
        let matches: string[];

        if (phase === 'OOS') {
          // OOS: windowXX/.../portfolio_history_*wf_test_windowXX*.xlsx
          matches = files.filter(f => {
            const lower = f.toLowerCase();
            return lower.includes(windowNum) && lower.includes('wf_test') && lower.includes('portfolio_history');
          });
        } else {
          // IS: windowXX/wf_windowXX/.../portfolio_history_*paramSet*.xlsx
          const paramKey = paramSet.toLowerCase().replace(/\s+/g, '_');
          matches = files.filter(f => {
            const lower = f.toLowerCase();
            return lower.includes(windowNum) && !lower.includes('wf_test') && lower.includes('portfolio_history') && lower.includes(paramKey);
          });
        }

        // Pick the latest file (last alphabetically — filenames end with timestamp)
        if (matches.length > 0) {
          matches.sort();
          setResolvedPath(matches[matches.length - 1]);
        } else {
          setError(`No xlsx file found for ${strategy} ${win} ${phase}${phase === 'IS' ? ` (${paramSet})` : ''}`);
        }
      })
      .catch(err => setError(err.message))
      .finally(() => setResolving(false));
  }, [strategy, win, phase, paramSet]);

  if (resolving) return <LoadingState />;
  if (error) return <div className="text-xs text-[var(--error)] p-4">{error}</div>;
  if (!resolvedPath) return null;

  return <InlineXlsxViewer strategy={strategy.toLowerCase()} relPath={resolvedPath} />;
}

function FileTreeNode({ node, level, onSelect, selectedNode }: { node: FileNode, level: number, onSelect: (n: FileNode) => void, selectedNode: FileNode | null, key?: any }) {
  const [isOpen, setIsOpen] = useState(level < 1);
  const isSelected = selectedNode === node;

  return (
    <div>
      <div
        className={`flex items-center py-1 px-2 cursor-pointer hover:bg-[var(--bg-secondary)] rounded-md transition-colors ${isSelected ? 'bg-[var(--bg-secondary)] text-[var(--accent-primary)]' : 'text-[var(--text-primary)]'}`}
        style={{ paddingLeft: `${level * 12 + 8}px` }}
        onClick={() => {
          if (node.type === 'folder') setIsOpen(!isOpen);
          onSelect(node);
        }}
      >
        <div className="w-4 h-4 mr-1 flex items-center justify-center shrink-0">
          {node.type === 'folder' && (
            isOpen ? <ChevronDown className="w-3 h-3 text-[var(--text-muted)]" /> : <ChevronRight className="w-3 h-3 text-[var(--text-muted)]" />
          )}
        </div>
        <div className="mr-2 shrink-0">{getIcon(node.type)}</div>
        <span className="text-xs truncate select-none">{node.name}</span>
      </div>
      {node.type === 'folder' && isOpen && node.children && (
        <div>
          {node.children.map((child, i) => (
            <FileTreeNode key={i} node={child} level={level + 1} onSelect={onSelect} selectedNode={selectedNode} />
          ))}
        </div>
      )}
    </div>
  );
}

const MRPT_PARAM_SETS = mrptParams.split(', ');
const MTFS_PARAM_SETS = mtfsParams.split(', ');

export default function WFStructureViewer({ data }: { data?: any }) {
  const { t } = useTranslation();
  const [viewMode, setViewMode] = useState<'inspector' | 'explorer'>('inspector');
  const [selectedNode, setSelectedNode] = useState<FileNode | null>(fileSystem[0]);

  // Inspector State
  const [strategy, setStrategy] = useState('MRPT');
  const [windowIdx, setWindowIdx] = useState(6);
  const [phase, setPhase] = useState('OOS');
  const [paramSet, setParamSet] = useState('');
  const [showInlineViewer, setShowInlineViewer] = useState(false);

  // Fetch WF summary for the selected strategy
  const { data: wfSummary, loading: wfLoading, error: wfError } = useApi(
    () => getWFSummary(strategy.toLowerCase()),
    [strategy]
  );

  // Derive window info from summary
  const windows = useMemo(() => wfSummary?.windows || [], [wfSummary]);
  const selectedWindow = useMemo(() => windows.find((w: any) => w.window_idx === windowIdx), [windows, windowIdx]);
  const selectedPairs: [string, string, string][] = useMemo(() => selectedWindow?.selected_pairs || [], [selectedWindow]);
  const paramSets = strategy === 'MRPT' ? MRPT_PARAM_SETS : MTFS_PARAM_SETS;

  // Auto-select first param set when switching to IS
  useEffect(() => {
    if (phase === 'IS' && !paramSet) {
      setParamSet(paramSets[0] || 'default');
    }
  }, [phase, paramSets]);

  // Reset inline viewer when config changes
  const handlePhaseChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    setPhase(e.target.value);
    if (e.target.value === 'IS' && !paramSet) {
      setParamSet(paramSets[0] || 'default');
    }
    setShowInlineViewer(false);
  };

  // Determine if selected node is an xlsx file and compute its relative path
  const selectedNodePath = selectedNode ? getNodePath(selectedNode, fileSystem) : null;
  const isXlsx = selectedNode?.type === 'excel' && selectedNodePath && !selectedNode.name.includes('...');
  const xlsxStrategy = selectedNodePath ? strategyFromPath(selectedNodePath) : 'mrpt';
  const xlsxRelPath = selectedNodePath ? relPathInWfDir(selectedNodePath) : '';

  return (
    <div className="flex flex-col">
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)] flex items-center gap-2">
          <LayoutTemplate className="w-4 h-4 text-[var(--accent-primary)]" />
          {t('wfStructure.title')}
        </div>
        <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
          <button onClick={() => setViewMode('inspector')} className={`px-3 py-1.5 flex items-center gap-2 text-xs rounded-sm transition-colors ${viewMode === 'inspector' ? 'bg-[var(--bg-secondary)] text-[var(--text-primary)]' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
            <Activity className="w-3.5 h-3.5" /> {t('wfStructure.runInspector')}
          </button>
          <button onClick={() => setViewMode('explorer')} className={`px-3 py-1.5 flex items-center gap-2 text-xs rounded-sm transition-colors ${viewMode === 'explorer' ? 'bg-[var(--bg-secondary)] text-[var(--text-primary)]' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
            <Folder className="w-3.5 h-3.5" /> {t('wfStructure.fileExplorer')}
          </button>
        </div>
      </div>

      {viewMode === 'explorer' ? (
        <div className="flex-1 flex gap-4 min-h-0">
          {/* Left Pane: File Tree */}
          <div className="w-1/2 flex flex-col bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg overflow-hidden">
            <div className="p-2 border-b border-[var(--border-subtle)] bg-[var(--bg-secondary)] text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider">
              {t('wfStructure.explorer')}
            </div>
            <div className="flex-1 overflow-y-auto p-2">
              {fileSystem.map((node, i) => (
                <FileTreeNode key={i} node={node} level={0} onSelect={setSelectedNode} selectedNode={selectedNode} />
              ))}
            </div>
          </div>

          {/* Right Pane: Details */}
          <div className="w-1/2 flex flex-col bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg overflow-hidden">
            <div className="p-2 border-b border-[var(--border-subtle)] bg-[var(--bg-secondary)] text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider">
              {t('wfStructure.details')}
            </div>
            <div className="flex-1 overflow-y-auto p-4">
              {selectedNode ? (
                <div className="space-y-4">
                  <div className="flex items-start gap-3 border-b border-[var(--border-subtle)] pb-4">
                    <div className="p-2 bg-[var(--bg-secondary)] rounded-lg shrink-0">
                      {getIcon(selectedNode.type)}
                    </div>
                    <div className="min-w-0">
                      <h3 className="text-sm font-medium text-[var(--text-primary)] break-all">{selectedNode.name}</h3>
                      <div className="text-xs text-[var(--text-muted)] mt-1 uppercase tracking-wider">{selectedNode.type} File</div>
                    </div>
                  </div>

                  {selectedNode.description && (
                    <div className="bg-[var(--accent-primary)]/5 border border-[var(--accent-primary)]/20 rounded-lg p-3 text-sm text-[var(--text-primary)]">
                      <div className="flex items-center gap-1.5 text-[var(--accent-primary)] font-medium mb-1 text-xs uppercase tracking-wider">
                        <Info className="w-3.5 h-3.5" /> {t('wfStructure.description')}
                      </div>
                      {selectedNode.description}
                    </div>
                  )}

                  {/* Excel inline viewer */}
                  {isXlsx && (
                    <InlineXlsxViewer strategy={xlsxStrategy} relPath={xlsxRelPath} />
                  )}

                  {/* Static excel sheet list for reference */}
                  {selectedNode.type === 'excel' && !isXlsx && (
                    <div className="space-y-2">
                      <h4 className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider">{t('wfStructure.excelSheets')}</h4>
                      <div className="bg-[var(--bg-secondary)] border border-[var(--border-subtle)] rounded-lg overflow-hidden max-h-[300px] overflow-y-auto">
                        <table className="w-full text-left text-xs">
                          <thead className="bg-[var(--bg-tertiary)] text-[var(--text-muted)] sticky top-0">
                            <tr>
                              <th className="px-3 py-2 font-medium">#</th>
                              <th className="px-3 py-2 font-medium">Sheet Name</th>
                              <th className="px-3 py-2 font-medium">Content</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-[var(--border-subtle)]">
                            {excelSheets.map((sheet, idx) => (
                              <tr key={idx} className="hover:bg-[var(--bg-primary)]">
                                <td className="px-3 py-1.5 text-[var(--text-muted)]">{idx + 1}</td>
                                <td className="px-3 py-1.5 font-mono text-[var(--accent-primary)]">{sheet.name}</td>
                                <td className="px-3 py-1.5 text-[var(--text-secondary)]">{sheet.desc}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}

                  {selectedNode.type === 'csv' && selectedNode.content && (
                    <div className="space-y-4">
                      <div className="space-y-2">
                        <h4 className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider">{t('wfStructure.columns')}</h4>
                        <div className="bg-[var(--bg-secondary)] border border-[var(--border-subtle)] rounded-lg p-3 font-mono text-xs text-[var(--text-primary)] leading-relaxed break-words">
                          {selectedNode.content.cols.split(', ').map((col: string, i: number) => (
                            <span key={i} className="inline-block bg-[var(--bg-tertiary)] px-1.5 py-0.5 rounded mr-1.5 mb-1.5 border border-[var(--border-subtle)]">{col}</span>
                          ))}
                        </div>
                      </div>
                      <div className="space-y-2">
                        <h4 className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider">{t('wfStructure.parameterSets')}</h4>
                        <div className="bg-[var(--bg-secondary)] border border-[var(--border-subtle)] rounded-lg p-3 font-mono text-xs text-[var(--text-secondary)] leading-relaxed break-words">
                          {selectedNode.content.params.split(', ').map((param: string, i: number) => (
                            <span key={i} className="inline-block bg-[var(--bg-tertiary)] px-1.5 py-0.5 rounded mr-1.5 mb-1.5 border border-[var(--border-subtle)]">{param}</span>
                          ))}
                        </div>
                      </div>
                    </div>
                  )}

                  {selectedNode.type === 'folder' && selectedNode.name === 'historical_runs' && (
                    <div className="bg-[var(--warning)]/10 border border-[var(--warning)]/20 rounded-lg p-3 text-sm text-[var(--text-primary)]">
                      <div className="font-medium text-[var(--warning)] mb-1 text-xs uppercase tracking-wider">{t('wfStructure.keyDifference')}</div>
                      <ul className="list-disc pl-4 space-y-1 text-xs text-[var(--text-secondary)]">
                        <li>{t('wfStructure.rootHistoricalRuns')}</li>
                        <li>{t('wfStructure.windowHistoricalRuns')}</li>
                      </ul>
                    </div>
                  )}

                </div>
              ) : (
                <div className="flex flex-col items-center justify-center h-full text-[var(--text-muted)]">
                  <File className="w-8 h-8 mb-2 opacity-20" />
                  <span className="text-sm">{t('wfStructure.selectFile')}</span>
                </div>
              )}
            </div>
          </div>
        </div>
      ) : (
        <div className="flex flex-col pb-4 space-y-6">
          {wfLoading ? <LoadingState /> : wfError ? <ErrorState message={wfError} /> : (<>
          {/* Inspector Controls */}
          <div className="bg-[var(--bg-secondary)] border border-[var(--border-subtle)] rounded-xl p-4">
            <h3 className="text-sm font-medium text-[var(--text-primary)] mb-4 flex items-center gap-2">
              <Settings className="w-4 h-4 text-[var(--accent-primary)]" />
              {t('wfStructure.runConfig')}
            </h3>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider">{t('wfStructure.strategyLabel')}</label>
                <select value={strategy} onChange={(e) => { setStrategy(e.target.value); setShowInlineViewer(false); }} className="w-full bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)]">
                  <option value="MRPT">{t('wfStructure.mrptFull')}</option>
                  <option value="MTFS">{t('wfStructure.mtfsFull')}</option>
                </select>
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider">{t('wfStructure.windowLabel')}</label>
                <select value={windowIdx} onChange={(e) => { setWindowIdx(Number(e.target.value)); setShowInlineViewer(false); }} className="w-full bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)]">
                  {windows.map((w: any) => (
                    <option key={w.window_idx} value={w.window_idx}>
                      W{String(w.window_idx).padStart(2, '0')} ({w.train_start} ~ {w.test_end})
                    </option>
                  ))}
                </select>
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider">{t('wfStructure.phaseLabel')}</label>
                <select value={phase} onChange={handlePhaseChange} className="w-full bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)]">
                  <option value="OOS">{t('wfStructure.oosPhase')}</option>
                  <option value="IS">{t('wfStructure.isPhase')}</option>
                </select>
              </div>
              <div className="space-y-1.5">
                <label className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider">
                  {phase === 'IS' ? t('wfStructure.paramSetLabel') : t('wfStructure.paramSelection')}
                </label>
                <select
                  value={phase === 'OOS' ? '__optimal__' : paramSet}
                  onChange={(e) => { setParamSet(e.target.value); setShowInlineViewer(false); }}
                  disabled={phase === 'OOS'}
                  className="w-full bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)] disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {phase === 'OOS' ? (
                    <option value="__optimal__">{t('wfStructure.perPairOptimal')}</option>
                  ) : (
                    paramSets.map(ps => (
                      <option key={ps} value={ps}>{ps}</option>
                    ))
                  )}
                </select>
              </div>
            </div>
          </div>

          {/* Window Info + Selected Pairs */}
          {selectedWindow && (
            <div className="grid grid-cols-2 gap-4">
              {/* Date Range */}
              <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-4">
                <h4 className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider mb-3 flex items-center gap-2">
                  <Calendar className="w-4 h-4" /> {t('wfStructure.dateRange', { n: String(windowIdx).padStart(2, '0') })}
                </h4>
                <div className="space-y-2">
                  <div className="flex justify-between text-sm">
                    <span className="text-[var(--text-muted)]">{t('wfStructure.isTrain')}</span>
                    <span className="font-mono text-[var(--text-primary)]">{selectedWindow.train_start} ~ {selectedWindow.train_end}</span>
                  </div>
                  <div className="flex justify-between text-sm">
                    <span className="text-[var(--text-muted)]">{t('wfStructure.oosTest')}</span>
                    <span className="font-mono text-[var(--text-primary)]">{selectedWindow.test_start} ~ {selectedWindow.test_end}</span>
                  </div>
                  {selectedWindow.oos_sharpe != null && (
                    <div className="flex justify-between text-sm border-t border-[var(--border-subtle)] pt-2 mt-2">
                      <span className="text-[var(--text-muted)]">{t('wfSummary.oosSharpe')}</span>
                      <span className="font-mono text-[var(--text-primary)]" style={{ color: selectedWindow.oos_sharpe >= 0 ? 'var(--success)' : 'var(--error)' }}>
                        {selectedWindow.oos_sharpe.toFixed(2)}
                      </span>
                    </div>
                  )}
                  {selectedWindow.oos_pnl != null && (
                    <div className="flex justify-between text-sm">
                      <span className="text-[var(--text-muted)]">{t('wfSummary.oosTotalPnl')}</span>
                      <span className="font-mono text-[var(--text-primary)]" style={{ color: selectedWindow.oos_pnl >= 0 ? 'var(--success)' : 'var(--error)' }}>
                        ${selectedWindow.oos_pnl.toLocaleString('en-US', { minimumFractionDigits: 0 })}
                      </span>
                    </div>
                  )}
                </div>
              </div>

              {/* Selected Pairs Table */}
              <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden">
                <div className="px-4 py-2 border-b border-[var(--border-subtle)] bg-[var(--bg-secondary)]">
                  <h4 className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider flex items-center gap-2">
                    <Database className="w-4 h-4" /> {t('wfStructure.selectedPairsCount', { count: selectedPairs.length })}
                    <span className="text-[10px] font-normal normal-case text-[var(--text-muted)]">{t('wfStructure.optimalParamPerPair')}</span>
                  </h4>
                </div>
                <div className="max-h-[200px] overflow-y-auto">
                  <table className="w-full text-xs text-left">
                    <thead className="text-[10px] text-[var(--text-muted)] uppercase bg-[var(--bg-tertiary)] sticky top-0 z-10">
                      <tr>
                        <th className="px-3 py-1.5 font-medium">#</th>
                        <th className="px-3 py-1.5 font-medium">Pair</th>
                        <th className="px-3 py-1.5 font-medium">{t('wfStructure.optimalParamSetCol')}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[var(--border-subtle)]">
                      {selectedPairs.map((pair, i) => (
                        <tr key={i} className="hover:bg-[var(--bg-secondary)] transition-colors">
                          <td className="px-3 py-1.5 text-[var(--text-muted)]">{i + 1}</td>
                          <td className="px-3 py-1.5"><PairBadge s1={pair[0]} s2={pair[1]} strategy={strategy.toLowerCase()} compact /></td>
                          <td className="px-3 py-1.5 font-mono text-[var(--accent-primary)]">{pair[2]}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}

          {/* Context Card */}
          <div className="bg-[var(--accent-primary)]/5 border border-[var(--accent-primary)]/20 rounded-xl p-4">
            <div className="flex items-start gap-3">
              <Info className="w-5 h-5 text-[var(--accent-primary)] shrink-0 mt-0.5" />
              <div>
                <h4 className="text-sm font-medium text-[var(--text-primary)] mb-1">
                  {phase === 'IS' ? t('wfStructure.isGridTitle') : t('wfStructure.oosTestTitle')}
                </h4>
                <p className="text-xs text-[var(--text-secondary)] leading-relaxed">
                  {phase === 'IS'
                    ? t('wfStructure.isGridInfo', { paramCount: paramSets.length, pairCount: selectedPairs.length, total: paramSets.length * selectedPairs.length, paramSet })
                    : t('wfStructure.oosInfo', { pairCount: selectedPairs.length })}
                </p>
              </div>
            </div>
          </div>

          {/* Run Pipeline Visualization */}
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-4">
            <h4 className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider mb-6 flex items-center gap-2">
              <Activity className="w-4 h-4" /> {t('wfStructure.pipelineTitle')}
            </h4>

            {phase === 'IS' ? (
              <div className="flex items-center justify-between text-xs">
                <div className="flex flex-col items-center gap-2 w-1/5">
                  <div className="w-10 h-10 flex items-center justify-center relative" style={{ background: '#f5a623', border: '2px solid #111', boxShadow: '2px 2px 0 0 #111' }}>
                    <FileJson className="w-5 h-5" style={{ color: '#fff' }} />
                  </div>
                  <span className="text-center text-[var(--text-primary)] font-medium">{t('wfStructure.gridConfig')}</span>
                  <span className="text-center text-[var(--text-muted)] text-[10px]">{t('wfStructure.paramSetsCount', { count: paramSets.length })}</span>
                </div>
                <div className="flex-1 h-0.5 bg-[#111] relative mx-1">
                  <ChevronRight className="w-4 h-4 text-[#111] absolute right-0 top-1/2 -translate-y-1/2 translate-x-1/2 bg-[var(--bg-primary)]" />
                </div>
                <div className="flex flex-col items-center gap-2 w-1/5">
                  <div className="w-10 h-10 flex items-center justify-center relative" style={{ background: '#111', border: '2px solid #111', boxShadow: '2px 2px 0 0 #111' }}>
                    <Play className="w-5 h-5" style={{ color: '#fff' }} />
                    <span className="absolute -top-2 -right-2 flex items-center justify-center text-[9px] font-bold px-1.5 py-0.5" style={{ background: '#111', color: '#fff', border: '1px solid #111' }}>x{paramSets.length}</span>
                  </div>
                  <span className="text-center text-[var(--text-primary)] font-medium">{t('wfStructure.parallelRuns')}</span>
                  <span className="text-center text-[var(--text-muted)] text-[10px]">{t('wfStructure.pairsEach', { count: selectedPairs.length })}</span>
                </div>
                <div className="flex-1 h-0.5 bg-[#111] relative mx-1">
                  <ChevronRight className="w-4 h-4 text-[#111] absolute right-0 top-1/2 -translate-y-1/2 translate-x-1/2 bg-[var(--bg-primary)]" />
                </div>
                <div className="flex flex-col items-center gap-2 w-1/5">
                  <div className="w-10 h-10 flex items-center justify-center relative" style={{ background: '#00cc66', border: '2px solid #111', boxShadow: '2px 2px 0 0 #111' }}>
                    <FileSpreadsheet className="w-5 h-5" style={{ color: '#fff' }} />
                    <span className="absolute -top-2 -right-2 flex items-center justify-center text-[9px] font-bold px-1.5 py-0.5" style={{ background: '#00cc66', color: '#fff', border: '1px solid #111' }}>x{paramSets.length}</span>
                  </div>
                  <span className="text-center text-[var(--text-primary)] font-medium">{t('wfStructure.portfolioHistory')}</span>
                  <span className="text-center text-[var(--text-muted)] text-[10px]">{t('wfStructure.oneXlsxPerParam')}</span>
                </div>
                <div className="flex-1 h-0.5 bg-[#111] relative mx-1">
                  <ChevronRight className="w-4 h-4 text-[#111] absolute right-0 top-1/2 -translate-y-1/2 translate-x-1/2 bg-[var(--bg-primary)]" />
                </div>
                <div className="flex flex-col items-center gap-2 w-1/5">
                  <div className="w-10 h-10 flex items-center justify-center" style={{ background: '#00cc66', border: '2px solid #111', boxShadow: '2px 2px 0 0 #111' }}>
                    <Database className="w-5 h-5" style={{ color: '#fff' }} />
                  </div>
                  <span className="text-center text-[var(--text-primary)] font-medium">{t('wfStructure.dsrSelection')}</span>
                  <span className="text-center text-[var(--text-muted)] text-[10px]">{t('wfStructure.bestParamPerPair')}</span>
                </div>
              </div>
            ) : (
              <div className="flex items-center justify-between text-xs">
                <div className="flex flex-col items-center gap-2 w-1/4">
                  <div className="w-10 h-10 flex items-center justify-center" style={{ background: '#f5a623', border: '2px solid #111', boxShadow: '2px 2px 0 0 #111' }}>
                    <FileJson className="w-5 h-5" style={{ color: '#fff' }} />
                  </div>
                  <span className="text-center text-[var(--text-primary)] font-medium">{t('wfStructure.selectedPairsLabel')}</span>
                  <span className="text-center text-[var(--text-muted)] text-[10px]">{t('wfStructure.pairsPerPairParams', { count: selectedPairs.length })}</span>
                </div>
                <div className="flex-1 h-0.5 bg-[#111] relative mx-1">
                  <ChevronRight className="w-4 h-4 text-[#111] absolute right-0 top-1/2 -translate-y-1/2 translate-x-1/2 bg-[var(--bg-primary)]" />
                </div>
                <div className="flex flex-col items-center gap-2 w-1/4">
                  <div className="w-10 h-10 flex items-center justify-center" style={{ background: '#111', border: '2px solid #111', boxShadow: '2px 2px 0 0 #111' }}>
                    <Play className="w-5 h-5" style={{ color: '#fff' }} />
                  </div>
                  <span className="text-center text-[var(--text-primary)] font-medium">{t('wfStructure.oosRun')}</span>
                  <span className="text-center text-[var(--text-muted)] text-[10px]">{t('wfStructure.optimalParamsPerPair')}</span>
                </div>
                <div className="flex-1 h-0.5 bg-[#111] relative mx-1">
                  <ChevronRight className="w-4 h-4 text-[#111] absolute right-0 top-1/2 -translate-y-1/2 translate-x-1/2 bg-[var(--bg-primary)]" />
                </div>
                <div className="flex flex-col items-center gap-2 w-1/4">
                  <div className="w-10 h-10 flex items-center justify-center" style={{ background: '#00cc66', border: '2px solid #111', boxShadow: '2px 2px 0 0 #111' }}>
                    <FileSpreadsheet className="w-5 h-5" style={{ color: '#fff' }} />
                  </div>
                  <span className="text-center text-[var(--text-primary)] font-medium">{t('wfStructure.oosPortfolioHistory')}</span>
                  <span className="text-center text-[var(--text-muted)] text-[10px]">{t('wfStructure.oneXlsxReal')}</span>
                </div>
              </div>
            )}
          </div>

          {/* Portfolio History Deep Dive */}
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden">
            <div className="p-4 border-b border-[var(--border-subtle)] bg-[var(--bg-secondary)] flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="p-2 bg-[var(--success)]/10 rounded-lg">
                  <FileSpreadsheet className="w-5 h-5 text-[var(--success)]" />
                </div>
                <div>
                  <h4 className="text-sm font-medium text-[var(--text-primary)]">{t('wfStructure.deepDive')}</h4>
                  <p className="text-xs text-[var(--text-muted)] mt-0.5">
                    {phase === 'IS'
                      ? `all15_${paramSet} (${selectedPairs.length} pairs x 1 param set)`
                      : `wf_test_window${String(windowIdx).padStart(2, '0')} (${selectedPairs.length} pairs, optimal params)`}
                  </p>
                </div>
              </div>
              <button
                onClick={() => setShowInlineViewer(!showInlineViewer)}
                className={`text-xs flex items-center gap-1.5 px-3 py-1.5 rounded-md transition-colors shadow-sm shrink-0 ${showInlineViewer ? 'bg-[var(--bg-tertiary)] text-[var(--text-primary)] border border-[var(--border-subtle)]' : 'bg-[var(--accent-primary)] text-white hover:bg-[var(--accent-primary)]/90'}`}
              >
                <Play className="w-3.5 h-3.5" /> {showInlineViewer ? 'Close' : 'Open'}
              </button>
            </div>

            {showInlineViewer ? (
              <div className="p-4">
                <ResolvedInlineXlsxViewer
                  strategy={strategy}
                  window={`Window ${String(windowIdx).padStart(2, '0')}`}
                  phase={phase}
                  paramSet={paramSet}
                />
              </div>
            ) : (
              <div className="p-0 overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead className="bg-[var(--bg-tertiary)] text-[var(--text-muted)]">
                    <tr>
                      <th className="px-4 py-2 font-medium">Key Sheet Name</th>
                      <th className="px-4 py-2 font-medium">Description</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-subtle)]">
                    {[
                      { name: 'equity_history', desc: 'Net Asset Value curve' },
                      { name: 'daily_pnl_history', desc: 'Daily Profit and Loss' },
                      { name: 'pair_trade_history', desc: 'Record of all pair trades' },
                      { name: 'percentage_history_by_pair', desc: 'Position sizing per pair' },
                      { name: 'recorded_vars', desc: 'Strategy recorded variables' },
                    ].map((s, i) => (
                      <tr key={i} className="hover:bg-[var(--bg-secondary)] transition-colors">
                        <td className="px-4 py-2 font-mono text-[var(--accent-primary)]">{s.name}</td>
                        <td className="px-4 py-2 text-[var(--text-secondary)]">{s.desc}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Generated Charts */}
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-4">
            <h4 className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider mb-3 flex items-center gap-2">
              <BarChart2 className="w-4 h-4" /> {t('wfStructure.generatedCharts')}
            </h4>
            <div className="flex flex-wrap gap-2">
              {['individual_stocks.png', 'pair_trades.png', 'portfolio_history.png'].map(name => (
                <span key={name} className="inline-flex items-center gap-1.5 px-2 py-1 rounded bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-xs text-[var(--text-primary)]">
                  <ImageIcon className="w-3 h-3 text-[var(--accent-primary)]" /> {name}
                </span>
              ))}
              {strategy === 'MRPT' ? (
                <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-xs text-[var(--text-primary)]">
                  <ImageIcon className="w-3 h-3 text-[var(--accent-primary)]" /> z_scores_and_pnl.png
                </span>
              ) : (
                <>
                  <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-xs text-[var(--text-primary)]">
                    <ImageIcon className="w-3 h-3 text-[var(--accent-primary)]" /> momentum_scores_and_pnl.png
                  </span>
                  <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-xs text-[var(--text-primary)]">
                    <ImageIcon className="w-3 h-3 text-[var(--accent-primary)]" /> vams_window_decomposition.png
                  </span>
                </>
              )}
            </div>
          </div>

          </>)}
        </div>
      )}
    </div>
  );
}
