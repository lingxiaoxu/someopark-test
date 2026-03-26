import { MessageSquare, Plus, Terminal, Settings, Cloud, Laptop, LogIn, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Session } from '@supabase/supabase-js';
import { useState, useRef, useEffect } from 'react';
import i18n from '../i18n';

const LANGUAGES = [
  { code: 'en', flag: 'EN', label: 'EN' },
  { code: 'zh', flag: 'ZH', label: 'ZH' },
  { code: 'ja', flag: 'JP', label: 'JP' },
  { code: 'fr', flag: 'FR', label: 'FR' },
  { code: 'es', flag: 'ES', label: 'ES' },
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
  onNewChat,
  chatHistory,
  activeChatId,
  onSelectChat,
  onDeleteChat,
}: {
  onConnectClick: () => void,
  agentMode: 'cloud' | 'local',
  setAgentMode: (mode: 'cloud' | 'local') => void,
  isLocalConnected: boolean,
  onSettingsClick?: () => void,
  session: Session | null,
  onSignInClick?: () => void,
  onSignOut?: () => void,
  onNewChat?: () => void,
  chatHistory?: { id: number; title: string }[],
  activeChatId?: number | null,
  onSelectChat?: (id: number) => void,
  onDeleteChat?: (id: number) => void,
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
      {/* Logo */}
      <div className="flex items-center gap-2 py-2 mb-5" style={{ borderBottom: '3px solid #111', paddingBottom: '12px' }}>
        <Terminal className="w-5 h-5" style={{ color: '#111' }} />
        <span style={{ fontFamily: 'var(--font-pixel)', fontSize: '20px', color: '#111', letterSpacing: '.06em', lineHeight: 1 }}>{t('sidebar.appName')}</span>
      </div>

      {/* Agent Mode Selector */}
      <div className="mb-5">
        <div className="section-label">{t('sidebar.agentRuntime')}</div>
        <div className="flex flex-col gap-2">
          <button
            onClick={() => setAgentMode('cloud')}
            className="flex items-center gap-3 px-3 py-2.5 transition-all"
            style={{
              background: agentMode === 'cloud' ? '#111' : '#fff',
              border: '2px solid #111',
              borderLeft: agentMode === 'cloud' ? '4px solid #111' : '2px solid #111',
              color: agentMode === 'cloud' ? '#fff' : '#333',
              boxShadow: agentMode === 'cloud' ? 'var(--shadow-pixel-sm)' : 'none',
              fontFamily: 'var(--font-mono)',
              cursor: 'pointer',
            }}
          >
            <Cloud className="w-4 h-4" style={{ color: agentMode === 'cloud' ? '#fff' : '#555' }} />
            <div className="flex flex-col items-start">
              <span style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase' }}>{t('sidebar.cloudVps')}</span>
              <span style={{ fontSize: '10px', color: agentMode === 'cloud' ? '#ccc' : '#888' }}>{t('sidebar.cloudHosted')}</span>
            </div>
          </button>

          <button
            onClick={() => isLocalConnected ? setAgentMode('local') : onConnectClick()}
            className="flex items-center gap-3 px-3 py-2.5 transition-all"
            style={{
              background: agentMode === 'local' ? '#111' : '#fff',
              border: '2px solid #111',
              borderLeft: agentMode === 'local' ? '4px solid #111' : '2px solid #111',
              color: agentMode === 'local' ? '#fff' : '#333',
              boxShadow: agentMode === 'local' ? 'var(--shadow-pixel-sm)' : 'none',
              fontFamily: 'var(--font-mono)',
              cursor: 'pointer',
            }}
          >
            <Laptop className="w-4 h-4" style={{ color: agentMode === 'local' ? '#fff' : '#555' }} />
            <div className="flex flex-col items-start flex-1">
              <span style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase' }}>{t('sidebar.localOpenClaw')}</span>
              <span style={{ fontSize: '10px', color: agentMode === 'local' ? '#ccc' : '#888' }}>{isLocalConnected ? t('sidebar.connected') : t('sidebar.notConnected')}</span>
            </div>
            {!isLocalConnected && <Plus className="w-3.5 h-3.5" style={{ color: '#888' }} />}
          </button>
        </div>
      </div>

      {/* New Chat button */}
      <button className="button button-primary w-full mb-5" onClick={onNewChat}>
        <Plus className="w-4 h-4" />
        {t('sidebar.newChat')}
      </button>

      <div className="flex-1 overflow-y-auto flex flex-col gap-1">
        <div className="text-xs font-medium text-[var(--text-muted)] mb-2 uppercase tracking-wider">{t('sidebar.recentChats')}</div>
        {/* Dynamic chat history from user sessions */}
        {chatHistory && chatHistory.map(chat => (
          <div
            key={chat.id}
            className={`chat-item flex items-center gap-3 group ${chat.id === activeChatId ? 'active' : ''}`}
            onClick={() => onSelectChat?.(chat.id)}
            style={{ cursor: 'pointer' }}
          >
            <MessageSquare className="w-4 h-4 text-[var(--text-secondary)] shrink-0" />
            <span className="text-sm truncate flex-1">{chat.title}</span>
            <button
              onClick={(e) => { e.stopPropagation(); onDeleteChat?.(chat.id); }}
              className="opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
              style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px' }}
              title="Delete chat"
            >
              <Trash2 className="w-3 h-3 text-[var(--text-muted)] hover:text-red-400" />
            </button>
          </div>
        ))}
        {/* Default placeholder chats (shown when no history) */}
        {(!chatHistory || chatHistory.length === 0) && (
          <>
            <div className={`chat-item flex items-center gap-3 ${activeChatId == null ? 'active' : ''}`}>
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
          </>
        )}
      </div>

      {/* Bottom area */}
      <div className="mt-auto pt-4" style={{ borderTop: '2px solid var(--border-subtle)' }}>

        {/* Language flags */}
        <div className="flex items-center gap-1 px-1 mb-3">
          {LANGUAGES.map(lang => (
            <button
              key={lang.code}
              onClick={() => changeLang(lang.code)}
              title={lang.label}
              style={{
                flex: 1,
                padding: '4px 0',
                background: currentLang === lang.code ? '#111' : '#fff',
                color: currentLang === lang.code ? '#fff' : '#555',
                border: '2px solid #111',
                boxShadow: currentLang === lang.code ? '2px 2px 0 0 #111' : 'none',
                cursor: 'pointer',
                fontFamily: 'var(--font-mono)',
                fontSize: '10px',
                fontWeight: 700,
                letterSpacing: '.06em',
                transition: 'all .1s',
              }}
              onMouseEnter={e => {
                if (currentLang !== lang.code) {
                  (e.currentTarget as HTMLElement).style.background = '#f4f4f4'
                }
              }}
              onMouseLeave={e => {
                if (currentLang !== lang.code) {
                  (e.currentTarget as HTMLElement).style.background = '#fff'
                }
              }}
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
              <div className="absolute bottom-full left-0 right-0 mb-1 overflow-hidden z-50 animate-slide-in" style={{ background: '#fff', border: '2px solid #111', boxShadow: 'var(--shadow-pixel)' }}>
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
