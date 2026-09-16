import type { StrategyId } from "./types";

// Preserve the stock selector's geometry, inheriting the prediction shell palette.
export default function StrategyToggle({
  value,
  onChange,
  label = "选择策略",
  variant = "overview",
}: {
  value: StrategyId;
  onChange: (value: StrategyId) => void;
  label?: string;
  variant?: "overview" | "artifact";
}) {
  const artifact = variant === "artifact";
  return (
    <div
      className={artifact
        ? "flex shrink-0 bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-md p-0.5"
        : "flex overflow-hidden"}
      role="group"
      aria-label={label}
      style={artifact ? undefined : { border: "2px solid var(--ink)", flexShrink: 0 }}
    >
      {(["fave", "pfme"] as const).map((s, i) => (
        <button
          type="button"
          key={s}
          onClick={() => onChange(s)}
          aria-pressed={value === s}
          title={
            s === "fave" ? "FAVE · 优势侧价值入场" : "PFME · 被动优势侧入场"
          }
          className={artifact
            ? `px-2.5 py-1 text-xs rounded-sm transition-colors whitespace-nowrap ${value === s
              ? "bg-[var(--ink)] text-[var(--paper)]"
              : "text-[var(--text-muted)] hover:text-[var(--text-primary)]"}`
            : undefined}
          style={artifact ? { cursor: "pointer" } : {
            padding: "3px 12px",
            fontSize: "10px",
            fontFamily: "var(--font-mono)",
            fontWeight: 700,
            letterSpacing: ".06em",
            textTransform: "uppercase",
            transition: "all .1s",
            background: value === s ? "var(--ink)" : "var(--paper)",
            color: value === s ? "var(--paper)" : "var(--ink-dim)",
            borderTop: "none",
            borderBottom: "none",
            borderRight: "none",
            borderLeft: i > 0 ? "2px solid var(--ink)" : "none",
            cursor: "pointer",
          }}
        >
          {s.toUpperCase()}
        </button>
      ))}
    </div>
  );
}
