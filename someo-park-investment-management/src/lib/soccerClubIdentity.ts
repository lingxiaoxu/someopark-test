/** Pure, shared identity resolver. No React, browser globals, network or filesystem.
 * IDs never change when a display name or spelling changes. The catalog covers the
 * current reviewed roster; future IDs retain their source name until reviewed.
 */
import catalog from '../../../prediction_market_soccer/config/club_identity.json';

export type ClubIdentityRecord = {
  club_id: string;
  api_football_id?: number | string | null;
  source_name: string;
  competitions?: string[];
  aliases?: string[];
};
export type SoccerClubRef = string | number | {
  club_id?: string; team_id?: string | number; id?: string | number;
  api_football_id?: string | number; api_team_id?: string | number;
  name?: string; zh?: string;
} | null | undefined;

/** Typography only: compatibility width, canonical Unicode, case and punctuation.
 * Deliberately keeps words and accents; no substring or club-prefix deletion.
 */
export function normalizeClubName(value: unknown): string {
  return String(value ?? '').normalize('NFKC').toLowerCase()
    .replace(/[‘’ʼ`´]/g, "'").replace(/[‐‑‒–—−]/g, '-')
    .replace(/\s+/gu, ' ').trim().normalize('NFC');
}

/** Latin-only accent folding. Japanese dakuten/handakuten stay significant. */
export function foldClubName(value: unknown): string {
  let latin = false, out = '';
  for (const char of normalizeClubName(value).normalize('NFKD')) {
    if (/\p{M}/u.test(char)) { if (!latin) out += char; }
    else { latin = /\p{Script=Latin}/u.test(char); out += char; }
  }
  const extra: Record<string, string> = { 'ø':'o', 'ł':'l', 'ß':'ss', 'ı':'i', 'æ':'ae', 'œ':'oe', 'đ':'d' };
  return out.replace(/[øłßıæœđ]/g, (c) => extra[c]).normalize('NFC');
}

const unique = (ids?: Set<string>): string | null => ids?.size === 1 ? [...ids][0] : null;
function add(index: Map<string, Set<string>>, key: string, id: string) {
  if (!key) return;
  if (!index.has(key)) index.set(key, new Set());
  index.get(key)!.add(id);
}

export function createClubIdentityIndex(records: readonly ClubIdentityRecord[]) {
  const byId = new Map<string, ClubIdentityRecord>();
  const byApi = new Map<string, Set<string>>();
  const exact = new Map<string, Set<string>>();
  const folded = new Map<string, Set<string>>();
  for (const rec of records) {
    byId.set(rec.club_id, rec);
    if (rec.api_football_id != null) add(byApi, String(rec.api_football_id), rec.club_id);
    for (const form of [rec.club_id, rec.source_name, ...(rec.aliases || [])]) {
      add(exact, normalizeClubName(form), rec.club_id);
      add(folded, foldClubName(form), rec.club_id);
    }
  }
  const candidates = (label: unknown): Set<string> => {
    const raw = String(label ?? '').trim();
    if (byId.has(raw)) return new Set([raw]);
    // An exact collision is not rescued by a looser spelling.
    return new Set(exact.get(normalizeClubName(raw)) ?? folded.get(foldClubName(raw)) ?? []);
  };
  const explicit = (value: unknown, apiOnly = false): string | null => {
    const raw = String(value ?? '').trim();
    if (!apiOnly && byId.has(raw)) return raw;
    return unique(byApi.get(raw));
  };
  const resolve = (ref: SoccerClubRef): string | null => {
    if (ref == null) return null;
    if (typeof ref === 'number') return explicit(ref, true);
    if (typeof ref === 'string') return unique(candidates(ref));
    const fields = [ref.club_id, ref.team_id, ref.id].filter((v) => v != null && v !== '');
    const api = [ref.api_football_id, ref.api_team_id].filter((v) => v != null && v !== '');
    if (fields.length || api.length) {
      const ids = [...fields.map((v) => explicit(v)), ...api.map((v) => explicit(v, true))];
      if (ids.some((v) => !v) || new Set(ids).size !== 1) return null;
      return ids[0];
    }
    const hits = [ref.name, ref.zh].filter(Boolean).map(candidates).filter((s) => s.size > 0);
    if (!hits.length) return null;
    return unique(new Set([...hits[0]].filter((id) => hits.every((s) => s.has(id)))));
  };
  return { byId, byApi, exact, folded, candidates, resolve };
}

export const soccerClubCatalog: readonly ClubIdentityRecord[] = catalog.clubs;
export const soccerClubIdentity = createClubIdentityIndex(soccerClubCatalog);
export const resolveClubId = soccerClubIdentity.resolve;

/** Only unambiguous keys are exposed to older consumers of clubIndex.byName. */
export function uniqueClubNameMap(): Map<string, string> {
  return new Map([...soccerClubIdentity.folded].flatMap(([key, ids]) => {
    const id = unique(ids); return id ? [[key, id] as [string, string]] : [];
  }));
}
