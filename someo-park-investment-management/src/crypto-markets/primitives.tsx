import type { ReactNode } from "react";

export const publicText = (value: string) =>
  value
    .replace(/\bw7\b/gi, "FAVE")
    .replace(/\bw8\b/gi, "PFME")
    .replace(/w7_/gi, "FAVE_")
    .replace(/w8_/gi, "PFME_");
export const number = (value: number | null, digits = 2) =>
  value === null
    ? "—"
    : value.toLocaleString("en-US", { maximumFractionDigits: digits });
export const money = (value: number | null) =>
  value === null
    ? "—"
    : `${value < 0 ? "−" : ""}$${Math.abs(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
export const price = (value: number | null) =>
  value === null ? "—" : `$${value.toFixed(4)}`;
export const underlyingPrice = (value: number | null) =>
  value === null
    ? "—"
    : `$${value.toLocaleString("en-US", { minimumFractionDigits: 4, maximumFractionDigits: 8 })}`;
export const ratio = (numerator: number | null, denominator: number | null) =>
  numerator === null || denominator === null || denominator === 0
    ? "—"
    : `${((numerator / denominator) * 100).toFixed(2)}%`;
export function date(value: string | null) {
  if (value === null) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? "无效时间"
    : `${parsed.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}
export function age(value: string | null, now: number) {
  if (!value) return "时间未提供";
  const seconds = Math.floor((now - new Date(value).getTime()) / 1000);
  if (!Number.isFinite(seconds)) return "时间无效";
  if (seconds < -5) return "时间在未来";
  if (seconds < 60) return `${Math.max(0, seconds)} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}
export const pnlClass = (value: number | null) =>
  value === null || value === 0
    ? ""
    : value > 0
      ? "cm-positive"
      : "cm-negative";

const LABELS: Record<string, string> = {
  verified: "已核验",
  partial: "部分可核验",
  unavailable: "不可用",
  active: "交易中",
  open: "开放",
  closed: "已关闭",
  finalized: "已结算",
  settled: "已结算",
  executed: "已成交",
  resting: "挂单中",
  canceled: "已撤销",
  cancelled: "已撤销",
  rejected: "被拒绝",
  unknown: "待确认",
  pending: "待确认",
  filled: "已成交",
  partially_filled: "部分成交",
  expired: "已到期",
  ok: "正常",
  healthy: "正常",
  degraded: "当前异常",
  recent_event: "近期记录",
  stale: "已过期",
  rate_limit_backoff: "限流等待",
  RATE_LIMIT_BACKOFF: "限流等待",
  awaiting_settlement: "等待结算",
  flat: "净仓已归零",
  exit_requested: "已请求退出",
  not_requested: "未请求退出",
  empty_side: "所需方向无挂单",
  orderbook_unavailable: "盘口请求不可用",
  available: "双边可用",
  one_sided: "仅单侧挂单",
  empty: "双侧空盘口",
  summary: "摘要报价",
  updated: "已更新",
  missing: "缺失",
  unverified: "待核验",
  zero_fill: "零成交",
  pending_send: "发送待确认",
  absent_after_close: "到期后确认无此订单",
  error: "错误",
  failed: "失败",
  running: "运行中",
  stopped: "已停止",
  waiting: "等待中",
  refreshing: "刷新中",
  loading: "加载中",
  disconnected: "连接中断",
  recorded: "已记录",
  SOURCE_HASH_MISMATCH: "版本待核验",
  SOURCE_HASH_UNAVAILABLE: "源码无法核验",
  RECENT_OBSERVATION_GAPS: "近期缺口记录",
  CURRENT_OBSERVATION_GAP: "当前存在缺口",
  GAP_HISTORY_PARTIAL: "缺口记录不完整",
  GAP_HISTORY_UNAVAILABLE: "缺口记录不可用",
  RECENT_RATE_LIMIT: "近期限流记录",
  CURRENT_RATE_LIMIT_BACKOFF: "当前限流等待",
  RATE_LIMIT_HISTORY_UNAVAILABLE: "限流记录不可用",
  OBSERVING: "观察中",
  DATA_DEGRADED: "当前数据异常",
  WAITING_FOR_MARKET: "等待市场",
  FEE_UNVERIFIED: "费用待核验",
  STOPPED_NEW: "已停止新入场",
  STALE: "已过期",
};
export const statusLabel = (value: string) =>
  LABELS[value] ?? publicText(value);
export const orderStatusLabel = (value: string) =>
  value === "partial" ? "部分成交" : statusLabel(value);
export function Status({ value, label }: { value: string; label?: string }) {
  const good = [
    "verified",
    "ok",
    "healthy",
    "executed",
    "filled",
    "settled",
    "finalized",
  ].includes(value);
  const bad = ["unavailable", "rejected", "stale", "STALE", "error", "failed", "degraded", "DATA_DEGRADED", "CURRENT_RATE_LIMIT_BACKOFF", "CURRENT_OBSERVATION_GAP"].includes(
    value,
  );
  return (
    <span
      className={`cm-status ${good ? "cm-status-good" : bad ? "cm-status-bad" : ""}`}
    >
      {label ?? statusLabel(value)}
    </span>
  );
}
// The original project's typecheck does not load React's JSX key declaration.
// Accept its special attribute locally; React consumes it and this component never reads it.
export function Panel({
  title,
  children,
  aside,
}: {
  title: string;
  children: ReactNode;
  aside?: ReactNode;
  key?: string;
}) {
  return (
    <section className="cm-section">
      <div className="cm-section-heading">
        <h3>{title}</h3>
        {aside}
      </div>
      {children}
    </section>
  );
}
export function Metric({
  label,
  value,
  detail,
  tone = "",
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: string;
}) {
  return (
    <div className="cm-metric">
      <span>{label}</span>
      <strong className={tone}>{value}</strong>
      {detail !== undefined && <small>{detail}</small>}
    </div>
  );
}
export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="cm-empty" role="status">
      {children}
    </div>
  );
}
export function Pagination({
  page,
  count,
  pageSize,
  onChange,
}: {
  page: number;
  count: number;
  pageSize: number;
  onChange: (page: number) => void;
}) {
  const pages = Math.ceil(count / pageSize);
  if (count === 0) return null;
  return (
    <div className="cm-pagination">
      <span>
        共 {count} 条 · 第 {page + 1} / {pages} 页
      </span>
      <div>
        <button
          type="button"
          disabled={page === 0}
          onClick={() => onChange(page - 1)}
        >
          上一页
        </button>
        <button
          type="button"
          disabled={page + 1 >= pages}
          onClick={() => onChange(page + 1)}
        >
          下一页
        </button>
      </div>
    </div>
  );
}
export function Segments<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div className="cm-segments" role="group" aria-label={label}>
      {options.map((option) => (
        <button
          type="button"
          key={option.value}
          aria-pressed={value === option.value}
          className={value === option.value ? "is-active" : ""}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
export function Ticker({ value }: { value: string }) {
  return (
    <span className="cm-ticker" title={value}>
      {value}
    </span>
  );
}
