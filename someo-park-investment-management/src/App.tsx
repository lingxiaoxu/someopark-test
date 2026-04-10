import { useState, useRef, useEffect, useCallback } from 'react';
import { PanelLeftOpen } from 'lucide-react';
import Sidebar from './components/Sidebar';
import ChatArea from './components/ChatArea';
import RightPanel from './components/RightPanel';
import ConnectionModal from './components/ConnectionModal';
import SettingsPage from './components/SettingsPage';
import { AuthDialog } from './components/AuthDialog';
import { CodePreview } from './components/CodePreview';
import { ArtifactProvider } from './contexts/ArtifactContext';
import { useAuth, ViewType } from './hooks/useAuth';
import { supabase } from './lib/supabase';
import { LLMModelConfig } from './lib/models';
import { StanseAgentSchema } from './lib/schema';
import { ExecutionResult } from './lib/types';
import { Message } from './lib/messages';
import { DeepPartial } from 'ai';

export type AgentMode = 'cloud' | 'local';

type ChatEntry = { id: number; title: string };

function useLocalStorage<T>(key: string, initialValue: T): [T, (value: T | ((prev: T) => T)) => void] {
  const [storedValue, setStoredValue] = useState<T>(() => {
    try {
      const item = localStorage.getItem(key);
      return item ? JSON.parse(item) : initialValue;
    } catch {
      return initialValue;
    }
  });

  const setValue = useCallback((value: T | ((prev: T) => T)) => {
    setStoredValue(prev => {
      const nextValue = value instanceof Function ? value(prev) : value;
      localStorage.setItem(key, JSON.stringify(nextValue));
      return nextValue;
    });
  }, [key]);

  return [storedValue, setValue];
}

// Helpers for per-chat message cache in localStorage
function loadChatMessages(chatId: number): Message[] {
  try {
    const raw = localStorage.getItem(`sp-chat-${chatId}`);
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}
function saveChatMessages(chatId: number, messages: Message[]) {
  try {
    localStorage.setItem(`sp-chat-${chatId}`, JSON.stringify(messages));
  } catch { /* quota exceeded — silently fail */ }
}
function deleteChatMessages(chatId: number) {
  localStorage.removeItem(`sp-chat-${chatId}`);
}

export default function App() {
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [agentMode, setAgentMode] = useState<AgentMode>('cloud');
  const [isLocalConnected, setIsLocalConnected] = useState(false);
  const [activeArtifact, setActiveArtifact] = useState<any>(null);
  const [lastClosedArtifact, setLastClosedArtifact] = useState<any>(null);
  const [isMaximized, setIsMaximized] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [chatKey, setChatKey] = useState(0);
  const [chatHistory, setChatHistory] = useLocalStorage<ChatEntry[]>('sp-chatHistory', []);
  const [activeChatId, setActiveChatId] = useState<number | null>(null);
  const [initialMessages, setInitialMessages] = useState<Message[]>([]);
  // Ref to track current messages for saving before switching
  const currentMessagesRef = useRef<Message[]>([]);

  const handleMessagesChange = useCallback((messages: Message[]) => {
    currentMessagesRef.current = messages;
    // Auto-save to localStorage
    if (activeChatId != null) {
      saveChatMessages(activeChatId, messages);
    }
  }, [activeChatId]);

  const handleNewChat = useCallback(() => {
    const newId = Date.now();
    setChatKey(k => k + 1);
    setActiveChatId(newId);
    setInitialMessages([]);
    currentMessagesRef.current = [];
    setActiveArtifact(null);
    setCodePreview(null);
    setShowSettings(false);
  }, []);

  const handleSelectChat = useCallback((chatId: number) => {
    if (chatId === activeChatId) return;
    // Load the target chat's messages from cache
    const msgs = loadChatMessages(chatId);
    setActiveChatId(chatId);
    setInitialMessages(msgs);
    currentMessagesRef.current = msgs;
    setChatKey(k => k + 1);
    setActiveArtifact(null);
    setCodePreview(null);
    setShowSettings(false);
  }, [activeChatId]);

  const handleDeleteChat = useCallback((chatId: number) => {
    setChatHistory(prev => prev.filter(c => c.id !== chatId));
    deleteChatMessages(chatId);
    // If deleting the active chat, reset to welcome
    if (chatId === activeChatId) {
      setActiveChatId(null);
      setInitialMessages([]);
      currentMessagesRef.current = [];
      setChatKey(k => k + 1);
    }
  }, [activeChatId, setChatHistory]);

  const handleFirstMessage = useCallback((text: string) => {
    let chatId = activeChatId
    // Auto-create chat if user sends from welcome screen without clicking "New Chat"
    if (chatId == null) {
      chatId = Date.now()
      setActiveChatId(chatId)
    }
    setChatHistory(prev => {
      const exists = prev.find(c => c.id === chatId);
      if (exists) return prev;
      return [{ id: chatId!, title: text.slice(0, 40) }, ...prev];
    });
  }, [activeChatId, setChatHistory]);

  const [rightPanelWidth, setRightPanelWidth] = useState(480);
  const [isResizing, setIsResizing] = useState(false);
  const appRef = useRef<HTMLDivElement>(null);

  // Sidebar resize
  const DEFAULT_SIDEBAR_WIDTH = 260;
  const SIDEBAR_MIN = Math.round(DEFAULT_SIDEBAR_WIDTH * 0.82); // 213
  const SIDEBAR_MAX = Math.round(DEFAULT_SIDEBAR_WIDTH * 1.18); // 307
  const [sidebarWidth, setSidebarWidth] = useState(DEFAULT_SIDEBAR_WIDTH);
  const [isSidebarResizing, setIsSidebarResizing] = useState(false);

  // Auth
  const [isAuthDialogOpen, setIsAuthDialogOpen] = useState(false);
  const [authView, setAuthView] = useState<ViewType>('sign_in');
  const { session } = useAuth(setIsAuthDialogOpen, setAuthView);

  // Model & Template (persisted in localStorage)
  const [languageModel, setLanguageModel] = useLocalStorage<LLMModelConfig>('sp-languageModel', {
    model: 'claude-sonnet-4-5-20250929',
  });
  const [useMorphApply, setUseMorphApply] = useLocalStorage('sp-useMorphApply', false);
  const [selectedTemplate, setSelectedTemplate] = useLocalStorage('sp-selectedTemplate', 'auto');

  // Code preview
  const [codePreview, setCodePreview] = useState<{
    stanseAgent: DeepPartial<StanseAgentSchema>;
    result?: ExecutionResult;
  } | null>(null);
  const [isPreviewLoading, setIsPreviewLoading] = useState(false);

  const handleLanguageModelChange = useCallback((config: LLMModelConfig) => {
    setLanguageModel(prev => ({ ...prev, ...config }));
  }, [setLanguageModel]);

  const handleCodePreview = useCallback((preview: { stanseAgent: DeepPartial<StanseAgentSchema>; result?: ExecutionResult; isLoading?: boolean }) => {
    if (preview.isLoading) {
      setIsPreviewLoading(true);
      setCodePreview({ stanseAgent: preview.stanseAgent });
    } else {
      setIsPreviewLoading(false);
      setCodePreview({ stanseAgent: preview.stanseAgent, result: preview.result });
    }
    setActiveArtifact(null);
  }, []);

  useEffect(() => {
    const handleMove = (clientX: number) => {
      if (!isResizing || !appRef.current) return;
      const appRect = appRef.current.getBoundingClientRect();
      const newWidth = appRect.right - clientX;
      setRightPanelWidth(Math.min(Math.max(newWidth, 320), appRect.width * 0.6));
    };
    const handleMouseMove = (e: MouseEvent) => handleMove(e.clientX);
    const handleTouchMove = (e: TouchEvent) => { e.preventDefault(); handleMove(e.touches[0].clientX); };
    const handleEnd = () => setIsResizing(false);

    if (isResizing) {
      document.addEventListener('mousemove', handleMouseMove);
      document.addEventListener('mouseup', handleEnd);
      document.addEventListener('touchmove', handleTouchMove, { passive: false });
      document.addEventListener('touchend', handleEnd);
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    } else {
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }
    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleEnd);
      document.removeEventListener('touchmove', handleTouchMove);
      document.removeEventListener('touchend', handleEnd);
    };
  }, [isResizing]);

  // Sidebar resize effect
  useEffect(() => {
    const handleMove = (clientX: number) => {
      if (!isSidebarResizing || !appRef.current) return;
      const appRect = appRef.current.getBoundingClientRect();
      const newWidth = clientX - appRect.left;
      setSidebarWidth(Math.min(Math.max(newWidth, SIDEBAR_MIN), SIDEBAR_MAX));
    };
    const handleMouseMove = (e: MouseEvent) => handleMove(e.clientX);
    const handleTouchMove = (e: TouchEvent) => { e.preventDefault(); handleMove(e.touches[0].clientX); };
    const handleEnd = () => setIsSidebarResizing(false);

    if (isSidebarResizing) {
      document.addEventListener('mousemove', handleMouseMove);
      document.addEventListener('mouseup', handleEnd);
      document.addEventListener('touchmove', handleTouchMove, { passive: false });
      document.addEventListener('touchend', handleEnd);
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    } else if (!isResizing) {
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }
    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleEnd);
      document.removeEventListener('touchmove', handleTouchMove);
      document.removeEventListener('touchend', handleEnd);
    };
  }, [isSidebarResizing]);

  const showRightPanel = activeArtifact || codePreview;

  return (
    <ArtifactProvider value={setActiveArtifact}>
    <div ref={appRef} className="flex h-full w-full bg-[var(--bg-primary)] text-[var(--text-primary)] overflow-hidden font-sans">
      <div className="shrink-0 z-20 bg-[var(--bg-primary)] relative flex" style={{ width: sidebarWidth }}>
        <div className="flex-1 min-w-0">
          <Sidebar
            onConnectClick={() => setIsModalOpen(true)}
            agentMode={agentMode}
            setAgentMode={setAgentMode}
            isLocalConnected={isLocalConnected}
            onSettingsClick={() => { setShowSettings(true); setActiveArtifact(null); setCodePreview(null); }}
            session={session}
            onSignInClick={() => setIsAuthDialogOpen(true)}
            onSignOut={() => supabase?.auth.signOut()}
            onNewChat={handleNewChat}
            chatHistory={chatHistory}
            activeChatId={activeChatId}
            onSelectChat={handleSelectChat}
            onDeleteChat={handleDeleteChat}
          />
        </div>
        {/* Sidebar resize handle — touch-friendly with wider hit area */}
        <div
          onMouseDown={() => setIsSidebarResizing(true)}
          onTouchStart={() => setIsSidebarResizing(true)}
          className="cursor-col-resize"
          style={{
            width: '16px',
            position: 'absolute',
            right: 0,
            top: 0,
            bottom: 0,
            zIndex: 30,
            display: 'flex',
            alignItems: 'stretch',
            justifyContent: 'flex-end',
            touchAction: 'none',
          }}
        >
          <div className="hover:w-1.5 active:w-1.5" style={{
            width: isSidebarResizing ? '6px' : '4px',
            background: '#111',
            transition: 'width 0.15s',
          }} />
        </div>
      </div>
      <div className="flex-1 flex min-w-0 relative">
        <div className="flex-1 min-w-0">
          {showSettings ? (
            <SettingsPage onBack={() => setShowSettings(false)} />
          ) : (
            <ChatArea
              agentMode={agentMode}
              isLocalConnected={isLocalConnected}
              setActiveArtifact={(a: any) => { setActiveArtifact(a); setShowSettings(false); setCodePreview(null); }}
              onCodePreview={handleCodePreview}
              languageModel={languageModel}
              onLanguageModelChange={handleLanguageModelChange}
              useMorphApply={useMorphApply}
              onUseMorphApplyChange={setUseMorphApply}
              selectedTemplate={selectedTemplate}
              onSelectedTemplateChange={setSelectedTemplate}
              session={session}
              onSignInClick={() => setIsAuthDialogOpen(true)}
              chatKey={chatKey}
              onFirstMessage={handleFirstMessage}
              onConnectClick={() => setIsModalOpen(true)}
              initialMessages={initialMessages}
              onMessagesChange={handleMessagesChange}
            />
          )}
        </div>
        {/* Reopen button — shown when panel is closed but lastClosedArtifact exists */}
        {!showRightPanel && lastClosedArtifact && (
          <div className="absolute top-3 right-3 z-30">
            <button
              onClick={() => { setActiveArtifact(lastClosedArtifact); setLastClosedArtifact(null); }}
              style={{
                padding: '5px',
                background: 'transparent',
                border: '2px solid transparent',
                cursor: 'pointer',
                transition: 'all .1s',
                color: '#555',
              }}
              onMouseEnter={e => { const el = e.currentTarget; el.style.background = '#111'; el.style.borderColor = '#111'; el.style.color = '#fff'; }}
              onMouseLeave={e => { const el = e.currentTarget; el.style.background = 'transparent'; el.style.borderColor = 'transparent'; el.style.color = '#555'; }}
              title="Reopen panel"
            >
              <PanelLeftOpen className="w-4 h-4" />
            </button>
          </div>
        )}
        {showRightPanel && (
          <>
            <div
              className="cursor-col-resize z-30 flex items-stretch justify-center"
              style={{ width: '16px', marginLeft: '-8px', marginRight: '-8px', touchAction: 'none' }}
              onMouseDown={() => {
                if (isMaximized && appRef.current) {
                  setRightPanelWidth(appRef.current.getBoundingClientRect().width * 0.6);
                }
                setIsMaximized(false);
                setIsResizing(true);
              }}
              onTouchStart={() => {
                if (isMaximized && appRef.current) {
                  setRightPanelWidth(appRef.current.getBoundingClientRect().width * 0.6);
                }
                setIsMaximized(false);
                setIsResizing(true);
              }}
            >
              <div className="w-1 hover:w-1.5 bg-transparent hover:bg-[var(--accent-primary)] active:bg-[var(--accent-primary)] transition-all" />
            </div>
            <div className="shrink-0 z-20 bg-[var(--bg-primary)] overflow-hidden" style={{ width: isMaximized ? (appRef.current ? appRef.current.getBoundingClientRect().width * 0.6 : rightPanelWidth) : rightPanelWidth }}>
              {activeArtifact ? (
                <RightPanel
                  artifact={activeArtifact}
                  onClose={() => { setLastClosedArtifact(activeArtifact); setActiveArtifact(null); setIsMaximized(false); }}
                  onMaximize={() => setIsMaximized(m => !m)}
                  isMaximized={isMaximized}
                />
              ) : codePreview ? (
                <CodePreview
                  stanseAgent={codePreview.stanseAgent}
                  result={codePreview.result}
                  isChatLoading={false}
                  isPreviewLoading={isPreviewLoading}
                  onClose={() => { setCodePreview(null); setIsPreviewLoading(false); }}
                />
              ) : null}
            </div>
          </>
        )}
      </div>
      {isModalOpen && (
        <ConnectionModal
          onClose={() => setIsModalOpen(false)}
          onConnect={() => { setIsLocalConnected(true); setAgentMode('local'); setIsModalOpen(false); }}
        />
      )}
      {isAuthDialogOpen && supabase && (
        <AuthDialog open={isAuthDialogOpen} setOpen={setIsAuthDialogOpen} supabase={supabase} view={authView} />
      )}
    </div>
    </ArtifactProvider>
  );
}
