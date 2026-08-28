import { createContext, useContext, useEffect, RefObject } from 'react';

/**
 * Cross-artifact CLUB focus — the soccer mirror of PredictionFocusContext (附录 C-31).
 *
 * When a club is clicked in one soccer artifact we navigate to another and pass the
 * target through this context so the destination can scroll to that club's row and
 * briefly highlight it.
 *
 *  - `club`     : canonical club_id to focus (null = no focus this open). An ID, not a
 *                 name: the same club is spelled differently per source and per language,
 *                 so a name-based anchor would miss in exactly the languages that need it.
 *  - `nonce`    : bumped on every click so re-focusing the SAME club re-fires the
 *                 scroll/flash (the id alone may be unchanged).
 *  - `selfType` : the soccer_* type currently rendering, so a ClubName inside it can
 *                 exclude "this artifact" from its "appears in" list.
 */
export interface SoccerFocus {
  club: string | null;
  nonce: number;
  selfType: string | null;
}

export const SoccerFocusContext = createContext<SoccerFocus>({
  club: null,
  nonce: 0,
  selfType: null,
});

export function useSoccerFocus(): SoccerFocus {
  return useContext(SoccerFocusContext);
}

/**
 * On focus (club/nonce change), find the club's anchor inside `ref`, smooth-scroll it
 * into view, and flash its enclosing row/card for ~2s.
 *
 * Same hazard as the World Cup version, and it bites harder here: a destination view
 * fetches async and re-renders mid-flight (the schedule paints a short fallback, then
 * swaps to the full list), while tables key rows positionally — so React REUSES a
 * `<tr key=n>` DOM node and replaces its content. A flash class added imperatively
 * would strand on that node and light up the wrong club. We therefore watch the subtree
 * and, on every change, MOVE the flash to whatever element currently matches, never
 * trusting a previously-flashed node to still be right.
 */
export function useClubFocusScroll(
  ref: RefObject<HTMLElement>,
  club: string | null,
  nonce: number,
): void {
  useEffect(() => {
    if (!club) return;
    const esc = typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(club) : club;
    const sel = `[data-club="${esc}"]`;
    let flashed: HTMLElement | null = null;
    let done = false;
    let removeTimer: ReturnType<typeof setTimeout>;
    const clear = () => { if (flashed) { flashed.classList.remove('country-focus-flash'); flashed = null; } };
    const locate = () => {
      if (done) return;
      const el = ref.current?.querySelector(sel) as HTMLElement | null;
      const row = el ? ((el.closest('tr, .card, .pair-card, li') as HTMLElement) || el) : null;
      if (row && row !== flashed) {
        clear();
        row.classList.add('country-focus-flash');
        flashed = row;
        el!.scrollIntoView({ behavior: 'smooth', block: 'center' });
        clearTimeout(removeTimer);                    // 2s of highlight from the LAST settle
        removeTimer = setTimeout(() => { clear(); done = true; obs.disconnect(); }, 2000);
      }
    };
    const obs = new MutationObserver(locate);
    if (ref.current) obs.observe(ref.current, { childList: true, subtree: true });
    const startTimer = setTimeout(locate, 60);        // first look after the initial paint
    const capTimer = setTimeout(() => { done = true; obs.disconnect(); clear(); }, 6000);
    return () => {
      done = true; obs.disconnect();
      clearTimeout(startTimer); clearTimeout(removeTimer); clearTimeout(capTimer); clear();
    };
  }, [club, nonce, ref]);
}
