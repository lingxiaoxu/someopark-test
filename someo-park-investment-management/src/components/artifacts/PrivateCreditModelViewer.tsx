import React, { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';

interface ModelInput {
  name: string;
  value: number | string;
  description?: string;
}

interface ModelOutput {
  name: string;
  value: number | string;
  unit?: string;
}

interface CashflowRow {
  period: number;
  label?: string;
  [key: string]: number | string | undefined;
}

interface PrivateCreditModelData {
  model_name?: string;
  model_description?: string;
  inputs?: ModelInput[];
  outputs?: ModelOutput[];
  cashflows?: CashflowRow[];
  cashflow_columns?: string[];
  error?: string;
}

function formatValue(val: number | string | undefined, unit?: string): string {
  if (val == null) return '—';
  if (typeof val === 'string') return val;
  if (unit === '%' || (typeof val === 'number' && Math.abs(val) < 1 && Math.abs(val) > 0)) {
    return (val * 100).toFixed(2) + '%';
  }
  if (unit === 'x') return val.toFixed(2) + 'x';
  if (Math.abs(val) >= 1000) return '$' + val.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return val.toFixed(4);
}

function getOutputBadgeColor(name: string, val: number | string): string {
  const n = name.toLowerCase();
  const v = typeof val === 'number' ? val : parseFloat(String(val));
  if (isNaN(v)) return 'bg-[var(--bg-tertiary)] text-[var(--text-primary)]';
  if (n.includes('irr') || n.includes('return') || n.includes('yield')) {
    return v >= 0.1 ? 'bg-[var(--success)]/15 text-[var(--success)]' :
           v >= 0 ? 'bg-[var(--warning)]/15 text-[var(--warning)]' :
           'bg-[var(--error)]/15 text-[var(--error)]';
  }
  if (n.includes('moic') || n.includes('multiple')) {
    return v >= 1.5 ? 'bg-[var(--success)]/15 text-[var(--success)]' :
           v >= 1.0 ? 'bg-[var(--warning)]/15 text-[var(--warning)]' :
           'bg-[var(--error)]/15 text-[var(--error)]';
  }
  return 'bg-[var(--accent-primary)]/15 text-[var(--accent-primary)]';
}

export default function PrivateCreditModelViewer({ data }: { data?: PrivateCreditModelData; params?: any }) {
  const [showInputs, setShowInputs] = useState(true);
  const [showOutputs, setShowOutputs] = useState(true);
  const [showCashflows, setShowCashflows] = useState(true);

  if (!data) {
    return <div className="text-sm text-[var(--text-muted)] text-center py-8">No model data available.</div>;
  }

  if (data.error) {
    return (
      <div className="bg-[var(--error)]/10 border border-[var(--error)]/20 rounded-lg p-4">
        <div className="text-sm font-medium text-[var(--error)]">Error</div>
        <div className="text-xs text-[var(--text-secondary)] mt-1">{data.error}</div>
      </div>
    );
  }

  const inputs = data.inputs || [];
  const outputs = data.outputs || [];
  const cashflows = data.cashflows || [];
  const cfColumns = data.cashflow_columns || (cashflows.length > 0 ? Object.keys(cashflows[0]).filter(k => k !== 'period' && k !== 'label') : []);

  return (
    <div className="flex flex-col h-full space-y-4">
      {/* Header */}
      <div className="shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)]">{data.model_name || 'Private Credit Model'}</div>
        {data.model_description && (
          <div className="text-[10px] text-[var(--text-muted)] mt-1">{data.model_description}</div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto space-y-4">
        {/* Outputs Section */}
        {outputs.length > 0 && (
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden">
            <button
              onClick={() => setShowOutputs(!showOutputs)}
              className="w-full flex items-center gap-2 px-4 py-3 bg-[var(--bg-tertiary)] border-b border-[var(--border-subtle)] hover:bg-[var(--bg-secondary)] transition-colors"
            >
              {showOutputs ? <ChevronDown className="w-3.5 h-3.5 text-[var(--text-muted)]" /> : <ChevronRight className="w-3.5 h-3.5 text-[var(--text-muted)]" />}
              <span className="text-[10px] font-medium uppercase tracking-wider text-[var(--text-muted)]">Output Metrics</span>
              <span className="text-[10px] text-[var(--text-muted)]">({outputs.length})</span>
            </button>
            {showOutputs && (
              <div className="p-4 grid grid-cols-2 gap-3">
                {outputs.map((out, idx) => (
                  <div key={idx} className="flex items-center justify-between bg-[var(--bg-secondary)] rounded-lg p-3">
                    <span className="text-xs text-[var(--text-secondary)]">{out.name}</span>
                    <span className={`px-2.5 py-1 rounded text-xs font-bold font-mono ${getOutputBadgeColor(out.name, out.value)}`}>
                      {formatValue(out.value, out.unit)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Inputs Section */}
        {inputs.length > 0 && (
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden">
            <button
              onClick={() => setShowInputs(!showInputs)}
              className="w-full flex items-center gap-2 px-4 py-3 bg-[var(--bg-tertiary)] border-b border-[var(--border-subtle)] hover:bg-[var(--bg-secondary)] transition-colors"
            >
              {showInputs ? <ChevronDown className="w-3.5 h-3.5 text-[var(--text-muted)]" /> : <ChevronRight className="w-3.5 h-3.5 text-[var(--text-muted)]" />}
              <span className="text-[10px] font-medium uppercase tracking-wider text-[var(--text-muted)]">Input Parameters</span>
              <span className="text-[10px] text-[var(--text-muted)]">({inputs.length})</span>
            </button>
            {showInputs && (
              <table className="w-full text-left text-sm">
                <thead className="bg-[var(--bg-tertiary)] border-b border-[var(--border-subtle)] text-[10px] uppercase tracking-wider text-[var(--text-muted)]">
                  <tr>
                    <th className="px-4 py-2 font-medium">Parameter</th>
                    <th className="px-4 py-2 font-medium text-right">Value</th>
                    <th className="px-4 py-2 font-medium">Description</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-subtle)]">
                  {inputs.map((inp, idx) => (
                    <tr key={idx} className="hover:bg-[var(--bg-secondary)] transition-colors">
                      <td className="px-4 py-2 text-xs text-[var(--text-primary)] font-mono">{inp.name}</td>
                      <td className="px-4 py-2 text-xs text-right font-mono text-[var(--accent-primary)]">{formatValue(inp.value)}</td>
                      <td className="px-4 py-2 text-[10px] text-[var(--text-muted)]">{inp.description || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}

        {/* Cashflow Schedule Section */}
        {cashflows.length > 0 && (
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden">
            <button
              onClick={() => setShowCashflows(!showCashflows)}
              className="w-full flex items-center gap-2 px-4 py-3 bg-[var(--bg-tertiary)] border-b border-[var(--border-subtle)] hover:bg-[var(--bg-secondary)] transition-colors"
            >
              {showCashflows ? <ChevronDown className="w-3.5 h-3.5 text-[var(--text-muted)]" /> : <ChevronRight className="w-3.5 h-3.5 text-[var(--text-muted)]" />}
              <span className="text-[10px] font-medium uppercase tracking-wider text-[var(--text-muted)]">Cash Flow Schedule</span>
              <span className="text-[10px] text-[var(--text-muted)]">({cashflows.length} periods)</span>
            </button>
            {showCashflows && (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="bg-[var(--bg-tertiary)] border-b border-[var(--border-subtle)] text-[10px] uppercase tracking-wider text-[var(--text-muted)] sticky top-0">
                    <tr>
                      <th className="px-3 py-2 font-medium">Period</th>
                      <th className="px-3 py-2 font-medium">Label</th>
                      {cfColumns.map(col => (
                        <th key={col} className="px-3 py-2 font-medium text-right">{col}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-[var(--border-subtle)]">
                    {cashflows.map((row, idx) => (
                      <tr key={idx} className="hover:bg-[var(--bg-secondary)] transition-colors">
                        <td className="px-3 py-2 text-xs font-mono text-[var(--text-primary)]">{row.period}</td>
                        <td className="px-3 py-2 text-xs text-[var(--text-secondary)]">{row.label || '—'}</td>
                        {cfColumns.map(col => {
                          const v = row[col];
                          const num = typeof v === 'number' ? v : parseFloat(String(v ?? ''));
                          const isNeg = !isNaN(num) && num < 0;
                          return (
                            <td key={col} className={`px-3 py-2 text-xs font-mono text-right ${isNeg ? 'text-[var(--error)]' : 'text-[var(--text-primary)]'}`}>
                              {v != null ? (typeof v === 'number' ? v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : String(v)) : '—'}
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
        )}
      </div>
    </div>
  );
}
