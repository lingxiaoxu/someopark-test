import { useState, useRef, useEffect, useCallback } from 'react';
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
import { DeepPartial } from 'ai';

export type AgentMode = 'cloud' | 'local';

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

export default function App() {
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [agentMode, setAgentMode] = useState<AgentMode>('cloud');
  const [isLocalConnected, setIsLocalConnected] = useState(false);
  const [activeArtifact, setActiveArtifact] = useState<any>(null);
  const [showSettings, setShowSettings] = useState(false);

  const [rightPanelWidth, setRightPanelWidth] = useState(480);
  const [isResizing, setIsResizing] = useState(false);
  const appRef = useRef<HTMLDivElement>(null);

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
    const handleMouseMove = (e: MouseEvent) => {
      if (!isResizing || !appRef.current) return;
      const appRect = appRef.current.getBoundingClientRect();
      const newWidth = appRect.right - e.clientX;
      setRightPanelWidth(Math.min(Math.max(newWidth, 320), appRect.width * 0.5));
    };
    const handleMouseUp = () => setIsResizing(false);

    if (isResizing) {
      document.addEventListener('mousemove', handleMouseMove);
      document.addEventListener('mouseup', handleMouseUp);
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
    } else {
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }
    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, [isResizing]);

  const showRightPanel = activeArtifact || codePreview;

  return (
    <ArtifactProvider value={setActiveArtifact}>
    <div ref={appRef} className="flex h-screen w-full bg-[var(--bg-primary)] text-[var(--text-primary)] overflow-hidden font-sans">
      <div className="w-[260px] shrink-0 border-r border-[var(--border-subtle)] z-20 bg-[var(--bg-primary)]">
        <Sidebar
          onConnectClick={() => setIsModalOpen(true)}
          agentMode={agentMode}
          setAgentMode={setAgentMode}
          isLocalConnected={isLocalConnected}
          onSettingsClick={() => { setShowSettings(true); setActiveArtifact(null); setCodePreview(null); }}
          session={session}
          onSignInClick={() => setIsAuthDialogOpen(true)}
          onSignOut={() => supabase?.auth.signOut()}
        />
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
            />
          )}
        </div>
        {showRightPanel && (
          <>
            <div
              className="w-1 hover:w-1.5 cursor-col-resize hover:bg-[var(--accent-primary)] active:bg-[var(--accent-primary)] transition-all z-30 -ml-1"
              onMouseDown={() => setIsResizing(true)}
            />
            <div className="shrink-0 z-20 bg-[var(--bg-primary)]" style={{ width: rightPanelWidth }}>
              {activeArtifact ? (
                <RightPanel artifact={activeArtifact} onClose={() => setActiveArtifact(null)} />
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
