import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Download } from 'lucide-react';
import { getPnlReportList, getPnlReportUrl } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';

export default function PnlReportViewer() {
  const { t } = useTranslation();
  const [dates, setDates] = useState<{ date: string; filename: string }[]>([]);
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getPnlReportList()
      .then(list => {
        setDates(list);
        if (list.length > 0) setSelectedDate(list[0].date);
        setLoading(false);
      })
      .catch(err => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  const handleDownload = () => {
    if (!selectedDate) return;
    const url = getPnlReportUrl(selectedDate);
    const a = document.createElement('a');
    a.href = url;
    a.download = `pnl_report_${selectedDate}.pdf`;
    a.click();
  };

  const formatDateLabel = (d: string) => {
    if (d.length === 8) return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
    return d;
  };

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={() => window.location.reload()} />;
  if (dates.length === 0) return <div className="text-sm text-[var(--text-muted)] p-4">{t('pnlReport.noReports')}</div>;

  const pdfUrl = selectedDate ? getPnlReportUrl(selectedDate) : '';

  return (
    <div className="flex flex-col h-full gap-3">
      {/* Toolbar */}
      <div className="flex items-center justify-between shrink-0">
        <select
          value={selectedDate || ''}
          onChange={e => setSelectedDate(e.target.value)}
          className="text-xs font-mono bg-[var(--bg-primary)] border border-[var(--border-subtle)] px-2 py-1.5 text-[var(--text-primary)]"
        >
          {dates.map(d => (
            <option key={d.date} value={d.date}>{formatDateLabel(d.date)}</option>
          ))}
        </select>

        <button onClick={handleDownload} className="p-1.5 hover:bg-[var(--bg-tertiary)] transition-colors" title="Download">
          <Download className="w-4 h-4 text-[var(--text-muted)]" />
        </button>
      </div>

      {/* PDF via iframe — browser built-in PDF viewer with zoom, scroll, page nav */}
      <div className="flex-1 border border-[var(--border-subtle)] rounded-xl overflow-hidden min-h-[400px]">
        <iframe
          key={selectedDate}
          src={pdfUrl}
          className="w-full h-full"
          style={{ border: 'none', minHeight: '100%' }}
          title="PnL Report"
        />
      </div>
    </div>
  );
}
