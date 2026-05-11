/**
 * IRR solver using Newton-Raphson with bisection fallback.
 * Mirrors Excel's IRR function behavior.
 */

const MAX_ITERATIONS = 200
const TOLERANCE = 1e-10

/**
 * Calculate Net Present Value of a series of cashflows at the given rate.
 * cashflows[0] is at t=0, cashflows[1] at t=1, etc.
 */
export function calcNPV(rate: number, cashflows: number[]): number {
  let npv = 0
  for (let i = 0; i < cashflows.length; i++) {
    npv += cashflows[i] / Math.pow(1 + rate, i)
  }
  return npv
}

/**
 * Derivative of NPV with respect to rate (for Newton-Raphson).
 */
function calcNPVDerivative(rate: number, cashflows: number[]): number {
  let d = 0
  for (let i = 1; i < cashflows.length; i++) {
    d += -i * cashflows[i] / Math.pow(1 + rate, i + 1)
  }
  return d
}

/**
 * Solve for IRR using Newton-Raphson with bisection fallback.
 * Returns NaN if no solution found.
 */
export function solveIRR(cashflows: number[], guess = 0.1): number {
  // Check trivial: if all cashflows are zero, IRR is undefined
  if (cashflows.every(cf => cf === 0)) return NaN

  // Check that there's at least one sign change
  const hasNeg = cashflows.some(cf => cf < 0)
  const hasPos = cashflows.some(cf => cf > 0)
  if (!hasNeg || !hasPos) return NaN

  // Newton-Raphson
  let rate = guess
  for (let i = 0; i < MAX_ITERATIONS; i++) {
    const npv = calcNPV(rate, cashflows)
    if (Math.abs(npv) < TOLERANCE) return rate

    const dNpv = calcNPVDerivative(rate, cashflows)
    if (Math.abs(dNpv) < TOLERANCE) break // derivative too small, fall through to bisection

    const newRate = rate - npv / dNpv
    // Guard against divergence
    if (newRate < -0.999) break
    if (Math.abs(newRate - rate) < TOLERANCE) return newRate
    rate = newRate
  }

  // Bisection fallback
  let lo = -0.99
  let hi = 10.0

  // Find bracket
  let npvLo = calcNPV(lo, cashflows)
  let npvHi = calcNPV(hi, cashflows)

  // Expand hi if needed
  while (npvLo * npvHi > 0 && hi < 1e6) {
    hi *= 2
    npvHi = calcNPV(hi, cashflows)
  }

  if (npvLo * npvHi > 0) return NaN // no bracket found

  for (let i = 0; i < MAX_ITERATIONS; i++) {
    const mid = (lo + hi) / 2
    const npvMid = calcNPV(mid, cashflows)

    if (Math.abs(npvMid) < TOLERANCE || (hi - lo) / 2 < TOLERANCE) {
      return mid
    }

    if (npvMid * npvLo < 0) {
      hi = mid
      npvHi = npvMid
    } else {
      lo = mid
      npvLo = npvMid
    }
  }

  return (lo + hi) / 2
}

/**
 * Calculate MOIC (Multiple on Invested Capital).
 * Sum of positive cashflows / abs(sum of negative cashflows).
 */
export function calcMOIC(cashflows: number[]): number {
  let invested = 0
  let returned = 0
  for (const cf of cashflows) {
    if (cf < 0) invested += Math.abs(cf)
    else returned += cf
  }
  if (invested === 0) return 0
  return returned / invested
}
