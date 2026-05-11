import React from 'react';

interface CashflowRow {
  date: string;
  outstanding_start?: number;
  interest?: number;
  principal?: number;
  fees?: number;
  total_cf?: number;
  outstanding_end?: number;
  rate?: number;
  [key: string]: number | string | undefined;
}

interface CashflowScheduleData {
  irr?: number;
  moic?: number;
  principal?: number;
  rate?: number;
  term?: number;
  frequency?: string;
  cashflows?: CashflowRow[];
  error?: string;
}

function fmtCurrency(v: number | undefined | null): string {
  if (v == null || isNaN(v)) return '—';
  const abs = Math.abs(v);
  const sign = v < 0 ? '-' : '';
  return sign + '$' + abs.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtPct(v: number | undefined | null): string {
  if (v == null || isNaN(v)) return '—';
  return (v * 100).toFixed(2) + '%';
}

export default function CashflowScheduleViewer({ data }: { data?: CashflowScheduleData; params?: any }) {
  if (!data) {
    return <div className="text-sm text-[var(--text-muted)] text-center py-8">No cashflow data available.</div>;
  }

  if (data.error) {
    return (
      <div className="bg-[var(--error)]/10 border border-[var(--error)]/20 rounded-lg p-4">
        <div className="text-sm font-medium text-[var(--error)]">Error</div>
        <div className="text-xs text-[var(--text-secondary)] mt-1">{data.error}</div>
      </div>
    );
  }

  const cashflows = data.cashflows || [];

  return (
    <div className="flex flex-col h-full space-y-4">
      {/* IRR & MOIC Badges */}
      <div className="flex items-center gap-4 shrink-0">
        {data.irr != null && (
          <div className={`px-4 py-2 rounded-xl text-center ${
            data.irr >= 0.1 ? 'bg-[var(--success)]/15 border border-[var(--success)]/30' :
            data.irr >= 0 ? 'bg-[var(--warning)]/15 border border-[var(--warning)]/30' :
            'bg-[var(--error)]/15 border border-[var(--error)]/30'
          }`}>
            <div className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] mb-0.5">IRR</div>
            <div className={`text-lg font-bold font-mono ${
              data.irr >= 0.1 ? 'text-[var(--success)]' : data.irr >= 0 ? 'text-[var(--warning)]' : 'text-[var(--error)]'
            }`}>{fmtPct(data.irr)}</div>
          </div>
        )}
        {data.moic != null && (
          <div className={`px-4 py-2 rounded-xl text-center ${
            data.moic >= 1.5 ? 'bg-[var(--success)]/15 border border-[var(--success)]/30' :
            data.moic >= 1.0 ? 'bg-[var(--warning)]/15 border border-[var(--warning)]/30' :
            'bg-[var(--error)]/15 border border-[var(--error)]/30'
          }`}>
            <div className="text-[10px] uppercase tracking-wider text-[var(--text-muted)] mb-0.5">MOIC</div>
            <div className={`text-lg font-bold font-mono ${
              data.moic >= 1.5 ? 'text-[var(--success)]' : data.moic >= 1.0 ? 'text-[var(--warning)]' : 'text-[var(--error)]'
            }`}>{data.moic.toFixed(2)}x</div>
          </div>
        )}
      </div>

      {/* Summary Info */}
      <div className="grid grid-cols-4 gap-3 shrink-0">
        {data.principal != null && (
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
            <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">Principal</div>
            <div className="text-sm font-mono text-[var(--text-primary)]">{fmtCurrency(data.principal)}</div>
          </div>
        )}
        {data.rate != null && (
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
            <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">Rate</div>
            <div className="text-sm font-mono text-[var(--text-primary)]">{fmtPct(data.rate)}</div>
          </div>
        )}
        {data.term != null && (
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
            <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">Term</div>
            <div className="text-sm font-mono text-[var(--text-primary)]">{data.term} months</div>
          </div>
        )}
        {data.frequency && (
          <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-3">
            <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">Frequency</div>
            <div className="text-sm font-mono text-[var(--text-primary)]">{data.frequency}</div>
          </div>
        )}
      </div>

      {/* Cashflow Table */}
      {cashflows.length > 0 && (
        <div className="flex-1 overflow-y-auto bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden">
          <table className="w-full text-left text-sm">
            <thead className="bg-[var(--bg-tertiary)] border-b border-[var(--border-subtle)] text-[10px] uppercase tracking-wider text-[var(--text-muted)] sticky top-0 z-10">
              <tr>
                <th className="px-3 py-3 font-medium">Date</th>
                <th className="px-3 py-3 font-medium text-right">Outstanding Start</th>
                <th className="px-3 py-3 font-medium text-right">Interest</th>
                <th className="px-3 py-3 font-medium text-right">Principal</th>
                <th className="px-3 py-3 font-medium text-right">Fees</th>
                <th className="px-3 py-3 font-medium text-right">Total CF</th>
                <th className="px-3 py-3 font-medium text-right">Outstanding End</th>
                <th className="px-3 py-3 font-medium text-right">Rate</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-subtle)]">
              {cashflows.map((row, idx) => (
                <tr key={idx} className="hover:bg-[var(--bg-secondary)] transition-colors">
                  <td className="px-3 py-2 text-xs font-mono text-[var(--text-primary)]">{row.date || '—'}</td>
                  <td className="px-3 py-2 text-xs font-mono text-right text-[var(--text-primary)]">{fmtCurrency(row.outstanding_start)}</td>
                  <td className="px-3 py-2 text-xs font-mono text-right text-[var(--text-primary)]">{fmtCurrency(row.interest)}</td>
                  <td className={`px-3 py-2 text-xs font-mono text-right ${(row.principal ?? 0) < 0 ? 'text-[var(--error)]' : 'text-[var(--text-primary)]'}`}>
                    {fmtCurrency(row.principal)}
                  </td>
                  <td className={`px-3 py-2 text-xs font-mono text-right ${(row.fees ?? 0) < 0 ? 'text-[var(--error)]' : 'text-[var(--text-primary)]'}`}>
                    {fmtCurrency(row.fees)}
                  </td>
                  <td className={`px-3 py-2 text-xs font-mono text-right font-bold ${(row.total_cf ?? 0) < 0 ? 'text-[var(--error)]' : 'text-[var(--success)]'}`}>
                    {fmtCurrency(row.total_cf)}
                  </td>
                  <td className="px-3 py-2 text-xs font-mono text-right text-[var(--text-primary)]">{fmtCurrency(row.outstanding_end)}</td>
                  <td className="px-3 py-2 text-xs font-mono text-right text-[var(--text-secondary)]">{row.rate != null ? fmtPct(row.rate) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {cashflows.length === 0 && (
        <div className="text-sm text-[var(--text-muted)] text-center py-8">No cashflow schedule generated.</div>
      )}
    </div>
  );
}
