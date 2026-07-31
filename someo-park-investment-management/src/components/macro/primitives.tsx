/**
 * Macro family shared primitives — compact re-implementations of the small UI
 * building blocks the prediction viewers use (KV rows, DataTable, Loading,
 * ErrorBox, tab buttons, mono style, AiResult, 60s polling hook). Kept INSIDE
 * src/components/macro/ so the prediction family stays untouched. Same look:
 * CSS vars / .table / .card classes, so everything inverts with the dark shell.
 */
import { useEffect, useRef, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

export const mono: CSSProperties = { fontFamily: 'var(--font-mono)' };

export const fmt = (v?: number | null, d = 3) => (v == null || isNaN(v) ? '—' : v.toFixed(d));
export const pct = (v?: number | null, d = 1) => (v == null || isNaN(v) ? '—' : `${(v * 100).toFixed(d)}%`);
export const money = (v?: number | null) => (v == null || isNaN(Number(v)) ? '—' : `$${Number(v).toFixed(2)}`);

export function Loading() {
  const { t } = useTranslation();
  return <div className="text-xs py-3" style={{ color: 'var(--text-muted)', ...mono }}>{t('macro.loading')}</div>;
}

export function ErrorBox({ e }: { e: string }) {
  const { t } = useTranslation();
  return <div className="text-xs py-3" style={{ color: 'var(--error)', ...mono }}>{t('macro.failed')}: {e}</div>;
}

export function KV({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <table className="table" style={{ marginBottom: 12 }}>
      <tbody>
        {rows.map(([k, v], i) => (
          <tr key={i}><td style={{ fontWeight: 700, width: '50%' }}>{k}</td><td style={{ textAlign: 'right' }}>{v}</td></tr>
        ))}
      </tbody>
    </table>
  );
}

export function DataTable({ cols, rows }: { cols: ReactNode[]; rows: ReactNode[][] }) {
  return (
    <table className="table">
      <thead><tr>{cols.map((c, i) => (
        <th key={i} style={{ textAlign: i === 0 ? 'left' : 'right' }}>{c}</th>
      ))}</tr></thead>
      <tbody>
        {rows.map((row, ri) => (
          <tr key={ri}>{row.map((cell, j) => <td key={j} style={{ textAlign: j === 0 ? 'left' : 'right' }}>{cell}</td>)}</tr>
        ))}
      </tbody>
    </table>
  );
}

export function TabButton({ active, onClick, label }: { active: boolean; onClick: () => void; label: ReactNode }) {
  return (
    <button onClick={onClick}
      style={{ padding: '3px 10px', fontSize: 11, fontFamily: 'var(--font-mono)', fontWeight: active ? 700 : 400,
        color: active ? 'var(--text-primary)' : 'var(--text-muted)',
        background: active ? 'var(--bg-tertiary)' : 'transparent',
        border: `1px solid ${active ? 'var(--accent-primary)' : 'var(--border-subtle)'}`,
        borderRadius: 4, cursor: 'pointer', marginRight: 6, marginBottom: 4 }}>
      {label}
    </button>
  );
}

/** Tiny muted "generated at" timestamp line shown at the top of data views. */
export function Generated({ ts }: { ts?: string | null }) {
  const { t } = useTranslation();
  if (!ts) return null;
  const d = new Date(ts);
  const s = isNaN(d.getTime()) ? String(ts) : `${d.toISOString().slice(0, 16).replace('T', ' ')} UTC`;
  return (
    <div style={{ fontSize: 9.5, color: 'var(--text-muted)', ...mono, marginBottom: 6 }}>
      {t('macro.generatedAt')}: {s}
    </div>
  );
}

/** Small colored chip (coverage states, cadence tags, decision status …). */
export function Chip({ color, children }: { color: string; children: ReactNode }) {
  return (
    <span style={{ display: 'inline-block', padding: '1px 7px', fontSize: 9.5, fontWeight: 700,
      fontFamily: 'var(--font-mono)', letterSpacing: '.05em', textTransform: 'uppercase',
      border: `1px solid ${color}`, color, borderRadius: 3 }}>
      {children}
    </span>
  );
}

// ── AiResult (button → local-model analysis → rendered text) ──────────────────
export type AiState = { loading: boolean; text?: string; error?: string; cached?: boolean };

export function AiResult({ state, onRun, label }: { state: AiState; onRun: () => void; label: string }) {
  const { t } = useTranslation();
  return (
    <div style={{ marginTop: 10 }}>
      <button onClick={onRun} disabled={state.loading}
        style={{ padding: '3px 12px', fontSize: 11, fontFamily: 'var(--font-mono)', fontWeight: 700,
          letterSpacing: '.04em', border: '1px solid var(--accent-primary)', borderRadius: 4,
          background: state.loading ? 'var(--bg-subtle)' : 'var(--bg-tertiary)',
          color: 'var(--text-primary)', cursor: state.loading ? 'default' : 'pointer' }}>
        {state.loading ? t('macro.aiLoading') : label}
      </button>
      {state.loading && <div style={{ fontSize: 10, color: 'var(--text-muted)', ...mono, marginTop: 4, fontStyle: 'italic' }}>{t('macro.aiNote')}</div>}
      {state.error && <div style={{ fontSize: 11, color: 'var(--error)', ...mono, marginTop: 4 }}>{t('macro.aiError')}: {state.error}</div>}
      {state.text && (
        <div className="card" style={{ marginTop: 6, padding: '8px 10px', fontSize: 12, lineHeight: 1.6, color: 'var(--text-primary)', whiteSpace: 'pre-wrap' }}>
          <div style={{ fontSize: 9, color: 'var(--text-muted)', ...mono, marginBottom: 4, textTransform: 'uppercase', letterSpacing: '.06em' }}>
            🤖 {t('macro.aiTitle')} · Someo Park Local Model 120B{state.cached ? ` · ⚡ ${t('macro.aiCached')}` : ''}
          </div>
          {state.text}
        </div>
      )}
    </div>
  );
}

// ── 60s polling hook (usePoll pattern, self-contained macro copy) ─────────────
export function useMacroPoll<T>(fetchFn: () => Promise<T>, pollMs = 60000) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const fnRef = useRef(fetchFn);
  fnRef.current = fetchFn;

  useEffect(() => {
    let cancelled = false;
    const run = () =>
      fnRef.current()
        .then((r) => { if (!cancelled) { setData(r); setError(null); } })
        .catch((e) => { if (!cancelled) setError(e.message); })
        .finally(() => { if (!cancelled) setLoading(false); });
    run();
    const id = setInterval(run, pollMs);
    return () => { cancelled = true; clearInterval(id); };
  }, [pollMs]);

  return { data, loading, error };
}
