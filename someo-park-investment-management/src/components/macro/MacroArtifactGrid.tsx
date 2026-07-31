/**
 * MacroArtifactGrid — the middle artifact grid shown in Macro Markets mode.
 * Mirrors PredictionArtifactGrid's layout/classes (so the CSS inversion applies
 * identically). Since the §21.3 IA reorg the grid shows 5 GROUP tiles (overview /
 * series / trading / quality / reports); clicking a group opens its first member
 * artifact. All 13 legacy `macro_*` artifact types keep working (MACRO_ITEMS is
 * still exported for titles + the second-level tab bar inside MacroArtifact).
 * Labels are i18n (macro.*); the translated label is passed as the artifact title.
 */
import {
  LayoutDashboard, Landmark, TrendingUp, Users, Flame, GitCompare,
  ClipboardList, LineChart, Gauge, LayoutGrid, Shield, BookOpen, Download,
  BarChart3, ShieldCheck,
} from 'lucide-react';
import type { ComponentType } from 'react';
import { useTranslation } from 'react-i18next';

type Artifact = { type: string; title: string };
type Item = { type: string; i18nKey: string; Icon: ComponentType<{ className?: string }> };

// i18nKey resolves to macro.<key>; shared with the MacroArtifact viewer titles.
export const MACRO_ITEMS: Item[] = [
  { type: 'macro_board',       i18nKey: 'board',       Icon: LayoutDashboard },
  { type: 'macro_fed',         i18nKey: 'fed',         Icon: Landmark },
  { type: 'macro_inflation',   i18nKey: 'inflation',   Icon: TrendingUp },
  { type: 'macro_labor',       i18nKey: 'labor',       Icon: Users },
  { type: 'macro_energy',      i18nKey: 'energy',      Icon: Flame },
  { type: 'macro_divergence',  i18nKey: 'divergence',  Icon: GitCompare },
  { type: 'macro_decisions',   i18nKey: 'decisions',   Icon: ClipboardList },
  { type: 'macro_performance', i18nKey: 'performance', Icon: LineChart },
  { type: 'macro_calibration', i18nKey: 'calibration', Icon: Gauge },
  { type: 'macro_coverage',    i18nKey: 'coverage',    Icon: LayoutGrid },
  { type: 'macro_risk',        i18nKey: 'risk',        Icon: Shield },
  { type: 'macro_overview',    i18nKey: 'overview',    Icon: BookOpen },
  { type: 'macro_reports',     i18nKey: 'reports',     Icon: Download },
];

// ── §21.3 five-group IA — every legacy type belongs to exactly one group ─────
export type MacroGroup = {
  key: string; i18nKey: string;
  Icon: ComponentType<{ className?: string }>;
  types: string[]; // first entry = the artifact a group tile opens
};

export const MACRO_GROUPS: MacroGroup[] = [
  { key: 'overview', i18nKey: 'groupOverview', Icon: LayoutDashboard,
    types: ['macro_board', 'macro_overview'] },
  { key: 'series',   i18nKey: 'groupSeries',   Icon: BarChart3,
    types: ['macro_fed', 'macro_inflation', 'macro_labor', 'macro_energy'] },
  { key: 'trading',  i18nKey: 'groupTrading',  Icon: ClipboardList,
    types: ['macro_decisions', 'macro_performance', 'macro_divergence'] },
  { key: 'quality',  i18nKey: 'groupQuality',  Icon: ShieldCheck,
    types: ['macro_calibration', 'macro_coverage', 'macro_risk'] },
  { key: 'reports',  i18nKey: 'groupReports',  Icon: Download,
    types: ['macro_reports'] },
];

/** Group containing a given artifact type (undefined for non-macro types). */
export const groupOfType = (type: string): MacroGroup | undefined =>
  MACRO_GROUPS.find((g) => g.types.includes(type));

export default function MacroArtifactGrid({ onOpen }: { onOpen: (a: Artifact) => void }) {
  const { t } = useTranslation();
  const keyByType: Record<string, string> = Object.fromEntries(MACRO_ITEMS.map((i) => [i.type, i.i18nKey]));
  return (
    <div className="grid grid-cols-2 gap-2">
      {MACRO_GROUPS.map(({ key, i18nKey, Icon, types }) => {
        const first = types[0];
        return (
          <button
            key={key}
            onClick={() => onOpen({ type: first, title: t(`macro.${keyByType[first]}`) })}
            className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]"
          >
            <Icon className="w-4 h-4 text-[var(--accent-primary)]" /> {t(`macro.${i18nKey}`)}
          </button>
        );
      })}
    </div>
  );
}
