import assert from "node:assert/strict";
import { test } from "node:test";
import { selectHomeMarkets } from "../../src/crypto-markets/homeMarkets";
import { MarketSchema, type Market } from "../../src/crypto-markets/types";

const NOW = Date.parse("2026-09-15T17:45:00Z");
const ASSETS = ["BTC", "ETH", "SOL", "DOGE", "XRP"];
const iso = (offset: number) => new Date(NOW + offset).toISOString();

function market(asset = "BTC", changes: Partial<Market> = {}): Market {
  return MarketSchema.parse({
    ticker: `KX${asset}15M-26SEP151400-00`,
    asset,
    close_at: iso(15 * 60_000),
    threshold: 60_000,
    spot: 60_001,
    yes_ask: 0.51,
    no_ask: 0.50,
    yes_quantity: 25,
    no_quantity: 25,
    quote_at: iso(-1000),
    quote_source: "orderbook",
    quote_status: "available",
    status: "active",
    result: null,
    ...changes,
  });
}

test("all five open assets stay visible even with one-sided or empty orderbooks", () => {
  const markets = ASSETS.map((asset) => market(asset));
  markets[1] = market("ETH", {
    status: "open",
    quote_status: "one_sided",
    no_ask: null,
    no_quantity: null,
  });
  markets[2] = market("SOL", {
    quote_status: "empty",
    yes_ask: null,
    no_ask: null,
    yes_quantity: null,
    no_quantity: null,
  });
  const rows = selectHomeMarkets(markets, NOW);
  assert.deepEqual(rows.map((row) => row.asset), ASSETS);
  assert.equal(rows.filter((row) => row.state === "open").length, 5);
  rows.forEach((row, index) => assert.strictEqual(row.market, markets[index]));
  assert.equal(rows[1].market?.quote_status, "one_sided");
  assert.equal(rows[2].market?.quote_status, "empty");
});

test("the exact expiry boundary hides the old contract and prices while retaining its asset", () => {
  const current = market();
  const closeAt = Date.parse(current.close_at!);
  assert.equal(selectHomeMarkets([current], closeAt - 1)[0].state, "open");
  assert.deepEqual(selectHomeMarkets([current], closeAt), [
    { asset: "BTC", state: "awaiting_next", market: null },
  ]);
  assert.deepEqual(selectHomeMarkets([current], closeAt + 1), [
    { asset: "BTC", state: "awaiting_next", market: null },
  ]);
});

test("a complete rollover retains all five asset slots but reports zero open contracts", () => {
  const rows = selectHomeMarkets(
    ASSETS.map((asset) => market(asset, { close_at: iso(0) })),
    NOW,
  );
  assert.deepEqual(rows, ASSETS.map((asset) => ({
    asset,
    state: "awaiting_next",
    market: null,
  })));
  assert.equal(rows.filter((row) => row.state === "open").length, 0);
});

test("an unexpired stale recording is stale, not an expired or open contract", () => {
  const stale = market("BTC", { status: "stale_recording" });
  assert.deepEqual(selectHomeMarkets([stale], NOW), [
    { asset: "BTC", state: "stale", market: null },
  ]);
  assert.deepEqual(selectHomeMarkets([stale], Date.parse(stale.close_at!)), [
    { asset: "BTC", state: "awaiting_next", market: null },
  ]);
});

test("settled, missing/invalid expiry, and unknown status records never count as open", () => {
  for (const result of ["yes", "no"] as const) {
    assert.deepEqual(selectHomeMarkets([market("BTC", { result })], NOW), [
      { asset: "BTC", state: "awaiting_next", market: null },
    ]);
  }
  const records = [
    market("BTC", { close_at: null }),
    // The snapshot schema rejects this upstream; also check the selector's
    // defensive handling if a future caller bypasses snapshot validation.
    { ...market("ETH"), close_at: "invalid-date" },
    market("SOL", { status: "unrecognized_status" }),
    market("DOGE", { status: "closed" }),
  ];
  assert.equal(MarketSchema.safeParse(records[1]).success, false);
  assert.deepEqual(selectHomeMarkets(records, NOW), records.map((record) => ({
    asset: record.asset,
    state: "unconfirmed",
    market: null,
  })));
});

test("multiple contracts select the nearest still-open expiry and recover when the next window arrives", () => {
  const previous = market("BTC", { ticker: "KXBTC15M-26SEP151345-00", close_at: iso(0) });
  const next = market("BTC", { ticker: "KXBTC15M-26SEP151400-00", close_at: iso(15 * 60_000) });
  const later = market("BTC", { ticker: "KXBTC15M-26SEP151415-00", close_at: iso(30 * 60_000) });
  const unavailableNearest = market("BTC", {
    ticker: "KXBTC15M-26SEP151350-00", close_at: iso(5 * 60_000), status: "stale_recording",
  });
  assert.deepEqual(selectHomeMarkets([previous], NOW), [
    { asset: "BTC", state: "awaiting_next", market: null },
  ]);
  const recovered = selectHomeMarkets([later, previous, unavailableNearest, next], NOW);
  assert.equal(recovered.length, 1);
  assert.equal(recovered[0].state, "open");
  assert.strictEqual(recovered[0].market, next);
  assert.strictEqual(selectHomeMarkets([next, later], Date.parse(next.close_at!))[0].market, later);
});

test("a four-asset PFME input stays scoped to its own records and is never mutated", () => {
  const pfmeAssets = ["BTC", "ETH", "SOL", "DOGE"];
  const records = pfmeAssets.map((asset, index) => market(asset, {
    close_at: iso(index % 2 === 0 ? 0 : 15 * 60_000),
  }));
  const before = structuredClone(records);
  records.forEach(Object.freeze);
  Object.freeze(records);
  const rows = selectHomeMarkets(records, NOW);
  assert.deepEqual(rows.map((row) => row.asset), pfmeAssets);
  assert.equal(rows.some((row) => row.asset === "XRP"), false);
  assert.equal(rows.filter((row) => row.state === "open").length, 2);
  assert.deepEqual(records, before);
});

test("an empty recording does not invent any assets or contracts", () => {
  assert.deepEqual(selectHomeMarkets([], NOW), []);
});
