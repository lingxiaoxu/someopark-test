/**
 * AdvanceMode — shared "Regulation Time vs Advances" view mode for Prediction Market.
 *
 * From the knockout stage, every match has TWO real markets: the 90-min 3-way
 * (home/draw/away — "Regulation Time") and the 2-way "who advances" (home/away incl.
 * extra time + penalties — "Advances"). This context holds the user's chosen lens so
 * every selector (Upcoming panel, In-play arb module, Model-vs-Market, Today's
 * Predictions) stays in sync and the views render the SAME match in the SAME mode.
 *
 * Default is 'regulation' (the existing display). Group-stage matches have no advance
 * market, so cards auto-fall back to regulation regardless of this mode.
 *
 * The toggle UI mirrors the stock-mode strategy switcher (segmented, pixel border).
 */
import { createContext, useContext, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

export type AdvanceMode = 'regulation' | 'advance';

type Ctx = { mode: AdvanceMode; setMode: (m: AdvanceMode) => void };
const AdvanceModeContext = createContext<Ctx>({ mode: 'regulation', setMode: () => {} });

export function AdvanceModeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<AdvanceMode>('regulation');
  return <AdvanceModeContext.Provider value={{ mode, setMode }}>{children}</AdvanceModeContext.Provider>;
}

export function useAdvanceMode(): Ctx {
  return useContext(AdvanceModeContext);
}

/**
 * Two-option segmented selector (Regulation Time / Advances), same look as the
 * stock-strategy switcher. Uses theme tokens (selected = --text-primary background) so it
 * renders identically in light + dark and in EVERY location (upcoming panel + artifact title
 * rows) — no per-location variant. Reads/writes the shared AdvanceMode context.
 */
export function AdvanceModeToggle() {
  const { mode, setMode } = useAdvanceMode();
  const { t } = useTranslation();
  // Theme-token styling (consistent in light + dark): selected = --text-primary background.
  // Used identically in every location (upcoming panel + the artifact title rows).
  const ink = 'var(--text-primary)';
  const onBg = 'var(--text-primary)';
  const onFg = 'var(--bg-primary)';
  const offFg = 'var(--text-muted)';
  const offBg = 'transparent';
  const opts: [AdvanceMode, string][] = [
    ['regulation', t('prediction.modeRegulation')],
    ['advance', t('prediction.modeAdvance')],
  ];
  return (
    <div className="flex overflow-hidden" style={{ border: `2px solid ${ink}` }}>
      {opts.map(([val, label], i) => (
        <button
          key={val}
          onClick={() => setMode(val)}
          title={t(val === 'advance' ? 'prediction.modeAdvanceHint' : 'prediction.modeRegulationHint')}
          style={{
            padding: '3px 12px',
            fontSize: '10px',
            fontFamily: 'var(--font-mono)',
            fontWeight: 700,
            letterSpacing: '.06em',
            textTransform: 'uppercase',
            transition: 'all .1s',
            background: mode === val ? onBg : offBg,
            color: mode === val ? onFg : offFg,
            border: 'none',
            borderLeft: i > 0 ? `2px solid ${ink}` : 'none',
            cursor: 'pointer',
            whiteSpace: 'nowrap',
          }}
        >{label}</button>
      ))}
    </div>
  );
}
