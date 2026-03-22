import { LLMModelConfig } from '../lib/models'
import { Settings2, ExternalLink } from 'lucide-react'
import { useState, useRef, useEffect } from 'react'
import { useTranslation } from 'react-i18next'

export function ChatSettings({
  apiKeyConfigurable,
  baseURLConfigurable,
  languageModel,
  onLanguageModelChange,
  useMorphApply,
  onUseMorphApplyChange,
}: {
  apiKeyConfigurable: boolean
  baseURLConfigurable: boolean
  languageModel: LLMModelConfig
  onLanguageModelChange: (model: LLMModelConfig) => void
  useMorphApply: boolean
  onUseMorphApplyChange: (enabled: boolean) => void
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="p-1.5 rounded-md text-[var(--text-muted)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-secondary)] transition-colors"
        title={t('chatSettings.title')}
      >
        <Settings2 className="h-4 w-4" />
      </button>

      {open && (
        <div className="absolute bottom-full mb-2 right-0 w-[260px] rounded-lg bg-[var(--bg-primary)] border border-[var(--border-subtle)] shadow-xl z-[100] p-3 space-y-3">
          {/* Morph Apply Toggle */}
          <div className="flex items-center justify-between">
            <label className="text-sm text-[var(--text-primary)]">{t('chatSettings.morphApply')}</label>
            <button
              type="button"
              onClick={() => onUseMorphApplyChange(!useMorphApply)}
              className={`relative inline-flex w-11 h-6 rounded-full transition-colors flex-shrink-0 ${useMorphApply ? 'bg-[var(--accent-primary)]' : 'bg-[var(--bg-tertiary)]'}`}
            >
              <span className={`inline-block w-5 h-5 rounded-full bg-white shadow transition-transform duration-200 ease-in-out self-center ${useMorphApply ? 'translate-x-5' : 'translate-x-0.5'}`} />
            </button>
          </div>
          <a className="text-xs text-[var(--text-muted)] flex items-center gap-1 hover:underline" target="_blank" href="https://morphllm.com" rel="noreferrer">
            {t('chatSettings.learnMorph')} <ExternalLink className="h-3 w-3" />
          </a>

          <div className="border-t border-[var(--border-subtle)]" />

          {/* API Key */}
          {apiKeyConfigurable && (
            <>
              <div className="space-y-1">
                <label className="text-xs text-[var(--text-secondary)]">{t('chatSettings.apiKey')}</label>
                <input type="password" placeholder="Auto" defaultValue={languageModel.apiKey}
                  onChange={(e) => onLanguageModelChange({ apiKey: e.target.value || undefined })}
                  className="w-full px-2 py-1.5 rounded-md bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-xs text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)]" />
              </div>
              <div className="border-t border-[var(--border-subtle)]" />
            </>
          )}

          {/* Base URL */}
          {baseURLConfigurable && (
            <>
              <div className="space-y-1">
                <label className="text-xs text-[var(--text-secondary)]">{t('chatSettings.baseURL')}</label>
                <input type="text" placeholder="Auto" defaultValue={languageModel.baseURL}
                  onChange={(e) => onLanguageModelChange({ baseURL: e.target.value || undefined })}
                  className="w-full px-2 py-1.5 rounded-md bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-xs text-[var(--text-primary)] focus:outline-none focus:border-[var(--accent-primary)]" />
              </div>
              <div className="border-t border-[var(--border-subtle)]" />
            </>
          )}

          {/* Parameters */}
          <div className="space-y-2">
            <span className="text-xs font-medium text-[var(--text-primary)]">{t('chatSettings.parameters')}</span>
            {[
              { label: t('chatSettings.outputTokens'), key: 'maxTokens', min: 50, max: 10000, step: 1 },
              { label: t('chatSettings.temperature'), key: 'temperature', min: 0, max: 5, step: 0.01 },
              { label: 'Top P', key: 'topP', min: 0, max: 1, step: 0.01 },
              { label: 'Top K', key: 'topK', min: 0, max: 500, step: 1 },
              { label: t('chatSettings.frequencyPenalty'), key: 'frequencyPenalty', min: 0, max: 2, step: 0.01 },
              { label: t('chatSettings.presencePenalty'), key: 'presencePenalty', min: 0, max: 2, step: 0.01 },
            ].map(({ label, key, min, max, step }) => (
              <div key={key} className="flex items-center gap-2">
                <span className="text-xs text-[var(--text-muted)] flex-1">{label}</span>
                <input
                  type="number"
                  defaultValue={(languageModel as any)[key]}
                  min={min} max={max} step={step}
                  placeholder="Auto"
                  onChange={(e) => onLanguageModelChange({ [key]: parseFloat(e.target.value) || undefined })}
                  className="w-[72px] h-6 rounded-md bg-[var(--bg-secondary)] border border-[var(--border-subtle)] text-[10px] text-center text-[var(--text-primary)] tabular-nums focus:outline-none focus:border-[var(--accent-primary)]"
                />
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
