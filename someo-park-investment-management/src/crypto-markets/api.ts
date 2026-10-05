import { useCallback, useEffect, useRef, useState } from "react";
import type { Snapshot } from "./types";
import { kalshiUser, usesOwnKalshi, useKalshiUser } from "../lib/kalshiUser";
import { applyUserLedger } from "./userLedger";

export { snapshotUrl, fetchSnapshot, fetchSnapshotFrom } from "./snapshotFetch";
import { fetchSnapshot } from "./snapshotFetch";

export async function overlayUserLedger(snap: Snapshot): Promise<Snapshot> {
  // A connected user whose ledger cannot be fetched sees an EMPTY own view,
  // never the owner's figures.
  const raw = await kalshiUser.ledger().catch(() => ({ source: "user", unavailable: true }));
  return applyUserLedger(snap, raw);
}

export function useCryptoSnapshot(enabled = true) {
  const { status: kalshiStatus, ready: kalshiReady } = useKalshiUser();
  const ownAccount = usesOwnKalshi(kalshiStatus);
  const [data, setData] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [request, setRequest] = useState(0);
  const controller = useRef<AbortController | null>(null);
  const viewOwner = useRef<boolean | null>(null);
  const refresh = useCallback(() => setRequest((n) => n + 1), []);
  useEffect(() => {
    if (!enabled) return;
    // Until we know whose view this is, publish nothing: a connected user must
    // never see the owner's prod book, even for the second it takes to load.
    if (!kalshiReady) return;
    if (viewOwner.current !== ownAccount) {   // owner<->own view switch: drop the old book
      viewOwner.current = ownAccount;
      setData(null);
    }
    let stopped = false;
    const run = async () => {
      if (controller.current) return;
      const ac = new AbortController();
      controller.current = ac;
      setLoading(true);
      const timeout = setTimeout(() => ac.abort(), 15_000);
      try {
        let next = await fetchSnapshot(ac.signal);
        if (ownAccount) next = await overlayUserLedger(next);
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
  }, [request, enabled, ownAccount, kalshiReady]);
  return { data, error, loading, refresh };
}
