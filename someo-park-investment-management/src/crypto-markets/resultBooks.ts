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
    role: "纸面模拟（主口径），非实际账户盈亏。",
  },
  demo: {
    source: "kalshi_demo",
    title: "Kalshi Demo 执行验证",
    role: "Demo 账户实际成交与官方结算。",
  },
  prod: {
    source: "kalshi_prod",
    title: "Kalshi Prod 实盘结果",
    role: "实盘账户：按交易所回执与官方结算计账。",
  },
};

/** Prod connected 2026-09-28 (W7). A strategy without a prod book (W8)
 * simply has no `prod` leg and renders as not_connected. */
export function selectResultBook(
  strategy: Strategy | undefined,
  basis: ResultBasis,
): ResultBook {
  const metadata = BOOK_METADATA[basis];
  if (basis === "prod" && !strategy?.prod) {
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
  if (basis === "prod") {
    if (!performance) return "Prod 实盘净收益（未接入）";
    return performance.status === "partial"
      ? "Prod 实盘净收益（部分待核验）"
      : "Prod 实盘已结算净收益";
  }
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
