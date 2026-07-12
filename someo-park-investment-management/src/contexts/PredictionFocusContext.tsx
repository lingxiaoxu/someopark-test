import { createContext, useContext, useEffect, RefObject } from 'react';

/**
 * Cross-artifact country focus. When a country is clicked in one prediction artifact,
 * we navigate to another and pass the target country through this context so the
 * destination view can scroll to that country's row and briefly highlight it.
 *
 *  - `country`  : canonical English name to focus (null = no focus this open).
 *  - `nonce`    : bumped on every click so re-focusing the SAME country re-fires the
 *                 scroll/flash effect (the country string alone may be unchanged).
 *  - `selfType` : the wc_* type of the artifact currently rendering, so a CountryName
 *                 inside it can exclude "this artifact" from its "appears in" list.
 */
export interface PredictionFocus {
  country: string | null;
  nonce: number;
  selfType: string | null;
}

export const PredictionFocusContext = createContext<PredictionFocus>({
  country: null,
  nonce: 0,
  selfType: null,
});

export function usePredictionFocus(): PredictionFocus {
  return useContext(PredictionFocusContext);
}

/**
 * On focus (country/nonce change), find the country's anchor inside `ref`, smooth-scroll
 * it into view, and flash its enclosing row/card for ~2s.
 *
 * The destination view fetches async AND may re-render mid-flight (e.g. the schedule first
 * paints a 3-row upcoming fallback, then swaps to the full 100-row list). Tables key rows
 * positionally, so React REUSES a `<tr key=n>` DOM node while replacing its content — a
 * flash class added imperatively would strand on that node and light up the wrong match.
 * So we watch the subtree with a MutationObserver and, on every change, MOVE the flash to
 * whatever element currently matches `country` (and re-scroll to it), never trusting a
 * previously-flashed node to still be correct.
 */
export function useCountryFocusScroll(
  ref: RefObject<HTMLElement>,
  country: string | null,
  nonce: number,
): void {
  useEffect(() => {
    if (!country) return;
    const sel = `[data-country="${typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(country) : country}"]`;
    let flashed: HTMLElement | null = null;
    let done = false;
    let removeTimer: ReturnType<typeof setTimeout>;
    const clear = () => { if (flashed) { flashed.classList.remove('country-focus-flash'); flashed = null; } };
    const locate = () => {
      if (done) return;
      const el = ref.current?.querySelector(sel) as HTMLElement | null;
      const row = el ? ((el.closest('tr, .card, li') as HTMLElement) || el) : null;
      if (row && row !== flashed) {
        clear();
        row.classList.add('country-focus-flash');
        flashed = row;
        el!.scrollIntoView({ behavior: 'smooth', block: 'center' });
        clearTimeout(removeTimer);                           // 2s of highlight from the LAST settle
        removeTimer = setTimeout(() => { clear(); done = true; obs.disconnect(); }, 2000);
      }
    };
    const obs = new MutationObserver(locate);
    if (ref.current) obs.observe(ref.current, { childList: true, subtree: true });
    const startTimer = setTimeout(locate, 60);               // first look after initial paint
    const capTimer = setTimeout(() => { done = true; obs.disconnect(); clear(); }, 6000); // never observe forever
    return () => {
      done = true; obs.disconnect();
      clearTimeout(startTimer); clearTimeout(removeTimer); clearTimeout(capTimer); clear();
    };
  }, [country, nonce, ref]);
}
