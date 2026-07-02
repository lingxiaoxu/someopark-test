import React, { useEffect, useRef, useState } from 'react';

/**
 * Render Recharts children only after the wrapper has a measured, nonzero
 * size. Panels/tabs mount charts during open animations when the container
 * briefly measures 0/-1 — Recharts' ResponsiveContainer then logs
 * "The width(-1) and height(-1) of chart should be greater than 0" once per
 * chart. Gating on a ResizeObserver removes the zero-size mount entirely.
 *
 * Usage: give the wrapper its size via `height` (fixed px) or `className`
 * (e.g. "h-full" inside a sized flex parent), then put the
 * ResponsiveContainer inside.
 */
export default function SizedChart({ height, className, children }: {
  height?: number;
  className?: string;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const check = () => {
      if (el.clientWidth > 0 && el.clientHeight > 0) setReady(true);
    };
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return (
    <div ref={ref} className={className} style={height != null ? { height } : undefined}>
      {ready ? children : null}
    </div>
  );
}
