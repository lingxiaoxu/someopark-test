import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Download } from 'lucide-react';
import { getRiskReportList, getRiskReportUrl } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';

export default function RiskReportViewer({ params: _params }: { params?: any }) {
  const { t } = useTranslation();
  const [items, setItems] = useState<any[]>([]);
  const [selectedItem, setSelectedItem] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadList = () => {
    setLoading(true);
    setError(null);
    getRiskReportList()
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

  useEffect(loadList, []);

  const handleDownload = () => {
    if (!selectedItem) return;
    const a = document.createElement('a');
    a.href = getRiskReportUrl(selectedItem);
    a.download = `risk_report_${selectedItem}.pdf`;
    a.click();
  };

  // 20260602_200952 → 2026-06-02 20:09:52
  const formatLabel = (ts: string) => {
    const m = ts?.match(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
    if (m) return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`;
    return ts || '—';
  };

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={loadList} />;
  if (items.length === 0) return (
    <div className="text-sm text-[var(--text-muted)] p-4 text-center">{t('riskReport.noReports')}</div>
  );

  const pdfUrl = selectedItem ? getRiskReportUrl(selectedItem) : '';

  return (
    <div className="flex flex-col h-full gap-3">
      {/* Toolbar: file selector + download */}
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
          <button onClick={handleDownload} className="p-1.5 hover:bg-[var(--bg-tertiary)] transition-colors" title="Download">
            <Download className="w-4 h-4 text-[var(--text-muted)]" />
          </button>
        </div>
      </div>

      {/* PDF via iframe */}
      <div className="flex-1 border border-[var(--border-subtle)] rounded-xl overflow-hidden min-h-[400px]">
        <iframe
          key={selectedItem}
          src={pdfUrl}
          className="w-full h-full"
          style={{ border: 'none', minHeight: '100%' }}
          title="Risk Report"
        />
      </div>
    </div>
  );
}
