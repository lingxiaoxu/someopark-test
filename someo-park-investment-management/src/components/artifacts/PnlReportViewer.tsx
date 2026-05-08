import React, { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Download } from 'lucide-react';
import { getPnlReportList, getPnlReportUrl, API_BASE, apiHeaders } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';

export default function PnlReportViewer({ params }: { params?: any }) {
  const { t } = useTranslation();
  const [strategy, setStrategy] = useState(params?.strategy === 'sr' ? 'sr' : 'mrpt');
  const [dates, setDates] = useState<any[]>([]);
  const [selectedItem, setSelectedItem] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Fetch list when strategy changes
  useEffect(() => {
    setLoading(true);
    setError(null);
    setSelectedItem('');
    getPnlReportList(strategy === 'sr' ? 'sr' : undefined)
      .then(list => {
        setDates(list || []);
        if (list && list.length > 0) {
          // MRPT/MTFS: select by date; SR: select by filename
          setSelectedItem(strategy === 'sr' ? list[0].filename : list[0].date);
        }
        setLoading(false);
      })
      .catch(err => {
        setError(err.message);
        setLoading(false);
      });
  }, [strategy]);

  const handleDownload = () => {
    if (!selectedItem) return;
    const url = strategy === 'sr'
      ? `${API_BASE}/api/sr/tearsheet/${selectedItem}${apiHeaders()['x-api-key'] ? '?key=' + apiHeaders()['x-api-key'] : ''}`
      : getPnlReportUrl(selectedItem);
    const a = document.createElement('a');
    a.href = url;
    a.download = strategy === 'sr' ? selectedItem : `pnl_report_${selectedItem}.pdf`;
    a.click();
  };

  // Format SR filename for display: tearsheet_stagflation_v1_IS-OOS_20260508_002106.pdf
  const formatSRLabel = (f: any) => {
    const match = f.filename?.match(/tearsheet_(.+)_(v[12])_(IS(?:-OOS)?)_(\d{8})_(\d{6})\.pdf/);
    if (match) {
      return `${match[1]} [${match[2]} ${match[3]}] ${match[4].slice(0,4)}-${match[4].slice(4,6)}-${match[4].slice(6,8)}`;
    }
    return f.filename || f.timestamp || '—';
  };

  const formatDateLabel = (d: string) => {
    if (!d) return '—';
    if (d.length === 8) return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;
    return d;
  };

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={() => { setError(null); setLoading(true); getPnlReportList(strategy === 'sr' ? 'sr' : undefined).then(l => { setDates(l || []); setLoading(false); }).catch(e => { setError(e.message); setLoading(false); }); }} />;
  if (dates.length === 0) return (
    <div className="flex flex-col h-full">
      <div className="flex justify-end mb-3 shrink-0">
        <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
          <button onClick={() => setStrategy('mrpt')} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy !== 'sr' ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)]'}`}>MRPT/MTFS</button>
          <button onClick={() => setStrategy('sr')} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === 'sr' ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)]'}`}>SR</button>
        </div>
      </div>
      <div className="text-sm text-[var(--text-muted)] p-4 text-center">{t('pnlReport.noReports')}</div>
    </div>
  );

  // Build PDF URL
  const pdfUrl = selectedItem
    ? (strategy === 'sr'
      ? `${API_BASE}/api/sr/tearsheet/${encodeURIComponent(selectedItem)}${apiHeaders()['x-api-key'] ? '?key=' + apiHeaders()['x-api-key'] : ''}`
      : getPnlReportUrl(selectedItem))
    : '';

  return (
    <div className="flex flex-col h-full gap-3">
      {/* Toolbar: strategy switcher + file selector + download */}
      <div className="flex items-center justify-between shrink-0">
        <select
          value={selectedItem}
          onChange={e => setSelectedItem(e.target.value)}
          className="text-xs font-mono bg-[var(--bg-primary)] border border-[var(--border-subtle)] px-2 py-1.5 text-[var(--text-primary)] max-w-[350px] truncate"
        >
          {strategy === 'sr' ? (
            dates.map((f: any) => (
              <option key={f.filename} value={f.filename}>{formatSRLabel(f)}</option>
            ))
          ) : (
            dates.map((d: any) => (
              <option key={d.date} value={d.date}>{formatDateLabel(d.date)}</option>
            ))
          )}
        </select>

        <div className="flex items-center gap-2">
          <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
            <button onClick={() => setStrategy('mrpt')} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy !== 'sr' ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>MRPT/MTFS</button>
            <button onClick={() => setStrategy('sr')} className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === 'sr' ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>SR</button>
          </div>
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
          title={strategy === 'sr' ? 'SR Tearsheet' : 'PnL Report'}
        />
      </div>
    </div>
  );
}
