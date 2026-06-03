import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Download } from 'lucide-react';
import { getPnlReportList, getPnlReportUrl, API_BASE, apiHeaders } from '../../lib/api';
import LoadingState from '../LoadingState';
import ErrorState from '../ErrorState';

// 'mrpt' = MRPT/MTFS PnL PDFs; 'ssrs'/'aiss' = qlib tearsheet PDFs (selected by filename)
type Mode = 'mrpt' | 'ssrs' | 'aiss';
const isQlib = (m: Mode) => m === 'ssrs' || m === 'aiss';

export default function PnlReportViewer({ params }: { params?: any }) {
  const { t } = useTranslation();
  const initial: Mode = params?.strategy === 'ssrs' ? 'ssrs' : params?.strategy === 'aiss' ? 'aiss' : 'mrpt';
  const [strategy, setStrategy] = useState<Mode>(initial);
  const [dates, setDates] = useState<any[]>([]);
  const [selectedItem, setSelectedItem] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    setSelectedItem('');
    getPnlReportList(isQlib(strategy) ? strategy : undefined)
      .then(list => {
        setDates(list || []);
        if (list && list.length > 0) {
          setSelectedItem(isQlib(strategy) ? list[0].filename : list[0].date);
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
    const url = isQlib(strategy)
      ? `${API_BASE}/api/${strategy}/tearsheet/${selectedItem}${apiHeaders()['x-api-key'] ? '?key=' + apiHeaders()['x-api-key'] : ''}`
      : getPnlReportUrl(selectedItem);
    const a = document.createElement('a');
    a.href = url;
    a.download = isQlib(strategy) ? selectedItem : `pnl_report_${selectedItem}.pdf`;
    a.click();
  };

  // tearsheet_<param>_<v1|v2>_<IS|IS-OOS>_<ts>.pdf → readable label
  const formatTearsheetLabel = (f: any) => {
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

  const Tabs = () => (
    <div className="flex bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5">
      {([['mrpt', 'MRPT/MTFS'], ['ssrs', 'SSRS'], ['aiss', 'AISS']] as [Mode, string][]).map(([m, label]) => (
        <button key={m} onClick={() => setStrategy(m)}
          className={`px-2.5 py-1 text-xs rounded-sm transition-colors ${strategy === m ? 'bg-[var(--accent-primary)] text-white' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'}`}>{label}</button>
      ))}
    </div>
  );

  if (loading) return <LoadingState />;
  if (error) return <ErrorState message={error} onRetry={() => { setError(null); setLoading(true); getPnlReportList(isQlib(strategy) ? strategy : undefined).then(l => { setDates(l || []); setLoading(false); }).catch(e => { setError(e.message); setLoading(false); }); }} />;
  if (dates.length === 0) return (
    <div className="flex flex-col h-full">
      <div className="flex justify-end mb-3 shrink-0"><Tabs /></div>
      <div className="text-sm text-[var(--text-muted)] p-4 text-center">{t('pnlReport.noReports')}</div>
    </div>
  );

  const pdfUrl = selectedItem
    ? (isQlib(strategy)
      ? `${API_BASE}/api/${strategy}/tearsheet/${encodeURIComponent(selectedItem)}${apiHeaders()['x-api-key'] ? '?key=' + apiHeaders()['x-api-key'] : ''}`
      : getPnlReportUrl(selectedItem))
    : '';

  return (
    <div className="flex flex-col h-full gap-3">
      <div className="flex items-center justify-between shrink-0">
        <select
          value={selectedItem}
          onChange={e => setSelectedItem(e.target.value)}
          className="text-xs font-mono bg-[var(--bg-primary)] border border-[var(--border-subtle)] px-2 py-1.5 text-[var(--text-primary)] max-w-[450px]"
        >
          {isQlib(strategy) ? (
            dates.map((f: any) => (
              <option key={f.filename} value={f.filename}>{formatTearsheetLabel(f)}</option>
            ))
          ) : (
            dates.map((d: any) => (
              <option key={d.date} value={d.date}>{formatDateLabel(d.date)}</option>
            ))
          )}
        </select>

        <div className="flex items-center gap-2">
          <Tabs />
          <button onClick={handleDownload} className="p-1.5 hover:bg-[var(--bg-tertiary)] transition-colors" title="Download">
            <Download className="w-4 h-4 text-[var(--text-muted)]" />
          </button>
        </div>
      </div>

      <div className="flex-1 border border-[var(--border-subtle)] rounded-xl overflow-hidden min-h-[400px]">
        <iframe
          key={selectedItem}
          src={pdfUrl}
          className="w-full h-full"
          style={{ border: 'none', minHeight: '100%' }}
          title={isQlib(strategy) ? `${strategy.toUpperCase()} Tearsheet` : 'PnL Report'}
        />
      </div>
    </div>
  );
}
