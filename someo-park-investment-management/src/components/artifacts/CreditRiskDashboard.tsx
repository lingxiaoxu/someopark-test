import React from 'react';

interface CreditRiskData {
  credit_score?: number;
  default_probability?: number;
  recovery_rate?: number;
  stress_multiplier?: number;
  expected_loss?: number;
  risk_adjusted_spread?: number;
  mode?: string;
  borrower_name?: string;
  rating?: string;
  industry?: string;
  details?: Record<string, number | string>;
  error?: string;
}

function fmtPct(v: number | undefined | null): string {
  if (v == null || isNaN(v)) return '—';
  return (v * 100).toFixed(2) + '%';
}

function fmtBps(v: number | undefined | null): string {
  if (v == null || isNaN(v)) return '—';
  return (v * 10000).toFixed(0) + ' bps';
}

function getScoreColor(score: number): { text: string; bg: string; border: string; label: string } {
  if (score >= 80) return { text: 'text-[var(--success)]', bg: 'bg-[var(--success)]', border: 'border-[var(--success)]', label: 'Low Risk' };
  if (score >= 60) return { text: 'text-[var(--warning)]', bg: 'bg-[var(--warning)]', border: 'border-[var(--warning)]', label: 'Medium Risk' };
  return { text: 'text-[var(--error)]', bg: 'bg-[var(--error)]', border: 'border-[var(--error)]', label: 'High Risk' };
}

function ScoreGauge({ score }: { score: number }) {
  const colors = getScoreColor(score);
  const pct = Math.min(100, Math.max(0, (score / 120) * 100));

  return (
    <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-6 flex flex-col items-center">
      <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-3">Credit Score</div>

      {/* Gauge visualization */}
      <div className="relative w-32 h-16 mb-2">
        {/* Background arc */}
        <svg viewBox="0 0 120 60" className="w-full h-full">
          {/* Track */}
          <path
            d="M 10 55 A 50 50 0 0 1 110 55"
            fill="none"
            stroke="var(--border-subtle)"
            strokeWidth="8"
            strokeLinecap="round"
          />
          {/* Filled arc */}
          <path
            d="M 10 55 A 50 50 0 0 1 110 55"
            fill="none"
            stroke={score >= 80 ? 'var(--success)' : score >= 60 ? 'var(--warning)' : 'var(--error)'}
            strokeWidth="8"
            strokeLinecap="round"
            strokeDasharray={`${pct * 1.57} 157`}
          />
          {/* Score labels */}
          <text x="10" y="58" fontSize="6" fill="var(--text-muted)" textAnchor="middle">0</text>
          <text x="60" y="10" fontSize="6" fill="var(--text-muted)" textAnchor="middle">60</text>
          <text x="110" y="58" fontSize="6" fill="var(--text-muted)" textAnchor="middle">120</text>
        </svg>
      </div>

      {/* Score value */}
      <div className={`text-3xl font-bold font-mono ${colors.text}`}>{score.toFixed(0)}</div>
      <div className={`text-xs font-medium mt-1 px-2 py-0.5 rounded ${colors.bg}/15 ${colors.text}`}>{colors.label}</div>
    </div>
  );
}

function MetricCard({ label, value, sublabel }: { label: string; value: string; sublabel?: string }) {
  return (
    <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
      <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{label}</div>
      <div className="text-sm font-mono font-medium text-[var(--text-primary)]">{value}</div>
      {sublabel && <div className="text-[10px] text-[var(--text-muted)] mt-0.5">{sublabel}</div>}
    </div>
  );
}

export default function CreditRiskDashboard({ data }: { data?: CreditRiskData; params?: any }) {
  if (!data) {
    return <div className="text-sm text-[var(--text-muted)] text-center py-8">No credit risk data available.</div>;
  }

  if (data.error) {
    return (
      <div className="bg-[var(--error)]/10 border border-[var(--error)]/20 rounded-lg p-4">
        <div className="text-sm font-medium text-[var(--error)]">Error</div>
        <div className="text-xs text-[var(--text-secondary)] mt-1">{data.error}</div>
      </div>
    );
  }

  const score = data.credit_score ?? 0;
  const scoreColors = getScoreColor(score);

  return (
    <div className="flex flex-col h-full space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)]">
          Credit Risk Assessment
          {data.borrower_name && <span className="text-[var(--text-secondary)]"> - {data.borrower_name}</span>}
        </div>
        {data.mode && (
          <span className={`px-2.5 py-1 rounded text-[10px] font-bold tracking-wide uppercase ${
            data.mode === 'advanced' ? 'bg-[var(--accent-primary)]/10 text-[var(--accent-primary)]' : 'bg-[var(--text-muted)]/10 text-[var(--text-muted)]'
          }`}>{data.mode}</span>
        )}
      </div>

      {/* Rating & Industry */}
      {(data.rating || data.industry) && (
        <div className="flex items-center gap-3 shrink-0">
          {data.rating && (
            <span className="px-2 py-0.5 rounded text-[10px] font-bold font-mono bg-[var(--bg-tertiary)] text-[var(--text-primary)] border border-[var(--border-subtle)]">
              Rating: {data.rating}
            </span>
          )}
          {data.industry && (
            <span className="text-xs text-[var(--text-secondary)]">{data.industry}</span>
          )}
        </div>
      )}

      <div className="flex-1 overflow-y-auto space-y-4">
        {/* Score Gauge */}
        <ScoreGauge score={score} />

        {/* Key Metrics Grid */}
        <div className="grid grid-cols-3 gap-3">
          <MetricCard label="Default Probability" value={fmtPct(data.default_probability)} />
          <MetricCard label="Recovery Rate" value={fmtPct(data.recovery_rate)} />
          <MetricCard label="Stress Multiplier" value={data.stress_multiplier != null ? data.stress_multiplier.toFixed(2) + 'x' : '—'} />
          <MetricCard label="Expected Loss" value={fmtPct(data.expected_loss)} />
          <MetricCard label="Risk-Adjusted Spread" value={fmtBps(data.risk_adjusted_spread)} />
          <div className={`bg-[var(--bg-primary)] border rounded-lg p-3 ${scoreColors.border}/30`}>
            <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">Overall</div>
            <div className={`text-sm font-bold ${scoreColors.text}`}>{scoreColors.label}</div>
            <div className={`text-[10px] font-mono mt-0.5 ${scoreColors.text}`}>Score: {score.toFixed(0)} / 120</div>
          </div>
        </div>

        {/* Additional Details */}
        {data.details && Object.keys(data.details).length > 0 && (
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden">
            <div className="px-4 py-3 bg-[var(--bg-tertiary)] border-b border-[var(--border-subtle)]">
              <span className="text-[10px] font-medium uppercase tracking-wider text-[var(--text-muted)]">Detailed Breakdown</span>
            </div>
            <div className="divide-y divide-[var(--border-subtle)]">
              {Object.entries(data.details).map(([key, val]) => (
                <div key={key} className="flex items-center justify-between px-4 py-2 hover:bg-[var(--bg-secondary)] transition-colors">
                  <span className="text-xs text-[var(--text-secondary)]">{key.replace(/_/g, ' ')}</span>
                  <span className="text-xs font-mono text-[var(--text-primary)]">
                    {typeof val === 'number' ? (Math.abs(val) < 1 ? fmtPct(val) : val.toLocaleString('en-US', { maximumFractionDigits: 2 })) : String(val)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
