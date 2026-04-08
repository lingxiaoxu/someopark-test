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

// i18n keys for category labels — resolved at render time via t()
const CATEGORY_KEYS: Record<string, string> = {
  volatility: 'regime.catVolatility',
  credit: 'regime.catCredit',
  rates: 'regime.catRates',
  momentum_ai: 'regime.catMomentumAI',
  macro_stress: 'regime.catMacroStress',
  geopolitical: 'regime.catGeopolitical',
  strategy_vol: 'regime.catStrategyVol',
};

const CATEGORY_ORDER = ['volatility', 'credit', 'rates', 'momentum_ai', 'macro_stress', 'geopolitical', 'strategy_vol'];

// ── Vol decomposition detail panel ──────────────────────────────────────────
// sub-score key → { label, hint }
const VOL_SUB_META: Record<string, { label: string; hint: string }> = {
  eq_vix_level:  { label: 'VIX Level (倒U)', hint: 'VIX 水平在倒U型曲线上的位置：极低/极高→MTFS，中段→MRPT' },
  eq_vix_z:      { label: 'VIX z-score',     hint: 'VIX 相对1年均值的标准差；z高→处历史高位将回归→偏MRPT' },
  eq_long:       { label: 'Equity Long-term (CISS)', hint: 'VIX Level + z-score 经CISS相关矩阵加权合成（长期层）' },
  eq_short:      { label: 'VIX 90d %ile',    hint: 'VIX当前值在近90天hourly分布中的百分位；反转映射：近期高位→均值回归→偏MRPT' },
  equity_vol:    { label: 'Equity Vol',       hint: 'Long×0.60 + Short×0.40（时间维度分层合成）' },
  rt_move_level: { label: 'MOVE Level (倒U)', hint: 'MOVE 债券波动率在倒U型曲线上的位置' },
  rt_move_z:     { label: 'MOVE z-score',     hint: 'MOVE 相对1年均值的标准差；z高→历史高位→偏MRPT' },
  rt_long:       { label: 'Rates Long-term (CISS)', hint: 'MOVE Level + z-score 经CISS加权合成（长期层）' },
  rt_short:      { label: 'VXTLT 90d %ile',  hint: 'VXTLT(30yr国债vol)近90天hourly百分位；反转映射：高位→均值回归→偏MRPT' },
  rates_vol:     { label: 'Rates Vol',        hint: 'Long×0.60 + Short×0.40' },
  vol_composite: { label: 'Vol Composite',    hint: 'Equity×0.65 + Rates×0.35 → 最终波动率得分（0=MRPT，1=MTFS）' },
};

const VOL_SUB_ORDER = [
  'eq_vix_level', 'eq_vix_z', 'eq_long', 'eq_short', 'equity_vol',
  'rt_move_level', 'rt_move_z', 'rt_long', 'rt_short', 'rates_vol',
  'vol_composite',
];

function scoreBar(val: number) {
  const pct = Math.round(val * 100);
  const color = val >= 0.6 ? 'var(--error)' : val >= 0.4 ? 'var(--warning)' : 'var(--success)';
  return (
    <div className="flex items-center gap-1.5 mt-0.5">
      <div className="flex-1 h-1.5 rounded-full bg-[#ddd] overflow-hidden">
        <div className="h-full rounded-full" style={{ width: `${pct}%`, backgroundColor: color }} />
      </div>
      <span className="text-[10px] font-mono tabular-nums" style={{ color }}>{val.toFixed(3)}</span>
    </div>
  );
}

function VolDecomposition({ sub }: { sub: Record<string, number> }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const rows = VOL_SUB_ORDER.filter(k => sub[k] != null);
  if (rows.length === 0) return null;
  return (
    <div className="mt-2 border-t border-[var(--border-subtle)] pt-2">
      <button
        onClick={() => setOpen((o: boolean) => !o)}
        className="flex items-center gap-1 text-[10px] text-[var(--text-muted)] hover:text-[var(--text-secondary)] transition-colors"
      >
        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        {t('regime.volDecomposition')}
      </button>
      {open && (
        <div className="mt-2 flex flex-col gap-1.5">
          {rows.map(k => {
            const meta = VOL_SUB_META[k] || { label: k, hint: '' };
            const isComposite = k === 'vol_composite' || k === 'equity_vol' || k === 'rates_vol' || k === 'eq_long' || k === 'rt_long';
            return (
              <div key={k} className={`px-2 py-1.5 rounded-lg ${isComposite ? 'bg-[var(--bg-primary)] border border-[#ccc]' : 'bg-[var(--bg-secondary)]'}`}>
                <div className="flex items-center justify-between">
                  <span className={`text-[10px] ${isComposite ? 'font-semibold text-[var(--text-primary)]' : 'text-[var(--text-muted)]'}`}>
                    {meta.label}
                  </span>
                </div>
                {scoreBar(sub[k])}
                <div className="text-[9px] text-[var(--text-muted)] mt-0.5 leading-tight">{meta.hint}</div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

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

  const toggleCat = (cat: string) => setCollapsed((prev: Record<string, boolean>) => ({ ...prev, [cat]: !prev[cat] }));

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
            {regime.regime_label?.replace('_', '-')?.toUpperCase()} ({t('regime.score')}: {regime.regime_score?.toFixed(1)})
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
              const val = typeof raw === 'number' ? raw : (raw?.aggregate_score ?? raw?.aggregate ?? 0);
              const clr = val >= 0.6 ? 'var(--error)' : val >= 0.4 ? 'var(--warning)' : 'var(--success)';
              return (
                <div key={cat} className="px-2 py-1 rounded text-[11px] font-mono border" style={{ color: clr, borderColor: `color-mix(in srgb, ${clr} 30%, transparent)` }}>
                  {t(CATEGORY_KEYS[cat]) || cat}: {val.toFixed(2)}
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
                <span className="text-xs font-medium text-[var(--text-primary)]">{t(CATEGORY_KEYS[cat]) || cat}</span>
                <span className="text-[10px] text-[var(--text-muted)]">({items.length})</span>
              </div>
              {score != null && (
                <span className="text-[10px] font-mono text-[var(--text-muted)]">{t('regime.score')}: {(typeof score === 'number' ? score : (score?.aggregate_score ?? score?.aggregate ?? 0)).toFixed(2)}</span>
              )}
            </button>
            {!isCollapsed && (
              <div className="px-3 pb-3">
                <div className="grid grid-cols-3 gap-2">
                  {items.map(({ name, ind }) => (
                    <IndicatorCard key={name} name={name} ind={ind} t={t} />
                  ))}
                </div>
                {cat === 'volatility' && (score?.sub_scores || score?.sub) && (
                  <VolDecomposition sub={score.sub_scores ?? score.sub} />
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
