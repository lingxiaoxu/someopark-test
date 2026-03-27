import React from 'react';
import { useTranslation } from 'react-i18next';
import { X, Maximize2, Minimize2, Download } from 'lucide-react';
import EquityChart from './artifacts/EquityChart';
import SignalTable from './artifacts/SignalTable';
import RegimeDashboard from './artifacts/RegimeDashboard';
import PairUniverseViewer from './artifacts/PairUniverseViewer';
import WFGridViewer from './artifacts/WFGridViewer';
import PortfolioHistoryViewer from './artifacts/PortfolioHistoryViewer';
import WFDiagnosticViewer from './artifacts/WFDiagnosticViewer';
import WalkForwardSummaryViewer from './artifacts/WalkForwardSummaryViewer';
import OOSPairSummaryViewer from './artifacts/OOSPairSummaryViewer';
import DailyReportViewer from './artifacts/DailyReportViewer';
import InventoryViewer from './artifacts/InventoryViewer';
import InventoryHistoryViewer from './artifacts/InventoryHistoryViewer';
import WFStructureViewer from './artifacts/WFStructureViewer';
import PnlReportViewer from './artifacts/PnlReportViewer';
import StrategyPerformanceViewer from './artifacts/StrategyPerformanceViewer';

// Artifact type → i18n title key mapping
const ARTIFACT_TITLE_KEYS: Record<string, string> = {
  chart: 'artifactTitles.equityCurve',
  table: 'artifactTitles.signals',
  dashboard: 'artifactTitles.regime',
  pair_universe: 'artifactTitles.pairUniverse',
  wf_grid: 'artifactTitles.wfGrid',
  portfolio_history: 'artifactTitles.portfolioHistory',
  wf_diagnostic: 'artifactTitles.wfDiagnostic',
  wf_summary: 'artifactTitles.wfSummary',
  oos_pair_summary: 'artifactTitles.oosPairSummary',
  daily_report: 'artifactTitles.dailyReport',
  inventory: 'artifactTitles.inventory',
  inventory_history: 'artifactTitles.inventoryHistory',
  wf_structure: 'artifactTitles.wfStructure',
  pnl_report: 'artifactTitles.pnlReport',
  strategy_performance: 'artifactTitles.strategyPerformance',
};

// Download URLs for artifact types that have downloadable files
function getDownloadUrl(artifact: any): string | null {
  const type = artifact.type;
  const strategy = artifact.params?.strategy || 'mrpt';
  switch (type) {
    case 'pnl_report': return '/api/pnl-report/latest';
    case 'wf_diagnostic': return '/api/diagnostic/latest/download';
    case 'chart': return `/api/wf/equity-curve/${strategy}?format=csv`;
    case 'oos_pair_summary': return `/api/wf/pair-summary/${strategy}?format=csv`;
    default: return null;
  }
}

export default function RightPanel({ artifact, onClose, onMaximize, isMaximized }: { artifact: any, onClose: () => void, onMaximize?: () => void, isMaximized?: boolean }) {
  const { t } = useTranslation();
  const params = artifact.params || {};

  const titleKey = ARTIFACT_TITLE_KEYS[artifact.type];
  const displayTitle = titleKey ? t(titleKey) : artifact.title;

  const handleDownload = () => {
    const url = getDownloadUrl(artifact);
    if (url) {
      const a = document.createElement('a');
      a.href = url;
      a.download = '';
      a.click();
    } else {
      // Download empty txt placeholder
      const blob = new Blob([''], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${artifact.type}.txt`;
      a.click();
      URL.revokeObjectURL(url);
    }
  };

  const MaxIcon = isMaximized ? Minimize2 : Maximize2;

  return (
    <div className="h-full flex flex-col relative" style={{ background: '#f4f4f4', borderLeft: '4px solid #111' }}>

      {/* Header — Stanse sticky header style */}
      <div
        className="h-14 flex items-center justify-between px-4 shrink-0"
        style={{ background: '#fff', borderBottom: '3px solid #111' }}
      >
        <div className="flex items-center gap-3 min-w-0">
          {/* Title */}
          <span className="truncate" style={{ fontFamily: 'var(--font-mono)', fontSize: '13px', fontWeight: 700, color: '#111', textTransform: 'uppercase', letterSpacing: '.04em' }}>
            {displayTitle}
          </span>
          {/* Type badge — Stanse sharp tag */}
          <span className="shrink-0" style={{
            padding: '2px 8px',
            fontFamily: 'var(--font-mono)',
            fontSize: '10px',
            fontWeight: 700,
            letterSpacing: '.08em',
            textTransform: 'uppercase',
            background: '#111',
            color: '#fff',
            border: '2px solid #111',
          }}>
            {artifact.type}
          </span>
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-1 shrink-0">
          {[
            { icon: Download, action: handleDownload },
            { icon: MaxIcon, action: onMaximize || (() => {}) },
            { icon: X, action: onClose },
          ].map(({ icon: Icon, action }, i) => (
            <button
              key={i}
              onClick={action}
              style={{
                padding: '5px',
                background: 'transparent',
                border: '2px solid transparent',
                cursor: 'pointer',
                transition: 'all .1s',
                color: '#555',
              }}
              onMouseEnter={e => {
                const el = e.currentTarget as HTMLElement
                el.style.background = '#111'
                el.style.borderColor = '#111'
                el.style.color = '#fff'
              }}
              onMouseLeave={e => {
                const el = e.currentTarget as HTMLElement
                el.style.background = 'transparent'
                el.style.borderColor = 'transparent'
                el.style.color = '#555'
              }}
            >
              <Icon className="w-4 h-4" />
            </button>
          ))}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4">
        {artifact.type === 'chart'             && <EquityChart params={params} />}
        {artifact.type === 'table'             && <SignalTable params={params} />}
        {artifact.type === 'dashboard'         && <RegimeDashboard />}
        {artifact.type === 'pair_universe'     && <PairUniverseViewer params={params} />}
        {artifact.type === 'wf_grid'           && <WFGridViewer params={params} />}
        {artifact.type === 'portfolio_history' && <PortfolioHistoryViewer params={params} />}
        {artifact.type === 'wf_diagnostic'     && <WFDiagnosticViewer />}
        {artifact.type === 'wf_summary'        && <WalkForwardSummaryViewer params={params} />}
        {artifact.type === 'oos_pair_summary'  && <OOSPairSummaryViewer params={params} />}
        {artifact.type === 'daily_report'      && <DailyReportViewer />}
        {artifact.type === 'inventory'         && <InventoryViewer params={params} />}
        {artifact.type === 'inventory_history' && <InventoryHistoryViewer params={params} />}
        {artifact.type === 'wf_structure'      && <WFStructureViewer data={artifact.data} />}
        {artifact.type === 'pnl_report'        && <PnlReportViewer />}
        {artifact.type === 'strategy_performance' && <StrategyPerformanceViewer />}
      </div>
    </div>
  );
}
