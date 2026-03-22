import React, { useState, useEffect, useRef, useCallback } from 'react'
import { Activity, Terminal, Cloud, Laptop, LoaderIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Message, ArtifactTrigger } from '../lib/messages'
import { StanseAgentSchema } from '../lib/schema'
import { ExecutionResult } from '../lib/types'
import { LLMModelConfig } from '../lib/models'
import { ChatInput } from './ChatInput'
import { ChatPicker, LLMModel } from './ChatPicker'
import { ChatSettings } from './ChatSettings'
import { DeepPartial } from 'ai'
import templates from '../lib/templates'
import modelList from '../lib/models.json'
import PairBadge from './PairBadge'
import { useApi } from '../hooks/useApi'
import { getInventory } from '../lib/api'

export default function ChatArea({
  agentMode,
  isLocalConnected,
  setActiveArtifact,
  onCodePreview,
  languageModel,
  onLanguageModelChange,
  useMorphApply,
  onUseMorphApplyChange,
  selectedTemplate,
  onSelectedTemplateChange,
}: {
  agentMode: 'cloud' | 'local'
  isLocalConnected: boolean
  setActiveArtifact: (a: any) => void
  onCodePreview?: (preview: { stanseAgent: DeepPartial<StanseAgentSchema>; result?: ExecutionResult; isLoading?: boolean }) => void
  languageModel: LLMModelConfig
  onLanguageModelChange: (config: LLMModelConfig) => void
  useMorphApply: boolean
  onUseMorphApplyChange: (v: boolean) => void
  selectedTemplate: string
  onSelectedTemplateChange: (t: string) => void
}) {
  const { t } = useTranslation()
  const [input, setInput] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [isErrored, setIsErrored] = useState(false)
  const [errorMessage, setErrorMessage] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [currentStanseAgent, setCurrentStanseAgent] = useState<DeepPartial<StanseAgentSchema> | null>(null)
  const [selectedStrategy, setSelectedStrategy] = useState<'mrpt' | 'mtfs'>('mrpt')
  const chatContainerRef = useRef<HTMLDivElement>(null)
  const abortControllerRef = useRef<AbortController | null>(null)

  const { data: mrptInv } = useApi(() => getInventory('mrpt'), [])
  const { data: mtfsInv } = useApi(() => getInventory('mtfs'), [])
  const currentInv = selectedStrategy === 'mrpt' ? mrptInv : mtfsInv
  const activePairs = currentInv ? Object.entries(currentInv.pairs || {}).filter(([, p]: any) => (p as any).direction !== null) : []

  const models = modelList as LLMModel[]
  const currentModel = models.find(m => m.id === languageModel.model) || models[0]
  const isMultiModal = currentModel?.multiModal ?? false

  // Auto-scroll to bottom
  useEffect(() => {
    if (chatContainerRef.current) {
      chatContainerRef.current.scrollTop = chatContainerRef.current.scrollHeight
    }
  }, [messages, isLoading])

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault()
    if (!input.trim() || isLoading) return

    const userMessage: Message = {
      role: 'user',
      content: [{ type: 'text', text: input.trim() }],
    }

    setMessages(prev => [...prev, userMessage])
    setInput('')
    setIsLoading(true)
    setIsErrored(false)

    const controller = new AbortController()
    abortControllerRef.current = controller

    try {
      const endpoint = useMorphApply && currentStanseAgent ? '/api/morph-chat' : '/api/chat'

      const msgPayload = [...messages, userMessage].map(m => ({
        role: m.role,
        content: m.content.map(c => c.type === 'code' ? { type: 'text', text: c.text } : c),
      }))

      const body = useMorphApply && currentStanseAgent
        ? { messages: msgPayload, model: currentModel, config: languageModel, currentStanseAgent }
        : { messages: msgPayload, model: currentModel, config: languageModel }

      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      })

      if (!response.ok) {
        const err = await response.json().catch(() => ({ error: 'Request failed' }))
        throw new Error(err.error || `HTTP ${response.status}`)
      }

      const text = await response.text()

      // Check for artifacts marker
      let artifacts: ArtifactTrigger[] = []
      let responseText = text
      const artifactIdx = text.indexOf('\n__ARTIFACTS__')
      if (artifactIdx !== -1) {
        responseText = text.substring(0, artifactIdx)
        try {
          artifacts = JSON.parse(text.substring(artifactIdx + '\n__ARTIFACTS__'.length))
        } catch {}
      }

      // Try to parse as stanseAgent JSON
      let parsedAgent: DeepPartial<StanseAgentSchema> | null = null
      let commentary = ''
      try {
        const parsed = JSON.parse(responseText)
        if (parsed.commentary) {
          commentary = parsed.commentary
          if (parsed.code && parsed.file_path) {
            parsedAgent = parsed
            setCurrentStanseAgent(parsed)
          }
          if (parsed._artifacts) {
            artifacts = [...artifacts, ...parsed._artifacts]
          }
        }
      } catch {
        commentary = responseText
      }

      const assistantMessage: Message = {
        role: 'assistant',
        content: [{ type: 'text', text: commentary }],
        object: parsedAgent || undefined,
        artifacts: artifacts.length > 0 ? artifacts : undefined,
      }

      setMessages(prev => [...prev, assistantMessage])

      // Auto-open the first detected artifact in RightPanel
      if (artifacts.length > 0) {
        const first = artifacts[0]
        setActiveArtifact({ type: first.type, title: first.title, params: first.params })
      }

      // If code was generated, open preview immediately then run sandbox
      if (parsedAgent?.code && parsedAgent.template && parsedAgent.template !== 'chat-response') {
        // Open code panel right away (no result yet, preview tab will show loading)
        if (onCodePreview) {
          onCodePreview({ stanseAgent: parsedAgent, isLoading: true })
        }
        try {
          const sandboxRes = await fetch('/api/sandbox', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ stanseAgent: parsedAgent }),
          })
          if (sandboxRes.ok) {
            const result = await sandboxRes.json() as ExecutionResult
            setMessages(prev => {
              const updated = [...prev]
              const lastAssistant = updated[updated.length - 1]
              if (lastAssistant?.role === 'assistant') {
                lastAssistant.result = result
              }
              return [...updated]
            })
            if (onCodePreview && parsedAgent) {
              onCodePreview({ stanseAgent: parsedAgent, result })
            }
          }
        } catch (err) {
          console.error('Sandbox error:', err)
          // Still open preview with just the code
          if (onCodePreview && parsedAgent) {
            onCodePreview({ stanseAgent: parsedAgent })
          }
        }
      }
    } catch (err: any) {
      if (err.name !== 'AbortError') {
        setIsErrored(true)
        setErrorMessage(err.message || 'Something went wrong')
      }
    } finally {
      setIsLoading(false)
      abortControllerRef.current = null
    }
  }, [input, isLoading, messages, currentModel, languageModel, useMorphApply, currentStanseAgent, onCodePreview])

  const stop = useCallback(() => {
    abortControllerRef.current?.abort()
    setIsLoading(false)
  }, [])

  const retry = useCallback(() => {
    setIsErrored(false)
    setErrorMessage('')
    const lastUserIdx = messages.map(m => m.role).lastIndexOf('user')
    if (lastUserIdx >= 0) {
      const lastUserMsg = messages[lastUserIdx]
      const textContent = lastUserMsg.content.find(c => c.type === 'text')
      if (textContent && 'text' in textContent) {
        setInput(textContent.text)
        setMessages(prev => prev.slice(0, lastUserIdx))
      }
    }
  }, [messages])

  const handleInputChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value)
  }, [])

  const renderArtifactButton = (artifact: ArtifactTrigger) => (
    <button
      key={`${artifact.type}-${artifact.title}`}
      onClick={() => setActiveArtifact({ type: artifact.type, title: artifact.title, params: artifact.params })}
      className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-xs text-[var(--text-primary)]"
    >
      <Activity className="w-3.5 h-3.5 text-[var(--accent-primary)]" />
      {artifact.title}
    </button>
  )

  const hasMessages = messages.length > 0

  return (
    <div className="flex flex-col h-full bg-[var(--bg-primary)] relative">
      {/* Header */}
      <div className="h-14 border-b border-[var(--border-subtle)] flex items-center justify-between px-6 shrink-0">
        <div className="flex items-center gap-3">
          <span className="text-sm font-medium text-[var(--text-secondary)]">{t('chat.currentRuntime')}</span>
          {agentMode === 'cloud' ? (
            <div className="flex items-center gap-2 px-2.5 py-1 rounded-full bg-[var(--bg-tertiary)] border border-[var(--border-subtle)]">
              <Cloud className="w-3.5 h-3.5 text-[var(--accent-primary)]" />
              <span className="text-xs font-mono text-[var(--text-primary)]">{t('chat.cloudVpsLabel')}</span>
            </div>
          ) : (
            <div className="flex items-center gap-2 px-2.5 py-1 rounded-full bg-[var(--bg-tertiary)] border border-[var(--border-subtle)]">
              <Laptop className="w-3.5 h-3.5 text-[var(--success)]" />
              <span className="text-xs font-mono text-[var(--text-primary)]">{t('chat.localConnectedLabel')}</span>
            </div>
          )}
        </div>
      </div>

      {/* Messages or Welcome */}
      <div ref={chatContainerRef} className="flex-1 overflow-y-auto px-6 pt-4 pb-2 flex flex-col items-center">
        <div className="w-full max-w-3xl flex flex-col gap-4 pb-2">
          {!hasMessages ? (
            <>
              <div className="flex flex-col items-center justify-center py-6 gap-4">
                <div className="w-12 h-12 rounded-full bg-[var(--bg-tertiary)] flex items-center justify-center border border-[var(--border-subtle)]">
                  <Terminal className="w-6 h-6 text-[var(--accent-primary)]" />
                </div>
                <h2 className="text-lg font-semibold text-[var(--text-primary)]">{t('chat.welcomeTitle')}</h2>
                <p className="text-sm text-[var(--text-muted)] text-center max-w-md">{t('chat.welcomeDesc')}</p>
              </div>

              <div className="p-4 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)]">
                <div className="flex items-center justify-between mb-3">
                  <div className="text-xs font-medium text-[var(--text-muted)]">{t('chat.activePairs')} ({activePairs.length})</div>
                  <div className="flex rounded-lg overflow-hidden border border-[var(--border-subtle)]">
                    <button
                      onClick={() => setSelectedStrategy('mrpt')}
                      className={`px-3 py-1 text-xs font-medium transition-colors ${selectedStrategy === 'mrpt' ? 'bg-[var(--accent-primary)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]'}`}
                    >MRPT</button>
                    <button
                      onClick={() => setSelectedStrategy('mtfs')}
                      className={`px-3 py-1 text-xs font-medium transition-colors ${selectedStrategy === 'mtfs' ? 'bg-[var(--accent-primary)] text-white' : 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]'}`}
                    >MTFS</button>
                  </div>
                </div>
                {activePairs.length > 0 ? (
                  <div className="flex flex-wrap gap-2">
                    {activePairs.map(([key, pos]: any) => (
                      <span key={key}>
                        <PairBadge pair={key} direction={pos.direction} strategy={selectedStrategy}
                          details={{ s1Shares: pos.s1_shares, s2Shares: pos.s2_shares, s1Price: pos.open_s1_price, s2Price: pos.open_s2_price, openDate: pos.open_date, hedgeRatio: pos.open_hedge_ratio, paramSet: pos.param_set, zScore: pos.open_signal?.z_score }} />
                      </span>
                    ))}
                  </div>
                ) : (
                  <div className="text-xs text-[var(--text-muted)] py-2">{t('chat.noActivePairs', 'No active positions')}</div>
                )}
              </div>

              <div className="grid grid-cols-2 gap-2">
                <button onClick={() => setActiveArtifact({ type: 'pair_universe', title: 'Pair Universe', params: { strategy: selectedStrategy } })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnPairUniverse')}
                </button>
                <button onClick={() => setActiveArtifact({ type: 'table', title: 'Trading Signals', params: { strategy: selectedStrategy } })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnSignals')}
                </button>
                <button onClick={() => setActiveArtifact({ type: 'inventory', title: 'Current Inventory', params: { strategy: selectedStrategy } })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnCurrentInventory')}
                </button>
                <button onClick={() => setActiveArtifact({ type: 'inventory_history', title: 'Inventory History', params: { strategy: selectedStrategy } })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnInventoryHistory')}
                </button>
                <button onClick={() => setActiveArtifact({ type: 'daily_report', title: 'Daily Report' })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnDailyReport')}
                </button>
                <button onClick={() => setActiveArtifact({ type: 'dashboard', title: 'Macro Regime Status' })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnRegime')}
                </button>
                <button onClick={() => setActiveArtifact({ type: 'chart', title: 'OOS Equity Curve', params: { strategy: selectedStrategy } })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnOosEquity')}
                </button>
                <button onClick={() => setActiveArtifact({ type: 'oos_pair_summary', title: 'OOS Pair Summary', params: { strategy: selectedStrategy } })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnOosPairSummary')}
                </button>
                <button onClick={() => setActiveArtifact({ type: 'wf_grid', title: 'Walk-Forward Grid', params: { strategy: selectedStrategy } })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnDsrGrid')}
                </button>
                <button onClick={() => setActiveArtifact({ type: 'wf_summary', title: 'Walk-Forward Summary', params: { strategy: selectedStrategy } })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnWfSummary')}
                </button>
                <button onClick={() => setActiveArtifact({ type: 'wf_diagnostic', title: 'WF Diagnostic Report' })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnWfDiagnostic')}
                </button>
                <button onClick={() => setActiveArtifact({ type: 'portfolio_history', title: 'Portfolio History', params: { strategy: selectedStrategy } })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnMonitorHistory')}
                </button>
                <button onClick={() => setActiveArtifact({ type: 'wf_structure', title: 'WF File Structure' })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnWfStructure')}
                </button>
              </div>
            </>
          ) : (
            messages.map((msg, idx) => (
              <div key={idx} className={msg.role === 'user' ? 'message-user' : 'message-ai'}>
                {msg.role === 'assistant' && (
                  <div className="w-8 h-8 rounded-full bg-[var(--bg-tertiary)] flex items-center justify-center shrink-0 border border-[var(--border-subtle)]">
                    <Terminal className="w-4 h-4 text-[var(--accent-primary)]" />
                  </div>
                )}
                <div className={msg.role === 'user' ? '' : 'message-content w-full'}>
                  {msg.content.map((c, ci) => {
                    if (c.type === 'text') return <p key={ci} className="text-sm text-[var(--text-primary)] leading-relaxed whitespace-pre-wrap">{c.text}</p>
                    if (c.type === 'image') return <img key={ci} src={c.image} alt="" className="w-16 h-16 rounded-lg object-cover" />
                    return null
                  })}
                  {msg.object && (
                    <div onClick={() => onCodePreview?.({ stanseAgent: msg.object!, result: msg.result })}
                      className="mt-3 py-2 pl-2 pr-4 w-fit flex items-center border border-[var(--border-subtle)] rounded-xl cursor-pointer hover:bg-[var(--bg-tertiary)] transition-colors">
                      <div className="rounded-lg w-10 h-10 bg-[var(--bg-tertiary)] flex items-center justify-center">
                        <Terminal strokeWidth={2} className="text-[var(--accent-primary)]" />
                      </div>
                      <div className="pl-2 flex flex-col">
                        <span className="font-semibold text-sm text-[var(--text-primary)]">{msg.object.title}</span>
                        <span className="text-xs text-[var(--text-muted)]">{t('chat.clickToPreview')}</span>
                      </div>
                    </div>
                  )}
                  {msg.artifacts && msg.artifacts.length > 0 && (
                    <div className="mt-3 flex flex-wrap gap-2">
                      {msg.artifacts.map(a => renderArtifactButton(a))}
                    </div>
                  )}
                </div>
              </div>
            ))
          )}

          {isLoading && (
            <div className="message-ai">
              <div className="w-8 h-8 rounded-full bg-[var(--bg-tertiary)] flex items-center justify-center shrink-0 border border-[var(--border-subtle)] shimmer">
                <Terminal className="w-4 h-4 text-[var(--text-muted)]" />
              </div>
              <div className="message-content justify-center">
                <div className="flex items-center gap-1.5 text-sm text-[var(--text-muted)]">
                  <LoaderIcon strokeWidth={2} className="animate-spin w-4 h-4" />
                  <span>{t('chat.agentThinking')}</span>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Input Area */}
      <div className="p-6 pt-0 shrink-0 flex justify-center">
        <div className="w-full max-w-3xl">
          <ChatInput
            retry={retry}
            isErrored={isErrored}
            errorMessage={errorMessage}
            isLoading={isLoading}
            stop={stop}
            input={input}
            handleInputChange={handleInputChange}
            handleSubmit={handleSubmit}
            isMultiModal={isMultiModal}
            files={files}
            handleFileChange={setFiles}
          >
            <ChatPicker
              templates={templates}
              selectedTemplate={selectedTemplate}
              onSelectedTemplateChange={onSelectedTemplateChange}
              models={models}
              languageModel={languageModel}
              onLanguageModelChange={onLanguageModelChange}
            />
            <div className="flex-1" />
            <ChatSettings
              apiKeyConfigurable={true}
              baseURLConfigurable={currentModel?.providerId === 'ollama'}
              languageModel={languageModel}
              onLanguageModelChange={onLanguageModelChange}
              useMorphApply={useMorphApply}
              onUseMorphApplyChange={onUseMorphApplyChange}
            />
          </ChatInput>
        </div>
      </div>
    </div>
  )
}
