import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { SnapshotSchema } from "../../src/crypto-markets/types";
import { fetchSnapshotFrom, snapshotUrl } from "../../src/crypto-markets/api";

const raw = JSON.parse(
  readFileSync(
    new URL(
      "../../public/data/crypto_prediction/snapshot.json",
      import.meta.url,
    ),
    "utf8",
  ),
);
test("real published snapshot matches the strict two-strategy Demo contract", () => {
  const snapshot = SnapshotSchema.parse(raw);
  for (const strategy of Object.values(snapshot.strategies)) {
    assert.equal(
      new Set(strategy.orders.map((o) => o.id)).size,
      strategy.orders.length,
    );
    for (const order of strategy.orders) {
      if (!order.verified)
        assert.equal(order.filled, null, "unknown fill is not zero");
      if (order.filled !== null && order.quantity !== null)
        assert.ok(order.filled <= order.quantity + 1e-7);
    }
    for (const settlement of strategy.settlements) {
      assert.ok(
        Math.abs(
          settlement.payout_usd -
            settlement.cost_usd -
            settlement.fees_usd -
            settlement.net_usd,
        ) < 1e-6,
      );
    }
    if (strategy.demo.net_pnl_usd !== null) {
      assert.ok(
        Math.abs(
          strategy.settlements.reduce((n, r) => n + r.net_usd, 0) -
            strategy.demo.net_pnl_usd,
        ) < 1e-6,
      );
    }
    for (const perf of [strategy.demo, strategy.paper]) {
      if (perf.curve.length && perf.net_pnl_usd !== null)
        assert.ok(
          Math.abs(perf.curve.at(-1)!.cumulative_usd - perf.net_pnl_usd) < 1e-5,
        );
    }
    if (strategy.id === "pfme")
      assert.ok(strategy.markets.every((m) => m.asset !== "SOL"));
  }
});
test("rejects production execution, swapped identity, extra fields, and invalid amounts", () => {
  for (const mutate of [
    (x: any) => {
      x.prod_execution_enabled = true;
    },
    (x: any) => {
      x.execution_environment = "prod";
    },
    (x: any) => {
      x.strategies.fave.id = "pfme";
    },
    (x: any) => {
      x.account_balance = 1;
    },
    (x: any) => {
      x.strategies.fave.demo.net_pnl_usd = "unknown";
    },
  ]) {
    const data = structuredClone(raw);
    mutate(data);
    assert.equal(SnapshotSchema.safeParse(data).success, false);
  }
});
test("missing values remain null and cannot become fabricated zero balances", () => {
  const data = structuredClone(raw);
  data.strategies.fave.demo.net_pnl_usd = null;
  assert.equal(
    SnapshotSchema.parse(data).strategies.fave.demo.net_pnl_usd,
    null,
  );
  delete data.strategies.fave.demo.net_pnl_usd;
  assert.equal(SnapshotSchema.safeParse(data).success, false);
});
test("fetcher rejects HTTP, invalid JSON, schema, and future timestamp failures without fallback requests", async () => {
  const original = globalThis.fetch;
  try {
    const future = structuredClone(raw);
    future.generated_at = new Date(Date.now() + 3600_000).toISOString();
    for (const response of [
      new Response("", { status: 503 }),
      new Response("<html>not data</html>"),
      Response.json({}),
      Response.json(future),
    ]) {
      let calls = 0;
      globalThis.fetch = (async (url: string, init: RequestInit) => {
        calls++;
        assert.equal(url, "/data/crypto_prediction/snapshot.json");
        assert.equal(init.cache, "no-store");
        return response;
      }) as typeof fetch;
      await assert.rejects(fetchSnapshotFrom("/data/crypto_prediction/snapshot.json"));
      assert.equal(calls, 1);
    }
  } finally {
    globalThis.fetch = original;
  }
});

test("Hosting uses the configured live source and never substitutes its bundled snapshot", () => {
  assert.equal(snapshotUrl(false), "/data/crypto_prediction/snapshot.json");
  assert.equal(snapshotUrl(true, "https://example.test/"), "https://example.test/data/crypto_prediction/snapshot.json");
  assert.throws(() => snapshotUrl(true));
  assert.throws(() => snapshotUrl(true, "http://example.test"));
  assert.throws(() => snapshotUrl(true, "https://user:password@example.test"));
});
