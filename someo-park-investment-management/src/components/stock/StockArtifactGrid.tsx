/**
 * StockArtifactGrid — the middle artifact grid shown in AI Quant Strategy mode
 * (extracted from the inline JSX previously in ChatArea.tsx). Mirrors
 * MacroArtifactGrid/PredictionArtifactGrid's layout/classes and categorized/flat
 * toggle. Every button's `title`/`params` payload is preserved byte-for-byte from
 * the original inline version — only the label text is i18n (chat.btnXxx), same
 * as before extraction.
 */
import { Activity, LayoutDashboard, Layers, FlaskConical, LineChart } from 'lucide-react'
import type { ComponentType } from 'react'
import { useTranslation } from 'react-i18next'

type Artifact = { type: string; title: string; params?: any }
type Item = { type: string; title: string; i18nKey: string; needsStrategy?: boolean }

// i18nKey resolves to chat.<key>; `title` is the literal (untranslated) artifact
// title passed through to the panel — preserved verbatim from the pre-extraction
// inline buttons in ChatArea.tsx.
export const STOCK_ITEMS: Item[] = [
  { type: 'pair_universe',        title: 'Pair Universe',         i18nKey: 'btnPairUniverse',        needsStrategy: true },
  { type: 'table',                title: 'Trading Signals',       i18nKey: 'btnSignals',              needsStrategy: true },
  { type: 'inventory',            title: 'Current Inventory',     i18nKey: 'btnCurrentInventory',     needsStrategy: true },
  { type: 'inventory_history',    title: 'Inventory History',     i18nKey: 'btnInventoryHistory',     needsStrategy: true },
  { type: 'daily_report',         title: 'Daily Report',          i18nKey: 'btnDailyReport' },
  { type: 'dashboard',            title: 'Macro Regime Status',   i18nKey: 'btnRegime' },
  { type: 'chart',                title: 'OOS Equity Curve',      i18nKey: 'btnOosEquity',            needsStrategy: true },
  { type: 'oos_pair_summary',     title: 'OOS Pair Summary',      i18nKey: 'btnOosPairSummary',       needsStrategy: true },
  { type: 'wf_grid',              title: 'Walk-Forward Grid',     i18nKey: 'btnDsrGrid',              needsStrategy: true },
  { type: 'wf_summary',           title: 'Walk-Forward Summary',  i18nKey: 'btnWfSummary',            needsStrategy: true },
  { type: 'wf_diagnostic',        title: 'WF Diagnostic Report',  i18nKey: 'btnWfDiagnostic' },
  { type: 'portfolio_history',    title: 'Portfolio History',     i18nKey: 'btnMonitorHistory',       needsStrategy: true },
  { type: 'wf_structure',         title: 'WF File Structure',     i18nKey: 'btnWfStructure' },
  { type: 'pnl_report',           title: 'PnL Report',            i18nKey: 'btnPnlReport' },
  { type: 'risk_report',          title: 'Risk Report',           i18nKey: 'btnRiskReport' },
  { type: 'strategy_performance', title: 'Strategy Performance',  i18nKey: 'btnStrategyPerformance' },
  { type: 'realtime_nav',         title: 'Realtime NAV',          i18nKey: 'btnRealtimeNav' },
]

type Group = {
  key: string; i18nKey: string;
  Icon: ComponentType<{ className?: string }>;
  types: string[];
}

export const STOCK_GROUPS: Group[] = [
  { key: 'overview',   i18nKey: 'groupOverview',   Icon: LayoutDashboard,
    types: ['daily_report', 'dashboard'] },
  { key: 'positions',  i18nKey: 'groupPositions',  Icon: Layers,
    types: ['pair_universe', 'table', 'inventory', 'inventory_history'] },
  { key: 'research',   i18nKey: 'groupResearch',   Icon: FlaskConical,
    types: ['chart', 'oos_pair_summary', 'wf_grid', 'wf_summary', 'wf_diagnostic', 'wf_structure'] },
  { key: 'performance', i18nKey: 'groupPerformance', Icon: LineChart,
    types: ['portfolio_history', 'pnl_report', 'risk_report', 'strategy_performance', 'realtime_nav'] },
]

export default function StockArtifactGrid({
  onOpen,
  strategy,
  categorized = false,
}: {
  onOpen: (a: Artifact) => void
  strategy: string
  categorized?: boolean
}) {
  const { t } = useTranslation()
  const itemByType: Record<string, Item> = Object.fromEntries(STOCK_ITEMS.map((i) => [i.type, i]))

  const open = (it: Item) => onOpen({
    type: it.type,
    title: it.title,
    ...(it.needsStrategy ? { params: { strategy } } : {}),
  })

  if (categorized) {
    return (
      <div className="flex flex-col gap-3">
        {STOCK_GROUPS.map(({ key, i18nKey, Icon: GIcon, types }) => (
          <div key={key}>
            <div className="flex items-center gap-1.5 mb-1.5 text-[10px] font-bold uppercase tracking-wider text-[var(--text-muted)]">
              <GIcon className="w-3 h-3" /> {t(`chat.${i18nKey}`)}
            </div>
            <div className="grid grid-cols-2 gap-2">
              {types.map((type) => {
                const it = itemByType[type]
                if (!it) return null
                return (
                  <button
                    key={type}
                    onClick={() => open(it)}
                    className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]"
                  >
                    <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t(`chat.${it.i18nKey}`)}
                  </button>
                )
              })}
            </div>
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="grid grid-cols-2 gap-2">
      {STOCK_ITEMS.map((it) => (
        <button
          key={it.type}
          onClick={() => open(it)}
          className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]"
        >
          <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t(`chat.${it.i18nKey}`)}
        </button>
      ))}
    </div>
  )
}
