import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { SnapshotSchema } from "../../src/crypto-markets/types";
import {
  RESULT_BOOKS,
  resultDrawdown,
  resultNetLabel,
  selectResultBook,
} from "../../src/crypto-markets/resultBooks";

const snapshot = SnapshotSchema.parse(
  JSON.parse(
    readFileSync(
      new URL("../../public/data/crypto_prediction/snapshot.json", import.meta.url),
      "utf8",
    ),
  ),
);

test("paper is the first result choice and each source retains its own real ledger", () => {
  assert.deepEqual(RESULT_BOOKS.map((book) => book.value), ["paper", "demo", "prod"]);
  for (const strategy of Object.values(snapshot.strategies)) {
    const paper = selectResultBook(strategy, "paper");
    const demo = selectResultBook(strategy, "demo");
    assert.equal(paper.source, "paper");
    assert.equal(demo.source, "kalshi_demo");
    assert.strictEqual(paper.performance, strategy.paper);
    assert.strictEqual(demo.performance, strategy.demo);
    assert.notStrictEqual(paper.performance, demo.performance);
    assert.match(paper.role, /主口径/);
    assert.match(demo.role, /模拟账户/);
  }
});

test("missing or unavailable paper results never borrow a healthy Demo ledger", () => {
  const strategy = structuredClone(snapshot.strategies.fave);
  strategy.paper = {
    ...strategy.paper,
    status: "unavailable",
    net_pnl_usd: null,
    fees_usd: null,
    settled_count: null,
    open_count: null,
    curve: [],
    sample_windows: null,
    unresolved_count: null,
  };
  const paper = selectResultBook(strategy, "paper");
  assert.equal(paper.availability, "unavailable");
  assert.equal(paper.performance?.net_pnl_usd, null);
  assert.deepEqual(paper.performance?.curve, []);
  assert.notStrictEqual(paper.performance, strategy.demo);
  for (const basis of ["paper", "demo"] as const) {
    const missing = selectResultBook(undefined, basis);
    assert.equal(missing.availability, "unavailable");
    assert.equal(missing.performance, null);
  }
});

test("unconnected Prod has no numeric result even if a caller supplies a renamed Demo book", () => {
  for (const strategy of Object.values(snapshot.strategies)) {
    const withUntrustedProd = Object.assign(structuredClone(strategy), {
      prod: strategy.demo,
    });
    for (const input of [undefined, strategy, withUntrustedProd]) {
      const prod = selectResultBook(input, "prod");
      assert.equal(prod.source, "kalshi_prod");
      assert.equal(prod.availability, "not_connected");
      assert.equal(prod.performance, null);
      assert.equal(resultDrawdown(prod.performance), null);
      assert.match(prod.role, /只读行情不等于实盘成交或收益/);
      assert.ok(!Object.values(prod).some((value) => typeof value === "number"));
    }
  }
});

test("partial paper describes the whole booked amount, while Demo identifies its verified subset", () => {
  const partial = { ...snapshot.strategies.pfme.paper, status: "partial" as const };
  const paperLabel = resultNetLabel("paper", partial);
  assert.equal(paperLabel, "纸面账本净收益（部分待核验）");
  assert.doesNotMatch(paperLabel, /已核验部分/);
  assert.equal(resultNetLabel("demo", partial), "Demo 已核验部分净收益");
  assert.match(resultNetLabel("prod", partial), /未接入/);
});

test("drawdown uses only the selected curve and keeps unknown fees unknown", () => {
  for (const strategy of Object.values(snapshot.strategies)) {
    for (const basis of ["paper", "demo"] as const) {
      const book = selectResultBook(strategy, basis);
      const expected = strategy[basis].curve.length
        ? Math.min(0, ...strategy[basis].curve.map((point) => point.drawdown_usd))
        : null;
      assert.equal(resultDrawdown(book.performance), expected);
      assert.equal(book.performance?.fees_usd, strategy[basis].fees_usd);
    }
  }
  const paper = { ...snapshot.strategies.fave.paper, fees_usd: null };
  const strategy = { ...snapshot.strategies.fave, paper };
  assert.equal(selectResultBook(strategy, "paper").performance?.fees_usd, null);
  assert.equal(resultDrawdown({ ...paper, curve: [] }), null);
});
