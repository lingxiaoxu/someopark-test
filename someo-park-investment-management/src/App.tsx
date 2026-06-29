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
import { AdvanceModeProvider } from './components/prediction/AdvanceMode';
import { useAuth, ViewType } from './hooks/useAuth';
import { supabase } from './lib/supabase';
import { loadUserChats, upsertUserChat, deleteUserChat } from './lib/chatStore';
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
      try {
        localStorage.setItem(key, JSON.stringify(nextValue));
      } catch { /* quota exceeded — ignore */ }
      return nextValue;
    });
  }, [key]);

  return [storedValue, setValue];
}

// Helpers for per-chat message cache in localStorage
function loadChatMessages(chatId: number): Message[] {
  try {
    const raw = localStorage.getItem(`sp-chat-${chatId}`);
    if (!raw) return [];
    const msgs: Message[] = JSON.parse(raw);
    // Sanitize agent messages loaded from history:
    // - Fix pending tool_calls (no matching result → mark interrupted so spinner stops)
    // - Ensure content reflects agentFinalText if content is empty
    return msgs.map(msg => {
      if (!msg.isAgentMessage || !msg.agentSteps || msg.agentSteps.length === 0) return msg;

      const steps = msg.agentSteps.map(step => {
        if (step.type !== 'tool_call') return step;
        if (step.status === 'pending') {
          const hasResult = msg.agentSteps!.some(
            s => s.type === 'tool_result' && (s as any).toolUseId === (step as any).toolUseId
          );
          return { ...step, status: (hasResult ? 'completed' : 'error') as 'completed' | 'error' };
        }
        return step;
      });

      let content = msg.content;
      const first = content[0];
      if (msg.agentFinalText && first && first.type === 'text' && !first.text) {
        content = [{ type: 'text' as const, text: msg.agentFinalText }, ...content.slice(1)];
      }

      return { ...msg, agentSteps: steps, content };
    });
  } catch { return []; }
}
// Strip base64 image data from messages before saving to avoid blowing localStorage quota
function stripBase64ForStorage(messages: Message[]): Message[] {
  const b64re = /!\[.*?\]\(data:image\/[^)]+\)/g;
  const b64placeholder = '![Chart](image stripped for storage)';
  return messages.map(msg => {
    if (!msg.isAgentMessage) return msg;
    let changed = false;
    const steps = msg.agentSteps?.map(step => {
      if (step.type === 'tool_result' && b64re.test(step.toolResult)) {
        changed = true;
        return { ...step, toolResult: step.toolResult.replace(b64re, b64placeholder) };
      }
      return step;
    });
    const content = msg.content.map(c => {
      if (c.type === 'text' && 'text' in c && b64re.test(c.text)) {
        changed = true;
        return { ...c, text: c.text.replace(b64re, b64placeholder) };
      }
      return c;
    });
    return changed ? { ...msg, agentSteps: steps, content } : msg;
  });
}
function saveChatMessages(chatId: number, messages: Message[]) {
  try {
    localStorage.setItem(`sp-chat-${chatId}`, JSON.stringify(stripBase64ForStorage(messages)));
  } catch { /* quota exceeded — silently fail */ }
}
function deleteChatMessages(chatId: number) {
  localStorage.removeItem(`sp-chat-${chatId}`);
}

export default function App() {
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [agentMode, setAgentMode] = useState<AgentMode>('cloud');
  const [isLocalConnected, setIsLocalConnected] = useState(false);
  // Prediction Market mode: 'stock' (default, pixel-identical to before) | 'prediction'.
  // The attribute lives on <html> so the whole viewport (incl. <body>) inverts.
  const [appMode, setAppMode] = useLocalStorage<'stock' | 'prediction'>('sp-appMode', 'stock');
  const [activeArtifact, setActiveArtifact] = useState<any>(null);

  useEffect(() => {
    document.documentElement.setAttribute('data-mode', appMode);
  }, [appMode]);

  const toggleAppMode = useCallback(() => {
    setActiveArtifact(null);   // drop any open artifact so modes never cross-render
    setShowSettings(false);
    setAppMode(m => (m === 'prediction' ? 'stock' : 'prediction'));
  }, [setAppMode]);
  const [lastClosedArtifact, setLastClosedArtifact] = useState<any>(null);
  const [isMaximized, setIsMaximized] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [chatKey, setChatKey] = useState(0);
  const [chatHistory, setChatHistory] = useLocalStorage<ChatEntry[]>('sp-chatHistory', []);
  const [activeChatId, setActiveChatId] = useState<number | null>(null);
  const [initialMessages, setInitialMessages] = useState<Message[]>([]);
  // Ref to track current messages for saving before switching
  const currentMessagesRef = useRef<Message[]>([]);

  // Cross-device chat history sync (Supabase). Refs avoid TDZ since `session` is
  // declared further down; effects below keep them current.
  const sessionRef = useRef<any>(null);
  const chatHistoryRef = useRef<ChatEntry[]>([]);
  const syncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const loadedUserRef = useRef<string | null>(null);

  // Debounced upsert of the active chat to Supabase (per logged-in user).
  const scheduleChatSync = useCallback((chatId: number, messages: Message[]) => {
    if (!supabase) return;
    const snapshot = stripBase64ForStorage(messages);
    if (syncTimerRef.current) clearTimeout(syncTimerRef.current);
    syncTimerRef.current = setTimeout(() => {
      const userId = sessionRef.current?.user?.id;
      if (!userId) return;
      const title = chatHistoryRef.current.find(c => c.id === chatId)?.title || '';
      upsertUserChat(supabase!, userId, { id: chatId, title, messages: snapshot });
    }, 1200);
  }, []);

  const handleMessagesChange = useCallback((messages: Message[]) => {
    currentMessagesRef.current = messages;
    // Auto-save to localStorage (fast local cache) + debounced Supabase sync (cross-device)
    if (activeChatId != null) {
      saveChatMessages(activeChatId, messages);
      scheduleChatSync(activeChatId, messages);
    }
  }, [activeChatId, scheduleChatSync]);

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
    const userId = sessionRef.current?.user?.id;
    if (supabase && userId) deleteUserChat(supabase, userId, chatId);
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
    const title = text.slice(0, 40);
    setChatHistory(prev => {
      const exists = prev.find(c => c.id === chatId);
      if (exists) return prev;
      return [{ id: chatId!, title }, ...prev];
    });
    // Create the row in Supabase right away so the chat shows up on other devices
    const userId = sessionRef.current?.user?.id;
    if (supabase && userId) upsertUserChat(supabase, userId, { id: chatId!, title, messages: [] });
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

  // Keep refs current for the debounced/deferred chat-sync closures above.
  useEffect(() => { sessionRef.current = session; }, [session]);
  useEffect(() => { chatHistoryRef.current = chatHistory; }, [chatHistory]);

  // On login, pull this user's chat history from Supabase (cross-device) and merge
  // it into the sidebar list + hydrate the local message cache. Runs once per user.
  useEffect(() => {
    const userId = session?.user?.id;
    if (!supabase || !userId || loadedUserRef.current === userId) return;
    loadedUserRef.current = userId;
    loadUserChats(supabase, userId).then(remote => {
      if (!remote.length) return;
      remote.forEach(c => saveChatMessages(c.id, c.messages as Message[]));
      setChatHistory(prev => {
        const remoteIds = new Set(remote.map(c => c.id));
        const localOnly = prev.filter(c => !remoteIds.has(c.id));
        return [...remote.map(c => ({ id: c.id, title: c.title })), ...localOnly];
      });
    });
  }, [session, setChatHistory]);

  // Model & Template (persisted in localStorage)
  // Default chat model is the open-source Nemotron (Ollama). Someo Agent mode is exempt —
  // it falls back to Claude in server/routes/agent.ts since it requires the Anthropic API.
  const [languageModel, setLanguageModel] = useLocalStorage<LLMModelConfig>('sp-languageModel', {
    model: 'nemotron-3-super:120b',
  });
  // Morph fast-apply is ON by default; user can turn it off in chat settings (persisted).
  const [useMorphApply, setUseMorphApply] = useLocalStorage('sp-useMorphApply', true);
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

  // One-time migration: switch the default chat model to open-source Nemotron.
  // Runs once per browser (guarded by a flag) so it overrides a previously-stored
  // Claude default just once — afterwards the user's own model choice persists freely.
  useEffect(() => {
    const MIGRATION_FLAG = 'sp-defaultModel-nemotron-v1';
    if (!localStorage.getItem(MIGRATION_FLAG)) {
      setLanguageModel(prev => ({ ...prev, model: 'nemotron-3-super:120b' }));
      localStorage.setItem(MIGRATION_FLAG, '1');
    }
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
    <AdvanceModeProvider>
    <div ref={appRef} className="flex h-full w-full bg-[var(--bg-primary)] text-[var(--text-primary)] overflow-hidden font-sans">
      <div className="shrink-0 z-20 bg-[var(--bg-primary)] relative flex" style={{ width: sidebarWidth }}>
        <div className="flex-1 min-w-0">
          <Sidebar
            onConnectClick={() => setIsModalOpen(true)}
            agentMode={agentMode}
            setAgentMode={setAgentMode}
            appMode={appMode}
            onToggleAppMode={toggleAppMode}
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
              appMode={appMode}
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
                  appMode={appMode}
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
    </AdvanceModeProvider>
    </ArtifactProvider>
  );
}
