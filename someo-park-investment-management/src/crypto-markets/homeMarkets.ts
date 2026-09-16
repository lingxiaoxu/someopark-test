import type { Market } from "./types";

export type HomeMarket = {
  asset: string;
  state: "open" | "awaiting_next" | "stale" | "unconfirmed";
  market: Market | null;
};

/** Keep only asset identities from inactive recordings, never their quotes or
 * ticker. A pending row is not an open contract or a replacement market. */
export function selectHomeMarkets(markets: Market[], now: number): HomeMarket[] {
  const byAsset = new Map<string, Market[]>();
  for (const market of markets) {
    const rows = byAsset.get(market.asset) ?? [];
    rows.push(market);
    byAsset.set(market.asset, rows);
  }
  const closeAt = (market: Market) => market.close_at ? Date.parse(market.close_at) : NaN;
  return [...byAsset].map(([asset, records]) => {
    const current = records
      .filter(m => !m.result && closeAt(m) > now && ["active", "open"].includes(m.status))
      .sort((a, b) => closeAt(a) - closeAt(b))[0];
    if (current) return { asset, state: "open", market: current };

    // Inspect the latest known expiry only to explain the missing current
    // contract. Missing/invalid dates must not become an invented next window.
    const latest = [...records].sort((a, b) =>
      (Number.isFinite(closeAt(b)) ? closeAt(b) : Infinity)
      - (Number.isFinite(closeAt(a)) ? closeAt(a) : Infinity))[0];
    const state = latest.result || closeAt(latest) <= now ? "awaiting_next"
      : latest.status === "stale_recording" ? "stale" : "unconfirmed";
    return { asset, state, market: null };
  });
}
