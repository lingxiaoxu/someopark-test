import React from 'react';
import { X, Maximize2, Download } from 'lucide-react';
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

export default function RightPanel({ artifact, onClose }: { artifact: any, onClose: () => void }) {
  const params = artifact.params || {};
  return (
    <div className="h-full flex flex-col bg-[var(--bg-secondary)] relative border-l border-[var(--border-subtle)]">
      <div className="h-14 border-b border-[var(--border-subtle)] flex items-center justify-between px-4 shrink-0 bg-[var(--bg-primary)]">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium truncate max-w-[300px]">{artifact.title}</span>
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] text-[var(--text-muted)] border border-[var(--border-subtle)] uppercase tracking-wider shrink-0">
            {artifact.type}
          </span>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button className="p-1.5 text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] rounded-md transition-colors">
            <Download className="w-4 h-4" />
          </button>
          <button className="p-1.5 text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] rounded-md transition-colors">
            <Maximize2 className="w-4 h-4" />
          </button>
          <button onClick={onClose} className="p-1.5 text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] rounded-md transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-4">
        {artifact.type === 'chart' && <EquityChart params={params} />}
        {artifact.type === 'table' && <SignalTable params={params} />}
        {artifact.type === 'dashboard' && <RegimeDashboard />}
        {artifact.type === 'pair_universe' && <PairUniverseViewer params={params} />}
        {artifact.type === 'wf_grid' && <WFGridViewer params={params} />}
        {artifact.type === 'portfolio_history' && <PortfolioHistoryViewer params={params} />}
        {artifact.type === 'wf_diagnostic' && <WFDiagnosticViewer />}
        {artifact.type === 'wf_summary' && <WalkForwardSummaryViewer params={params} />}
        {artifact.type === 'oos_pair_summary' && <OOSPairSummaryViewer params={params} />}
        {artifact.type === 'daily_report' && <DailyReportViewer />}
        {artifact.type === 'inventory' && <InventoryViewer params={params} />}
        {artifact.type === 'inventory_history' && <InventoryHistoryViewer params={params} />}
        {artifact.type === 'wf_structure' && <WFStructureViewer data={artifact.data} />}
      </div>
    </div>
  );
}
