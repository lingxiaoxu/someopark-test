import type { TFunction } from 'i18next';

/**
 * The 8 AISS semiconductor subsectors + 10 AEUS electric-utilities subsectors
 * (lowercase keys as they appear in the backend inventory / signal data).
 * Display names are localized via i18n under the `subsectors.*` locale block —
 * see locales/{en,zh,fr}.json.
 */
export const SUBSECTOR_KEYS = [
  // AISS (semiconductor)
  'ai_gpu',
  'custom_asic',
  'equipment',
  'memory_hbm',
  'foundry',
  'analog_defense',
  'logic_cpu',
  'rf_edge',
  // AEUS (electric utilities)
  'nuclear_fuel',
  'gas_midstream',
  'grid_equipment',
  'grid_epc',
  'ipp_wholesale',
  'regulated_mega',
  'regional_utility',
  'dc_power_cooling',
  'renewables_storage',
  'water_cooling',
] as const;

export type SubsectorKey = (typeof SUBSECTOR_KEYS)[number];

/**
 * Localized display name for an AISS/AEUS subsector key, synced with the active
 * language. Falls back to the raw key (uppercased by the call-site CSS) for any
 * value that isn't one of the known subsectors — so other labels render
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
