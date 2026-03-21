import { LLMModelConfig } from '../lib/models'
import { Templates } from '../lib/templates'
import { Sparkles, ChevronDown } from 'lucide-react'
import React, { useState, useRef, useEffect } from 'react'
import { useTranslation } from 'react-i18next'

export type LLMModel = {
  id: string
  name: string
  provider: string
  providerId: string
  multiModal?: boolean
}

function Dropdown({ trigger, children, open, setOpen }: { trigger: React.ReactNode, children: React.ReactNode, open: boolean, setOpen: (o: boolean) => void }) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [setOpen])

  return (
    <div className="relative" ref={ref}>
      <div onClick={() => setOpen(!open)}>{trigger}</div>
      {open && (
        <div className="absolute bottom-full mb-1 left-0 min-w-[200px] max-h-[300px] overflow-y-auto rounded-lg bg-[var(--bg-primary)] border border-[var(--border-subtle)] shadow-xl z-50">
          {children}
        </div>
      )}
    </div>
  )
}

export function ChatPicker({
  templates,
  selectedTemplate,
  onSelectedTemplateChange,
  models,
  languageModel,
  onLanguageModelChange,
}: {
  templates: Templates
  selectedTemplate: string
  onSelectedTemplateChange: (template: string) => void
  models: LLMModel[]
  languageModel: LLMModelConfig
  onLanguageModelChange: (config: LLMModelConfig) => void
}) {
  const { t } = useTranslation()
  const [templateOpen, setTemplateOpen] = useState(false)
  const [modelOpen, setModelOpen] = useState(false)

  const selectedModelName = models.find(m => m.id === languageModel.model)?.name || t('chatPicker.selectModel')

  // Group models by provider
  const grouped = models.reduce((acc, m) => {
    if (!acc[m.provider]) acc[m.provider] = []
    acc[m.provider].push(m)
    return acc
  }, {} as Record<string, LLMModel[]>)

  return (
    <div className="flex items-center gap-2">
      <Dropdown
        open={templateOpen}
        setOpen={setTemplateOpen}
        trigger={
          <button type="button" className="flex items-center gap-1 px-2 py-1 rounded-md text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] transition-colors">
            <Sparkles className="w-3 h-3" />
            <span>{selectedTemplate === 'auto' ? 'Auto' : (templates as any)[selectedTemplate]?.name || selectedTemplate}</span>
            <ChevronDown className="w-3 h-3" />
          </button>
        }
      >
        <div className="p-1">
          <div className="px-2 py-1 text-[10px] font-medium text-[var(--text-muted)] uppercase">{t('chatPicker.persona')}</div>
          <button onClick={() => { onSelectedTemplateChange('auto'); setTemplateOpen(false) }}
            className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-xs ${selectedTemplate === 'auto' ? 'bg-[var(--bg-tertiary)] text-[var(--text-primary)]' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)]'}`}>
            <Sparkles className="w-3.5 h-3.5 text-[var(--text-muted)]" /> Auto
          </button>
          {Object.entries(templates).map(([id, tmpl]) => (
            <button key={id} onClick={() => { onSelectedTemplateChange(id); setTemplateOpen(false) }}
              className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-xs ${selectedTemplate === id ? 'bg-[var(--bg-tertiary)] text-[var(--text-primary)]' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)]'}`}>
              {tmpl.name}
            </button>
          ))}
        </div>
      </Dropdown>

      <Dropdown
        open={modelOpen}
        setOpen={setModelOpen}
        trigger={
          <button type="button" className="flex items-center gap-1 px-2 py-1 rounded-md text-xs text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] transition-colors">
            <span>{selectedModelName}</span>
            <ChevronDown className="w-3 h-3" />
          </button>
        }
      >
        <div className="p-1">
          {Object.entries(grouped).map(([provider, providerModels]) => (
            <div key={provider}>
              <div className="px-2 py-1 text-[10px] font-medium text-[var(--text-muted)] uppercase">{provider}</div>
              {providerModels.map((m) => (
                <button key={m.id} onClick={() => { onLanguageModelChange({ model: m.id }); setModelOpen(false) }}
                  className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-xs ${languageModel.model === m.id ? 'bg-[var(--bg-tertiary)] text-[var(--text-primary)]' : 'text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)]'}`}>
                  {m.name}
                </button>
              ))}
            </div>
          ))}
        </div>
      </Dropdown>
    </div>
  )
}
