/** Daily-return statistics for two equity series in an already selected date window. */
export interface PerformanceEquityRow {
  date: string;
  [key: string]: unknown;
}

export interface PerformancePairAnalysis {
  keyA: string;
  keyB: string;
  /** Valid paired intervals, including intervals with a zero return. */
  sampleCount: number;
  /** Dates on which the first/last valid return interval ends. */
  sampleStartDate: string | null;
  sampleEndDate: string | null;
  firstIntervalStartDate: string | null;
  correlation: number | null;
  /** Sample covariance in decimal-return squared units; multiply by 10,000 for %². */
  covariance: number | null;
  /** A relative to B: sample covariance(A, B) / sample variance(B). */
  beta: number | null;
  status: 'ok' | 'insufficient_samples' | 'zero_variance' | 'invalid_dates'
    | 'non_finite_statistics';
}

function isDate(date: unknown): date is string {
  if (typeof date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return false;
  const stamp = Date.parse(`${date}T00:00:00Z`);
  return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === date;
}

function isEquity(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0;
}

/**
 * Both returns must use the same two adjacent source records. Invalid equity
 * blocks both adjoining intervals; it is never filled or removed before return
 * calculation. Adjacent records may straddle a weekend/holiday: this function
 * does not invent a trading calendar or data outside the selected window.
 *
 * Dates are sorted without mutating the caller. Duplicate/invalid dates reject
 * the window rather than arbitrarily choosing one of two conflicting records.
 */
export function analyzePerformancePair(
  rows: readonly PerformanceEquityRow[], keyA: string, keyB: string,
): PerformancePairAnalysis {
  const result: PerformancePairAnalysis = {
    keyA, keyB, sampleCount: 0, sampleStartDate: null, sampleEndDate: null,
    firstIntervalStartDate: null, correlation: null, covariance: null, beta: null,
    status: 'insufficient_samples',
  };
  const dates = rows.map(row => row.date);
  if (dates.some(date => !isDate(date)) || new Set(dates).size !== dates.length) {
    return { ...result, status: 'invalid_dates' };
  }
  const ordered = [...rows].sort((a, b) => a.date.localeCompare(b.date));
  const fieldA = `${keyA}_equity`;
  const fieldB = `${keyB}_equity`;
  let meanA = 0, meanB = 0, m2A = 0, m2B = 0, cross = 0;
  let maxAbsA = 0, maxAbsB = 0;

  for (let i = 1; i < ordered.length; i += 1) {
    const previous = ordered[i - 1];
    const current = ordered[i];
    const a0 = previous[fieldA], a1 = current[fieldA];
    const b0 = previous[fieldB], b1 = current[fieldB];
    if (!isEquity(a0) || !isEquity(a1) || !isEquity(b0) || !isEquity(b1)) continue;
    const a = a1 / a0 - 1;
    const b = b1 / b0 - 1;
    if (!Number.isFinite(a) || !Number.isFinite(b)) continue;

    result.sampleCount += 1;
    if (result.sampleStartDate === null) {
      result.sampleStartDate = current.date;
      result.firstIntervalStartDate = previous.date;
    }
    result.sampleEndDate = current.date;
    maxAbsA = Math.max(maxAbsA, Math.abs(a));
    maxAbsB = Math.max(maxAbsB, Math.abs(b));
    // Online centered moments avoid subtracting two nearly equal raw sums.
    const deltaA = a - meanA;
    const deltaB = b - meanB;
    meanA += deltaA / result.sampleCount;
    meanB += deltaB / result.sampleCount;
    m2A += deltaA * (a - meanA);
    m2B += deltaB * (b - meanB);
    cross += deltaA * (b - meanB);
  }

  const n = result.sampleCount;
  if (n < 2) return result;
  if (![meanA, meanB, m2A, m2B, cross].every(Number.isFinite)) {
    return { ...result, status: 'non_finite_statistics' };
  }
  const varianceA = Math.max(0, m2A / (n - 1));
  const varianceB = Math.max(0, m2B / (n - 1));
  // Equity ratios representing a constant nonzero return can differ by a few
  // floating-point ulps. Treat only that precision floor as zero volatility.
  const zeroA = Math.sqrt(varianceA) <= 8 * Number.EPSILON * Math.max(1, maxAbsA);
  const zeroB = Math.sqrt(varianceB) <= 8 * Number.EPSILON * Math.max(1, maxAbsB);
  result.covariance = zeroA || zeroB ? 0 : cross / (n - 1);
  if (n < 3) return result;
  if (zeroA || zeroB) return { ...result, status: 'zero_variance' };

  const rho = (cross / Math.sqrt(m2A)) / Math.sqrt(m2B);
  const beta = cross / m2B;
  if (![result.covariance, rho, beta].every(Number.isFinite)) {
    return { ...result, covariance: null, status: 'non_finite_statistics' };
  }
  return { ...result, correlation: Math.max(-1, Math.min(1, rho)), beta, status: 'ok' };
}
