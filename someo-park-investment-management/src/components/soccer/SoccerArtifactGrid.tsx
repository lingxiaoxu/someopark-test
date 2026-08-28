/**
 * SoccerArtifactGrid — the middle artifact grid shown in Club Soccer Market mode.
 * Mirrors PredictionArtifactGrid's layout/classes (so the CSS inversion applies
 * identically) and its five-group IA (overview / teamIntel / live / quality /
 * reports), with every button opening a `soccer_*` artifact. 附录 C parity: all WC
 * cards are mirrored with club semantics EXCEPT microfootball (C-26: 明确不移植).
 * Labels are i18n (soccer.*); the translated label is passed as the artifact title.
 */
import {
  Trophy, ListOrdered, Activity, CalendarClock, Clock, Zap, LayoutGrid,
  Target, Users, Shapes, TrendingUp, GitCompare, Coins, LineChart, Gauge,
  FlaskConical, SlidersHorizontal, Landmark, Download, Network, ShieldAlert,
} from 'lucide-react';
import type { ComponentType } from 'react';
import { useTranslation } from 'react-i18next';

type Artifact = { type: string; title: string };
type Item = { type: string; i18nKey: string; Icon: ComponentType<{ className?: string }> };

// i18nKey resolves to soccer.<key>; shared with the SoccerArtifact viewer titles.
// (soccer_overview replaces the v1 soccer_model_notes card; the old type stays a
// working REGISTRY alias for deep links.)
export const SOCCER_ITEMS: Item[] = [
  { type: 'soccer_season_odds',   i18nKey: 'seasonOdds',        Icon: Trophy },
  { type: 'soccer_league_table',  i18nKey: 'leagueTable',       Icon: ListOrdered },
  { type: 'soccer_top_scorer',    i18nKey: 'topScorer',         Icon: Target },
  { type: 'soccer_squad',         i18nKey: 'squadStrength',     Icon: Users },
  { type: 'soccer_styles',        i18nKey: 'teamStyles',        Icon: Shapes },
  { type: 'soccer_form',          i18nKey: 'recentForm',        Icon: TrendingUp },
  { type: 'soccer_match_pricing', i18nKey: 'matchPricing',      Icon: Activity },
  { type: 'soccer_predictions',   i18nKey: 'todaysPredictions', Icon: CalendarClock },
  { type: 'soccer_divergence',    i18nKey: 'modelVsMarket',     Icon: GitCompare },
  { type: 'soccer_inplay',        i18nKey: 'inPlay',            Icon: Zap },
  { type: 'soccer_pricetrack',    i18nKey: 'priceTrack',        Icon: Coins },
  { type: 'soccer_schedule',      i18nKey: 'schedule',          Icon: Clock },
  // i18nKey is nested ('bracket.title') because every string this card owns lives
  // under soccer.bracket.* — `soccer.bracket` itself is the namespace, not a label.
  { type: 'soccer_bracket',       i18nKey: 'bracket.title',     Icon: Network },
  { type: 'soccer_performance',   i18nKey: 'accuracyPnl',       Icon: LineChart },
  { type: 'soccer_calibration',   i18nKey: 'calibration',       Icon: Gauge },
  { type: 'soccer_backtest',      i18nKey: 'backtest',          Icon: FlaskConical },
  { type: 'soccer_params',        i18nKey: 'paramSweep',        Icon: SlidersHorizontal },
  { type: 'soccer_overview',      i18nKey: 'overview',          Icon: LayoutGrid },
  { type: 'soccer_venues',        i18nKey: 'venuesApi',         Icon: Landmark },
  { type: 'soccer_risk',          i18nKey: 'riskLimits',        Icon: ShieldAlert },
  { type: 'soccer_pdfs',          i18nKey: 'downloadReports',   Icon: Download },
];

// ── Categorized layout (opt-in via the Settings "card categorization" toggle) —
// mirrors PREDICTION_GROUPS; every item belongs to exactly one group.
type Group = {
  key: string; i18nKey: string;
  Icon: ComponentType<{ className?: string }>;
  types: string[];
};

export const SOCCER_GROUPS: Group[] = [
  { key: 'overview', i18nKey: 'groupOverview', Icon: LayoutGrid,
    types: ['soccer_overview', 'soccer_venues', 'soccer_risk'] },
  { key: 'teamIntel', i18nKey: 'groupTeamIntel', Icon: Users,
    types: ['soccer_season_odds', 'soccer_league_table', 'soccer_top_scorer', 'soccer_squad', 'soccer_styles', 'soccer_form'] },
  { key: 'live', i18nKey: 'groupLive', Icon: Zap,
    types: ['soccer_match_pricing', 'soccer_predictions', 'soccer_divergence', 'soccer_inplay', 'soccer_pricetrack', 'soccer_schedule', 'soccer_bracket'] },
  { key: 'quality', i18nKey: 'groupQuality', Icon: Gauge,
    types: ['soccer_performance', 'soccer_calibration', 'soccer_backtest', 'soccer_params'] },
  { key: 'reports', i18nKey: 'groupReports', Icon: Download,
    types: ['soccer_pdfs'] },
];

export default function SoccerArtifactGrid({ onOpen, categorized = false }: { onOpen: (a: Artifact) => void; categorized?: boolean }) {
  const { t } = useTranslation();
  const itemByType: Record<string, Item> = Object.fromEntries(SOCCER_ITEMS.map((i) => [i.type, i]));

  if (categorized) {
    return (
      <div className="flex flex-col gap-3">
        {SOCCER_GROUPS.map(({ key, i18nKey, Icon: GIcon, types }) => (
          <div key={key}>
            <div className="flex items-center gap-1.5 mb-1.5 text-[10px] font-bold uppercase tracking-wider text-[var(--text-muted)]">
              <GIcon className="w-3 h-3" /> {t(`soccer.${i18nKey}`)}
            </div>
            <div className="grid grid-cols-2 gap-2">
              {types.map((type) => {
                const it = itemByType[type];
                if (!it) return null;
                const { Icon } = it;
                const title = t(`soccer.${it.i18nKey}`);
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
      {SOCCER_ITEMS.map(({ type, i18nKey, Icon }) => {
        const title = t(`soccer.${i18nKey}`);
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
