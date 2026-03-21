import React from 'react';
import { useTranslation } from 'react-i18next';
import { Globe, ArrowLeft } from 'lucide-react';

const languages = [
  { code: 'en', label: 'English', flag: '🇺🇸' },
  { code: 'zh', label: '中文', flag: '🇨🇳' },
  { code: 'ja', label: '日本語', flag: '🇯🇵' },
  { code: 'fr', label: 'Français', flag: '🇫🇷' },
  { code: 'es', label: 'Español', flag: '🇪🇸' },
];

export default function SettingsPage({ onBack }: { onBack: () => void }) {
  const { t, i18n } = useTranslation();

  const changeLang = (code: string) => {
    i18n.changeLanguage(code);
    localStorage.setItem('sp-lang', code);
  };

  const current = languages.find(l => l.code === i18n.language) || languages[0];

  return (
    <div className="flex flex-col h-full p-6">
      <div className="flex items-center gap-3 mb-6">
        <button
          onClick={onBack}
          className="p-1.5 rounded-lg hover:bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
        >
          <ArrowLeft className="w-5 h-5" />
        </button>
        <h2 className="text-lg font-semibold text-[var(--text-primary)]">{t('settingsPage.title')}</h2>
      </div>

      <div className="space-y-6 max-w-lg">
        {/* Language */}
        <div className="bg-[var(--bg-secondary)] border border-[var(--border-subtle)] rounded-xl p-5">
          <div className="flex items-center gap-3 mb-3">
            <div className="p-2 bg-[var(--accent-primary)]/10 rounded-lg">
              <Globe className="w-5 h-5 text-[var(--accent-primary)]" />
            </div>
            <div>
              <h3 className="text-sm font-medium text-[var(--text-primary)]">{t('settingsPage.languageLabel')}</h3>
              <p className="text-xs text-[var(--text-muted)] mt-0.5">{t('settingsPage.languageDesc')}</p>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-2 mt-4">
            {languages.map(lang => (
              <button
                key={lang.code}
                onClick={() => changeLang(lang.code)}
                className={`flex items-center gap-3 px-4 py-3 rounded-lg text-sm transition-all border ${
                  current.code === lang.code
                    ? 'bg-[var(--accent-primary)]/10 border-[var(--accent-primary)]/40 text-[var(--text-primary)] shadow-sm'
                    : 'bg-[var(--bg-primary)] border-[var(--border-subtle)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]'
                }`}
              >
                <span className="text-lg">{lang.flag}</span>
                <span className="font-medium">{lang.label}</span>
                {current.code === lang.code && (
                  <span className="ml-auto text-[10px] font-semibold uppercase tracking-wider text-[var(--accent-primary)]">
                    {t('common.active')}
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
