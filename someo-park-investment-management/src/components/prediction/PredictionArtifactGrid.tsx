/**
 * PredictionArtifactGrid — the middle artifact grid shown in Prediction Market mode.
 * Mirrors the stock grid's layout/classes (so the CSS inversion applies identically),
 * but every button opens a World Cup `wc_*` artifact instead of a stock one.
 * Rendered ONLY when appMode === 'prediction'; the stock grid is untouched.
 */
import {
  Trophy, Activity, Target, GitCompare, Zap, CalendarClock, Clock,
  LineChart, ShieldAlert, Gauge, LayoutGrid, Landmark, FileText, Cpu,
  Sparkles, Download,
} from 'lucide-react';
import type { ComponentType } from 'react';

type Artifact = { type: string; title: string };
type Item = { type: string; title: string; Icon: ComponentType<{ className?: string }> };

const ITEMS: Item[] = [
  { type: 'wc_champion',      title: 'Champion Odds',          Icon: Trophy },
  { type: 'wc_match_pricing', title: 'Match Pricing (3-way)',  Icon: Activity },
  { type: 'wc_golden_boot',   title: 'Golden Boot',            Icon: Target },
  { type: 'wc_divergence',    title: 'Model vs Market',        Icon: GitCompare },
  { type: 'wc_inplay',        title: 'In-Play Arbitrage',      Icon: Zap },
  { type: 'wc_predictions',   title: "Today's Predictions",    Icon: CalendarClock },
  { type: 'wc_schedule',      title: 'Schedule (ET / PT)',     Icon: Clock },
  { type: 'wc_performance',   title: 'Accuracy & P&L',         Icon: LineChart },
  { type: 'wc_risk',          title: 'Risk Report',            Icon: ShieldAlert },
  { type: 'wc_calibration',   title: 'Calibration (OOS)',      Icon: Gauge },
  { type: 'wc_overview',      title: 'System Overview',        Icon: LayoutGrid },
  { type: 'wc_venues',        title: 'Venues & Gates',         Icon: Landmark },
  { type: 'wc_methodology',   title: 'Model Notes',            Icon: FileText },
  { type: 'wc_budget',        title: 'API Budget / Health',    Icon: Cpu },
  { type: 'wc_value',         title: 'Value & How to See',     Icon: Sparkles },
  { type: 'wc_pdfs',          title: 'Download Reports (PDF)', Icon: Download },
];

export default function PredictionArtifactGrid({ onOpen }: { onOpen: (a: Artifact) => void }) {
  return (
    <div className="grid grid-cols-2 gap-2">
      {ITEMS.map(({ type, title, Icon }) => (
        <button
          key={type}
          onClick={() => onOpen({ type, title })}
          className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]"
        >
          <Icon className="w-4 h-4 text-[var(--accent-primary)]" /> {title}
        </button>
      ))}
    </div>
  );
}
