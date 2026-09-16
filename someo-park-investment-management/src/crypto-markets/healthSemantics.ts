import type { Strategy } from "./types";

export type HealthIssue = Strategy["issues"][number];
export type IssueKind = "current" | "pending" | "recent";

// Only explicit event codes identify historical records. Unknown codes never
// imply recovery, and a recent event does not prove the source is healthy now.
export function issueKind(code: string): IssueKind {
  if (["CURRENT_RATE_LIMIT_BACKOFF", "CURRENT_OBSERVATION_GAP"].includes(code)) {
    return "current";
  }
  if (["RECENT_OBSERVATION_GAPS", "RECENT_RATE_LIMIT"].includes(code)) {
    return "recent";
  }
  return "pending";
}

export const ISSUE_GROUPS: { kind: IssueKind; title: string }[] = [
  { kind: "current", title: "当前需处理" },
  { kind: "pending", title: "待核验" },
  { kind: "recent", title: "近期记录" },
];

export function runtimeFreshness(
  runtime: Strategy["runtime"][number],
  now: number,
): { status: string; label?: string; freshnessIssue: boolean } {
  if (runtime.as_of === null) {
    return { status: "unavailable", label: "来源时间缺失", freshnessIssue: true };
  }
  const source = Date.parse(runtime.as_of);
  if (!Number.isFinite(source) || !Number.isFinite(now) ||
      !Number.isFinite(runtime.stale_after_seconds) || runtime.stale_after_seconds <= 0) {
    return { status: "unavailable", label: "来源时间无法核验", freshnessIssue: true };
  }
  const elapsed = (now - source) / 1000;
  // Match the module's age formatter: tolerate at most five seconds of clock skew.
  if (elapsed < -5) {
    return { status: "unavailable", label: "来源时间在未来", freshnessIssue: true };
  }
  if (elapsed > runtime.stale_after_seconds) {
    return { status: "stale", label: "来源已过期", freshnessIssue: true };
  }
  return { status: runtime.status, freshnessIssue: false };
}
