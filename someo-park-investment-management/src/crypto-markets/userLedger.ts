// Pure merge of a connected user's private prod ledger into the public crypto
// snapshot (2026-10-01). No React / env / network imports - unit-testable.
import { z } from "zod";
import {
  PerformanceSchema, OrderSchema, PositionSchema, SettlementSchema, ExecutionWindowSchema,
  type Snapshot,
} from "./types";

// 2026-10-01: a logged-in NON-owner whose own Kalshi PROD key passed the live
// check sees HIS account's prod ledger (served privately by the authenticated
// /api/kalshi-keys/ledger route) in place of the owner's FAVE prod block,
// orders, positions and settlements. The owner and everyone else keep the
// public snapshot exactly as before.
const UserLedgerSchema = z.object({
  source: z.literal("user"),
  prod: PerformanceSchema,
  orders: z.array(OrderSchema),
  positions: z.array(PositionSchema),
  settlements: z.array(SettlementSchema),
  // the user's OWN execution statistics (fill rate etc.); without it the
  // Execution view would keep showing the owner's prod fills.
  execution: z.object({ all: ExecutionWindowSchema, recent: ExecutionWindowSchema }).optional(),
}).passthrough();

type Window = z.infer<typeof ExecutionWindowSchema>;
const emptyWindow = (label: string, at: string): Window => ({
  label, since: at, source_as_of: null, scope: "你的 Kalshi Production 账户", note: "尚无跟单订单。",
  signals: null, requested_contracts: null, accepted: null, filled_orders: null, full_fills: null,
  partial_fills: null, zero_fills: null, filled_contracts: null, empty_side: null, unavailable: null,
  other_skips: null,
});

export function emptyUserLedger(snap: Snapshot) {
  return {
    prod: {
      status: "unavailable" as const, source_as_of: snap.generated_at,
      scope: "你的 Kalshi Production 账户", net_pnl_usd: null, fees_usd: null,
      settled_count: 0, open_count: 0, curve: [], sample_windows: null,
      unresolved_count: 0, verdict: null, note: "账户已连接，尚无实盘订单。",
    },
    orders: [], positions: [], settlements: [],
    execution: { all: emptyWindow("跟单开通以来", snap.generated_at), recent: emptyWindow("最近48h MAIN", snap.generated_at) },
  };
}

/** Pure merge: replace ONLY fave's prod-ledger fields; everything else intact. */
export function applyUserLedger(snap: Snapshot, raw: unknown): Snapshot {
  const fave = snap.strategies.fave;
  if (!fave || !raw || (raw as any).source !== "user") return snap;
  const parsed = UserLedgerSchema.safeParse(raw);
  // Connected but no ledger yet (or unreadable): the user's view is EMPTY,
  // never the owner's numbers.
  const led = parsed.success ? parsed.data : emptyUserLedger(snap);
  const empty = emptyUserLedger(snap).execution;
  return {
    ...snap,
    strategies: {
      ...snap.strategies,
      fave: {
        ...fave, prod: led.prod, orders: led.orders, positions: led.positions, settlements: led.settlements,
        execution: { ...fave.execution, all: led.execution?.all ?? empty.all, recent: led.execution?.recent ?? empty.recent },
      },
    },
  };
}

