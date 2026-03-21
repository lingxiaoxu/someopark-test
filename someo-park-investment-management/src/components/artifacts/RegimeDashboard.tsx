import React, { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Activity, TrendingUp, TrendingDown, AlertTriangle, ChevronDown, ChevronRight } from 'lucide-react';
import { useApi } from '../../hooks/useApi';
import { getLatestRegime } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';

// Map each indicator to its component_scores category
const INDICATOR_CATEGORY: Record<string, string> = {
  vix_level: 'volatility',
  vix_z: 'volatility',
  move_level: 'volatility',
  hy_spread_level: 'credit',
  ig_spread_level: 'credit',
  yield_curve_level: 'rates',
  effr_level: 'rates',
  effr_1y_change: 'rates',
  breakeven_10y_level: 'rates',
  fin_stress_level: 'macro_stress',
  nfci_level: 'macro_stress',
  consumer_sent_level: 'macro_stress',
  recession_flag: 'macro_stress',
  unrate_level: 'macro_stress',
  payems_mom: 'macro_stress',
  icsa_level: 'macro_stress',
  ccsa_level: 'macro_stress',
  nvda_20d: 'momentum_ai',
  arkk_20d: 'momentum_ai',
  soxx_20d: 'momentum_ai',
  gld_20d: 'strategy_vol',
  uso_20d: 'strategy_vol',
  uup_20d: 'strategy_vol',
  spy_20d: 'strategy_vol',
  tnx_level: 'rates',
};

const CATEGORY_LABELS: Record<string, string> = {
  volatility: 'Volatility',
  credit: 'Credit',
  rates: 'Rates & Yields',
  momentum_ai: 'Momentum / AI',
  macro_stress: 'Macro Stress',
  geopolitical: 'Geopolitical',
  strategy_vol: 'Strategy & Vol',
};

const CATEGORY_ORDER = ['volatility', 'credit', 'rates', 'momentum_ai', 'macro_stress', 'geopolitical', 'strategy_vol'];

function interpColor(interp: string | undefined): string {
  if (!interp) return 'var(--text-secondary)';
  const lower = interp.toLowerCase();
  if (lower.includes('elevated') || lower.includes('high') || lower.includes('inverted') || lower.includes('tight') || lower.includes('negative') || lower.includes('bearish') || lower.includes('recession')) return 'var(--error)';
  if (lower.includes('low') || lower.includes('normal') || lower.includes('positive') || lower.includes('bullish') || lower.includes('expanding') || lower.includes('healthy')) return 'var(--success)';
  if (lower.includes('moderate') || lower.includes('neutral') || lower.includes('flat') || lower.includes('mixed')) return 'var(--warning)';
  return 'var(--text-secondary)';
}

function IndicatorCard({ name, ind, t }: { name: string; ind: any; key?: any; t: (key: string, opts?: any) => string }) {
  const label = ind.description || name.replace(/_/g, ' ').replace(/\b\w/g, (c: string) => c.toUpperCase());
  const color = interpColor(ind.interpretation);

  return (
    <div className="p-2.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)]">
      <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1 truncate" title={label}>{label}</div>
      <div className="flex items-end gap-1.5">
        <span className="text-sm font-semibold text-[var(--text-primary)]">{ind.formatted ?? ind.raw_value?.toFixed?.(2) ?? '—'}</span>
        {ind.history?.change_abs != null && (
          <span className="text-[10px] text-[var(--text-muted)] mb-0.5 flex items-center">
            {ind.history.change_abs > 0 ? <TrendingUp className="w-2.5 h-2.5 mr-0.5" /> : <TrendingDown className="w-2.5 h-2.5 mr-0.5" />}
            {ind.history.change_abs > 0 ? '+' : ''}{typeof ind.history.change_abs === 'number' ? ind.history.change_abs.toFixed(2) : ind.history.change_abs}
          </span>
        )}
      </div>
      {ind.interpretation && (
        <div className="text-[10px] mt-1 font-medium truncate" title={ind.interpretation} style={{ color }}>{ind.interpretation}</div>
      )}
      {ind.history?.avg90 != null && (
        <div className="text-[10px] text-[var(--text-muted)] mt-0.5">{t('regime.history90d', { value: typeof ind.history.avg90 === 'number' ? ind.history.avg90.toFixed(2) : ind.history.avg90 })}</div>
      )}
    </div>
  );
}

export default function RegimeDashboard() {
  const { t } = useTranslation();
  const { data: regime, loading, error, refetch } = useApi(() => getLatestRegime(), []);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const groupedIndicators = useMemo(() => {
    if (!regime?.indicators) return {};
    const groups: Record<string, { name: string; ind: any }[]> = {};
    for (const cat of CATEGORY_ORDER) groups[cat] = [];

    for (const [key, ind] of Object.entries(regime.indicators)) {
      const cat = INDICATOR_CATEGORY[key] || 'macro_stress';
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push({ name: key, ind });
    }
    return groups;
  }, [regime]);

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={refetch} />;
  if (!regime) return null;

  const labelColor = regime.regime_label === 'risk_off' ? 'var(--error)' :
    regime.regime_label === 'risk_on' ? 'var(--success)' : 'var(--text-primary)';

  const toggleCat = (cat: string) => setCollapsed(prev => ({ ...prev, [cat]: !prev[cat] }));

  const componentScores = regime.component_scores || {};

  return (
    <div className="flex flex-col gap-3">
      {/* Header */}
      <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-4">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <Activity className="w-4 h-4 text-[var(--accent-primary)]" />
            <h3 className="text-sm font-medium">{t('regime.title')}</h3>
          </div>
          <div className="px-2.5 py-1 rounded-full text-xs font-medium border" style={{ color: labelColor, borderColor: labelColor, backgroundColor: `color-mix(in srgb, ${labelColor} 10%, transparent)` }}>
            {regime.regime_label?.replace('_', '-')?.toUpperCase()} (Score: {regime.regime_score?.toFixed(1)})
          </div>
        </div>

        {regime.interpretation && (
          <p className="text-xs text-[var(--text-secondary)] leading-relaxed mb-3">{regime.interpretation}</p>
        )}

        {/* Weights */}
        <div className="flex gap-3">
          <div className="flex-1 p-2.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)]">
            <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('regime.mrptMtfsWeight')}</div>
            <span className="text-sm font-semibold text-[var(--accent-primary)]">
              {(regime.mrpt_weight * 100).toFixed(0)}% / {(regime.mtfs_weight * 100).toFixed(0)}%
            </span>
          </div>
          {regime.weight_rationale && (
            <div className="flex-[2] p-2.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)]">
              <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-1">{t('regime.rationale')}</div>
              <div className="text-[11px] text-[var(--text-secondary)] line-clamp-2">{regime.weight_rationale}</div>
            </div>
          )}
        </div>
      </div>

      {/* Risk-Off Warning */}
      {regime.regime_label === 'risk_off' && (
        <div className="bg-[var(--warning)]/10 border border-[var(--warning)]/20 rounded-xl p-3 flex gap-3">
          <AlertTriangle className="w-5 h-5 text-[var(--warning)] shrink-0" />
          <div>
            <h4 className="text-xs font-medium text-[var(--warning)] mb-1">{t('regime.riskOffTitle')}</h4>
            <p className="text-[11px] text-[var(--warning)]/80 leading-relaxed">
              {t('regime.riskOffMsg')}
            </p>
          </div>
        </div>
      )}

      {/* Component Scores Summary */}
      {Object.keys(componentScores).length > 0 && (
        <div className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl p-3">
          <div className="text-[10px] text-[var(--text-muted)] uppercase tracking-wider mb-2">{t('regime.componentScores')}</div>
          <div className="flex flex-wrap gap-2">
            {CATEGORY_ORDER.filter(cat => componentScores[cat] != null).map(cat => {
              const raw = componentScores[cat];
              const val = typeof raw === 'number' ? raw : raw?.aggregate_score ?? 0;
              const clr = val >= 0.6 ? 'var(--error)' : val >= 0.4 ? 'var(--warning)' : 'var(--success)';
              return (
                <div key={cat} className="px-2 py-1 rounded text-[11px] font-mono border" style={{ color: clr, borderColor: `color-mix(in srgb, ${clr} 30%, transparent)` }}>
                  {CATEGORY_LABELS[cat] || cat}: {val.toFixed(2)}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Indicator Categories */}
      {CATEGORY_ORDER.filter(cat => groupedIndicators[cat]?.length > 0).map(cat => {
        const isCollapsed = collapsed[cat];
        const items = groupedIndicators[cat];
        const score = componentScores[cat];
        return (
          <div key={cat} className="bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl overflow-hidden">
            <button
              onClick={() => toggleCat(cat)}
              className="w-full flex items-center justify-between px-3 py-2 hover:bg-[var(--bg-secondary)] transition-colors"
            >
              <div className="flex items-center gap-2">
                {isCollapsed ? <ChevronRight className="w-3.5 h-3.5 text-[var(--text-muted)]" /> : <ChevronDown className="w-3.5 h-3.5 text-[var(--text-muted)]" />}
                <span className="text-xs font-medium text-[var(--text-primary)]">{CATEGORY_LABELS[cat] || cat}</span>
                <span className="text-[10px] text-[var(--text-muted)]">({items.length})</span>
              </div>
              {score != null && (
                <span className="text-[10px] font-mono text-[var(--text-muted)]">score: {(typeof score === 'number' ? score : score?.aggregate_score ?? 0).toFixed(2)}</span>
              )}
            </button>
            {!isCollapsed && (
              <div className="px-3 pb-3 grid grid-cols-3 gap-2">
                {items.map(({ name, ind }) => (
                  <IndicatorCard key={name} name={name} ind={ind} t={t} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
