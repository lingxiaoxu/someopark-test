/**
 * TrajectoryPlayer — interactive ⚽ + players replay rendered from a sim's trajectory.jsonl.
 *
 * Lazy-loads ONE sim's 10MB trajectory (never all 20) from the Express /sim mount, parses the
 * 4000 per-tick frames ({iter,min,ball{pos},players[{team,pos,hasBall}]}), and animates a
 * portrait pitch on a <canvas> with play/pause, a scrub slider, and 1×/2×/4× speed. The GIF in
 * the parent is the instant preview; this is the interactive view. Engine pitch is ~680×1050.
 */
import { useEffect as useEff, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

const FIELD_W = 680, FIELD_H = 1050;     // engine coordinate space (observed bounds, portrait)
const CW = 300, CH = Math.round((CW * FIELD_H) / FIELD_W);  // canvas display size, same aspect
const BASE_STEP = 3;                      // frames advanced per animation tick at 1× (~22s full match)

type Frame = { min: number; ball: [number, number]; players: { t: 0 | 1; x: number; y: number; b: boolean }[] };

export function TrajectoryPlayer({ src, homeName, awayName }: { src: string; homeName: string; awayName: string }) {
  const { t } = useTranslation();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const framesRef = useRef<Frame[]>([]);
  const idxRef = useRef(0);
  const rafRef = useRef<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nFrames, setNFrames] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [idx, setIdx] = useState(0);          // mirrors idxRef for the slider/readout
  const [speed, setSpeed] = useState(1);

  // Lazy-load + parse the trajectory for THIS sim only.
  useEff(() => {
    let aborted = false;
    const ctrl = new AbortController();
    setLoading(true); setError(null); setPlaying(false); idxRef.current = 0; setIdx(0);
    fetch(src, { cache: 'no-store', signal: ctrl.signal })
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.text(); })
      .then((txt) => {
        if (aborted) return;
        const out: Frame[] = [];
        for (const line of txt.split('\n')) {
          if (!line) continue;
          try {
            const o = JSON.parse(line);
            out.push({
              min: o.min,
              ball: [o.ball.pos[0], o.ball.pos[1]],
              players: o.players.map((p: any) => ({ t: p.team === 'home' ? 0 : 1, x: p.pos[0], y: p.pos[1], b: !!p.hasBall })),
            });
          } catch { /* skip a malformed tick */ }
        }
        framesRef.current = out;
        setNFrames(out.length);
        setLoading(false);
        draw(0);
      })
      .catch((e) => { if (!aborted) { setError(String(e?.message || e)); setLoading(false); } });
    return () => { aborted = true; ctrl.abort(); framesRef.current = []; if (rafRef.current) cancelAnimationFrame(rafRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src]);

  // Animation loop.
  useEff(() => {
    if (!playing) { if (rafRef.current) cancelAnimationFrame(rafRef.current); return; }
    const tick = () => {
      const frames = framesRef.current;
      let i = idxRef.current + Math.max(1, Math.round(BASE_STEP * speed));
      if (i >= frames.length - 1) { i = frames.length - 1; setPlaying(false); }
      idxRef.current = i; setIdx(i); draw(i);
      if (i < frames.length - 1) rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, speed]);

  function sx(x: number) { return (x / FIELD_W) * CW; }
  function sy(y: number) { return (y / FIELD_H) * CH; }

  function draw(i: number) {
    const cv = canvasRef.current; if (!cv) return;
    const ctx = cv.getContext('2d'); if (!ctx) return;
    // pitch
    ctx.fillStyle = '#1f7a3f'; ctx.fillRect(0, 0, CW, CH);
    ctx.strokeStyle = 'rgba(255,255,255,.55)'; ctx.lineWidth = 1.2;
    ctx.strokeRect(6, 6, CW - 12, CH - 12);                         // touchlines
    ctx.beginPath(); ctx.moveTo(6, CH / 2); ctx.lineTo(CW - 6, CH / 2); ctx.stroke();   // halfway
    ctx.beginPath(); ctx.arc(CW / 2, CH / 2, 34, 0, Math.PI * 2); ctx.stroke();          // centre circle
    for (const gy of [6, CH - 6 - 46]) { ctx.strokeRect(CW / 2 - 52, gy, 104, 46); }     // penalty boxes
    const f = framesRef.current[i]; if (!f) return;
    // players
    for (const p of f.players) {
      ctx.beginPath(); ctx.arc(sx(p.x), sy(p.y), 5.5, 0, Math.PI * 2);
      ctx.fillStyle = p.t === 0 ? '#3b82f6' : '#ef4444';            // home blue / away red
      ctx.fill();
      if (p.b) { ctx.lineWidth = 2; ctx.strokeStyle = '#fde047'; ctx.stroke(); }          // ball-holder ring
    }
    // ball
    ctx.beginPath(); ctx.arc(sx(f.ball[0]), sy(f.ball[1]), 3.5, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff'; ctx.fill(); ctx.lineWidth = 1; ctx.strokeStyle = '#111'; ctx.stroke();
  }

  const onScrub = (v: number) => { setPlaying(false); idxRef.current = v; setIdx(v); draw(v); };
  const mono = { fontFamily: 'var(--font-mono)' } as const;
  const cur = framesRef.current[idx];

  if (error) return <div style={{ fontSize: 11, color: 'var(--error)', ...mono }}>{t('prediction.mfTrajError')}: {error}</div>;

  return (
    <div style={{ ...mono }}>
      <div style={{ position: 'relative', width: CW, margin: '0 auto' }}>
        <canvas ref={canvasRef} width={CW} height={CH} style={{ width: CW, height: CH, borderRadius: 4, display: 'block', background: '#1f7a3f' }} />
        {loading && (
          <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 11, color: '#fff', background: 'rgba(0,0,0,.35)', borderRadius: 4 }}>
            {t('prediction.mfTrajLoading')}
          </div>
        )}
      </div>
      {/* legend + minute */}
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10, color: 'var(--text-muted)', margin: '4px 2px 2px' }}>
        <span><span style={{ color: '#3b82f6' }}>●</span> {homeName}　<span style={{ color: '#ef4444' }}>●</span> {awayName}</span>
        <span>{cur ? `${cur.min.toFixed(0)}'` : '—'} · {nFrames ? `${idx + 1}/${nFrames}` : ''}</span>
      </div>
      {/* controls */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 2 }}>
        <button
          onClick={() => { if (idxRef.current >= nFrames - 1) onScrub(0); setPlaying((p) => !p); }}
          disabled={loading || !nFrames}
          style={{ padding: '2px 10px', fontSize: 11, fontFamily: 'var(--font-mono)', fontWeight: 700, border: '1px solid var(--accent-primary)', borderRadius: 4, background: 'var(--bg-tertiary)', color: 'var(--text-primary)', cursor: 'pointer' }}>
          {playing ? t('prediction.mfPause') : t('prediction.mfPlay')}
        </button>
        <input type="range" min={0} max={Math.max(0, nFrames - 1)} value={idx} onChange={(e) => onScrub(Number(e.target.value))} disabled={loading || !nFrames} style={{ flex: 1 }} />
        {[1, 2, 4].map((s) => (
          <button key={s} onClick={() => setSpeed(s)} style={{ padding: '2px 6px', fontSize: 10, fontFamily: 'var(--font-mono)', fontWeight: speed === s ? 700 : 400, border: `1px solid ${speed === s ? 'var(--accent-primary)' : 'var(--border-subtle)'}`, borderRadius: 4, background: speed === s ? 'var(--bg-tertiary)' : 'transparent', color: speed === s ? 'var(--text-primary)' : 'var(--text-muted)', cursor: 'pointer' }}>{s}×</button>
        ))}
      </div>
    </div>
  );
}
