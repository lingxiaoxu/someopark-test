import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { useCryptoSnapshot } from "./api";
import type { StrategyId } from "./types";
import type { ResultBasis } from "./resultBooks";

function initialStrategy(): StrategyId {
  try {
    return localStorage.getItem("sp-crypto-prediction-strategy") === "pfme" ? "pfme" : "fave";
  } catch {
    return "fave";
  }
}

function usePredictionState(enabled: boolean) {
  const snapshot = useCryptoSnapshot(enabled);
  const [selected, setSelected] = useState<StrategyId>(initialStrategy);
  // Viewing a book never changes the exchange or the running strategy.
  const [resultBasis, setResultBasis] = useState<ResultBasis>("paper");
  const [now, setNow] = useState(Date.now());
  const choose = useCallback((id: StrategyId) => {
    setSelected(id);
    try { localStorage.setItem("sp-crypto-prediction-strategy", id); } catch { /* In-memory selection remains available. */ }
  }, []);
  useEffect(() => {
    if (!enabled) return;
    setNow(Date.now());
    const interval = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(interval);
  }, [enabled]);
  const strategy = snapshot.data?.strategies[selected];
  const stale = !!snapshot.data && now - Date.parse(snapshot.data.generated_at) > 180_000;
  const issues = [...(snapshot.data?.issues ?? []), ...(strategy?.issues ?? [])];
  return { ...snapshot, selected, choose, resultBasis, setResultBasis, now, stale, issues, strategy };
}

const CryptoContext = createContext<ReturnType<typeof usePredictionState> | null>(null);

export function CryptoPredictionProvider({ enabled, children }: { enabled: boolean; children?: ReactNode }) {
  const value = usePredictionState(enabled);
  return <CryptoContext.Provider value={value}>{children}</CryptoContext.Provider>;
}

export function useCryptoPrediction() {
  const value = useContext(CryptoContext);
  if (!value) throw new Error("Crypto content must be rendered inside the shared application provider");
  return value;
}
