import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import en from './locales/en.json';
import zh from './locales/zh.json';
import ja from './locales/ja.json';
import fr from './locales/fr.json';
import es from './locales/es.json';

// Languages we ship UI strings for (both stock mode and prediction-market mode).
export const SUPPORTED_LANGS = ['en', 'zh', 'ja', 'fr', 'es'] as const;

/**
 * Initial language selection (applies to BOTH stock + prediction modes):
 *   1. an explicit choice the user previously saved (sp-lang) — never overridden
 *   2. otherwise the browser / system language (navigator.languages, in order),
 *      matched on the primary subtag (zh-CN→zh, en-GB→en, pt-BR→unsupported→skip)
 *   3. otherwise English
 * Works on mobile and desktop (navigator.language(s) is available on both).
 * Chinese is the secondary fallback for any missing key (see fallbackLng).
 */
function detectInitialLang(): string {
  const supported = SUPPORTED_LANGS as readonly string[];

  // 1. Explicit saved choice wins.
  try {
    const saved = localStorage.getItem('sp-lang');
    if (saved && supported.includes(saved)) return saved;
  } catch { /* localStorage unavailable (private mode / SSR) — fall through */ }

  // 2. Browser / system language, most-preferred first.
  if (typeof navigator !== 'undefined') {
    const prefs = navigator.languages && navigator.languages.length
      ? navigator.languages
      : [navigator.language];
    for (const tag of prefs) {
      if (!tag) continue;
      const primary = tag.toLowerCase().split('-')[0];
      if (supported.includes(primary)) return primary;
    }
  }

  // 3. Default to English.
  return 'en';
}

i18n.use(initReactI18next).init({
  resources: { en: { translation: en }, zh: { translation: zh }, ja: { translation: ja }, fr: { translation: fr }, es: { translation: es } },
  lng: detectInitialLang(),
  // Missing-key fallback chain: English first, then Chinese (per product rule).
  fallbackLng: ['en', 'zh'],
  supportedLngs: SUPPORTED_LANGS as unknown as string[],
  interpolation: { escapeValue: false },
});

export default i18n;
