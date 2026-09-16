import assert from "node:assert/strict";
import { test } from "node:test";
import { issueKind, runtimeFreshness } from "../../src/crypto-markets/healthSemantics";
import { IssueSchema, RuntimeSchema } from "../../src/crypto-markets/types";

const now = Date.parse("2026-09-15T17:00:00Z");
const runtime = (status = "verified", offset = -30_000) => ({
  source: "PFME 观察",
  status,
  as_of: new Date(now + offset).toISOString(),
  stale_after_seconds: 300,
  detail: "来源自身的时间",
});

test("current problems and retained events have distinct categories without asserting recovery", () => {
  for (const code of ["CURRENT_RATE_LIMIT_BACKOFF", "CURRENT_OBSERVATION_GAP"]) {
    assert.equal(issueKind(code), "current");
  }
  for (const code of ["RECENT_RATE_LIMIT", "RECENT_OBSERVATION_GAPS"]) {
    assert.equal(issueKind(code), "recent");
  }
  for (const code of ["SOURCE_HASH_MISMATCH", "SOURCE_HASH_UNAVAILABLE", "GAP_HISTORY_PARTIAL",
    "RATE_LIMIT_HISTORY_UNAVAILABLE", "UNKNOWN", "RECOVERED_UNKNOWN_EVENT", ""]) {
    assert.equal(issueKind(code), "pending");
  }
});

test("stale evidence overrides healthy and historical badges instead of refreshing with the snapshot", () => {
  for (const status of ["verified", "recent_event", "OBSERVING", "RATE_LIMIT_BACKOFF"]) {
    assert.deepEqual(runtimeFreshness(runtime(status, -300_001), now), {
      status: "stale", label: "来源已过期", freshnessIssue: true,
    });
  }
  assert.equal(runtimeFreshness(runtime("verified", -300_000), now).status, "verified");
});

test("missing, invalid, and future source times cannot retain a healthy badge", () => {
  for (const as_of of [null, "bad-time", new Date(now + 6_000).toISOString()]) {
    const result = runtimeFreshness({ ...runtime(), as_of }, now);
    assert.equal(result.status, "unavailable");
    assert.equal(result.freshnessIssue, true);
  }
  assert.equal(runtimeFreshness({ ...runtime(), stale_after_seconds: -1 }, now).status, "unavailable");
  assert.equal(runtimeFreshness(runtime(), Number.NaN).status, "unavailable");
});

test("fresh source time preserves its reported operational status without manufacturing success", () => {
  for (const status of ["verified", "recent_event", "degraded", "unavailable", "WAITING_FOR_MARKET"]) {
    assert.deepEqual(runtimeFreshness(runtime(status), now), { status, freshnessIssue: false });
  }
});

test("explicit health codes remain compatible with the existing strict source schemas", () => {
  for (const code of ["CURRENT_RATE_LIMIT_BACKOFF", "CURRENT_OBSERVATION_GAP", "RECENT_RATE_LIMIT"]) {
    assert.equal(IssueSchema.parse({ source: "pfme_observer", code, detail: "记录" }).code, code);
  }
  assert.equal(RuntimeSchema.parse(runtime("recent_event")).status, "recent_event");
  assert.equal(RuntimeSchema.safeParse({ ...runtime(), recovered: true }).success, false);
});
