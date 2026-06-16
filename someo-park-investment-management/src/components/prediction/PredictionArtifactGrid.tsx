/**
 * PredictionArtifactGrid — the middle artifact grid shown in Prediction Market mode.
 * Mirrors the stock grid's layout/classes (so the CSS inversion applies identically),
 * but every button opens a World Cup `wc_*` artifact instead of a stock one.
 * Labels are i18n (prediction.*); the translated label is passed as the artifact title.
 */
import {
  Trophy, Activity, Target, GitCompare, Zap, CalendarClock, Clock,
  LineChart, ShieldAlert, Gauge, LayoutGrid, Landmark, FileText, Cpu,
  Sparkles, Download,
} from 'lucide-react';
import type { ComponentType } from 'react';
import { useTranslation } from 'react-i18next';

type Artifact = { type: string; title: string };
type Item = { type: string; i18nKey: string; Icon: ComponentType<{ className?: string }> };

// i18nKey resolves to prediction.<key>; shared with PredictionArtifact viewer titles.
export const PREDICTION_ITEMS: Item[] = [
  { type: 'wc_champion',      i18nKey: 'championOdds',     Icon: Trophy },
  { type: 'wc_match_pricing', i18nKey: 'matchPricing',     Icon: Activity },
  { type: 'wc_golden_boot',   i18nKey: 'goldenBoot',       Icon: Target },
  { type: 'wc_divergence',    i18nKey: 'modelVsMarket',    Icon: GitCompare },
  { type: 'wc_inplay',        i18nKey: 'inPlayArb',        Icon: Zap },
  { type: 'wc_predictions',   i18nKey: 'todaysPredictions', Icon: CalendarClock },
  { type: 'wc_schedule',      i18nKey: 'schedule',         Icon: Clock },
  { type: 'wc_performance',   i18nKey: 'accuracyPnl',      Icon: LineChart },
  { type: 'wc_risk',          i18nKey: 'riskReport',       Icon: ShieldAlert },
  { type: 'wc_calibration',   i18nKey: 'calibration',      Icon: Gauge },
  { type: 'wc_overview',      i18nKey: 'systemOverview',   Icon: LayoutGrid },
  { type: 'wc_venues',        i18nKey: 'venuesGates',      Icon: Landmark },
  { type: 'wc_methodology',   i18nKey: 'modelNotes',       Icon: FileText },
  { type: 'wc_budget',        i18nKey: 'apiBudget',        Icon: Cpu },
  { type: 'wc_value',         i18nKey: 'valueHowToSee',    Icon: Sparkles },
  { type: 'wc_pdfs',          i18nKey: 'downloadReports',  Icon: Download },
];

export default function PredictionArtifactGrid({ onOpen }: { onOpen: (a: Artifact) => void }) {
  const { t } = useTranslation();
  return (
    <div className="grid grid-cols-2 gap-2">
      {PREDICTION_ITEMS.map(({ type, i18nKey, Icon }) => {
        const title = t(`prediction.${i18nKey}`);
        return (
          <button
            key={type}
            onClick={() => onOpen({ type, title })}
            className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]"
          >
            <Icon className="w-4 h-4 text-[var(--accent-primary)]" /> {title}
          </button>
        );
      })}
    </div>
  );
}
