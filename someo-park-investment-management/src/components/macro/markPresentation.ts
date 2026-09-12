import type { MacroMarkStatus, MacroValuation } from './macroApi';

/** Export/mark timestamps are bookkeeping clocks. Only the book timestamp ages a price. */
export function markDisplayStatus(value?: MacroValuation, nowMs = Date.now()): MacroMarkStatus {
  if (!value?.mark_status) return 'unverified';
  if (value.mark_status !== 'marked') return value.mark_status;
  const quoteMs = Date.parse(value.quote_ts ?? '');
  if (!Number.isFinite(quoteMs) || !Number.isFinite(value.quote_max_age_seconds)) return 'unverified';
  const age = (nowMs - quoteMs) / 1000;
  return age < 0 || age > value.quote_max_age_seconds! ? 'stale' : 'marked';
}
