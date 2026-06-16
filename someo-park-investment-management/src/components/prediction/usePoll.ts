/**
 * usePoll — fetch on mount and then re-fetch every `pollMs` (default 30s).
 * Used by the live in-play / upcoming views so model + tricks refresh during a
 * match without a page reload. Self-contained (does not touch the shared useApi
 * used by stock-strategy components). The last good value is kept across refreshes
 * so the UI never flashes empty while polling.
 */
import { useEffect, useRef, useState } from 'react';

export function usePoll<T>(fetchFn: () => Promise<T>, pollMs = 30000) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const fnRef = useRef(fetchFn);
  fnRef.current = fetchFn;

  useEffect(() => {
    let cancelled = false;
    const run = () =>
      fnRef.current()
        .then((r) => { if (!cancelled) { setData(r); setError(null); setUpdatedAt(Date.now()); } })
        .catch((e) => { if (!cancelled) setError(e.message); })
        .finally(() => { if (!cancelled) setLoading(false); });
    run();
    const id = setInterval(run, pollMs);
    return () => { cancelled = true; clearInterval(id); };
  }, [pollMs]);

  return { data, loading, error, updatedAt };
}
