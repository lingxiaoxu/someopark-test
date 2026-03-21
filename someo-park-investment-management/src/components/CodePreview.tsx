import { StanseAgentSchema } from '../lib/schema'
import { ExecutionResult, ExecutionResultWeb } from '../lib/types'
import { DeepPartial } from 'ai'
import { ChevronsRight, LoaderCircle, Copy, Check } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

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

  if (!stanseAgent) return null

  const isWebResult = result && 'url' in result

  const handleCopy = () => {
    if (stanseAgent.code) {
      navigator.clipboard.writeText(stanseAgent.code)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
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

        <button onClick={handleCopy} className="p-1.5 rounded-md text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors">
          {copied ? <Check className="h-4 w-4 text-[var(--success)]" /> : <Copy className="h-4 w-4" />}
        </button>
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
