import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Download } from 'lucide-react';
import { getRiskReportList, getRiskReportUrl } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';

// Strategy tabs (same UX as PnlReportViewer):
//   mrpt = MRPT/MTFS pairs RiskManager reports (risk_report_YYYYMMDD_HHMMSS.pdf)
//   ssrs/aiss/aeus = portfolio_ledger risk reports (risk_report_YYYYMMDD.pdf, since 2026-07-02)
// Sources are mapped server-side in server/routes/riskReport.ts (legacy notes there).
type Mode = 'mrpt' | 'ssrs' | 'aiss' | 'aeus';

export default function RiskReportViewer({ params }: { params?: any }) {
  const { t } = useTranslation();
  const initial: Mode = params?.strategy === 'ssrs' ? 'ssrs' : params?.strategy === 'aiss' ? 'aiss' : params?.strategy === 'aeus' ? 'aeus' : 'mrpt';
  const [strategy, setStrategy] = useState<Mode>(initial);
  const [items, setItems] = useState<any[]>([]);
  const [selectedItem, setSelectedItem] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadList = () => {
    setLoading(true);
    setError(null);
    setSelectedItem('');
    getRiskReportList(strategy)
      .then(list => {
        setItems(list || []);
        if (list && list.length > 0) setSelectedItem(list[0].timestamp);
        setLoading(false);
      })
      .catch(err => {
        setError(err.message);
        setLoading(false);
      });
  };

  useEffect(loadList, [strategy]);

  const handleDownload = () => {
    if (!selectedItem) return;
    const a = document.createElement('a');
    a.href = getRiskReportUrl(selectedItem, strategy);
    a.download = `risk_report_${selectedItem}.pdf`;
    a.click();
  };

  // 20260602_200952 → 2026-06-02 20:09:52；20260701 → 2026-07-01
  const formatLabel = (ts: string) => {
    const m = ts?.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/);
    if (m) return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`;
    const d = ts?.match(/^(\d{4})(\d{2})(\d{2})$/);
    if (d) return `${d[1]}-${d[2]}-${d[3]}`;
    return ts || '—';
  };

  const Tabs = () => (
    <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
      {([['mrpt', 'MRPT/MTFS'], ['ssrs', 'SSRS'], ['aiss', 'AISS'], ['aeus', 'AEUS']] as [Mode, string][]).map(([m, label]) => (
        <button key={m} onClick={() => setStrategy(m)}
          className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === m ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>{label}</button>
      ))}
    </div>
  );

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={loadList} />;
  if (items.length === 0) return (
    <div className="flex flex-col h-full">
      <div className="flex justify-end mb-3 shrink-0"><Tabs /></div>
      <div className="text-sm text-[var(--text-muted)] p-4 text-center">{t('riskReport.noReports')}</div>
    </div>
  );

  const pdfUrl = selectedItem ? getRiskReportUrl(selectedItem, strategy) : '';

  return (
    <div className="flex flex-col h-full gap-3">
      {/* Toolbar: file selector + strategy tabs + download */}
      <div className="flex items-center justify-between shrink-0">
        <select
          value={selectedItem}
          onChange={e => setSelectedItem(e.target.value)}
          className="text-xs font-mono bg-[var(--bg-primary)] border border-[var(--border-subtle)] px-2 py-1.5 text-[var(--text-primary)] max-w-[450px]"
        >
          {items.map((f: any) => (
            <option key={f.timestamp} value={f.timestamp}>{formatLabel(f.timestamp)}</option>
          ))}
        </select>

        <div className="flex items-center gap-2">
          <Tabs />
          <button onClick={handleDownload} className="p-1.5 hover:bg-[var(--bg-tertiary)] transition-colors" title="Download">
            <Download className="w-4 h-4 text-[var(--text-muted)]" />
          </button>
        </div>
      </div>

      {/* PDF via iframe */}
      <div className="flex-1 border border-[var(--border-subtle)] rounded-xl overflow-hidden min-h-[400px]">
        <iframe
          key={`${strategy}-${selectedItem}`}
          src={pdfUrl}
          className="w-full h-full"
          style={{ border: 'none', minHeight: '100%' }}
          title={`${strategy.toUpperCase()} Risk Report`}
        />
      </div>
    </div>
  );
}
