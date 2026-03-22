import { StanseAgentSchema } from '../lib/schema'
import { ExecutionResult, ExecutionResultWeb } from '../lib/types'
import { DeepPartial } from 'ai'
import { ChevronsRight, LoaderCircle, Copy, Check, Rocket } from 'lucide-react'
import { useState, useRef, useEffect } from 'react'
import { useTranslation } from 'react-i18next'

type Duration = '30m' | '1h' | '3h' | '6h' | '1d'

const DURATION_OPTIONS: { value: Duration; label: string }[] = [
  { value: '30m', label: '30 分钟' },
  { value: '1h', label: '1 小时' },
  { value: '3h', label: '3 小时' },
  { value: '6h', label: '6 小时' },
  { value: '1d', label: '1 天' },
]

export function CodePreview({
  stanseAgent,
  result,
  isChatLoading,
  isPreviewLoading,
  onClose,
}: {
  stanseAgent?: DeepPartial<StanseAgentSchema>
  result?: ExecutionResult
  isChatLoading: boolean
  isPreviewLoading: boolean
  onClose: () => void
}) {
  const { t } = useTranslation()
  const [selectedTab, setSelectedTab] = useState<'code' | 'preview'>('code')
  const [copied, setCopied] = useState(false)
  const [deployOpen, setDeployOpen] = useState(false)
  const [selectedDuration, setSelectedDuration] = useState<Duration | null>(null)
  const [isDeploying, setIsDeploying] = useState(false)
  const [deployed, setDeployed] = useState(false)
  const deployRef = useRef<HTMLDivElement>(null)

  const isWebResult = result && 'url' in result

  // Reset deploy state when result changes
  useEffect(() => {
    setDeployed(false)
    setSelectedDuration(null)
  }, [result])

  // Close deploy dropdown when clicking outside
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (deployRef.current && !deployRef.current.contains(e.target as Node)) {
        setDeployOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  if (!stanseAgent) return null

  const handleCopy = () => {
    if (stanseAgent.code) {
      navigator.clipboard.writeText(stanseAgent.code)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  const handleDeploy = async () => {
    if (!selectedDuration || !result?.sbxId) return
    setIsDeploying(true)
    try {
      await fetch('/api/publish', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sbxId: result.sbxId, duration: selectedDuration }),
      })
      setDeployed(true)
      setDeployOpen(false)
      // Open the sandbox URL in a new tab immediately after deploy
      if (isWebResult) {
        window.open((result as ExecutionResultWeb).url, '_blank')
      }
    } catch (e) {
      console.error('Deploy failed:', e)
    } finally {
      setIsDeploying(false)
    }
  }

  return (
    <div className="h-full flex flex-col bg-[var(--bg-secondary)] border-l border-[var(--border-subtle)]">
      {/* Header */}
      <div className="flex items-center justify-between p-3 border-b border-[var(--border-subtle)]">
        <button onClick={onClose} className="p-1.5 rounded-md text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors">
          <ChevronsRight className="h-4 w-4" />
        </button>

        <div className="flex items-center gap-1 bg-[var(--bg-tertiary)] rounded-lg p-0.5">
          <button
            onClick={() => setSelectedTab('code')}
            className={`flex items-center gap-1 px-3 py-1 rounded-md text-xs transition-colors ${selectedTab === 'code' ? 'bg-[var(--bg-primary)] text-[var(--text-primary)] shadow-sm' : 'text-[var(--text-muted)]'}`}
          >
            {isChatLoading && <LoaderCircle className="h-3 w-3 animate-spin" />}
            {t('codePreview.code')}
          </button>
          <button
            onClick={() => result && setSelectedTab('preview')}
            disabled={!result}
            className={`flex items-center gap-1 px-3 py-1 rounded-md text-xs transition-colors disabled:opacity-30 ${selectedTab === 'preview' ? 'bg-[var(--bg-primary)] text-[var(--text-primary)] shadow-sm' : 'text-[var(--text-muted)]'}`}
          >
            {t('codePreview.preview')}
            {isPreviewLoading && <LoaderCircle className="h-3 w-3 animate-spin" />}
          </button>
        </div>

        <div className="flex items-center gap-1">
          {/* Deploy to E2B button — only for web results */}
          {isWebResult && (
            <div className="relative" ref={deployRef}>
              <button
                onClick={() => !deployed && setDeployOpen((prev) => !prev)}
                disabled={deployed}
                className={`flex items-center gap-1 px-2 py-1 rounded-md text-xs transition-colors disabled:cursor-not-allowed ${deployed ? 'text-[var(--success)] opacity-60' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]'}`}
                title={deployed ? 'Already deployed' : 'Deploy to E2B'}
              >
                <Rocket className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">{deployed ? 'Deployed' : 'Deploy'}</span>
              </button>

              {deployOpen && (
                <div className="absolute right-0 top-8 z-50 w-56 bg-[var(--bg-primary)] border border-[var(--border-subtle)] rounded-xl shadow-xl p-3 flex flex-col gap-2">
                  <div className="text-xs font-semibold text-[var(--text-primary)]">Deploy to E2B</div>
                  <div className="text-xs text-[var(--text-muted)]">保持沙盒运行并通过链接公开访问，按使用时长计费。</div>
                  <div className="flex flex-col gap-1">
                    {DURATION_OPTIONS.map((opt) => (
                      <button
                        key={opt.value}
                        onClick={() => setSelectedDuration(opt.value)}
                        className={`text-left px-2 py-1.5 rounded-md text-xs transition-colors ${selectedDuration === opt.value ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'}`}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                  <button
                    onClick={handleDeploy}
                    disabled={!selectedDuration || isDeploying}
                    className="mt-1 w-full py-1.5 rounded-md text-xs font-medium bg-[var(--accent)] text-white disabled:opacity-40 flex items-center justify-center gap-1 transition-opacity"
                  >
                    {isDeploying && <LoaderCircle className="h-3 w-3 animate-spin" />}
                    {isDeploying ? '部署中...' : '确认部署'}
                  </button>
                </div>
              )}
            </div>
          )}

          <button onClick={handleCopy} className="p-1.5 rounded-md text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors">
            {copied ? <Check className="h-4 w-4 text-[var(--success)]" /> : <Copy className="h-4 w-4" />}
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {selectedTab === 'code' && stanseAgent.code && (
          <div className="p-4">
            {stanseAgent.file_path && (
              <div className="text-xs text-[var(--text-muted)] mb-2 font-mono">{stanseAgent.file_path}</div>
            )}
            <pre className="text-xs font-mono text-[var(--text-primary)] whitespace-pre-wrap break-words leading-relaxed">
              {stanseAgent.code}
            </pre>
          </div>
        )}

        {selectedTab === 'preview' && result && (
          <div className="h-full">
            {isWebResult ? (
              <iframe
                src={(result as ExecutionResultWeb).url}
                className="w-full h-full border-0"
                title="Preview"
                sandbox="allow-scripts allow-same-origin allow-forms"
              />
            ) : (
              <div className="p-4 space-y-3">
                {'stdout' in result && result.stdout && result.stdout.length > 0 && (
                  <div>
                    <div className="text-xs font-medium text-[var(--text-secondary)] mb-1">stdout</div>
                    <pre className="text-xs font-mono text-[var(--text-primary)] bg-[var(--bg-tertiary)] p-3 rounded-lg whitespace-pre-wrap">
                      {result.stdout.join('\n')}
                    </pre>
                  </div>
                )}
                {'stderr' in result && result.stderr && result.stderr.length > 0 && (
                  <div>
                    <div className="text-xs font-medium text-red-400 mb-1">stderr</div>
                    <pre className="text-xs font-mono text-red-400 bg-red-400/10 p-3 rounded-lg whitespace-pre-wrap">
                      {result.stderr.join('\n')}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
