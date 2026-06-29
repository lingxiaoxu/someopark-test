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
 * stock-strategy switcher in ChatArea. `dark=false` uses theme tokens (for panels
 * with the dashboard background); `dark=true` uses the inverted #111/#fff scheme used
 * on the white cards. Reads/writes the shared AdvanceMode context.
 */
export function AdvanceModeToggle({ dark = false }: { dark?: boolean }) {
  const { mode, setMode } = useAdvanceMode();
  const { t } = useTranslation();
  const ink = dark ? '#111' : 'var(--text-primary)';
  const onBg = dark ? '#111' : 'var(--text-primary)';
  const onFg = dark ? '#fff' : 'var(--bg-primary)';
  const offFg = dark ? '#555' : 'var(--text-muted)';
  const offBg = dark ? '#fff' : 'transparent';
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
