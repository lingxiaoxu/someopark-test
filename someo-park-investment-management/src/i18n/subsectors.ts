import type { TFunction } from 'i18next';

/**
 * The 8 AISS semiconductor subsectors (lowercase keys as they appear in the
 * backend inventory / signal data). Display names are localized via i18n under
 * the `subsectors.*` locale block — see locales/{en,zh,fr}.json.
 */
export const SUBSECTOR_KEYS = [
  'ai_gpu',
  'custom_asic',
  'equipment',
  'memory_hbm',
  'foundry',
  'analog_defense',
  'logic_cpu',
  'rf_edge',
] as const;

export type SubsectorKey = (typeof SUBSECTOR_KEYS)[number];

/**
 * Localized display name for an AISS subsector key, synced with the active
 * language. Falls back to the raw key (uppercased by the call-site CSS) for any
 * value that isn't one of the 8 known subsectors — so non-AISS labels render
 * unchanged.
 *
 *   subsectorName('ai_gpu', t)  // zh → "AI / GPU", fr → "IA / GPU"
 */
export function subsectorName(key: string | null | undefined, t: TFunction): string {
  if (!key) return key ?? '';
  const k = String(key).toLowerCase();
  const tr = t(`subsectors.${k}`, { defaultValue: '' });
  return tr || String(key);
}
