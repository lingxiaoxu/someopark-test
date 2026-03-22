import { MessageSquare, Plus, Terminal, Settings, Cloud, Laptop, LogIn } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Session } from '@supabase/supabase-js';
import { useState, useRef, useEffect } from 'react';
import i18n from '../i18n';

const LANGUAGES = [
  { code: 'en', flag: '🇺🇸', label: 'EN' },
  { code: 'zh', flag: '🇨🇳', label: '中' },
  { code: 'ja', flag: '🇯🇵', label: 'JP' },
  { code: 'fr', flag: '🇫🇷', label: 'FR' },
  { code: 'es', flag: '🇪🇸', label: 'ES' },
];

export default function Sidebar({
  onConnectClick,
  agentMode,
  setAgentMode,
  isLocalConnected,
  onSettingsClick,
  session,
  onSignInClick,
  onSignOut,
}: {
  onConnectClick: () => void,
  agentMode: 'cloud' | 'local',
  setAgentMode: (mode: 'cloud' | 'local') => void,
  isLocalConnected: boolean,
  onSettingsClick?: () => void,
  session: Session | null,
  onSignInClick?: () => void,
  onSignOut?: () => void,
}) {
  const { t } = useTranslation();
  const [currentLang, setCurrentLang] = useState(localStorage.getItem('sp-lang') || 'en');
  const [menuOpen, setMenuOpen] = useState(false);
  const [showAbout, setShowAbout] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const changeLang = (code: string) => {
    i18n.changeLanguage(code);
    localStorage.setItem('sp-lang', code);
    setCurrentLang(code);
  };

  // Close dropdown when clicking outside
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
        setShowAbout(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  return (
    <div className="sidebar flex flex-col h-full">
      <div className="flex items-center gap-2 py-2 mb-4">
        <Terminal className="w-6 h-6 text-[var(--accent-primary)]" />
        <span className="font-semibold text-[var(--text-primary)] tracking-wide">{t('sidebar.appName')}</span>
      </div>

      {/* Agent Mode Selector */}
      <div className="mb-6">
        <div className="text-[10px] font-medium text-[var(--text-muted)] uppercase tracking-wider mb-2">{t('sidebar.agentRuntime')}</div>
        <div className="flex flex-col gap-1.5">
          <button
            onClick={() => setAgentMode('cloud')}
            className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-all ${agentMode === 'cloud' ? 'bg-[var(--bg-tertiary)] text-[var(--text-primary)] border border-[var(--border-subtle)] shadow-sm' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] border border-transparent'}`}
          >
            <Cloud className={`w-4 h-4 ${agentMode === 'cloud' ? 'text-[var(--accent-primary)]' : ''}`} />
            <div className="flex flex-col items-start">
              <span className="font-medium">{t('sidebar.cloudVps')}</span>
              <span className="text-[10px] text-[var(--text-muted)]">{t('sidebar.cloudHosted')}</span>
            </div>
          </button>

          <button
            onClick={() => isLocalConnected ? setAgentMode('local') : onConnectClick()}
            className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-all ${agentMode === 'local' ? 'bg-[var(--bg-tertiary)] text-[var(--text-primary)] border border-[var(--border-subtle)] shadow-sm' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] border border-transparent'}`}
          >
            <Laptop className={`w-4 h-4 ${agentMode === 'local' ? 'text-[var(--success)]' : ''}`} />
            <div className="flex flex-col items-start flex-1">
              <span className="font-medium">{t('sidebar.localOpenClaw')}</span>
              <span className="text-[10px] text-[var(--text-muted)]">{isLocalConnected ? t('sidebar.connected') : t('sidebar.notConnected')}</span>
            </div>
            {!isLocalConnected && <Plus className="w-3.5 h-3.5 text-[var(--text-muted)]" />}
          </button>
        </div>
      </div>

      <button className="button button-primary w-full mb-6">
        <Plus className="w-4 h-4" />
        {t('sidebar.newChat')}
      </button>

      <div className="flex-1 overflow-y-auto flex flex-col gap-1">
        <div className="text-xs font-medium text-[var(--text-muted)] mb-2 uppercase tracking-wider">{t('sidebar.recentChats')}</div>
        <div className="chat-item active flex items-center gap-3">
          <MessageSquare className="w-4 h-4 text-[var(--text-secondary)]" />
          <span className="text-sm truncate">{t('sidebar.chatMrptInventory')}</span>
        </div>
        <div className="chat-item flex items-center gap-3">
          <MessageSquare className="w-4 h-4 text-[var(--text-secondary)]" />
          <span className="text-sm truncate">{t('sidebar.chatWfDiagnostics')}</span>
        </div>
        <div className="chat-item flex items-center gap-3">
          <MessageSquare className="w-4 h-4 text-[var(--text-secondary)]" />
          <span className="text-sm truncate">{t('sidebar.chatDailySignal')}</span>
        </div>
        <div className="chat-item flex items-center gap-3">
          <MessageSquare className="w-4 h-4 text-[var(--text-secondary)]" />
          <span className="text-sm truncate">{t('sidebar.chatPairUniverse')}</span>
        </div>
        <div className="chat-item flex items-center gap-3">
          <MessageSquare className="w-4 h-4 text-[var(--text-secondary)]" />
          <span className="text-sm truncate">{t('sidebar.chatWfGrid')}</span>
        </div>
      </div>

      {/* Bottom area */}
      <div className="mt-auto pt-4 border-t border-[var(--border-subtle)]">

        {/* Language flags - 4 inline icons */}
        <div className="flex items-center gap-1 px-1 mb-3">
          {LANGUAGES.map(lang => (
            <button
              key={lang.code}
              onClick={() => changeLang(lang.code)}
              title={lang.label}
              className={`flex-1 py-1 rounded-md text-base transition-all ${currentLang === lang.code ? 'bg-[var(--bg-tertiary)] ring-1 ring-[var(--accent-primary)]/40' : 'hover:bg-[var(--bg-secondary)] opacity-50 hover:opacity-80'}`}
            >
              {lang.flag}
            </button>
          ))}
        </div>

        {/* Auth section */}
        {session ? (
          <div className="relative" ref={menuRef}>
            <button
              onClick={() => setMenuOpen(prev => !prev)}
              className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg hover:bg-[var(--bg-secondary)] transition-colors"
            >
              <div className="w-7 h-7 rounded-full bg-[var(--accent-primary)]/20 flex items-center justify-center text-xs font-semibold text-[var(--accent-primary)] shrink-0">
                {session.user?.email?.[0]?.toUpperCase() ?? '?'}
              </div>
              <div className="flex-1 min-w-0 text-left">
                <div className="text-xs font-medium text-[var(--text-primary)] truncate">{session.user?.email}</div>
              </div>
            </button>

            {menuOpen && (
              <div className="absolute bottom-full left-0 right-0 mb-1 bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl shadow-xl overflow-hidden z-50">
                {showAbout ? (
                  <>
                    <div className="px-3 py-2 border-b border-[var(--border-subtle)] flex items-center gap-2">
                      <button onClick={() => setShowAbout(false)} className="text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors text-xs">←</button>
                      <div className="text-xs font-medium text-[var(--text-primary)]">About SomeoClaw</div>
                    </div>
                    <div className="px-3 py-3 text-[11px] text-[var(--text-secondary)] leading-relaxed">
                      SomeoClaw is an AI-powered investment research assistant built on Someo Park's quantitative infrastructure. It connects to walk-forward trading strategies (MRPT & MTFS), live inventory, signals, and diagnostics — letting you query, visualize, and build with your data through natural language and code generation.
                    </div>
                  </>
                ) : (
                  <>
                    <div className="px-3 py-2 border-b border-[var(--border-subtle)]">
                      <div className="text-xs font-medium text-[var(--text-primary)]">My Account</div>
                      <div className="text-[11px] text-[var(--text-muted)] truncate">{session.user?.email}</div>
                    </div>
                    <button
                      onClick={() => setShowAbout(true)}
                      className="w-full flex items-center gap-2 px-3 py-2 text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] transition-colors"
                    >
                      <Settings className="w-3.5 h-3.5" />
                      About SomeoClaw
                    </button>
                    <div className="border-t border-[var(--border-subtle)]" />
                    <button
                      onClick={() => { setMenuOpen(false); setShowAbout(false); onSignOut?.(); }}
                      className="w-full flex items-center gap-2 px-3 py-2 text-xs text-red-400 hover:bg-red-400/10 transition-colors"
                    >
                      <span className="w-3.5 h-3.5 text-sm">↩</span>
                      Sign out
                    </button>
                  </>
                )}
              </div>
            )}
          </div>
        ) : (
          <button onClick={onSignInClick} className="chat-item flex items-center gap-3 w-full">
            <LogIn className="w-4 h-4 text-[var(--text-secondary)]" />
            <span className="text-sm">{t('sidebar.signIn')}</span>
          </button>
        )}
      </div>
    </div>
  );
}
