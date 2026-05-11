import React, { useState, useMemo } from 'react';

interface Sensitivity1DRow {
  param_value: number | string;
  [metric: string]: number | string | undefined;
}

interface Sensitivity2DCell {
  param1_value: number | string;
  param2_value: number | string;
  value: number;
  [key: string]: number | string | undefined;
}

interface SensitivityData {
  model_name?: string;
  mode?: '1d' | '2d';
  param1_name?: string;
  param2_name?: string;
  output_metric?: string;
  results_1d?: Sensitivity1DRow[];
  results_2d?: Sensitivity2DCell[];
  param1_values?: (number | string)[];
  param2_values?: (number | string)[];
  error?: string;
}

function getHeatColor(value: number, min: number, max: number): string {
  if (max === min) return 'rgba(59, 130, 246, 0.3)';
  const ratio = Math.max(0, Math.min(1, (value - min) / (max - min)));
  // Green (good) -> Yellow (mid) -> Red (bad)
  if (ratio >= 0.5) {
    const t = (ratio - 0.5) * 2;
    const r = Math.round(34 * (1 - t));
    const g = Math.round(197 * t + 150 * (1 - t));
    const b = Math.round(94 * t);
    return `rgba(${r}, ${g}, ${b}, 0.25)`;
  } else {
    const t = ratio * 2;
    const r = Math.round(239 * (1 - t) + 200 * t);
    const g = Math.round(68 * (1 - t) + 150 * t);
    const b = Math.round(68 * (1 - t));
    return `rgba(${r}, ${g}, ${b}, 0.25)`;
  }
}

function fmtVal(v: number | string | undefined): string {
  if (v == null) return '—';
  if (typeof v === 'string') return v;
  if (Math.abs(v) < 1 && v !== 0) return (v * 100).toFixed(2) + '%';
  return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

export default function SensitivityHeatmap({ data }: { data?: SensitivityData; params?: any }) {
  const [selectedMetric, setSelectedMetric] = useState<string | null>(null);

  // Detect mode and available metrics
  const mode = data?.mode || (data?.results_2d && data.results_2d.length > 0 ? '2d' : '1d');
  const results1d = data?.results_1d || [];
  const results2d = data?.results_2d || [];

  // Available metric columns for 1D
  const metricColumns = useMemo(() => {
    if (results1d.length === 0) return [];
    return Object.keys(results1d[0]).filter(k => k !== 'param_value');
  }, [results1d]);

  const activeMetric = selectedMetric || data?.output_metric || (metricColumns.length > 0 ? metricColumns[0] : null);

  if (!data) {
    return <div className="text-sm text-[var(--text-muted)] text-center py-8">No sensitivity data available.</div>;
  }

  if (data.error) {
    return (
      <div className="bg-[var(--error)]/10 border border-[var(--error)]/20 rounded-lg p-4">
        <div className="text-sm font-medium text-[var(--error)]">Error</div>
        <div className="text-xs text-[var(--text-secondary)] mt-1">{data.error}</div>
      </div>
    );
  }

  // ══ 2D Heatmap Mode ══
  if (mode === '2d' && results2d.length > 0) {
    const p1Vals = data.param1_values || [...new Set(results2d.map(c => c.param1_value))];
    const p2Vals = data.param2_values || [...new Set(results2d.map(c => c.param2_value))];
    const allVals = results2d.map(c => c.value);
    const minVal = Math.min(...allVals);
    const maxVal = Math.max(...allVals);

    const valueMap = new Map<string, number>();
    results2d.forEach(c => {
      valueMap.set(`${c.param1_value}|${c.param2_value}`, c.value);
    });

    return (
      <div className="flex flex-col h-full space-y-4">
        {/* Header */}
        <div className="shrink-0">
          <div className="text-sm font-medium text-[var(--text-primary)]">
            {data.model_name ? `${data.model_name} - ` : ''}Sensitivity Analysis (2D)
          </div>
          <div className="text-[10px] text-[var(--text-muted)] mt-1">
            {data.param1_name || 'Param 1'} vs {data.param2_name || 'Param 2'}
            {data.output_metric && <span> — Metric: <span className="font-mono text-[var(--accent-primary)]">{data.output_metric}</span></span>}
          </div>
        </div>

        {/* Color legend */}
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-[10px] text-[var(--text-muted)]">Low</span>
          <div className="h-2 flex-1 rounded" style={{
            background: 'linear-gradient(to right, rgba(239,68,68,0.4), rgba(200,150,0,0.4), rgba(34,197,94,0.4))'
          }} />
          <span className="text-[10px] text-[var(--text-muted)]">High</span>
          <span className="text-[10px] font-mono text-[var(--text-muted)]">[{fmtVal(minVal)} .. {fmtVal(maxVal)}]</span>
        </div>

        {/* Heatmap Table */}
        <div className="flex-1 overflow-auto bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl">
          <table className="w-full text-sm">
            <thead className="sticky top-0 z-10">
              <tr className="bg-[var(--bg-tertiary)]">
                <th className="px-3 py-2 text-[10px] text-[var(--text-muted)] uppercase tracking-wider font-medium text-left border-b border-r border-[var(--border-subtle)]">
                  {data.param1_name || 'P1'} \ {data.param2_name || 'P2'}
                </th>
                {p2Vals.map((p2, i) => (
                  <th key={i} className="px-3 py-2 text-[10px] text-[var(--text-muted)] uppercase font-mono font-medium text-center border-b border-[var(--border-subtle)]">
                    {fmtVal(p2)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {p1Vals.map((p1, ri) => (
                <tr key={ri} className="hover:bg-[var(--bg-secondary)]/30">
                  <td className="px-3 py-2 text-xs font-mono font-medium text-[var(--text-primary)] border-r border-[var(--border-subtle)] bg-[var(--bg-tertiary)]">
                    {fmtVal(p1)}
                  </td>
                  {p2Vals.map((p2, ci) => {
                    const val = valueMap.get(`${p1}|${p2}`);
                    return (
                      <td
                        key={ci}
                        className="px-3 py-2 text-xs font-mono text-center border-[var(--border-subtle)]"
                        style={{ backgroundColor: val != null ? getHeatColor(val, minVal, maxVal) : 'transparent' }}
                      >
                        {val != null ? fmtVal(val) : '—'}
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

  // ══ 1D Table Mode ══
  if (results1d.length > 0) {
    return (
      <div className="flex flex-col h-full space-y-4">
        {/* Header */}
        <div className="shrink-0">
          <div className="text-sm font-medium text-[var(--text-primary)]">
            {data.model_name ? `${data.model_name} - ` : ''}Sensitivity Analysis (1D)
          </div>
          <div className="text-[10px] text-[var(--text-muted)] mt-1">
            Varying: <span className="font-mono text-[var(--accent-primary)]">{data.param1_name || 'Parameter'}</span>
          </div>
        </div>

        {/* Metric Selector (if multiple) */}
        {metricColumns.length > 1 && (
          <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5 shrink-0 w-fit">
            {metricColumns.map(m => (
              <button
                key={m}
                onClick={() => setSelectedMetric(m)}
                className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${
                  activeMetric === m ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'
                }`}
              >
                {m}
              </button>
            ))}
          </div>
        )}

        {/* 1D Table */}
        <div className="flex-1 overflow-y-auto bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden">
          <table className="w-full text-left text-sm">
            <thead className="bg-[var(--bg-tertiary)] border-b border-[var(--border-subtle)] text-[10px] uppercase tracking-wider text-[var(--text-muted)] sticky top-0 z-10">
              <tr>
                <th className="px-4 py-3 font-medium">{data.param1_name || 'Parameter'}</th>
                {metricColumns.map(col => (
                  <th key={col} className={`px-4 py-3 font-medium text-right ${activeMetric === col ? 'text-[var(--accent-primary)]' : ''}`}>{col}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-subtle)]">
              {results1d.map((row, idx) => {
                const metricVal = activeMetric ? row[activeMetric] : undefined;
                const allMetricVals = activeMetric ? results1d.map(r => {
                  const v = r[activeMetric!];
                  return typeof v === 'number' ? v : parseFloat(String(v ?? ''));
                }).filter(v => !isNaN(v)) : [];
                const minV = allMetricVals.length > 0 ? Math.min(...allMetricVals) : 0;
                const maxV = allMetricVals.length > 0 ? Math.max(...allMetricVals) : 1;

                return (
                  <tr key={idx} className="hover:bg-[var(--bg-secondary)] transition-colors">
                    <td className="px-4 py-2 text-xs font-mono text-[var(--text-primary)] font-medium">{fmtVal(row.param_value)}</td>
                    {metricColumns.map(col => {
                      const v = row[col];
                      const numV = typeof v === 'number' ? v : parseFloat(String(v ?? ''));
                      const bgColor = !isNaN(numV) && col === activeMetric ? getHeatColor(numV, minV, maxV) : 'transparent';
                      return (
                        <td
                          key={col}
                          className="px-4 py-2 text-xs font-mono text-right text-[var(--text-primary)]"
                          style={{ backgroundColor: bgColor }}
                        >
                          {fmtVal(v)}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    );
  }

  return <div className="text-sm text-[var(--text-muted)] text-center py-8">No sensitivity results available.</div>;
}
