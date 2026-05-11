import React, { useMemo } from 'react';

interface RatePoint {
  date: string;
  rate: number;
  source?: 'historical' | 'forward' | string;
}

interface ForwardRateData {
  rate_code?: string;
  rate_name?: string;
  current_rate?: number;
  as_of_date?: string;
  rates?: RatePoint[];
  error?: string;
}

function fmtPct(v: number | undefined | null, decimals: number = 4): string {
  if (v == null || isNaN(v)) return '—';
  return (v * 100).toFixed(decimals) + '%';
}

function SvgLineChart({ rates }: { rates: RatePoint[] }) {
  const width = 600;
  const height = 200;
  const padding = { top: 20, right: 50, bottom: 30, left: 50 };
  const chartW = width - padding.left - padding.right;
  const chartH = height - padding.top - padding.bottom;

  const values = rates.map(r => r.rate);
  const minY = Math.min(...values);
  const maxY = Math.max(...values);
  const rangeY = maxY - minY || 0.001;
  const padY = rangeY * 0.1;

  const scaleX = (i: number) => padding.left + (i / (rates.length - 1)) * chartW;
  const scaleY = (v: number) => padding.top + chartH - ((v - (minY - padY)) / (rangeY + 2 * padY)) * chartH;

  // Build polyline points
  const historicalPts: string[] = [];
  const forwardPts: string[] = [];
  let transitionIdx = -1;

  rates.forEach((r, i) => {
    const x = scaleX(i);
    const y = scaleY(r.rate);
    const pt = `${x.toFixed(1)},${y.toFixed(1)}`;
    if (r.source === 'forward') {
      if (transitionIdx < 0) transitionIdx = i;
      forwardPts.push(pt);
    } else {
      historicalPts.push(pt);
    }
  });

  // Connect last historical to first forward for continuity
  if (transitionIdx > 0 && historicalPts.length > 0) {
    const lastHist = historicalPts[historicalPts.length - 1];
    forwardPts.unshift(lastHist);
  }

  // Y-axis tick values
  const yTicks = 5;
  const yTickVals = Array.from({ length: yTicks }, (_, i) => minY - padY + (rangeY + 2 * padY) * (i / (yTicks - 1)));

  // X-axis labels (show ~6)
  const labelCount = Math.min(6, rates.length);
  const labelIndices = Array.from({ length: labelCount }, (_, i) => Math.round(i * (rates.length - 1) / (labelCount - 1)));

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto" preserveAspectRatio="xMidYMid meet">
      {/* Grid lines */}
      {yTickVals.map((v, i) => (
        <g key={i}>
          <line
            x1={padding.left} y1={scaleY(v)} x2={width - padding.right} y2={scaleY(v)}
            stroke="var(--border-subtle)" strokeWidth="0.5" strokeDasharray="3,3"
          />
          <text x={padding.left - 5} y={scaleY(v) + 3} fontSize="7" fill="var(--text-muted)" textAnchor="end" fontFamily="monospace">
            {fmtPct(v, 2)}
          </text>
        </g>
      ))}

      {/* X-axis labels */}
      {labelIndices.map(idx => (
        <text
          key={idx}
          x={scaleX(idx)} y={height - 5}
          fontSize="7" fill="var(--text-muted)" textAnchor="middle" fontFamily="monospace"
        >
          {rates[idx]?.date || ''}
        </text>
      ))}

      {/* Transition line */}
      {transitionIdx > 0 && (
        <line
          x1={scaleX(transitionIdx)} y1={padding.top}
          x2={scaleX(transitionIdx)} y2={padding.top + chartH}
          stroke="var(--warning)" strokeWidth="1" strokeDasharray="4,2" opacity="0.6"
        />
      )}
      {transitionIdx > 0 && (
        <text
          x={scaleX(transitionIdx) + 3} y={padding.top + 10}
          fontSize="7" fill="var(--warning)" fontFamily="monospace"
        >
          Forward
        </text>
      )}

      {/* Historical line */}
      {historicalPts.length > 1 && (
        <polyline
          points={historicalPts.join(' ')}
          fill="none"
          stroke="var(--accent-primary)"
          strokeWidth="2"
          strokeLinejoin="round"
        />
      )}

      {/* Forward line */}
      {forwardPts.length > 1 && (
        <polyline
          points={forwardPts.join(' ')}
          fill="none"
          stroke="var(--warning)"
          strokeWidth="2"
          strokeLinejoin="round"
          strokeDasharray="6,3"
        />
      )}

      {/* Dots */}
      {rates.map((r, i) => (
        <circle
          key={i}
          cx={scaleX(i)} cy={scaleY(r.rate)} r="2.5"
          fill={r.source === 'forward' ? 'var(--warning)' : 'var(--accent-primary)'}
          stroke="var(--bg-primary)" strokeWidth="1"
        />
      ))}
    </svg>
  );
}

export default function ForwardRateCurve({ data }: { data?: ForwardRateData; params?: any }) {
  if (!data) {
    return <div className="text-sm text-[var(--text-muted)] text-center py-8">No forward rate data available.</div>;
  }

  if (data.error) {
    return (
      <div className="bg-[var(--error)]/10 border border-[var(--error)]/20 rounded-lg p-4">
        <div className="text-sm font-medium text-[var(--error)]">Error</div>
        <div className="text-xs text-[var(--text-secondary)] mt-1">{data.error}</div>
      </div>
    );
  }

  const rates = data.rates || [];
  const historicalCount = useMemo(() => rates.filter(r => r.source !== 'forward').length, [rates]);
  const forwardCount = useMemo(() => rates.filter(r => r.source === 'forward').length, [rates]);

  return (
    <div className="flex flex-col h-full space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between shrink-0">
        <div>
          <div className="text-sm font-medium text-[var(--text-primary)]">
            {data.rate_name || data.rate_code || 'Forward Rate Curve'}
          </div>
          {data.as_of_date && (
            <div className="text-[10px] text-[var(--text-muted)] mt-0.5">As of {data.as_of_date}</div>
          )}
        </div>
        {data.current_rate != null && (
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg px-3 py-2 text-right">
            <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider">Current</div>
            <div className="text-sm font-mono font-bold text-[var(--accent-primary)]">{fmtPct(data.current_rate, 3)}</div>
          </div>
        )}
      </div>

      {/* Legend */}
      <div className="flex items-center gap-4 shrink-0">
        <div className="flex items-center gap-1.5">
          <div className="w-4 h-0.5 bg-[var(--accent-primary)]" />
          <span className="text-[10px] text-[var(--text-muted)]">Historical ({historicalCount})</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-4 h-0.5 bg-[var(--warning)]" style={{ borderTop: '2px dashed var(--warning)', height: 0 }} />
          <span className="text-[10px] text-[var(--text-muted)]">Forward ({forwardCount})</span>
        </div>
      </div>

      {/* Chart */}
      {rates.length > 1 && (
        <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-4 shrink-0">
          <SvgLineChart rates={rates} />
        </div>
      )}

      {/* Rate Table */}
      {rates.length > 0 && (
        <div className="flex-1 overflow-y-auto bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden">
          <table className="w-full text-left text-sm">
            <thead className="bg-[var(--bg-tertiary)] border-b border-[var(--border-subtle)] text-[10px] uppercase tracking-wider text-[var(--text-muted)] sticky top-0 z-10">
              <tr>
                <th className="px-4 py-3 font-medium">Date</th>
                <th className="px-4 py-3 font-medium text-right">Rate (%)</th>
                <th className="px-4 py-3 font-medium">Source</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-subtle)]">
              {rates.map((r, idx) => {
                const isForward = r.source === 'forward';
                const isTransition = idx > 0 && rates[idx - 1]?.source !== 'forward' && isForward;
                return (
                  <tr
                    key={idx}
                    className={`hover:bg-[var(--bg-secondary)] transition-colors ${isTransition ? 'border-t-2 border-t-[var(--warning)]/40' : ''}`}
                  >
                    <td className="px-4 py-2 text-xs font-mono text-[var(--text-primary)]">{r.date}</td>
                    <td className={`px-4 py-2 text-xs font-mono text-right font-medium ${isForward ? 'text-[var(--warning)]' : 'text-[var(--text-primary)]'}`}>
                      {fmtPct(r.rate, 3)}
                    </td>
                    <td className="px-4 py-2">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold tracking-wide uppercase ${
                        isForward
                          ? 'bg-[var(--warning)]/10 text-[var(--warning)]'
                          : 'bg-[var(--accent-primary)]/10 text-[var(--accent-primary)]'
                      }`}>
                        {r.source || 'historical'}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {rates.length === 0 && (
        <div className="text-sm text-[var(--text-muted)] text-center py-8">No rate data available.</div>
      )}
    </div>
  );
}
