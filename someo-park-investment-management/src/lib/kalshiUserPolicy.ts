// Pure display policy for per-user Kalshi PROD data (2026-10-01).
// No React / env / network imports so it is unit-testable under Node.

export type KalshiStatus = {
  owner: boolean;
  user_id?: string;            // Supabase auth.users.id - the one id used everywhere
  email?: string | null;
  connected: boolean;
  auth_failed?: boolean;
  validated_at?: string | null;
  key_id_masked?: string | null;
  pending_key_id?: boolean;
  pending_key_id_masked?: string | null;
  pem_uploaded?: boolean;
  pending_pem?: boolean;       // a replacement key awaiting the live check
  trading_enabled?: boolean;   // owner's manual allowlist; display-only until then
  trading?: TradingState | null;   // live-trading application (2026-10-04)
};

/** Live-trading application state (server/utils/kalshiUserKeys.ts tradingState). */
export type TradingState = {
  state: 'none' | 'requested' | 'approved' | 'active' | 'stopped';
  requested_ratio: number | null;
  requested_at: string | null;
  approved_ratio: number | null;
  stopped_at: string | null;
  stopped_by: 'user' | 'owner' | null;
};

export type KalshiUserStore = { status: KalshiStatus | null; balanceUsd: number | null };

/** True only for a logged-in NON-owner whose key passed the live check. */
export const usesOwnKalshi = (s: KalshiStatus | null) => !!s && !s.owner && s.connected;

/**
 * Platform-wide Kalshi PROD balance override (World Cup risk + venues, Soccer
 * venues). Connected non-owner → HIS balance, or '—' if it could not be read
 * (never the owner's figure). Owner / not connected → the object unchanged.
 */
export function withUserKalshiProd<T extends Record<string, any>>(
  balances: T, s: KalshiUserStore, format?: (usd: number) => unknown,
): T {
  if (!usesOwnKalshi(s.status)) return balances;
  const v = typeof s.balanceUsd === 'number' ? (format ? format(s.balanceUsd) : s.balanceUsd) : '—';
  return { ...balances, kalshi_prod_usd: v };
}
