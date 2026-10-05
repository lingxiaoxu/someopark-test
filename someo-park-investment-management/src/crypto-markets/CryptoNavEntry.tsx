import { Bitcoin } from "lucide-react";
import { useTranslation } from "react-i18next";

const labels = {
  zh: { title: "加密货币预测市场", subtitle: "二元合约 · 15分钟 · Kalshi" },
  en: { title: "Crypto Prediction Markets", subtitle: "Binary contracts · 15 min · Kalshi" },
  ja: { title: "暗号資産予測市場", subtitle: "バイナリー契約 · 15分 · Kalshi" },
  fr: { title: "Marchés prédictifs crypto", subtitle: "Contrats binaires · 15 min · Kalshi" },
  es: { title: "Mercados predictivos cripto", subtitle: "Contratos binarios · 15 min · Kalshi" },
};

// The fifth mode uses the same Sidebar and in-place mode switch as World Cup.
export default function CryptoNavEntry({ active, onSelect, collapsed }: { active: boolean; onSelect: () => void; collapsed?: boolean }) {
  const { i18n } = useTranslation();
  const language = (i18n.resolvedLanguage || i18n.language).split("-")[0];
  const label = labels[language as keyof typeof labels] || labels.en;

  // Collapsed sidebar rail: icon-only square, same active inversion as the full button.
  if (collapsed) {
    return (
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={active}
        title={label.title}
        aria-label={label.title}
        style={{
          width: 32, height: 32, display: "flex", alignItems: "center", justifyContent: "center",
          background: active ? "#fff" : "#111",
          color: active ? "#111" : "#fff",
          border: "2px solid var(--ink)",
          boxShadow: "var(--shadow-pixel-sm)",
          cursor: "pointer",
        }}
      >
        <Bitcoin style={{ width: 16, height: 16 }} aria-hidden="true" />
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      className="flex items-center gap-3 px-3 py-2.5 transition-all w-full"
      style={{
        marginTop: "8px",
        background: active ? "#fff" : "#111",
        color: active ? "#111" : "#fff",
        border: "2px solid var(--ink)",
        borderLeft: "4px solid var(--ink)",
        boxShadow: "var(--shadow-pixel-sm)",
        fontFamily: "var(--font-mono)",
        cursor: "pointer",
        textDecoration: "none",
      }}
    >
      <Bitcoin className="w-4 h-4 shrink-0" aria-hidden="true" />
      <span className="flex flex-col items-start flex-1">
        <span
          style={{
            fontSize: "11px",
            fontWeight: 700,
            letterSpacing: ".06em",
            textTransform: "uppercase",
          }}
        >
          {label.title}
        </span>
        <span style={{ fontSize: "10px", color: active ? "#555" : "#ccc" }}>{label.subtitle}</span>
      </span>
    </button>
  );
}
