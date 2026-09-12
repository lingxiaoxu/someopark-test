import { useTranslation } from 'react-i18next';
import { usePoll } from '../prediction/usePoll';
import { getSoccerOverview } from '../../lib/soccerApi';
import { fmtDateTime } from './soccerLabels';

/** Soccer-only status. A browser fetch never changes the source's timestamp. */
export default function SoccerDataStatus({ data, error, source, labelKey, fixtureIds, maxAgeSeconds = 30 * 3600 }: {
  data?: any; error?: unknown; source?: string; labelKey?: string; fixtureIds?: Array<number | string>; maxAgeSeconds?: number;
}) {
  const { t, i18n } = useTranslation();
  const stamp = data?.source_as_of || data?.as_of || data?.ts || data?.meta?.run_ts;
  const generatedAt = data?.as_of || data?.ts || data?.meta?.run_ts || stamp;
  const parsed = generatedAt ? Date.parse(generatedAt) : NaN;
  const operationsOnly = source === 'operations';
  const stale = !operationsOnly && Number.isFinite(parsed) && Date.now() - parsed > maxAgeSeconds * 1000;
  const jobs: any[] = data?.operations?.jobs ?? [];
  const updating = jobs.some(j => j.state === 'running');
  const failed = jobs.filter(j => ['failed', 'degraded', 'unavailable'].includes(j.state));
  const staleSources = Object.entries(operationsOnly ? {} : data?.sources ?? {}).filter(([, source]: any) => {
    const cutoff = source.generated_at || source.as_of;
    return cutoff && Date.now() - Date.parse(cutoff) > source.max_age_seconds * 1000;
  });
  const allIssues: any[] = operationsOnly ? [] : data?.data_status?.issues ?? [];
  const visibleIds = fixtureIds == null ? null : new Set(fixtureIds.map(String));
  // A six-match preview must not inherit fixture warnings from the rest of the
  // calendar. Issues without a fixture, failed jobs and stale snapshots stay global.
  const issues = allIssues.filter(issue => issue.fixture_id == null || visibleIds == null || visibleIds.has(String(issue.fixture_id)));
  const unavailable = data?.data_status?.state === 'unavailable';
  const unexplainedDegradation = data?.data_status?.state === 'degraded' && !allIssues.length;
  const broaderFailure = error || ['degraded', 'unavailable'].includes(data?.operations?.state) || failed.length
    || (!operationsOnly && (unavailable || unexplainedDegradation || stale || staleSources.length || data?.scan_error));
  const degraded = broaderFailure || issues.length > 0;
  const quotesOnly = !broaderFailure && issues.length > 0 && issues.every(issue => issue.code === 'quote_unavailable');
  const venueNames: Record<string, string> = { kalshi: 'Kalshi', poly_us: 'Polymarket US', poly_global: 'Polymarket' };
  const venues = [...new Set(issues.map(issue => venueNames[issue.venue]).filter(Boolean))];
  if (!data && !error) return null;
  if (source === 'quotes' && !degraded) return null;
  const label = source || labelKey ? t(labelKey || `soccer.dataHealth.sources.${source}`, { defaultValue: source || '' }) : '';
  const quoteVerdict = `${t('soccer.dataHealth.reasons.quote_unavailable')}${venues.length ? ` (${venues.join(' / ')})` : ''}`;
  const verdict = error ? t('soccer.dataHealth.loadFailed') : quotesOnly ? quoteVerdict : degraded ? t('soccer.dataHealth.degraded') : updating ? t('soccer.dataHealth.updating') : '';
  const headline = [label, verdict].filter(Boolean).join(' · ');
  return <div role={degraded ? 'status' : undefined} style={{ fontSize: 10, fontFamily: 'var(--font-mono)',
    color: degraded ? 'var(--warning, #a86b00)' : 'var(--text-muted)', marginBottom: 8, lineHeight: 1.6 }}>
    {headline}
    {stamp && <>{headline ? ' · ' : ''}{t('soccer.dataHealth.sourceAt')} {fmtDateTime(stamp, i18n.language)}</>}
    {(stale || !!staleSources.length) && <> · {t('soccer.dataHealth.stale')}</>}
    {!!failed.length && <div>{t('soccer.dataHealth.failedJobs')}: {failed.map(j => `${t(`soccer.dataHealth.jobs.${j.name}`, { defaultValue: j.name })} (${j.last_success_at ? `${t('soccer.dataHealth.lastSuccess')} ${fmtDateTime(j.last_success_at, i18n.language)}` : t('soccer.dataHealth.noSuccess')})`).join(' · ')}</div>}
    {!!staleSources.length && <div>{staleSources.map(([key, source]: any) => `${t(`soccer.dataHealth.sources.${key}`, { defaultValue: key })}: ${fmtDateTime(source.generated_at || source.as_of, i18n.language)}`).join(' · ')}</div>}
    {!!issues.length && !quotesOnly && <div>{[...new Set(issues.map(issue => [venueNames[issue.venue], t(`soccer.dataHealth.reasons.${issue.code}`, { defaultValue: t('soccer.dataHealth.unavailable') })].filter(Boolean).join(': ')))].join(' · ')}</div>}
  </div>;
}

export function SoccerHealth() {
  const status = usePoll<any>(() => getSoccerOverview(), 30000);
  const timestamp = status.data?.operations?.as_of || status.data?.as_of;
  const data = status.data ? { operations: status.data.operations, as_of: timestamp, source_as_of: timestamp } : undefined;
  return <SoccerDataStatus source="operations" data={data} error={status.error} />;
}
