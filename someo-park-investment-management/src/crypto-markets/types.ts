import { z } from "zod";

export const StrategyIdSchema = z.enum(["fave", "pfme"]);
export type StrategyId = z.infer<typeof StrategyIdSchema>;
const N = z.number().finite().nullable();
const T = z.string().datetime({ offset: true }).nullable();
export const IssueSchema = z
  .object({ source: z.string(), code: z.string(), detail: z.string() })
  .strict();
export const RuntimeSchema = z
  .object({
    source: z.string(),
    status: z.string(),
    as_of: T,
    stale_after_seconds: z.number(),
    detail: z.string(),
  })
  .strict();
const PointSchema = z
  .object({
    at: z.string(),
    net_usd: z.number(),
    cumulative_usd: z.number(),
    drawdown_usd: z.number(),
  })
  .strict();
export const PerformanceSchema = z
  .object({
    status: z.enum(["verified", "partial", "unavailable"]),
    source_as_of: T,
    scope: z.string(),
    net_pnl_usd: N,
    fees_usd: N,
    settled_count: N,
    open_count: N,
    curve: z.array(PointSchema),
    sample_windows: N,
    unresolved_count: N,
    verdict: z.string().nullable(),
    note: z.string(),
  })
  .strict();
export const ExecutionWindowSchema = z
  .object({
    label: z.string(),
    since: T,
    source_as_of: T,
    scope: z.string(),
    note: z.string(),
    signals: N,
    requested_contracts: N,
    accepted: N,
    filled_orders: N,
    full_fills: N,
    partial_fills: N,
    zero_fills: N,
    filled_contracts: N,
    empty_side: N,
    unavailable: N,
    other_skips: N,
  })
  .strict();
export const OrderSchema = z
  .object({
    id: z.string(),
    ticker: z.string(),
    asset: z.string(),
    at: T,
    close_at: T,
    side: z.enum(["yes", "no"]),
    entry: z.boolean(),
    quantity: N,
    filled: N,
    price: N,
    cost_usd: N,
    fees_usd: N,
    status: z.string(),
    verified: z.boolean(),
  })
  .strict();
export const PositionSchema = z
  .object({
    ticker: z.string(),
    asset: z.string(),
    close_at: T,
    yes_quantity: N,
    no_quantity: N,
    paired_quantity: N,
    net_quantity: N,
    cost_usd: N,
    fees_usd: N,
    exit_status: z.string(),
    verified: z.boolean(),
    source_as_of: T,
  })
  .strict();
export const SettlementSchema = z
  .object({
    ticker: z.string(),
    asset: z.string(),
    at: T,
    result: z.enum(["yes", "no"]),
    quantity: z.number(),
    cost_usd: z.number(),
    fees_usd: z.number(),
    payout_usd: z.number(),
    net_usd: z.number(),
  })
  .strict();
export const MarketSchema = z
  .object({
    ticker: z.string(),
    asset: z.string(),
    close_at: T,
    threshold: N,
    spot: N,
    yes_ask: N,
    no_ask: N,
    yes_quantity: N,
    no_quantity: N,
    quote_at: T,
    quote_source: z.enum(["orderbook", "market_summary", "unavailable"]),
    quote_status: z.enum([
      "available",
      "one_sided",
      "empty",
      "unavailable",
      "summary",
    ]),
    status: z.string(),
    result: z.enum(["yes", "no"]).nullable(),
  })
  .strict();
export const StrategySchema = z
  .object({
    id: StrategyIdSchema,
    acronym: z.string(),
    name: z.string(),
    english_name: z.string(),
    description: z.string(),
    version: z.string(),
    parameters: z.array(
      z.object({ label: z.string(), value: z.string() }).strict(),
    ),
    runtime: z.array(RuntimeSchema),
    demo: PerformanceSchema,
    paper: PerformanceSchema,
    execution: z
      .object({
        all: ExecutionWindowSchema,
        recent: ExecutionWindowSchema,
        exits: z
          .object({ all: ExecutionWindowSchema, recent: ExecutionWindowSchema })
          .strict(),
      })
      .strict(),
    orders: z.array(OrderSchema),
    positions: z.array(PositionSchema),
    settlements: z.array(SettlementSchema),
    markets: z.array(MarketSchema),
    issues: z.array(IssueSchema),
  })
  .strict();
export const SnapshotSchema = z
  .object({
    schema_version: z.literal(1),
    snapshot_id: z.string(),
    generated_at: z.string().datetime({ offset: true }),
    execution_environment: z.literal("demo"),
    market_data_environment: z.literal("prod"),
    prod_execution_enabled: z.literal(false),
    issues: z.array(IssueSchema),
    strategies: z
      .object({ fave: StrategySchema, pfme: StrategySchema })
      .strict(),
  })
  .strict()
  .superRefine((s, ctx) => {
    for (const id of ["fave", "pfme"] as const) {
      if (
        s.strategies[id].id !== id ||
        s.strategies[id].acronym !== id.toUpperCase()
      ) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Strategy identity mismatch",
          path: ["strategies", id],
        });
      }
    }
  });
export type Snapshot = z.infer<typeof SnapshotSchema>;
export type Strategy = z.infer<typeof StrategySchema>;
export type Performance = z.infer<typeof PerformanceSchema>;
export type Market = z.infer<typeof MarketSchema>;
export type ArtifactType =
  | "performance"
  | "orders"
  | "positions"
  | "settlements"
  | "execution"
  | "markets"
  | "rules"
  | "health";
