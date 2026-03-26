import React, { useState, useEffect, useRef, useCallback } from 'react'
import { Activity, Terminal, Cloud, Laptop, LoaderIcon, ChevronDown } from 'lucide-react'
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

/** Lightweight Markdown-to-JSX renderer for chat messages */
function renderMarkdown(text: string): React.ReactNode[] {
  const lines = text.split('\n')
  const result: React.ReactNode[] = []
  let listItems: React.ReactNode[] = []
  let listLevel = 0 // 0 = not in list

  const flushList = () => {
    if (listItems.length > 0) {
      result.push(<ul key={`ul-${result.length}`} style={{ margin: '4px 0', paddingLeft: '1.2em', listStyleType: 'disc' }}>{listItems}</ul>)
      listItems = []
      listLevel = 0
    }
  }

  const inlineBold = (s: string): React.ReactNode[] => {
    const parts: React.ReactNode[] = []
    const re = /\*\*(.+?)\*\*/g
    let last = 0
    let m: RegExpExecArray | null
    while ((m = re.exec(s)) !== null) {
      if (m.index > last) parts.push(s.slice(last, m.index))
      parts.push(<strong key={m.index}>{m[1]}</strong>)
      last = m.index + m[0].length
    }
    if (last < s.length) parts.push(s.slice(last))
    return parts
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]

    // Headings: # ## ###
    const hMatch = line.match(/^(#{1,3})\s+(.+)$/)
    if (hMatch) {
      flushList()
      const level = hMatch[1].length
      const sizes = { 1: '16px', 2: '14px', 3: '13px' } as Record<number, string>
      const weights = { 1: 800, 2: 700, 3: 600 } as Record<number, number>
      result.push(
        <div key={`h-${i}`} style={{ fontSize: sizes[level], fontWeight: weights[level], margin: '8px 0 4px', lineHeight: 1.4 }}>
          {inlineBold(hMatch[2])}
        </div>
      )
      continue
    }

    // List items: - or * or numbered (1.)
    const liMatch = line.match(/^(\s*)[-*]\s+(.+)$/) || line.match(/^(\s*)\d+\.\s+(.+)$/)
    if (liMatch) {
      const indent = liMatch[1].length
      if (listItems.length === 0) listLevel = indent
      listItems.push(<li key={`li-${i}`} style={{ marginBottom: '2px' }}>{inlineBold(liMatch[2])}</li>)
      continue
    }

    // Empty line
    if (line.trim() === '') {
      flushList()
      result.push(<div key={`br-${i}`} style={{ height: '8px' }} />)
      continue
    }

    // Regular paragraph
    flushList()
    result.push(<span key={`p-${i}`}>{inlineBold(line)}{'\n'}</span>)
  }

  flushList()
  return result
}
import modelList from '../lib/models.json'
import PairBadge from './PairBadge'
import { useApi } from '../hooks/useApi'
import { getInventory, API_BASE, apiHeaders } from '../lib/api'
import { db } from '../lib/firebase'
import { collection, addDoc, onSnapshot, serverTimestamp } from 'firebase/firestore'
import { Session } from '@supabase/supabase-js'

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
  session,
  chatKey,
  onFirstMessage,
  onConnectClick,
  initialMessages,
  onMessagesChange,
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
  session?: Session | null
  chatKey?: number
  onFirstMessage?: (text: string) => void
  onConnectClick?: () => void
  initialMessages?: Message[]
  onMessagesChange?: (messages: Message[]) => void
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
  const [runtimeDropdownOpen, setRuntimeDropdownOpen] = useState(false)
  const runtimeDropdownRef = useRef<HTMLDivElement>(null)
  const chatContainerRef = useRef<HTMLDivElement>(null)
  const abortControllerRef = useRef<AbortController | null>(null)

  const { data: mrptInv } = useApi(() => getInventory('mrpt'), [])
  const { data: mtfsInv } = useApi(() => getInventory('mtfs'), [])
  const currentInv = selectedStrategy === 'mrpt' ? mrptInv : mtfsInv
  const activePairs = currentInv ? Object.entries(currentInv.pairs || {}).filter(([, p]: any) => (p as any).direction !== null) : []

  const models = modelList as LLMModel[]
  const currentModel = models.find(m => m.id === languageModel.model) || models[0]
  const isMultiModal = currentModel?.multiModal ?? false

  // Close runtime dropdown on outside click
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (runtimeDropdownRef.current && !runtimeDropdownRef.current.contains(e.target as Node)) {
        setRuntimeDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  // Reset messages when chatKey changes (new chat or switching chat)
  useEffect(() => {
    setMessages(initialMessages || [])
    setInput('')
    setIsLoading(false)
    setIsErrored(false)
    setCurrentStanseAgent(null)
  }, [chatKey])

  // Auto-scroll: to top when no messages (welcome), to bottom when chatting
  useEffect(() => {
    if (chatContainerRef.current) {
      if (messages.length === 0) {
        chatContainerRef.current.scrollTop = 0
      } else {
        chatContainerRef.current.scrollTop = chatContainerRef.current.scrollHeight
      }
    }
  }, [messages, isLoading])

  // Notify parent when messages change (for persistence)
  useEffect(() => {
    if (messages.length > 0) {
      onMessagesChange?.(messages)
    }
  }, [messages])

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault()
    if (!input.trim() || isLoading) return

    // Notify parent on first message for chat history
    if (messages.length === 0 && onFirstMessage) {
      onFirstMessage(input.trim())
    }

    const userMessage: Message = {
      role: 'user',
      content: [{ type: 'text', text: input.trim() }],
    }

    setMessages(prev => [...prev, userMessage])
    setInput('')
    setIsLoading(true)
    setIsErrored(false)

    // Route to VPS via Firestore when cloud mode and message mentions SomeoClaw
    if (agentMode === 'cloud' && input.trim().includes('SomeoClaw')) {
      const docRef = await addDoc(collection(db, 'bot_commands'), {
        command: input.trim(),
        uid: session?.user?.id ?? 'anonymous',
        status: 'pending',
        reply: null,
        createdAt: serverTimestamp(),
        repliedAt: null,
      })

      setIsLoading(true)
      const unsub = onSnapshot(docRef, (snap) => {
        const data = snap.data()
        if (data?.reply) {
          const assistantMessage: Message = {
            role: 'assistant',
            content: [{ type: 'text', text: data.reply }],
          }
          setMessages(prev => [...prev, assistantMessage])
          setIsLoading(false)
          unsub()
        } else if (data?.status === 'error') {
          setIsErrored(true)
          setErrorMessage(data.error || 'VPS command failed')
          setIsLoading(false)
          unsub()
        }
      })
      return
    }

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

      const response = await fetch(`${API_BASE}${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...apiHeaders() },
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
          const sandboxRes = await fetch(`${API_BASE}/api/sandbox`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...apiHeaders() },
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
    <div className="flex flex-col h-full relative" style={{ background: 'var(--color-bg)' }}>
      {/* Header */}
      <div className="h-14 flex items-center justify-between px-6 shrink-0" style={{ borderBottom: '3px solid #111', background: '#fff' }}>
        <div className="flex items-center gap-3">
          <span style={{ fontSize: '10px', letterSpacing: '.12em', textTransform: 'uppercase', color: '#888', fontFamily: 'var(--font-mono)' }}>{t('chat.currentRuntime')}</span>
          <div className="relative" ref={runtimeDropdownRef}>
            <button
              onClick={() => setRuntimeDropdownOpen(prev => !prev)}
              className="flex items-center gap-2 px-3 py-1"
              style={{
                background: agentMode === 'cloud' ? '#111' : '#fff',
                border: '2px solid #111',
                boxShadow: 'var(--shadow-pixel-sm)',
                cursor: 'pointer',
              }}
            >
              {agentMode === 'cloud' ? (
                <Cloud className="w-3.5 h-3.5" style={{ color: '#fff' }} />
              ) : (
                <Laptop className="w-3.5 h-3.5" style={{ color: '#111' }} />
              )}
              <span style={{ fontSize: '10px', fontFamily: 'var(--font-mono)', color: agentMode === 'cloud' ? '#fff' : '#111', textTransform: 'uppercase', letterSpacing: '.06em', fontWeight: 700 }}>
                {agentMode === 'cloud' ? t('chat.cloudVpsLabel') : t('chat.localConnectedLabel')}
              </span>
              <ChevronDown className="w-3 h-3" style={{ color: agentMode === 'cloud' ? '#fff' : '#111', transition: 'transform .15s', transform: runtimeDropdownOpen ? 'rotate(180deg)' : 'none' }} />
            </button>
            {runtimeDropdownOpen && (
              <div className="absolute top-full left-0 mt-1 z-50 animate-slide-in" style={{ background: '#fff', border: '2px solid #111', boxShadow: 'var(--shadow-pixel)', minWidth: '100%' }}>
                <button
                  onClick={() => { setRuntimeDropdownOpen(false); onConnectClick?.(); }}
                  className="w-full flex items-center gap-2 px-3 py-2"
                  style={{ cursor: 'pointer', background: '#fff', border: 'none', fontFamily: 'var(--font-mono)', fontSize: '10px', fontWeight: 700, letterSpacing: '.06em', textTransform: 'uppercase', color: '#333', transition: 'all .1s' }}
                  onMouseEnter={e => { e.currentTarget.style.background = '#111'; e.currentTarget.style.color = '#fff'; }}
                  onMouseLeave={e => { e.currentTarget.style.background = '#fff'; e.currentTarget.style.color = '#333'; }}
                >
                  <Laptop className="w-3.5 h-3.5" />
                  Open Claw
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Messages or Welcome */}
      <div ref={chatContainerRef} className="flex-1 overflow-y-auto px-6 pt-4 pb-2 flex flex-col items-center">
        <div className="w-full max-w-3xl flex flex-col gap-4 pb-2">
          {!hasMessages ? (
            <>
              <div className="flex flex-col items-center justify-center py-6 gap-4">
                {/* Stanse-style icon — black box with pixel shadow */}
                <div className="w-14 h-14 flex items-center justify-center" style={{ background: '#111', border: '2px solid #111', boxShadow: 'var(--shadow-pixel)' }}>
                  <Terminal className="w-7 h-7" style={{ color: '#fff', opacity: 1 }} />
                </div>
                <h2 style={{ fontFamily: 'var(--font-pixel)', fontSize: '28px', color: '#111', letterSpacing: '.04em', textTransform: 'uppercase', lineHeight: 1 }}>{t('chat.welcomeTitle')}</h2>
                <p style={{ fontSize: '12px', color: '#555', textAlign: 'center', maxWidth: '28rem', fontFamily: 'var(--font-mono)' }}>{t('chat.welcomeDesc')}</p>
              </div>

              {/* Active Pairs card — Stanse PixelCard */}
              <div className="p-4 relative" style={{ background: '#fff', border: '3px solid #111', boxShadow: 'var(--shadow-pixel-sm)' }}>
                {/* Corner dots */}
                <div style={{ position: 'absolute', top: -2, left: -2, width: 6, height: 6, background: '#111' }} />
                <div style={{ position: 'absolute', top: -2, right: -2, width: 6, height: 6, background: '#111' }} />
                <div style={{ position: 'absolute', bottom: -2, left: -2, width: 6, height: 6, background: '#111' }} />
                <div style={{ position: 'absolute', bottom: -2, right: -2, width: 6, height: 6, background: '#111' }} />
                <div className="flex items-center justify-between mb-3">
                  <div style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '.14em', textTransform: 'uppercase', color: '#111', fontFamily: 'var(--font-mono)' }}>{t('chat.activePairs')} <span style={{ color: '#00cc66' }}>({activePairs.length})</span></div>
                  {/* Strategy toggle */}
                  <div className="flex overflow-hidden" style={{ border: '2px solid #111' }}>
                    <button
                      onClick={() => setSelectedStrategy('mrpt')}
                      style={{
                        padding: '3px 12px',
                        fontSize: '10px',
                        fontFamily: 'var(--font-mono)',
                        fontWeight: 700,
                        letterSpacing: '.06em',
                        textTransform: 'uppercase',
                        transition: 'all .1s',
                        background: selectedStrategy === 'mrpt' ? '#111' : '#fff',
                        color: selectedStrategy === 'mrpt' ? '#fff' : '#555',
                        border: 'none',
                        cursor: 'pointer',
                      }}
                    >MRPT</button>
                    <button
                      onClick={() => setSelectedStrategy('mtfs')}
                      style={{
                        padding: '3px 12px',
                        fontSize: '10px',
                        fontFamily: 'var(--font-mono)',
                        fontWeight: 700,
                        letterSpacing: '.06em',
                        textTransform: 'uppercase',
                        transition: 'all .1s',
                        background: selectedStrategy === 'mtfs' ? '#111' : '#fff',
                        color: selectedStrategy === 'mtfs' ? '#fff' : '#555',
                        borderLeft: '2px solid #111',
                        cursor: 'pointer',
                      }}
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
                <button onClick={() => setActiveArtifact({ type: 'pnl_report', title: 'PnL Report' })} className="flex items-center gap-2 p-2.5 rounded-xl bg-[var(--bg-secondary)] border border-[var(--border-subtle)] hover:bg-[var(--bg-tertiary)] transition-colors text-sm text-[var(--text-primary)]">
                  <Activity className="w-4 h-4 text-[var(--accent-primary)]" /> {t('chat.btnPnlReport')}
                </button>
              </div>
            </>
          ) : (
            messages.map((msg, idx) => (
              <div key={idx} className={msg.role === 'user' ? 'message-user' : 'message-ai'}>
                {msg.role === 'assistant' && (
                  <div className="w-8 h-8 flex items-center justify-center shrink-0" style={{ background: '#111', border: '2px solid #111', boxShadow: 'var(--shadow-pixel-sm)' }}>
                    <Terminal className="w-4 h-4" style={{ color: '#fff', opacity: 1 }} />
                  </div>
                )}
                <div className={msg.role === 'user' ? '' : 'message-content w-full'}>
                  {msg.content.map((c, ci) => {
                    if (c.type === 'text') return (
                      <div key={ci} className={`text-sm leading-relaxed ${msg.role === 'user' ? 'text-white whitespace-pre-wrap' : 'text-[var(--text-primary)]'}`}>
                        {msg.role === 'assistant' ? renderMarkdown(c.text) : c.text}
                      </div>
                    )
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
              <div className="w-8 h-8 flex items-center justify-center shrink-0 shimmer" style={{ background: '#e5e5e5', border: '2px solid #111' }}>
                <Terminal className="w-4 h-4" style={{ color: '#888', opacity: 1 }} />
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
