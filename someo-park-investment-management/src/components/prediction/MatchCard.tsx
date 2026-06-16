/**
 * MatchCard — one upcoming World Cup match in Prediction Market mode.
 * Shows the two teams + kickoff (ET), our model's 3-way (+O2.5), the sharp book
 * de-vig, and REAL Kalshi / Polymarket US single-match quotes when listed.
 * Reads the shape produced by prediction_market/ops/upcoming_export.py.
 * Pure presentational; colours come from CSS vars so it inverts with the theme.
 */
type ThreeWay = { home: number; draw: number; away: number };
type VenueQuote = {
  home?: { ask: number | null; bid: number | null };
  draw?: { ask: number | null; bid: number | null };
  away?: { ask: number | null; bid: number | null };
  devig?: ThreeWay | null;
} | null;

export type UpcomingMatch = {
  kickoff: string;
  et?: string | null;
  round?: string;
  home: { id: string; name: string; zh?: string };
  away: { id: string; name: string; zh?: string };
  model: ThreeWay & { over_2_5?: number; btts?: number };
  book_devig?: ThreeWay | null;
  kalshi?: VenueQuote;
  poly_us?: VenueQuote;
  edge?: { best?: { side: string; venue: string; net_edge: number; tradable: boolean } | null };
};

const pct = (v?: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`);
const px = (v?: number | null) => (v == null ? '—' : v.toFixed(2));

function Row({ label, t, dim }: { label: string; t?: ThreeWay | null; dim?: boolean }) {
  const c = dim ? 'var(--text-muted)' : 'var(--text-secondary)';
  return (
    <div className="flex items-center gap-2" style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: c }}>
      <span style={{ width: 52, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '.06em' }}>{label}</span>
      <span style={{ width: 64 }}>H {pct(t?.home)}</span>
      <span style={{ width: 64 }}>D {pct(t?.draw)}</span>
      <span style={{ width: 64 }}>A {pct(t?.away)}</span>
    </div>
  );
}

function VenueRow({ label, q }: { label: string; q?: VenueQuote }) {
  if (!q) {
    return (
      <div className="flex items-center gap-2" style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--text-muted)' }}>
        <span style={{ width: 52, textTransform: 'uppercase', letterSpacing: '.06em' }}>{label}</span>
        <span style={{ opacity: 0.7 }}>not listed yet —</span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2" style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}>
      <span style={{ width: 52, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '.06em' }}>{label}</span>
      <span style={{ width: 64 }}>H {px(q.home?.ask)}</span>
      <span style={{ width: 64 }}>D {px(q.draw?.ask)}</span>
      <span style={{ width: 64 }}>A {px(q.away?.ask)}</span>
    </div>
  );
}

export default function MatchCard({ m }: { m: UpcomingMatch }) {
  const best = m.edge?.best;
  const edgeColor = best?.tradable ? 'var(--success)' : 'var(--text-muted)';
  return (
    <div className="pair-card" style={{ display: 'flex', flexDirection: 'column', gap: 6, minWidth: 280, flex: '1 1 320px' }}>
      <div className="flex items-center justify-between">
        <span style={{ fontSize: 13, fontWeight: 700, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)', textTransform: 'uppercase', letterSpacing: '.03em' }}>
          {m.home.name} <span style={{ color: 'var(--text-muted)' }}>vs</span> {m.away.name}
        </span>
        <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{m.et || ''}</span>
      </div>
      <Row label="Model" t={m.model} />
      {(m.model.over_2_5 != null || m.model.btts != null) && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
          O2.5 {pct(m.model.over_2_5)} · BTTS {pct(m.model.btts)}
        </div>
      )}
      <Row label="Book" t={m.book_devig} dim />
      <VenueRow label="Kalshi" q={m.kalshi} />
      <VenueRow label="Poly US" q={m.poly_us} />
      {best && (
        <div style={{ fontSize: 11, fontFamily: 'var(--font-mono)', color: edgeColor, fontWeight: 700 }}>
          Edge {best.venue}/{best.side} {best.net_edge >= 0 ? '+' : ''}{(best.net_edge * 100).toFixed(1)}%
          {best.tradable ? ' ★' : ''}
        </div>
      )}
    </div>
  );
}
