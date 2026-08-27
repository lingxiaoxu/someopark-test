/**
 * soccerLabels — the translation helpers every Club Soccer view shares.
 *
 * They live outside SoccerArtifact.tsx because the match card and the welcome card
 * need the same labels, and importing them from the artifact would make the module
 * graph circular (SoccerArtifact already imports SoccerMatchCard).
 *
 * Working rule: anything the BACKEND emits as an enum — fixture status, round name,
 * milestone code, quote source, gate flag — is rendered through a locale key here and
 * never printed raw. The exporters speak English (and occasionally Chinese) only, so a
 * raw token is readable to at most one of the five audiences we ship.
 */
import { useTranslation } from 'react-i18next';

export type ChipLeague = { league: string; name?: string; zh?: string };
type TFn = (k: string, o?: any) => string;

/** t() that returns '' instead of the key itself when nothing is defined. */
function opt(t: TFn | undefined, key: string, args?: any): string {
  if (!t) return '';
  const v = String(t(key, { ...(args || {}), defaultValue: '' }) ?? '');
  return v === key ? '' : v;
}

// ── competitions ─────────────────────────────────────────────────────────────
/** Competition name from soccer.league.<id> (all five languages). The backend name
 * is the fallback for a competition added before its translation lands; the bare id
 * is the last resort — never upper-cased, because a shouted id reads as a code. */
export function leagueLabel(l: ChipLeague, lang: string, t?: TFn): string {
  const id = l.league || '';
  const v = id ? opt(t, `soccer.league.${id}`) : '';
  if (v) return v;
  if ((lang || '').startsWith('zh') && l.zh) return l.zh;
  return l.name || id;
}

// ── fixture status (API-Football codes) ──────────────────────────────────────
export function statusLabel(code: any, t?: TFn): string {
  const c = String(code ?? '').trim().toUpperCase();
  if (!c) return '';
  return opt(t, `soccer.status.${c}`) || c;
}

// ── round / stage names ──────────────────────────────────────────────────────
// API-Football writes rounds as "<stage> - <n>" for the league-style families and as a
// bare stage name for knockout ties. Splitting on that pattern is the whole parser: the
// numbered families take {{n}}, the fixed ones don't, and an unrecognised stage is shown
// exactly as the backend wrote it (better a stray English name than a wrong translation).
const NUMBERED_STAGE: Record<string, string> = {
  'regular season': 'regularSeason',
  'group stage': 'groupStage',
  'league stage': 'leagueStage',
  'league phase': 'leagueStage',
  clausura: 'clausura',
  apertura: 'apertura',
  phase: 'phase',
  '1st phase': 'phase',
  '2nd phase': 'phase',
};
const FIXED_STAGE: Record<string, string> = {
  'round of 32': 'roundOf32',
  'round of 16': 'roundOf16',
  '8th finals': 'roundOf16',
  'quarter-finals': 'quarterFinals',
  'quarterfinals': 'quarterFinals',
  'semi-finals': 'semiFinals',
  'semifinals': 'semiFinals',
  final: 'final',
  'grand final': 'final',
  '3rd place final': 'thirdPlace',
  'play-offs': 'playOffs',
  playoffs: 'playOffs',
  'playoff round': 'playoffRound',
  'play-off round': 'playoffRound',
  'knockout round play-offs': 'knockoutPlayoffs',
  'preliminary round': 'preliminaryRound',
  '1st qualifying round': 'qualifying1',
  '2nd qualifying round': 'qualifying2',
  '3rd qualifying round': 'qualifying3',
};

export function stageLabel(round: any, t?: TFn): string {
  const raw = String(round ?? '').trim();
  if (!raw || !t) return raw;
  const m = raw.match(/^(.*?)\s*[-–—]\s*(\d+)$/);
  const name = (m ? m[1] : raw).trim().toLowerCase();
  if (m) {
    const key = NUMBERED_STAGE[name];
    return key ? opt(t, `soccer.stage.${key}`, { n: Number(m[2]) }) || raw : raw;
  }
  const key = FIXED_STAGE[name];
  return key ? opt(t, `soccer.stage.${key}`) || raw : raw;
}

// ── outcome sides ────────────────────────────────────────────────────────────
/** home/draw/away (or the backend's H/D/A) → the one-letter column label. */
export function sideAbbr(side: any, t: TFn): string {
  const s = String(side ?? '').trim().toLowerCase();
  const key = s === 'home' || s === 'h' ? 'abbrHome'
    : s === 'draw' || s === 'd' ? 'abbrDraw'
      : s === 'away' || s === 'a' ? 'abbrAway' : '';
  return key ? t(`soccer.${key}`) : String(side ?? '');
}

// ── locale-aware formatting ──────────────────────────────────────────────────
// Intl always follows the UI language, never the browser's: a French user reading the
// French UI must not get "8/26/2026, 7:13:34 PM" or "200,000".
const asLocale = (lang?: string) => (lang ? lang : undefined);

export function fmtDateTime(v: any, lang?: string): string {
  if (!v) return '';
  const d = new Date(v);
  return isNaN(d.getTime()) ? String(v) : d.toLocaleString(asLocale(lang));
}
export function fmtDate(v: any, lang?: string): string {
  if (!v) return '';
  // "2026-08-26" is a calendar day, not an instant: new Date() reads it as UTC midnight,
  // which renders as the PREVIOUS day for every reader west of Greenwich.
  const ymd = String(v).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  const d = ymd ? new Date(Number(ymd[1]), Number(ymd[2]) - 1, Number(ymd[3])) : new Date(v);
  return isNaN(d.getTime())
    ? String(v)
    : d.toLocaleDateString(asLocale(lang), { year: 'numeric', month: 'short', day: 'numeric', weekday: 'short' });
}
export function fmtTime(v: any, lang?: string): string {
  if (!v) return '';
  const d = new Date(v);
  return isNaN(d.getTime()) ? String(v) : d.toLocaleTimeString(asLocale(lang));
}
export function fmtInt(v: any, lang?: string): string {
  return typeof v === 'number' && !isNaN(v) ? v.toLocaleString(asLocale(lang)) : '—';
}
/** USD amounts in the reader's convention (fr: "1 504,58 $US"). */
export function fmtMoney(v: any, lang?: string): string {
  if (typeof v !== 'number' || isNaN(v)) return '—';
  try {
    return new Intl.NumberFormat(asLocale(lang), { style: 'currency', currency: 'USD' }).format(v);
  } catch {
    return `$${v.toFixed(2)}`;
  }
}

// ── backend prose ────────────────────────────────────────────────────────────
// Exports carry a `<field>_key` / `<field>_i18n` next to the English (sometimes Chinese)
// string; the string is only the fallback for an export that has not been keyed yet.
const NOTE_PREFIXES = ['soccer.msg.', 'soccer.msg.notes.', 'prediction.note.', 'soccer.'];
const HAN = /[一-鿿]/;

/** An unkeyed backend string is only safe to print when it is in the reader's own
 * script. frontend_overview's headline/mode are written in Chinese, and handing those
 * to an en/ja/es/fr reader is the same leak as handing English to a Chinese one. */
function safeFallback(fallback: string | null | undefined, lang: string): string {
  const s = fallback || '';
  if (!s) return '';
  return HAN.test(s) && !(lang || '').startsWith('zh') ? '' : s;
}

export function useLocalizedNote() {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || '';
  return (fallback?: string | null, key?: string | null, args?: any): string => {
    if (key) {
      for (const p of NOTE_PREFIXES) {
        const v = opt(t as unknown as TFn, p + key, args);
        if (v) return v;
      }
    }
    return safeFallback(fallback, lang);
  };
}

/** List form: prefers the structured [{key,args}] export, falls back per-item to the
 * plain prose array the exporter also ships. */
export function useLocalizedNotes() {
  const noteOf = useLocalizedNote();
  return (items?: any, keyed?: any): string[] => {
    const prose: string[] = Array.isArray(items) ? items.map(String) : items ? [String(items)] : [];
    if (Array.isArray(keyed) && keyed.length) {
      return keyed
        .map((n: any, i: number) => (typeof n === 'string'
          ? noteOf(prose[i], n)
          : noteOf(prose[i], n?.key, n?.args)))
        .filter(Boolean);
    }
    return prose.map((p) => noteOf(p)).filter(Boolean);
  };
}
