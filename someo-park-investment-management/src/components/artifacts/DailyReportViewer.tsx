import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FileText, Code, LayoutDashboard, AlertTriangle, Activity, ShieldAlert, CheckCircle2 } from 'lucide-react';
import { useApi } from '../../hooks/useApi';
import { getLatestDailyReport, getLatestDailyReportTxt } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';
import PairBadge from '../PairBadge';

export default function DailyReportViewer() {
  const { t } = useTranslation();
  const [view, setView] = useState<'ui' | 'txt' | 'json'>('ui');
  const { data: report, loading, error, refetch } = useApi(() => getLatestDailyReport(), []);
  const { data: txtReport } = useApi(() => getLatestDailyReportTxt(), []);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!report) return null;

  const regime = report.regime || {};
  const indicators = regime.indicators || {};
  const positionMonitor = report.position_monitor || {};
  const mrptActions = (positionMonitor.mrpt || []).map((a: any) => ({ ...a, _strategy: 'MRPT' }));
  const mtfsActions = (positionMonitor.mtfs || []).map((a: any) => ({ ...a, _strategy: 'MTFS' }));
  const allActions = [...mrptActions, ...mtfsActions];
  const actionRequired = allActions.filter((a: any) => a.action && !a.action.includes('HOLD'));
  const holdings = allActions.filter((a: any) => a.action?.includes('HOLD'));

  const labelColor = regime.regime_label === 'risk_off' ? 'var(--error)' :
    regime.regime_label === 'risk_on' ? 'var(--success)' : 'var(--warning)';
  const labelText = regime.regime_label?.replace('_', '-')?.toUpperCase() || 'NEUTRAL';

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div className="text-sm font-medium text-[var(--text-primary)]">{t('dailyReport.title')}</div>
        <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
          <button onClick={() => setView('ui')} className={`px-2 py-1 flex items-center gap-1 text-xs rounded-sm transition-colors ${view === 'ui' ? 'bg-[var(--bg-secondary)] text-[var(--text-primary)]' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
            <LayoutDashboard className="w-3 h-3" /> UI
          </button>
          <button onClick={() => setView('txt')} className={`px-2 py-1 flex items-center gap-1 text-xs rounded-sm transition-colors ${view === 'txt' ? 'bg-[var(--bg-secondary)] text-[var(--text-primary)]' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
            <FileText className="w-3 h-3" /> TXT
          </button>
          <button onClick={() => setView('json')} className={`px-2 py-1 flex items-center gap-1 text-xs rounded-sm transition-colors ${view === 'json' ? 'bg-[var(--bg-secondary)] text-[var(--text-primary)]' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>
            <Code className="w-3 h-3" /> JSON
          </button>
        </div>
      </div>

      <div className={`flex-1 overflow-y-auto bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-lg p-4 ${view !== 'ui' ? 'font-mono text-xs text-[var(--text-secondary)] whitespace-pre-wrap' : ''}`}>
        {view === 'ui' && (
          <div className="space-y-6">
            <div className="flex items-center justify-between border-b border-[var(--border-subtle)] pb-3">
              <div>
                <h2 className="text-lg font-semibold text-[var(--text-primary)]">{t('dailyReport.dailyQuantReport')}</h2>
                <div className="text-xs text-[var(--text-muted)] mt-1">{t('dailyReport.signalDate', { date: report.signal_date, time: report.generated_at })}</div>
              </div>
              <div className="px-3 py-1 border rounded-full text-xs font-medium flex items-center gap-1.5" style={{ color: labelColor, borderColor: labelColor, backgroundColor: `color-mix(in srgb, ${labelColor} 10%, transparent)` }}>
                <ShieldAlert className="w-3.5 h-3.5" />
                {labelText} ({regime.regime_score?.toFixed(1)})
              </div>
            </div>

            <div className="space-y-3">
              <h3 className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider flex items-center gap-1.5">
                <Activity className="w-4 h-4" /> {t('dailyReport.regimeAnalysis')}
              </h3>
              <div className="grid grid-cols-2 gap-3">
                {Object.entries(indicators).slice(0, 4).map(([key, ind]: any) => (
                  <div key={key} className="bg-[var(--bg-secondary)] border border-[var(--border-subtle)] rounded-lg p-3">
                    <div className="text-xs text-[var(--text-muted)] mb-1">{ind.name || key}</div>
                    <div className="flex items-end gap-2">
                      <span className="text-xl font-mono text-[var(--text-primary)]">{ind.formatted || ind.raw_value?.toFixed?.(2) || ind.raw_value}</span>
                      {ind.interpretation && <span className="text-xs text-[var(--text-muted)] mb-1 truncate max-w-[120px]">{ind.interpretation}</span>}
                    </div>
                    {ind.history?.avg90 != null && <div className="text-[10px] text-[var(--text-muted)] mt-1">90d avg: {ind.history.avg90.toFixed(2)}</div>}
                  </div>
                ))}
              </div>
              {regime.interpretation && (
                <div className="bg-[var(--accent-primary)]/10 border border-[var(--accent-primary)]/20 rounded-lg p-3 text-sm text-[var(--text-primary)]">
                  <span className="font-medium text-[var(--accent-primary)]">{t('dailyReport.recommendation')}</span> {regime.interpretation}
                </div>
              )}
            </div>

            {actionRequired.length > 0 && (
              <div className="space-y-3">
                <h3 className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider flex items-center gap-1.5">
                  <AlertTriangle className="w-4 h-4" /> {t('dailyReport.actionRequired')}
                </h3>
                <div className="space-y-2">
                  {actionRequired.map((act: any, idx: number) => (
                    <div key={idx} className="flex items-center justify-between bg-[var(--bg-secondary)] border border-[var(--border-subtle)] rounded-lg p-3">
                      <div className="flex items-center gap-3">
                        <div className={`w-10 h-10 rounded-full flex items-center justify-center font-bold text-[10px] ${
                          act.action?.includes('OPEN') ? 'bg-[var(--success)]/10 text-[var(--success)]' :
                          'bg-[var(--warning)]/10 text-[var(--warning)]'
                        }`}>{act.action?.split('_')[0]}</div>
                        <div>
                          <PairBadge
                            pair={act.pair}
                            direction={act.direction}
                            strategy={act._strategy?.toLowerCase()}
                            compact
                          />
                          <div className="text-xs text-[var(--text-muted)] mt-0.5">
                            {act._strategy}{act.param_set ? ` | ${act.param_set}` : ''}
                          </div>
                        </div>
                      </div>
                      <div className="text-right max-w-[300px]">
                        {act.z_score != null ? (
                          <div className="text-sm font-mono text-[var(--text-primary)]">Z: {act.z_score.toFixed(2)}</div>
                        ) : act.momentum_spread != null ? (
                          <div className="text-sm font-mono text-[var(--text-primary)]">Mom: {act.momentum_spread.toFixed(2)}</div>
                        ) : act.note ? (
                          <div className="text-xs text-[var(--text-secondary)] leading-relaxed">{act.note}</div>
                        ) : null}
                        {act.note && act.z_score != null && (
                          <div className="text-[10px] text-[var(--text-muted)] mt-0.5 truncate" title={act.note}>{act.note}</div>
                        )}
                        {act.note && act.momentum_spread != null && (
                          <div className="text-[10px] text-[var(--text-muted)] mt-0.5 truncate" title={act.note}>{act.note}</div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {holdings.length > 0 && (
              <div className="space-y-3">
                <h3 className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider flex items-center gap-1.5">
                  <CheckCircle2 className="w-4 h-4" /> {t('dailyReport.holdingsMonitor')}
                </h3>
                <div className="bg-[var(--bg-secondary)] border border-[var(--border-subtle)] rounded-lg overflow-hidden">
                  <table className="w-full text-sm text-left">
                    <thead className="text-[10px] text-[var(--text-muted)] uppercase bg-[var(--bg-primary)]">
                      <tr>
                        <th className="px-3 py-2 font-medium">{t('common.pair')}</th>
                        <th className="px-3 py-2 font-medium">{t('common.strategy')}</th>
                        <th className="px-3 py-2 font-medium">{t('common.direction')}</th>
                        <th className="px-3 py-2 font-medium">{t('dailyReport.signal')}</th>
                        <th className="px-3 py-2 font-medium">{t('dailyReport.pnl')}</th>
                        <th className="px-3 py-2 font-medium">{t('common.note')}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[var(--border-subtle)]">
                      {holdings.map((h: any, idx: number) => (
                        <tr key={idx}>
                          <td className="px-3 py-2"><PairBadge pair={h.pair} direction={h.direction} strategy={h._strategy?.toLowerCase()} compact /></td>
                          <td className="px-3 py-2 text-xs text-[var(--text-secondary)]">{h._strategy}</td>
                          <td className="px-3 py-2 text-xs">
                            <span className={h.direction === 'long' ? 'text-[var(--success)]' : 'text-[var(--error)]'}>{h.direction?.toUpperCase()}</span>
                          </td>
                          <td className="px-3 py-2 font-mono text-xs text-[var(--text-secondary)]">
                            {h.z_score != null ? `Z: ${h.z_score.toFixed(2)}` : h.momentum_spread != null ? `Mom: ${h.momentum_spread.toFixed(2)}` : '—'}
                          </td>
                          <td className="px-3 py-2 font-mono text-xs" style={{ color: (h.unrealized_pnl ?? 0) >= 0 ? 'var(--success)' : 'var(--error)' }}>
                            {h.unrealized_pnl != null ? `$${h.unrealized_pnl.toLocaleString('en-US', { minimumFractionDigits: 0 })} (${h.unrealized_pnl_pct >= 0 ? '+' : ''}${h.unrealized_pnl_pct?.toFixed(2)}%)` : '—'}
                          </td>
                          <td className="px-3 py-2 text-[10px] text-[var(--text-muted)] max-w-[200px] truncate" title={h.note}>{h.note || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}

        {view === 'txt' && (txtReport || t('dailyReport.loadingTxt'))}

        {view === 'json' && JSON.stringify(report, null, 2)}
      </div>
    </div>
  );
}
