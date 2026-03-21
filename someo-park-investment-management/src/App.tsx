/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { useState, useRef, useEffect } from 'react';
import Sidebar from './components/Sidebar';
import ChatArea from './components/ChatArea';
import RightPanel from './components/RightPanel';
import ConnectionModal from './components/ConnectionModal';
import SettingsPage from './components/SettingsPage';
import { ArtifactProvider } from './contexts/ArtifactContext';

export type AgentMode = 'cloud' | 'local';

export default function App() {
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [agentMode, setAgentMode] = useState<AgentMode>('cloud');
  const [isLocalConnected, setIsLocalConnected] = useState(false);
  const [activeArtifact, setActiveArtifact] = useState<any>(null);
  const [showSettings, setShowSettings] = useState(false);
  
  const [rightPanelWidth, setRightPanelWidth] = useState(480);
  const [isResizing, setIsResizing] = useState(false);
  const appRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isResizing || !appRef.current) return;
      const appRect = appRef.current.getBoundingClientRect();
      const newWidth = appRect.right - e.clientX;
      const maxWidth = appRect.width * 0.5;
      const minWidth = 320;
      setRightPanelWidth(Math.min(Math.max(newWidth, minWidth), maxWidth));
    };

    const handleMouseUp = () => {
      setIsResizing(false);
    };

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

  return (
    <ArtifactProvider value={setActiveArtifact}>
    <div ref={appRef} className="flex h-screen w-full bg-[var(--bg-primary)] text-[var(--text-primary)] overflow-hidden font-sans">
      <div className="w-[260px] shrink-0 border-r border-[var(--border-subtle)] z-20 bg-[var(--bg-primary)]">
        <Sidebar
          onConnectClick={() => setIsModalOpen(true)}
          agentMode={agentMode}
          setAgentMode={setAgentMode}
          isLocalConnected={isLocalConnected}
          onSettingsClick={() => { setShowSettings(true); setActiveArtifact(null); }}
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
              setActiveArtifact={(a: any) => { setActiveArtifact(a); setShowSettings(false); }}
            />
          )}
        </div>
        {activeArtifact && (
          <>
            <div 
              className="w-1 hover:w-1.5 cursor-col-resize hover:bg-[var(--accent-primary)] active:bg-[var(--accent-primary)] transition-all z-30 -ml-1"
              onMouseDown={() => setIsResizing(true)}
            />
            <div className="shrink-0 z-20 bg-[var(--bg-primary)]" style={{ width: rightPanelWidth }}>
              <RightPanel artifact={activeArtifact} onClose={() => setActiveArtifact(null)} />
            </div>
          </>
        )}
      </div>
      {isModalOpen && (
        <ConnectionModal 
          onClose={() => setIsModalOpen(false)} 
          onConnect={() => {
            setIsLocalConnected(true);
            setAgentMode('local');
            setIsModalOpen(false);
          }}
        />
      )}
    </div>
    </ArtifactProvider>
  );
}
