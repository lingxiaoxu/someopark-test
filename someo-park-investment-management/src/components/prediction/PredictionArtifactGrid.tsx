/**
 * PredictionArtifactGrid — the middle artifact grid shown in Prediction Market mode.
 * Mirrors the stock grid's layout/classes (so the CSS inversion applies identically),
 * but every button opens a World Cup `wc_*` artifact instead of a stock one.
 * Labels are i18n (prediction.*); the translated label is passed as the artifact title.
 */
import {
  Trophy, Activity, Target, GitCompare, Zap, CalendarClock, Clock,
  LineChart, Gauge, LayoutGrid, Landmark,
  Download, FlaskConical, Users, SlidersHorizontal, TrendingUp, Coins, ChevronsUp, Shapes, Clapperboard,
} from 'lucide-react';
import type { ComponentType } from 'react';
import { useTranslation } from 'react-i18next';

type Artifact = { type: string; title: string };
type Item = { type: string; i18nKey: string; Icon: ComponentType<{ className?: string }> };

// i18nKey resolves to prediction.<key>; shared with PredictionArtifact viewer titles.
export const PREDICTION_ITEMS: Item[] = [
  { type: 'wc_champion',      i18nKey: 'championOdds',     Icon: Trophy },
  { type: 'wc_reach_round',   i18nKey: 'reachRound',       Icon: ChevronsUp },
  { type: 'wc_match_pricing', i18nKey: 'matchPricing',     Icon: Activity },
  { type: 'wc_golden_boot',   i18nKey: 'goldenBoot',       Icon: Target },
  { type: 'wc_squad',         i18nKey: 'squadStrength',    Icon: Users },
  { type: 'wc_styles',        i18nKey: 'teamStyles',       Icon: Shapes },
  { type: 'wc_form',          i18nKey: 'recentForm',       Icon: TrendingUp },
  { type: 'wc_divergence',    i18nKey: 'modelVsMarket',    Icon: GitCompare },
  { type: 'wc_inplay',        i18nKey: 'inPlayArb',        Icon: Zap },
  { type: 'wc_pricetrack',    i18nKey: 'priceTrack',       Icon: Coins },
  { type: 'wc_predictions',   i18nKey: 'todaysPredictions', Icon: CalendarClock },
  { type: 'wc_schedule',      i18nKey: 'schedule',         Icon: Clock },
  { type: 'wc_performance',   i18nKey: 'accuracyPnl',      Icon: LineChart },
  { type: 'wc_calibration',   i18nKey: 'calibration',      Icon: Gauge },
  { type: 'wc_backtest',      i18nKey: 'backtest',         Icon: FlaskConical },
  { type: 'wc_params',        i18nKey: 'paramSweep',       Icon: SlidersHorizontal },
  { type: 'wc_overview',      i18nKey: 'systemModelNotes', Icon: LayoutGrid },
  { type: 'wc_venues',        i18nKey: 'venuesApi',        Icon: Landmark },
  { type: 'wc_microfootball', i18nKey: 'microfootballSim', Icon: Clapperboard },
  { type: 'wc_pdfs',          i18nKey: 'downloadReports',  Icon: Download },
];

// ── Categorized layout (opt-in via the Settings "card categorization" toggle) —
// mirrors MacroArtifactGrid's MACRO_GROUPS structure; every item belongs to exactly
// one group, all 20 items covered.
type Group = {
  key: string; i18nKey: string;
  Icon: ComponentType<{ className?: string }>;
  types: string[];
};

export const PREDICTION_GROUPS: Group[] = [
  { key: 'overview', i18nKey: 'groupOverview', Icon: LayoutGrid,
    types: ['wc_overview', 'wc_venues'] },
  { key: 'teamIntel', i18nKey: 'groupTeamIntel', Icon: Users,
    types: ['wc_champion', 'wc_reach_round', 'wc_golden_boot', 'wc_squad', 'wc_styles', 'wc_form'] },
  { key: 'live', i18nKey: 'groupLive', Icon: Zap,
    types: ['wc_match_pricing', 'wc_divergence', 'wc_inplay', 'wc_pricetrack', 'wc_predictions', 'wc_schedule'] },
  { key: 'quality', i18nKey: 'groupQuality', Icon: Gauge,
    types: ['wc_performance', 'wc_calibration', 'wc_backtest', 'wc_params'] },
  { key: 'reports', i18nKey: 'groupReports', Icon: Download,
    types: ['wc_pdfs', 'wc_microfootball'] },
];

export default function PredictionArtifactGrid({ onOpen, categorized = false }: { onOpen: (a: Artifact) => void, categorized?: boolean }) {
  const { t } = useTranslation();
  const itemByType: Record<string, Item> = Object.fromEntries(PREDICTION_ITEMS.map((i) => [i.type, i]));

  if (categorized) {
    return (
      <div className="flex flex-col gap-3">
        {PREDICTION_GROUPS.map(({ key, i18nKey, Icon: GIcon, types }) => (
          <div key={key}>
            <div className="flex items-center gap-1.5 mb-1.5 text-[10px] font-bold uppercase tracking-wider text-[var(--text-muted)]">
              <GIcon className="w-3 h-3" /> {t(`prediction.${i18nKey}`)}
            </div>
            <div className="grid grid-cols-2 gap-2">
              {types.map((type) => {
                const it = itemByType[type];
                if (!it) return null;
                const { Icon } = it;
                const title = t(`prediction.${it.i18nKey}`);
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
          </div>
        ))}
      </div>
    );
  }

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
