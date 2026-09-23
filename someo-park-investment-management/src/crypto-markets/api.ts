import { useCallback, useEffect, useRef, useState } from "react";
import { SnapshotSchema, type Snapshot } from "./types";

const SNAPSHOT_PATH = "/data/crypto_prediction/snapshot.json";
type CryptoImportMeta = ImportMeta & {
  env: { PROD: boolean; VITE_API_URL?: string };
};

export function snapshotUrl(production: boolean, origin?: string): string {
  if (!production) return SNAPSHOT_PATH;
  if (!origin?.trim()) throw new Error("线上实时数据地址未配置，无法读取当前快照");
  const url = new URL(origin);
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) {
    throw new Error("线上实时数据地址无效");
  }
  return origin.replace(/\/+$/, "") + SNAPSHOT_PATH;
}

export function fetchSnapshot(signal?: AbortSignal): Promise<Snapshot> {
  return fetchSnapshotFrom(
    snapshotUrl(
      (import.meta as CryptoImportMeta).env.PROD,
      (import.meta as CryptoImportMeta).env.VITE_API_URL,
    ),
    signal,
  );
}

export async function fetchSnapshotFrom(url: string, signal?: AbortSignal): Promise<Snapshot> {
  const response = await fetch(url, {
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw new Error(`数据请求失败（HTTP ${response.status}）`);
  let raw: unknown;
  try {
    raw = await response.json();
  } catch {
    throw new Error("数据不是有效 JSON；当前快照不可用");
  }
  const parsed = SnapshotSchema.safeParse(raw);
  if (!parsed.success)
    throw new Error(
      `数据合同校验失败：${parsed.error.issues[0]?.path.join(".") || "snapshot"}`,
    );
  if (Date.parse(parsed.data.generated_at) > Date.now() + 60_000)
    throw new Error("快照时间在未来，请检查数据源时钟");
  return parsed.data;
}

export function useCryptoSnapshot(enabled = true) {
  const [data, setData] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [request, setRequest] = useState(0);
  const controller = useRef<AbortController | null>(null);
  const refresh = useCallback(() => setRequest((n) => n + 1), []);
  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    const run = async () => {
      if (controller.current) return;
      const ac = new AbortController();
      controller.current = ac;
      setLoading(true);
      const timeout = setTimeout(() => ac.abort(), 15_000);
      try {
        const next = await fetchSnapshot(ac.signal);
        if (!stopped) {
          setData(next);
          setError(null);
        }
      } catch (e) {
        if (!stopped)
          setError(
            ac.signal.aborted
              ? "数据请求超时（15 秒）"
              : e instanceof Error
                ? e.message
                : "数据请求失败",
          );
      } finally {
        clearTimeout(timeout);
        if (controller.current === ac) controller.current = null;
        if (!stopped) setLoading(false);
      }
    };
    void run();
    const interval = setInterval(() => void run(), 60_000);
    return () => {
      stopped = true;
      clearInterval(interval);
      controller.current?.abort();
      controller.current = null;
    };
  }, [request, enabled]);
  return { data, error, loading, refresh };
}
