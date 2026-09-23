import type { Performance, Strategy } from "./types";

/** A display choice only. Selecting a result book never changes execution. */
export type ResultBasis = "paper" | "demo" | "prod";

export const RESULT_BOOKS: readonly { value: ResultBasis; label: string }[] = [
  { value: "paper", label: "纸面评估" },
  { value: "demo", label: "Kalshi Demo" },
  { value: "prod", label: "Kalshi Prod" },
];

export type ResultBook = {
  basis: ResultBasis;
  source: "paper" | "kalshi_demo" | "kalshi_prod";
  title: string;
  role: string;
  performance: Performance | null;
  availability: "available" | "unavailable" | "not_connected";
};

const BOOK_METADATA: Record<
  ResultBasis,
  Pick<ResultBook, "source" | "title" | "role">
> = {
  paper: {
    source: "paper",
    title: "纸面策略评估",
    role: "策略评估主口径；采用纸面成交及费用模型，不代表实际账户盈亏。",
  },
  demo: {
    source: "kalshi_demo",
    title: "Kalshi Demo 执行验证",
    role: "独立模拟账户的实际成交与官方结算，用于验证下单和持仓管理。",
  },
  prod: {
    source: "kalshi_prod",
    title: "Kalshi Prod 实盘结果",
    role: "尚未接入交易结果；Prod 只读行情不等于实盘成交或收益。",
  },
};

/** The v1 contract has no Prod ledger. Never infer it from Demo or quotes. */
export function selectResultBook(
  strategy: Strategy | undefined,
  basis: ResultBasis,
): ResultBook {
  const metadata = BOOK_METADATA[basis];
  if (basis === "prod") {
    return {
      basis,
      ...metadata,
      performance: null,
      availability: "not_connected",
    };
  }
  const performance = strategy?.[basis] ?? null;
  return {
    basis,
    ...metadata,
    performance,
    availability:
      performance && performance.status !== "unavailable"
        ? "available"
        : "unavailable",
  };
}

export function resultNetLabel(
  basis: ResultBasis,
  performance: Performance | null,
): string {
  if (basis === "prod") return "Prod 实盘净收益（未接入）";
  if (basis === "paper") {
    // PFME paper totals include all booked trades, including windows whose
    // execution-model quantities remain unverified. This is not a clean subset.
    return performance?.status === "partial"
      ? "纸面账本净收益（部分待核验）"
      : "纸面已结算净收益";
  }
  return performance?.status === "partial"
    ? "Demo 已核验部分净收益"
    : "Demo 已结算净收益";
}

/** Dollar drawdown of this book only; no assumed balance or percentage return. */
export function resultDrawdown(performance: Performance | null): number | null {
  if (!performance?.curve.length) return null;
  return performance.curve.reduce(
    (worst, point) => Math.min(worst, point.drawdown_usd),
    0,
  );
}
