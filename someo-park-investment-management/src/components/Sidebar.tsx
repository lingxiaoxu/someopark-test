import { MessageSquare, Plus, Terminal, Settings, Cloud, Laptop, LogIn, LogOut, User } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Session } from '@supabase/supabase-js';

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

      <div className="mt-auto pt-4 border-t border-[var(--border-subtle)] space-y-1">
        {session ? (
          <div className="flex items-center gap-3 px-3 py-2 rounded-lg">
            <div className="w-7 h-7 rounded-full bg-[var(--bg-tertiary)] flex items-center justify-center border border-[var(--border-subtle)]">
              <User className="w-3.5 h-3.5 text-[var(--text-secondary)]" />
            </div>
            <div className="flex-1 min-w-0">
              <span className="text-xs text-[var(--text-primary)] truncate block">{session.user?.email}</span>
            </div>
            {onSignOut && (
              <button onClick={onSignOut} className="p-1 text-[var(--text-muted)] hover:text-[var(--text-primary)] transition-colors" title={t('sidebar.signOut')}>
                <LogOut className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        ) : (
          <button onClick={onSignInClick} className="chat-item flex items-center gap-3 w-full">
            <LogIn className="w-4 h-4 text-[var(--text-secondary)]" />
            <span className="text-sm">{t('sidebar.signIn')}</span>
          </button>
        )}

        <div className="chat-item flex items-center gap-3 cursor-pointer" onClick={onSettingsClick}>
          <Settings className="w-4 h-4 text-[var(--text-secondary)]" />
          <span className="text-sm">{t('common.settings')}</span>
        </div>
      </div>
    </div>
  );
}
